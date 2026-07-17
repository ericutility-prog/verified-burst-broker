# SPEC — Tiered Escalation Verifier v1 (inference-burst)

Status: DRAFT for review (not implemented). Author pass: 2026-07-17.
Sibling of `SPEC-clearance-v1.md`. Target module: `broker.py` (verify path) + `burst.verify_judge`.

## 1. Goal

Get **reasoning-grade verdict quality** at **mostly fast-path cost and latency**. Today you
must choose per verify: a fast Cerebras judge (cheap, sub-second, but over-rejects) OR a
reasoning judge (near-perfect, but ~15× slower and metered). The router removes the choice:
spend the expensive judge **only on the verifies that actually need it**.

## 2. Empirical basis (measured 2026-07-17, 50 programmatic items, live hardened prompt)

| Judge | false-alarm (rejects CORRECT) | false-confirm (passes WRONG) |
|---|---|---|
| GLM-4.7 (fast, Cerebras) | 7.1% | **0.0%** |
| Qwen3-235B *non-thinking* (fast) | 14.3% | **0.0%** |
| Gemma-4-31B (fast) | 47.6% | **0.0%** |
| DeepSeek-V3.2 *non-thinking* (fast) | 92.9% | 12.5% |
| **Qwen3-235B-thinking** (reasoning) | **0.0%** | 0.0% |
| DeepSeek-R1 (reasoning) | 4.8% | 0.0% |

**The load-bearing asymmetry:** every *capable* cheap judge (GLM, Qwen-non-thinking) has
**~0% false-confirm** — it essentially never passes a wrong answer. Its errors are ALL
false-alarms (rejecting correct answers). Therefore:

- A cheap judge that says **PASS** → high-precision signal the answer is correct. Trust it.
- A cheap judge that says **HOLD** → ambiguous: real catch OR false-alarm. This is the only
  place the expensive judge earns its cost.

