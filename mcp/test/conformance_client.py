#!/usr/bin/env python3
"""Conformance test: drive whoogle_mcp_server.py with the official MCP SDK.

This is a *client*. It proves the hand-rolled stdio server speaks enough of
the MCP protocol for a real, third-party MCP client to complete the handshake
and call a tool.

Usage:
    pip install -r mcp/requirements-dev.txt
    WHOOGLE_URL=http://127.0.0.1:5000 python mcp/test/conformance_client.py "raspberry pi 5"

Exit code 0 on a successful initialize + tools/list + tools/call, non-zero
otherwise.
"""

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "whoogle_mcp_server.py")
SERVER = os.path.abspath(SERVER)


async def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "raspberry pi 5"
    pass_env = {
        "WHOOGLE_URL": os.environ.get("WHOOGLE_URL", "http://127.0.0.1:5000"),
        "PATH": os.environ.get("PATH", ""),
    }
    for extra in ("WHOOGLE_TIMEOUT", "WHOOGLE_MCP_MAX_RESULTS"):
        if extra in os.environ:
            pass_env[extra] = os.environ[extra]

    params = StdioServerParameters(
        command=sys.executable,
        args=[SERVER],
        env=pass_env,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("== initialize ==")
            print("serverInfo:", getattr(init, "server_info", None) or getattr(init, "serverInfo", None))
            print("protocolVersion:", getattr(init, "protocol_version", None) or getattr(init, "protocolVersion", None))
            print("capabilities:", init.capabilities)

            tools = await session.list_tools()
            print("\n== tools/list ==")
            print("tools:", [t.name for t in tools.tools])

            print(f"\n== tools/call search(query={query!r}) ==")
            result = await session.call_tool("search", {"query": query, "limit": 3})
            is_error = getattr(result, "isError", None)
            if is_error is None:
                is_error = getattr(result, "is_error", False)
            print("isError:", is_error)
            for block in result.content:
                if getattr(block, "type", None) == "text":
                    print(block.text)
            structured = getattr(result, "structuredContent", None) or getattr(
                result, "structured_content", None
            )
            if structured:
                print("structuredContent.results[0]:", structured.get("results", [])[:1])

            if is_error:
                return 1
            ok = bool(structured) and len(structured.get("results", [])) > 0
            print("\nRESULT:", "PASS" if ok else "FAIL (no structured results)")
            return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
