"""
bestprice.py — a paid, real-time "best price right now" search on the broker's
PROVEN x402 money-path. One micropayment buys one broad best-price lookup.

Reuses the exact sign -> verify -> settle flow that already settles real USDC on
Base mainnet (broker._gate / fac.verify / fac.settle), swapping the fulfillment
from "run a burst" to "run a best-price search" against AgentsPrice.

Honest charging (the analog of pay-only-if-verified): we settle ONLY if the
search returns real results. An empty/failed search is NOT charged — the buyer
never pays for "no info" (matches the honest-data rule across the stack).
"""
import os
import json
import urllib.request
import urllib.parse

import pricing
import broker
import ledger

AGENTSPRICE_BASE = os.environ.get("AGENTSPRICE_BASE", "https://agentsprice.com").rstrip("/")
AGENTSPRICE_KEY = os.environ.get("AGENTSPRICE_KEY", "")   # optional: real keyed best-price
_UA = "verified-bestprice/0.1"


def _get(url, key=None):
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def _search(query):
    """Return {query, deals[], source}. Prefer the real keyed best-price; fall
    back to the free warmed board filtered by query (still REAL data, clearly
    labelled). Never fabricates — an empty result stays empty (and isn't charged)."""
    q = (query or "").strip()
    if AGENTSPRICE_KEY:
        try:
            d = _get(f"{AGENTSPRICE_BASE}/api/best-price?q={urllib.parse.quote(q)}", AGENTSPRICE_KEY)
            deals = d.get("deals") or d.get("results") or []
            if deals:
                return {"query": q, "deals": deals, "source": "AgentsPrice best-price (live)"}
        except Exception:
            pass
    # Free fallback: the warmed board, filtered to the query — real prices.
    board = _get(f"{AGENTSPRICE_BASE}/api/board")
    deals = [d for d in (board.get("deals") or [])
             if q.lower() in (str(d.get("name", "")) + " " + str(d.get("category", ""))).lower()]
    return {"query": q, "deals": deals, "source": "AgentsPrice board (free fallback)"}


def serve_search(query, *, x_payment=None, budget_cap=broker.DEFAULT_BUDGET_USD, facilitator=None):
    """quote -> verify payment -> SEARCH -> settle-IF-results. Mirrors broker.serve_burst.
    Status: payment_required | budget_exceeded | no_results(charged:false) | ok(charged:true)."""
    q = pricing.quote()   # flat per-search service fee (same governor as a burst)
    if facilitator is not None:
        fac, reqs = facilitator, broker.build_requirements(q)
        accepts = reqs["accepts"]
    else:
        fac, reqs, accepts = broker._gate(q)

    auth = fac.verify(x_payment, reqs)
    if not auth["valid"]:
        return {"status": "payment_required", "quote": q, "accepts": accepts,
                "reason": auth.get("reason")}
    payer = auth.get("payer", "unknown")

    # single-use payment: reject replay / concurrent fan-out of one authorization
    pay_key = broker._payment_key(x_payment)
    if pay_key is not None and not ledger.claim_nonce(pay_key):
        return {"status": "payment_already_used", "payer": payer,
                "hint": "this x402 authorization was already used — sign a fresh payment per search"}

    # governor: atomically HOLD the fee up front (check-and-reserve in one txn), same as
    # the burst path — so two concurrent searches from one wallet can't both clear the
    # check near the cap boundary and both settle. Released on any non-charge exit below.
    if not ledger.reserve(payer, q["price_usd"], budget_cap):
        return {"status": "budget_exceeded", "payer": payer,
                "remaining_usd": round(broker.remaining_budget(payer, budget_cap), 6),
                "price_usd": q["price_usd"]}

    # fulfill the search BEFORE settling — we only charge for real data
    try:
        result = _search(query)
    except Exception as e:
        ledger.release(payer, q["price_usd"])   # search blew up -> free the hold, no charge
        return {"status": "search_failed", "charged": False, "price_usd": 0.0,
                "query": query, "error": str(e), "payer": payer}

    deals = result.get("deals") or []
    if not deals:
        # honest: no info found -> no charge (authorization discarded)
        ledger.release(payer, q["price_usd"])   # nothing to sell -> free the hold
        return {"status": "no_results", "charged": False, "price_usd": 0.0,
                "query": query, "source": result.get("source"), "payer": payer,
                "remaining_budget_usd": round(broker.remaining_budget(payer, budget_cap), 6)}

    # only hand over results if the capture actually confirms on-chain
    try:
        s = fac.settle(x_payment, reqs)
    except Exception:
        s = {"success": False}
    if not s["success"]:
        ledger.release(payer, q["price_usd"])   # capture didn't confirm -> free the hold
        return {"status": "settle_failed", "charged": False, "price_usd": 0.0,
                "query": query, "payer": payer,
                "hint": ("payment capture did not confirm — results withheld and you were "
                         "NOT charged; retry with a fresh payment")}
    ledger.commit(payer, q["price_usd"])
    return {"status": "ok", "charged": True, "price_usd": q["price_usd"],
            "tx": s.get("tx"), "mode": s.get("mode"),
            "query": query, "result": result, "count": len(deals), "payer": payer,
            "remaining_budget_usd": round(broker.remaining_budget(payer, budget_cap), 6),
            "budget_cap_usd": budget_cap}
