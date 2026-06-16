"""HTTP surface for the verified-burst broker — x402-gated.

  POST /v1/burst   body: {"request": "...", "strategy": "best_of_n", "n": 3,
                          "verifier": "self_consistency", "answer_key": ["json","choice"]}
    - no  X-PAYMENT header  -> 402 + payment requirements (the x402 challenge)
    - yes X-PAYMENT header  -> verify, run burst, settle ONLY if verified
  GET  /v1/quote?n=3&strategy=best_of_n  -> price up front
  GET  /healthz

Stdlib only. Run: python3 server.py  (PORT env, default 8402).
"""
import json
import os
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import env; env.load_env()
import pricing
import broker

# --- hardening knobs -------------------------------------------------------- #
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")  # localhost only; nginx fronts TLS
MAX_BODY = int(os.environ.get("BURST_MAX_BODY", str(32 * 1024)))   # 32 KB request cap
MAX_REQ_CHARS = int(os.environ.get("BURST_MAX_REQ_CHARS", "8000")) # prompt length cap
RATE_PER_MIN = int(os.environ.get("BURST_RATE_PER_MIN", "30"))     # /v1/burst per IP/min
# Blowback: require the caller's OWN provider key (BYOK) so every burst costs the
# CALLER their tokens — no free inference to extract from the host on non-verified
# results. On for the public endpoint; off for in-process/demo (host key fallback).
REQUIRE_BYOK = os.environ.get("BURST_REQUIRE_BYOK", "0").lower() in ("1", "true", "yes")
# Free-trial: a wallet with no BYOK key gets this many bursts on the HOST key (still
# paid per burst), then must bring its own key. 0 = strict BYOK (no trial).
TRIAL_CAP = int(os.environ.get("BURST_TRIAL_BURSTS", "0"))

_HITS = defaultdict(deque)
_HITS_LOCK = threading.Lock()


def _rate_ok(ip):
    """Sliding 60s window per client IP for the expensive /v1/burst path."""
    now = time.monotonic()
    with _HITS_LOCK:
        dq = _HITS[ip]
        while dq and now - dq[0] > 60.0:
            dq.popleft()
        if len(dq) >= RATE_PER_MIN:
            return False
        dq.append(now)
        if len(_HITS) > 10000:  # bound memory: drop emptied buckets
            for k in [k for k, v in _HITS.items() if not v]:
                _HITS.pop(k, None)
        return True


PUBLIC_URL = os.environ.get("BURST_PUBLIC_URL", "https://solcleus.com").rstrip("/")
# Hosts we serve under: discovery URLs reflect the host the caller used (so the
# burst.solcleus.com listing is self-consistent), but ONLY for known hosts — an
# unknown/spoofed Host falls back to PUBLIC_URL, never echoing an attacker URL.
_ALLOWED_HOSTS = {h.strip().lower() for h in
                  os.environ.get("BURST_ALLOWED_HOSTS", "solcleus.com,burst.solcleus.com").split(",")
                  if h.strip()}


def _base_url_for(host_header):
    """https://<host> when Host is one we serve; else PUBLIC_URL (anti-spoof)."""
    host = (host_header or "").split(":", 1)[0].strip().lower()
    return f"https://{host}" if host in _ALLOWED_HOSTS else PUBLIC_URL

# The buyable tool's input shape — advertised so crawlers/agent frameworks can
# call it without reading docs. Kept in sync with mcp_remote.py's TOOL.
_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "request": {"type": "string", "description": "The decision/question to resolve."},
        "strategy": {"type": "string", "enum": ["fast", "best_of_n"], "default": "best_of_n"},
        "n": {"type": "integer", "default": 3},
        "verifier": {"type": "string", "enum": ["self_consistency", "judge", "none"],
                     "default": "self_consistency"},
        "answer_key": {"type": "array", "items": {"type": "string"},
                       "description": 'Optional ["json","<field>"] or ["regex","(<pat>)"].'},
        "model": {"type": "string", "description": "Optional model (must match your BYOK key)."},
    },
    "required": ["request"],
}

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "description": "ok | not_verified | payment_required | budget_exceeded"},
        "answer": {"type": "string", "description": "The verified answer (present when status=ok)."},
        "verified": {"type": "boolean", "description": "Whether the verifier passed (gates the charge)."},
        "charged": {"type": "boolean", "description": "True only if verified; you pay nothing otherwise."},
        "amount_usd": {"type": "number", "description": "Service fee charged on a verified burst."},
        "settle_tx": {"type": "string", "description": "On-chain settlement tx hash when charged."},
        "remaining_budget_usd": {"type": "number", "description": "Wallet budget left after this call."},
    },
}


