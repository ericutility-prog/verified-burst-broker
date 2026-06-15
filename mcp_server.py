#!/usr/bin/env python3
"""MCP stdio server — THE WEDGE. One tool: buy_verified_burst.

An agent-builder adds this server to their MCP client (one line of config) and
their agent gains a single tool: at a hard fork, call buy_verified_burst -> it
escalates to fast silicon (Cerebras), runs best-of-N, gates on a verifier, pays
per-burst over x402, and is charged ONLY if the answer passed. Budget-capped.

Newline-delimited JSON-RPC 2.0 over stdio (MCP stdio transport). Stdlib only.

Add to an MCP client config, e.g.:
  { "mcpServers": { "verified-burst": { "command": "python3",
      "args": ["/root/inference-burst/mcp_server.py"] } } }
"""
import base64
import json
import os
import sys

import env; env.load_env()
import broker

AGENT_ID = os.environ.get("BURST_AGENT_ID", "agent-local")
PROTOCOL = "2024-11-05"

TOOL = {
    "name": "buy_verified_burst",
    "description": (
        "Buy a verified inference burst at a hard/irreversible/low-confidence decision. "
        "Escalates to fast silicon, samples best-of-N, gates the answer through a verifier, "
        "and charges (x402) ONLY if it passes. Returns the verified answer + a receipt. "
        "Budget-capped per agent. Use when getting it wrong is costly."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "request": {"type": "string", "description": "The decision/question to resolve."},
            "strategy": {"type": "string", "enum": ["fast", "best_of_n"], "default": "best_of_n"},
            "n": {"type": "integer", "default": 3, "description": "best-of-N sample count."},
            "verifier": {"type": "string",
                         "enum": ["self_consistency", "judge", "none"],
                         "default": "self_consistency"},
            "answer_key": {"type": "array", "items": {"type": "string"},
                           "description": 'Optional ["json","<field>"] or ["regex","<pat>"] to normalize answers.'},
        },
        "required": ["request"],
    },
}


def _sim_payment():
    """In sim mode the server presents the agent's identity as the payer."""
    return base64.b64encode(json.dumps({"from": AGENT_ID}).encode()).decode()


def _payment(strategy, n, verifier):
    """Produce the X-PAYMENT for this burst.

    LIVE (X402_MODE=live): sign a REAL x402 (EIP-3009) authorization for the
    quoted price using the agent's own funding wallet (BURST_BUYER_KEY) paying to
    the seller (X402_PAY_TO). The agent never has to "decide" to sign — the tool
    auto-signs, and the broker charges ONLY if the answer verifies, capped by the
    per-agent budget. SIM: present the agent identity.
    """
    if os.environ.get("X402_MODE", "sim").lower() != "live":
        return _sim_payment()
    import pricing, x402_live
    buyer_key = os.environ.get("BURST_BUYER_KEY")
    if not buyer_key:
        raise RuntimeError("X402_MODE=live needs BURST_BUYER_KEY (the agent's funding wallet key)")
    pay_to = os.environ.get("X402_PAY_TO")
    if not pay_to:
        raise RuntimeError("X402_MODE=live needs X402_PAY_TO (the seller wallet)")
    q = pricing.quote(strategy=strategy, n=n, verifier=verifier)
    reqs, _ = x402_live.build_requirements_v2(q["price_usd"], pay_to)
    _, x_payment = x402_live.sign_payment(reqs, buyer_key)
    return x_payment


def call_tool(args):
    ak = args.get("answer_key")
    strategy = args.get("strategy", "best_of_n")
    n = int(args.get("n", 3))
    verifier = args.get("verifier", "self_consistency")
    result = broker.serve_burst(
        args["request"],
        x_payment=_payment(strategy, n, verifier),
        strategy=strategy,
        n=n,
        verifier=verifier,
        answer_key=tuple(ak) if isinstance(ak, list) else None,
        receipt_id=f"mcp-{AGENT_ID}",
    )
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": result["status"] not in ("ok", "not_verified")}


def handle(msg):
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "verified-burst", "version": "0.1.0"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [TOOL]}}
    if method == "tools/call":
        params = msg.get("params", {})
        if params.get("name") != "buy_verified_burst":
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": "unknown tool"}}
        try:
            return {"jsonrpc": "2.0", "id": mid, "result": call_tool(params.get("arguments", {}))}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if mid is None:
        return None  # a notification (e.g. notifications/initialized) — no reply
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
