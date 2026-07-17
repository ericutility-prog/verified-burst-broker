"""The orchestration both surfaces share: quote -> authorize -> burst -> settle-IF-verified.

One function so the HTTP endpoint and the MCP tool behave identically. This is the
whole product in ~40 lines: the buyer is charged only when the verifier passes, and
never beyond their per-agent budget cap (the governor that lets builders trust
autonomous spend).
"""
import base64
import hashlib
import json
import os

import pricing
import provider
import burst as burst_mod
from x402_gate import Facilitator, build_requirements


def _payment_key(x_payment):
    """A stable, unforgeable dedup key for a signed x402 authorization — its nonce +
    signature. Used to make a payment single-use (no replay, no concurrent fan-out).
    Returns None when the payment can't be decoded (the SIM/test facilitator shapes),
    which is safe: sim is loopback-only by the boot guard, so there's nothing to dedup."""
    if not x_payment or not isinstance(x_payment, str):
        return None
    try:
        obj = json.loads(base64.b64decode(x_payment, validate=True))
        payload = obj.get("payload") or {}
        auth = payload.get("authorization") or {}
        sig = payload.get("signature") or ""
        nonce = auth.get("nonce") or ""
        if not (sig or nonce):
            return None
        return hashlib.sha256(f"{nonce}|{sig}".encode()).hexdigest()
    except Exception:
        return None


def _settle_failed(payer, budget_cap):
    """Verifier passed but the payment did not capture on-chain. The result is withheld
    and nothing is charged; the buyer signs a fresh payment to retry."""
    return {"status": "settle_failed", "charged": False, "price_usd": 0.0, "payer": payer,
            "hint": ("payment capture did not confirm on-chain — the result is withheld "
                     "and you were NOT charged; retry with a fresh payment"),
            "remaining_budget_usd": round(remaining_budget(payer, budget_cap), 6)}

# ───────────────────────────────────────────────────────────────────────────
# ROADMAP — where the next expansions plug in. `grep -rn ">>> EXTENSION POINT"`
# to jump to each seam. Each is isolated so it can grow without touching the
# money path:
#   • ledger.py    — swap sqlite -> Postgres/Redis for multi-process / multi-host scale
#   • broker.py /  — add vendors to the judge pool for cross-PROVIDER independence
#     provider.py    (deepens independent_quorum past same-vendor weights)
#   • burst.py     — new verifier strategies; full ReDoS isolation
#   • flagstore.py — cross-agent SYNC of the verified-flag commons (the "hive")
#   • clearance.py — on-chain settle_tx verification; publish the cert as an open spec
#   • pricing.py   — dynamic / margin-governed pricing
#   • server.py    — new x402-gated resources; observability / metrics
# ───────────────────────────────────────────────────────────────────────────

# The two model families on our account. Independence = judging on a DIFFERENT family
# than generated the answer, so the check's errors are decorrelated from the answer's.
# Same inference vendor (Cerebras), different weights (OpenAI OSS vs Zhipu GLM) — good
# enough for real decorrelation; cross-PROVIDER independence is the stronger v2.
VERIFIER_MODEL = os.environ.get("VERIFIER_MODEL", "zai-glm-4.7")
VERIFIER_ALT = os.environ.get("VERIFIER_ALT", "gpt-oss-120b")
# The verifier families are REASONING models: they spend completion tokens on hidden
# reasoning before emitting the JSON verdict. With too small a budget the reasoning
# eats the whole allowance and `content` comes back EMPTY -> the judge fails closed on
# everything (even correct answers). Give the judge headroom so it actually answers.
JUDGE_MAX_TOKENS = int(os.environ.get("JUDGE_MAX_TOKENS", "1024"))
# The pool of judge families for a quorum (the k-of-M tier). Each must differ from the
# generator for real independence. Today both are on Cerebras — different weights, same
# vendor — so a genuine quorum is at most 2-of-2; the list is CONFIG, so adding a
# cross-PROVIDER model deepens the quorum with zero code change. Order = the single
# 'auto'-tier judge's preference.
JUDGE_FAMILIES = [m.strip() for m in
                  os.environ.get("JUDGE_FAMILIES", f"{VERIFIER_MODEL},{VERIFIER_ALT}").split(",")
                  if m.strip()]
