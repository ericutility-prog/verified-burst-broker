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
By default a cert proves an INDEPENDENT CHECK + ISSUER; it proves PAYMENT only when the
verifier passes verify_settlement=True, which confirms settle_tx moved USDC to the seller
on-chain (settle_tx is otherwise an unverified claim, so demo/placeholder hashes never
settle-verify). It's a *network* mechanism only once counterparties agree to honor certs.
Pure module (lazy flagstore/broker/web3 imports) so it stays offline-testable.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

from eth_account import Account
from eth_account.messages import encode_defunct

_DOMAIN = "vb-clearance-v1"

# >>> EXTENSION POINT (network clearance): (1) DONE — verify_clearance(verify_settlement=True)
# confirms settle_tx moved USDC to the seller on-chain (see _verify_settlement below);
# (2) publish _DOMAIN + the _signed_payload shape as an OPEN SPEC so other parties can
# mint/verify certs — that is what turns this from a local gate into a network standard.
# SPEC: the v1 wire format (this module's shapes) is written up in SPEC-clearance-v1.md;
# the federated-commons architecture + the enforcer/bootstrap blocker are in COMMONS_DESIGN.md.


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


def _default_trusted_issuer() -> str | None:
    """Resolve the address a verifier pins against when the caller passes none. Prefer
    an explicit CLEARANCE_ISSUER; otherwise derive it from the signer key this process
    holds (self-hosted mint+verify). Returns None when nothing is available — callers
    then fail closed rather than trust a self-signed cert."""
    explicit = os.environ.get("CLEARANCE_ISSUER")
    if explicit:
        return explicit
    key = os.environ.get("CLEARANCE_SIGNER_KEY") or os.environ.get("X402_RELAYER_KEY")
    if key:
        try:
            return Account.from_key(key).address
        except Exception:
            return None
    return None


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


# --- on-chain settlement verification (opt-in) --------------------------------- #
# USDC (6-decimals) per network + the ERC-20 Transfer topic. verify_clearance(
# verify_settlement=True) uses these to confirm settle_tx actually moved USDC to the
# seller — so a cert can prove PAYMENT, not just an independent check.
_USDC = {
    "eip155:8453":  "0x833589fCD6eDb6E08f4c7C32d4f71B54bda02913",  # Base mainnet
    "eip155:84532": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",  # Base Sepolia
}
_TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_RPC = {"eip155:8453": "https://mainnet.base.org", "eip155:84532": "https://sepolia.base.org"}


def _network() -> str:
    return os.environ.get("X402_NETWORK", "eip155:84532")


def _default_receipt_fetch(tx: str):
    """Fetch a tx receipt from the chain (lazy web3 import; None if unreachable/absent)."""
    try:
        from web3 import Web3
    except Exception:
        return None
    rpc = os.environ.get("X402_RPC_URL") or _RPC.get(_network())
    if not rpc:
        return None
    try:
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))
        return w3.eth.get_transaction_receipt(tx)
    except Exception:
        return None


def _hx(v) -> str:
    return v.hex() if hasattr(v, "hex") else str(v)


def _topic_addr(topic) -> str:
    """Last 20 bytes of a 32-byte log topic -> 0x-address (lowercased)."""
    h = _hx(topic)
    h = h[2:] if h.startswith("0x") else h
    return "0x" + h[-40:].lower()


def _usdc_to(receipt, pay_to: str, usdc: str) -> int:
    """Sum USDC (6-dec integer units) transferred TO pay_to in this receipt's logs."""
    pay_to = pay_to.lower(); usdc = usdc.lower(); total = 0
    logs = receipt.get("logs", []) if isinstance(receipt, dict) else getattr(receipt, "logs", [])
    for log in logs:
        addr = (log.get("address") if isinstance(log, dict) else log.address)
        topics = (log.get("topics") if isinstance(log, dict) else log.topics)
        if not addr or addr.lower() != usdc or not topics or len(topics) < 3:
            continue
        if not _hx(topics[0]).lower().endswith(_TRANSFER_TOPIC):
            continue
        if _topic_addr(topics[2]) != pay_to:
            continue
        data = _hx(log.get("data") if isinstance(log, dict) else log.data)
        total += int(data, 16) if data not in ("", "0x") else 0
    return total


