import asyncio
import json
import time
from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError

from lixiang_mcp.cloud.api import MESH_SCOPE, SEND, VSS
from lixiang_mcp.cloud.auth import CachedToken
from lixiang_mcp.cloud.backend import CloudBackend, usable_relation
from lixiang_mcp.cloud.config import CloudConfig, load_cloud_config
from lixiang_mcp.cloud.signals import LOCATION_PATH, PATHS, location_from_vss, state_from_vss
from lixiang_mcp.cloud.transport import API, CloudHTTP, ProtocolError
from lixiang_mcp.models import ClimateCommand, Phase, Principal, ServiceError, now
from lixiang_mcp.protocol import cloud_result
from lixiang_mcp.service import VehicleService
from lixiang_mcp.store import OperationStore


def actor():
    return Principal(
        subject="user-a",
        account_id="account-a",
        vehicle_ids={"car-0-0", "car-0-1"},
        scopes={"vehicle:read", "vehicle:location", "vehicle:climate"},
    )


def command(vehicle="car-0-0", key="contract-0001"):
    return ClimateCommand(vehicle_id=vehicle, enabled=True, temperature_c=23, idempotency_key=key)


async def final(service, op):
    for _ in range(200):
        result = await service.operation(actor(), op.operation_id)
        if result.phase in (Phase.VEHICLE_CONFIRMED, Phase.FAILED, Phase.UNKNOWN):
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("did not settle")


async def test_complete_protocol_through_business_service(config, cloud, tmp_path):
    backend = CloudBackend(config, transport_factory=cloud.transport)
    service = VehicleService(
        backend, OperationStore(tmp_path / "ops.sqlite"), enable_control=True, command_timeout=2
    )
    try:
        assert [v.vehicle_id for v in await service.vehicles(actor())] == ["car-0-0", "car-0-1"]
        state = await service.state(actor(), "car-0-0")
        assert state.signals["battery_percent"].value == 40
        assert not any(LOCATION_PATH in r.content.decode() for r in cloud.requests)
        location = await service.location(actor(), "car-0-0")
        assert location.latitude == location.longitude == 0
        ops = await asyncio.gather(*(service.submit(actor(), command()) for _ in range(10)))
        assert len({op.operation_id for op in ops}) == 1
        result = await final(service, ops[0])
        assert result.phase == Phase.VEHICLE_CONFIRMED
        assert [e.phase for e in result.events] == [
            Phase.SUBMITTED,
            Phase.RUNNING,
            Phase.CLOUD_COMPLETED,
            Phase.VEHICLE_CONFIRMED,
        ]
        assert cloud.login_count == 1 and len(cloud.sends) == 1
        sent = cloud.sends[0]
        assert sent["cmdKey"] == "remoteVehACSmartControl" and sent["domain"] == "xcu"
        assert sent["jobExpire"] == sent["expire"] == 900
        assert sent["cmdData"] == {
            "acCtrlType": "frtACSw",
            "acCtrlValue": "ON",
            "acCountdownTimer": "15",
            "acCtrlTemp": 23,
        }
        assert abs(sent["expireAt"] - int(time.time() * 1000) - 900000) < 5000
        vat_scopes = [s for _, aud, s in cloud.scope_count if aud == config.profile.vat_audience]
        assert vat_scopes == [
            "remoteVehACSmartControl:" + config.vehicles[0].vin.get_secret_value()
        ]
        assert MESH_SCOPE not in ("remote-wakeup:wakeup",)
        for private in [a.device_id.get_secret_value() for a in config.accounts] + [
            v.vin.get_secret_value() for v in config.vehicles
        ]:
            assert private not in state.model_dump_json() + result.model_dump_json()
    finally:
        await service.close()


async def test_cookie_token_cache_refresh_and_vehicle_scope_isolation(config, cloud):
    backend = CloudBackend(config, transport_factory=cloud.transport)
    a = backend._apis["account-a"].auth
    b = backend._apis["account-b"].auth
    try:
        await asyncio.gather(
            *(a.scope("aud-a", "read") for _ in range(20)), b.scope("aud-a", "read")
        )
        assert cloud.login_count == 2 and all(v == 1 for v in cloud.scope_count.values())
        a._main = CachedToken("expired-main", 0)
        a._scopes.clear()
        await asyncio.gather(*(a.scope("aud-a", "read") for _ in range(20)))
        assert cloud.refresh_count == 1 and a._refresh == "synthetic-refresh-1"
        assert cloud.login_count == 2
        cloud.scope_error_once = True
        await a.scope("aud-a", "new-scope")
        assert cloud.login_count == 3  # refresh did not stand in for a lost cookie session
        await asyncio.gather(a.scope("aud-a", "car-one"), a.scope("aud-a", "car-two"))
        assert ("aud-a", "car-one") in a._scopes and ("aud-a", "car-two") in a._scopes
        old = a._scopes[("aud-a", "car-one")].value
        a._scopes[("aud-a", "car-one")] = CachedToken(
            "newer-synthetic-token", time.monotonic() + 300
        )
        await a.invalidate("aud-a", "car-one", old)
        assert a._scopes[("aud-a", "car-one")].value == "newer-synthetic-token"
    finally:
        await backend.close()