# >>> EXTENSION POINT (independence depth): widen the judge pool across VENDORS here —
# add (provider, model) entries so independent_quorum spans different vendors, not just
# different weights on one vendor. The stronger the cross-vendor quorum, the stronger
# the "independent" claim (and the clearance tier built on it).
# Cross-PROVIDER judge (different vendor + weights, via OpenRouter). Active ONLY when
# both the key and a model are set; otherwise the pool is the Cerebras families and
# behaviour is unchanged. This is what makes "independent" defensible with no asterisk
# and deepens the quorum past 2-of-2.
OPENROUTER_JUDGE_MODEL = os.environ.get("OPENROUTER_JUDGE_MODEL", "").strip()


def _judge_pool():
    """All configured judges as (provider, model). Cerebras families always; the
    OpenRouter cross-provider judge appended when its key + model are present."""
    pool = [("cerebras", m) for m in JUDGE_FAMILIES]
    if OPENROUTER_JUDGE_MODEL and os.environ.get("OPENROUTER_API_KEY"):
        pool.append(("openrouter", OPENROUTER_JUDGE_MODEL))
    return pool


def _bind_judge(pname, vmodel):
    """A judge call bound to OUR key + ONE (provider, model), with reasoning headroom.
    api_key=None -> falls back to our env key for that provider (never the buyer's)."""
    tier = provider.OPENROUTER if pname == "openrouter" else provider.CEREBRAS
    def verify_fn(msgs, temperature=0.0):
        return provider.chat(msgs, tier=tier, temperature=temperature, api_key=None,
                             model=vmodel, max_tokens=JUDGE_MAX_TOKENS)
    return verify_fn


def _judge_families(generator_model):
    """The judges whose MODEL differs from the generator (the basis of real
    independence). Returns [(provider, model), ...]; always at least one."""
    gen = generator_model or provider.CEREBRAS["model"]
    fams = [(p, m) for (p, m) in _judge_pool() if m != gen]
    return fams or [("cerebras", VERIFIER_ALT if gen != VERIFIER_ALT else VERIFIER_MODEL)]


def _independent_verify_fn(generator_model):
    """Single independent judge — the 'auto' tier (verifier=independent_judge).
    Returns (verify_fn, verifier_model)."""
    pname, vmodel = _judge_families(generator_model)[0]
    return _bind_judge(pname, vmodel), vmodel


def _independent_verify_fns(generator_model):
    """ALL distinct independent judges — the quorum tier (verifier=independent_quorum).
    Returns [(verify_fn, verifier_model), ...]."""
    return [(_bind_judge(p, vm), vm) for (p, vm) in _judge_families(generator_model)]


# Per-payer spend, holds, free-trial counts and abuse breakers live in a DURABLE
# sqlite ledger (see ledger.py): they survive restarts and stay correct under
# concurrency (each read-modify-write is one transaction under a lock). This is what
# turns the spend governor + Sybil breakers from best-effort-in-RAM into real.
import ledger

DEFAULT_BUDGET_USD = float(os.environ.get("BURST_BUDGET_USD", "1.00"))  # per-agent cap

# Anti-abuse for the broker-paid judges (independent_judge/quorum — the only paths that
# spend OUR tokens). A miss yields no revenue, so without these a non-paying wallet could
# spam guaranteed-fail bursts to burn judge tokens. All three breakers are now DURABLE
# (ledger-backed), so a restart can't reset an attacker's streak:
#   Rule 1 = independent_judge requires BYOK (never host-key) -> a miss costs us at most
#            the ~$0.0004 judge call, never the buyer's generation.
#   Rule 2 = a wallet is cut off once its LIFETIME independent-miss count exceeds its
#            allowance (_miss_limit): unproven wallets get the flat IJ_MISS_LIMIT; proven
#            payers get revenue-scaled headroom. Misses are CUMULATIVE (not reset on a
#            pass), so the allowance is a CONSUMABLE budget, not a resettable ceiling —
#            each extra free miss must be "paid for" by settled spend that raised the
#            limit, keeping total free judge-burn bounded by (in fact below) revenue.
#   Rule 3 = a GLOBAL daily ceiling on judge calls from unproven wallets, so Sybil
#            wallet-rotation can't defeat the per-wallet breaker.
IJ_MISS_LIMIT = int(os.environ.get("IJ_MISS_LIMIT", "3"))
IJ_GLOBAL_DAILY = int(os.environ.get("IJ_GLOBAL_DAILY", "2000"))   # judge calls/day, unproven
IJ_PROVEN_MISS_UNIT_USD = float(os.environ.get("IJ_PROVEN_MISS_UNIT_USD", "0.003"))  # proven: +1 free-miss of headroom per this much settled spend
# Same Sybil-rotation cap for the free-trial HOST-key path: an unproven wallet's trial
# burst runs on our Cerebras key, and a craft-to-fail prompt never settles (its USDC
# recycles into fresh wallets), so the per-wallet trial_cap alone is defeatable. This is
# the aggregate daily ceiling on free-trial bursts from unproven wallets.
TRIAL_GLOBAL_DAILY = int(os.environ.get("TRIAL_GLOBAL_DAILY", "500"))  # trial bursts/day, unproven


