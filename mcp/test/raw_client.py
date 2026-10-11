#!/usr/bin/env python3
"""Minimal zero-dependency MCP stdio client (raw JSON-RPC 2.0).

Spawns whoogle_mcp_server.py and exchanges newline-delimited JSON-RPC
messages with it, printing the exact bytes on the wire. Useful as a
dependency-free proof and for debugging.

Usage:
    WHOOGLE_URL=http://127.0.0.1:5000 python mcp/test/raw_client.py "raspberry pi 5"
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.abspath(os.path.join(HERE, "..", "whoogle_mcp_server.py"))


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "raspberry pi 5"
    env = dict(os.environ)
    env.setdefault("WHOOGLE_URL", "http://127.0.0.1:5000")

    proc = subprocess.Popen(
        [sys.executable, SERVER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=env,
        text=True,
        bufsize=1,
    )

    def send(obj, label):
        line = json.dumps(obj)
        print(f">>> {label}\n{line}")
        proc.stdin.write(line + "\n")
        proc.stdin.flush()
        resp = proc.stdout.readline()
        print(f"<<< {label}\n{resp.rstrip()}\n")
        return json.loads(resp) if resp else None

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05",
                     "capabilities": {}, "clientInfo": {"name": "raw", "version": "0"}}},
         "initialize")
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
         "tools/list")
    res = send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "search", "arguments": {"query": query, "limit": 3}}},
               "tools/call search")

    proc.stdin.close()
    proc.terminate()
    ok = res and "result" in res and not res["result"].get("isError")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