@pytest.mark.parametrize("status", [401, 403, 500])
async def test_write_http_errors_never_replay(config, cloud, tmp_path, status):
    cloud.send_status = status
    backend = CloudBackend(config, transport_factory=cloud.transport)
    service = VehicleService(
        backend, OperationStore(tmp_path / "ops.sqlite"), enable_control=True, command_timeout=2
    )
    try:
        result = await final(service, await service.submit(actor(), command()))
        assert result.phase == Phase.UNKNOWN and len(cloud.sends) == 1
        assert (await service.submit(actor(), command())).operation_id == result.operation_id
        with pytest.raises(ServiceError, match="vehicle_has_unknown_operation"):
            await service.submit(actor(), command(key="second-contract"))
    finally:
        await service.close()


@pytest.mark.parametrize("fault", ["send_timeout", "send_missing_receipt", "stale"])
async def test_lost_response_missing_receipt_and_stale_confirmation(config, cloud, tmp_path, fault):
    setattr(cloud, fault, True)
    backend = CloudBackend(config, transport_factory=cloud.transport)
    service = VehicleService(
        backend, OperationStore(tmp_path / "ops.sqlite"), enable_control=True, command_timeout=0.3
    )
    try:
        result = await final(service, await service.submit(actor(), command()))
        assert result.phase == Phase.UNKNOWN and len(cloud.sends) == 1
    finally:
        await service.close()


async def test_read_401_once_and_invalid_paths_only_split_reads(config, cloud):
    backend = CloudBackend(config, transport_factory=cloud.transport)
    api = backend._apis["account-a"]
    vin = config.vehicles[0].vin.get_secret_value()
    try:
        cloud.read_401 = 1
        response = await api.vss(vin, list(PATHS)[:2])
        assert len(response["items"]) == 2
        requests = [r for r in cloud.requests if r.url.path == VSS]
        assert len(requests) == 2
        assert requests[0].headers["authorization"] != requests[1].headers["authorization"]
        cloud.invalid_path = list(PATHS)[0]
        result = await api.vss(vin, list(PATHS)[:3])
        assert len(result["items"]) == 2
        assert not any(r.url.path == SEND for r in cloud.requests)
        cloud.read_401 = 5
        with pytest.raises(ProtocolError, match="upstream_http_error"):
            await api.vss(vin, list(PATHS)[:2])
        assert cloud.read_401 == 3  # bounded at two read attempts
    finally:
        await backend.close()


@pytest.mark.parametrize(
    "location,code",
    [
        ("fixture://auth/callback?require=SMS_CODE", "challenge_required"),
        ("fixture://auth/callback?code=anything&state=wrong", "state_or_code"),
        ("https://attacker.invalid/?code=anything", "unexpected_auth_redirect"),
    ],
)
async def test_risk_challenges_and_redirects_block_future_login(config, cloud, location, code):
    cloud.login_redirect = location
    backend = CloudBackend(config, transport_factory=cloud.transport)
    try:
        with pytest.raises(ProtocolError, match=code):
            await backend.vehicles("account-a")
        count = len(cloud.requests)
        with pytest.raises(ProtocolError, match="operator_attention"):
            await backend.vehicles("account-a")
        assert len(cloud.requests) == count
        assert not any(r.url.host == "attacker.invalid" for r in cloud.requests)
    finally:
        await backend.close()


async def test_partial_scope_grant_is_rejected(config, cloud):
    cloud.scope_subset = True
    backend = CloudBackend(config, transport_factory=cloud.transport)
    try:
        with pytest.raises(ProtocolError, match="scope_not_granted"):
            await backend.vehicles("account-a")
        assert not cloud.sends
    finally:
        await backend.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"vehicleType": "inviting"},
        {"vehicleType": "transferring"},
        {"vehicleState": "Registered"},
        {"vehicleState": "Transferred"},
        {"vehicleState": "ReverseActivating"},
        {"isReceiver": True},
        {"vehicleType": "authorized", "vehicleRoleId": 999},
        {"vehicleType": "authorized", "vehicleRoleId": 10},
        {"vehicleState": ""},
    ],
)
async def test_vehicle_permissions_fail_closed(config, cloud, changes):
    cloud.records["account-a"][0].update(changes)
    backend = CloudBackend(config, transport_factory=cloud.transport)
    try:
        assert [v.vehicle_id for v in await backend.vehicles("account-a")] == ["car-0-1"]
        with pytest.raises(ServiceError, match="vehicle_not_found"):
            await backend.submit_climate(command())
        assert not cloud.sends
    finally:
        await backend.close()


