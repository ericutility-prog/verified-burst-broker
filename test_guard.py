"""Offline tests for the governance guard — deterministic, no network, no money.

Injects a fake `_buy` so we test the POLICY + GATE + HUMAN logic in isolation
(the real judges are exercised live in governance_demo.py / test_independence.py).
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "client"))

import verified_burst.guard as G
from verified_burst.guard import verify, verified, DEFAULT_POLICY

PROCEED = {"status": "ok", "answer": "Canberra",
           "gate": {"action": "proceed", "verified": True, "quorum": "2/2 agreed (needed 2)"},
           "receipt": {"verified": True, "corrected": False}}
HOLD = {"status": "not_verified", "answer": "wrong",
        "gate": {"action": "hold", "verified": False, "advice": "did not pass"},
        "receipt": {"verified": False, "corrected": True}}


def fixed(resp):
    return lambda args: resp


def main():
    P = DEFAULT_POLICY
    # 1) policy routes stakes -> tier
    assert P.tier_for(value_usd=0).name == "auto"
    assert P.tier_for(irreversible=True).name == "quorum"
    assert P.tier_for(value_usd=5).name == "quorum"
    assert P.tier_for(value_usd=500).name == "human"
    print("[policy] auto / quorum / human routing OK")

    # 2) proceed -> truthy gate; hold -> falsy gate
    g = verify("q", candidate="Canberra", _buy=fixed(PROCEED))
    assert bool(g) and g.action == "proceed" and g.tier == "auto"
    g = verify("q", candidate="wrong", _buy=fixed(HOLD))
    assert not g and g.action == "hold"
    print("[gate] proceed=truthy, hold=falsy OK")

    # 3) human tier: judges pass but NO approver -> escalate (not auto-proceed)
    g = verify("q", candidate="Canberra", value_usd=500, _buy=fixed(PROCEED))
    assert g.tier == "human" and g.action == "escalate" and not g
    # approver declines -> hold; approves -> proceed
    g = verify("q", candidate="Canberra", value_usd=500,
               on_escalate=lambda r, gate: False, _buy=fixed(PROCEED))
    assert g.action == "hold"
    g = verify("q", candidate="Canberra", value_usd=500,
               on_escalate=lambda r, gate: True, _buy=fixed(PROCEED))
    assert bool(g) and g.action == "proceed"
    print("[human] no-approver=escalate, decline=hold, approve=proceed OK")

    # 4) human tier never proceeds if the judges themselves FAILED
    g = verify("q", candidate="wrong", value_usd=500,
               on_escalate=lambda r, gate: True, _buy=fixed(HOLD))
    assert g.action == "hold"
    print("[human] failed judges can't be human-approved into proceed OK")

    # 5) fail-safe: a buy that raises -> HOLD, never a silent proceed
    def boom(args): raise RuntimeError("network down")
    g = verify("q", candidate="x", _buy=boom)
    assert not g and g.action == "hold"
    print("[failsafe] buy error -> hold OK")

    # 6) decorator: first arg is the question, return is the answer, result is a Gate
    G._client.buy = fixed(PROCEED)            # monkeypatch the real buy for the deco path
    @verified(value_usd=lambda q, a: 0.0)
    def decide(question):
        return "Canberra"
    g = decide("What is the capital of Australia?")
    assert bool(g) and g.answer == "Canberra"
    print("[decorator] @verified wraps + gates OK")

    print("\nGUARD LOGIC OK — policy routes, gate gates, human tier enforced, fails safe.")


if __name__ == "__main__":
    main()
