"""Self-test for x402_live.py — REAL signing + REAL facilitator calls.

Run with the venv interpreter:
    /root/inference-burst/.venv/bin/python test_x402_live.py

It is EXPECTED that verify/settle may fail with an insufficient-funds /
invalid-balance reason, because the throwaway buyer wallet holds no Base Sepolia
testnet USDC yet (faucet funding is a separate manual step). The point of this
test is to prove the SIGNING + FACILITATOR-CALL PLUMBING is correct and to
capture the EXACT facilitator response. We do NOT fake success.
"""
import json
from pathlib import Path

from x402.http import DEFAULT_FACILITATOR_URL

import x402_live

WALLETS = Path(__file__).with_name(".testnet_wallets.json")


def _show(label, obj):
    print(f"\n--- {label} ---")
    print(json.dumps(obj, indent=2, default=str))


def main():
    wallets = json.loads(WALLETS.read_text())
    buyer = wallets["buyer"]
    seller = wallets["seller"]
    print(f"facilitator   : {DEFAULT_FACILITATOR_URL}")
    print(f"buyer  address: {buyer['address']}")
    print(f"seller address: {seller['address']}")

    fac = x402_live.LiveFacilitator()

    # 0) confirm the facilitator advertises exact / Base Sepolia
    supported = fac.get_supported()
    kinds = [
        {"version": k.x402_version, "scheme": k.scheme, "network": str(k.network)}
        for k in supported.kinds
    ]
    base_sepolia_supported = any(
        k["scheme"] == "exact" and k["network"] == x402_live.BASE_SEPOLIA for k in kinds
    )
    _show("facilitator get_supported kinds", kinds)
    print(f"Base Sepolia 'exact' supported: {base_sepolia_supported}")

    # 1) build requirements for $0.004 -> seller
    requirements, accepts = x402_live.build_requirements_v2("$0.004", seller["address"])
    _show("PaymentRequirements (v2)", requirements.model_dump(by_alias=True, exclude_none=True))
    print(f"resolved asset  : {requirements.asset}")
    print(f"atomic amount   : {requirements.amount}  (expect '4000' for $0.004 USDC)")

    # 2) buyer signs the EIP-3009 authorization
    payload, x_payment = x402_live.sign_payment(requirements, buyer["key"])
    _show("signed PaymentPayload (v2)", payload.model_dump(by_alias=True, exclude_none=True))
    print(f"X-PAYMENT header (base64, {len(x_payment)} chars): {x_payment[:64]}...")

    # 3) REAL verify
    verify_result = fac.verify(x_payment, requirements)
    _show("LiveFacilitator.verify -> REAL result", verify_result)

    # 4) REAL settle (attempt regardless — capture the real response)
    settle_result = fac.settle(x_payment, requirements)
    _show("LiveFacilitator.settle -> REAL result", settle_result)

    print("\n=== SUMMARY ===")
    print(f"signing produced a signature : "
          f"{bool(payload.payload.get('signature'))}")
    print(f"verify.valid   : {verify_result['valid']}  reason={verify_result['reason']!r}")
    print(f"settle.success : {settle_result['success']}  "
          f"tx={settle_result['tx']!r} reason={settle_result.get('reason')!r}")


if __name__ == "__main__":
    main()