def trial_used(payer):
    return ledger.trial_count(payer)


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
    """Spendable budget = cap minus settled spend AND outstanding holds (durable)."""
    return ledger.remaining(payer, cap)


def _gate_signal(res):
    """Machine-first go/no-go — the point of the product against 'agents going haywire'.
    The verdict gates the agent's NEXT STEP, not just the charge: an agent (or the MCP
    wrapper) reads `action` and HOLDS instead of acting on an unverified answer. We can't
    force a client to obey, but we return an unambiguous verdict it can gate on."""
    v = res.verdict or {}
    conf = v.get("agreement")  # self_consistency exposes a fraction; judge/check -> None
    # Surface independence so the agent knows the check was decorrelated from its own
    # answer (a different model family judged it) — the thing it can't self-supply. For a
    # quorum, expose the k-of-M tally so the agent sees HOW STRONG the consensus was.
    meth = v.get("method")
    if meth == "independent_judge":
        indep = {"independent": bool(v.get("independent")), "verifier_model": v.get("verifier_model")}
    elif meth == "independent_quorum":
        indep = {"independent": bool(v.get("independent")),
                 "quorum": f"{v.get('votes_for')}/{v.get('m')} agreed (needed {v.get('k')})",
                 "votes": v.get("votes")}
    else:
        indep = {}
    if res.passed:
        return {"verified": True, "action": "proceed",
                "advice": "Answer passed the verifier — OK to proceed (verified, not guaranteed).",
                "method": meth, "confidence": conf, **indep}
    return {"verified": False, "action": "hold",
            "advice": ("Answer did NOT pass the verifier — DO NOT act on it. Re-try, "
                       "escalate to a human, or treat the decision as unresolved."),
            "method": meth, "confidence": conf,
            "reason": v.get("reason") or v.get("votes"), **indep}


def _receipt(res, *, charged, tx=None):
    """A compact, KEEPABLE record an agent can store so the purchase compounds into
    memory instead of evaporating after one decision. The agent already holds the
    question; this gives it the durable lesson: did an INDEPENDENT check confirm or
    correct my answer, by which model, and is there an on-chain proof. `corrected`
    (independent check disagreed) is the signal worth remembering — 'on decisions
    like this, my first instinct was flagged.'"""
    v = res.verdict or {}
    reason = (v.get("reason") or v.get("raw") or "")
    rec = {
        "receipt_id": res.receipt_id,
        "verified": bool(res.passed),
        "corrected": not res.passed,                 # the learnable signal
        "method": v.get("method"),
        "independent": bool(v.get("independent")),
        "generator_model": v.get("generator_model"),
        "verifier_model": v.get("verifier_model"),
        "agreement": v.get("agreement"),
        "answer": res.answer,
        "verifier_note": (reason[:200] if isinstance(reason, str) else reason),
        "settle_tx": tx if charged else None,        # verifiable proof when charged
    }
    if v.get("method") == "independent_quorum":      # record the consensus, not one voice
        rec["quorum"] = {"k": v.get("k"), "m": v.get("m"), "votes_for": v.get("votes_for"),
                         "judges": [vote.get("verifier_model") for vote in (v.get("votes") or [])]}
    return rec


