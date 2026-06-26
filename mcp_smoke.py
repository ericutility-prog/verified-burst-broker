#!/usr/bin/env python3
"""MCP introspection smoke-test — what Glama (and any MCP client) checks first.

Spawns mcp_server.py, drives the stdio JSON-RPC handshake (initialize ->
notifications/initialized -> tools/list), and asserts the server identifies
itself and advertises the buy_verified_burst tool. Pure stdlib; runs in sim mode
so it needs no secrets. Exit 0 = healthy.

    python3 mcp_smoke.py
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    env = dict(os.environ, X402_MODE="sim")
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "smoke", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    stdin = "".join(json.dumps(m) + "\n" for m in msgs)
    p = subprocess.run(
        [sys.executable, os.path.join(HERE, "mcp_server.py")],
        input=stdin, capture_output=True, text=True, env=env, timeout=60)

    replies = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
    by_id = {r.get("id"): r for r in replies}

    init = by_id.get(1, {}).get("result", {})
    assert init.get("serverInfo", {}).get("name") == "verified-burst", \
        f"bad initialize result: {init!r}\nstderr: {p.stderr}"

    tools = by_id.get(2, {}).get("result", {}).get("tools", [])
    names = [t.get("name") for t in tools]
    assert "buy_verified_burst" in names, f"tool not advertised: {names!r}"

    print("MCP smoke OK — serverInfo=verified-burst, tools=" + ", ".join(names))


if __name__ == "__main__":
    main()
