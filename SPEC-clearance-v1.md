# Verified-Burst Clearance & Flag — Open Wire Spec (v1)

**Purpose:** the format + verification steps so *any* party can mint and, more importantly,
**recognize** a Verified-Burst clearance certificate or verified flag — without trusting the
issuer's word, without re-paying, and without a central authority. This is the "instant
recognition" / "portable trust" standard the [COMMONS_DESIGN.md](COMMONS_DESIGN.md)
enforcer-bootstrap depends on.

**Conventions:** sections marked **[v1 — NORMATIVE]** describe what `clearance.py` /
`flagstore.py` implement **today** and are stable. Sections marked **[v1.1 — PROPOSED]**
are design targets, not yet implemented; do not depend on them.

---

## 1. Domain & crypto primitives [v1 — NORMATIVE]

- **Domain tag:** `vb-clearance-v1` (binds every hash/signature to this protocol+version).
- **Hash:** SHA-256, hex-encoded.
- **Signatures:** secp256k1 via Ethereum `personal_sign` / EIP-191 (`eth_account`
  `encode_defunct(text=...)`). A verifier **recovers** the signer address from the
  signature — no key distribution beyond knowing the trusted issuer *address*.

---

## 2. Clearance certificate [v1 — NORMATIVE]

A positive "this exact decision was independently verified" attestation.

### 2.1 Content hash (anti-replay binding)
```
content_hash = SHA256( "vb-clearance-v1" + "\n" + request + "\n" + answer + "\n" + (verifier_model or "") )
```
The cert is valid **only** for the exact `(request, answer, verifier_model)` it was minted
for — it cannot be lifted onto a different action.

### 2.2 Signed payload (the canonical bytes the issuer signs / a verifier recovers)
Deterministic JSON — sorted keys, compact separators `(",", ":")`:
```json
{"d":"vb-clearance-v1","ch":"<content_hash>","verified":true,"independent":true,"vm":"<verifier_model>","tx":"<settle_tx|null>","iat":<issued_at_unix>}
```

### 2.3 Cert object (on the wire)
```json
{
  "content_hash":   "<sha256 hex>",
  "answer":         "<the cleared answer>",
  "verifier_model": "<judge model, e.g. zai-glm-4.7>",
  "generator_model":"<generator model, e.g. gpt-oss-120b>",
  "verified":       true,
  "independent":    true,
  "settle_tx":      "<on-chain tx hash | null>",
  "issued_at":      1782634267,
  "issuer":         "0x<issuer address>",
  "signature":      "<hex secp256k1 signature over §2.2>",
  "cleared":        true
}
```
`cleared` is a convenience = `verified && independent`; a verifier MUST re-derive it, never
trust the field.

### 2.4 Verification steps (what a recognizing agent runs) [v1 — NORMATIVE]
A cert is **honored** iff ALL pass:
1. **Content binding:** recompute §2.1 over the action you're about to accept; must equal
   `content_hash`. (Mismatch ⇒ replay/forgery.)
2. **Issuer signature:** recover the signer over §2.2; must equal `issuer`, and — if you
   pin a trusted authority — must equal your `trusted_issuer`.
3. **Independence + verdict:** `verified == true` AND `independent == true`.
4. **Not revoked:** the target is not present on the verified-flag commons (§3).
5. **Fresh:** `now - issued_at <= max_age_s` (see §2.5).

Reference issuer (current): `0x307176445D836c18BFdCdED2D5901eA7C429f69D`.

### 2.5 Freshness / revocation [v1.1 — PROPOSED]
v1 supports an optional `max_age_s` at verify time. PROPOSED additions, because an
append-only log cannot delete:
- **`ttl`** field in the cert (short, e.g. 24h) so most checks are offline.
- **Revocation overlay** = the flag commons used as a CRL: a target later flagged unsafe
  fails step 4 even with a valid signature. (Already wired in `verify_clearance`.)
- **OCSP-analog** online freshness check for high-stakes/irreversible actions.

---

## 3. Verified flag [v1 — NORMATIVE for admission; feed PROPOSED]

A negative "this target was independently caught as unsafe/wrong" attestation = a
crowd-sourced, poison-resistant blocklist entry.

### 3.1 Admission rule (the poisoning gate)
A flag is admissible **only** with an accompanying independent-verified receipt:
`receipt.independent == true AND receipt.verified == true`. Claims are **not** writable;
only verified catches are. (Implemented: `flagstore.record_verified_catch`.)

### 3.2 Flag object [v1 current fields]
```json
{
  "target_hash":   "<sha256 of normalized target>",
  "kind":          "address | injection | listing | generic",
  "target_preview":"<short, non-reversing preview>",
  "reason":        "<why it was caught>",
  "severity":      "low | med | high",
  "receipt_id":    "<originating burst receipt>",
  "verifier_model":"<judge model>",
  "ts":            1782634267
}
```

### 3.3 Federated flag feed [v1.1 — PROPOSED]
Today the flagstore is **local**. To federate (sharing = syncing hashes, no redesign):
- **Signed peer feed:** each flag entry carries an issuer signature over a canonical
  payload (same primitive as §2.2) + an **SCT-style receipt** (§4).
- **Weight (not raw count):** `weight = payment_stake × corroboration_quorum × time_decay`
  (see COMMONS_DESIGN §3). Honor a flag at/above a consumer-chosen weight threshold.
- **Dedup** by `(target_hash, receipt_id)`.

---

## 4. SCT-style inclusion receipt [v1.1 — PROPOSED]

Borrowed from CT (RFC 6962). On submitting a cert/flag to the (future) append-only log,
the log returns an immediate signed promise to include it within a Maximum Merge Delay:
```json
{"d":"vb-clearance-v1","entry_hash":"<sha256 of the cert/flag>","log_id":"<log key id>","timestamp":<unix>,"mmd_s":86400,"sig":"<log signature>"}
```
A holder presents the receipt as portable proof-of-submission; a watcher later confirms
inclusion via a Merkle **inclusion proof** and the log's append-only property via a
**consistency proof**.

---

## 5. Privacy-preserving lookup [v1.1 — PROPOSED]

k-anonymity range query (HIBP / Safe Browsing pattern): to ask "is `<target>` flagged?",
the client sends only a short prefix of `target_hash` (e.g. first 5 hex chars); the server
returns the whole bucket of matching suffixes; the client compares **offline**. The server
never learns the exact target. Same pattern for "is this decision cleared?".

---

## 6. Honoring rules / forcing functions [v1.1 — PROPOSED]

Per COMMONS_DESIGN §8 (the make-or-break). A conforming consumer SHOULD:
- Require **≥2 attestations from distinct issuers** before treating a clearance as strong
  (CT's ≥2-operator diversity).
- Treat a single log/issuer as **untrusted** absent gossip / multiple watchers
  (split-view defense).
- Bundle honoring into the client so recognition ships with the buying capability
  (the bootstrap: be your own first enforcer).

---

## 7. Non-goals / honest scope

- A cert is **risk-reduction clearance, not proof of correctness** — only as strong as the
  independent check (strongest with `independent_quorum` across vendors).
- "Logged" does **not** imply "seen" — monitors return incomplete results (NDSS 2024).
  Don't equate publishing with protection.
- This is a *network* mechanism only once counterparties agree to require + honor certs;
  the spec is necessary but not sufficient — adoption is the open problem.

---

## Changelog
- **v1** — current `clearance.py` / `flagstore.py` behavior documented as normative.
- **v1.1 (proposed)** — SCT receipt, append-only log, federated signed flag feed, weight
  formula, k-anonymity lookup, TTL/revocation overlay, honoring rules.
