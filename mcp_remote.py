#!/usr/bin/env python3
"""THE DISTRIBUTABLE WEDGE — a thin MCP server an agent-builder installs.

Unlike mcp_server.py (which runs the broker in-process and needs the SELLER's
secrets), this is a pure CLIENT: it talks to the hosted verified-burst endpoint
over HTTPS, pays per call with the BUILDER's OWN wallet via x402, and optionally
brings the builder's OWN Cerebras key (BYOK). It holds none of our secrets.

An agent gains one tool — buy_verified_burst — and at a hard fork it escalates to
fast silicon, samples best-of-N, gates the answer through a verifier, and pays
(stablecoin, x402) ONLY if the answer passes. Budget-capped server-side.

Install (one line in an MCP client config):
  { "mcpServers": { "verified-burst": {
      "command": "python3", "args": ["/path/to/mcp_remote.py"],
      "env": {
        "BURST_BUYER_KEY": "0x<your funding wallet private key>",
        "BURST_PROVIDER_KEY": "csk-<your cerebras key>"   // optional (BYOK)
      } } } }

Env:
  BURST_ENDPOINT     hosted burst URL (default https://solcleus.com/v1/burst)
  BURST_BUYER_KEY    REQUIRED — the wallet that pays per burst (USDC on Base)
  BURST_PROVIDER_KEY optional — your Cerebras key; if set, bursts run on YOUR
                     tokens/rate limit (BYOK). Omitted -> the host's key.

Signing deps (buyer side): pip install "x402[evm]" eth-account web3
"""
import json
import os
import sys
import urllib.error
import urllib.request

# client-side x402 helpers (build requirements from the 402 challenge + sign);
# these are pure buyer-side, hold no seller secrets.
import x402_live as L

ENDPOINT = os.environ.get("BURST_ENDPOINT", "https://solcleus.com/v1/burst")
BUYER_KEY = os.environ.get("BURST_BUYER_KEY")
PROVIDER_KEY = os.environ.get("BURST_PROVIDER_KEY")
PROTOCOL = "2024-11-05"

TOOL = {
    "name": "buy_verified_burst",
    "description": (
        "Buy a verified inference burst at a hard/irreversible/low-confidence decision. "
        "Escalates to fast silicon, samples best-of-N, gates the answer through a verifier, "
        "and pays (x402 stablecoin) ONLY if it passes. Returns the verified answer + receipt. "
        "Use when getting it wrong is costly."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "request": {"type": "string", "description": "The decision/question to resolve."},
            "strategy": {"type": "string", "enum": ["fast", "best_of_n"], "default": "best_of_n"},
            "n": {"type": "integer", "default": 3, "description": "best-of-N sample count."},
            "verifier": {"type": "string", "enum": ["self_consistency", "judge", "none"],
                         "default": "self_consistency"},
            "answer_key": {"type": "array", "items": {"type": "string"},
                           "description": 'Optional ["json","<field>"] or ["regex","(<pat>)"] to normalize answers.'},
            "model": {"type": "string", "description": "Optional model override (must match your BYOK key)."},
        },
        "required": ["request"],
    },
}


def _post(body, headers=None):
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def buy(args):
    """Run the x402 flow: 402 challenge -> sign with the builder's wallet -> pay."""
    if not BUYER_KEY:
        return {"error": "BURST_BUYER_KEY not set (the wallet that pays per burst)"}
    body = {"request": args["request"],
            "strategy": args.get("strategy", "best_of_n"),
            "n": int(args.get("n", 3)),
            "verifier": args.get("verifier", "self_consistency")}
    if args.get("answer_key"):
        body["answer_key"] = args["answer_key"]
    if args.get("model"):
        body["model"] = args["model"]

    code, resp = _post(body)                       # 1) unpaid -> 402 challenge
    if code == 402 and "accepts" in resp:
        reqs = L._coerce_requirements(resp["accepts"])
        _, x_payment = L.sign_payment(reqs, BUYER_KEY)   # 2) sign with builder wallet
        headers = {"X-PAYMENT": x_payment}
        if PROVIDER_KEY:
            headers["X-Provider-Key"] = PROVIDER_KEY     # BYOK
        code, resp = _post(body, headers)          # 3) pay -> verify -> settle-if-passed
    return resp


def handle(msg):
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
            "serverInfo": {"name": "verified-burst", "version": "1.0.0"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [TOOL]}}
    if method == "tools/call":
        params = msg.get("params", {})
        if params.get("name") != "buy_verified_burst":
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown tool"}}
        try:
            result = buy(params.get("arguments", {}))
            is_err = result.get("status") not in ("ok", "not_verified") or "error" in result
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                "isError": bool(is_err)}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if mid is None:
        return None
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
