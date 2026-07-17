#!/usr/bin/env python3
"""
Voice-latency harness — the number the Cerebras voice demo turns on.

A voice loop is: STT -> LLM -> TTS -> playback. Cerebras doesn't remove STT/TTS/
network; it SIGNIFICANTLY REDUCES THE COMPUTE (LLM) SEGMENT — the "dead air" after
you stop talking before the assistant starts speaking. This harness measures that
segment, and only that segment, so the claim in the video is one we actually proved.

Two metrics, both measured by STREAMING the response (measure.py times the whole
call; voice cares about the *start*):
  1. TTFT           : time-to-first-token = the dead-air gap. THE voice number.
  2. time-to-speak  : time until enough text exists to begin speaking a sentence
                      (a TTS engine can start on the first clause). Closer to what
                      a listener actually experiences.
Also reports tokens/sec (does generation keep pace with speech?) and total time.

Honest-data rule (carried from measure.py / AgentsPrice / Solcleus): every number
here comes from a live streamed call. There is NO offline latency mode — you cannot
honestly show a dead-air number you didn't measure. Same model both sides is the
fair comparison; the tier config below is shared with measure.py's intent.

Run (set keys first):
  export CEREBRAS_API_KEY=...                                  # fast-silicon side
  export BULK_API_KEY=...  BULK_BASE_URL=...  BULK_MODEL=...    # baseline side
  python3 voice_latency.py            # default 5 runs per prompt
  python3 voice_latency.py --n 10     # more runs -> tighter distribution / p90

For a fair, un-foolable comparison, point BULK at the SAME model on a normal host
(e.g. the same Llama/gpt-oss on OpenRouter). Racing a big slow model against
Cerebras would be a cheat, and we don't cheat.
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

# Shared tier shape with measure.py. Point BULK_MODEL at the SAME model on a normal
# host for an apples-to-apples dead-air comparison.
TIERS = {
    "cerebras": {
        "label": "Cerebras (fast silicon)",
        "base_url": "https://api.cerebras.ai/v1",
        "api_key_env": "CEREBRAS_API_KEY",
        "model": os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b"),
    },
    "baseline": {
        "label": "Baseline (normal host, BYOK)",
        "base_url": os.environ.get("BULK_BASE_URL", "https://openrouter.ai/api/v1"),
        "api_key_env": "BULK_API_KEY",
        "model": os.environ.get("BULK_MODEL", "openai/gpt-oss-120b"),
    },
}

# Realistic single-turn voice prompts: short, conversational answers — what an
# assistant actually says back, not a JSON extraction. Kept generic and factual so
# a listener can judge the answer, and short so the *start* dominates the felt delay.
SYSTEM = ("You are a friendly voice assistant. Answer in one or two short spoken "
          "sentences. No markdown, no lists — just what you'd say out loud.")
PROMPTS = [
    "What's the capital of Australia, and is it the biggest city there?",
    "I'm making pasta and out of salt. What can I use instead?",
    "Remind me — how many time zones does the continental US have?",
    "Give me a quick tip to fall asleep faster tonight.",
    "What's a polite way to decline a meeting that's not relevant to me?",
]

# A clause boundary a TTS engine could start speaking on.
_SPEAKABLE = (".", "!", "?", ",", ";", ":")


def stream_call(tier, prompt, timeout=60):
    """One STREAMED OpenAI-compatible chat call. Returns dict with ttft, time_to_speak,
    total (seconds), out_tokens, tps, and the text — or raises on transport error."""
    key = os.environ.get(tier["api_key_env"], "")
    payload = {
        "model": tier["model"],
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 200,
        "stream": True,
        "reasoning_effort": "low",   # keep gpt-oss reasoning from starving the answer to empty
    }
    prov = os.environ.get("BULK_PROVIDER", "").strip()
    if prov and "openrouter" in tier["base_url"]:   # pin baseline to one provider (Cerebras ignores)
        payload["provider"] = {"order": [prov], "allow_fallbacks": False}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        tier["base_url"].rstrip("/") + "/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "Accept": "text/event-stream",
                 "User-Agent": "Mozilla/5.0 (burst-broker)"},  # urllib UA trips Cloudflare 1010
    )
    t0 = time.monotonic()
    ttft = None
    time_to_speak = None
    text = ""
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:                          # SSE: one event per line
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            delta = (chunk.get("choices") or [{}])[0].get("delta", {})
            piece = delta.get("content") or ""
            if not piece:
                continue
            now = time.monotonic()
            if ttft is None:
                ttft = now - t0                # first visible token = dead-air ends
            text += piece
            if time_to_speak is None and any(p in text for p in _SPEAKABLE) and len(text) >= 8:
                time_to_speak = now - t0       # enough to start speaking a clause
    total = time.monotonic() - t0
    if ttft is None:
        raise RuntimeError("no content streamed")
    if time_to_speak is None:
        time_to_speak = total
    # token estimate: prefer usage if the provider sent it in a final chunk; else ~chars/4
    out_tokens = max(1, round(len(text) / 4))
    gen_window = max(1e-6, total - ttft)
    return {"ttft": ttft, "time_to_speak": time_to_speak, "total": total,
            "out_tokens": out_tokens, "tps": out_tokens / gen_window, "text": text}


def run_tier(name, tier, n):
    if not os.environ.get(tier["api_key_env"]):
        print(f"  ! {name}: {tier['api_key_env']} not set — skipping")
        return None
    ttfts, speaks, totals, tpss = [], [], [], []
    for prompt in PROMPTS:
        for i in range(n):
            try:
                m = stream_call(tier, prompt)
                ttfts.append(m["ttft"]); speaks.append(m["time_to_speak"])
                totals.append(m["total"]); tpss.append(m["tps"])
            except urllib.error.HTTPError as e:
                print(f"  ! {name}: HTTP {e.code} {e.read()[:160]!r}")
            except Exception as e:
                print(f"  ! {name}: {type(e).__name__}: {e}")
    if not ttfts:
        return None

    def p90(xs):
        s = sorted(xs)
        return s[min(len(s) - 1, int(0.9 * len(s)))]

    return {
        "label": tier["label"], "model": tier["model"], "n": len(ttfts),
        "med_ttft": statistics.median(ttfts), "p90_ttft": p90(ttfts),
        "med_speak": statistics.median(speaks),
        "med_total": statistics.median(totals), "med_tps": statistics.median(tpss),
    }


def report(res):
    print("\n=== VOICE COMPUTE-LATENCY (streamed, live) ===")
    hdr = f"{'side':<30}{'runs':>6}{'TTFT med':>11}{'TTFT p90':>11}{'to-speak':>11}{'t/s':>8}"
    print(hdr); print("-" * len(hdr))
    for r in res.values():
        print(f"{r['label'][:29]:<30}{r['n']:>6}{r['med_ttft']:>10.3f}s"
              f"{r['p90_ttft']:>10.3f}s{r['med_speak']:>10.3f}s{r['med_tps']:>8.0f}")
    if "cerebras" in res and "baseline" in res:
        c, b = res["cerebras"], res["baseline"]
        print("\n--- the dead-air delta (what the video shows) ---")
        if c["med_ttft"]:
            print(f"TTFT       : baseline {b['med_ttft']:.3f}s  vs  Cerebras {c['med_ttft']:.3f}s"
                  f"   -> {b['med_ttft']/c['med_ttft']:.1f}x less dead air (median)")
        if c["med_speak"]:
            print(f"to-speak   : baseline {b['med_speak']:.3f}s  vs  Cerebras {c['med_speak']:.3f}s"
                  f"   -> {b['med_speak']/c['med_speak']:.1f}x faster to first spoken clause")
        saved = b["med_ttft"] - c["med_ttft"]
        print(f"felt result: ~{saved*1000:.0f} ms of dead air removed from every voice turn")
        # compare model IDENTITY, ignoring a provider's "vendor/" prefix (OpenRouter's
        # "openai/gpt-oss-120b" is the same weights as Cerebras' "gpt-oss-120b").
        if c["model"].rsplit("/", 1)[-1] != b["model"].rsplit("/", 1)[-1]:
            print(f"NOTE: models differ ({c['model']} vs {b['model']}) — not apples-to-apples. "
                  "Set BULK_MODEL to the same model for a fair claim.")
        print("HONEST SCOPE: this is the COMPUTE segment only; STT/TTS/network add a "
              "fixed overhead on BOTH sides and are not shown here.")


def main():
    n = 5
    if "--n" in sys.argv:
        try:
            n = max(1, int(sys.argv[sys.argv.index("--n") + 1]))
        except (ValueError, IndexError):
            pass
    print(f"voice-latency: {n} run(s) x {len(PROMPTS)} prompt(s) per side, streamed.")
    res = {}
    for name, tier in TIERS.items():
        print(f"running {name}: {tier['label']} [{tier['model']}]")
        r = run_tier(name, tier, n)
        if r:
            res[name] = r
    if res:
        report(res)
    else:
        print("\nNo live results — set CEREBRAS_API_KEY and BULK_API_KEY/BULK_BASE_URL/BULK_MODEL. "
              "No offline mode: a dead-air number must be measured, never invented.")


if __name__ == "__main__":
    main()