async def test_family_readonly_unknown_model_default_control_and_account_isolation(config, cloud):
    backend = CloudBackend(
        config.model_copy(update={"allow_real_control": False}), transport_factory=cloud.transport
    )
    try:
        assert not (await backend.capabilities("car-0-0")).climate
        with pytest.raises(ServiceError, match="real_control_disabled"):
            await backend.submit_climate(command())
        assert [v.vehicle_id for v in await backend.vehicles("account-b")] == ["car-1-0", "car-1-1"]
        cloud.records["account-a"][0]["modelId"] = "unknown-model"
        assert not (await backend.capabilities("car-0-0")).model_known
        cloud.records["account-a"][0].update(vehicleType="authorized", vehicleRoleId=15)
        assert usable_relation(cloud.records["account-a"][0])
        assert not usable_relation(cloud.records["account-a"][0], write=True)
        assert not cloud.sends
    finally:
        await backend.close()


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"pushState": 5, "resultCode": 0}, "completed"),
        ({"pushState": 7, "resultCode": 2009}, "failed"),
        ({"pushState": 7, "resultCode": 0}, "unknown"),
        ({"pushState": 5, "resultCode": 2009}, "unknown"),
        ({"pushState": 5}, "unknown"),
        ({"pushState": 7}, "unknown"),
    ],
)
def test_conflicting_terminal_results(data, expected):
    assert cloud_result(data) == expected


def test_configuration_file_privacy_and_identity_alias_binding(config, tmp_path):
    path = tmp_path / "synthetic-secrets.json"
    # Explicitly convert only synthetic secrets; production code never serializes them.
    data = config.model_dump(mode="json")
    for i, a in enumerate(config.accounts):
        for key in ("phone", "password", "device_id", "key_id", "hac_key_hex", "app_token"):
            data["accounts"][i][key] = getattr(a, key).get_secret_value()
    for i, v in enumerate(config.vehicles):
        data["vehicles"][i]["vin"] = v.vin.get_secret_value()
    path.write_text(json.dumps(data))
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private_regular_file"):
        load_cloud_config(path)
    path.chmod(0o600)
    assert load_cloud_config(path).accounts[0].account_id == "account-a"
    for secret in (
        config.accounts[0].password,
        config.accounts[0].device_id,
        config.vehicles[0].vin,
    ):
        assert secret.get_secret_value() not in repr(config)
    data["vehicles"][1]["vin"] = data["vehicles"][0]["vin"]
    with pytest.raises(ValidationError, match="duplicate_vehicle_identity"):
        CloudConfig.model_validate(data)
    store = OperationStore(tmp_path / "ops.sqlite")
    store.bind_backend("mock")
    with pytest.raises(RuntimeError, match="backend_mismatch"):
        store.bind_backend("lixiang:synthetic-different-binding")
    store.close()


