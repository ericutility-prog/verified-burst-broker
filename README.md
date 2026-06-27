# Verified Burst

**Pay-per-correct inference for agents.** At a hard, irreversible, or low-confidence
decision, an agent escalates to fast silicon (Cerebras), samples best-of-N, runs the
answer through a verifier, and settles over **x402 only if it passes**. A non-verified
result costs nothing.

Live on **Base mainnet** with a **self-hosted facilitator** (no Coinbase dependency).
The buyer brings their own provider key (**BYOK**) — we sell routing, verification, and
settlement, never marked-up tokens.

- 🟢 Live endpoint: `https://burst.solcleus.com/v1/burst` (manifest at `GET /v1/info`)
- 📦 PyPI: [`pip install verified-burst`](https://pypi.org/project/verified-burst/)
- 🔎 Listed on [x402scan](https://www.x402scan.com/server/d09b513d-cefa-46de-a1c8-34189026c408), [402 Index](https://402index.io), and [Glama](https://glama.ai/mcp/servers/ericutility-prog/verified-burst-broker)

## Why

An agent can already sample itself more (its own best-of-N is correlated — it shares its
own blind spots). The one thing it *can't* self-supply is an **independent, zero-downside,
keepable verification**: a different model family checks the answer, you pay only if it
passes, and you keep the receipt. Because the downside is $0 by construction, an agent's
budget policy can auto-approve the spend without a human in the loop.

## Quickstart (MCP, one line)

```bash
pip install verified-burst
```

Add the server to your MCP client — the agent gains one tool, `buy_verified_burst`:

```json
{
  "mcpServers": {
    "verified-burst": {
      "command": "verified-burst",
      "env": {
        "BURST_BUYER_KEY": "0x<wallet-private-key-that-pays-per-call>",
        "BURST_PROVIDER_KEY": "csk-<your-cerebras-key>"
      }
    }
  }
}
```

- `BURST_BUYER_KEY` — the wallet that pays per verified burst (USDC on Base). **Required** for live settlement.
- `BURST_PROVIDER_KEY` — your Cerebras key; bursts run on **your** tokens (BYOK). Optional.
- `BURST_ENDPOINT` — defaults to the hosted broker; override to self-host.

The tool:

```
buy_verified_burst(request, strategy="best_of_n", n=3,
                   verifier="self_consistency", answer_key=None)
  -> { answer, verified, charged, receipt, settle_tx }
```

## How it works

```
request → escalate to fast silicon → best-of-N → verify → settle ONLY if passed
                                                              ↑ pay-only-if-verified
```

1. **402 challenge** — `GET /v1/burst` returns the x402 payment requirements (Base mainnet, USDC).
2. **Authorize** — the client signs an x402 (EIP-3009) authorization for the quoted price.
3. **Burst** — the answer is generated on the buyer's BYOK key and gated through the chosen verifier.
4. **Settle** — USDC is captured **only if the verifier passes**. A miss settles nothing.

Every response includes a **keepable receipt** (`verified`, `corrected`, `independent`,
generator/verifier model, `settle_tx`) so verified decisions compound into memory.

## Verifiers

| verifier | what it does | cost to you |
|---|---|---|
| `self_consistency` | best-of-N must agree | free (BYOK) |
| `judge` | an adversarial judge checks the answer | free (BYOK) |
| `independent_judge` | a **different model family** judges (decorrelated errors) | small |
| `independent_quorum` | k-of-M independent judges across vendors | small |

Independence is the moat: `independent_judge`/`independent_quorum` are judged on models
in a different family (and, where configured, a different vendor) than the generator, so
the check doesn't share the generator's blind spots.

## Pricing

A small **service fee** per burst (routing + verification + the pay-only-if-verified
guarantee), paid in USDC on Base — **only on a pass**. Per-burst $0.002 (fast) to
$0.0045 (independent-judge). Generation tokens are billed to the buyer's own key; we
never mark up tokens. Hard per-wallet spend cap; abuse breakers on the judge path.

## Proof

This is live and settling real money. End-to-end mainnet settlement proof on BaseScan:
[`0x76921c33…d359c4`](https://basescan.org/tx/0x76921c33f6c83e78757b8218c101ac362ab19ba994ded6743b9bbb2defd359c4).

A reproducible catch-rate harness ([`proof_harness.py`](proof_harness.py) → `PROOF.md`)
generates code-checkable items and runs the real product path. At N=120 the base model
was 86.7% accurate; the independent judge caught **16/16** mistakes with **0 false-confirms**
(never charged for a wrong answer) and a 3.8% false-alarm rate (free redo).

> **Honest caveat:** that result is the *checkable-answer* regime (arithmetic, counting,
> labels) — the verifier's strongest suit and the product's stated sweet spot. It does
> **not** prove catch rate on fuzzy/subjective decisions, and 16 mistakes is a modest
> denominator. Claims are kept to what's measured.

## HTTP API

| route | purpose |
|---|---|
| `GET /v1/info` | discovery manifest (capabilities, pricing, verifier enum) |
| `GET /v1/burst` | x402 challenge for the paid resource (also advertised in `WWW-Authenticate` / `PAYMENT-REQUIRED` headers) |
| `POST /v1/burst` | buy a verified burst (send `X-PAYMENT`; BYOK via `X-Provider-Key`) |
| `GET /v1/quote` | price a burst without buying |
| `GET /healthz` | liveness |

## Self-hosting

The broker is stdlib-Python plus the live-payment deps in `requirements-live.txt`.
Run `PORT=8402 python3 server.py` behind a TLS proxy; set `X402_MODE=live` with a relayer
wallet and `X402_PAY_TO`. The MCP stdio server is `mcp_server.py` (sim mode needs no
secrets — it starts and introspects out of the box).

---

Built honestly: only what's measured is claimed. Questions or integrations welcome.
