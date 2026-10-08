import asyncio
import hashlib
import json
import secrets
import socket

import httpx
import pytest_asyncio
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from lixiang_mcp.config import Settings
from lixiang_mcp.server import create_app


@pytest_asyncio.fixture
async def http_server(tmp_path, actor):
    token, other_token, readonly_token = (secrets.token_urlsafe(32) for _ in range(3))
    other = actor.model_copy(
        update={
            "subject": "other-user",
            "account_id": "other-account",
            "vehicle_ids": frozenset({"other-l6"}),
        }
    )
    readonly = actor.model_copy(
        update={"subject": "readonly-user", "scopes": frozenset({"vehicle:read"})}
    )
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "credentials": [
                    {
                        "token_sha256": hashlib.sha256(t.encode()).hexdigest(),
                        "principal": p.model_dump(mode="json"),
                    }
                    for t, p in [(token, actor), (other_token, other), (readonly_token, readonly)]
                ]
            }
        )
    )
    settings = Settings(auth_file=auth_file, database=tmp_path / "ops.sqlite", enable_control=True)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(create_app(settings), log_config=None, access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        assert server.started
        yield f"http://127.0.0.1:{sock.getsockname()[1]}", token, other_token, readonly_token
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()


async def test_sdk_full_lifecycle_and_background_operation(http_server):
    base, token, _, _ = http_server
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as client:
        async with streamable_http_client(base + "/mcp", http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {t.name for t in tools.tools} == {
                    "list_vehicles",
                    "get_vehicle_capabilities",
                    "get_vehicle_state",
                    "get_charging_status",
                    "get_connection_status",
                    "get_vehicle_location",
                    "set_climate",
                    "get_operation",
                }
                listed = await session.call_tool("list_vehicles")
                assert len(listed.structuredContent["vehicles"]) == 3
                state = await session.call_tool("get_vehicle_state", {"vehicle_id": "demo-l6"})
                assert not state.isError and state.structuredContent["simulated"]
                assert "latitude" not in str(state.structuredContent)
                for name in (
                    "get_connection_status",
                    "get_charging_status",
                    "get_vehicle_capabilities",
                    "get_vehicle_location",
                ):
                    response = await session.call_tool(name, {"vehicle_id": "demo-l6"})
                    assert not response.isError and "error" not in response.structuredContent
                args = {
                    "command": {
                        "vehicle_id": "demo-l6",
                        "enabled": True,
                        "temperature_c": 23,
                        "idempotency_key": "sdk-test-001",
                    }
                }
                submitted = await session.call_tool("set_climate", args)
                op_id = submitted.structuredContent["operation_id"]
                assert submitted.structuredContent["phase"] == "submitted"
                for _ in range(100):
                    op = await session.call_tool("get_operation", {"operation_id": op_id})
                    if op.structuredContent["phase"] == "vehicle_confirmed":
                        break
                    await asyncio.sleep(0.01)
                assert op.structuredContent["phase"] == "vehicle_confirmed"
                duplicate = await session.call_tool("set_climate", args)
                assert duplicate.structuredContent["operation_id"] == op_id
                invalid = await session.call_tool(
                    "set_climate", {"command": {**args["command"], "confirmed": True}}
                )
                assert invalid.isError
                unknown = await session.call_tool("send_command_raw", {"command": "anything"})
                assert unknown.isError


async def rpc(client, base, token, name, args=None):
    response = await client.post(
        base + "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args or {}},
        },
    )
    assert response.status_code == 200
    return response.json()["result"]["structuredContent"]


async def test_http_isolation_scope_and_forged_identity(http_server):
    base, token, other, readonly = http_server
    async with httpx.AsyncClient() as client:
        # Same TCP pool and forged identity headers cannot change credential-bound principal.
        client.headers["X-User-Id"] = "demo-user"
        client.headers["X-Account-Id"] = "demo-account"
        a, b = await asyncio.gather(
            rpc(client, base, token, "list_vehicles"), rpc(client, base, other, "list_vehicles")
        )
        assert a["vehicles"][0]["vehicle_id"] == "demo-l6"
        assert b["vehicles"][0]["vehicle_id"] == "other-l6"
        denied = await rpc(client, base, other, "get_vehicle_state", {"vehicle_id": "demo-l6"})
        assert denied["error"]["code"] == "vehicle_not_found"
        location = await rpc(
            client, base, readonly, "get_vehicle_location", {"vehicle_id": "demo-l6"}
        )
        assert location["error"]["code"] == "permission_denied"
        submitted = await rpc(
            client,
            base,
            token,
            "set_climate",
            {
                "command": {
                    "vehicle_id": "demo-l6",
                    "enabled": False,
                    "idempotency_key": "isolation-test",
                }
            },
        )
        denied_op = await rpc(
            client, base, other, "get_operation", {"operation_id": submitted["operation_id"]}
        )
        assert denied_op["error"]["code"] == "operation_not_found"


async def test_http_auth_origin_host_and_size_limits(http_server):
    base, token, _, _ = http_server
    async with httpx.AsyncClient() as client:
        assert (await client.get(base + "/healthz")).status_code == 200
        assert (await client.post(base + "/mcp")).status_code == 401
        assert (
            await client.post(
                base + "/mcp",
                headers={"Authorization": "Bearer unrelated-oauth-token-not-accepted-here"},
            )
        ).status_code == 401
        for headers in [{"Host": "attacker.invalid"}, {"Origin": "https://attacker.invalid"}]:
            headers["Authorization"] = f"Bearer {token}"
            assert (await client.post(base + "/mcp", headers=headers)).status_code == 403
        assert (
            await client.post(
                base + "/mcp", headers={"Authorization": f"Bearer {token}"}, content=b"x" * 16385
            )
        ).status_code == 413
