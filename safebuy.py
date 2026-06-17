"""
safebuy.py — the SAFE STARTER agent: find genuinely-good deals, verify they're
real, explain in plain words. It NEVER buys anything. Zero spend = zero risk.

This is Slice 1's spine (find -> verify -> explain), minus the buy step. It's the
"extremely safe starter wow": a non-coder can watch it surface a mispriced deal
and PROVE it's legit, with no way for it to spend a cent.

Flow:
  1. FIND   — pull candidate deals from AgentsPrice /api/anomalies (free, no key).
              The detector casts a wide net (it over-flags on purpose).
  2. VERIFY — for each candidate, run a best-of-N self-consistency burst on the
              host key: is this real | mismatch | error | scam? The verifier is
              exactly what filters the detector's noise down to real deals.
  3. EXPLAIN— plain-language summary a non-technical person can trust. No jargon.
              No buy is executed; a buy_url is shown for the human to tap if THEY
              choose. Money never moves here.

Run:  .venv/bin/python safebuy.py            (prints verified deals)
Import: from safebuy import find_verified_deals
"""
import json
import urllib.request

import env; env.load_env()
import provider
import burst

ANOMALIES_URL = "https://agentsprice.com/api/anomalies?category=all"

_VERIFY_PROMPT = (
    "A shopping assistant flagged this listing as a possible deal:\n"
    "  product: {name}\n"
    "  price: ${price}\n"
    "  seller: {seller}\n"
    "  category: {category}\n"
    "  why it was flagged: {why}\n\n"
    "Judge it for a careful, non-technical shopper. Is this most likely a REAL "
    "genuine deal on the product as described, or a problem? Consider: a wrong/"
    "mismatched product, a price/data error, or a scam.\n"
    "Reply with ONE word only: real, mismatch, error, or scam."
)


def _call_fn(msgs, temperature=0.0):
    # Host key, roomier budget so the model doesn't truncate to empty on harder calls.
    return provider.chat(msgs, temperature=temperature, max_tokens=512)


def _fetch_candidates(limit):
    req = urllib.request.Request(ANOMALIES_URL, headers={"User-Agent": "safebuy/0.1"})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.load(r)
    return (data.get("candidates") or [])[:limit]


def verify_deal(deal):
    """Best-of-N self-consistency on the real/mismatch/error/scam label."""
    why = "; ".join(s.get("detail", "") for s in (deal.get("anomaly") or {}).get("signals", []))
    prompt = _VERIFY_PROMPT.format(
        name=deal.get("name"), price=deal.get("best_price"),
        seller=deal.get("best_seller") or deal.get("source"),
        category=deal.get("category"), why=why or "lower than usual")
    res = burst.run_burst(prompt, strategy="best_of_n", n=3, verifier="self_consistency",
                          answer_key=("regex", r"(?i)\b(real|mismatch|error|scam)\b"),
                          call_fn=_call_fn, receipt_id="safebuy")
    return {"verdict": (res.answer or "").strip().lower(),
            "agreed": bool(res.passed),
            "agreement": (res.verdict or {}).get("agreement")}


def _plain_summary(deal, v):
    name = deal.get("name"); price = deal.get("best_price")
    seller = deal.get("best_seller") or deal.get("source")
    if v["verdict"] == "real" and v["agreed"]:
        return (f"I found {name} for ${price} at {seller}. I double-checked it and it "
                f"looks like a genuine deal on the real product. I did NOT buy anything — "
                f"tap to buy yourself if you want it.")
    reason = {"mismatch": "it may not be the product it claims to be",
              "error": "the price looks like a data error",
              "scam": "it has signs of a scam"}.get(v["verdict"], "I couldn't confirm it")
    return (f"I found {name} listed for ${price} at {seller}, but I'm NOT confident — "
            f"{reason}, so I'd hold off. I did not buy anything.")


def find_verified_deals(limit=5):
    """Return scored, explained deals. Read-only: nothing is ever purchased."""
    out = []
    for d in _fetch_candidates(limit):
        try:
            v = verify_deal(d)
        except Exception as e:
            v = {"verdict": "unsure", "agreed": False, "agreement": None, "error": str(e)}
        out.append({
            "name": d.get("name"), "price": d.get("best_price"),
            "seller": d.get("best_seller") or d.get("source"),
            "category": d.get("category"), "buy_url": d.get("buy_url"),
            "verdict": v["verdict"], "verified_real": v["verdict"] == "real" and v["agreed"],
            "summary": _plain_summary(d, v),
            "bought": False,  # ALWAYS — the safe starter never buys
        })
    return out


if __name__ == "__main__":
    deals = find_verified_deals(limit=6)
    real = [d for d in deals if d["verified_real"]]
    print(f"\nScanned {len(deals)} candidates — {len(real)} verified as real deals.\n")
    for d in deals:
        mark = "✅ REAL DEAL" if d["verified_real"] else f"⚠️  HOLD ({d['verdict']})"
        print(f"{mark}: {d['name']} — ${d['price']} @ {d['seller']}")
        print(f"    {d['summary']}\n")
