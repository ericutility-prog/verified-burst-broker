"""Prove the independent_judge is REAL independence, not self-grading.

Three checks, no fabricated numbers:
  A) regression — existing self_consistency still works on a scripted call_fn.
  B) CATCH     — a scripted generator is confidently WRONG ("Berlin"); a REAL judge
                 on a DIFFERENT family (zai-glm-4.7) must reject it. This is the proof:
                 the check is decorrelated from the answer's blind spots.
  C) CONFIRM   — fully live: generate on gpt-oss-120b, judge on zai-glm-4.7; a correct
                 answer passes and the verdict records generator != verifier model.

Run:  .venv/bin/python test_independence.py
Needs our CEREBRAS_API_KEY in .env (judge tokens are ours). Generation in (B) is
scripted so the only network calls are the independent judge — cheap + deterministic
about WHAT is being judged.
"""
import env; env.load_env()
import burst as burst_mod
import broker


def scripted(text):
    def call_fn(msgs, temperature=0.0):
        return {"text": text, "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                "latency_s": 0.0}
    return call_fn


def main():
    # A) regression: self_consistency unchanged
    res = burst_mod.run_burst("pick yes or no", strategy="best_of_n", n=3,
                              verifier="self_consistency", answer_key=("regex", r"(yes|no)"),
                              call_fn=scripted("Answer: yes"))
    print(f"[A] self_consistency passed={res.passed} method={res.verdict['method']} "
          f"votes={res.verdict.get('votes')}")
    assert res.verdict["method"] == "self_consistency", "regression: method changed"

    # Build the REAL independent judge (our key + a different family than the generator).
    vfn, vmodel = broker._independent_verify_fn("gpt-oss-120b")
    print(f"    independent verifier model = {vmodel}  (generator = gpt-oss-120b)")
    assert vmodel != "gpt-oss-120b", "verifier must differ from generator family"

    # B) CATCH: confidently-wrong generator, REAL independent judge must reject.
    req = "What is the capital of France? Reply with just the city name."
    res = burst_mod.run_burst(req, strategy="fast", n=1, verifier="independent_judge",
                              call_fn=scripted("The capital of France is Berlin."),
                              verify_fn=vfn, verifier_model=vmodel, model="gpt-oss-120b")
    v = res.verdict
    print(f"[B] CATCH wrong-answer: passed={res.passed} independent={v.get('independent')} "
          f"gen={v.get('generator_model')} judge={v.get('verifier_model')} "
          f"reason={v.get('raw','')[:80]!r}")
    assert v["method"] == "independent_judge"
    assert v["independent"] is True, "judge must be flagged independent"
    assert res.passed is False, "independent judge FAILED to catch a wrong answer"
    # guard against a fail-CLOSED judge masquerading as 'catching': it must actually
    # have emitted a verdict, not returned empty (which would default to false on ALL).
    assert v.get("raw", "").strip(), "judge returned EMPTY — failing closed, not judging"

    # C) CONFIRM: fully live generate + independent judge on a correct, easy fact.
    vfn2, vmodel2 = broker._independent_verify_fn("gpt-oss-120b")
    res = burst_mod.run_burst(req, strategy="best_of_n", n=2, verifier="independent_judge",
                              provider_key=None, model="gpt-oss-120b",
                              verify_fn=vfn2, verifier_model=vmodel2)
    v = res.verdict
    print(f"[C] CONFIRM live: answer={res.answer.strip()[:40]!r} passed={res.passed} "
          f"gen={v.get('generator_model')} judge={v.get('verifier_model')} "
          f"latency={res.latency_s:.2f}s")
    assert v["generator_model"] != v["verifier_model"], "not actually independent"
    assert res.passed is True, "independent judge REJECTED a correct answer (failing closed?)"

    print("\nALL CHECKS PASSED — the judge is a different model and gates on its own verdict.")


if __name__ == "__main__":
    main()
