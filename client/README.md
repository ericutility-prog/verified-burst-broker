# verified-burst

An MCP tool that lets your agent **buy verified inference at hard decisions and
pay only if the answer is correct.** At an irreversible/ambiguous/deadline fork,
your agent escalates to fast silicon, samples best-of-N, runs the answers through
a verifier, and pays a few tenths of a cent over x402 — **charged only if it
passes.** Non-verified results cost nothing. Budget-capped, BYOK.

## Install

```bash
pip install verified-burst
# or zero-install:  uvx verified-burst   (pipx run verified-burst)
```

## Add to your MCP client

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

Fund the wallet with a little USDC on **Base**. `BURST_PROVIDER_KEY` is optional
(BYOK — your tokens, your rate limit). `BURST_ENDPOINT` overrides the host
(default `https://solcleus.com/v1/burst`).

## The tool

`buy_verified_burst(request, strategy="best_of_n", n=3, verifier="self_consistency", answer_key=None, model=None)`

```
buy_verified_burst(
  request="Is this clause enforceable? yes/no",
  answer_key=["regex", "(yes|no)"]
)
```

Returns the verified answer + a receipt. **You are charged only when the verifier
passes.** Spend is capped per wallet server-side, so autonomous purchases are safe.

## How the money moves (no mystery)

1. The endpoint answers an unpaid call with **HTTP 402** + payment requirements.
2. This client signs an x402 (EIP-3009) authorization with your wallet and retries.
3. The host verifies the answer and **settles on-chain only if it passed** — your
   USDC moves directly to the provider; the host eats the gas. Self-hosted
   facilitator, no third party holding funds.

Service discovery: `GET https://solcleus.com/.well-known/x402`.
