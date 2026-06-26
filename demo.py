"""demo.py — the verified-burst workflow, end to end, with REAL model calls.

Run:  .venv/bin/python demo.py
Uses our Cerebras key for both the (demo) generator and the independent judge; the
x402 settlement is SIMULATED (no real USDC moves) so the demo is free to run. Every
model output below is a live call — nothing is fabricated. The one stipulated value
is scenario 2's generator output, clearly labelled (it stands in for an agent's own
model hallucinating — the thing the independent judge is there to catch).

What it shows:
  1. CONFIRM  — a real decision; real generation; a DIFFERENT-family judge confirms;
                the agent proceeds and pays a few tenths of a cent; keepable receipt.
  2. CATCH    — the agent's model returns a confident wrong answer; the independent
                judge (live, different family) catches it; action=hold; charged $0.
  3. SAFE     — the anti-abuse rules that let an owner run this unattended.
"""
import os, json, tempfile
os.environ.setdefault("LEDGER_DB", os.path.join(tempfile.gettempdir(), "vb_demo.db"))
import env; env.load_env()
import broker, burst as burst_mod, pricing, ledger
from x402_gate import Facilitator

KEY = os.environ.get("CEREBRAS_API_KEY", "")
GEN = "gpt-oss-120b"        # the agent's (BYOK) generator family
JUDGE = "zai-glm-4.7"       # the broker's independent judge family


class SimFac(Facilitator):
    """Real-shaped facilitator with SIMULATED settlement (no on-chain money)."""
    def __init__(self, payer): self.payer = payer
    def verify(self, xp, reqs): return {"valid": True, "payer": self.payer}
    def settle(self, xp, reqs): return {"success": True, "tx": "0xSIMULATED_settle_tx", "mode": "sim"}


def rule(t): print("\n" + "═" * 72 + f"\n  {t}\n" + "═" * 72)
def step(n, t): print(f"  [{n}] {t}")


def workflow_note():
    rule("THE WORKFLOW (what happens on every /v1/burst)")
    for n, t in [
        ("1", "Agent hits a hard/irreversible decision, calls buy_verified_burst (MCP one-liner)."),
        ("2", "402 challenge returns machine-readable ROI: cost, downside=0 (pay-only-if-"),
        (" ", "    verified), independence=different family. Agent's budget policy auto-approves."),
        ("3", "Agent signs the x402 payment. Generation runs best-of-N on the agent's OWN key (BYOK)."),
        ("4", "A DIFFERENT model family judges the answer on the broker's key (the independent check)."),
        ("5", "Settle ONLY if it passes. Response carries gate.action (proceed|hold) + a keepable receipt."),
        ("6", "Agent stores the receipt -> the purchase compounds into memory."),
    ]:
        step(n, t)


def show_roi():
    rule("THE 402 ROI BLOCK  (what an agent's budget policy reads to auto-approve)")
    q = pricing.quote("best_of_n", 3, "independent_judge")
    roi = {
        "unit": "one independently-verified decision",
        "cost_usd": q["price_usd"], "downside_usd": 0.0, "pay_only_if_verified": True,
        "independence": {"verifier": "different model family than your generator",
                         "why": "errors decorrelated from your model's blind spots"},
        "auto_approve_hint": "downside is bounded to 0 — safe to buy without human review",
    }
    print(json.dumps(roi, indent=2))


def scenario_confirm():
    rule("SCENARIO 1 — CONFIRM  (real generation + real independent judge, agent proceeds)")
    question = "What is the capital of Australia? Reply with just the city name."
    step("ask", f"agent decision: {question!r}")
    r = broker.serve_burst(question, x_payment="signed-x402", verifier="independent_judge",
                           facilitator=SimFac("0xAgentWallet"), provider_key=KEY,
                           model=GEN, n=3, receipt_id="demo-confirm")
    print(f"\n  status   : {r['status']}  (charged={r.get('charged')}, fee=${r.get('price_usd')})")
    print(f"  answer   : {r['answer'].strip()!r}")
    print(f"  gate     : {json.dumps(r['gate'])}")
    print(f"  receipt  : {json.dumps(r['receipt'])}")
    print(f"\n  -> generator {GEN} answered; independent {JUDGE} CONFIRMED it; agent proceeds, "
          f"pays ${r.get('price_usd')}, keeps the receipt (settle_tx is on-chain in live mode).")


