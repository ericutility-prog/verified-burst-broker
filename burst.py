"""The verified-burst core — the thing we actually sell.

A "burst" = an agent hits a hard/irreversible/deadline decision and buys MORE
THINKING: escalate to fast silicon, optionally sample best-of-N, then GATE the
output through a verifier. The caller pays only if the verifier says it's adequate
(pay-only-if-verified). This module produces the result + verdict; settlement
(charge/void) is the x402 layer's job, keyed off `result.passed`.

Design note: `call_fn` is injectable. In production it's provider.chat (real
Cerebras, BYOK). Tests/sim inject a scripted call_fn so we can demo both the
verified and unverified settlement paths WITHOUT fabricating numbers inside the
product code.
"""
import json
import re
import concurrent.futures
from collections import Counter
from dataclasses import dataclass, field, asdict

import provider


@dataclass
class BurstResult:
    answer: str
    candidates: list
    passed: bool
    verdict: dict
    strategy: str
    n: int
    usage_total: dict
    cost_basis: float          # what the underlying tokens cost (BYOK)
    latency_s: float
    receipt_id: str

    def public(self):
        d = asdict(self)
        d.pop("candidates", None)  # don't leak rejected drafts to the buyer
        return d


# --------------------------- verifiers --------------------------------------- #
# >>> EXTENSION POINT (verifiers): add a strategy by writing a verify_* fn here and a
# branch in run_burst's dispatch below. ReDoS note: _extract's regex screen is a
# heuristic for the common nested-quantifier class — full safety needs the `regex`
# module's timeout or subprocess isolation (stdlib `re` can't be interrupted in-thread).
def _extract(text, answer_key):
    """Pull a normalized answer for agreement checks. answer_key:
       None -> whole trimmed text;  ('json', field) -> JSON field;  ('regex', pat) -> group 1."""
    if answer_key is None:
        return text.strip()
    kind, spec = answer_key
    if kind == "json":
        try:
            s = text[text.index("{"): text.rindex("}") + 1]
            return str(json.loads(s).get(spec, "")).strip().lower()
        except Exception:
            return ""
    if kind == "regex":
        # Caller-supplied pattern → bound it: cap length and never let a pathological
        # pattern (ReDoS) crash or wedge the worker. A bad/oversized pattern yields "".
        if not isinstance(spec, str) or len(spec) > 200:
            return ""
        # ReDoS screen: reject nested-quantifier patterns — a group containing * or +
        # that is ITSELF quantified, e.g. (a+)+ / (a*)* / (.*)+ — the classic
        # catastrophic-backtracking class that can wedge a worker for seconds on a
        # short input. Legit extractors like (\d+) or (yes|no) have no quantifier
        # AFTER the group, so they pass. (Heuristic, not a proof — stdlib `re` can't
        # be interrupted; full coverage needs the `regex` module's timeout. See
        # security_probe.py frontier.)
        if re.search(r"\([^)]*[*+][^)]*\)[*+?{]", spec):
            return ""
        try:
            m = re.search(spec, text[:4000])
            return (m.group(1) if (m and m.groups()) else "").strip().lower()
        except re.error:
            return ""
    return text.strip()


def verify_self_consistency(candidates, answer_key=None, threshold=0.6):
    """Majority agreement across best-of-N. Adequate if the modal answer clears threshold."""
    norm = [_extract(c["text"], answer_key) for c in candidates]
    norm = [x for x in norm if x]
    if not norm:
        return "", {"method": "self_consistency", "adequate": False, "reason": "no parseable answers"}
    winner, count = Counter(norm).most_common(1)[0]
    frac = count / len(candidates)
    pick = next(c["text"] for c in candidates if _extract(c["text"], answer_key) == winner)
    return pick, {"method": "self_consistency", "adequate": frac >= threshold,
                  "agreement": round(frac, 2), "votes": f"{count}/{len(candidates)}"}


def verify_check(candidate_text, check):
    """Deterministic caller-supplied gate. check(text)->bool. The honest gold standard."""
    ok = bool(check(candidate_text))
    return candidate_text, {"method": "deterministic_check", "adequate": ok}


def verify_judge(candidate_text, request, call_fn, *, method="judge", meta=None):
    """Adversarial LLM judge — default to NOT adequate when unsure.

    `call_fn` is whatever model does the judging. For `method="independent_judge"`
    the caller binds `call_fn` to a DIFFERENT model family than the generator, so the
    check is decorrelated from the answer's own blind spots — the one form of 'more
    thinking' an agent cannot self-supply from its own correlated samples. `meta`
    carries that independence record (generator vs verifier model) for the receipt."""
    prompt = (f"You are a strict adversarial verifier. TASK:\n{request}\n\nCANDIDATE ANSWER:\n"
              f"{candidate_text}\n\nIs the candidate correct AND fully responsive? "
              'Reply JSON {"adequate": true|false, "reason": "<short>"} only. '
              "Default to false if you are not sure.")
    r = call_fn([{"role": "user", "content": prompt}])
    v = _extract(r["text"], ("json", "adequate"))
    verdict = {"method": method, "adequate": v in ("true", "1", "yes"),
               "raw": r["text"][:200]}
    if meta:
        verdict.update(meta)
    return candidate_text, verdict


