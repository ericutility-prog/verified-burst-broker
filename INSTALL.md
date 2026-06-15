# Verified Burst — MCP install (for agent builders)

Give your agent one tool: at a hard, irreversible, or low-confidence decision it
**buys more thinking** — escalates to fast silicon, samples best-of-N, gates the
answer through a verifier, and pays per call (stablecoin, x402) **only if the
answer passes**. No subscription, no human in the loop, budget-capped.

## 1. Prereqs

- **A funded wallet on Base** — holds a little USDC; pays per burst. (You only
  ever put in its *private key*; never a seed phrase.)
- **(Optional) your own Cerebras key** — bring it and bursts run on *your* tokens
  and rate limit (BYOK). Omit it and the host's key is used.
- **Python 3.10+.**

## 2. Install

```bash
pip install verified-burst
# or zero-install:  uvx verified-burst   (pipx run verified-burst)
```

## 3. One-line MCP config

Add to your MCP client (Claude Desktop / Cursor / your agent framework):

```json
{
  "mcpServers": {
    "verified-burst": {
      "command": "verified-burst",
      "env": {
        "BURST_BUYER_KEY": "0x<your Base wallet private key>",
        "BURST_PROVIDER_KEY": "csk-<your Cerebras key>"
      }
    }
  }
}
```

`BURST_PROVIDER_KEY` is optional (BYOK). `BURST_ENDPOINT` defaults to
`https://solcleus.com/v1/burst` — override it to point at your own host.

> No-install alternative: run the single file `mcp_remote.py` (needs
> `pip install "x402[evm]" eth-account web3` + `x402_live.py` alongside it) with
> `"command": "python3", "args": ["/path/to/mcp_remote.py"]`.

## 3. Use it

Your agent now has `buy_verified_burst(request, strategy, n, verifier, answer_key)`.

```
buy_verified_burst(
  request="Is this contract clause enforceable? yes/no",
  strategy="best_of_n", n=3, verifier="self_consistency",
  answer_key=["regex", "(yes|no)"]   # normalize answers for the agreement check
)
```

Returns the verified answer + a receipt. **You are charged only when the verifier
passes** — a non-verified result costs you nothing. Spend is capped per wallet
server-side, so you can safely enable autonomous purchases.

## How the money works

- The endpoint answers an unpaid call with **HTTP 402** + payment requirements.
- `mcp_remote.py` signs an x402 (EIP-3009) authorization with your wallet and retries.
- The host verifies the answer and **settles on-chain only if it passed** — your
  USDC moves directly to the provider; gas is the host's problem.
- BYOK means your tokens are billed to *your* Cerebras key; the host charges a
  flat service fee for routing + verification + settlement, not for tokens.