# Discovery 402 representation is canonical x402 **v2** (x402Version 2). Crawlers/
# registries (x402scan via @x402/core + @agentcash/discovery) reject v1 and require
# a v2 body: a top-level `resource` object, amount-based `accepts`, and the
# input/output JSON Schemas exposed under the `extensions.bazaar` discovery
# extension. The ACTUAL payment 402 (POST /v1/burst) builds its own SDK
# requirements via the broker and is unaffected — buyers sign against that.
def _accepts_for(price_usd):
    """Canonical x402 v2 `accepts` (amount-based, CAIP-2 network)."""
    return [{
        "scheme": "exact",
        "network": os.environ.get("X402_NETWORK", "eip155:8453"),
        "amount": str(int(round(price_usd * 1e6))),  # USDC has 6 decimals
        "asset": os.environ.get("X402_ASSET", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
        "payTo": os.environ.get("X402_PAY_TO", ""),
        "maxTimeoutSeconds": 300,
        "extra": {"name": "USD Coin", "version": "2"},
    }]


def _resource_info(base=PUBLIC_URL):
    """x402 v2 top-level `resource` object."""
    return {
        "url": f"{base}/v1/burst",
        "description": "Buy a verified inference burst. Pay only if the verifier passes.",
        "mimeType": "application/json",
        "serviceName": "Verified Burst",
    }


def _bazaar_ext():
    """x402 v2 `extensions.bazaar` discovery extension. `info` carries example-
    shaped input/output; `schema` carries the JSON Schemas where crawlers look
    (schema.properties.input.properties.body / output.properties.example)."""
    return {
        "bazaar": {
            "info": {"input": {"body": _INPUT_SCHEMA}, "output": _OUTPUT_SCHEMA},
            "schema": {
                "type": "object",
                "properties": {
                    "input":  {"properties": {"body": _INPUT_SCHEMA}},
                    "output": {"properties": {"example": _OUTPUT_SCHEMA}},
                },
            },
        }
    }


def _discovery_402(q, base=PUBLIC_URL):
    """Full canonical x402 v2 Payment-Required body for the GET discovery surface."""
    return {
        "x402Version": 2,
        "error": "payment_required",
        "resource": _resource_info(base),
        "accepts": _accepts_for(q["price_usd"]),
        "extensions": _bazaar_ext(),
        # Human/agent-friendly extras (ignored/stripped by x402 validators):
        "quote": q,
        "hint": ("POST to this URL with an X-PAYMENT header to buy a verified burst — "
                 "you are charged ONLY if the answer passes the verifier. "
                 "GET /v1/info for the full manifest."),
        "human_url": f"{base}/burst",
    }


def _manifest(base=PUBLIC_URL):
    """Self-describing service manifest for agent/crawler discovery."""
    q = pricing.quote()
    return {
        "x402Version": 2,
        "name": "Verified Burst",
        "description": ("Pay-per-correct-answer inference bursts for agents: escalate to fast "
                        "silicon, sample best-of-N, verify, and settle over x402 — charged ONLY "
                        "if the answer passes a verifier. BYOK; self-hosted settlement."),
        "resources": [{
            "method": "POST",
            "path": "/v1/burst",
            "url": f"{base}/v1/burst",
            "description": "Buy a verified inference burst. Pay only if the verifier passes.",
            "price": {"display": f"${q['price_usd']}",
                      "amount": str(int(round(q['price_usd'] * 1e6))),
                      "currency": "USDC", "decimals": 6},
            "accepts": _accepts_for(q["price_usd"]),
            "requires_byok": REQUIRE_BYOK,
            "free_trial_bursts": TRIAL_CAP,  # first N per wallet run on the host key (still paid)
            "byok_header": "X-Provider-Key",
            "input_schema": _INPUT_SCHEMA,
        }],
        "quote_url": f"{base}/v1/quote",
        "human_url": f"{base}/burst",
        "facilitator": "self-hosted",
        "networks": [os.environ.get("X402_NETWORK", "eip155:8453")],
        "mcp": {"package": "verified-burst", "command": "verified-burst",
                "tool": "buy_verified_burst", "install": "pip install verified-burst"},
    }


def _key(d, *names, default=None):
    for n in names:
        if n in d:
            return d[n]
    return default


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, obj, extra_headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        base = _base_url_for(self.headers.get("Host"))
        if u.path == "/healthz":
            return self._send(200, {"ok": True})
        if u.path in ("/.well-known/x402", "/v1/info"):
            # machine-readable discovery manifest (cacheable)
            return self._send(200, _manifest(base), {"Cache-Control": "public, max-age=300"})
        if u.path == "/v1/quote":
            qs = parse_qs(u.query)
            return self._send(200, pricing.quote(
                strategy=qs.get("strategy", ["best_of_n"])[0],
                n=int(qs.get("n", ["3"])[0])))
        if u.path == "/v1/burst":
            # A bare GET on the paid resource: answer with the x402 challenge so a
            # curious agent/dev sees HOW to pay instead of a dead-end 404. No burst
            # runs and nothing is charged — this is discovery, not purchase. To buy,
            # POST here with an X-PAYMENT header (charged only if the verifier passes).
            qs = parse_qs(u.query)
            try:
                q = pricing.quote(strategy=qs.get("strategy", ["best_of_n"])[0],
                                  n=int(qs.get("n", ["3"])[0]))
            except (ValueError, KeyError):
                q = pricing.quote()
            return self._send(402, _discovery_402(q, base), {"Cache-Control": "public, max-age=60"})
        return self._send(404, {"error": "not_found"})

    def _client_ip(self):
        # Behind nginx (bound to localhost), X-Real-IP is set by us to the real
        # peer and is not client-spoofable. Fall back to XFF[0], then socket.
        return (self.headers.get("X-Real-IP")
                or self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                or self.client_address[0])

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/v1/burst":
            return self._send(404, {"error": "not_found"})
        if not _rate_ok(self._client_ip()):
            return self._send(429, {"error": "rate_limited", "retry_after_s": 60},
                              {"Retry-After": "60"})
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self._send(400, {"error": "bad_length"})
        if n > MAX_BODY:
            return self._send(413, {"error": "request_too_large", "max_bytes": MAX_BODY})
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad_json"})

        if not req.get("request"):
            return self._send(400, {"error": "missing 'request'"})
        if len(str(req["request"])) > MAX_REQ_CHARS:
            return self._send(413, {"error": "request_too_long", "max_chars": MAX_REQ_CHARS})

        # BYOK: buyer brings their own provider key via header (their tokens, their
        # rate limit). Never logged (log_message is silenced). The BYOK/free-trial
        # gate runs inside serve_burst AFTER the payment is validated — so a no-key
        # buyer can still pay and (within the per-wallet free-trial cap) run on the
        # host key, then is asked to bring their own. Non-paying callers never run a
        # burst, so there's still no free inference to extract.
        provider_key = self.headers.get("X-Provider-Key") or self.headers.get("X-Cerebras-Key")

        ak = req.get("answer_key")
        result = broker.serve_burst(
            req["request"],
            x_payment=self.headers.get("X-PAYMENT"),
            strategy=req.get("strategy", "best_of_n"),
            n=int(req.get("n", 3)),
            verifier=req.get("verifier", "self_consistency"),
            answer_key=tuple(ak) if isinstance(ak, list) else None,
            provider_key=provider_key,
            model=req.get("model"),
            require_byok=REQUIRE_BYOK,
            trial_cap=TRIAL_CAP,
        )

        if result["status"] == "payment_required":
            return self._send(402, {"x402Version": 1, "accepts": result["accepts"],
                                    "quote": result["quote"], "error": "payment_required"})
        if result["status"] == "byok_required":
            return self._send(400, {"error": "byok_required", "hint": result.get("hint"),
                                    "trial_used": result.get("trial_used"),
                                    "trial_cap": result.get("trial_cap")})
        if result["status"] == "budget_exceeded":
            return self._send(402, result)
        # not_verified -> 200 with charged:false (honest: no charge); ok -> 200 charged:true
        hdrs = {"X-PAYMENT-RESPONSE": result["tx"]} if result.get("tx") else None
        return self._send(200, result, hdrs)


def main():
    port = int(os.environ.get("PORT", "8402"))
    mode = os.environ.get("X402_MODE", "sim")
    print(f"verified-burst broker on {BIND_HOST}:{port}  "
          f"(x402={mode}, rate={RATE_PER_MIN}/min/ip, model={pricing.quote()['model']})")
    ThreadingHTTPServer((BIND_HOST, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
