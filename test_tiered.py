"""Offline unit tests for tiered.py — mocked judges, NO network, NO spend.
Proves the decision table, fail-closed behavior, cost-saving short-circuit, and the
human rung. Run: .venv/bin/python test_tiered.py  (exit 0 = all pass)."""
import tiered

PASS = '{"adequate": true, "reason": "ok"}'
HOLD = '{"adequate": false, "reason": "wrong"}'


def J(text):
    def f(msgs, temperature=0.0):
        return {"text": text}
    return f


def ERR(msgs, temperature=0.0):
    raise RuntimeError("boom")


def counted(text):
    box = {"n": 0}
    def f(msgs, temperature=0.0):
        box["n"] += 1
        return {"text": text}
    return f, box


fails = []
def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        fails.append(name)


# 1) unanimous PASS -> fast tier, adequate, reasoning NOT called (the cost win)
rfn, box = counted(PASS)
v = tiered.verify("ans", "req", fast_fns=[(J(PASS), "A"), (J(PASS), "B")],
                  reasoning=(rfn, "R"), escalate="nonunanimous_pass")
check("unanimous pass -> tier=fast", v["tier"] == "fast" and v["adequate"] is True)
check("unanimous pass -> reasoning NOT invoked", box["n"] == 0)

# 2) one HOLD -> escalate -> reasoning PASS -> adequate
v = tiered.verify("ans", "req", fast_fns=[(J(PASS), "A"), (J(HOLD), "B")],
                  reasoning=(J(PASS), "R"))
check("pass+hold -> tier=escalated", v["tier"] == "escalated")
check("escalated + reasoning pass -> adequate", v["adequate"] is True)
check("escalation_reason=disagreement", v["escalation_reason"] == "disagreement")

# 3) both HOLD -> escalate (correlated false-alarm guard) -> reasoning HOLD -> not adequate
v = tiered.verify("ans", "req", fast_fns=[(J(HOLD), "A"), (J(HOLD), "B")],
                  reasoning=(J(HOLD), "R"))
check("both hold still escalates", v["tier"] == "escalated")
check("both hold + reasoning hold -> not adequate", v["adequate"] is False)
check("escalation_reason=hold", v["escalation_reason"] == "hold")

# 4) a fast judge ERRORS -> escalate; reasoning PASS -> adequate (error never a silent pass)
v = tiered.verify("ans", "req", fast_fns=[(J(PASS), "A"), (ERR, "B")],
                  reasoning=(J(PASS), "R"))
check("fast error -> escalate", v["tier"] == "escalated")
check("fast error escalation_reason=judge_error", v["escalation_reason"] == "judge_error")
check("fast error + reasoning pass -> adequate", v["adequate"] is True)

# 5) reasoning judge ERRORS -> FAIL-CLOSED (not adequate)
v = tiered.verify("ans", "req", fast_fns=[(J(HOLD), "A"), (J(HOLD), "B")],
                  reasoning=(ERR, "R"))
check("reasoning error -> fail-closed not adequate", v["adequate"] is False)

# 6) NO reasoning judge configured + escalate -> fail-closed hold
v = tiered.verify("ans", "req", fast_fns=[(J(HOLD), "A"), (J(PASS), "B")],
                  reasoning=(None, None))
check("no reasoning + escalate -> not adequate", v["adequate"] is False)
check("no reasoning -> note present", "no reasoning judge configured" in v.get("note", ""))

# 7) escalate='never' -> terminal fast, adequate = consensus, reasoning NEVER called
rfn, box = counted(PASS)
v = tiered.verify("ans", "req", fast_fns=[(J(PASS), "A"), (J(HOLD), "B")],
                  reasoning=(rfn, "R"), escalate="never")
check("never: non-unanimous -> fast tier", v["tier"] == "fast")
check("never: non-unanimous -> not adequate (fail-closed)", v["adequate"] is False)
check("never: reasoning NOT invoked", box["n"] == 0)

# 8) human gate: PASS / PENDING / ERROR  (on an escalated case)
esc = dict(fast_fns=[(J(HOLD), "A"), (J(HOLD), "B")], reasoning=(J(HOLD), "R"))
v = tiered.verify("ans", "req", human_gate=lambda **k: {"decision": "pass", "pending": False}, **esc)
check("human pass -> tier=human adequate", v["tier"] == "human" and v["adequate"] is True)
v = tiered.verify("ans", "req", human_gate=lambda **k: {"pending": True}, **esc)
check("human pending -> withheld", v["tier"] == "human_pending" and v["adequate"] is False)
def hg_err(**k):
    raise RuntimeError("queue down")
v = tiered.verify("ans", "req", human_gate=hg_err, **esc)
check("human gate error -> fail-closed hold", v["adequate"] is False)
v = tiered.verify("ans", "req", human_gate=lambda **k: None, **esc)  # abstain
check("human abstain -> keeps reasoning verdict (escalated)", v["tier"] == "escalated")

# 9) disagree_only mode: agreement terminates fast; disagreement escalates
rfn, box = counted(PASS)
v = tiered.verify("ans", "req", fast_fns=[(J(HOLD), "A"), (J(HOLD), "B")],
                  reasoning=(rfn, "R"), escalate="disagree_only")
check("disagree_only: agreed hold -> fast, reasoning NOT called", v["tier"] == "fast" and box["n"] == 0)
check("disagree_only: agreed hold -> not adequate", v["adequate"] is False)
v = tiered.verify("ans", "req", fast_fns=[(J(PASS), "A"), (J(HOLD), "B")],
                  reasoning=(J(PASS), "R"), escalate="disagree_only")
check("disagree_only: disagreement -> escalate", v["tier"] == "escalated")

print("\n%d checks, %d failed" % (24, len(fails)))
if fails:
    print("FAILURES:", fails)
    raise SystemExit(1)
print("ALL TIERED TESTS PASS")
