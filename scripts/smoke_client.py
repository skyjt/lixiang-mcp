#!/usr/bin/env python3
"""Official MCP SDK smoke client. Reads only the freshly generated mock backend credential."""

import argparse
import asyncio
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main(url: str, token_file: Path) -> None:
    token = (await asyncio.to_thread(token_file.read_text)).strip()
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as client:
        async with streamable_http_client(url, http_client=client) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool("list_vehicles")
                assert not result.isError and result.structuredContent is not None
                assert len(result.structuredContent["vehicles"]) == 3
                state = await session.call_tool("get_vehicle_state", {"vehicle_id": "demo-l6"})
                assert not state.isError and state.structuredContent["simulated"]
                print(f"PASS: initialize, {len(tools.tools)} tools, 3 mock vehicles, sampled state")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    parser.add_argument("--token-file", type=Path, default=Path("runtime/demo-token"))
    args = parser.parse_args()
    asyncio.run(main(args.url, args.token_file))
