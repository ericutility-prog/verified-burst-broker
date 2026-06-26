"""Adversarial security self-audit for the verified-burst broker.

This is the "self-improving security layer" done safely: it does NOT rewrite the
broker. It ATTACKS the broker's own security invariants, fails LOUD on any
regression (non-zero exit -> usable as a CI gate / scheduled probe), and prints a
hardening FRONTIER so each run points at the next thing to fix. Humans apply the
fixes; this loop just makes weaknesses impossible to ignore.

Two surfaces:
  * in-process (sim facilitator / scripted models) — money-path + input invariants,
  * live HTTP against the running broker (127.0.0.1:8402) — the real edge.

Run:  .venv/bin/python security_probe.py            (exits 1 if any HIGH/MED fails)
"""
import json
import os
import sys
import time
import tempfile
import threading
import urllib.request
import urllib.error
import concurrent.futures as cf

# In-process money checks must hit an ISOLATED ledger/flagstore, never production.
os.environ.setdefault("LEDGER_DB", os.path.join(tempfile.gettempdir(), "vb_probe.db"))
os.environ.setdefault("FLAGSTORE_DB", os.path.join(tempfile.gettempdir(), "vb_probe_flags.db"))
import env; env.load_env()
import ledger
import flagstore
import broker
import burst
import pricing
import server
from x402_gate import Facilitator

LIVE = os.environ.get("PROBE_BASE", "http://127.0.0.1:8402")
RESULTS = []


def check(name, area, severity):
    """Register a check; the decorated fn returns (passed: bool, detail: str)."""
    def deco(fn):
        try:
            passed, detail = fn()
        except Exception as e:
            passed, detail = False, f"probe error: {type(e).__name__}: {e}"
        RESULTS.append({"name": name, "area": area, "severity": severity,
                        "passed": bool(passed), "detail": detail})
        return fn
    return deco


class _OKFac(Facilitator):
    """Sim facilitator that ALWAYS authorizes+settles — lets us drive the money
    path deterministically without touching the chain or spending real USDC."""
    def __init__(self, payer="0xprobe"): self.payer = payer
    def verify(self, xp, reqs): return {"valid": True, "payer": self.payer, "mode": "sim"}
    def settle(self, xp, reqs): return {"success": True, "tx": "0xSIM", "mode": "sim"}


def _miss(msgs, temperature=0.0):
    return {"text": "definitely-wrong", "usage": {}, "latency_s": 0.0}


def _pass(msgs, temperature=0.0):
    return {"text": '{"adequate": true}', "usage": {}, "latency_s": 0.0}


