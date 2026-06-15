"""What a verified burst costs to buy.

BYOK passthrough model (the legal-clean path): the customer's TOKENS are paid by
the customer's own Cerebras key — we do NOT mark up tokens. We charge a small
SERVICE fee for the thing that's actually ours: escalation/routing + the
verification gate + the burst guarantee. That fee is what flows over x402.

cost_basis (token cost) is carried only for transparency/reporting, never billed
to the buyer in passthrough mode.
"""

# All amounts in USD. These are micro-amounts — x402 per-call territory.
FEES = {
    "base_burst": 0.0020,      # per burst: routing + the guarantee
    "per_candidate": 0.0005,   # each extra best-of-N sample we run/compare
    "verify": 0.0010,          # the correctness gate (our moat)
}
USDC_DECIMALS = 6


def quote(strategy="best_of_n", n=3, verifier="self_consistency"):
    """Price a burst UP FRONT (independent of token usage, so it's quotable pre-run)."""
    n = 1 if strategy == "fast" else max(2, n)
    price = FEES["base_burst"] + FEES["per_candidate"] * (n - 1)
    if verifier != "none":
        price += FEES["verify"]
    price = round(price, 6)
    return {
        "price_usd": price,
        "price_usdc_base_units": int(round(price * 10 ** USDC_DECIMALS)),
        "breakdown": {
            "base_burst": FEES["base_burst"],
            "candidates": round(FEES["per_candidate"] * (n - 1), 6),
            "verify": FEES["verify"] if verifier != "none" else 0.0,
        },
        "model": "service-fee (BYOK: tokens billed to buyer's own key, not marked up)",
    }
