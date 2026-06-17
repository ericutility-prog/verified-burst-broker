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
- **Try it with just a Base wallet — no API key. First 3 bursts on us.**

---

## Hero pitch (landing / README top)

**Your agent runs cheap by default. At the decisions that actually matter — the
irreversible, the ambiguous, the deadline call — it should buy more thinking.**

Verified Burst gives your agent one tool: `buy_verified_burst`. At a hard fork it
escalates to fast silicon, samples best-of-N, runs the answers through a verifier,
and pays per call over x402 stablecoin — **charged only if the answer passes.** A
non-verified result costs nothing. No subscription, no human in the loop, spend
capped per wallet so you can safely let the agent buy on its own.

- **No key to start.** Your first 3 bursts run on the host key — bring only a
  funded Base wallet. After that, BYOK. Lowest-friction way to try it.
- **Pay only if verified.** The verifier gates the charge. Risk-free purchase.
- **Fast.** Best-of-N runs concurrently on Cerebras — a 3-sample burst in ~0.24s.
- **BYOK.** Bring your own provider key; your tokens, your rate limit. We sell
  routing + verification + settlement, never your tokens.
- **Agent-native payments.** Per-call x402 micropayments — no accounts, no invoices.
- **One line to install.** Drop it into any MCP client.

---

## Install (the one-liner)

```bash
pip install verified-burst      # or zero-install: uvx verified-burst
```

```json
{ "mcpServers": { "verified-burst": {
    "command": "verified-burst",
    "env": { "BURST_BUYER_KEY": "0x<base wallet key>",
             "BURST_PROVIDER_KEY": "csk-<your cerebras key>" } } } }
```

Fund the wallet with a little USDC on Base. That's it — your agent can now buy
verified bursts. **`BURST_PROVIDER_KEY` is optional:** leave it out and your first
3 bursts run on the host key; after that, drop in your own Cerebras key (BYOK —
your tokens, your rate limit). Full guide: INSTALL.md.

---

## Short post (finalized — Show HN primary; adapt for forums/Discord)

**Live links (use these, all true today):**
- Landing/endpoint: https://burst.solcleus.com
- Install: https://pypi.org/project/verified-burst/  (`pip install verified-burst`)
- x402scan listing: https://www.x402scan.com/server/d09b513d-cefa-46de-a1c8-34189026c408
- Settlement proof (BaseScan): https://basescan.org/tx/0x76921c33f6c83e78757b8218c101ac362ab19ba994ded6743b9bbb2defd359c4

### Show HN

**Title:** `Show HN: Verified Burst – agents buy inference over x402, pay only if it's correct`
**URL:** `https://burst.solcleus.com`

**Text:** (lead with the no-key trial — lowest barrier to try)
You can try it with just a Base wallet and a few cents — **no API key of your own.**
Your first 3 bursts run on the host key; after that you bring your own (BYOK).

The idea: agents run a cheap model by default, then quietly get the high-stakes call
wrong. `buy_verified_burst` is one MCP tool for those moments. At a checkable decision
the agent samples best-of-N (on your own key), runs the answers through a verifier, and
gets back a **go/no-go gate** — `proceed` when the samples agree, `hold` when they don't,
so your agent holds and escalates instead of acting on a shaky answer. The few-tenths-of-
a-cent fee settles over x402 **only when it verifies**; a miss waives the fee (you still
pay your own provider tokens). Think of it less as "buy more thinking" and more as a
**circuit breaker for an agent's irreversible decisions.**

Use it when the agent is acting unattended, the call is consequential, and the answer is
checkable (a label, number, JSON field, or yes/no):

- **Your agent spends money.** Before it signs a swap or buys, verify the contract/SKU — `hold` stops it acting on a hallucinated or injected target, and the wallet can't be drained past what you fund. (All three rails fire here: gate + hard spend cap + on-chain receipt.)
- **Extraction at scale.** Only commit a field when N samples agree — a split routes the row to review instead of silently writing `$42,100` where the answer was `$4,210`.
- **Irreversible ops.** Before a migration / force-push / delete, gate on "is this safe and reversible?" — the agent holds instead of dropping a production table.

Not for open-ended prose or low-stakes steps — the verifier checks a checkable answer, not creative output.

- No key to start: first 3 bursts run on the host key, then BYOK (your own Cerebras key — your tokens, your rate limit).
- Pay-only-if-verified: the verifier gates the service fee (a miss is free; your own tokens still apply).
- Hard spend cap: the agent pays from a wallet you fund and can't spend past it — enforced on-chain, not honor-system.
- x402 micropayments: per-call USDC on Base, no subscription, no human in the loop.
- Self-hosted settlement: no third-party facilitator holding funds.

One line to install: `pip install verified-burst`, then drop it into any MCP client
with a Base wallet key. Live on Base mainnet, listed on x402scan, no outside users
yet — looking for first builders. Feedback (especially "this is the wrong
abstraction") very welcome.

### Forum / Discord / r/AI_Agents version

**Title:** `MCP tool: your agent buys a "verified inference burst" and pays only if the answer passes a verifier (no API key needed to start)`

Same body as above — keep the no-key-trial line first; it's the biggest friction
drop for a first user (just a Base wallet, no Cerebras key for the first 3 bursts).
NOTE: trial count is the `BURST_TRIAL_BURSTS` env value (3 today) — keep the copy in
sync if you change it.

**Where to post:** Show HN (news.ycombinator.com/submit) is the primary shot.
Then r/AI_Agents, the MCP community, and x402/Coinbase developer Discords. Skip
r/LocalLLaMA unless leading hard with BYOK — a hosted paid layer can get pushback
there. Post one at a time; reply fast to the first comments — that's what decides
whether a Show HN catches.

---

## x402 ecosystem directory entry

- **Name:** Verified Burst
- **Category:** Service / AI inference (x402-gated)
- **One-line:** Pay-per-correct-answer inference bursts for agents — escalate +
  best-of-N + verify, settled over x402, charged only if verified. No API key to
  start (first 3 bursts on the host key), then BYOK. MCP one-liner.
- **Endpoint:** https://burst.solcleus.com/v1/burst (GET /v1/quote for price)
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
