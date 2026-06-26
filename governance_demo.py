"""governance_demo.py — the tiered guard, end to end, with REAL judges (no money).

The guard's `_buy` hook is pointed at the LOCAL broker (real judge models, SIMULATED
settlement) so every verdict below is a live model call — but nothing is charged.

Shows the single spectrum: a decision's stakes pick the tier, and the tier is just
how many independent judges must agree (+ whether a human signs off).
    low stakes      -> auto   : 1 independent judge
    irreversible    -> quorum : k-of-M independent judges must agree
    high value      -> human  : quorum + a person authorizes

Run:  .venv/bin/python governance_demo.py
"""
import os, sys, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "client"))
os.environ.setdefault("LEDGER_DB", os.path.join(tempfile.gettempdir(), "vb_govdemo.db"))
import env; env.load_env()
import broker, ledger
from x402_gate import Facilitator
from verified_burst.guard import verify, verified, Policy

KEY = os.environ["CEREBRAS_API_KEY"]
ledger.commit("0xDemoWallet", 0.01)      # spent>0 = proven (breaker exempt); leaves budget headroom
BYOK_MODEL = "gpt-4o-mini"               # a non-pool generator label -> BOTH families judge (M=2)


class SimFac(Facilitator):
    def __init__(s, p): s.payer = p
    def verify(s, x, r): return {"valid": True, "payer": s.payer}
    def settle(s, x, r): return {"success": True, "tx": "0xSIMULATED", "mode": "sim"}


def local_buy(args):
    """Point the guard at the real broker; judges are real, settlement simulated."""
    return broker.serve_burst(
        args["request"], x_payment="x", facilitator=SimFac("0xDemoWallet"),
        verifier=args.get("verifier", "independent_judge"),
        candidate=args.get("candidate"), quorum_k=args.get("quorum_k"),
        model=args.get("model", BYOK_MODEL), provider_key=None, n=args.get("n", 3))


def rule(t): print("\n" + "═" * 72 + f"\n  {t}\n" + "═" * 72)
def show(label, g):
    print(f"  {label}: tier={g.tier:6} action={g.action:9} "
          f"{'quorum='+g.quorum if g.quorum else ''}")
    if g.reason:
        print(f"          reason: {str(g.reason)[:88]}")


def main():
    print("GOVERNANCE DEMO — one policy, the spectrum single→quorum→human (real judges, no money)")

    rule("AUTO — low-stakes decision, one independent judge")
    g = verify("What is the capital of Australia?", candidate="Canberra",
               value_usd=0, model=BYOK_MODEL, _buy=local_buy)
    show("agent said 'Canberra'", g)

    rule("QUORUM — an IRREVERSIBLE action; k-of-M judges must agree")
    g = verify("Is it safe to run `DROP TABLE users;` on the production database? yes/no",
               candidate="yes, it is safe to run", irreversible=True,
               model=BYOK_MODEL, _buy=local_buy)
    show("agent said 'yes, safe' (WRONG)", g)
    g = verify("Is Canberra the capital of Australia? yes/no", candidate="yes",
               irreversible=True, model=BYOK_MODEL, _buy=local_buy)
    show("agent said 'yes' (right)", g)

    rule("HUMAN — high value; quorum PLUS a person must authorize")
    decision = ("Approve a $500 vendor payment? Invoice = 5 items x $100 = $500, "
                "matches the purchase order.")
    g = verify(decision, candidate="Approved — the $500 total checks out.",
               value_usd=500, model=BYOK_MODEL, _buy=local_buy)
    show("no approver wired", g)
    g = verify(decision, candidate="Approved — the $500 total checks out.",
               value_usd=500, on_escalate=lambda r, gate: False, model=BYOK_MODEL, _buy=local_buy)
    show("human DECLINES", g)
    g = verify(decision, candidate="Approved — the $500 total checks out.",
               value_usd=500, on_escalate=lambda r, gate: True, model=BYOK_MODEL, _buy=local_buy)
    show("human APPROVES", g)

    rule("DECORATOR — bolt it onto a decision function")
    print("  @verified(value_usd=lambda q,a: 0.0)\n  def decide(question): -> agent's answer")
    import verified_burst.guard as G
    G._client.buy = local_buy                 # point the decorator's buy at the broker too

    @verified(value_usd=lambda q, a: 0.0)
    def decide(question):
        return "Canberra"
    g = decide("What is the capital of Australia?")
    show("decide(...) returned a Gate", g)

    print("\n" + "═" * 72)
    print("  One mechanism, parameterized by stakes. Bolt `verify(...)` or `@verified`")
    print("  onto any decision and it routes itself through the right tier.")
    print("═" * 72)


if __name__ == "__main__":
    main()
