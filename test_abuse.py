"""Prove the independent_judge abuse defenses (Rules 1 & 2) without spending tokens.

Rule 1: independent_judge with no BYOK key -> byok_required (we never run the
        broker-paid judge on the host key, so a miss costs us at most the judge call).
Rule 2: an UNPROVEN wallet gets IJ_MISS_LIMIT free misses, then verifier_locked;
        a PROVEN payer (settled >=1) is never locked; a pass clears the streak.
"""
import env; env.load_env()
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
    broker._IJ_MISSES.clear(); broker._SPENT.clear()

    # Rule 1: no BYOK, no injected call_fn -> byok_required (no model call happens)
    r = call("0xnokey", model="gpt-oss-120b")           # provider_key=None, call_fn=None
    print(f"[R1] no-BYOK independent_judge -> {r['status']}  (want byok_required)")
    assert r["status"] == "byok_required"

    # Rule 2: unproven attacker, scripted always-miss. IJ_MISS_LIMIT free, then locked.
    atk = "0xattacker"
    for i in range(broker.IJ_MISS_LIMIT):
        r = call(atk, provider_key="byok", call_fn=always_miss)
        assert r["status"] == "not_verified", f"miss {i} -> {r['status']}"
    print(f"[R2] attacker got {broker.IJ_MISS_LIMIT} free misses (streak={broker._IJ_MISSES[atk]})")
    r = call(atk, provider_key="byok", call_fn=always_miss)
    print(f"[R2] next attempt -> {r['status']}  (want verifier_locked)")
    assert r["status"] == "verifier_locked"

    # Proven payer is exempt even with a long miss streak.
    good = "0xgood"; broker._SPENT[good] = 0.01; broker._IJ_MISSES[good] = 99
    r = call(good, provider_key="byok", call_fn=always_miss)
    print(f"[R2] proven payer w/ 99 misses -> {r['status']}  (want not_verified, NOT locked)")
    assert r["status"] == "not_verified"

    # A pass clears the streak: scripted judge that approves.
    def passes(msgs, temperature=0.0):
        return {"text": '{"adequate": true}', "usage": {}, "latency_s": 0.0}
    fresh = "0xfresh"
    broker._IJ_MISSES[fresh] = 2
    r = call(fresh, provider_key="byok", call_fn=passes)
    print(f"[R2] pass after 2 misses -> status={r['status']} streak_now={broker._IJ_MISSES[fresh]} (want 0)")
    assert r["status"] == "ok" and broker._IJ_MISSES[fresh] == 0

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

    print("\nABUSE DEFENSES OK — BYOK-gated, unproven wallets capped, payers exempt, pass resets, "
          "quorum integrity (k>=1, fail-closed judges).")


if __name__ == "__main__":
    main()
