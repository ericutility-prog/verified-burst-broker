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
        m = re.search(spec, text)
        return (m.group(1) if m else "").strip().lower()
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


def verify_judge(candidate_text, request, call_fn):
    """Adversarial LLM judge — default to NOT adequate when unsure."""
    prompt = (f"You are a strict adversarial verifier. TASK:\n{request}\n\nCANDIDATE ANSWER:\n"
              f"{candidate_text}\n\nIs the candidate correct AND fully responsive? "
              'Reply JSON {"adequate": true|false, "reason": "<short>"} only. '
              "Default to false if you are not sure.")
    r = call_fn([{"role": "user", "content": prompt}])
    v = _extract(r["text"], ("json", "adequate"))
    return candidate_text, {"method": "judge", "adequate": v in ("true", "1", "yes"),
                            "raw": r["text"][:200]}


# --------------------------- the burst -------------------------------------- #
def run_burst(request, *, strategy="best_of_n", n=3, verifier="self_consistency",
              answer_key=None, check=None, receipt_id="sim", call_fn=None):
    call_fn = call_fn or provider.chat
    n = 1 if strategy == "fast" else max(2, n)
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
    indexed.sort(key=lambda t: t[0])           # keep i==0 (the temp-0 anchor) first
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
