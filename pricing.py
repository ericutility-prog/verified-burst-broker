"""What a verified burst costs to buy.

BYOK passthrough model (the legal-clean path): the customer's TOKENS are paid by
the customer's own Cerebras key — we do NOT mark up tokens. We charge a small
SERVICE fee for the thing that's actually ours: escalation/routing + the
verification gate + the burst guarantee. That fee is what flows over x402.

cost_basis (token cost) is carried only for transparency/reporting, never billed
to the buyer in passthrough mode.
"""

# >>> EXTENSION POINT (pricing): FEES is the seam for dynamic / margin-governed pricing
# (demand- or cost-based). Keep quote() the single source of the up-front, quotable price.
# All amounts in USD. These are micro-amounts — x402 per-call territory.
FEES = {
    "base_burst": 0.0020,      # per burst: routing + the guarantee
    "per_candidate": 0.0005,   # each extra best-of-N sample we run/compare
    "verify": 0.0010,          # the correctness gate (our moat)
    "verify_independent": 0.0015,  # independent judge: an extra call on a DIFFERENT
                                   # family, on OUR tokens — the one check an agent
                                   # can't self-supply. Priced just over self-verify.
}
USDC_DECIMALS = 6


def _verify_fee(verifier, judges=1):
    if verifier == "none":
        return 0.0
    if verifier == "independent_judge":
        return FEES["verify_independent"]
    if verifier == "independent_quorum":
        # k-of-M consensus: we run (and pay for) M independent judges.
        return FEES["verify_independent"] * max(1, judges)
    return FEES["verify"]


def quote(strategy="best_of_n", n=3, verifier="self_consistency", judges=1):
    """Price a burst UP FRONT (independent of token usage, so it's quotable pre-run).
    `judges` = how many independent judges run (M for a quorum; 1 otherwise)."""
    n = 1 if strategy == "fast" else max(2, n)
    vfee = _verify_fee(verifier, judges)
    price = round(FEES["base_burst"] + FEES["per_candidate"] * (n - 1) + vfee, 6)
    return {
        "price_usd": price,
        "price_usdc_base_units": int(round(price * 10 ** USDC_DECIMALS)),
        "breakdown": {
            "base_burst": FEES["base_burst"],
            "candidates": round(FEES["per_candidate"] * (n - 1), 6),
            "verify": vfee,
        },
        "model": "service-fee (BYOK: tokens billed to buyer's own key, not marked up)",
    }
