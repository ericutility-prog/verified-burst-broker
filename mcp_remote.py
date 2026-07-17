#!/usr/bin/env python3
"""verified-burst — a thin MCP server an agent-builder installs (talks to the hosted burst endpoint over x402; BYOK optional).

Unlike mcp_server.py (which runs the broker in-process and needs the SELLER's
secrets), this is a pure CLIENT: it talks to the hosted verified-burst endpoint
over HTTPS, pays per call with the BUILDER's OWN wallet via x402, and optionally
brings the builder's OWN Cerebras key (BYOK). It holds none of our secrets.

An agent gains one tool — buy_verified_burst — and at a hard fork (best when the
answer is checkable) it samples best-of-N on the buyer's own key, gates the answer
through a verifier, and pays the service fee (stablecoin, x402) ONLY if it passes.
Budget-capped server-side.

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
BESTPRICE_ENDPOINT = os.environ.get("BESTPRICE_ENDPOINT", "https://agentsprice.com/v1/best-price")
BUYER_KEY = os.environ.get("BURST_BUYER_KEY")
PROVIDER_KEY = os.environ.get("BURST_PROVIDER_KEY")
PROTOCOL = "2024-11-05"

TOOL = {
    "name": "buy_verified_burst",
    "description": (
        "Buy a verified decision at a hard/irreversible/low-confidence fork — best when the answer "
        "is checkable (a label, number, JSON field, or yes/no). Samples best-of-N on your own key, "
        "then gates the answer through a verifier — up to an INDEPENDENT model family (a different "
        "vendor) that checks it, the one form of 'more thinking' your own correlated samples can't "
        "supply — and pays the service fee (x402 stablecoin) ONLY if it passes; a miss waives the "
        "fee (your BYOK tokens still apply). Returns the passing answer + a keepable receipt. Use "
        "when getting it wrong is costly."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "request": {"type": "string", "description": "The decision to resolve. Verifies best "
                        "when the answer is checkable — a label, number, JSON field, or yes/no."},
            "strategy": {"type": "string", "enum": ["fast", "best_of_n"], "default": "best_of_n"},
            "n": {"type": "integer", "default": 3, "description": "best-of-N sample count."},
            "verifier": {"type": "string",
                         "enum": ["self_consistency", "judge", "independent_judge",
                                  "independent_quorum", "none"],
                         "default": "self_consistency",
                         "description": ("How the answer is checked before you're charged: "
                         "self_consistency = N-of-M samples agree (pair with answer_key); "
                         "judge = adversarial LLM check; "
                         "independent_judge = a DIFFERENT model family checks it (decorrelated from "
                         "your model's blind spots — the one check you can't self-supply); "
                         "independent_quorum = multiple independent models ACROSS VENDORS must agree "
                         "(k-of-M; pass quorum_k); none = no gate. The independent verifiers charge "
                         "only if they pass; pass a 'candidate' to verify your agent's OWN answer "
                         "with no generation.")},
            "answer_key": {"type": "array", "items": {"type": "string"},
                           "description": 'How to extract the comparable answer for self_consistency '
                           '— ["json","<field>"] or ["regex","(<pat>)"]. Recommended; without it, '
                           'agreement is measured on the full text and rarely matches on prose.'},
            "candidate": {"type": "string", "description": "Optional: your agent's OWN answer to "
                          "verify directly — skips generation, the independent judge just checks it."},
            "quorum_k": {"type": "integer", "description": "For independent_quorum: how many of the "
                         "M independent models must agree (k-of-M)."},
            "model": {"type": "string", "description": "Optional model override (must match your BYOK key)."},
        },
        "required": ["request"],
    },
}

TOOL_BESTPRICE = {
    "name": "best_price_now",
    "description": (
        "Pay a micropayment (x402 stablecoin) for ONE broad, real-time best-price search across "
        "sellers, and get back the current best price + where to buy. Charged ONLY if the search "
        "returns real results — no info found waives the fee. Use to price a specific product right "
        "now before buying or quoting."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to price — a product name, "
                      "e.g. 'airpods pro' or 'ninja air fryer'."},
        },
        "required": ["query"],
    },
}


def _post(endpoint, body, headers=None):
    req = urllib.request.Request(
        endpoint, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _pay_flow(endpoint, body, extra_headers=None):
    """Shared x402 flow: unpaid -> 402 challenge -> sign with the builder's wallet -> pay."""
    if not BUYER_KEY:
        return {"error": "BURST_BUYER_KEY not set (the wallet that pays per call)"}
    code, resp = _post(endpoint, body)                   # 1) unpaid -> 402 challenge
    if code == 402 and "accepts" in resp:
        reqs = L._coerce_requirements(resp["accepts"])
        _, x_payment = L.sign_payment(reqs, BUYER_KEY)   # 2) sign with builder wallet
        headers = {"X-PAYMENT": x_payment, **(extra_headers or {})}
        code, resp = _post(endpoint, body, headers)      # 3) pay -> fulfill -> settle-if-earned
    return resp


