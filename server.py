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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import env; env.load_env()
import pricing
import broker


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
        if u.path == "/healthz":
            return self._send(200, {"ok": True})
        if u.path == "/v1/quote":
            qs = parse_qs(u.query)
            return self._send(200, pricing.quote(
                strategy=qs.get("strategy", ["best_of_n"])[0],
                n=int(qs.get("n", ["3"])[0])))
        return self._send(404, {"error": "not_found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/v1/burst":
            return self._send(404, {"error": "not_found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad_json"})

        if not req.get("request"):
            return self._send(400, {"error": "missing 'request'"})

        ak = req.get("answer_key")
        # BYOK: buyer brings their own provider key via header (their tokens, their
        # rate limit). Never logged (log_message is silenced); falls back to our key.
        result = broker.serve_burst(
            req["request"],
            x_payment=self.headers.get("X-PAYMENT"),
            strategy=req.get("strategy", "best_of_n"),
            n=int(req.get("n", 3)),
            verifier=req.get("verifier", "self_consistency"),
            answer_key=tuple(ak) if isinstance(ak, list) else None,
            provider_key=self.headers.get("X-Provider-Key") or self.headers.get("X-Cerebras-Key"),
            model=req.get("model"),
        )

        if result["status"] == "payment_required":
            return self._send(402, {"x402Version": 1, "accepts": result["accepts"],
                                    "quote": result["quote"], "error": "payment_required"})
        if result["status"] == "budget_exceeded":
            return self._send(402, result)
        # not_verified -> 200 with charged:false (honest: no charge); ok -> 200 charged:true
        hdrs = {"X-PAYMENT-RESPONSE": result["tx"]} if result.get("tx") else None
        return self._send(200, result, hdrs)


def main():
    port = int(os.environ.get("PORT", "8402"))
    fac_mode = "SIM" if not os.environ.get("X402_FACILITATOR_URL") else "REAL"
    print(f"verified-burst broker on :{port}  (x402={fac_mode}, model={pricing.quote()['model']})")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