# --------------------------- the burst -------------------------------------- #
def run_burst(request, *, strategy="best_of_n", n=3, verifier="self_consistency",
              answer_key=None, check=None, receipt_id="sim", call_fn=None,
              provider_key=None, model=None, verify_fn=None, verifier_model=None,
              candidate=None, verify_fns=None, quorum_k=None):
    if candidate is not None:
        # Caller ALREADY has an answer (e.g. their agent's own decision) — judge THAT
        # instead of generating. No generation tokens spend; the independent judge
        # checks the provided answer. This is what makes the guard a 'verify my agent's
        # decision' bolt-on. Pairs with verifier=independent_judge (or judge).
        candidates = [{"text": str(candidate), "usage": {}, "latency_s": 0.0}]
    else:
        if call_fn is None:
            # Real provider path: thread the buyer's BYOK key/model into every sample.
            def call_fn(msgs, temperature=0.0):
                return provider.chat(msgs, temperature=temperature,
                                     api_key=provider_key, model=model)
        n = 1 if strategy == "fast" else min(max(2, n), 16)   # hard cap thread fan-out
        msgs = [{"role": "user", "content": request}]

        # vary temperature across samples so best-of-N actually explores
        temps = [0.0 if i == 0 else 0.7 for i in range(n)]

        # Run the N samples CONCURRENTLY (I/O-bound HTTP). Burst latency = the slowest
        # single call, not the sum — this is what makes a best-of-N burst "hum". Tolerant
        # of partial failures: a 429/timeout on one sample doesn't sink the whole burst.
        indexed = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
            futs = {ex.submit(call_fn, msgs, temperature=temps[i]): i for i in range(n)}
            for fut in concurrent.futures.as_completed(futs):
                try:
                    indexed.append((futs[fut], fut.result()))
                except Exception:
                    pass  # drop a failed sample; verifier works on whoever returned
        if not indexed:
            raise RuntimeError("all burst samples failed")
        indexed.sort(key=lambda t: t[0])       # keep i==0 (the temp-0 anchor) first
        candidates = [r for _, r in indexed]

    usage_total, latency = Counter(), 0.0
    for r in candidates:
        for k, v in (r.get("usage") or {}).items():
            if isinstance(v, int):
                usage_total[k] += v
        latency = max(latency, r.get("latency_s", 0.0))  # concurrent -> wall ≈ max

    if verifier == "deterministic_check" and check is not None:
        answer, verdict = verify_check(candidates[0]["text"], check)
        # with a hard check, prefer the first candidate that passes
        for c in candidates:
            if check(c["text"]):
                answer, verdict = c["text"], {"method": "deterministic_check", "adequate": True}
                break
    elif verifier == "judge":
        answer, verdict = verify_judge(candidates[0]["text"], request, call_fn)
    elif verifier == "independent_judge":
        # Broker-provided independence: judge the buyer's primary answer (the temp-0
        # anchor) on a DIFFERENT model family than generated it. `verify_fn` is bound
        # by the orchestrator to our key + a decorrelated model. If no independent fn
        # was supplied we fall back to the generator's own model AND flag it honestly
        # (independent: False) rather than silently pretending the check was independent.
        independent = verify_fn is not None and (verifier_model or "") != (model or "")
        vfn = verify_fn or call_fn
        meta = {"independent": independent,
                "generator_model": model,
                "verifier_model": (verifier_model if verify_fn else model)}
        answer, verdict = verify_judge(candidates[0]["text"], request, vfn,
                                       method="independent_judge", meta=meta)
    elif verifier == "independent_quorum":
        # k-of-M independent judges (distinct families) must agree — the consensus tier.
        # A lone judge is just the 1-of-1 case, so this generalizes independent_judge.
        # Each judge is decorrelated from the others AND from the generator, so the vote
        # is a real quorum, not one model echoing itself.
        answer = candidates[0]["text"]
        fns = list(verify_fns or ([(verify_fn, verifier_model)] if verify_fn else []))
        votes = []
        for vf, vm in fns:
            try:
                _, vd = verify_judge(answer, request, vf, method="independent_judge",
                                     meta={"verifier_model": vm})
                adequate, reason = bool(vd.get("adequate")), (vd.get("raw") or "")[:160]
            except Exception as e:                 # a judge that ERRORS is a NO vote, never a crash
                adequate, reason = False, f"judge error: {type(e).__name__}"
            votes.append({"verifier_model": vm, "adequate": adequate, "reason": reason})
        m = len(votes)
        # Clamp k into [1, m]. NEVER let k <= 0 (a negative/zero k would pass with ZERO
        # agreeing judges — charging for a wholly-unverified answer) nor k > m (impossible
        # to satisfy). `quorum_k or m` maps None/0 to unanimous; min/max bound the rest.
        k = max(1, min(quorum_k or m, m)) if m else 1
        yes = sum(1 for v in votes if v["adequate"])
        verdict = {"method": "independent_quorum", "adequate": (m > 0 and yes >= k),
                   "k": k, "m": m, "votes_for": yes, "independent": m >= 1,
                   "generator_model": model, "votes": votes}
    else:
        answer, verdict = verify_self_consistency(candidates, answer_key)

    return BurstResult(
        answer=answer,
        candidates=candidates,
        passed=bool(verdict.get("adequate")),
        verdict=verdict,
        strategy=strategy,
        n=n,
        usage_total=dict(usage_total),
        cost_basis=provider.token_cost(dict(usage_total)),
        latency_s=latency,
        receipt_id=receipt_id,
    )
