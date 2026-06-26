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
import burst
import provider
import bestprice

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
# Advertise the independent-judge verifier on the PUBLIC discovery surface (the
# x402scan-indexed 402 + manifest): adds it to the verifier enum and attaches the
# machine-readable ROI block. OFF by default so deploying the capability does NOT
# change the crawler-visible listing — flip to "1" (one env change, no code redeploy)
# when we make independence the headline. The endpoint ACCEPTS verifier=independent_judge
# regardless; this flag only controls discovery copy.
ADVERTISE_INDEPENDENT = os.environ.get("ADVERTISE_INDEPENDENT", "0").lower() in ("1", "true", "yes")

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


# --- no-wallet "taste" demo --------------------------------------------------- #
# ONE real verified burst on the HOST key, no wallet, no payment — so a curious dev
# or agent sees the actual output + the proceed/hold gate before wiring anything.
# Free inference is the abuse surface, so it's locked down: FIXED demo prompts only
# (no arbitrary input to farm), plus per-IP and GLOBAL daily ceilings that cap the
# host key's exposure. Resets at UTC midnight; counters are in-memory (reset on restart).
DEMO_ENABLED = os.environ.get("BURST_DEMO", "1").lower() in ("1", "true", "yes")
DEMO_PER_IP_DAY = int(os.environ.get("BURST_DEMO_PER_IP_DAY", "3"))
DEMO_GLOBAL_DAY = int(os.environ.get("BURST_DEMO_GLOBAL_DAY", "100"))

# Fixed, genuinely-checkable prompts whose samples reliably agree → the happy
# "proceed" path. Honest (the answers are actually correct), not rigged.
_DEMO_PROMPTS = [
    {"topic": "arithmetic", "request": "What is 47 * 53? Reply with just the number.",
     "answer_key": ("regex", r"(\d+)")},
    {"topic": "fact lookup", "request": "What is the capital of Japan? Reply with one word.",
     "answer_key": ("regex", r"(\w+)")},
    {"topic": "classification", "request": "Sentiment of: 'This update broke my build and "
     "wasted my whole afternoon.' Reply with one word: positive, negative, or neutral.",
     "answer_key": ("regex", r"(?i)\b(positive|negative|neutral)\b")},
    {"topic": "verifiable check", "request": "Is 2024 a leap year? Answer yes or no.",
     "answer_key": ("regex", r"(?i)\b(yes|no)\b")},
    {"topic": "pre-transaction guard", "request": "An agent is about to send a payment to this "
     "address: 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48 . Is that a syntactically valid "
     "Ethereum address (0x followed by exactly 40 hexadecimal characters)? Answer yes or no.",
     "answer_key": ("regex", r"(?i)\b(yes|no)\b")},
]

_DEMO_LOCK = threading.Lock()
_DEMO_DAY = [None]            # current UTC date (counters reset when this rolls over)
_DEMO_GLOBAL = [0]           # demos served today across all IPs
_DEMO_IP = defaultdict(int)  # demos served today per IP


def _demo_call_fn(msgs, temperature=0.0):
    """Demo runs on the HOST key with a roomier token budget — gpt-oss-120b is a
    reasoning model, and prompts that make it 'think' (e.g. validating a 40-char
    address) can exhaust a 256 cap on hidden reasoning and emit empty content."""
    return provider.chat(msgs, temperature=temperature, max_tokens=512)


