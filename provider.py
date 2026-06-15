"""OpenAI-compatible provider call (BYOK). Cerebras = the urgent/fast tier.

Pure passthrough: we never store the key, never log prompts/outputs. We sell
routing + verification + burst guarantee on TOP of the customer's tokens, not the
tokens themselves (BYOK = no resale TOS risk).
"""
import json
import os
import time
import urllib.request

import env
env.load_env()

CEREBRAS = {
    "base_url": "https://api.cerebras.ai/v1",
    "key_env": "CEREBRAS_API_KEY",
    "model": os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b"),
    # $ / 1M tokens — VERIFY at https://www.cerebras.ai/pricing before quoting.
    "price_in": 0.85,
    "price_out": 1.20,
    "price_verified": False,
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
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    dt = time.monotonic() - t0
    msg = data["choices"][0]["message"]
    # reasoning models (gpt-oss) may put text in reasoning_content / leave content null
    text = msg.get("content") or msg.get("reasoning_content") or ""
    return {"text": text, "usage": data.get("usage", {}), "latency_s": dt}


def token_cost(usage, tier=CEREBRAS):
    return (usage.get("prompt_tokens", 0) / 1e6 * tier["price_in"]
            + usage.get("completion_tokens", 0) / 1e6 * tier["price_out"])
