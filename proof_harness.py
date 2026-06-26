"""Honest proof harness for verified-burst — does the independent judge actually
earn its penny?

WHAT THIS MEASURES (and why it's proof, not marketing):
  * Items are PROGRAMMATICALLY GENERATED with ground truth computed by Python
    (multiplication, letter-counting, modular/date/base arithmetic, power
    comparison). Nobody hand-picks which questions the model gets wrong, and
    nobody hand-asserts the "correct" answer — so the catch rate can't be cooked.
  * It runs the PRODUCT's real judge path: gpt-oss-120b generates the answer,
    a DIFFERENT family (zai-glm-4.7) judges it via broker._independent_verify_fn —
    the exact independent_judge mechanism a buyer pays for.
  * It reports its OWN failure modes (false-confirm, false-alarm), not just the
    number that flatters the product. If the judge is mediocre, this says so.

The pay logic mirrored from the product: a buyer pays the service fee ONLY when
the judge PASSES (verdict adequate -> gate 'proceed'). A judge MISS is free.

  base WRONG  + judge HOLD  -> CAUGHT      (free; the win — agent doesn't act on a bug)
  base WRONG  + judge PASS  -> MISSED      (charged; the dangerous case — paid AND wrong)
  base RIGHT  + judge PASS  -> CONFIRMED   (charged; what the penny buys: a vouched answer)
  base RIGHT  + judge HOLD  -> FALSE ALARM (free; annoying redo, but no $ lost)

Run:  .venv/bin/python proof_harness.py [N]   (default N=30)
Writes proof_results.json + PROOF.md.
"""
import json
import random
import re
import sys
import time
import concurrent.futures
from datetime import date, timedelta

import env; env.load_env()
import provider
import pricing
import broker
import burst

SEED = 1729                       # fixed -> the whole run is reproducible
GEN_MODEL = provider.CEREBRAS["model"]   # gpt-oss-120b
N = int(sys.argv[1]) if len(sys.argv) > 1 else 30

_WORDS = ["strawberry", "mississippi", "bookkeeper", "raspberry", "millennium",
          "assassination", "embarrassment", "committee", "tennessee", "successful",
          "possession", "necessary", "accommodate", "occurrence", "parallel"]
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _gen(rng):
    """One programmatically-generated, mechanically-checkable item, in the HARD
    regime where a tool-less base model genuinely errs at a measurable rate (that
    is exactly where buying verification pays — on easy decisions the base model is
    usually right and you wouldn't). Difficulty is fixed/declared, not tuned to a
    target answer. Returns (question, ground_truth_normalized, kind)."""
    kind = rng.choice(["count", "mult", "step", "mod", "weekday", "hex", "power", "index"])
    if kind == "count":
        # count a letter in a LONG random string — real counting, not recall
        s = "".join(rng.choice("abcde") for _ in range(rng.randint(55, 80)))
        c = rng.choice("abcde")
        return (f"How many times does the letter '{c}' appear in this string? "
                f"Reply with just the number.\n{s}", str(s.count(c)), kind)
    if kind == "mult":
        a, b = rng.randint(1000, 9999), rng.randint(100, 999)   # 4x3 digit
        return (f"What is {a} * {b}? Reply with just the number.", str(a * b), kind)
    if kind == "step":
        a, b, c, d = (rng.randint(11, 99) for _ in range(4))
        return (f"What is {a} * {b} + {c} * {d}? Reply with just the number.",
                str(a * b + c * d), kind)
    if kind == "mod":
        a, b = rng.randint(100000, 9999999), rng.randint(101, 997)
        return (f"What is the remainder when {a} is divided by {b}? "
                f"Reply with just the number.", str(a % b), kind)
    if kind == "weekday":
        d0 = date(2026, 1, 1) + timedelta(days=rng.randint(0, 364))
        n = rng.randint(40, 900)
        gt = _WEEKDAYS[(d0 + timedelta(days=n)).weekday()]
        return (f"What day of the week is {n} days after {d0.isoformat()} "
                f"(a {_WEEKDAYS[d0.weekday()]})? Reply with just the weekday name.", gt, kind)
    if kind == "hex":
        v = rng.randint(20000, 999999)
        return (f"What is the decimal number {v} written in hexadecimal? "
                f"Reply with just the hex digits, no '0x'.", format(v, "x"), kind)
    if kind == "power":
        a, b = rng.randint(2, 9), rng.randint(5, 12)
        c, d = rng.randint(2, 9), rng.randint(5, 12)
        while a ** b == c ** d:
            d += 1
        gt = "first" if a ** b > c ** d else "second"
        return (f"Which is larger, {a}^{b} or {c}^{d}? Reply with exactly one word: "
                f"'first' or 'second'.", gt, kind)
    # index
    w = rng.choice(_WORDS); c = rng.choice(sorted(set(w)))
    return (f"What is the 1-indexed position of the first '{c}' in '{w}'? "
            f"Reply with just the number.", str(w.index(c) + 1), kind)


def _norm(s):
    return re.sub(r"[\s,]", "", str(s)).strip().lower()


def _extract(text, kind):
    """Pull the comparable answer from the model's reply, by item kind."""
    t = text.strip().lower()
    if kind == "weekday":
        m = re.search(r"\b(" + "|".join(_WEEKDAYS) + r")\b", t)
        return m.group(1) if m else _norm(t)
    if kind == "power":
        m = re.search(r"\b(first|second)\b", t)
        return m.group(1) if m else _norm(t)
    if kind == "hex":
        m = re.search(r"0x([0-9a-f]+)|\b([0-9a-f]{2,})\b", t)
        return (m.group(1) or m.group(2)) if m else _norm(t)
    m = re.search(r"-?\d[\d,]*", t)          # first integer
    return _norm(m.group(0)) if m else _norm(t)


