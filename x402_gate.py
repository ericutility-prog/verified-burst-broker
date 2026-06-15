"""x402 payment gate — with the pay-only-if-verified twist.

Standard x402 (HTTP 402) flow: server answers 402 with payment `accepts`; client
returns an `X-PAYMENT` header (a signed authorization, e.g. EIP-3009); server
VERIFIES it, does the work, then SETTLES (broadcasts) and returns X-PAYMENT-RESPONSE.

Our twist that makes it honest: we hold the verified authorization, run the burst,
and SETTLE only if the verifier passes. On failure we simply never settle — the
signed authorization is discarded, so the buyer is not charged. "Pay only if
verified-adequate" falls straight out of authorize-then-capture.

Facilitator has two modes:
  - REAL : set X402_FACILITATOR_URL (+ optional X402_API_KEY). verify/settle call it.
  - SIM  : no URL -> deterministic local stand-in so the whole flow runs today.
Settlement is keyed off the burst result by the caller (broker.py), never here.
"""
import base64
import json
import os
import urllib.request


def build_requirements(quote, *, pay_to=None, network=None, asset=None,
                       resource="/v1/burst", description="verified inference burst"):
    """The `accepts` entry returned in a 402 challenge (x402 'exact' scheme shape)."""
    return {
        "x402Version": 1,
        "accepts": [{
            "scheme": "exact",
            "network": network or os.environ.get("X402_NETWORK", "base-sepolia"),
            "maxAmountRequired": str(quote["price_usdc_base_units"]),
            "resource": resource,
            "description": description,
            "mimeType": "application/json",
            "payTo": pay_to or os.environ.get("X402_PAY_TO", "0xSELLER_WALLET_UNSET"),
            "asset": asset or os.environ.get("X402_USDC_ASSET", "0xUSDC_UNSET"),
            "maxTimeoutSeconds": 60,
            "extra": {"priceUsd": quote["price_usd"]},
        }],
    }


class Facilitator:
    def __init__(self, base_url=None, api_key=None):
        self.base_url = base_url or os.environ.get("X402_FACILITATOR_URL", "")
        self.api_key = api_key or os.environ.get("X402_API_KEY", "")
        self.sim = not self.base_url

    # -- internal -------------------------------------------------------------
    def _post(self, path, payload):
        req = urllib.request.Request(
            self.base_url.rstrip("/") + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}",
                     "User-Agent": "burst-broker/x402"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    @staticmethod
    def _decode(x_payment):
        try:
            return json.loads(base64.b64decode(x_payment))
        except Exception:
            try:
                return json.loads(x_payment)
            except Exception:
                return None

    # -- public ---------------------------------------------------------------
    def verify(self, x_payment, requirements):
        """Authorize (do NOT capture). Returns dict(valid, reason, payer, mode)."""
        if not x_payment:
            return {"valid": False, "reason": "no X-PAYMENT", "mode": "sim" if self.sim else "real"}
        if self.sim:
            p = self._decode(x_payment)
            ok = bool(p) and "from" in p
            return {"valid": ok, "reason": "sim-ok" if ok else "sim-bad-payload",
                    "payer": (p or {}).get("from", "sim-payer"), "mode": "sim"}
        r = self._post("/verify", {"paymentPayload": self._decode(x_payment),
                                    "paymentRequirements": requirements["accepts"][0]})
        return {"valid": bool(r.get("isValid")), "reason": r.get("invalidReason", ""),
                "payer": r.get("payer", ""), "mode": "real"}

    def settle(self, x_payment, requirements):
        """Capture / broadcast the held authorization. Call ONLY when verified-adequate."""
        if self.sim:
            p = self._decode(x_payment) or {}
            payer = p.get("from", "sim-payer")
            return {"success": True, "tx": "sim-tx-" + payer[-6:], "mode": "sim"}
        r = self._post("/settle", {"paymentPayload": self._decode(x_payment),
                                    "paymentRequirements": requirements["accepts"][0]})
        return {"success": bool(r.get("success")), "tx": r.get("transaction", ""),
                "mode": "real"}
