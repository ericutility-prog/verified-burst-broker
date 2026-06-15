"""Client-side x402 signing — standalone (no seller code).

Given the `accepts` from a 402 challenge and the buyer's wallet key, produce the
base64 `X-PAYMENT` header value: a signed EIP-3009 'exact' authorization. Mirrors
the buyer half of the broker's x402_live, with none of the facilitator/seller bits.
"""
import base64
import json

from eth_account import Account
from x402.schemas import PaymentPayload, PaymentRequirements
from x402.mechanisms.evm.exact import ExactEvmClientScheme


def coerce_requirements(requirements) -> PaymentRequirements:
    """Accept a PaymentRequirements, a list, a dict, or a {'accepts': [...]} body."""
    if isinstance(requirements, PaymentRequirements):
        return requirements
    if isinstance(requirements, (list, tuple)):
        return coerce_requirements(requirements[0])
    if isinstance(requirements, dict):
        if "accepts" in requirements:
            return PaymentRequirements.model_validate(requirements["accepts"][0])
        return PaymentRequirements.model_validate(requirements)
    raise ValueError(f"unsupported requirements type: {type(requirements)!r}")


def sign_payment(requirements, account_key_hex: str) -> str:
    """Sign an EIP-3009 'exact' authorization; return the base64 X-PAYMENT value."""
    reqs = coerce_requirements(requirements)
    account = Account.from_key(account_key_hex)
    scheme = ExactEvmClientScheme(account)
    inner = scheme.create_payment_payload(reqs)
    payload = PaymentPayload(x402_version=2, payload=inner, accepted=reqs)
    body = payload.model_dump(by_alias=True, exclude_none=True)
    return base64.b64encode(json.dumps(body).encode()).decode()