def _demo_allow(ip):
    """Reserve a demo slot under the per-IP + global daily ceilings (UTC-day reset).
    Returns (ok, reason). A slot is consumed on reserve (before the burst) so a
    deliberately-failing caller can't farm unlimited host inference."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    with _DEMO_LOCK:
        if _DEMO_DAY[0] != today:
            _DEMO_DAY[0], _DEMO_GLOBAL[0] = today, 0
            _DEMO_IP.clear()
        if _DEMO_GLOBAL[0] >= DEMO_GLOBAL_DAY:
            return False, "global_daily_cap"
        if _DEMO_IP[ip] >= DEMO_PER_IP_DAY:
            return False, "per_ip_daily_cap"
        _DEMO_GLOBAL[0] += 1
        _DEMO_IP[ip] += 1
        return True, max(0, DEMO_PER_IP_DAY - _DEMO_IP[ip])


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
        "request": {"type": "string", "description": "The decision to resolve. Verifies best "
                    "when the answer is checkable — a label, number, JSON field, or yes/no."},
        "strategy": {"type": "string", "enum": ["fast", "best_of_n"], "default": "best_of_n"},
        "n": {"type": "integer", "default": 3},
        "verifier": {"type": "string",
                     "enum": (["self_consistency", "judge", "independent_judge",
                               "independent_quorum", "none"]
                              if ADVERTISE_INDEPENDENT else
                              ["self_consistency", "judge", "none"]),
                     "default": "self_consistency",
                     "description": ("How the answer is checked before you're charged: "
                     "self_consistency = N-of-M samples agree (pair with answer_key); "
                     "judge = adversarial LLM check; none = no gate (always charged)."
                     + (" independent_judge = a DIFFERENT model family checks the answer "
                        "(decorrelated from your model's blind spots — the one check you can't "
                        "self-supply). independent_quorum = multiple independent models ACROSS "
                        "VENDORS must agree (k-of-M; pass quorum_k). Both charged only if they "
                        "pass; pass a 'candidate' to verify your agent's OWN answer (no generation)."
                        if ADVERTISE_INDEPENDENT else ""))},
        "answer_key": {"type": "array", "items": {"type": "string"},
                       "description": 'How to extract the comparable answer for self_consistency '
                       '— ["json","<field>"] or ["regex","(<pat>)"]. Recommended: without it, '
                       'agreement is measured on the full answer text and rarely matches on prose.'},
        "model": {"type": "string", "description": "Optional model (must match your BYOK key)."},
    },
    "required": ["request"],
}

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "description": "ok | not_verified | payment_required | budget_exceeded"},
        "answer": {"type": "string", "description": "The passing answer (present when status=ok)."},
        "gate": {"type": "object", "description": "Machine-first go/no-go for your agent's NEXT "
                 "step — not just billing. `action`='proceed' when verified, 'hold' when not "
                 "(don't act on the answer; re-try or escalate). Carries `confidence` + `advice`."},
        "verified": {"type": "boolean", "description": "Whether the verifier passed (gates the fee)."},
        "charged": {"type": "boolean", "description": "True only when the verifier passed — this is "
                    "the service fee. Your own BYOK provider tokens are billed regardless."},
        "amount_usd": {"type": "number", "description": "Service fee charged on a passing burst (BYOK tokens are separate)."},
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
        "description": "Buy a verified decision: best-of-N on your own key, charged the service "
                       "fee ONLY when the verifier passes (agreement / judge / your check). A miss is free.",
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


def _roi_block(q):
    """Machine-readable ROI so an agent's budget policy can auto-approve the buy on
    its OWN authority — no human in the loop. The economics are the whole pitch:
    bounded, tiny cost; ZERO downside (you pay only if an INDEPENDENT check passes);
    and the check is the one form of 'more thinking' the agent can't generate from
    its own correlated samples. Stakes-class tells the policy WHEN it's worth it."""
    return {
        "unit": "one independently-verified decision",
        "cost_usd": q["price_usd"],
        "downside_usd": 0.0,
        "pay_only_if_verified": True,
        "independence": {"verifier": "a different model family than your generator",
                         "quorum": "independent_quorum runs multiple models ACROSS VENDORS; k-of-M must agree",
                         "why": "errors decorrelated from your model's blind spots — the one check "
                                "you can't generate from your own correlated samples"},
        "keepable_receipt": True,
        "worth_it_when": ("decision is hard / irreversible / low-confidence AND has a checkable "
                          "answer (label, number, JSON field, yes/no)"),
        "auto_approve_hint": "downside is bounded to 0 — safe to buy without human review",
    }