def _http(method, path, headers=None, body=None, timeout=8, ip=None):
    # Each check uses a DISTINCT simulated client IP (X-Real-IP, which the broker
    # honors first) so the per-IP rate limiter can't make checks interfere — the
    # suite stays deterministic and re-runnable (incl. back-to-back from the Guardian).
    url = LIVE + path
    h = dict(headers or {})
    if ip:
        h["X-Real-IP"] = ip
    req = urllib.request.Request(url, method=method, data=body, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# MONEY PATH (in-process, sim) — no real funds move
# ---------------------------------------------------------------------------
@check("no payment never runs a burst or charges", "money", "high")
def _():
    r = broker.serve_burst("decide", x_payment=None, facilitator=Facilitator(),
                           call_fn=_pass, verifier="judge")
    return r["status"] == "payment_required", f"status={r['status']}"


@check("forged/garbage X-PAYMENT is rejected", "money", "high")
def _():
    r = broker.serve_burst("decide", x_payment="@@not-a-real-payment@@",
                           facilitator=Facilitator(), call_fn=_pass, verifier="judge")
    return r["status"] == "payment_required", f"status={r['status']}"


@check("a verifier MISS is never charged", "money", "high")
def _():
    r = broker.serve_burst("decide", x_payment="sim", facilitator=_OKFac(),
                           call_fn=_miss, verifier="judge")
    return (r["status"] == "not_verified" and r.get("charged") is False
            and r.get("price_usd") == 0.0), f"status={r['status']} charged={r.get('charged')}"


@check("budget governor holds under concurrency (no overspend)", "money", "high")
def _():
    ledger.reset_all()
    payer, cap, price = "0xwhale", 0.01, pricing.quote(verifier="judge")["price_usd"]
    afford = int(cap // price)
    oks, lock = [], threading.Lock()
    def w():
        r = broker.serve_burst("decide", x_payment="sim", verifier="judge",
                               provider_key="byok", call_fn=_pass,
                               facilitator=_OKFac(payer), budget_cap=cap)
        with lock: oks.append(r["status"])
    ts = [threading.Thread(target=w) for _ in range(50)]
    [t.start() for t in ts]; [t.join() for t in ts]
    ok = oks.count("ok")
    spent = ledger.spent(payer)
    resid = ledger.reserved(payer)
    ledger.reset_all()
    return (ok == afford and spent <= cap + 1e-9 and resid < 1e-9), \
        f"settled={ok} (cap fits {afford}), spent=${spent:.4f}<=${cap}, holds released"


@check("independent_judge refuses to burn host tokens without BYOK", "abuse", "high")
def _():
    r = broker.serve_burst("decide", x_payment="sim", facilitator=_OKFac("0xnokey"),
                           verifier="independent_judge", model="gpt-oss-120b")
    return r["status"] == "byok_required", f"status={r['status']}"


@check("verified-flag store rejects unverified flags (no poisoning)", "abuse", "high")
def _():
    flagstore.reset_all()
    rej = flagstore.record_verified_catch("0xX", "address", "claim",
                                          {"independent": False, "verified": True})
    adm = flagstore.record_verified_catch("0xX", "address", "real",
                                          {"independent": True, "verified": True,
                                           "verifier_model": "zai-glm-4.7", "receipt_id": "r"})
    hit = bool(flagstore.check_known("0xX", "address"))
    flagstore.reset_all()
    return (rej is False and adm is True and hit), \
        f"reject_unverified={rej is False} admit_verified={adm} lookup_hit={hit}"


@check("unproven wallet is cut off after the miss streak", "abuse", "high")
def _():
    ledger.reset_all()
    atk = "0xsybil"
    for _ in range(broker.IJ_MISS_LIMIT):
        broker.serve_burst("x", x_payment="sim", facilitator=_OKFac(atk),
                           verifier="independent_judge", provider_key="byok", call_fn=_miss)
    r = broker.serve_burst("x", x_payment="sim", facilitator=_OKFac(atk),
                           verifier="independent_judge", provider_key="byok", call_fn=_miss)
    ledger.reset_all()
    return r["status"] == "verifier_locked", f"after {broker.IJ_MISS_LIMIT} misses -> {r['status']}"


# ---------------------------------------------------------------------------
# INPUT ROBUSTNESS (in-process)
# ---------------------------------------------------------------------------
@check("caller answer_key regex can't ReDoS a worker", "input", "high")
def _():
    # Run the extractor in a KILLABLE subprocess: a true catastrophic backtrack
    # can't be interrupted in-thread (stdlib `re` is C-level), so an in-thread
    # timeout would just leak a spinning thread. subprocess.run kills on timeout.
    import subprocess
    code = ("import burst; burst._extract('a'*60+'!', ('regex','(a+)+$'))")
    try:
        subprocess.run([sys.executable, "-c", code], cwd=os.path.dirname(__file__),
                       timeout=2.0, capture_output=True)
        return True, "catastrophic pattern (a+)+$ neutralized (returned fast)"
    except subprocess.TimeoutExpired:
        return False, ("(a+)+$ on 60 chars ran >2s — a caller-supplied answer_key "
                       "regex can wedge a worker thread (catastrophic backtracking)")


@check("oversized regex is rejected, normal regex still works", "input", "med")
def _():
    big = burst._extract("x", ("regex", "(" + "a" * 300 + ")"))   # >200 -> ""
    good = burst._extract("answer: yes", ("regex", r"(yes|no)"))
    num = burst._extract("the result is 2491", ("regex", r"(\d+)"))
    return big == "" and good == "yes" and num == "2491", f"big={big!r} good={good!r} num={num!r}"


# ---------------------------------------------------------------------------
# STATIC INVARIANTS (lock properties against silent regression)
# ---------------------------------------------------------------------------
@check("500 responses never leak a stack trace / message", "leak", "high")
def _():
    src = open(os.path.join(os.path.dirname(__file__), "server.py")).read()
    ok = 'type(e).__name__' in src and '"detail": str(e)' not in src
    return ok, "internal_error returns type name only" if ok else "500 path may leak str(e)"


@check("prompts/keys are never logged (log_message silenced)", "leak", "med")
def _():
    src = open(os.path.join(os.path.dirname(__file__), "server.py")).read()
    return "def log_message(self, *a):  # quiet" in src, "request logging is silenced"


@check("SIM mode refuses to bind a public interface", "config", "high")
def _():
    src = open(os.path.join(os.path.dirname(__file__), "server.py")).read()
    return "refusing to serve SIM mode on a public interface" in src, "boot guard present"


# ---------------------------------------------------------------------------
# LIVE EDGE (against the running broker)
# ---------------------------------------------------------------------------
@check("LIVE: GET /v1/burst is discovery only (402, no charge)", "live", "high")
def _():
    s, _ = _http("GET", "/v1/burst", ip="10.0.0.1")
    return s == 402, f"HTTP {s}"


@check("LIVE: POST /v1/burst with no X-PAYMENT -> 402", "live", "high")
def _():
    s, _ = _http("POST", "/v1/burst", {"Content-Type": "application/json"},
                 b'{"request":"x"}', ip="10.0.0.2")
    return s == 402, f"HTTP {s}"


@check("LIVE: oversized body is capped (413)", "live", "med")
def _():
    s, _ = _http("POST", "/v1/burst", {"Content-Type": "application/json"},
                 b'{"request":"' + b"A" * 40000 + b'"}', ip="10.0.0.3")
    return s == 413, f"HTTP {s}"


@check("LIVE: malformed quote input doesn't 500", "live", "med")
def _():
    s, _ = _http("GET", "/v1/quote?n=abc", ip="10.0.0.4")
    return s == 200, f"HTTP {s}"


@check("LIVE: spoofed Host is not echoed in discovery URLs", "live", "high")
def _():
    s, body = _http("GET", "/v1/info", {"Host": "evil.attacker.example"}, ip="10.0.0.5")
    leaked = "evil.attacker.example" in body
    return s == 200 and not leaked, "attacker Host not reflected" if not leaked else "Host REFLECTED"


@check("LIVE: rate limiter trips on the paid path", "live", "med")
def _():
    # fire past the per-IP/min cap; expect at least one 429 (do this LAST — it
    # consumes the localhost rate bucket for ~60s)
    seen429 = False
    for _ in range(server.RATE_PER_MIN + 6):
        s, _b = _http("POST", "/v1/burst", {"Content-Type": "application/json"}, b'{"request":"x"}', ip="10.0.0.99")
        if s == 429:
            seen429 = True; break
    return seen429, "429 observed past the limit" if seen429 else "no 429 — limiter not enforcing"


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
FRONTIER = [
    ("durable ledger", "done", "spend/holds/breakers now in a sqlite ledger (ledger.py) with "
     "atomic transactions — survives restart, correct under concurrency."),
    ("ReDoS depth", "partial", "nested-quantifier screen added; full safety needs the `regex` "
     "module timeout or subprocess isolation (stdlib `re` can't be interrupted)."),
    ("secrets at rest", "open", "keys live in .env / config.json plaintext — move to a secrets "
     "manager / OS keyring; never on disk beside the code."),
    ("CI gate", "open", "wire this probe + the test suite into CI (and a systemd timer) so a "
     "regression is caught before deploy, not after."),
    ("typed money path", "open", "add type hints + mypy on broker/burst/server so a wrong-typed "
     "amount can't reach settlement."),
]


def main():
    t0 = time.monotonic()
    hi = [r for r in RESULTS if r["severity"] == "high"]
    fails = [r for r in RESULTS if not r["passed"]]
    hifail = [r for r in fails if r["severity"] == "high"]
    medfail = [r for r in fails if r["severity"] == "med"]

    print(f"\nverified-burst security self-audit — {len(RESULTS)} checks, "
          f"{len(RESULTS)-len(fails)} pass / {len(fails)} fail\n")
    for r in RESULTS:
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"  [{mark}] ({r['severity']:>4}/{r['area']:<6}) {r['name']}")
        if not r["passed"]:
            print(f"         -> {r['detail']}")
    print("\n  hardening frontier (what would move it toward production-grade):")
    for name, state, why in FRONTIER:
        print(f"    - [{state:>7}] {name}: {why}")

    out = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "checks": RESULTS, "frontier": [dict(item=n, state=s, note=w) for n, s, w in FRONTIER],
           "summary": {"total": len(RESULTS), "failed": len(fails),
                       "high_failed": len(hifail), "med_failed": len(medfail)}}
    with open("security_report.json", "w") as f:
        json.dump(out, f, indent=2)
    print(f"\n[wrote security_report.json · {time.monotonic()-t0:.1f}s]")

    # CI gate: any HIGH or MED failure fails the run.
    if hifail or medfail:
        print(f"\nFAILED: {len(hifail)} high + {len(medfail)} med security checks regressed.")
        sys.exit(1)
    print("\nALL HIGH/MED SECURITY INVARIANTS HOLD.")


if __name__ == "__main__":
    main()
