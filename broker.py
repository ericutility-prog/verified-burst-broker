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
DEFAULT_BUDGET_USD = 1.00  # per-agent cap; mirror AgentsPrice's governor


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


def serve_burst(request, *, x_payment=None, strategy="best_of_n", n=3,
                verifier="self_consistency", answer_key=None, check=None,
                budget_cap=DEFAULT_BUDGET_USD, facilitator=None, call_fn=None,
                receipt_id="burst", provider_key=None, model=None):
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

    # 2) governor: refuse if this burst would blow the per-agent cap
    if q["price_usd"] > remaining_budget(payer, budget_cap):
        return {"status": "budget_exceeded", "payer": payer,
                "remaining_usd": round(remaining_budget(payer, budget_cap), 6),
                "price_usd": q["price_usd"]}

    # 3) buy more thinking
    res = burst_mod.run_burst(request, strategy=strategy, n=n, verifier=verifier,
                              answer_key=answer_key, check=check,
                              receipt_id=receipt_id, call_fn=call_fn,
                              provider_key=provider_key, model=model)

    # 4) settle ONLY if the verifier passed — else discard the authorization (no charge)
    if not res.passed:
        return {"status": "not_verified", "charged": False, "price_usd": 0.0,
                "verdict": res.verdict, "answer": res.answer, "payer": payer,
                "latency_s": res.latency_s, "cost_basis": res.cost_basis}

    s = fac.settle(x_payment, reqs)
    if s["success"]:
        _SPENT[payer] = _SPENT.get(payer, 0.0) + q["price_usd"]
    return {"status": "ok", "charged": bool(s["success"]), "price_usd": q["price_usd"],
            "tx": s.get("tx"), "mode": s.get("mode"), "verdict": res.verdict,
            "answer": res.answer, "payer": payer, "latency_s": res.latency_s,
            "cost_basis": res.cost_basis, "receipt_id": res.receipt_id,
            "remaining_budget_usd": round(remaining_budget(payer, budget_cap), 6)}