def _verify_settlement(settle_tx, pay_to, min_amount_usd, *, fetch=None):
    """Confirm settle_tx settled >= min_amount_usd of USDC to pay_to on-chain. Returns
    (ok, reason). Rejects non-hash placeholders (e.g. '0xDEMO_settle_tx')."""
    if not isinstance(settle_tx, str) or not _HASH_RE.match(settle_tx):
        return False, "settle_tx is not a valid on-chain tx hash (placeholder/forged)"
    if not pay_to:
        return False, "no pay_to to check settlement against (set X402_PAY_TO / pass pay_to=)"
    usdc = _USDC.get(_network())
    if not usdc:
        return False, f"no known USDC asset for network {_network()}"
    rcpt = (fetch or _default_receipt_fetch)(settle_tx)
    if rcpt is None:
        return False, "settle_tx has no reachable on-chain receipt (not found / RPC down)"
    status = rcpt.get("status") if isinstance(rcpt, dict) else rcpt.status
    if int(status) != 1:
        return False, "settle_tx reverted on-chain (status != 1)"
    got = _usdc_to(rcpt, pay_to, usdc)
    need = int(round(float(min_amount_usd or 0) * 1_000_000))
    if got <= 0:
        return False, "settle_tx moved no USDC to the seller (pay_to)"
    if got < need:
        return False, f"settle_tx settled {got/1e6:.6f} USDC to pay_to, want >= {need/1e6:.6f}"
    return True, f"settlement confirmed on-chain ({got/1e6:.6f} USDC to pay_to)"


def verify_clearance(cert: dict, request: str, answer: str | None = None, *,
                     trusted_issuer: str | None = None, target: str | None = None,
                     kind: str = "generic", max_age_s: int | None = None,
                     verify_settlement: bool = False, pay_to: str | None = None,
                     min_amount_usd: float | None = None, _receipt_fetch=None) -> dict:
    """Counterparty-side check. Returns {cleared: bool, reasons: [...]}. `cleared` is
    True only if the cert is signed by the (trusted) issuer, bound to THIS exact action,
    independently verified, not expired, and the target isn't revoked on the commons.
    With verify_settlement=True it ALSO requires settle_tx to have moved >= min_amount_usd
    of USDC to pay_to (env X402_PAY_TO by default) on-chain — proving payment, not just the
    independent check. Off by default so the module stays offline/pure."""
    reasons, ok = [], True
    ans = answer if answer is not None else cert.get("answer", "")

    # 1) content binding — cert must be for THIS action (anti-replay)
    if _content_hash(request, ans, cert.get("verifier_model")) != cert.get("content_hash"):
        ok = False
        reasons.append("content_hash mismatch — cert is for a different action (replay/forgery)")

    # 2) issuer signature — must be signed by the TRUSTED clearance authority.
    #    Fail closed: with no trusted issuer to pin against, a self-signed cert is
    #    indistinguishable from a forgery, so refuse it rather than trust the cert's
    #    own stated issuer. trusted_issuer defaults to CLEARANCE_ISSUER / the signer
    #    key's address; a bare verifier with neither pins nothing and rejects.
    pin = trusted_issuer if trusted_issuer is not None else _default_trusted_issuer()
    if not pin:
        ok = False
        reasons.append("no trusted issuer pinned — refusing self-signed cert (fail-closed); "
                       "pass trusted_issuer= or set CLEARANCE_ISSUER")
    else:
        try:
            recovered = Account.recover_message(encode_defunct(text=_signed_payload(cert)),
                                                signature=cert["signature"])
            if recovered.lower() != str(cert.get("issuer", "")).lower():
                ok = False
                reasons.append("signature does not match the stated issuer (forged)")
            elif recovered.lower() != pin.lower():
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

    # 6) settlement (opt-in) — prove the cert's settle_tx actually moved USDC to the seller
    #    on-chain. Off by default so the module stays offline/pure; a counterparty that
    #    needs PAYMENT proof (not just an independent check) passes verify_settlement=True.
    if verify_settlement:
        pt = pay_to if pay_to is not None else os.environ.get("X402_PAY_TO")
        sok, sreason = _verify_settlement(cert.get("settle_tx"), pt, min_amount_usd,
                                          fetch=_receipt_fetch)
        if not sok:
            ok = False
            reasons.append("settlement not confirmed: " + sreason)

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