def _run_one(item):
    q, gt, kind = item
    # 1) base model's confident answer (temp 0) — the thing the agent would act on
    try:
        b = provider.chat([{"role": "user", "content": q}], temperature=0.0, max_tokens=512)
    except Exception as e:
        return {"q": q, "kind": kind, "error": f"gen:{type(e).__name__}"}
    base_ans = b["text"]
    correct = _extract(base_ans, kind) == _norm(gt)
    # 2) the PRODUCT's independent judge (different family) checks that answer
    vfn, vmodel = broker._independent_verify_fn(None)
    try:
        _, verdict = burst.verify_judge(base_ans, q, vfn, method="independent_judge")
        judge_pass = bool(verdict.get("adequate"))
        judge_err = None
    except Exception as e:
        judge_pass, judge_err, vmodel = True, f"judge:{type(e).__name__}", vmodel  # fail-open here = conservative for OUR claim
    return {"q": q, "kind": kind, "ground_truth": gt,
            "base_extract": _extract(base_ans, kind), "base_correct": correct,
            "judge_pass": judge_pass, "judge_model": vmodel, "judge_err": judge_err,
            "gen_usage": b.get("usage", {})}


def main():
    rng = random.Random(SEED)
    items = [_gen(rng) for _ in range(N)]
    t0 = time.monotonic()
    rows = []
    print(f"running {N} items  (gen={GEN_MODEL}, judge=independent family)  ...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for i, r in enumerate(ex.map(_run_one, items), 1):
            rows.append(r)
            if i % 5 == 0:
                print(f"  {i}/{N}")
    dt = time.monotonic() - t0
    rows = [r for r in rows if "error" not in r]

    n = len(rows)
    correct = sum(r["base_correct"] for r in rows)
    wrong = n - correct
    caught = sum(1 for r in rows if not r["base_correct"] and not r["judge_pass"])
    missed = sum(1 for r in rows if not r["base_correct"] and r["judge_pass"])
    confirmed = sum(1 for r in rows if r["base_correct"] and r["judge_pass"])
    false_alarm = sum(1 for r in rows if r["base_correct"] and not r["judge_pass"])
    charged = confirmed + missed                      # buyer pays only when judge passes

    fee = pricing.quote(verifier="independent_judge")["price_usd"]
    pct = lambda a, b: (round(100.0 * a / b, 1) if b else None)

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "seed": SEED, "n": n, "generator_model": GEN_MODEL,
        "judge_model": rows[0]["judge_model"] if rows else None,
        "service_fee_per_charged_usd": fee,
        "base_accuracy_pct": pct(correct, n),
        "base_wrong": wrong,
        "catch_rate_pct": pct(caught, wrong),          # of the model's mistakes, % the judge HELD
        "false_confirm_rate_pct": pct(missed, wrong),  # of the model's mistakes, % the judge waved through (paid+wrong)
        "false_alarm_rate_pct": pct(false_alarm, correct),  # of correct answers, % the judge wrongly held (free)
        "charged_precision_pct": pct(confirmed, charged),   # WHEN YOU PAY, % the answer was actually right
        "counts": {"caught": caught, "missed": missed, "confirmed": confirmed,
                   "false_alarm": false_alarm, "charged": charged},
        "economics": {
            "fees_charged_usd": round(charged * fee, 6),
            "catches_delivered_free": caught,
            "downside_per_catch_usd": 0.0,
        },
        "wall_s": round(dt, 1),
    }

    # --- human-readable proof ---
    L = []
    L.append(f"# verified-burst — independent judge proof  ({summary['generated_at']})\n")
    L.append(f"**{n}** programmatically-generated, mechanically-checkable decisions. "
             f"Generator **{GEN_MODEL}**; independent judge **{summary['judge_model']}** "
             f"(a different model family). Ground truth computed in code (seed {SEED}, reproducible). "
             f"Items, generator, and judge are the product's real path — nothing hand-picked.\n")
    L.append(f"- Base model got **{correct}/{n}** right ({summary['base_accuracy_pct']}%); "
             f"**{wrong}** wrong.")
    L.append(f"- Of those {wrong} mistakes, the independent judge **caught {caught}** "
             f"(**{summary['catch_rate_pct']}%**) — agent told to HOLD, **free** (a miss isn't charged).")
    L.append(f"- It waved **{missed}** wrong answers through "
             f"(**false-confirm {summary['false_confirm_rate_pct']}%** — the case we're NOT hiding: "
             f"you'd pay and act on a bug).")
    L.append(f"- On correct answers it false-alarmed **{false_alarm}/{correct}** "
             f"(**{summary['false_alarm_rate_pct']}%**) — a wasted redo, but free.")
    L.append(f"- **When you ARE charged, the answer was right {summary['charged_precision_pct']}% "
             f"of the time** (precision over the {charged} charged decisions).")
    L.append(f"\n**Economics:** fee ${fee} per charged (judge-passed) decision. This run charged "
             f"${summary['economics']['fees_charged_usd']} total and delivered **{caught} caught "
             f"mistakes for free** — downside on a catch is **$0** by construction (a miss never settles).")
    L.append(f"\n_Reproduce: `python proof_harness.py {N}` — same seed, same items._")
    md = "\n".join(L)

    with open("proof_results.json", "w") as f:
        json.dump({"summary": summary, "rows": rows}, f, indent=2)
    with open("PROOF.md", "w") as f:
        f.write(md + "\n")

    print("\n" + md)
    print(f"\n[wrote proof_results.json + PROOF.md · {n} items · {dt:.0f}s]")


if __name__ == "__main__":
    main()
