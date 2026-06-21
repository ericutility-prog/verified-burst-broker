# Verified Burst — Demo, Logic & Workflow

**What it is:** an agent hits a hard / irreversible / low-confidence decision and buys
*one independently-verified answer* — generated on its own model, **checked by a different
model family**, and **charged only if the check passes**. The one form of "more thinking"
an agent can't manufacture from its own correlated samples: an *independent* opinion.

Run the live demo yourself (real model calls, simulated settlement — free to run):

```bash
.venv/bin/python demo.py
```

Every transcript below is real output from that script. The only stipulated value is
Scenario 2's *generator* answer (it stands in for an agent's own model hallucinating —
the thing the independent judge exists to catch). The judge's verdict is a live call.

---

## The logic — why this is the thing an agent will buy on its own authority

Three properties, each mapping to a clause of "an agent **can** and **will** buy this, on
its **own authority**, to **improve itself**":

| property | why it matters |
|---|---|
| **Independent** — a *different model family* judges the answer | Best-of-N from one model shares that model's blind spots. An independent check is the only "more thinking" the agent **cannot self-supply**. |
| **Pay-only-if-verified** — `downside = 0` | A bounded, zero-downside spend is one an agent's **budget policy can auto-approve** without a human. That's "on its own authority." |
| **Keepable receipt** — every burst returns a durable record | "On decisions like this, my first instinct was flagged." The purchase **compounds into memory** instead of evaporating. |

---

## The workflow

```
[1] Agent hits a hard/irreversible decision → calls buy_verified_burst (MCP one-liner)
[2] 402 challenge returns machine-readable ROI (cost, downside=0, independence)
        → the agent's budget policy auto-approves
[3] Agent signs the x402 payment; generation runs best-of-N on the agent's OWN key (BYOK)
[4] A DIFFERENT model family judges the answer on the broker's key (the independent check)
[5] Settle ONLY if it passes → response carries gate.action (proceed|hold) + a keepable receipt
[6] Agent stores the receipt → it compounds
```

The 402 ROI block an agent reads to decide, on its own, whether to buy:

```json
{
  "unit": "one independently-verified decision",
  "cost_usd": 0.0045,
  "downside_usd": 0.0,
  "pay_only_if_verified": true,
  "independence": { "verifier": "different model family than your generator",
                    "why": "errors decorrelated from your model's blind spots" },
  "auto_approve_hint": "downside is bounded to 0 — safe to buy without human review"
}
```

---

## In the wild — Scenario 1: CONFIRM (the answer is right; agent proceeds and pays)

> Decision: *"What is the capital of Australia?"*
> Generator `gpt-oss-120b` (BYOK) answers; independent `zai-glm-4.7` (ours) checks it.

```
status   : ok   (charged=True, fee=$0.0045)
answer   : 'Canberra'
gate     : { "verified": true, "action": "proceed",
             "method": "independent_judge", "independent": true,
             "verifier_model": "zai-glm-4.7" }
receipt  : { "verified": true, "corrected": false, "independent": true,
             "generator_model": "gpt-oss-120b", "verifier_model": "zai-glm-4.7",
             "answer": "Canberra",
             "verifier_note": "{adequate: true, reason: Canberra is the correct capital
                                of Australia and the answer adheres to the constraint...}",
             "settle_tx": "0x… (on-chain in live mode)" }
```

A different family independently confirmed the answer. The agent gets `action: proceed`,
pays a few tenths of a cent for **verified certainty**, and keeps the receipt.

---

## In the wild — Scenario 2: CATCH (the agent's model is confidently wrong)

> Decision: *"Do US citizens need a visa for a 2-week tourist trip to Japan?"*
> The agent's own model returned (stipulated): **"Yes. US citizens must obtain a tourist
> visa in advance for any visit to Japan."** — confident, and **wrong**.
> A live independent `zai-glm-4.7` call judges it:

```
passed   : False   (charged $0 — a miss is free)
gate     : { "verified": false, "action": "hold",
             "advice": "Answer did NOT pass the verifier — DO NOT act on it...",
             "independent": true, "verifier_model": "zai-glm-4.7" }
receipt  : { "verified": false, "corrected": true, "independent": true,
             "answer": "Yes. US citizens must obtain a tourist visa...",
             "verifier_note": "{adequate: false, reason: US citizens do not need a visa
                                for tourist stays under 90 days in Japan.}",
             "settle_tx": null }
```

The independent judge **disagreed with the agent's model and was right.** `action: hold` —
the agent does **not** act on the wrong answer, pays **nothing**, and keeps a `corrected: true`
receipt it can learn from. This is the value: an independent check catches what the agent's
own model can't see in itself, and it's free when it fires.

> Honest note: a strong 120B model rarely misses *well-known* facts (we checked — it gets
> the classic traps right). Independence earns its keep on **harder / edge / ambiguous**
> calls and on **cheaper buyer models**, which is exactly where an agent is least able to
> trust itself.

---

## Safe to run unattended — the anti-abuse rules

The independent judge is the **only** path that spends *our* tokens (the BYOK verifiers
cost us nothing). Since the judge runs before the pay/no-pay decision, a non-paying wallet
could spam guaranteed-fail bursts to burn it. Two scoped rules close that:

```
[R1] independent_judge with no BYOK key → 'byok_required'
       (we never run the broker-paid judge on the host key; a miss costs us ≤ ~$0.0004)
[R2] an unproven wallet is locked after 3 free misses → 'verifier_locked' (HTTP 429)
       (a wallet that has settled ≥1 payment is exempt; a pass clears the streak)
```

An attacker never settles → never becomes a proven payer → is capped at ~3 × $0.0004 per
wallet, then must fund and rotate wallets (real Sybil cost). `downside = 0` stays true for
honest agents; the abuse tail is bounded and self-limiting.

---

## The economics (per independently-verified burst)

| flow | amount | direction |
|---|---|---|
| Generation tokens | buyer's own cost | buyer → Cerebras (their BYOK key) |
| Judge tokens | **−$0.00039** | broker → Cerebras (our key) |
| Service fee (only on a pass) | **+$0.0045** | buyer → our wallet, over x402 on Base |
| **Net on a PASS** | **≈ +$0.0041** | ~10× margin over judge cost |
| **Net on a MISS** | **≈ −$0.00039** | we ate the judge tokens, charged $0 (the guarantee) |

The judge is **our code wrapping a commodity model API** (a different family on Cerebras),
billed to our key — not a paid third-party "judge vendor." The model is swappable; the
moat is the independence guarantee + pay-only-if-verified settlement + the receipt.

---

## Wire it into an agent (one line)

```json
{ "mcpServers": { "verified-burst": { "command": "verified-burst" } } }
```

```python
buy_verified_burst(
    request="<the decision to resolve>",
    strategy="best_of_n", n=3,
    verifier="independent_judge",        # a different family checks your answer
    answer_key=["regex", "(yes|no)"],     # optional: how to extract the comparable answer
)
```

Install: `pip install verified-burst` — the client signs the x402 payment for you.