(DeepSeek-V3.2 is the counter-example — it violated the asymmetry, 12.5% false-confirm — so
it is disqualified as a fast judge. The router's fast tier admits only asymmetry-verified models.)

## 3. Core principles (non-negotiable)

1. **Escalate on independent DISAGREEMENT, not self-confidence.** RLHF confidence is
   miscalibrated (per [[eric-values-honest-pushback]] / escalation-ladder note). The trigger
   is structural (judges disagree / any hold), never "the model said it's sure."
2. **Fail-closed** (per [[customer-safety-paramount]]). Any judge error → escalate, never a
   silent pass. Reasoning-judge error → verdict = inadequate.
3. **Independence is the product.** Every judge in the ladder must differ from the generator
   (different model family), and the reasoning rung is cross-VENDOR (OpenRouter) as well.

## 4. Architecture — a 2(+1)-rung ladder

**Cost gradient (the whole design rests on this ordering — cheapest rung carries the load):**

| Rung | Cost / verify | Latency | Fires on | Sync? |
|---|---|---|---|---|
| 0 · fast pair | ~$0 (Cerebras plan) | sub-second | every verify | sync |
| 1 · reasoning | ~$0.0045 (OpenRouter) | ~10–40 s | any hold / disagreement / error (~22%) | sync |
| 2 · **human** | **dollars + minutes-to-hours** (~1000× rung 1) | **async** | rare: high-stakes / reasoning-uncertain only | **async** |

The reasoning rung is the **workhorse** — it resolves essentially everything at ~$0.0045,
which is exactly what keeps the human rung rare enough to be economic. The human gate must
therefore trigger on a NARROW condition (§4b), never on every rung-1 hold.


```
                 candidate answer + request
                            │
              ┌─────────────▼──────────────┐
   RUNG 0     │  FAST INDEPENDENT PAIR      │   Cerebras, different family than
   (fast)     │  judge_A ∥ judge_B          │   generator, hardened prompt,
              │  (parallel, low budget)     │   JUDGE_MAX_TOKENS≈1024
              └─────────────┬──────────────┘
                            │
             ┌──────────────┴───────────────┐
             │ ALL judges cleanly PASS?      │
             └──────┬────────────────┬───────┘
                   YES              NO  (any HOLD, any disagreement, any error)
                    │                │
              ┌─────▼─────┐   ┌──────▼───────────────────────┐
   TERMINATE  │ adequate  │   │ RUNG 1 — REASONING JUDGE     │  OpenRouter, cross-vendor,
   fast path  │ tier=fast │   │ qwen3-235b-thinking @ 4000   │  raised budget
              └───────────┘   │ (authoritative verdict)      │
                              └──────┬───────────────────────┘
                                     │
                            adequate = reasoning verdict
                            tier = escalated
```

**Why even a UNANIMOUS cheap HOLD must escalate:** the cheap judges share a blind spot
(they can't reliably recompute) → their false-alarms are *correlated*, so "both hold" does
NOT reliably mean "really wrong." Only a **unanimous clean PASS** terminates on the fast
path; everything else escalates. This is what converts the cheap tier's high false-alarm
rate into ~zero (the reasoning rung rescues every false-alarm).

## 4b. Rung 2 — the human gate (the costliest rung; default OFF)

The top rung is a HUMAN — the Concierge / verified-judgment product surface, and the
EU AI Act Art-14 human-oversight hook. Because it is ~1000× the cost of the reasoning rung
and resolves ASYNChronously, it is reserved for the irreducible minority. The **policy of
WHEN to pull in a human lives inside the gate, not the ladder** — the ladder always OFFERS
the gate the fully-formed rung-1 verdict; the gate decides whether to intervene.

**Contract:** `human_gate(answer, request, verdict) ->`
- `None` → abstain (keep the reasoning verdict). This is the default for routine escalations.
- `{"decision": "pass"|"hold", "pending": False}` → a human resolved it now.
- `{"pending": True, ...}` → queued for async review; the answer is **WITHHELD** (`adequate=False`,
  `tier="human_pending"`) until a human resolves it out of band.
- Any exception → **fail-closed hold**. The human rung never auto-passes.

**Recommended trigger (inside the gate), narrow by design:** high-stakes/regulated task types
(`FORCE_ESCALATE`), OR the reasoning judge itself signalled low-confidence / errored, OR a
configured allow-list. Never "every rung-1 hold" — that would make the costliest rung the
common case and destroy the economics.

**Async billing implication:** a `human_pending` verify cannot settle synchronously in the
pay-only-if-verified flow — it becomes queue → human resolves → THEN settle-or-discard. This
is the "human rung as a product (queue + resume-to-source)" net-new item; the ladder is built
async-ready (it emits `human_pending`) but the queue/resume backend is a separate build.

## 5. Decision table

| judge_A | judge_B | action | final verdict | tier |
|---|---|---|---|---|
| PASS | PASS | terminate | adequate | fast |
| PASS | HOLD | escalate | = reasoning | escalated |
| HOLD | HOLD | escalate | = reasoning | escalated |
| any | ERROR | escalate | = reasoning (or inadequate if it errors) | escalated |
| — | — (reasoning errors) | — | **inadequate** (fail-closed) | escalated |

## 6. Pseudocode (maps onto existing broker.py primitives)

```python
def tiered_verify(candidate, request, generator_model, *, meta=None):
    fast = _fast_judges(generator_model)      # [(provider, model), ...] different-family, != gen
    r_provider, r_model, r_max = _reasoning_judge()

    # RUNG 0 — run the fast pair in parallel (ThreadPool)
    fv = [ _judge(candidate, request, _bind_judge(p, m)) for (p, m) in fast ]  # parallel
    #   _judge -> {"adequate": bool|None, "model": m, "raw": ...}; None == ERROR (tri-state)

    unanimous_clean_pass = fast and all(v["adequate"] is True for v in fv)
    if unanimous_clean_pass:
        return _verdict(True, tier="fast", judges=[v["model"] for v in fv], meta=meta)

    # RUNG 1 — escalate (any hold / disagreement / error)
    rv = _judge(candidate, request, _bind_judge(r_provider, r_model, max_tokens=r_max))
    adequate = rv["adequate"] is True            # None (error) -> False, fail-closed
    return _verdict(adequate, tier="escalated",
                    escalation_reason=_why(fv),
                    judges=[v["model"] for v in fv] + [r_model],
                    reasoning_verdict=rv, meta=meta)
```

**Code deltas required:**
- `verify_judge` (or a thin `_judge` wrapper) must return a **tri-state** (pass / hold /
  error) — reuse the exact pattern PoB already ships (`_verify_one` fail-loud tri-state).
  Today `burst.verify_judge` doesn't catch; the wrapper adds try/except → `adequate=None`.
- `_bind_judge(pname, model, max_tokens=None)` — add optional per-call `max_tokens` (today
  it hard-codes `JUDGE_MAX_TOKENS`) so the reasoning rung can request 4000.
- New `verifier="tiered"` method selectable in `run_burst`, alongside existing
  `self_consistency` / `independent_judge` / `independent_quorum`.

## 7. Verdict / receipt schema additions (feeds attestation + audit-trail item)

```
verdict += {
  "tier": "fast" | "escalated",
  "judges": ["<fast_model_1>", "<fast_model_2>", ("<reasoning_model>")],
  "fast_verdicts": [{"model":..., "adequate":..., "raw":...}, ...],
  "escalation_reason": "unanimous_pass" | "hold" | "disagreement" | "judge_error",
  "reasoning_verdict": {...}   # present only when escalated
}
```
This makes the independence + escalation path part of the signed per-verify record →
directly closes the "signed, hash-chained audit trail" open item.

## 8. Config (env, all with safe defaults)

| var | default | meaning |
|---|---|---|
| `VERIFIER_FAST_JUDGES` | `cerebras:gemma-4-31b,cerebras:zai-glm-4.7`¹ | fast rung, comma list `provider:model`, must differ from generator |
| `VERIFIER_REASONING_JUDGE` | `openrouter:qwen/qwen3-235b-a22b-thinking-2507` | rung-1 model |
| `VERIFIER_REASONING_MAX_TOKENS` | `4000` | rung-1 token budget (reasoning needs room) |
| `VERIFIER_ESCALATE` | `nonunanimous_pass` | `nonunanimous_pass` \| `disagree_only` \| `always` \| `never` |
| `VERIFIER_FORCE_ESCALATE` | `0` | `1` = skip fast path (high-stakes / until asymmetry validated for a task type) |
| `VERIFIER_SHADOW_AUDIT_RATE` | `0.05` | fraction of fast-path PASSes ALSO sent to reasoning judge off-path to measure real false-confirm |

¹ Post-Aug-17 (GLM-4.7 gone): fast judges = `gemma-4-31b` alone unless a GLM successor lands
on the Cerebras endpoint. With a single fast judge, "unanimous pass" = that one judge's pass
(weaker); prefer 2. Re-check `/v1/models` before Aug 17. See TODO GLM-deprecation item.

## 9. Safety analysis

**Only fast-path acceptance = unanimous clean PASS of ≥2 independent different-family judges.**
Residual risk = ALL fast judges false-CONFIRM the *same* wrong answer simultaneously.
- On the tested regime each capable fast judge is ~0% false-confirm; two independent
  different-family judges → joint false-confirm ≈ product of two small numbers ≈ negligible.
- **Unmeasured on other task types.** Mitigations:
  1. `VERIFIER_FORCE_ESCALATE=1` for configured high-stakes task types until per-type
     false-confirm is validated (never assume the arithmetic-regime asymmetry generalizes).
  2. **Shadow audit** (`VERIFIER_SHADOW_AUDIT_RATE`): sample fast-path PASSes through the
     reasoning judge asynchronously — off the buyer's latency path, NOT charged. Build a
     running empirical false-confirm estimate; alert on drift. This is the "no silent caps"
     discipline — the fast path is continuously proven, not assumed.
- Fail-closed preserved end-to-end (§3.2). Escalation trigger is structural, not confidence (§3.1).

## 10. Cost / latency model (today's numbers)

- **Fast path:** 2 Cerebras calls, parallel, sub-second, ≈ $0 (plan tokens).
- **Escalated:** + 1 reasoning call, ~10–40 s, ≈ **$0.0045** (metered OpenRouter).
- **Escalation rate** ≈ base_error_rate + fast_false_alarm_rate. With hardened GLM (7.1% FA)
  and ~16% base error ⇒ ~22% escalate, ~78% fast.
- **Effective:** ≈ 0.22 × $0.0045 ≈ **$0.001/verify avg**, latency sub-second for ~78% of
  verifies — vs all-reasoning $0.0045 + ~15 s **every** time. **~4.5× cheaper & faster, same quality.**
- Ties back to the Cerebras thesis: fast inference carries the common case; the reasoning
  model is spent only where quality demands it.

## 11. Rollout (shadow-first, never a blind flip on the money path)

1. Implement behind `verifier="tiered"`, default OFF. Unit-test the decision table + tri-state
   + fail-closed with mocked judges (offline, no spend) — mirror `test_clearance.py` style.
2. **Shadow mode:** run tiered ALONGSIDE the current verifier on live traffic; log verdict,
   tier, escalation_reason, and shadow-audit disagreements. Change NOTHING about billing yet.
3. Read the shadow data: real escalation rate, real fast-path false-confirm (from shadow audit).
4. Flip the auto tier → tiered only when shadow looks clean. Keep the env kill-switch
   (`VERIFIER_ESCALATE=always` = degrade to pure reasoning; `never` = pure fast).

## 12. Open decisions (Eric)

- **Reasoning rung:** Qwen3-thinking (0% FA) vs DeepSeek-R1 (4.8%) vs a 2-judge quorum of
  both on escalated cases (max independence, 2× escalated cost). Recommend Qwen3-thinking solo
  to start; quorum is a config change later.
- **Fast rung after Aug-17:** live with `gemma-4-31b` solo, or hold GLM-4.7 as long as it
  lasts + add a GLM successor when it appears?
- **High-stakes force-escalate list** — which task types skip the fast path entirely?
- **Pricing:** flat (absorb escalation cost at low rate) vs tier-aware line on the receipt.

## 13. What this does NOT do (honesty scope)

- It does not make **non-verifiable / open-ended** tasks provable — for those the reasoning
  rung is still a *judgment*, not proof. The **Tier-2 tool/execution verifier** (deterministic
  check for computationally-checkable claims, using PoB's `sandbox.py`) remains the separate,
  stronger answer for that slice, and would slot in as a Rung-0.5 (deterministic, ~free, faster
  than any LLM) ahead of the reasoning rung for checkable claims.
- The cost/escalation numbers are regime-dependent (arithmetic benchmark). Real traffic will
  differ; §11 shadow mode measures the truth before any billing depends on it.
