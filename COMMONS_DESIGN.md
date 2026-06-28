# Verified-Burst Commons — Design Sketch

**Status:** design/roadmap artifact, *not* a commitment to build now. The income-now
(Cerebras migration) and first-customer tracks remain primary; this captures the
network thinking so it compounds instead of evaporating.

**Question this answers:** how can *more users of Verified Burst add security for all
users* — turning the per-user clearance/flag artifacts into a shared commons where one
agent's verified catch protects everyone and one agent's clearance is portable trust
others honor — without the commons being poisoned, gamed, or Sybil-flooded?

Grounded in a fact-checked research pass (24/25 claims confirmed by adversarial vote;
primary sources: IETF RFC 6962/9162, Chrome CT policy, Cloudflare/HIBP, MDPI Algorithms
2023 Sybil survey, NDSS 2024 CT-monitor study). Stripe Radar figures are vendor
self-reported → treated as directional, not measured.

---

## 1. The headline

A "more users → more security for all" commons is **proven possible** by stacking three
borrowed layers — **but only if a Sybil-cost gate is enforced at the same time**. We
already hold most of the pieces:

- `clearance.py` — content-hash + issuer signature + revocation-via-flags certs (`_DOMAIN = "vb-clearance-v1"`).
- `flagstore.py` — verified-only admission (a flag needs an independent-verified receipt).
- `ledger.py` — durable, atomic per-payer state = the proof-of-payment substrate.
- Live clearance issuer identity: `0x307176445D836c18BFdCdED2D5901eA7C429f69D`.

The research mostly tells us *which patterns to copy* and *where the idea breaks*.

---

## 2. The two-log architecture

### A. Clearance log — copy Certificate Transparency
- **Append-only Merkle log** with consistency proofs: anyone can prove history was never
  rewritten *without trusting the operator* [RFC 6962/9162]. Clearance certs become entries.
- **SCT-style receipt:** the log returns an immediate **signed promise to include**
  within a Maximum Merge Delay — a portable proof-of-submission decoupled from the
  inclusion proof [RFC 6962]. Every cert carries one.
- **Detection model:** "no central trust root, **many independent watchers**" — the log
  doesn't police itself; monitors/auditors do [RFC 6962].

