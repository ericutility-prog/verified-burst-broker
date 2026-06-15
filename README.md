# verified-burst broker

Sell **verified inference bursts** on Cerebras. An agent hits a hard / irreversible /
low-confidence decision and **buys more thinking**: escalate to fast silicon →
best-of-N → verify → **pay per burst over x402, charged only if the answer passes**.
Budget-capped per agent.

BYOK passthrough: the customer's tokens are billed to their own Cerebras key — we
**never mark up tokens**. We charge a small service fee for routing + verification +
the burst guarantee. (Legal-clean: selling our application, not reselling their API.)

## The wedge — one line for an agent builder
```json
{ "mcpServers": { "verified-burst": {
    "command": "python3", "args": ["/root/inference-burst/mcp_server.py"] } } }
```
Their agent gains one tool, `buy_verified_burst(request, strategy, n, verifier, answer_key)`,
that returns a verified answer + receipt and bills per burst.

## Files
| file | role |
|---|---|
| `provider.py` | BYOK Cerebras call (OpenAI-compatible). Key never stored/logged. |
| `burst.py` | verified-burst core: best-of-N + self-consistency / deterministic-check / judge verifiers. `passed` gates settlement. |
| `pricing.py` | service-fee quote (not a token markup). |
| `x402_gate.py` | x402 challenge + facilitator verify/settle. **Settle only on `passed`** = pay-only-if-verified. SIM until `X402_FACILITATOR_URL` set. |
| `broker.py` | orchestration: quote → authorize → burst → settle-IF-verified, + per-agent budget cap. |
| `server.py` | HTTP surface (`POST /v1/burst`, `GET /v1/quote`, `/healthz`). |
| `mcp_server.py` | MCP stdio surface — the one-liner. |
| `measure.py` | the 1-day BYOK measurement harness (offline + live). |

## Run
```bash
# offline measurement / shape
python3 measure.py --offline
# live (needs .env with CEREBRAS_API_KEY)
python3 measure.py
# HTTP broker
PORT=8402 python3 server.py
# MCP server is launched by the MCP client via the config above
```

## Config (.env, gitignored)
```
CEREBRAS_API_KEY=csk-...        # required (BYOK)
CEREBRAS_MODEL=gpt-oss-120b     # this account: gpt-oss-120b | zai-glm-4.7 (NOT llama)
# x402 (REAL mode — omit for SIM):
X402_FACILITATOR_URL=...        # facilitator that verifies/settles payments
X402_PAY_TO=0x...               # seller wallet that receives USDC
X402_USDC_ASSET=0x...           # USDC contract on the chosen network
X402_NETWORK=base-sepolia
```

## Go-live checklist
- [ ] **x402 creds**: facilitator URL + seller wallet (`X402_PAY_TO`) + USDC asset. Until set, payments run in **SIM** (flow is real, settlement is stubbed).
- [ ] **Verify pricing**: `gpt-oss-120b` per-token rate (current placeholder is UNVERIFIED) and set `FEES` in `pricing.py` to a margin you've checked.
- [ ] **Pick the verifier per use case**: `self_consistency` (cheap, default), `judge` (adversarial, costs an extra call), or a caller `deterministic_check` (gold standard).
- [ ] **Budget cap**: `broker.DEFAULT_BUDGET_USD` — wire to the AgentsPrice margin governor for real per-agent caps.
- [ ] **Cerebras gotcha**: requests need a browser-ish `User-Agent` or Cloudflare returns 403 error 1010 (already handled).

## Status (2026-06-15)
Core + both surfaces run **live against real Cerebras**. Verified→charged, failed→not
charged, no-payment→402, budget-cap→refused — all confirmed. x402 in SIM pending creds.