def _confirm_settlement_onchain(tx, pay_to, price_usd):
    """Defense-in-depth (#6): independently confirm the settle tx really moved >= price USDC
    to pay_to on-chain BEFORE we commit/deliver — so a facilitator that reports success
    without a real transfer can't buy a free result. Returns 'ok' (confirmed), 'bad' (receipt
    proves revert / underpay / wrong-payee -> WITHHOLD), or 'unknown' (sim tx or receipt
    unreachable -> trust the facilitator rather than block legit revenue on RPC flakiness)."""
    import clearance
    if not isinstance(tx, str) or not clearance._HASH_RE.match(tx):
        return "unknown"                      # sim/demo tx -> nothing on-chain to check
    if not pay_to:
        return "unknown"
    usdc = clearance._USDC.get(clearance._network())
    if not usdc:
        return "unknown"
    rcpt = clearance._default_receipt_fetch(tx)
    if rcpt is None:
        return "unknown"                      # RPC unreachable -> don't block revenue
    try:
        status = rcpt.get("status") if isinstance(rcpt, dict) else rcpt.status
        if int(status) != 1:
            return "bad"
        got = clearance._usdc_to(rcpt, pay_to, usdc)
    except Exception:
        return "unknown"
    need = int(round(float(price_usd) * 1_000_000))
    return "ok" if (got > 0 and got >= need) else "bad"


def _miss_limit(payer: str) -> int:
    """LIFETIME-miss allowance before Rule 2 locks a wallet. Unproven -> flat IJ_MISS_LIMIT.
    Proven -> that plus headroom proportional to settled revenue. Misses are cumulative
    (never reset on a pass), so this allowance is a CONSUMABLE budget: each settled pass
    raises it by ~1 per IJ_PROVEN_MISS_UNIT_USD of spend, and each miss consumes 1. An
    honest paying customer whose passes outrun their (legitimately-caught) misses keeps
    headroom and is never locked; a tiny-payment attacker cannot ratchet unlimited free
    judge-burn, because every extra miss must be funded by settled spend that raised the
    limit — so total free-burn stays below revenue."""
    base = IJ_MISS_LIMIT
    s = ledger.spent(payer)
    if s <= 0.0 or IJ_PROVEN_MISS_UNIT_USD <= 0.0:
        return base
    return base + int(s / IJ_PROVEN_MISS_UNIT_USD)


