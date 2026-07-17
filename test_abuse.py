"""Prove the independent_judge abuse defenses (Rules 1 & 2) without spending tokens.

Rule 1: independent_judge with no BYOK key -> byok_required (we never run the
        broker-paid judge on the host key, so a miss costs us at most the judge call).
Rule 2: a wallet gets a consecutive-miss allowance, then verifier_locked. Unproven ->
        flat IJ_MISS_LIMIT; proven -> revenue-scaled headroom, so an honest paying
        customer isn't locked for legitimate catches while a tiny-payment attacker stays
        bounded. A settled pass clears the streak.
"""
import os, tempfile
# Isolate the durable ledger from production — tests must never touch ledger.db.
os.environ.setdefault("LEDGER_DB", os.path.join(tempfile.gettempdir(), "vb_test_abuse.db"))
import env; env.load_env()
import ledger
import broker
from x402_gate import Facilitator


class OKFac(Facilitator):
    def verify(self, xp, reqs): return {"valid": True, "payer": self.payer}
    def settle(self, xp, reqs): return {"success": True, "tx": "0xSIM", "mode": "sim"}
    def __init__(self, payer): self.payer = payer


def always_miss(msgs, temperature=0.0):
    # generation -> a wrong answer; judge prompt -> unparseable -> adequate False.
    return {"text": "no", "usage": {"prompt_tokens": 0, "completion_tokens": 0}, "latency_s": 0.0}


def call(payer, **kw):
    return broker.serve_burst("decide something", x_payment="sim", verifier="independent_judge",
                              facilitator=OKFac(payer), **kw)


