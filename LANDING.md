<!-- Landing-page "claim to fame" copy — DRAFT for review (2026-07-17).
     Product: verified-burst / inference-burst. Every line is checkable against the
     shipped code (cross-vendor judge, _confirm_settlement_onchain, lifetime-cumulative
     miss allowance, fail-closed verify_judge, proof_harness benchmark). No scale/adoption
     implied. Slogan: "Pay for proof, not promises." -->

# Pay for proof, not promises.

**You're billed for an AI answer only when a _different_ model — different lab, different
vendor — independently verifies it. Miss the check, pay nothing. Pass it, and the charge
settles on-chain _before_ the answer is handed over.**

---

## The part that's actually hard

Charging for AI correctness is a wallet-drain waiting to happen: feed the system answers
engineered to fail verification and you burn its judge tokens for free, forever. So the
free-miss allowance is **lifetime-cumulative and consumable** — settling a cheap passing
call can't reset it — which bounds an attacker's free burn to a function of their _own_
settled spend. We found that exact reset-the-ceiling ratchet in our own adversarial audit
and closed it before shipping.

---

## The guarantees, stated plainly

- **Decorrelated verification.** The judge runs on a different model family _and_ a
  different vendor than the generator — the one check an agent can't self-supply from its
  own correlated samples.
- **Settle-then-deliver.** The on-chain payment is independently confirmed to have moved the
  fee to us before the result is released. A lying or buggy facilitator buys nothing.
- **Fail-closed.** A dead or erroring judge withholds the answer and charges nothing — never
  a silent pass dressed up as a verdict.
- **Measured, not asserted.** Verifier false-confirm and false-alarm rates are benchmarked on
  programmatically-generated ground truth — the catch rate can't be cooked.

---

_Small and early by design: this is a correctness-and-safety guarantee you can read the code
for, not a claim about scale._

---

### Notes (not for publication)

- Keep **no percentages** in the hero. The 0% false-confirm / 7.1% false-alarm figures are
  real but benchmark-scoped — put them on a linked "methodology" page with the caveat, not
  the hero. "Measured on programmatic ground truth" earns more trust than a naked stat.
- The honest footer is load-bearing: it converts skepticism into respect instead of
  triggering "who uses this?".
- **PoB variant:** swap the hero to _"You only pay for bugs we can prove"_ and replace
  "settle-then-deliver" with "runnable proof you execute yourself"; the other three
  guarantees carry over unchanged.
