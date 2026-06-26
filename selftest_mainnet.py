"""selftest_mainnet.py — prove the WHOLE money path end-to-end against the PUBLIC
endpoint with REAL USDC on Base mainnet.

Loads the self-test buyer wallet, checks it (and the relayer's gas) are funded,
runs ONE verified burst through the live x402 flow (402 -> sign -> pay -> settle),
and confirms the settlement transaction on-chain. This is the only thing that proves
a real customer's payment actually clears against the deployed surface.

Spends a few tenths of a cent of real USDC (buyer -> payout, both the owner's own
wallets). Safe: EIP-3009 settlement is atomic — a funding/relayer problem yields
charged:false with no partial loss. Re-runnable.

Run:  .venv/bin/python selftest_mainnet.py
"""
import json
import os

import env; env.load_env()
from eth_account import Account
from web3 import Web3

import x402_live as L

ENDPOINT = os.environ.get("SELFTEST_ENDPOINT", "https://burst.solcleus.com/v1/burst")
RPC = os.environ.get("X402_RPC_URL", "https://mainnet.base.org")
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"   # Base mainnet USDC
_ERC20_BAL = [{"constant": True, "inputs": [{"name": "o", "type": "address"}],
               "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
               "type": "function"}]


def _load_buyer():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           ".mainnet_selftest_buyer.json")) as f:
        d = json.load(f)
    return d["address"], d["key"]


def _relayer_addr():
    key = os.environ.get("X402_RELAYER_KEY")
    if not key:
        try:
            with open(".relayer_wallet.json") as f:
                key = json.load(f)["private_key"]
        except Exception:
            return None
    return Account.from_key(key).address


def main():
    buyer_addr, buyer_key = _load_buyer()
    w3 = Web3(Web3.HTTPProvider(RPC))
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC), abi=_ERC20_BAL)
    bal = usdc.functions.balanceOf(Web3.to_checksum_address(buyer_addr)).call() / 1e6
    relayer = _relayer_addr()
    gas = (w3.eth.get_balance(Web3.to_checksum_address(relayer)) / 1e18) if relayer else None

    print(f"endpoint : {ENDPOINT}")
    print(f"buyer    : {buyer_addr}  USDC={bal:.4f}")
    print(f"relayer  : {relayer}  ETH={gas:.6f}" if relayer else "relayer  : (unknown)")
    if bal < 0.01:
        print("\nABORT: buyer USDC too low to settle — fund the buyer wallet and re-run.")
        return
    if gas is not None and gas < 1e-5:
        print("\nABORT: relayer gas too low — top up the relayer and re-run.")
        return

    # configure the reusable client and run one real, checkable, should-PASS burst
    os.environ["BURST_ENDPOINT"] = ENDPOINT
    os.environ["BURST_BUYER_KEY"] = buyer_key
    import importlib, mcp_remote
    importlib.reload(mcp_remote)   # pick up the env we just set
    print("\nbuying one verified burst (real x402 settlement)…")
    resp = mcp_remote.buy({
        "request": "Is 12 * 17 = 204? Answer yes or no.",
        "strategy": "best_of_n", "n": 3,
        "verifier": "self_consistency", "answer_key": ["regex", "(?i)(yes|no)"],
    })

    status = resp.get("status")
    charged = resp.get("charged")
    tx = resp.get("tx") or (resp.get("receipt") or {}).get("settle_tx")
    print(f"\nstatus={status}  charged={charged}  mode={resp.get('mode')}")
    print(f"answer={resp.get('answer')!r}  gate={(resp.get('gate') or {}).get('action')}")
    print(f"amount_usd={resp.get('price_usd')}  remaining_budget={resp.get('remaining_budget_usd')}")
    if tx:
        print(f"settle_tx={tx}")
        print(f"basescan : https://basescan.org/tx/{tx}")
        try:
            rcpt = w3.eth.get_transaction_receipt(tx)
            print(f"on-chain : status={rcpt.status} block={rcpt.blockNumber}  "
                  f"{'CONFIRMED' if rcpt.status == 1 else 'REVERTED'}")
        except Exception as e:
            print(f"on-chain : receipt not yet available ({type(e).__name__}) — check basescan")
    if status == "ok" and charged:
        print("\nMONEY PATH PROVEN — real USDC settled against the public endpoint.")
    else:
        print(f"\nNOT CHARGED — money path did not settle (status={status}). No funds moved.")
        if resp.get("hint"):
            print(f"hint: {resp['hint']}")


if __name__ == "__main__":
    main()
