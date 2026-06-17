"""The MCP stdio server an agent-builder installs — pure client.

Talks to a hosted verified-burst endpoint over HTTPS, pays per call with the
builder's OWN wallet via x402, optionally brings the builder's OWN provider key
(BYOK). Holds no seller secrets.

Env:
  BURST_ENDPOINT     hosted burst URL (default https://solcleus.com/v1/burst)
  BURST_BUYER_KEY    REQUIRED — wallet that pays per burst (USDC on Base)
  BURST_PROVIDER_KEY optional — your Cerebras key; bursts run on your tokens (BYOK)
"""
import json
import os
import sys
import urllib.error
import urllib.request

from .signing import sign_payment

ENDPOINT = os.environ.get("BURST_ENDPOINT", "https://solcleus.com/v1/burst")
BESTPRICE_ENDPOINT = os.environ.get("BESTPRICE_ENDPOINT", "https://agentsprice.com/v1/best-price")
PROTOCOL = "2024-11-05"

TOOL = {
    "name": "buy_verified_burst",
    "description": (
        "Buy a verified decision at a hard/irreversible/low-confidence fork — best when the answer "
        "is checkable (a label, number, JSON field, or yes/no). Samples best-of-N on your own key, "
        "gates the answer through a verifier (samples agree / judge / your check), and pays the "
        "service fee (x402 stablecoin) ONLY if it passes — a miss waives the fee (your BYOK tokens "
        "still apply). Returns the passing answer + receipt. Use when getting it wrong is costly."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "request": {"type": "string", "description": "The decision to resolve. Verifies best "
                        "when the answer is checkable — a label, number, JSON field, or yes/no."},
            "strategy": {"type": "string", "enum": ["fast", "best_of_n"], "default": "best_of_n"},
            "n": {"type": "integer", "default": 3, "description": "best-of-N sample count."},
            "verifier": {"type": "string", "enum": ["self_consistency", "judge", "none"],
                         "default": "self_consistency",
                         "description": "self_consistency = N-of-M samples agree (pair with "
                         "answer_key); judge = adversarial LLM check; none = no gate."},
            "answer_key": {"type": "array", "items": {"type": "string"},
                           "description": 'How to extract the comparable answer for self_consistency '
                           '— ["json","<field>"] or ["regex","(<pat>)"]. Recommended; without it, '
                           'agreement is measured on the full text and rarely matches on prose.'},
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
    buyer_key = os.environ.get("BURST_BUYER_KEY")
    if not buyer_key:
        return {"error": "BURST_BUYER_KEY not set (the wallet that pays per call)"}
    base = dict(extra_headers or {})
    code, resp = _post(endpoint, body, base)                # 1) unpaid -> 402
    if code == 402 and "accepts" in resp:
        x_payment = sign_payment(resp["accepts"], buyer_key)  # 2) sign w/ builder wallet
        code, resp = _post(endpoint, body, {**base, "X-PAYMENT": x_payment})  # 3) pay
    return resp


def buy(args):
    provider_key = os.environ.get("BURST_PROVIDER_KEY")
    body = {"request": args["request"],
            "strategy": args.get("strategy", "best_of_n"),
            "n": int(args.get("n", 3)),
            "verifier": args.get("verifier", "self_consistency")}
    if args.get("answer_key"):
        body["answer_key"] = args["answer_key"]
    if args.get("model"):
        body["model"] = args["model"]
    extra = {"X-Provider-Key": provider_key} if provider_key else None
    return _pay_flow(ENDPOINT, body, extra)


def best_price(args):
    q = (args.get("query") or "").strip()
    if not q:
        return {"error": "query is required (the product to price)"}
    return _pay_flow(BESTPRICE_ENDPOINT, {"query": q})


def _price_banner(resp):
    if resp.get("error"):
        return f"⚠️ {resp['error']}\n\n"
    if resp.get("status") == "no_results":
        return "ℹ️ No real results found — you were NOT charged.\n\n"
    deals = (resp.get("result") or {}).get("deals") or []
    if resp.get("status") == "ok" and deals:
        top = deals[0]
        seller = top.get("best_seller") or top.get("source") or "?"
        tx = resp.get("tx")
        rcpt = f" Receipt: {tx}." if tx else ""
        return (f"💲 BEST PRICE: {top.get('name', resp.get('query',''))} — "
                f"${top.get('best_price')} @ {seller} "
                f"({resp.get('count', len(deals))} sellers compared).{rcpt}\n\n")
    return ""


def _gate_banner(resp):
    """Hoist the go/no-go to the FIRST line the agent reads, so the verdict gates the
    agent's next step instead of being buried in the JSON. This is the anti-haywire point."""
    g = resp.get("gate") or {}
    if g.get("action") == "hold":
        c = g.get("confidence")
        conf = f" (confidence {c})" if c is not None else ""
        return (f"⛔ GATE: HOLD — the answer did NOT pass the verifier{conf}. "
                "DO NOT act on it; re-try, escalate to a human, or treat the decision as "
                "unresolved. You were NOT charged.\n\n")
    if g.get("action") == "proceed":
        c = g.get("confidence")
        conf = f" (confidence {c})" if c is not None else ""
        tx = resp.get("tx")
        rcpt = f" Receipt: {tx}." if tx else ""
        return (f"✅ GATE: PROCEED — answer passed the verifier{conf}. Safe to act on."
                f"{rcpt}\n\n")
    return ""  # error/byok/budget paths: no gate, return the JSON as-is


def handle(msg):
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
            "serverInfo": {"name": "verified-burst", "version": "1.1.0"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [TOOL, TOOL_BESTPRICE]}}
    if method == "tools/call":
        params = msg.get("params", {})
        name = params.get("name")
        try:
            if name == "buy_verified_burst":
                result = buy(params.get("arguments", {}))
                is_err = "error" in result or result.get("status") not in ("ok", "not_verified")
                text = _gate_banner(result) + json.dumps(result, indent=2)
            elif name == "best_price_now":
                result = best_price(params.get("arguments", {}))
                is_err = "error" in result or result.get("status") not in ("ok", "no_results")
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
