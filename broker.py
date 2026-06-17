"""The orchestration both surfaces share: quote -> authorize -> burst -> settle-IF-verified.

One function so the HTTP endpoint and the MCP tool behave identically. This is the
whole product in ~40 lines: the buyer is charged only when the verifier passes, and
never beyond their per-agent budget cap (the governor that lets builders trust
autonomous spend).
"""
import os

import pricing
import burst as burst_mod
from x402_gate import Facilitator, build_requirements

# Per-payer spend ledger (in-memory; swap for the AgentsPrice margin governor in prod).
_SPENT = {}
# Per-payer count of free-trial bursts run on the HOST provider key (no BYOK).
# In-memory: resets on restart. The trial still requires a valid x402 payment, so
# each free burst proves a funded wallet and (when verified) is paid for — the cap
# just bounds host-token use per wallet before BYOK is required.
_TRIAL = {}
DEFAULT_BUDGET_USD = 1.00  # per-agent cap; mirror AgentsPrice's governor


def trial_used(payer):
    return _TRIAL.get(payer, 0)


def _gate(quote):
    """Pick the payment gate. X402_MODE=live -> real on-chain settlement via the
    SDK (venv-only, lazy-imported); otherwise the stdlib sim. Returns
    (facilitator, requirements, accepts_json)."""
    if os.environ.get("X402_MODE", "sim").lower() == "live":
        import x402_live  # needs the venv (x402 + eth_account + web3)
        pay_to = os.environ.get("X402_PAY_TO")
        if not pay_to:
            raise RuntimeError("X402_MODE=live but X402_PAY_TO (seller wallet) is unset")
        reqs, _ = x402_live.build_requirements_v2(quote["price_usd"], pay_to)
        return x402_live.LiveFacilitator(), reqs, [reqs.model_dump(by_alias=True, exclude_none=True)]
    r = build_requirements(quote)
    return Facilitator(), r, r["accepts"]


def remaining_budget(payer, cap=DEFAULT_BUDGET_USD):
    return max(0.0, cap - _SPENT.get(payer, 0.0))


def _gate_signal(res):
    """Machine-first go/no-go — the point of the product against 'agents going haywire'.
    The verdict gates the agent's NEXT STEP, not just the charge: an agent (or the MCP
    wrapper) reads `action` and HOLDS instead of acting on an unverified answer. We can't
    force a client to obey, but we return an unambiguous verdict it can gate on."""
    v = res.verdict or {}
    conf = v.get("agreement")  # self_consistency exposes a fraction; judge/check -> None
    if res.passed:
        return {"verified": True, "action": "proceed",
                "advice": "Answer passed the verifier — safe to act on.",
                "method": v.get("method"), "confidence": conf}
    return {"verified": False, "action": "hold",
            "advice": ("Answer did NOT pass the verifier — DO NOT act on it. Re-try, "
                       "escalate to a human, or treat the decision as unresolved."),
            "method": v.get("method"), "confidence": conf,
            "reason": v.get("reason") or v.get("votes")}


def serve_burst(request, *, x_payment=None, strategy="best_of_n", n=3,
                verifier="self_consistency", answer_key=None, check=None,
                budget_cap=DEFAULT_BUDGET_USD, facilitator=None, call_fn=None,
                receipt_id="burst", provider_key=None, model=None,
                require_byok=False, trial_cap=0):
    """Returns a result dict. `status` is one of:
       payment_required | budget_exceeded | not_verified(charged:false) | ok(charged:true)."""
    q = pricing.quote(strategy=strategy, n=n, verifier=verifier)
    if facilitator is not None:          # explicit override (tests) -> sim shape
        fac, reqs = facilitator, build_requirements(q)
        accepts = reqs["accepts"]
    else:
        fac, reqs, accepts = _gate(q)    # sim or live, by X402_MODE

    # 1) authorize the payment (do NOT capture yet)
    auth = fac.verify(x_payment, reqs)
    if not auth["valid"]:
        return {"status": "payment_required", "quote": q, "accepts": accepts,
                "reason": auth.get("reason")}
    payer = auth.get("payer", "unknown")

    # 2) BYOK / free-trial gate. With no BYOK key, a wallet may run on the HOST key
    #    for its first `trial_cap` bursts, then must bring its own key. The payment
    #    was already validated above, so trial bursts still prove a funded wallet.
    is_trial = False
    if not provider_key and require_byok:
        used = _TRIAL.get(payer, 0)
        if trial_cap and used < trial_cap:
            is_trial = True   # runs on the host env key; the slot is consumed after the burst
        else:
            return {"status": "byok_required", "payer": payer,
                    "trial_used": used, "trial_cap": trial_cap,
                    "hint": ("free trial used up — send X-Provider-Key with your own Cerebras key"
                             if trial_cap else
                             "send X-Provider-Key with your own Cerebras key")}

    # 3) governor: refuse if this burst would blow the per-agent cap
    if q["price_usd"] > remaining_budget(payer, budget_cap):
        return {"status": "budget_exceeded", "payer": payer,
                "remaining_usd": round(remaining_budget(payer, budget_cap), 6),
                "price_usd": q["price_usd"]}

    # 4) buy more thinking
    res = burst_mod.run_burst(request, strategy=strategy, n=n, verifier=verifier,
                              answer_key=answer_key, check=check,
                              receipt_id=receipt_id, call_fn=call_fn,
                              provider_key=provider_key, model=model)
    if is_trial:                       # consume one free-trial slot per completed host-key burst
        _TRIAL[payer] = _TRIAL.get(payer, 0) + 1

    trial_remaining = max(0, trial_cap - _TRIAL.get(payer, 0)) if trial_cap else 0

    # 5) settle ONLY if the verifier passed — else discard the authorization (no charge)
    if not res.passed:
        return {"status": "not_verified", "charged": False, "price_usd": 0.0,
                "gate": _gate_signal(res),               # action=hold — don't act on this answer
                "verdict": res.verdict, "answer": res.answer, "payer": payer,
                "latency_s": res.latency_s, "cost_basis": res.cost_basis,
                "remaining_budget_usd": round(remaining_budget(payer, budget_cap), 6),
                "budget_cap_usd": budget_cap,
                "trial": is_trial, "trial_remaining": trial_remaining}

    s = fac.settle(x_payment, reqs)
    if s["success"]:
        _SPENT[payer] = _SPENT.get(payer, 0.0) + q["price_usd"]
    return {"status": "ok", "charged": bool(s["success"]), "price_usd": q["price_usd"],
            "gate": _gate_signal(res),                   # action=proceed — verified, safe to act
            "tx": s.get("tx"), "mode": s.get("mode"), "verdict": res.verdict,
            "answer": res.answer, "payer": payer, "latency_s": res.latency_s,
            "cost_basis": res.cost_basis, "receipt_id": res.receipt_id,
            "remaining_budget_usd": round(remaining_budget(payer, budget_cap), 6),
            "budget_cap_usd": budget_cap,
            "trial": is_trial, "trial_remaining": trial_remaining}