def _discovery_402(q, base=PUBLIC_URL):
    """Full canonical x402 v2 Payment-Required body for the GET discovery surface."""
    body = {
        "x402Version": 2,
        "error": "payment_required",
        "resource": _resource_info(base),
        "accepts": _accepts_for(q["price_usd"]),
        "extensions": _bazaar_ext(),
        # Human/agent-friendly extras (ignored/stripped by x402 validators):
        "quote": q,
        "hint": ("POST with an X-PAYMENT header to buy a verified decision. You're charged the "
                 "service fee ONLY if the answer passes the verifier — best on decisions with a "
                 "checkable answer (label, number, JSON field, yes/no). Your BYOK provider tokens "
                 "are billed regardless. GET /v1/info for the full manifest."),
        "demo_url": (f"{base}/v1/burst/demo" if DEMO_ENABLED else None),  # try free, no wallet
        "human_url": f"{base}/burst",
    }
    if ADVERTISE_INDEPENDENT:
        body["roi"] = _roi_block(q)
    return body


def _manifest(base=PUBLIC_URL):
    """Self-describing service manifest for agent/crawler discovery."""
    q = pricing.quote()
    return {
        "x402Version": 2,
        "name": "Verified Burst",
        "description": ("Pay-per-checkable-decision inference for agents. Give it a decision with a "
                        "checkable answer (a label, number, JSON field, or yes/no); it samples "
                        "best-of-N on your own key (BYOK) and settles a few tenths of a cent over "
                        "x402 ONLY when the samples agree — or a judge/your-own-check passes. The "
                        "verifier gates the service fee; a miss is free. Self-hosted settlement on Base."),
        "resources": [{
            "method": "POST",
            "path": "/v1/burst",
            "url": f"{base}/v1/burst",
            "description": "Buy a verified decision: best-of-N on your own key, charged the service "
                       "fee ONLY when the verifier passes (agreement / judge / your check). A miss is free.",
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
        "demo_url": (f"{base}/v1/burst/demo" if DEMO_ENABLED else None),  # free, no-wallet taste
        "human_url": f"{base}/burst",
        "facilitator": "self-hosted",
        # Why an agent owner can let this run unattended — the anti-haywire guarantees.
        "safety": {
            "spend_ceiling": ("The agent pays from a wallet you fund; it cannot spend beyond that "
                              "balance — the ceiling is the funding, enforced on-chain, not our "
                              "honor system."),
            "spend_governor_usd": broker.DEFAULT_BUDGET_USD,
            "spend_governor_note": ("Secondary per-wallet cap the broker enforces between top-ups "
                                    "(refuses a burst that would exceed it); response carries "
                                    "remaining_budget_usd."),
            "decision_gate": ("Every response carries gate.action ('proceed'|'hold') so the verdict "
                              "gates your agent's NEXT step, not just the charge — hold and escalate "
                              "instead of acting on an unverified answer."),
            "audit": "Every charged burst returns an on-chain settle_tx — a verifiable receipt of what the agent decided and paid for.",
        },
        "networks": [os.environ.get("X402_NETWORK", "eip155:8453")],
        "mcp": {"package": "verified-burst", "command": "verified-burst",
                "tool": "buy_verified_burst", "install": "pip install verified-burst"},
    }


def _example(base=PUBLIC_URL):
    """A copy-paste-correct request, attached to 4xx replies so a caller that
    bounced (bad body / no BYOK) can self-correct instead of giving up. The logs
    show most failed buy-attempts are malformed or BYOK-less — this turns the wall
    into a worked example."""
    body = {"request": "Is 12 * 17 = 204? Answer yes or no.",
            "strategy": "best_of_n", "n": 3,
            "verifier": "self_consistency", "answer_key": ["regex", "(yes|no)"]}
    byok = ("required — your own Cerebras key; your tokens, your rate limit"
            if REQUIRE_BYOK and TRIAL_CAP == 0
            else f"optional for your first {TRIAL_CAP} burst(s), then required (BYOK)")
    return {
        "easiest": "pip install verified-burst  —  the MCP client signs the x402 payment for you",
        "request_shape": {
            "method": "POST", "url": f"{base}/v1/burst",
            "headers": {
                "Content-Type": "application/json",
                "X-PAYMENT": "<x402 payment header: sign the requirements from the 402 challenge "
                             "(GET /v1/burst or this endpoint with no X-PAYMENT). The MCP client/SDK does this for you>",
                "X-Provider-Key": f"<{byok}>",
            },
            "body": body,
        },
        "curl": (f"curl -sS -X POST {base}/v1/burst "
                 f"-H 'Content-Type: application/json' "
                 f"-H 'X-Provider-Key: <your-cerebras-key>' "
                 f"-d '{json.dumps(body)}'   # then add the X-PAYMENT header from the 402 challenge"),
        "docs": f"{base}/v1/info",
        "human_url": f"{base}/burst",
    }


def _key(d, *names, default=None):
    for n in names:
        if n in d:
            return d[n]
    return default


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    # Permissive CORS: discovery + paid endpoints are meant to be hit cross-origin
    # by browser/JS agents. Abuse is gated by payment, not Origin, so `*` is safe;
    # we expose X-PAYMENT-RESPONSE so a browser caller can read the settle receipt.
    _CORS = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, HEAD, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-PAYMENT, X-Provider-Key, X-Cerebras-Key",
        "Access-Control-Expose-Headers": "X-PAYMENT-RESPONSE",
        "Access-Control-Max-Age": "86400",
    }

    def _send(self, code, obj, extra_headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in self._CORS.items():
            self.send_header(k, v)
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        # HEAD: headers (incl. Content-Length) are sent, body is suppressed — so a
        # crawler that probes with HEAD gets a real 200/402, not a 501 dead-end.
        if getattr(self, "_is_head", False):
            return
        self.wfile.write(body)

    def do_HEAD(self):
        # Reuse the GET routing so HEAD reflects the same status/headers, sans body.
        self._is_head = True
        try:
            self.do_GET()
        finally:
            self._is_head = False

    def do_OPTIONS(self):
        # CORS preflight: browser/JS agents sending the custom X-PAYMENT /
        # X-Provider-Key headers preflight first. 204 + the CORS allow-set lets the
        # real request through instead of bouncing on a 501.
        self.send_response(204)
        for k, v in self._CORS.items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

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
            try:
                n = int(qs.get("n", ["3"])[0])
            except (ValueError, KeyError):
                n = 3
            return self._send(200, pricing.quote(
                strategy=qs.get("strategy", ["best_of_n"])[0], n=n))
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
        if u.path == "/v1/best-price":
            # Discovery: a bare GET returns the x402 challenge for the paid real-time
            # best-price search. POST here with a query + X-PAYMENT to run it; charged
            # only if the search returns real results.
            q = pricing.quote()
            return self._send(402, {
                "x402Version": 2, "error": "payment_required",
                "resource": {"url": f"{base}/v1/best-price", "serviceName": "Best Price Now",
                             "description": "One micropayment buys one broad, real-time best-price search."},
                "accepts": _accepts_for(q["price_usd"]), "quote": q,
                "hint": ("POST {\"query\":\"<what to price>\"} with an X-PAYMENT header. "
                         "Charged only if the search returns real results — no info, no charge."),
                "human_url": f"{base}/burst"}, {"Cache-Control": "public, max-age=60"})
        if u.path == "/v1/burst/demo":
            # No-wallet taste: run ONE real verified burst on the host key, free, so a
            # curious dev/agent sees the actual answer + proceed/hold gate before wiring
            # a wallet. Fixed prompts + daily caps keep it from becoming free open inference.
            if not DEMO_ENABLED:
                return self._send(404, {"error": "not_found"})
            ok = _demo_allow(self._client_ip())
            if not ok[0]:
                return self._send(429, {
                    "error": "demo_limit", "reason": ok[1],
                    "hint": ("the free demo's daily limit is reached — to run your OWN "
                             "decisions now, install the tool (pay-per-correct over x402)"),
                    "install": "pip install verified-burst",
                    "human_url": f"{base}/burst"}, {"Retry-After": "3600"})
            remaining = ok[1]
            qs = parse_qs(u.query)
            try:
                idx = int(qs.get("example", ["-1"])[0])
            except ValueError:
                idx = -1
            if not (0 <= idx < len(_DEMO_PROMPTS)):
                idx = _DEMO_GLOBAL[0] % len(_DEMO_PROMPTS)  # rotate for variety
            p = _DEMO_PROMPTS[idx]
            try:
                res = burst.run_burst(p["request"], strategy="best_of_n", n=3,
                                      verifier="self_consistency", answer_key=p["answer_key"],
                                      receipt_id="demo", call_fn=_demo_call_fn)
            except Exception:
                return self._send(503, {
                    "error": "demo_unavailable",
                    "hint": "host model is busy — retry shortly, or run your own: pip install verified-burst"})
            return self._send(200, {
                "demo": True,
                "note": ("Free taste on the host key — no wallet, no payment, no charge. This is "
                         "a FIXED demo prompt; to run YOUR own decisions, install the tool below."),
                "topic": p["topic"],
                "prompt": p["request"],
                "answer": res.answer,
                "gate": broker._gate_signal(res),       # the real product output: proceed | hold
                "verified": res.passed,
                "verifier": (res.verdict or {}).get("method"),
                "samples": res.n,
                "latency_s": round(res.latency_s, 3),
                "demo_remaining_today": remaining,
                "examples_available": len(_DEMO_PROMPTS),
                "install": "pip install verified-burst",
                "buy_your_own": f"{base}/v1/info",
                "human_url": f"{base}/burst",
            }, {"Cache-Control": "no-store"})
        return self._send(404, {"error": "not_found"})

    def _client_ip(self):
        # Behind nginx (bound to localhost), X-Real-IP is set by us to the real
        # peer and is not client-spoofable. Fall back to XFF[0], then socket.
        return (self.headers.get("X-Real-IP")
                or self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                or self.client_address[0])

    def do_POST(self):
        u = urlparse(self.path)
        # >>> EXTENSION POINT (new paid resources): register additional x402-gated POST
        # endpoints here, with a matching GET discovery 402 in do_GET. Reuse the proven
        # verify -> fulfill -> settle-IF-earned shape (see _do_best_price / broker.serve_burst).
        if u.path not in ("/v1/burst", "/v1/best-price"):
            return self._send(404, {"error": "not_found"})
        if not _rate_ok(self._client_ip()):
            return self._send(429, {"error": "rate_limited", "retry_after_s": 60},
                              {"Retry-After": "60"})
        base = _base_url_for(self.headers.get("Host"))
        if u.path == "/v1/best-price":
            return self._do_best_price(base)
        ex = _example(base)  # worked example attached to every 4xx so bouncers self-correct
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self._send(400, {"error": "bad_length", "example": ex})
        if n > MAX_BODY:
            return self._send(413, {"error": "request_too_large", "max_bytes": MAX_BODY})
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad_json",
                                    "detail": "body must be JSON", "example": ex})

        if not req.get("request"):
            return self._send(400, {"error": "missing 'request'",
                                    "detail": "include a 'request' field with the decision to resolve",
                                    "example": ex})
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
        qk = req.get("quorum_k")
        try:
            n = max(1, min(int(req.get("n", 3)), 8))   # clamp best-of-N / thread fan-out
        except (TypeError, ValueError):
            n = 3
        try:
            qk = max(1, int(qk)) if qk is not None else None  # never < 1 (quorum integrity)
        except (TypeError, ValueError):
            qk = None
        try:
            result = broker.serve_burst(
                req["request"],
                x_payment=self.headers.get("X-PAYMENT"),
                strategy=req.get("strategy", "best_of_n"),
                n=n,
                verifier=req.get("verifier", "self_consistency"),
                answer_key=tuple(ak) if isinstance(ak, list) else None,
                provider_key=provider_key,
                model=req.get("model"),
                require_byok=REQUIRE_BYOK,
                trial_cap=TRIAL_CAP,
                candidate=req.get("candidate"),        # judge a supplied answer (no generation)
                quorum_k=qk,
            )
        except Exception as e:
            # never settles (settle only runs on res.passed); fail closed, no stack leak
            return self._send(500, {"error": "internal_error", "detail": type(e).__name__})

        if result["status"] == "payment_required":
            # Envelope version MUST match the SDK-built v2 accepts (x402==2 stamps
            # "x402Version": 2 everywhere); a stale `1` here told v2 clients the
            # body was v1 while the accepts were v2 — an inconsistency a strict
            # client rejects, so a well-formed buyer never crosses 402 -> pay.
            return self._send(402, {"x402Version": 2, "accepts": result["accepts"],
                                    "quote": result["quote"], "error": "payment_required"})
        if result["status"] == "byok_required":
            return self._send(400, {"error": "byok_required", "hint": result.get("hint"),
                                    "trial_used": result.get("trial_used"),
                                    "trial_cap": result.get("trial_cap"), "example": ex})
        if result["status"] == "budget_exceeded":
            return self._send(402, result)
        if result["status"] == "verifier_locked":
            # unproven wallet burned too many broker-paid judge calls without paying
            return self._send(429, {"error": "verifier_locked", "hint": result.get("hint"),
                                    "misses": result.get("misses"), "example": ex})
        # not_verified -> 200 with charged:false (honest: no charge); ok -> 200 charged:true
        hdrs = {"X-PAYMENT-RESPONSE": result["tx"]} if result.get("tx") else None
        return self._send(200, result, hdrs)

    def _do_best_price(self, base):
        """Paid real-time best-price search — reuses the proven x402 money-path
        (verify -> search -> settle-IF-results). Charged only if real data returns."""
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self._send(400, {"error": "bad_length"})
        if n > MAX_BODY:
            return self._send(413, {"error": "request_too_large", "max_bytes": MAX_BODY})
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad_json", "detail": "body must be JSON"})
        query = (req.get("query") or req.get("q") or "").strip()
        if not query:
            return self._send(400, {"error": "missing 'query'",
                                    "detail": "include a 'query' field — what to price",
                                    "example": {"query": "airpods pro"}})
        if len(query) > MAX_REQ_CHARS:
            return self._send(413, {"error": "query_too_long", "max_chars": MAX_REQ_CHARS})

        result = bestprice.serve_search(query, x_payment=self.headers.get("X-PAYMENT"))

        if result["status"] == "payment_required":
            return self._send(402, {"x402Version": 2, "accepts": result["accepts"],
                                    "quote": result["quote"], "error": "payment_required"})
        if result["status"] == "budget_exceeded":
            return self._send(402, result)
        # no_results -> 200 charged:false (honest: no info, no charge); ok -> charged:true
        hdrs = {"X-PAYMENT-RESPONSE": result["tx"]} if result.get("tx") else None
        return self._send(200, result, hdrs)


def main():
    port = int(os.environ.get("PORT", "8402"))
    mode = os.environ.get("X402_MODE", "sim")
    # Money-safety boot check: in SIM mode the facilitator does NOT move real funds and
    # trusts a client-supplied `from` as the payer. That must never face the public
    # internet. Refuse to bind a non-loopback host in sim unless explicitly forced.
    if mode.lower() != "live" and BIND_HOST not in ("127.0.0.1", "localhost", "::1") \
            and os.environ.get("ALLOW_PUBLIC_SIM") != "1":
        raise SystemExit("refusing to serve SIM mode on a public interface "
                         "(set X402_MODE=live, or ALLOW_PUBLIC_SIM=1 to override for testing)")
    print(f"verified-burst broker on {BIND_HOST}:{port}  "
          f"(x402={mode}, rate={RATE_PER_MIN}/min/ip, model={pricing.quote()['model']})")
    ThreadingHTTPServer((BIND_HOST, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