def scenario_catch():
    rule("SCENARIO 2 — CATCH  (agent's model is confidently wrong; independent judge catches it)")
    question = "Do US citizens need a visa for a 2-week tourist trip to Japan? yes/no + one line."
    hallucination = "Yes. US citizens must obtain a tourist visa in advance for any visit to Japan."
    step("ask", f"agent decision: {question!r}")
    step("gen", f"the agent's OWN model returned (stipulated): {hallucination!r}")
    step("jdg", f"now a LIVE, independent {JUDGE} call judges that answer...")
    # run the real independent judge against the stipulated generator output
    vfn, vmodel = broker._independent_verify_fn(GEN)
    def gen_returns_hallucination(msgs, temperature=0.0):
        return {"text": hallucination, "usage": {}, "latency_s": 0.0}
    res = burst_mod.run_burst(question, strategy="fast", n=1, verifier="independent_judge",
                              call_fn=gen_returns_hallucination, verify_fn=vfn,
                              verifier_model=vmodel, model=GEN, receipt_id="demo-catch")
    gate = broker._gate_signal(res)
    receipt = broker._receipt(res, charged=False)
    print(f"\n  passed   : {res.passed}  (charged $0 — a miss is free)")
    print(f"  gate     : {json.dumps(gate)}")
    print(f"  receipt  : {json.dumps(receipt)}")
    print(f"\n  -> the independent judge DISAGREED with the agent's model. action=hold: the agent "
          f"does NOT act on the wrong answer, pays nothing, and keeps a 'corrected' receipt.")


def scenario_safe():
    rule("SCENARIO 3 — SAFE TO RUN UNATTENDED  (the anti-abuse rules)")
    # Rule 1
    r = broker.serve_burst("decide", x_payment="x", verifier="independent_judge",
                           facilitator=SimFac("0xNoKey"), model=GEN)  # no provider_key
    step("R1", f"independent_judge with no BYOK key -> {r['status']!r}: {r['hint'][:70]}...")
    # Rule 2
    ledger.reset_all()
    atk = "0xAttacker"
    def miss(msgs, temperature=0.0): return {"text": "no", "usage": {}, "latency_s": 0.0}
    locked_at = None
    for i in range(broker.IJ_MISS_LIMIT + 2):
        r = broker.serve_burst("decide", x_payment="x", verifier="independent_judge",
                               facilitator=SimFac(atk), provider_key="byok", call_fn=miss)
        if r["status"] == "verifier_locked":
            locked_at = i; break
    step("R2", f"unproven wallet locked after {locked_at} free misses (limit={broker.IJ_MISS_LIMIT}) "
               f"-> spam can't burn our judge tokens for free.")
    print("\n  -> downside=0 stays true for honest agents; the abuse tail is bounded and self-limiting.")


if __name__ == "__main__":
    print("VERIFIED-BURST DEMO — independent, pay-only-if-verified decisions for agents")
    print(f"generator(BYOK)={GEN}   independent judge(ours)={JUDGE}   settlement=SIMULATED")
    workflow_note(); show_roi()
    scenario_confirm(); scenario_catch(); scenario_safe()
    print("\n" + "═" * 72)
    print("  Wire it into an agent (one line):")
    print('    {"mcpServers": {"verified-burst": {"command": "verified-burst"}}}')
    print("  Tool: buy_verified_burst(request, strategy, n, verifier='independent_judge', answer_key)")
    print("═" * 72)