def serve_burst(request, *, x_payment=None, strategy="best_of_n", n=3,
                verifier="self_consistency", answer_key=None, check=None,
                budget_cap=DEFAULT_BUDGET_USD, facilitator=None, call_fn=None,
                receipt_id="burst", provider_key=None, model=None,
                require_byok=False, trial_cap=0, candidate=None, quorum_k=None):
    """Returns a result dict. `status` is one of:
       payment_required | budget_exceeded | not_verified(charged:false) | ok(charged:true).
    `candidate` = a caller-supplied answer to JUDGE (no generation). `quorum_k` = the k of
    a k-of-M independent quorum (verifier=independent_quorum)."""
    # How many independent judges this costs (M for a quorum; 1 otherwise) — prices the fee.
    judges = len(_judge_families(model)) if verifier == "independent_quorum" else 1
    # A caller-supplied candidate skips generation entirely (no best-of-N, no gen tokens).
    q = pricing.quote(strategy=("fast" if candidate is not None else strategy),
                      n=(1 if candidate is not None else n), verifier=verifier, judges=judges)
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

    # 1b) single-use payment: claim the authorization's nonce atomically BEFORE doing any
    #     work, so the same signed payment can't be replayed or fanned out across
    #     concurrent requests to extract multiple results before it settles on-chain.
    #     (Skipped for sim/test payments that don't decode — those are loopback-only.)
    pay_key = _payment_key(x_payment)
    if pay_key is not None and not ledger.claim_nonce(pay_key):
        return {"status": "payment_already_used", "payer": payer,
                "hint": ("this x402 authorization was already used — sign a fresh "
                         "payment per burst (authorizations are single-use)")}

    # 2) BYOK / free-trial gate. With no BYOK key, a wallet may run on the HOST key
    #    for its first `trial_cap` bursts, then must bring its own key. The payment
    #    was already validated above, so trial bursts still prove a funded wallet.
    is_trial = False
    if not provider_key and require_byok:
        used = ledger.trial_count(payer)
        if trial_cap and used < trial_cap:
            # Global Sybil cap (mirrors the independent-judge Rule 3): cap aggregate
            # host-key trial burn/day from UNPROVEN wallets so wallet rotation can't
            # defeat the per-wallet trial_cap. Proven payers (settled >=1) bypass.
            if not ledger.is_proven(payer) and not ledger.global_trial_reserve(1, TRIAL_GLOBAL_DAILY):
                return {"status": "trial_exhausted", "payer": payer,
                        "trial_used": used, "trial_cap": trial_cap,
                        "hint": ("daily free-trial host-inference budget is spent — send "
                                 "X-Provider-Key with your own Cerebras key, or retry tomorrow")}
            is_trial = True   # runs on the host env key; the slot is consumed after the burst
        else:
            return {"status": "byok_required", "payer": payer,
                    "trial_used": used, "trial_cap": trial_cap,
                    "hint": ("free trial used up — send X-Provider-Key with your own Cerebras key"
                             if trial_cap else
                             "send X-Provider-Key with your own Cerebras key")}

    # 2b) anti-abuse for the broker-paid judges (independent_judge + independent_quorum
    #     are the only paths that spend OUR tokens).
    if verifier in ("independent_judge", "independent_quorum", "tiered"):
        # Rule 1: never run the broker-paid judge on the host key. BYOK-only means a
        # miss costs us at most the judge call(s), never the buyer's generation. WAIVED
        # when a candidate is supplied (no generation happens — nothing to BYOK).
        # (call_fn is injected only by in-process demo/tests, which are trusted.)
        if not provider_key and call_fn is None and candidate is None:
            return {"status": "byok_required", "payer": payer,
                    "hint": ("this verifier runs judge model(s) on our key — send "
                             "X-Provider-Key (your own Cerebras key) so your generation is "
                             "BYOK; the independent check is on us. (Or pass a 'candidate' "
                             "answer to have us judge it directly, no generation.)"),
                    "verifier": verifier}
        # Rule 2: a wallet burning our judge tokens for free is cut off once its LIFETIME
        # miss count exceeds its allowance (_miss_limit). The allowance is revenue-scaled
        # and CONSUMABLE — misses are cumulative (not reset on a pass), so a proven wallet
        # can't ratchet unlimited free-burn by settling cheap passes: every extra miss must
        # be funded by settled spend that raised the limit. An honest payer whose passes
        # outrun their legitimate misses keeps headroom; a tiny-payment attacker stays near
        # base. (Unproven wallets never pass without settling, so lifetime == streak there.)
        proven = ledger.is_proven(payer)
        if ledger.miss_count(payer) >= _miss_limit(payer):
            return {"status": "verifier_locked", "payer": payer,
                    "verifier": verifier, "misses": ledger.miss_count(payer),
                    "hint": ("too many consecutive unverified independent bursts. "
                             "Use verifier=self_consistency (free to us, BYOK), or settle "
                             "one passing burst to reset and unlock broker-paid independence.")}
        # Rule 3 (global): cap aggregate host-key judge burn from unproven wallets/day,
        # so wallet rotation can't defeat the per-wallet breaker. Proven payers bypass.
        if not proven and not ledger.global_judge_reserve(judges, IJ_GLOBAL_DAILY):
            return {"status": "verifier_locked", "payer": payer, "verifier": verifier,
                    "hint": ("daily independent-verification budget for unproven wallets is "
                             "spent. Settle a passing burst to unlock, or retry tomorrow.")}

    # 3) governor: HOLD the fee up front (atomic check-and-reserve). Reserving before the
    #    burst — and counting holds against the cap — closes the gap where two concurrent
    #    bursts from one wallet both clear the check before either settles. The hold is
    #    released on a miss/failure and converted to spend only on a settled pass.
    if not ledger.reserve(payer, q["price_usd"], budget_cap):
        return {"status": "budget_exceeded", "payer": payer,
                "remaining_usd": round(remaining_budget(payer, budget_cap), 6),
                "price_usd": q["price_usd"]}

    # 4) buy more thinking. For independent_judge, bind the verifier to a DIFFERENT
    #    model family on OUR key so the check is genuinely decorrelated from the
    #    buyer's answer (the part an agent can't self-supply). Skip when a test
    #    injects its own call_fn (sim) — there's no real provider to judge on.
    try:
        verify_fn = verifier_model = verify_fns = reasoning_fn = reasoning_model = None
        if call_fn is None:
            if verifier == "independent_judge":
                verify_fn, verifier_model = _independent_verify_fn(model)
            elif verifier == "independent_quorum":
                verify_fns = _independent_verify_fns(model)        # M distinct families
            elif verifier == "tiered":
                import tiered                                       # SPEC-tiered-verifier-v1
                verify_fns = tiered.fast_judges(model)             # fast rung (diff families)
                reasoning_fn, reasoning_model = tiered.reasoning_judge()   # escalation rung
        res = burst_mod.run_burst(request, strategy=strategy, n=n, verifier=verifier,
                                  answer_key=answer_key, check=check,
                                  receipt_id=receipt_id, call_fn=call_fn,
                                  provider_key=provider_key, model=model,
                                  verify_fn=verify_fn, verifier_model=verifier_model,
                                  candidate=candidate, verify_fns=verify_fns, quorum_k=quorum_k,
                                  reasoning_fn=reasoning_fn, reasoning_model=reasoning_model)
    except Exception:
        ledger.release(payer, q["price_usd"])   # burst blew up -> nothing charged, free the hold
        raise
    if is_trial:                       # consume one free-trial slot per completed host-key burst
        ledger.trial_inc(payer)

    trial_remaining = max(0, trial_cap - ledger.trial_count(payer)) if trial_cap else 0

    # 5) settle ONLY if the verifier passed — else discard the authorization (no charge)
    if not res.passed:
        ledger.release(payer, q["price_usd"])   # miss -> free the hold, no charge
        if verifier in ("independent_judge", "independent_quorum", "tiered"):  # count toward abuse breaker
            ledger.record_miss(payer)
        return {"status": "not_verified", "charged": False, "price_usd": 0.0,
                "gate": _gate_signal(res),               # action=hold — don't act on this answer
                "verdict": res.verdict, "answer": res.answer, "payer": payer,
                "receipt": _receipt(res, charged=False),  # keepable even on a miss (corrected=true)
                "latency_s": res.latency_s, "cost_basis": res.cost_basis,
                "remaining_budget_usd": round(remaining_budget(payer, budget_cap), 6),
                "budget_cap_usd": budget_cap,
                "trial": is_trial, "trial_remaining": trial_remaining}

    # The verifier PASSED — but we only hand over the result if the on-chain capture
    # actually confirms. If settle errors or returns failure, WITHHOLD the answer and
    # release the hold: delivering a passing result without a captured payment would
    # break pay-only-if-verified (and let a reverted/expired auth buy free results).
    try:
        s = fac.settle(x_payment, reqs)
    except Exception:
        ledger.release(payer, q["price_usd"])   # settle errored -> no money moved, free the hold
        return _settle_failed(payer, budget_cap)
    if not s["success"]:
        ledger.release(payer, q["price_usd"])   # settle didn't confirm -> free the hold
        return _settle_failed(payer, budget_cap)
    # #6 defense-in-depth: independently confirm the tx really moved the fee on-chain before
    # we commit/deliver. A facilitator that reports success without a real USDC transfer to
    # the seller (bug/compromise) is caught here -> withhold the answer, no charge. A sim tx
    # or an unreachable RPC -> 'unknown' -> trust the facilitator (don't block legit revenue).
    if _confirm_settlement_onchain(s.get("tx"), os.environ.get("X402_PAY_TO"), q["price_usd"]) == "bad":
        ledger.release(payer, q["price_usd"])   # chain contradicts the facilitator -> free the hold
        return _settle_failed(payer, budget_cap)
    ledger.commit(payer, q["price_usd"])        # hold -> settled spend (raises _miss_limit)
    # NOTE: misses are LIFETIME-cumulative — a pass does NOT reset them. That makes the
    # proven free-miss allowance a consumable budget (not a resettable ceiling), so total
    # free judge-burn stays bounded by revenue. See _miss_limit / Rule 2.
    return {"status": "ok", "charged": True, "price_usd": q["price_usd"],
            "gate": _gate_signal(res),                   # action=proceed — verified, safe to act
            "tx": s.get("tx"), "mode": s.get("mode"), "verdict": res.verdict,
            "answer": res.answer, "payer": payer, "latency_s": res.latency_s,
            "receipt": _receipt(res, charged=True, tx=s.get("tx")),  # keep -> compounds
            "cost_basis": res.cost_basis, "receipt_id": res.receipt_id,
            "remaining_budget_usd": round(remaining_budget(payer, budget_cap), 6),
            "budget_cap_usd": budget_cap,
            "trial": is_trial, "trial_remaining": trial_remaining}
