"""test_bestprice_settle.py — proves the settle-window hardening ported into
bestprice.serve_search on 2026-07-28 actually holds.

The bug this guards: a settle CALL that dies (process death / lost response) was being
collapsed into "you were NOT charged; retry with a fresh payment" with NO pending row —
so a buyer could be told to pay twice while the first tx may have landed, and nothing
would ever reconcile it.

Isolated ledger DB. No network: the search is stubbed, the facilitator is injected.
"""
import os
import sys
import tempfile

os.environ["LEDGER_DB"] = os.path.join(tempfile.gettempdir(), "vb_test_bestprice_settle.db")
if os.path.exists(os.environ["LEDGER_DB"]):
    os.remove(os.environ["LEDGER_DB"])

import bestprice
import ledger

PAYER = "0xTESTPAYER"
fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{'' if cond else '  -> ' + detail}")
    if not cond:
        fails.append(name)


class Fac:
    """Injected facilitator. `mode` picks the settle behaviour under test."""
    sim = True

    def __init__(self, mode):
        self.mode = mode

    def verify(self, x_payment, reqs):
        return {"valid": True, "payer": PAYER}

    def settle(self, x_payment, reqs):
        if self.mode == "raise":
            raise RuntimeError("connection reset mid-settle")
        if self.mode == "malformed":
            return {}                      # no "success" key at all
        if self.mode == "fail":
            return {"success": False}
        return {"success": True, "tx": "0xTX", "mode": "sim"}


# Stub the search so no network is touched and deals always exist.
bestprice._search = lambda q: {"query": q, "deals": [{"name": "x", "price": 1}], "source": "stub"}


def run(mode, payment):
    return bestprice.serve_search("widget", x_payment=payment, facilitator=Fac(mode))


print("\nbestprice settle-window hardening")

# 1) settle RAISES -> ambiguous. Must NOT claim "not charged", MUST leave a pending row.
r = run("raise", "pay-raise")
check("raise -> status settle_unresolved", r.get("status") == "settle_unresolved", str(r.get("status")))
check("raise -> charged is None (not False)", r.get("charged") is None, repr(r.get("charged")))
check("raise -> does NOT tell buyer to retry with a fresh payment",
      "retry with a fresh payment" not in (r.get("hint") or ""), r.get("hint", ""))
pend = {p["payer"] for p in ledger.pending_list()}
check("raise -> pending row RECORDED for reconciliation", PAYER in pend, str(ledger.pending_list()))
check("raise -> hold released", ledger.reserved(PAYER) == 0.0, str(ledger.reserved(PAYER)))

# clear the row so the next case starts clean
for p in ledger.pending_list():
    ledger.pending_clear(p["nonce"])

# 2) settle reports explicit FAILURE -> clean. Safe to say "not charged", no row left.
r = run("fail", "pay-fail")
check("fail -> status settle_failed", r.get("status") == "settle_failed", str(r.get("status")))
check("fail -> charged is False", r.get("charged") is False, repr(r.get("charged")))
check("fail -> pending row CLEARED", not ledger.pending_list(), str(ledger.pending_list()))
check("fail -> hold released", ledger.reserved(PAYER) == 0.0, str(ledger.reserved(PAYER)))

# 3) malformed facilitator dict (no "success") must not raise past the hold.
before = ledger.reserved(PAYER)
try:
    r = run("malformed", "pay-malformed")
    raised = False
except Exception as e:
    r, raised = {}, True
check("malformed dict -> no exception escapes", not raised)
check("malformed dict -> treated as failure", r.get("status") == "settle_failed", str(r.get("status")))
check("malformed dict -> hold not stranded", ledger.reserved(PAYER) == 0.0, str(ledger.reserved(PAYER)))

# 4) happy path still settles and clears its row.
r = run("ok", "pay-ok")
check("ok -> status ok", r.get("status") == "ok", str(r.get("status")))
check("ok -> charged True", r.get("charged") is True, repr(r.get("charged")))
check("ok -> pending row cleared", not ledger.pending_list(), str(ledger.pending_list()))

print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}\n")
sys.exit(1 if fails else 0)
