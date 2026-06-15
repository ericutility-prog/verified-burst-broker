"""OpenAI-compatible provider call (BYOK). Cerebras = the urgent/fast tier.

Pure passthrough: we never store the key, never log prompts/outputs. We sell
routing + verification + burst guarantee on TOP of the customer's tokens, not the
tokens themselves (BYOK = no resale TOS risk).
"""
import json
import os
import random
import time
import urllib.error
import urllib.request

import env
env.load_env()

# Transient statuses worth retrying: rate-limit + gateway/overload.
_RETRY_STATUS = {429, 500, 502, 503, 529}
_MAX_RETRIES = int(os.environ.get("CEREBRAS_MAX_RETRIES", "4"))

CEREBRAS = {
    "base_url": "https://api.cerebras.ai/v1",
    "key_env": "CEREBRAS_API_KEY",
    "model": os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b"),
    # $ / 1M tokens — gpt-oss-120b on Cerebras, verified May 2026 (artificialanalysis/pricepertoken).
    "price_in": 0.35,
    "price_out": 0.75,
    "price_verified": True,
}


def chat(messages, *, tier=CEREBRAS, temperature=0.0, max_tokens=256, timeout=60):
    """One chat call. Returns dict(text, usage, latency_s). Raises on transport error."""
    key = os.environ.get(tier["key_env"], "")
    if not key:
        raise RuntimeError(f"{tier['key_env']} not set (BYOK)")
    body = json.dumps({
        "model": tier["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(
        tier["base_url"].rstrip("/") + "/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (burst-broker)"},  # urllib UA trips Cloudflare 1010
    )
    # Retry transient rate-limit/overload with exponential backoff + jitter so a
    # single 429 never sinks a burst. Honors Retry-After when the server sends it.
    last_exc = None
    for attempt in range(_MAX_RETRIES + 1):
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
            dt = time.monotonic() - t0
            break
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code in _RETRY_STATUS and attempt < _MAX_RETRIES:
                ra = e.headers.get("Retry-After") if e.headers else None
                wait = float(ra) if (ra and str(ra).replace(".", "", 1).isdigit()) \
                    else min(8.0, 0.4 * (2 ** attempt))
                time.sleep(wait + random.random() * 0.25)
                continue
            raise
        except urllib.error.URLError as e:  # transient network/timeout
            last_exc = e
            if attempt < _MAX_RETRIES:
                time.sleep(min(8.0, 0.4 * (2 ** attempt)) + random.random() * 0.25)
                continue
            raise
    else:  # pragma: no cover - loop always breaks or raises
        raise last_exc
    msg = data["choices"][0]["message"]
    # reasoning models (gpt-oss) may put text in reasoning_content / leave content null
    text = msg.get("content") or msg.get("reasoning_content") or ""
    return {"text": text, "usage": data.get("usage", {}), "latency_s": dt}


def token_cost(usage, tier=CEREBRAS):
    return (usage.get("prompt_tokens", 0) / 1e6 * tier["price_in"]
            + usage.get("completion_tokens", 0) / 1e6 * tier["price_out"])
