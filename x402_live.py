"""x402 LIVE facilitator — real on-chain settlement (Base Sepolia testnet).

This is the SDK-backed twin of the SIM/REAL gate in x402_gate.py. It uses the
official `x402` Python SDK (v2, scheme "exact") to:

  - build v2 PaymentRequirements with the correct USDC asset + EIP-712 domain
    (asset is RESOLVED by the SDK from the network config, not hardcoded),
  - sign an EIP-3009 authorization with an eth_account key (buyer side),
  - verify and settle that authorization against the public facilitator
    (https://x402.org/facilitator) via FacilitatorClientSync.

The public surface (build_requirements / sign_payment / LiveFacilitator with
.verify/.settle returning plain dicts) mirrors x402_gate.py so broker.py can be
pointed at this instead of the sim with minimal wiring.

REQUIRES the venv interpreter (.venv/bin/python) — x402 + eth_account are not in
the system stdlib build the product code uses.
"""
from __future__ import annotations

import base64
import json

from eth_account import Account

from x402.http import (
    DEFAULT_FACILITATOR_URL,
    HTTPFacilitatorClientSync,
)
from x402.schemas import PaymentPayload, PaymentRequirements
from x402.mechanisms.evm.exact import ExactEvmClientScheme, ExactEvmServerScheme

# CAIP-2 id for Base Sepolia testnet (chain id 84532).
BASE_SEPOLIA = "eip155:84532"

# Reused server scheme: only used to RESOLVE the asset + EIP-712 domain from the
# SDK's network config (parse_price / enhance_payment_requirements). It needs no
# network/facilitator to do that resolution.
_SERVER_SCHEME = ExactEvmServerScheme()


def build_requirements_v2(
    price_usd,
    pay_to,
    *,
    network: str = BASE_SEPOLIA,
    resource: str = "/v1/burst",
    description: str = "verified inference burst",
):
    """Build a v2 PaymentRequirements for `price_usd` paid to `pay_to`.

    `price_usd` may be a dollar string ("$0.004"), a number (0.004), or a plain
    decimal string ("0.004"). The USDC asset address, decimals and EIP-712
    domain (name/version) are resolved BY THE SDK from its Base Sepolia network
    config — we do not hardcode them.

    Returns:
        (requirements, accepts) where:
          - requirements is the SDK PaymentRequirements (v2) object, and
          - accepts is a one-element list [requirements] (the `accepts` array
            shape used in a 402 challenge).
    """
    price = _normalize_price(price_usd)

    # parse_price -> AssetAmount: resolves the default USDC asset + decimals and
    # converts the dollar amount to atomic units (string).
    asset_amount = _SERVER_SCHEME.parse_price(price, network)

    requirements = PaymentRequirements(
        scheme="exact",
        network=network,
        asset=asset_amount.asset,
        amount=asset_amount.amount,
        pay_to=pay_to,
        max_timeout_seconds=300,
        extra={**(asset_amount.extra or {}), "resource": resource, "description": description},
    )

    # enhance: fills default asset if blank and injects EIP-712 domain
    # (name, version) into `extra` so the buyer's signer can build the domain
    # separator without a network round-trip.
    requirements = _SERVER_SCHEME.enhance_payment_requirements(requirements, _supported_kind(network), [])

    return requirements, [requirements]


def sign_payment(requirements: PaymentRequirements, account_key_hex: str):
    """Sign an EIP-3009 'exact' authorization for `requirements` with a key.

    Returns:
        (payload, x_payment) where:
          - payload is the SDK PaymentPayload (v2) object, and
          - x_payment is the base64-encoded JSON string suitable for the
            `X-PAYMENT` HTTP header.
    """
    account = Account.from_key(account_key_hex)
    scheme = ExactEvmClientScheme(account)  # auto-wraps the LocalAccount

    # Inner scheme payload (authorization + signature). x402Client would
    # normally wrap this; we wrap it ourselves into a full v2 PaymentPayload.
    inner = scheme.create_payment_payload(requirements)

    payload = PaymentPayload(
        x402_version=2,
        payload=inner,
        accepted=requirements,
    )

    x_payment = _encode_x_payment(payload)
    return payload, x_payment