### B. Flag commons — copy Stripe Radar's data network effect
- Pool catch-signal across all participants → the network sees threats **earlier** than
  any solo agent; cross-participant reuse compounds value (Stripe: "90% of cards seen
  more than once") [Stripe — *vendor marketing, directional only*]. One agent's verified
  catch protecting all is the same mechanism.

---

## 3. How a flag earns weight

**Not raw user count** — weight = **payment-stake × corroboration-quorum × time-decay**,
with revocation for false positives. The binding constraint: in open settings only
**costly-resource** schemes (PoW/PoS/**payment**) give *strong* Sybil resistance;
reputation/identity give only *weak* resistance [MDPI 2023; Douceur 2002]. Our x402
payment **is** the Sybil cost. Corroboration = ≥N distinct paying buyers and/or distinct
judge model families confirming the same target.

---

## 4. Sybil / poisoning defenses (each tied to the failure it prevents)

| Defense | Prevents | Precedent | Status |
|---|---|---|---|
| Verified-only admission (flag needs independent-verified receipt) | poisoned feeds / false flags | — | ✅ `flagstore.py` |
| Payment-stake + quorum weighting | Sybil flooding when admission is free | MDPI; Douceur | ⏳ new |
| ≥2 attestations from **distinct issuers** | single-operator capture | Chrome CT (≥2 SCTs, distinct operators) | ⏳ new |
| Gossip / multiple watchers | **split-view equivocation** (a log showing different views to different clients) | RFC 9162; arXiv 1806.08817 | ⏳ new |

---

## 5. Privacy-preserving lookups (drives adoption)

Use the **HIBP / Safe Browsing k-anonymity range query**: the agent sends only a short
hash *prefix*, gets the whole bucket back, compares offline — the server never learns
what was checked (a 5-char SHA-1 prefix ≈ 305 candidates) [Cloudflare/HIBP]. Agents ask
"is this flagged/cleared?" without revealing the content. Privacy → more queriers → more
coverage.

---

## 5b. Dissemination — simulating nonlocality (the collapse model)

We can't *broadcast* instantly (speed of light, network latency, and our own "logged ≠
seen" finding rule it out), and we don't claim to. But the commons can deliver the
**experience** of "spooky action at a distance" — distant participants staying correlated
through shared state — which is the part of entanglement that actually fits. (Honest scope:
even real entanglement can't *signal*; it produces **correlation, not communication**. So
does this.)

### The collapse metaphor (the product narrative)
- A decision starts in "superposition" — unverified; could be right or wrong.
- The **first burst "measures" it** → collapses it to a definite verified value, written to
  the commons (a clearance cert, or a flag if it was caught).
- **Every later observer who looks gets the same collapsed value** — correlated, with no
  re-derivation. Like measuring entangled particles: whoever checks, whenever, finds
  agreement. The "hidden variable" isn't hidden to us — it's the commons.

"**Measure once, everyone observes the same collapse**" is the sharp one-line narrative for
*why* one agent's verification is worth something to all the others.

### How the simulation is delivered: pull-at-action ≫ push-to-everyone
The illusion of an instant global alert comes from **checking at the point of action, not
pushing to everyone.** B isn't notified the moment A makes a catch; B consults the commons
**in its own critical path**, right before the irreversible step. So A's distant catch
protects B *the instant B is about to act* — zero push. (Exactly how Safe Browsing / HIBP
feel instant: you check at click-time, nobody pushes you every bad URL.)

Three knobs turn up the fidelity:
1. **Check at the point of action** → protection appears precisely when needed (feels instant).
2. **Mandatory check at irreversible steps** → an agent can never "miss the entanglement."
3. **Fast, bounded propagation** → shared state fresh enough that the gap is imperceptible.

### Two dissemination modes
| Mode | When | Guarantee |
|---|---|---|
| **Pull-at-action** (default) | every irreversible decision | "instant" *relative to the actor* — synchronous lookup (k-anonymity, §5) in the critical path |
| **Bounded-staleness push** (gossip/pub-sub) | high-severity events (key compromise, mass revocation) | eventually-consistent, seconds–minutes; **not** instant. CT models this as the *Maximum Merge Delay*: queryable *within a fixed time*, not immediately [RFC 6962] |

### The honest floor (no version removes this)
An observer still has to **look**, and there's a nonzero window between A's catch and the
collapse being globally queryable. We can shrink it until imperceptible — never to zero.
We sell the *experience* of nonlocality (correlation through shared collapsed state, checked
at the point of action); we do **not** claim to beat causality.

---

## 6. Incentive loop

**Referral-on-reuse:** when a contributor's flag/cert is later reused to protect someone,
they earn a micro-rebate — so contributing pays. (*Design choice, not a cited deployed
result.*) Mirrors AgentsPrice's "only if it converts" referral ethic.

---

## 7. ⚠️ Where the research CONTRADICTS the idea (read this twice)

1. **"Logged ≠ seen."** The claim that *a federation of monitors reliably reconstructs
   the complete set* was **REFUTED** in verification — real CT monitors return
   **incomplete** results (no evaluated monitor returned a complete set; 12–52% of certs
   missing) [NDSS 2024]. → Never market "your catch is in the commons, therefore everyone
   is protected." Needs redundant reliable watchers + honest framing.
2. **Append-only can't delete → revoking a *wrong clearance* is unsolved.** A positive
   "safe" cert later found wrong can't be un-logged. Needs a CRL/OCSP analog + short TTL
   overlay. (Explicit open question.)
3. **The micropayment may be too cheap to deter poisoning.** x402 at fractions of a cent
   might not impose enough Sybil cost at scale; weighted entries may need an extra
   **stake/bond**. (Open question.)
4. **There is no "Chrome" to force adoption.** See §8 — the make-or-break.

---

## 8. The enforcer / bootstrap problem (the real blocker — deep-dive)

**Why CT actually worked:** not the cryptography — the *enforcement*. Chrome **hard-fails**
validation of non-compliant certs (since Chrome 68, July 2018), requires **≥2 SCTs from
distinct operators**, runs **random compliance testing**, and **removes** logs that break
the inclusion promise [Chrome CT policy]. Apple followed Oct 2018. A small number of
clients with overwhelming leverage made honoring CT *mandatory*. That forcing-function is
what separated CT from the graveyard.

**The graveyard:** PGP web-of-trust had sound math and died — no enforcer, brutal UX, no
reason for the next party to honor it. crev (code-review commons) died the same way: no
gatekeeper made attestations matter. "Many eyes" (Linus's Law) is likewise contested —
Heartbleed sat in open OpenSSL for years; eyes must be *incentivized and competent*, not
merely present.

**The hard truth for us:** an agent-decision commons has **no dominant client** with
Chrome-like leverage to mandate honoring certs/flags. This is the single biggest unsolved
risk. A technically perfect commons that nobody is *required* to honor is worth nothing.

**Candidate answers (in rough order of leverage):**

1. **Be your own first enforcer (highest-leverage, do this first).** Bundle
   cert-honoring + flag-lookup *into* `pip install verified-burst`. Every agent that
   installs the buying tool *also* enforces recognition — our own distribution is the
   bootstrap. The MCP tool's `buy_verified_burst` gains a sibling `verify_clearance` /
   `check_flag` that agents call by default. Adoption rides in on the thing they already
   wanted (the verified burst), exactly the recognition→join thesis.
2. **Make honoring the cheaper default.** An agent that honors a valid clearance skips
   re-paying for the same decision (free cache hit); an agent that honors a flag avoids an
   irreversible loss. Honoring is the *dominant* strategy, so no mandate is needed for the
   self-interested — the network grows by self-interest, not decree.
3. **Anchor to an existing enforcer instead of becoming one.** Piggyback a venue that
   *already* gates agents: an MCP registry that requires a clearance to list a tool; an
   x402 marketplace that surfaces "cleared" sellers first; a framework's tool-call
   middleware. Borrow someone else's hard-fail.
4. **Reputation-gated quorum as a soft enforcer.** Until a hard enforcer exists, weight is
   the enforcement: unhonored issuers/flags simply carry no weight, so honoring the
   high-weight ones is rational. This degrades gracefully from "mandate" to "gravity."

**Bootstrap sequence:** (1) ship honoring inside the pip tool → (2) make honoring the
cheaper path (cache-hit / loss-avoidance) → (3) accumulate a corroborated high-weight set
so honoring is rational → (4) only then seek an external venue to make it mandatory. Do
**not** invert this; a mandate without prior gravity is how crev died.

---

## 9. Prioritized build order (concrete to VB)

1. **SCT-style signed receipt** on every cert/flag (smallest step; extends `clearance.py`).
2. **Append-only Merkle log** + published consistency proofs for the cert/flag stream.
3. **k-anonymity prefix lookup** over `flagstore` (privacy unlocks adoption).
4. **Quorum + payment-stake weighting** for flags (extend the ledger).
5. **Gossip / ≥2-watcher** split-view defense before claiming tamper-evidence.
6. **Revocation overlay + short TTL** for clearance certs (the un-delete problem).
7. **Honoring bundled into the pip tool** (the bootstrap — arguably do alongside #1).

---

## 10. Open questions (from the research)

- What corroboration-quorum size + judge-family diversity minimizes false-confirms vs.
  false-catches in a *paid* setting? Can `proof_harness.py` (16/16 catch, 0 false-confirm
  @N=120) be extended to measure marginal security gain per additional honest participant?
- The CRL/OCSP analog + appeals process for revoking a wrong clearance in an append-only log.
- Sybil-cost calibration: is the x402 micropayment enough, or is a stake/bond needed?
- Who plays the "browser enforcer" role (see §8) — the make-or-break.

---

## Sources (primary-heavy)
- RFC 6962, RFC 9162 — Certificate Transparency (append-only Merkle log, SCT, proofs).
- Chrome CT policy + log policy — enforcement, ≥2-operator diversity, removal sanctions.
- arXiv 1806.08817 — gossip / split-view defense.
- NDSS 2024 — CT monitors return incomplete results ("logged ≠ seen").
- Stripe ML-fraud primer — data network effect (*vendor self-reported*).
- Cloudflare/HIBP — k-anonymity range-query lookups.
- MDPI Algorithms 2023; Douceur 2002 — Sybil resistance requires costly identity.
- PGP web-of-trust / Linus's Law — shared-trust failures without an enforcer.
