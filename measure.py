#!/usr/bin/env python3
"""
Burst-broker measurement harness — the 1-day BYOK experiment.

Thesis (see memory: inference-burst-broker): sell inference "in bursts, proven
correct." Two opposite supply chains:
  - URGENT  : fast silicon (Cerebras ~1,800-3,000 t/s)  -> latency-critical decisions
  - BULK    : cheap interruptible/commodity              -> throughput / batch

This harness measures, on ONE real gradeable agent workload, the three numbers
the thesis turns on:
  1. price spread   : $/task cheapest-vs-default
  2. quality gate   : how often the cheap output FAILS a deterministic check
  3. speed premium  : latency / tokens-per-sec the urgent tier buys you

Honest-data rule (carried from AgentsPrice/Solcleus): we NEVER report a number we
didn't measure. Latency & t/s come only from live calls. Prices come from a config
table you must verify against the provider's live pricing page (flagged VERIFY).
Offline mode runs the graders against canned outputs to prove the gate works, and
prints the cost model — it does NOT invent timings.

Run:
  # offline (no keys) — proves the graders + shows the cost model
  python3 measure.py --offline

  # live — set keys first
  export CEREBRAS_API_KEY=...                       # urgent tier
  export BULK_API_KEY=...  BULK_BASE_URL=...  BULK_MODEL=...   # bulk/default tier
  python3 measure.py
"""
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

import env
env.load_env()

# --------------------------------------------------------------------------- #
# Tier config. Prices are $ per 1M tokens (in, out). MUST be verified against the
# provider's live pricing page — marked VERIFY until you've checked today.
# --------------------------------------------------------------------------- #
TIERS = {
    "urgent": {
        "label": "Cerebras (urgent / fast silicon)",
        "base_url": "https://api.cerebras.ai/v1",
        "api_key_env": "CEREBRAS_API_KEY",
        "model": os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b"),
        # gpt-oss-120b on Cerebras, verified May 2026 (artificialanalysis / pricepertoken)
        "price_in": 0.35,
        "price_out": 0.75,
        "price_verified": True,
    },
    "bulk": {
        "label": "Bulk / default (BYOK, OpenAI-compatible)",
        "base_url": os.environ.get("BULK_BASE_URL", "https://openrouter.ai/api/v1"),
        "api_key_env": "BULK_API_KEY",
        "model": os.environ.get("BULK_MODEL", "meta-llama/llama-3.3-70b-instruct"),
        # VERIFY against whatever provider you point BULK_BASE_URL at.
        "price_in": 0.12,
        "price_out": 0.30,
        "price_verified": False,
    },
}

# --------------------------------------------------------------------------- #
# Workload: real decision-burst tasks, each with a DETERMINISTIC grader so
# "cheap output failed the quality check" is a fact, not a vibe.
# --------------------------------------------------------------------------- #
def _json_field(text, field, want):
    try:
        s = text[text.index("{"): text.rindex("}") + 1]
        return str(json.loads(s).get(field, "")).strip().lower() == str(want).lower()
    except Exception:
        return False

WORKLOAD = [
    {
        "id": "extract-amount",
        "prompt": 'Extract the total as JSON {"total": <number>} only. '
                  'Invoice: "3 units @ $19.99, shipping $5.00". No prose.',
        "grade": lambda t: _json_field(t, "total", "64.97"),
    },
    {
        "id": "classify-intent",
        "prompt": 'Classify intent as JSON {"intent": "refund"|"cancel"|"upgrade"} only. '
                  'Message: "I want my money back, this never worked."',
        "grade": lambda t: _json_field(t, "intent", "refund"),
    },
    {
        "id": "constraint-pick",
        # correct = C ($52, 5★): cheaper than B ($55, 4★) and still meets >=4★
        "prompt": 'A:$40/2★  B:$55/4★  C:$52/5★. Cheapest with >=4★. '
                  'Reply JSON {"choice":"A"|"B"|"C"} only.',
        "grade": lambda t: _json_field(t, "choice", "C"),
    },
    {
        "id": "arith-guard",
        "prompt": 'Reply JSON {"answer": <int>} only. If a cart has 7 items at $12 '
                  'and a $15 coupon, what is the total in dollars?',
        "grade": lambda t: _json_field(t, "answer", "69"),
    },
]

CANNED = {  # for --offline: proves graders pass good output and fail bad output
    "extract-amount": ('{"total": 64.97}', '{"total": 59.97}'),
    "classify-intent": ('{"intent": "refund"}', '{"intent": "cancel"}'),
    "constraint-pick": ('{"choice": "C"}', '{"choice": "B"}'),
    "arith-guard": ('{"answer": 69}', '{"answer": 84}'),
}

