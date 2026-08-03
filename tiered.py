"""Tiered escalation verifier — SPEC-tiered-verifier-v1.md.

STAGED / shadow: this module is ADDITIVE. It reuses burst.verify_judge (the hardened
prompt path) and provider, and is NOT wired into the payable serve_burst dispatch,
pricing, ledger, or abuse breakers. Nothing here can charge a buyer. Making `tiered` a
chargeable verifier (pricing + BYOK/miss-limit gates + receipt) is a deliberate follow-up.

Ladder (fail-closed throughout; escalation trigger is DISAGREEMENT, not self-confidence):
  RUNG 0  fast independent pair   (Cerebras, different family than generator, hardened prompt)
          -> unanimous clean PASS terminates here (cheap, sub-second)
  RUNG 1  reasoning judge         (cross-vendor, raised token budget) — authoritative
          -> consulted on ANY hold / disagreement / judge error
  RUNG 2  human gate              (extension point, default OFF) — the Concierge / Art-14 rung
          -> async: may return a PENDING resolution (answer withheld until a human resolves)
"""
import os
import concurrent.futures

import provider
import burst as burst_mod          # reuse verify_judge (hardened prompt); no cycle (lazy use there)

JUDGE_MAX_TOKENS = int(os.environ.get("JUDGE_MAX_TOKENS", "1024"))
FAST_JUDGES = os.environ.get("VERIFIER_FAST_JUDGES", "").strip()
REASONING = os.environ.get("VERIFIER_REASONING_JUDGE",
                           "openrouter:qwen/qwen3-235b-a22b-thinking-2507").strip()
REASONING_MAX = int(os.environ.get("VERIFIER_REASONING_MAX_TOKENS", "4000"))
ESCALATE = os.environ.get("VERIFIER_ESCALATE", "nonunanimous_pass").strip()
# Minimum judges for the fast rung to be able to say "unanimous" and TERMINATE on a pass.
# One judge agreeing with itself is not a quorum: with a single fast judge, ESCALATE=
# nonunanimous_pass makes `unanimous_pass` true on that one vote, the reasoning rung is
# never consulted on the pass side, and a payable verdict becomes a 1-of-1 call. Set to 1
# only if you deliberately want a single-judge fast tier to be terminal.
MIN_FAST_JUDGES = int(os.environ.get("VERIFIER_MIN_FAST_JUDGES", "2"))


def _parse_spec(spec):
    """'provider:model' -> (provider, model); bare 'model' defaults to cerebras."""
    spec = spec.strip()
    if ":" in spec:
        p, m = spec.split(":", 1)
        if p in ("cerebras", "openrouter"):
            return p, m.strip()
    return "cerebras", spec


def bind(pname, model, max_tokens=None):
    """A judge call bound to OUR key + one (provider, model). Mirrors broker._bind_judge,
    plus an optional per-call max_tokens so the reasoning rung gets reasoning headroom."""
    tier = provider.OPENROUTER if pname == "openrouter" else provider.CEREBRAS
    mt = max_tokens or JUDGE_MAX_TOKENS

    def vfn(msgs, temperature=0.0):
        return provider.chat(msgs, tier=tier, temperature=temperature, api_key=None,
                             model=model, max_tokens=mt)
    return vfn


def _run_judge(answer, request, vf, vm):
    """One judge -> tri-state: adequate True/False on a clean verdict, None on ERROR
    (mirrors PoB's fail-loud tri-state; an errored judge is never a silent pass)."""
    try:
        _, vd = burst_mod.verify_judge(answer, request, vf, method="independent_judge",
                                       meta={"verifier_model": vm})
        return {"verifier_model": vm, "adequate": bool(vd.get("adequate")),
                "reason": (vd.get("raw") or "")[:160], "ok": True}
    except Exception as e:
        return {"verifier_model": vm, "adequate": None,
                "reason": f"judge error: {type(e).__name__}", "ok": False}


def _reason(votes):
    if not votes:
        return "no_fast_judge"
    if any(not v["ok"] for v in votes):
        return "judge_error"
    if len({v["adequate"] for v in votes}) > 1:
        return "disagreement"
    return "hold"


def fast_judges(generator_model):
    """Fast-rung judges (different family than the generator). From VERIFIER_FAST_JUDGES
    if set, else a safe default (Gemma=Google + GLM-4.7 until Aug-17, both != gpt-oss)."""
    gen = generator_model or provider.CEREBRAS["model"]
    if FAST_JUDGES:
        specs = [_parse_spec(s) for s in FAST_JUDGES.split(",") if s.strip()]
    else:
        specs = [("cerebras", "gemma-4-31b"), ("cerebras", "zai-glm-4.7")]
    return [(bind(p, m), m) for (p, m) in specs if m != gen]


def reasoning_judge():
    if not REASONING:
        return (None, None)
    p, m = _parse_spec(REASONING)
    return (bind(p, m, max_tokens=REASONING_MAX), m)