def buy(args):
    body = {"request": args["request"],
            "strategy": args.get("strategy", "best_of_n"),
            "n": int(args.get("n", 3)),
            "verifier": args.get("verifier", "self_consistency")}
    if args.get("answer_key"):
        body["answer_key"] = args["answer_key"]
    if args.get("candidate"):
        body["candidate"] = args["candidate"]
    if args.get("quorum_k") is not None:
        body["quorum_k"] = int(args["quorum_k"])
    if args.get("model"):
        body["model"] = args["model"]
    extra = {"X-Provider-Key": PROVIDER_KEY} if PROVIDER_KEY else None   # BYOK
    return _pay_flow(ENDPOINT, body, extra)


def best_price(args):
    q = (args.get("query") or "").strip()
    if not q:
        return {"error": "query is required (the product to price)"}
    return _pay_flow(BESTPRICE_ENDPOINT, {"query": q})


def _gate_banner(resp):
    """Hoist the go/no-go to the FIRST line the agent reads — the verdict gates the
    agent's next step, not just the charge (the anti-haywire point)."""
    g = resp.get("gate") or {}
    c = g.get("confidence")
    conf = f" (confidence {c})" if c is not None else ""
    if g.get("action") == "hold":
        return (f"GATE: HOLD — the answer did NOT pass the verifier{conf}. "
                "DO NOT act on it; re-try, escalate to a human, or treat the decision as "
                "unresolved. You were NOT charged.\n\n")
    if g.get("action") == "proceed":
        tx = resp.get("tx")
        rcpt = f" Receipt: {tx}." if tx else ""
        return f"GATE: PROCEED — answer passed the verifier{conf}; OK to proceed (verified, not guaranteed).{rcpt}\n\n"
    return ""


def _price_banner(resp):
    """Hoist the best-price result to the first line the agent reads."""
    if resp.get("error"):
        return f"ERROR: {resp['error']}\n\n"
    if resp.get("status") == "no_results":
        return "No real results found — you were NOT charged.\n\n"
    deals = (resp.get("result") or {}).get("deals") or []
    if resp.get("status") == "ok" and deals:
        top = deals[0]
        price = top.get("best_price")
        name = top.get("name", resp.get("query", ""))
        seller = top.get("best_seller") or top.get("source") or "?"
        tx = resp.get("tx")
        rcpt = f" Receipt: {tx}." if tx else ""
        return (f"BEST PRICE: {name} — ${price} @ {seller} "
                f"({resp.get('count', len(deals))} sellers compared).{rcpt}\n\n")
    return ""


def handle(msg):
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
            "serverInfo": {"name": "verified-burst", "version": "1.0.1"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [TOOL, TOOL_BESTPRICE]}}
    if method == "tools/call":
        params = msg.get("params", {})
        name = params.get("name")
        try:
            if name == "buy_verified_burst":
                result = buy(params.get("arguments", {}))
                is_err = result.get("status") not in ("ok", "not_verified") or "error" in result
                text = _gate_banner(result) + json.dumps(result, indent=2)
            elif name == "best_price_now":
                result = best_price(params.get("arguments", {}))
                is_err = result.get("status") not in ("ok", "no_results") or "error" in result
                text = _price_banner(result) + json.dumps(result, indent=2)
            else:
                return {"jsonrpc": "2.0", "id": mid,
                        "error": {"code": -32601, "message": "unknown tool"}}
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": text}],
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
