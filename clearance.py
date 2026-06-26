"""clearance.py — burst as a network clearance mechanism.

A verified burst already produces a verdict + an on-chain settlement. This turns that
into an UNFORGEABLE, NON-REPLAYABLE clearance certificate a counterparty can check
before accepting an agent's action — admission control for a network of agents.

A clearance cert binds three things so it can't be misused:
  1. CONTENT BINDING — content_hash = H(domain, request, answer, verifier_model). A
     cert is valid ONLY for the exact (request, answer) it was minted for, so it can't
     be replayed to "clear" a different action.
  2. ISSUER SIGNATURE — the broker signs the cert with its key (eth_account). Anyone
     can recover the signer and confirm it's the trusted clearance authority, so a
     holder can't forge "cleared".
  3. INDEPENDENCE + VERDICT — the cert is only `cleared` if an INDEPENDENT family
     actually verified it (verified and independent both true).

verify_clearance() also consults the verified-flag commons as a REVOCATION list: a
target later flagged unsafe fails clearance even with a valid signature.

HONEST SCOPE: this is risk-reduction clearance, not a proof of correctness — it's only
as strong as the independent check (strongest with independent_quorum across vendors).
And it's a *network* mechanism only once counterparties agree to require + honor certs.
Pure module (lazy flagstore/broker imports) so it's offline-testable with no tokens.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

from eth_account import Account
from eth_account.messages import encode_defunct

_DOMAIN = "vb-clearance-v1"

# >>> EXTENSION POINT (network clearance): (1) verify settle_tx ON-CHAIN inside
# verify_clearance so a cert also proves payment cleared, not just an independent check;
# (2) publish _DOMAIN + the _signed_payload shape as an OPEN SPEC so other parties can
# mint/verify certs — that is what turns this from a local gate into a network standard.


def _content_hash(request: str, answer: str, verifier_model: str | None) -> str:
    """Bind the cert to THIS exact decision. Anyone can recompute it; the signature
    is what makes it unforgeable."""
    blob = f"{_DOMAIN}\n{request}\n{answer}\n{verifier_model or ''}"
    return hashlib.sha256(blob.encode()).hexdigest()


def _signed_payload(cert: dict) -> str:
    """The canonical bytes the issuer signs / a verifier recovers — deterministic."""
    return json.dumps({
        "d": _DOMAIN,
        "ch": cert["content_hash"],
        "verified": bool(cert["verified"]),
        "independent": bool(cert["independent"]),
        "vm": cert.get("verifier_model"),
        "tx": cert.get("settle_tx"),
        "iat": int(cert["issued_at"]),
    }, sort_keys=True, separators=(",", ":"))


def _signer_key(explicit: str | None) -> str:
    key = explicit or os.environ.get("CLEARANCE_SIGNER_KEY") or os.environ.get("X402_RELAYER_KEY")
    if not key:
        raise RuntimeError("no clearance signer key (set CLEARANCE_SIGNER_KEY)")
    return key


def sign_clearance(request: str, result: dict, *, signer_key: str | None = None) -> dict:
    """Mint a clearance certificate from a broker burst result. `cleared` is true only
    when an independent family verified the decision; the cert is signed + content-bound."""
    rec = result.get("receipt") or {}
    answer = result.get("answer") or rec.get("answer") or ""
    vm = rec.get("verifier_model")
    cert = {
        "content_hash": _content_hash(request, answer, vm),
        "answer": answer,
        "verifier_model": vm,
        "generator_model": rec.get("generator_model"),
        "verified": bool(rec.get("verified")),
        "independent": bool(rec.get("independent")),
        "settle_tx": result.get("tx") or rec.get("settle_tx"),
        "issued_at": int(time.time()),
    }
    acct = Account.from_key(_signer_key(signer_key))
    cert["issuer"] = acct.address
    cert["signature"] = Account.sign_message(
        encode_defunct(text=_signed_payload(cert)), _signer_key(signer_key)).signature.hex()
    cert["cleared"] = cert["verified"] and cert["independent"]
    return cert


def verify_clearance(cert: dict, request: str, answer: str | None = None, *,
                     trusted_issuer: str | None = None, target: str | None = None,
                     kind: str = "generic", max_age_s: int | None = None) -> dict:
    """Counterparty-side check. Returns {cleared: bool, reasons: [...]}. `cleared` is
    True only if the cert is signed by the (trusted) issuer, bound to THIS exact action,
    independently verified, not expired, and the target isn't revoked on the commons."""
    reasons, ok = [], True
    ans = answer if answer is not None else cert.get("answer", "")

    # 1) content binding — cert must be for THIS action (anti-replay)
    if _content_hash(request, ans, cert.get("verifier_model")) != cert.get("content_hash"):
        ok = False
        reasons.append("content_hash mismatch — cert is for a different action (replay/forgery)")

    # 2) issuer signature — must be signed by the (trusted) clearance authority
    try:
        recovered = Account.recover_message(encode_defunct(text=_signed_payload(cert)),
                                            signature=cert["signature"])
        if recovered.lower() != str(cert.get("issuer", "")).lower():
            ok = False
            reasons.append("signature does not match the stated issuer (forged)")
        elif trusted_issuer and recovered.lower() != trusted_issuer.lower():
            ok = False
            reasons.append(f"issuer {recovered} is not the trusted clearance authority")
    except Exception as e:
        ok = False
        reasons.append(f"signature invalid: {type(e).__name__}")

    # 3) actually cleared, independently
    if not cert.get("verified"):
        ok = False
        reasons.append("decision was not verified — clearance not granted")
    if not cert.get("independent"):
        ok = False
        reasons.append("decision was not independently checked")

    # 4) revocation — target later flagged on the verified-flag commons
    if target is not None:
        import flagstore
        if flagstore.check_known(target, kind):
            ok = False
            reasons.append("target is on the verified-flag commons (revoked/blocked)")

    # 5) freshness
    if max_age_s is not None and (int(time.time()) - int(cert.get("issued_at", 0))) > max_age_s:
        ok = False
        reasons.append("clearance expired")

    return {"cleared": ok, "reasons": reasons or ["all checks passed"]}


def clear_decision(request: str, *, signer_key: str | None = None, **burst_kwargs) -> dict:
    """Live convenience: run an INDEPENDENT-judge burst on `request` and return a signed
    clearance cert. Lazy broker import keeps the core offline-testable. burst_kwargs pass
    through to serve_burst (provider_key, candidate, model, quorum_k, facilitator, …)."""
    import broker
    burst_kwargs.setdefault("verifier", "independent_judge")
    result = broker.serve_burst(request, **burst_kwargs)
    cert = sign_clearance(request, result, signer_key=signer_key)
    return {"result": result, "cert": cert}