def verify(answer, request, *, fast_fns=None, reasoning=None, human_gate=None,
           escalate=None, generator_model=None):
    """The 2(+1)-rung ladder. Pure verify — no payment / no ledger. Returns a verdict dict
    with 'adequate' (bool) plus 'tier', 'fast_votes', 'escalation_reason' for the receipt."""
    escalate = escalate or ESCALATE
    if fast_fns is None:
        fast_fns = fast_judges(generator_model)
    if reasoning is None:
        reasoning = reasoning_judge()

    # RUNG 0 — fast independent pair, in parallel
    votes = []
    if fast_fns:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(fast_fns))) as ex:
            futs = [ex.submit(_run_judge, answer, request, vf, vm) for vf, vm in fast_fns]
            votes = [f.result() for f in futs]
    unanimous_pass = bool(votes) and all(v["ok"] and v["adequate"] is True for v in votes)

    if escalate == "never":
        esc = False
    elif escalate == "always":
        esc = True
    elif escalate == "disagree_only":
        oks = [v for v in votes if v["ok"]]
        esc = (len({v["adequate"] for v in oks}) > 1) or any(not v["ok"] for v in votes) or not votes
    else:  # nonunanimous_pass (default)
        esc = not unanimous_pass

    # A fast rung thinner than MIN_FAST_JUDGES cannot produce a meaningful "unanimous" pass.
    # Losing a judge (a deprecated model, an unset env var, a filtered-out generator match)
    # must degrade to MORE scrutiny, not less — otherwise the pool silently shrinking turns a
    # terminal PASS into a single model's opinion while the receipt still says independent.
    # ESCALATE="never" is an explicit operator override and is left alone.
    thin_rung = len(votes) < MIN_FAST_JUDGES
    if thin_rung and escalate != "never":
        esc = True

    # independent = at least one judge DECORRELATED FROM THE GENERATOR — the same predicate
    # burst.py:192 already uses for independent_judge. `bool(votes)` was weaker: it said True
    # for any vote at all, and that flag is load-bearing (broker._receipt, flagstore.is_cleared,
    # and clearance.py's cert["cleared"] = verified and independent, which strangers honour).
    base = {"method": "tiered", "generator_model": generator_model,
            "fast_votes": votes, "fast_judge_count": len(votes), "thin_fast_rung": thin_rung,
            "independent": any((v.get("verifier_model") or "") != (generator_model or "")
                               for v in votes)}

    if not esc:
        # Terminal on the fast path. adequate = the fast consensus (True only if unanimous
        # clean PASS; any agreed-hold / never-mode non-unanimous -> False, fail-closed).
        return {**base, "tier": "fast", "adequate": unanimous_pass,
                "verifier_model": ",".join(v["verifier_model"] for v in votes),
                "escalation_reason": "unanimous_pass" if unanimous_pass else _reason(votes)}

    # RUNG 1 — reasoning judge (authoritative), or fail-closed hold if none configured
    rfn, rmodel = reasoning
    if rfn is None:
        out = {**base, "tier": "escalated", "adequate": False, "verifier_model": None,
               "escalation_reason": _reason(votes),
               "note": "no reasoning judge configured -> fail-closed hold"}
    else:
        rv = _run_judge(answer, request, rfn, rmodel)
        out = {**base, "tier": "escalated", "adequate": rv["adequate"] is True,
               "verifier_model": rmodel, "reasoning_vote": rv,
               # the reasoning judge is itself decorrelated from the generator, so it
               # supplies independence even when the fast rung was thin or empty.
               "independent": base["independent"] or ((rmodel or "") != (generator_model or "")),
               "escalation_reason": (("fast rung had %d judge(s), need %d for a terminal pass; "
                                      % (len(votes), MIN_FAST_JUDGES)) if thin_rung else "")
                                    + (_reason(votes) or "")}

    # RUNG 2 — human gate (extension point; default OFF). Contract:
    #   human_gate(answer, request, verdict) ->
    #     None                                   abstain (keep the reasoning verdict)
    #     {"decision":"pass"|"hold", "pending":False}   human resolved it
    #     {"pending":True, ...}                  queued; answer WITHHELD until resolved (async)
    # Fail-closed: any human-gate error -> hold. Never auto-passes.
    if human_gate is not None:
        try:
            hg = human_gate(answer=answer, request=request, verdict=out)
        except Exception as e:
            hg = {"decision": "hold", "pending": False, "error": type(e).__name__}
        if hg:
            out["human"] = hg
            if hg.get("pending"):
                out.update(tier="human_pending", adequate=False)
            elif hg.get("decision") in ("pass", "hold"):
                out.update(tier="human", adequate=(hg["decision"] == "pass"))
    return out


def shadow(request, *, candidate=None, model=None, provider_key=None, human_gate=None):
    """Out-of-band SHADOW run — no payment, no ledger, never charges. Judges a supplied
    candidate, or generates one temp-0 answer to judge. For offline eval + future
    live-shadow logging alongside the real verifier."""
    if candidate is None:
        r = provider.chat([{"role": "user", "content": request}], temperature=0.0,
                          api_key=provider_key, model=model)
        candidate = r["text"]
    return {"candidate": candidate,
            "verdict": verify(candidate, request, generator_model=model, human_gate=human_gate)}