class LiveFacilitator:
    """Real facilitator client mirroring x402_gate.Facilitator's interface.

    .verify(x_payment, requirements) -> {"valid", "reason", "payer"}
    .settle(x_payment, requirements) -> {"success", "tx"}

    `x_payment` may be a base64/JSON string (as it arrives in an X-PAYMENT
    header) or an already-decoded SDK PaymentPayload. `requirements` may be the
    SDK PaymentRequirements, the [requirements] list, or a dict.
    """

    def __init__(self, url: str = DEFAULT_FACILITATOR_URL, timeout: float = 30.0):
        self.url = url
        # HTTPFacilitatorClientSync is the concrete sync client;
        # FacilitatorClientSync (in x402.http) is only a Protocol.
        self._client = HTTPFacilitatorClientSync({"url": url})

    # -- public ---------------------------------------------------------------
    def get_supported(self):
        """Raw SupportedResponse from the facilitator (kinds/extensions/signers)."""
        return self._client.get_supported()

    def verify(self, x_payment, requirements) -> dict:
        if not x_payment:
            return {"valid": False, "reason": "no X-PAYMENT", "payer": ""}
        payload = _coerce_payload(x_payment)
        reqs = _coerce_requirements(requirements)
        try:
            r = self._client.verify(payload, reqs)
        except Exception as exc:  # network / facilitator-level failure
            return {"valid": False, "reason": f"{type(exc).__name__}: {exc}", "payer": ""}
        return {
            "valid": bool(r.is_valid),
            "reason": r.invalid_reason or r.invalid_message or ("" if r.is_valid else "invalid"),
            "payer": r.payer or "",
        }

    def settle(self, x_payment, requirements) -> dict:
        if not x_payment:
            return {"success": False, "tx": "", "reason": "no X-PAYMENT"}
        payload = _coerce_payload(x_payment)
        reqs = _coerce_requirements(requirements)
        try:
            r = self._client.settle(payload, reqs)
        except Exception as exc:
            return {"success": False, "tx": "", "reason": f"{type(exc).__name__}: {exc}"}
        return {
            "success": bool(r.success),
            "tx": r.transaction or "",
            "reason": r.error_reason or r.error_message or "",
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _normalize_price(price_usd):
    """Accept '$0.004', 0.004, or '0.004' -> a Money value parse_price accepts."""
    if isinstance(price_usd, (int, float)):
        return f"${price_usd}"
    s = str(price_usd).strip()
    if not s.startswith("$"):
        s = "$" + s
    return s


def _supported_kind(network: str):
    """A minimal SupportedKind for enhance_payment_requirements.

    enhance only reads scheme/network from it (to confirm the kind); the asset
    + EIP-712 domain come from the SDK network config.
    """
    from x402.schemas.responses import SupportedKind

    return SupportedKind(x402_version=2, scheme="exact", network=network)


def _encode_x_payment(payload: PaymentPayload) -> str:
    body = payload.model_dump(by_alias=True, exclude_none=True)
    return base64.b64encode(json.dumps(body).encode()).decode()


def _coerce_payload(x_payment) -> PaymentPayload:
    """Decode an X-PAYMENT string (base64 or raw JSON) or pass through an object."""
    if isinstance(x_payment, PaymentPayload):
        return x_payment
    if isinstance(x_payment, dict):
        return PaymentPayload.model_validate(x_payment)
    s = x_payment
    # try base64-of-JSON first, then raw JSON
    for decode in (lambda v: base64.b64decode(v), lambda v: v):
        try:
            data = json.loads(decode(s))
            return PaymentPayload.model_validate(data)
        except Exception:
            continue
    raise ValueError("could not decode X-PAYMENT into a PaymentPayload")


def _coerce_requirements(requirements) -> PaymentRequirements:
    if isinstance(requirements, PaymentRequirements):
        return requirements
    if isinstance(requirements, (list, tuple)):
        return _coerce_requirements(requirements[0])
    if isinstance(requirements, dict):
        # support the x402_gate.py shape {"accepts": [ {...} ]} too
        if "accepts" in requirements:
            return PaymentRequirements.model_validate(requirements["accepts"][0])
        return PaymentRequirements.model_validate(requirements)
    raise ValueError(f"unsupported requirements type: {type(requirements)!r}")