# --------------------------------------------------------------------------- #
def call(tier, prompt, timeout=60):
    """One OpenAI-compatible chat call. Returns (text, usage, latency_s)."""
    key = os.environ.get(tier["api_key_env"], "")
    body = json.dumps({
        "model": tier["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 256,
    }).encode()
    req = urllib.request.Request(
        tier["base_url"].rstrip("/") + "/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (burst-broker)"},  # urllib UA trips Cloudflare 1010
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    dt = time.monotonic() - t0
    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return text, usage, dt


def cost(tier, usage):
    pin = usage.get("prompt_tokens", 0) / 1e6 * tier["price_in"]
    pout = usage.get("completion_tokens", 0) / 1e6 * tier["price_out"]
    return pin + pout


def run_live(name, tier):
    if not os.environ.get(tier["api_key_env"]):
        print(f"  ! {name}: {tier['api_key_env']} not set — skipping live run")
        return None
    rows = []
    for task in WORKLOAD:
        try:
            text, usage, dt = call(tier, task["prompt"])
            out_tok = usage.get("completion_tokens", 0)
            rows.append({
                "id": task["id"],
                "passed": bool(task["grade"](text)),
                "latency": dt,
                "tps": (out_tok / dt) if dt > 0 else 0.0,
                "cost": cost(tier, usage),
            })
        except urllib.error.HTTPError as e:
            print(f"  ! {name}/{task['id']}: HTTP {e.code} {e.read()[:200]!r}")
        except Exception as e:
            print(f"  ! {name}/{task['id']}: {type(e).__name__}: {e}")
    if not rows:
        return None
    return {
        "label": tier["label"],
        "model": tier["model"],
        "n": len(rows),
        "pass_rate": sum(r["passed"] for r in rows) / len(rows),
        "med_latency": statistics.median(r["latency"] for r in rows),
        "med_tps": statistics.median(r["tps"] for r in rows),
        "avg_cost": statistics.mean(r["cost"] for r in rows),
        "verified": tier["price_verified"],
    }


def report(results):
    print("\n=== BURST MEASUREMENT ===")
    hdr = f"{'tier':<38}{'pass':>6}{'med lat':>10}{'med t/s':>10}{'$/task':>12}"
    print(hdr)
    print("-" * len(hdr))
    for r in results.values():
        flag = "" if r["verified"] else "  (price UNVERIFIED)"
        print(f"{r['label'][:37]:<38}{r['pass_rate']*100:>5.0f}%"
              f"{r['med_latency']:>9.2f}s{r['med_tps']:>10.0f}"
              f"${r['avg_cost']*1000:>9.3f}/k{flag}")
    if "urgent" in results and "bulk" in results:
        u, b = results["urgent"], results["bulk"]
        print("\n--- the three thesis numbers ---")
        if b["avg_cost"]:
            print(f"price spread   : urgent costs {u['avg_cost']/b['avg_cost']:.1f}x the bulk tier")
        if u["med_latency"]:
            print(f"speed premium  : urgent is {b['med_latency']/u['med_latency']:.1f}x faster "
                  f"({u['med_tps']:.0f} vs {b['med_tps']:.0f} t/s)")
        print(f"quality gate   : bulk passed {b['pass_rate']*100:.0f}% of the grader "
              f"(urgent {u['pass_rate']*100:.0f}%)  -> failure rate = where Solcleus verify earns its margin")


def offline():
    print("OFFLINE MODE — no API calls. Proving graders + showing cost model.\n"
          "(Latency/t-s are MEASURED ONLY in live mode; none invented here.)\n")
    print("Grader self-test (good output must pass, bad must fail):")
    ok = True
    for task in WORKLOAD:
        good, bad = CANNED[task["id"]]
        gp, bp = task["grade"](good), task["grade"](bad)
        ok &= gp and not bp
        print(f"  {task['id']:<16} good->{'PASS' if gp else 'FAIL'}  bad->{'PASS' if bp else 'FAIL'}"
              f"  {'ok' if (gp and not bp) else '!! GRADER BROKEN'}")
    print(f"\nGrader self-test: {'ALL OK' if ok else 'BROKEN — fix before trusting live results'}")
    print("\nCost model ($/1M tokens, VERIFY against live pricing before quoting):")
    for name, t in TIERS.items():
        print(f"  {name:<8} {t['model']:<34} in ${t['price_in']:<6} out ${t['price_out']:<6}"
              f"  base={t['base_url']}")
    print("\nTo run live: set CEREBRAS_API_KEY (urgent) and BULK_API_KEY/BULK_BASE_URL/BULK_MODEL (bulk),"
          " then run without --offline.")


def main():
    if "--offline" in sys.argv:
        offline()
        return
    results = {}
    for name, tier in TIERS.items():
        print(f"running {name}: {tier['label']} [{tier['model']}]")
        r = run_live(name, tier)
        if r:
            results[name] = r
    if results:
        report(results)
    else:
        print("\nNo live results (no keys set). Run with --offline to see the shape, "
              "or export the tier keys.")


if __name__ == "__main__":
    main()
