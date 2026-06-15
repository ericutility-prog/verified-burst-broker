# Verified Burst — distribution pitch (drafts)

Ready-to-adapt copy for getting the MCP one-liner in front of agent builders.
Everything here is true today: live on Base mainnet, self-hosted facilitator,
x402 micropay, BYOK, one-line MCP install. Don't add claims we can't back (e.g.
user counts) — the honesty is the moat.

---

## Tagline options

- **Verified Burst — your agent buys more thinking, and pays only if it's right.**
- Pay-per-correct-answer inference for agents. x402 micropay, verified, one MCP line.
- The toll booth on agent cognition: escalate at hard forks, charged only if verified.

---

## Hero pitch (landing / README top)

**Your agent runs cheap by default. At the decisions that actually matter — the
irreversible, the ambiguous, the deadline call — it should buy more thinking.**

Verified Burst gives your agent one tool: `buy_verified_burst`. At a hard fork it
escalates to fast silicon, samples best-of-N, runs the answers through a verifier,
and pays per call over x402 stablecoin — **charged only if the answer passes.** A
non-verified result costs nothing. No subscription, no human in the loop, spend
capped per wallet so you can safely let the agent buy on its own.

- **Pay only if verified.** The verifier gates the charge. Risk-free purchase.
- **Fast.** Best-of-N runs concurrently on Cerebras — a 3-sample burst in ~0.24s.
- **BYOK.** Bring your own provider key; your tokens, your rate limit. We sell
  routing + verification + settlement, never your tokens.
- **Agent-native payments.** Per-call x402 micropayments — no accounts, no invoices.
- **One line to install.** Drop it into any MCP client.

---

## Install (the one-liner)

```json
{ "mcpServers": { "verified-burst": {
    "command": "python3", "args": ["/path/to/mcp_remote.py"],
    "env": { "BURST_BUYER_KEY": "0x<base wallet key>",
             "BURST_PROVIDER_KEY": "csk-<your cerebras key>" } } } }
```

Fund the wallet with a little USDC on Base. That's it — your agent can now buy
verified bursts. Full guide: INSTALL.md.

---

## Short post (Show HN / r/LocalLLaMA / MCP & agent communities)

**Show: Verified Burst — an MCP tool that lets your agent buy verified inference, pay only if correct**

Agents run cheap models by default, then quietly get the high-stakes call wrong.
I built a single MCP tool for exactly those moments: `buy_verified_burst`. At a
hard decision the agent escalates to fast silicon (Cerebras), samples best-of-N,
verifies the answer, and pays a few tenths of a cent over x402 — **only if it
passes a verifier.** Wrong/unverifiable answers cost nothing.

Design choices:
- **Pay-only-if-verified** — the verifier gates settlement, so purchase risk is ~0.
- **x402 micropayments** — per-call stablecoin on Base, no subscription/human.
- **BYOK** — you bring your own Cerebras key; I never resell tokens, just routing +
  verification + settlement. Throughput scales with you, not my key.
- **Self-hosted settlement** — no Coinbase/3rd-party facilitator in the loop.
- **Budget-capped** per wallet, so autonomous spend is safe to enable.

It's live on Base mainnet, one line to install, open about exactly how the money
moves. Endpoint + install: <link>. Honest status: it works end-to-end; I'm looking
for the first builders to put real agent workloads through it. Feedback welcome.

---

## x402 ecosystem directory entry

- **Name:** Verified Burst
- **Category:** Service / AI inference (x402-gated)
- **One-line:** Pay-per-correct-answer inference bursts for agents — escalate +
  best-of-N + verify, settled over x402, charged only if verified. BYOK, MCP one-liner.
- **Endpoint:** https://solcleus.com/v1/burst (GET /v1/quote for price)
- **Networks:** Base mainnet (USDC, scheme `exact`/EIP-3009)
- **Facilitator:** self-hosted
- **Client:** MCP server (`mcp_remote.py`) + raw HTTP

---

## The one-sentence "why now"

Agents can't sign up for monthly plans, but x402 lets them pay per call — so the
first thing they'll actually buy autonomously is **more thinking at the moments
that matter**, and the only version they can trust is one that charges them only
when it's verified.

---

## Talking points / FAQ

- **"Why would an agent pay instead of just re-prompting?"** Because at an
  irreversible fork, being right is worth a fraction of a cent and the verifier
  makes "right" checkable. Re-prompting cheaply is what it already does — this is
  for when that's not safe enough.
- **"Aren't you just reselling Cerebras?"** No — BYOK means the tokens bill to the
  buyer's own key. The fee is for routing + best-of-N + verification + settlement.
- **"What if the answer's wrong?"** You're not charged. Settlement is gated on the
  verifier passing.
- **"Custody?"** You hold your wallet; payments settle directly to the provider on
  chain. Self-hosted facilitator, no third party holding funds.
