"""test_concurrency.py — exercise the money path's concurrency guarantees for real.

Locking guarantees are cheap to assert and expensive to verify. `ledger.reserve` holds that
two concurrent bursts from one wallet cannot both clear the cap. `claim_nonce` holds that K
parallel requests sharing one authorization yield exactly one winner. `judge_enter` holds
that the breaker counts in-flight work so it cannot be raced. `trial_claim` holds that a
single-statement check-and-write beats read-then-act. Each is a property the code is meant
to have, and production is the wrong place to discover that it does not — a settle window
racing for the first time against real USDC on Base mainnet is not a test.

This proves the guards under genuine contention. Every thread waits on a barrier and is
released together, so the race is real rather than incidental.

ZERO COST, ZERO RISK: isolated temp ledger (never production), sim facilitator (no chain,
no USDC), injected call_fn (no provider, no tokens). Run: .venv/bin/python test_concurrency.py
"""
import base64
import json
import os
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

os.environ["LEDGER_DB"] = os.path.join(tempfile.gettempdir(), "vb_test_concurrency.db")
if os.path.exists(os.environ["LEDGER_DB"]):
    os.remove(os.environ["LEDGER_DB"])

import env; env.load_env()
import ledger
import broker
from x402_gate import Facilitator

fails = []


def check(name, cond, got=None):
    print(("  ok  " if cond else " FAIL ") + name + ("" if cond else "   got=%r" % (got,)))
    if not cond:
        fails.append(name)


def storm(fn, n):
    """Run fn() on n threads released simultaneously. A barrier is the difference between
    testing a race and hoping for one."""
    bar = threading.Barrier(n)
    def one(i):
        bar.wait()
        try:
            return fn(i)
        except Exception as e:
            return "EXC:" + type(e).__name__
    with ThreadPoolExecutor(max_workers=n) as ex:
        return list(ex.map(one, range(n)))


def payment(nonce, sig="0xsig"):
    """A payment shaped exactly as _payment_key decodes, so claim_nonce actually runs.
    x_payment='sim' keys to None and SKIPS single-use entirely — testing with it would
    prove nothing about replay."""
    return base64.b64encode(json.dumps(
        {"payload": {"authorization": {"nonce": nonce}, "signature": sig}}).encode()).decode()


class OKFac(Facilitator):
    def __init__(self, payer): self.payer = payer
    def verify(self, xp, reqs): return {"valid": True, "payer": self.payer}
    def settle(self, xp, reqs): return {"success": True, "tx": "0xSIM", "mode": "sim"}


def fake_call(msgs, temperature=0.0, **kw):
    return {"text": "42", "usage": {}, "latency_s": 0.0}


print("\nmoney-path concurrency — guards under real contention")
ledger.reset_all()

# 1) claim_nonce: one authorization, many simultaneous claimants -> exactly one winner.
N = 32
res = storm(lambda i: ledger.claim_nonce("nonce-A"), N)
check("claim_nonce: exactly 1 of %d concurrent claims wins" % N,
      res.count(True) == 1, "True=%d False=%d" % (res.count(True), res.count(False)))

# 2) reserve: hard budget cap must hold under contention (no overspend).
CAP, PRICE, M = 1.0, 0.4, 16          # cap allows exactly 2 holds of 0.4
res = storm(lambda i: ledger.reserve("0xcap", PRICE, CAP), M)
won = res.count(True)
check("reserve: at most floor(cap/price)=2 of %d concurrent reserves win" % M, won == 2, won)
check("reserve: total held never exceeds the cap",
      round(ledger.reserved("0xcap"), 6) <= CAP, ledger.reserved("0xcap"))

# 3) trial_claim: free-trial cap is the classic read-then-act bug. Must be exact.
res = storm(lambda i: ledger.trial_claim("0xtrial", 1), 24)
check("trial_claim: exactly 1 free trial granted from 24 concurrent claims",
      res.count(True) == 1, res.count(True))
check("trial_claim: stored count matches what was granted",
      ledger.trial_count("0xtrial") == 1, ledger.trial_count("0xtrial"))

# 4) judge_enter: breaker counts in-flight work, so it cannot be raced past its limit.
LIM = 3
res = storm(lambda i: ledger.judge_enter("0xjudge", LIM), 20)
check("judge_enter: exactly %d admitted from 20 concurrent (in-flight counted)" % LIM,
      res.count(True) == LIM, res.count(True))
for _ in range(res.count(True)):
    ledger.judge_exit("0xjudge")
check("judge_exit: slots fully returned (re-admit works after release)",
      ledger.judge_enter("0xjudge", LIM) is True)
ledger.judge_exit("0xjudge")

# 5) global_judge_reserve: the Sybil cap across DIFFERENT wallets.
DAILY = 5
res = storm(lambda i: ledger.global_judge_reserve(1, DAILY), 20)
check("global_judge_reserve: exactly %d of 20 concurrent reservations win" % DAILY,
      res.count(True) == DAILY, res.count(True))

# 6) END TO END: one signed authorization, replayed by many concurrent requests.
#    Exactly one may produce a result; the rest must be refused as already used.
ledger.reset_all()
XP = payment("nonce-e2e")
def burst(i):
    return broker.serve_burst("what is 6*7?", x_payment=XP, strategy="fast", n=1,
                              verifier="self_consistency", facilitator=OKFac("0xe2e"),
                              call_fn=fake_call, budget_cap=100.0)["status"]
res = storm(burst, 16)
used = res.count("payment_already_used")
served = [r for r in res if r != "payment_already_used"]
check("serve_burst: 1 of 16 concurrent replays served, 15 refused",
      len(served) == 1 and used == 15, "served=%r used=%d" % (served, used))
check("serve_burst: the one served did not error", served and not str(served[0]).startswith("EXC"), served)

# 7) A DIFFERENT authorization must still work (the dedup is per-payment, not a global lock).
r = broker.serve_burst("what is 6*7?", x_payment=payment("nonce-other"), strategy="fast", n=1,
                       verifier="self_consistency", facilitator=OKFac("0xe2e"),
                       call_fn=fake_call, budget_cap=100.0)
check("serve_burst: a fresh authorization is unaffected", r["status"] == "ok", r["status"])

# 8) CRASH SAFETY: a hold outstanding at boot is stranded by definition and must be
#    recovered LOUDLY, not silently left shrinking that payer's cap forever.
ledger.reset_all()
ledger.reserve("0xcrash", 0.5, 10.0)
check("crash: hold is outstanding before recovery", ledger.reserved("0xcrash") == 0.5,
      ledger.reserved("0xcrash"))
recovered = ledger.recover_holds()
check("crash: recover_holds REPORTS the stranded hold (not silent)",
      any(h.get("payer") == "0xcrash" for h in recovered), recovered)
check("crash: hold released after recovery", ledger.reserved("0xcrash") == 0.0,
      ledger.reserved("0xcrash"))

print("\n%s\n" % ("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
