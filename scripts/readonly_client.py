#!/usr/bin/env python3
"""Official SDK readonly check. Localhost only; credentials are read from private files."""

import argparse
import asyncio
import re
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from lixiang_mcp.config import Settings
from lixiang_mcp.private_files import private_read


async def check(config_file: Path, token_file: Path) -> None:
    settings = Settings.model_validate_json(await asyncio.to_thread(config_file.read_bytes))
    token = (await asyncio.to_thread(private_read, token_file, 512)).decode().strip()
    url = f"http://127.0.0.1:{settings.port}/mcp"
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, trust_env=False
    ) as client:
        async with streamable_http_client(url, http_client=client) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()

                async def call(name, arguments=None):
                    result = await session.call_tool(name, arguments or {})
                    body = result.structuredContent
                    if isinstance(body, dict) and isinstance(body.get("error"), dict):
                        code = body["error"].get("code")
                        if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,80}", code):
                            print("只读错误码:", code)
                        raise RuntimeError("readonly_call_failed")
                    if result.isError or not isinstance(body, dict) or "error" in body:
                        raise RuntimeError("readonly_call_failed")
                    return body

                vehicles = (await call("list_vehicles"))["vehicles"]
                if not vehicles:
                    raise RuntimeError("no_authorized_vehicles")
                for vehicle in vehicles:
                    if vehicle["simulated"] != (settings.backend == "mock"):
                        raise RuntimeError("unexpected_backend")
                    arguments = {"vehicle_id": vehicle["vehicle_id"]}
                    capabilities = await call("get_vehicle_capabilities", arguments)
                    if capabilities["climate"] or capabilities["location"]:
                        raise RuntimeError("expected_readonly_permissions")
                    print("model_known:", capabilities["model_known"])
                    for name in (
                        "get_connection_status",
                        "get_vehicle_state",
                        "get_charging_status",
                    ):
                        signals = (await call(name, arguments))["signals"]
                        print(
                            name,
                            "signals:",
                            len(signals),
                            "stale:",
                            sum(s["stale"] for s in signals.values()),
                        )
                print("PASS: readonly MCP calls completed; vehicles:", len(vehicles))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--token-file", type=Path)
    args = parser.parse_args()
    try:
        asyncio.run(check(args.config, args.token_file or args.config.parent / "backend-token"))
    except Exception:
        raise SystemExit("只读检查失败；检查固定错误码与本地配置，勿上传原始响应或凭据") from None
