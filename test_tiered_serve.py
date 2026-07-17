"""Integration smoke: verifier='tiered' through the PAYABLE serve_burst, sim payment,
ISOLATED temp ledger (never touches production ledger.db). Uses real judges (candidate
supplied -> no generation, no BYOK): 'Paris' -> fast-path pass -> charged; 'London' ->
escalate -> reasoning hold -> not_verified/charged:false. Also checks the BYOK abuse gate
now covers tiered."""
import os, tempfile
os.environ["LEDGER_DB"] = os.path.join(tempfile.gettempdir(), "vb_test_tiered_serve.db")
import env; env.load_env()
import ledger, broker, pricing
from x402_gate import Facilitator

Q = "What is the capital of France? Reply with just the city."


class OKFac(Facilitator):
    def verify(self, xp, reqs): return {"valid": True, "payer": self.payer}
    def settle(self, xp, reqs): return {"success": True, "tx": "0xSIM", "mode": "sim"}
    def __init__(self, payer): self.payer = payer


def serve(payer, **kw):
    return broker.serve_burst(Q, x_payment="sim", verifier="tiered",
                              facilitator=OKFac(payer), **kw)


fails = []
def check(name, cond, got=None):
    print(("  ok  " if cond else " FAIL ") + name + ("" if cond else f"   got={got}"))
    if not cond:
        fails.append(name)


def main():
    ledger.reset_all()

    # pricing: tiered is priced (auto-tier fee), quotable up front
    q = pricing.quote(strategy="fast", n=1, verifier="tiered", judges=1)
    check("tiered priced (== independent auto tier 0.0035)", abs(q["price_usd"] - 0.0035) < 1e-9, q["price_usd"])

    # A) BYOK abuse gate now covers tiered: no key, no candidate -> byok_required (no judge call)
    r = serve("0xnokey", model="gpt-oss-120b")
    check("A: no-BYOK tiered -> byok_required", r["status"] == "byok_required", r["status"])

    # B) fast-path PASS on a correct candidate -> settle -> charged
    r = serve("0xpayerB", candidate="Paris", budget_cap=1000.0)
    check("B: correct candidate -> status ok", r["status"] == "ok", r["status"])
    check("B: charged True", r.get("charged") is True, r.get("charged"))
    check("B: verdict tier == fast", r["verdict"].get("tier") == "fast", r["verdict"].get("tier"))
    check("B: gate says proceed", r["gate"].get("action") == "proceed", r["gate"].get("action"))

    # C) wrong candidate -> escalate -> reasoning hold -> not_verified, NOT charged
    r = serve("0xpayerC", candidate="London", budget_cap=1000.0)
    check("C: wrong candidate -> not_verified", r["status"] == "not_verified", r["status"])
    check("C: charged False", r.get("charged") is False, r.get("charged"))
    check("C: verdict tier == escalated", r["verdict"].get("tier") == "escalated", r["verdict"].get("tier"))
    check("C: miss recorded for tiered", ledger.miss_count("0xpayerC") == 1, ledger.miss_count("0xpayerC"))

    print(f"\n{len(fails)} failed")
    if fails:
        print("FAILURES:", fails); raise SystemExit(1)
    print("TIERED PAID-PATH SMOKE PASS")


if __name__ == "__main__":
    main()