async def test_transport_rejects_arbitrary_hosts_and_bounds_bodies():
    async def respond(request):
        return httpx.Response(200, content=b"x" * 1_048_577)

    http = CloudHTTP(frozenset({API}), transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(ProtocolError, match="forbidden_upstream_origin"):
            await http.request("GET", "https://attacker.invalid/")
        with pytest.raises(ProtocolError, match="response_too_large"):
            await http.request("GET", API + "/synthetic")
    finally:
        await http.close()


def test_signal_unknowns_preserve_time_and_location_separation():
    timestamp = (now() - timedelta(minutes=5)).isoformat()
    response = {
        "items": [
            {
                "path": "Vehicle.Cabin.AC.FOffStatus",
                "dp": {"value": "unexpected", "tsFormat": timestamp},
            },
            {"path": "Vehicle.Cabin.AC.SetTemp", "dp": {"value": 23, "tsFormat": timestamp}},
            {
                "path": LOCATION_PATH,
                "dp": {"value": json.dumps({"v": True, "lat": 0, "lon": 0}), "tsFormat": timestamp},
            },
        ]
    }
    state = state_from_vss("car-0-0", response, 120, simulated=True)
    assert state.signals["climate_enabled"].value is None
    assert state.signals["climate_target_c"].stale and state.signals["connected"].value is None
    assert "lat" not in state.signals and LOCATION_PATH not in str(state.model_dump())
    location = location_from_vss("car-0-0", response, 120, simulated=True)
    assert location.latitude == 0 and location.stale


async def test_mixed_generation_mesh_vat_bundle_reacquires_old_token(config, cloud):
    backend = CloudBackend(config, transport_factory=cloud.transport)
    auth = backend._apis["account-a"].auth
    try:
        old_mesh = await auth.scope(config.profile.mesh_audience, MESH_SCOPE)
        cloud.scope_error_once = True  # VAT exchange now invalidates the cookie session.
        mesh, vat = await auth.bundle(
            [
                (config.profile.mesh_audience, MESH_SCOPE, 780),
                (config.profile.vat_audience, "remoteVehACSmartControl:synthetic-car", 780),
            ]
        )
        assert mesh != old_mesh and vat
        assert cloud.login_count == 2
        assert auth._scopes[(config.profile.mesh_audience, MESH_SCOPE)].value == mesh
    finally:
        await backend.close()


@pytest.mark.parametrize(
    "result", [{"pushState": 7, "resultCode": 0}, {"pushState": 5, "resultCode": 2009}]
)
async def test_conflicting_results_become_unknown_and_block_writes(config, cloud, tmp_path, result):
    cloud.result = result
    backend = CloudBackend(config, transport_factory=cloud.transport)
    service = VehicleService(backend, OperationStore(tmp_path / "ops.sqlite"), enable_control=True)
    try:
        op = await final(service, await service.submit(actor(), command()))
        assert op.phase == Phase.UNKNOWN
        with pytest.raises(ServiceError, match="vehicle_has_unknown_operation"):
            await service.submit(actor(), command(key="conflicting-retry"))
        assert len(cloud.sends) == 1
    finally:
        await service.close()


async def test_http_adapter_network_error_does_not_expose_sensitive_text():
    async def failure(request):
        raise httpx.ReadTimeout("synthetic-phone-token-vin-coordinate-secret")

    client = CloudHTTP(frozenset({API}), transport=httpx.MockTransport(failure))
    try:
        with pytest.raises(ProtocolError) as caught:
            await client.request("POST", API + SEND, content=b"synthetic-sensitive-body")
        assert str(caught.value) == "upstream_network_error"
        assert "synthetic" not in str(caught.value)
    finally:
        await client.close()


async def test_mcp_routes_execute_protocol_adapter_instead_of_mock_backend(config, cloud, tmp_path):
    import hashlib
    import secrets

    from lixiang_mcp.config import Settings
    from lixiang_mcp.server import create_app

    token = secrets.token_urlsafe(32)
    auth_file = tmp_path / "backend-auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "credentials": [
                    {
                        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                        "principal": actor().model_dump(mode="json"),
                    }
                ]
            }
        )
    )
    settings = Settings(auth_file=auth_file, database=tmp_path / "mcp.sqlite", enable_control=True)
    backend = CloudBackend(config, transport_factory=cloud.transport)
    app = create_app(settings, backend=backend)
    async with (
        app.app.router.lifespan_context(app.app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        }

        async def tool(name, arguments=None):
            response = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments or {}},
                },
            )
            assert response.status_code == 200
            return response.json()["result"]["structuredContent"]

        vehicles = await tool("list_vehicles")
        assert vehicles["vehicles"][0]["vehicle_id"] == "car-0-0"
        operation = await tool("set_climate", {"command": command().model_dump()})
        for _ in range(200):
            result = await tool("get_operation", {"operation_id": operation["operation_id"]})
            if result["phase"] == "vehicle_confirmed":
                break
            await asyncio.sleep(0.01)
        assert result["phase"] == "vehicle_confirmed"
        assert len(cloud.sends) == 1 and cloud.login_count == 1
        # A client-supplied gateway identity never reaches the upstream protocol.
        assert all(
            token not in str(r.headers) and token not in r.content.decode() for r in cloud.requests
        )


async def test_owner_missing_transfer_evidence_is_readonly(config, cloud):
    cloud.records["account-a"][0].pop("isReceiver")
    backend = CloudBackend(config, transport_factory=cloud.transport)
    try:
        assert len(await backend.vehicles("account-a")) == 2
        assert not (await backend.capabilities("car-0-0")).climate
        with pytest.raises(ServiceError, match="vehicle_not_found"):
            await backend.submit_climate(command())
        assert not cloud.sends
    finally:
        await backend.close()