def main():
    ledger.reset_all()

    # Rule 1: no BYOK, no injected call_fn -> byok_required (no model call happens)
    r = call("0xnokey", model="gpt-oss-120b")           # provider_key=None, call_fn=None
    print(f"[R1] no-BYOK independent_judge -> {r['status']}  (want byok_required)")
    assert r["status"] == "byok_required"

    # Rule 2: unproven attacker, scripted always-miss. IJ_MISS_LIMIT free, then locked.
    atk = "0xattacker"
    for i in range(broker.IJ_MISS_LIMIT):
        r = call(atk, provider_key="byok", call_fn=always_miss)
        assert r["status"] == "not_verified", f"miss {i} -> {r['status']}"
    print(f"[R2] attacker got {broker.IJ_MISS_LIMIT} free misses (streak={ledger.miss_count(atk)})")
    r = call(atk, provider_key="byok", call_fn=always_miss)
    print(f"[R2] next attempt -> {r['status']}  (want verifier_locked)")
    assert r["status"] == "verifier_locked"

    # Fix #3 hardening: a proven wallet's consecutive-miss allowance is REVENUE-SCALED.
    # (a) a tiny-payment attacker stays bounded near the base limit — must pay again to burn more.
    small = "0xproven_small"; ledger.commit(small, broker.IJ_PROVEN_MISS_UNIT_USD)   # ~1 settled burst
    lim = broker._miss_limit(small)
    assert lim == broker.IJ_MISS_LIMIT + 1, f"tiny-payment allowance {lim} (want base+1)"
    for i in range(lim):
        r = call(small, provider_key="byok", call_fn=always_miss, budget_cap=1000.0)
        assert r["status"] == "not_verified", f"small-proven miss {i} -> {r['status']}"
    r = call(small, provider_key="byok", call_fn=always_miss, budget_cap=1000.0)
    print(f"[R2] tiny-payment proven wallet locked after {lim} misses -> {r['status']}  (want verifier_locked)")
    assert r["status"] == "verifier_locked", "tiny-payment attacker must stay bounded"

    # (b) an established paying customer is NOT wrongly locked for legitimate consecutive catches.
    big = "0xproven_big"; ledger.commit(big, broker.IJ_PROVEN_MISS_UNIT_USD * 50)    # heavy payer
    for i in range(broker.IJ_MISS_LIMIT + 20):     # far past the flat limit
        r = call(big, provider_key="byok", call_fn=always_miss, budget_cap=1000.0)
        assert r["status"] == "not_verified", f"heavy payer wrongly blocked at miss {i} -> {r['status']}"
    print(f"[R2] heavy paying customer NOT locked after {broker.IJ_MISS_LIMIT + 20} legit misses OK")

    # Consumable budget: a pass does NOT reset lifetime misses (that's what makes the proven
    # allowance non-ratchetable), but a settled pass raises `spent` -> _miss_limit, so a
    # paying customer keeps headroom above their (cumulative) misses and is never locked.
    def passes(msgs, temperature=0.0):
        return {"text": '{"adequate": true}', "usage": {}, "latency_s": 0.0}
    fresh = "0xfresh"
    ledger.record_miss(fresh); ledger.record_miss(fresh)              # 2 lifetime misses
    r = call(fresh, provider_key="byok", call_fn=passes, budget_cap=1000.0)
    assert r["status"] == "ok", r
    assert ledger.miss_count(fresh) == 2, f"misses must be cumulative (not reset), got {ledger.miss_count(fresh)}"
    assert broker._miss_limit(fresh) > ledger.miss_count(fresh), "a settled pass must keep the budget above current misses"
    print(f"[R2] pass keeps misses cumulative ({ledger.miss_count(fresh)}), budget now {broker._miss_limit(fresh)} (headroom) OK")

    # Anti-ratchet: once a proven wallet burns its allowance it LOCKS, and settling more
    # spend buys only ~1 more free miss per unit (no ceiling reset) — so total free-burn is
    # bounded by revenue. This is the drain Fable caught in the resettable-ceiling version.
    rat = "0xratchet"; ledger.commit(rat, broker.IJ_PROVEN_MISS_UNIT_USD)     # limit = base + 1
    while ledger.miss_count(rat) < broker._miss_limit(rat):
        assert call(rat, provider_key="byok", call_fn=always_miss, budget_cap=1000.0)["status"] == "not_verified"
    assert call(rat, provider_key="byok", call_fn=always_miss, budget_cap=1000.0)["status"] == "verifier_locked"
    locked_at = ledger.miss_count(rat)
    ledger.commit(rat, broker.IJ_PROVEN_MISS_UNIT_USD)                        # one more paid burst-worth
    assert ledger.miss_count(rat) == locked_at, "settled spend must not reset cumulative misses"
    assert broker._miss_limit(rat) - locked_at == 1, "each unit of spend buys exactly 1 more free miss (no reset)"
    print(f"[R2] anti-ratchet: misses never reset; each ${broker.IJ_PROVEN_MISS_UNIT_USD} of spend buys exactly 1 more free miss — free-burn bounded by revenue")

    # C1 regression: a quorum with k<=0 (or k>M) must NEVER pass with zero agreeing judges.
    import burst as burst_mod
    def vote_no(msgs, temperature=0.0):
        return {"text": '{"adequate": false}', "usage": {}, "latency_s": 0.0}
    for bad_k in (-1, 0, 99):
        res = burst_mod.run_burst("q", verifier="independent_quorum", candidate="x",
                                  verify_fns=[(vote_no, "a"), (vote_no, "b")], quorum_k=bad_k)
        assert res.passed is False, f"quorum_k={bad_k} passed with 0 votes!"
        assert 1 <= res.verdict["k"] <= res.verdict["m"], f"k not clamped for {bad_k}"
    # and a judge that ERRORS counts as a NO vote (fail-closed), never a crash
    def vote_boom(msgs, temperature=0.0):
        raise RuntimeError("judge down")
    res = burst_mod.run_burst("q", verifier="independent_quorum", candidate="x",
                              verify_fns=[(vote_boom, "a"), (vote_boom, "b")], quorum_k=1)
    assert res.passed is False and res.verdict["votes_for"] == 0
    print("[C1] quorum k clamped to [1,M]; judge errors fail closed OK")

    print("\nABUSE DEFENSES OK — BYOK-gated, miss-cap revenue-scaled & consumable (no ceiling reset), "
          "tiny-payment attacker bounded, quorum integrity (k>=1, fail-closed judges).")


if __name__ == "__main__":
    main()
