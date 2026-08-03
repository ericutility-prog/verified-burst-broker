"""Offline unit tests for the thin-fast-rung guard (finding #2, 2026-07-28).

Before the fix: a fast rung of ONE judge made `unanimous_pass` true on that single vote,
so ESCALATE=nonunanimous_pass terminated on the fast tier — a payable PASS decided 1-of-1,
with `independent: True` on the receipt (which flows into clearance's signed cert, where
cert["cleared"] = verified and independent). This is exactly the Aug-17 GLM-deprecation
shape: the pool shrinks and scrutiny silently drops.

NO network, NO spend. Run: .venv/bin/python test_tiered_thinrung.py
"""
import sys
import tiered

PASS = '{"adequate": true, "reason": "ok"}'
HOLD = '{"adequate": false, "reason": "wrong"}'


def J(text):
    def f(msgs, temperature=0.0):
        return {"text": text}
    return f


def counted(text):
    box = {"n": 0}
    def f(msgs, temperature=0.0):
        box["n"] += 1
        return {"text": text}
    return f, box


fails = []
def check(name, cond, detail=""):
    print(("  ok  " if cond else " FAIL ") + name + ("" if cond else "  -> " + detail))
    if not cond:
        fails.append(name)


print("\ntiered: thin fast rung must escalate, not terminate (finding #2)")

# --- THE BUG: one fast judge saying PASS must NOT be a terminal fast-tier pass -------
rfn, box = counted(HOLD)
v = tiered.verify("ans", "req", fast_fns=[(J(PASS), "A")], reasoning=(rfn, "R"),
                  escalate="nonunanimous_pass", generator_model="G")
check("1 fast judge + PASS -> escalates (not terminal fast)", v["tier"] == "escalated", v["tier"])
check("1 fast judge -> reasoning rung IS consulted", box["n"] == 1, str(box["n"]))
check("1 fast judge -> reasoning verdict decides (HOLD -> not adequate)",
      v["adequate"] is False, repr(v["adequate"]))
check("thin_fast_rung flag exposed", v.get("thin_fast_rung") is True, repr(v.get("thin_fast_rung")))
check("fast_judge_count exposed", v.get("fast_judge_count") == 1, repr(v.get("fast_judge_count")))
check("escalation_reason names the thin rung", "need 2" in (v.get("escalation_reason") or ""),
      v.get("escalation_reason", ""))

# reasoning judge AGREES -> pass, but via the reasoning rung, not a 1-of-1 fast call
v = tiered.verify("ans", "req", fast_fns=[(J(PASS), "A")], reasoning=(J(PASS), "R"),
                  escalate="nonunanimous_pass", generator_model="G")
check("1 fast judge + reasoning PASS -> adequate via escalated tier",
      v["tier"] == "escalated" and v["adequate"] is True, f"{v['tier']}/{v['adequate']}")

# --- ZERO judges: already fail-closed, must stay so ---------------------------------
v = tiered.verify("ans", "req", fast_fns=[], reasoning=(J(PASS), "R"),
                  escalate="nonunanimous_pass", generator_model="G")
check("0 fast judges -> escalated", v["tier"] == "escalated", v["tier"])
v = tiered.verify("ans", "req", fast_fns=[], reasoning=(None, None),
                  escalate="nonunanimous_pass", generator_model="G")
check("0 judges + no reasoning judge -> fail-closed hold", v["adequate"] is False, repr(v["adequate"]))

# thin rung with NO reasoning judge configured -> fail-closed, never a silent pass
v = tiered.verify("ans", "req", fast_fns=[(J(PASS), "A")], reasoning=(None, None),
                  escalate="nonunanimous_pass", generator_model="G")
check("1 fast judge + no reasoning judge -> fail-closed hold (never a 1-of-1 pass)",
      v["adequate"] is False and v["tier"] == "escalated", f"{v['tier']}/{v['adequate']}")

# --- EXISTING BEHAVIOUR UNCHANGED ---------------------------------------------------
rfn, box = counted(HOLD)
v = tiered.verify("ans", "req", fast_fns=[(J(PASS), "A"), (J(PASS), "B")], reasoning=(rfn, "R"),
                  escalate="nonunanimous_pass", generator_model="G")
check("2 judges unanimous pass -> STILL terminal fast (cost win preserved)",
      v["tier"] == "fast" and v["adequate"] is True, f"{v['tier']}/{v['adequate']}")
check("2 judges unanimous pass -> reasoning still NOT invoked", box["n"] == 0, str(box["n"]))
check("2 judges -> thin_fast_rung False", v.get("thin_fast_rung") is False,
      repr(v.get("thin_fast_rung")))

v = tiered.verify("ans", "req", fast_fns=[(J(PASS), "A"), (J(HOLD), "B")], reasoning=(J(PASS), "R"),
                  escalate="nonunanimous_pass", generator_model="G")
check("2 judges disagreeing -> escalates (unchanged)", v["tier"] == "escalated", v["tier"])

# explicit operator override must still win
v = tiered.verify("ans", "req", fast_fns=[(J(PASS), "A")], reasoning=(J(HOLD), "R"),
                  escalate="never", generator_model="G")
check("escalate=never respected even with a thin rung", v["tier"] == "fast", v["tier"])

# --- `independent` is now DECORRELATION, not "a vote happened" -----------------------
v = tiered.verify("ans", "req", fast_fns=[(J(PASS), "G")], reasoning=(None, None),
                  escalate="never", generator_model="G")
check("judge model == generator -> independent False", v["independent"] is False,
      repr(v["independent"]))
v = tiered.verify("ans", "req", fast_fns=[(J(PASS), "A"), (J(PASS), "B")], reasoning=(None, None),
                  escalate="never", generator_model="G")
check("judges differ from generator -> independent True", v["independent"] is True,
      repr(v["independent"]))
v = tiered.verify("ans", "req", fast_fns=[], reasoning=(J(PASS), "R"),
                  escalate="nonunanimous_pass", generator_model="G")
check("no fast judges but reasoning judge differs -> independent True (via reasoning rung)",
      v["independent"] is True, repr(v["independent"]))

print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}\n")
sys.exit(1 if fails else 0)
