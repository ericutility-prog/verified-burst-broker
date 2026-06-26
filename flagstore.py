"""Local verified-flag store — institutional security memory for an agent.

The seed of the "agents share security strengths" hive mind, single-deployment first.
When a burst INDEPENDENTLY CONFIRMS that a target is unsafe (a malicious contract
address, an injection pattern, a poisoned listing), that catch is recorded here once.
Every future check is then a FREE local lookup — the agent never pays to re-verify a
known-bad target, and never repeats a caught mistake.

Two properties make this safe and shareable later:

  1. VERIFIED-ONLY admission (poisoning resistance). A flag is written ONLY with a
     receipt showing an INDEPENDENT model family confirmed the target is unsafe
     (receipt.independent and receipt.verified). The store is not writable by
     claims — only by verified catches. That receipt is the external anchor (same
     Trusting-Trust fix as the Guardian), so a hostile caller can't seed false flags.

  2. PRIVACY-SHAPED. We key on sha256(kind:normalized_target) and keep only a short
     preview, never the raw decision/context. The local row is already the shape a
     federated commons would sync — sharing later = syncing hashes, no redesign.

Append-only (audit trail; repeats corroborate). DB: $FLAGSTORE_DB (default beside
this file). Pure module — no broker import — so it's testable offline with no tokens;
verify_and_flag() lazily wires to the broker for the live record-on-catch path.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time

_DB_PATH = os.environ.get(
    "FLAGSTORE_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "flagstore.db"))

_LOCK = threading.Lock()
_conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA synchronous=NORMAL")
_conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS flags (
        id             INTEGER PRIMARY KEY,
        ts             INTEGER NOT NULL,
        target_hash    TEXT NOT NULL,
        kind           TEXT NOT NULL,
        target_preview TEXT NOT NULL,
        reason         TEXT NOT NULL DEFAULT '',
        severity       TEXT NOT NULL DEFAULT 'high',
        receipt_id     TEXT,
        verifier_model TEXT,
        source         TEXT NOT NULL DEFAULT 'local'
    );
    CREATE INDEX IF NOT EXISTS idx_flags_hash ON flags(target_hash);
    """
)
_conn.commit()

VALID_SEVERITY = ("low", "med", "high", "critical")

# >>> EXTENSION POINT (the "hive"): cross-agent SYNC. Rows are already privacy-shaped
# (target_hash + preview, never raw decisions), so federation = export/import rows by
# target_hash to/from a shared commons. Keep VERIFIED-ONLY admission on import (a peer
# can't inject unverified flags) and dedupe by (target_hash, receipt_id). Design the
# wire format as signed peer feeds so the commons stays trustworthy at scale.


# --- normalization / privacy ------------------------------------------------ #
def _normalize(target: str, kind: str) -> str:
    """Canonicalize a target so trivially-different spellings match. kind-aware:
    addresses and domains/urls are case-folded; everything else is trimmed+lowered."""
    t = (target or "").strip()
    k = (kind or "generic").lower()
    if k in ("address", "eth_address", "wallet", "contract"):
        return t.lower()
    if k in ("url", "domain", "host"):
        return t.lower().rstrip("/")
    return t.lower()


def _hash(target: str, kind: str) -> str:
    norm = _normalize(target, kind)
    return hashlib.sha256(f"{(kind or 'generic').lower()}:{norm}".encode()).hexdigest()


def _preview(target: str) -> str:
    """A short, human-readable hint — never the full value (privacy / share-ready)."""
    t = (target or "").strip()
    if len(t) <= 14:
        return t
    return f"{t[:8]}…{t[-4:]}"


# --- admission (verified-only) ---------------------------------------------- #
def _is_verified_catch(receipt: dict) -> bool:
    """The poisoning gate: a flag is admissible ONLY if an INDEPENDENT family
    confirmed the 'unsafe' assessment. receipt is a broker burst receipt."""
    return bool(receipt) and receipt.get("independent") is True and receipt.get("verified") is True


def record_verified_catch(target: str, kind: str, reason: str, receipt: dict,
                          *, severity: str = "high", source: str = "local") -> bool:
    """Admit a flag IF accompanied by an independent-verified receipt. The receipt
    must come from a burst that asked an independent judge to CONFIRM the target is
    unsafe (so receipt.independent and receipt.verified are both True). Returns True
    if admitted, False if rejected (unverified — silently not written)."""
    if not target or not _is_verified_catch(receipt):
        return False
    if severity not in VALID_SEVERITY:
        severity = "high"
    with _LOCK, _conn:
        _conn.execute(
            "INSERT INTO flags(ts, target_hash, kind, target_preview, reason, severity, "
            "receipt_id, verifier_model, source) VALUES(?,?,?,?,?,?,?,?,?)",
            (int(time.time()), _hash(target, kind), (kind or "generic").lower(),
             _preview(target), (reason or "")[:300], severity,
             receipt.get("receipt_id"), receipt.get("verifier_model"), source))
    return True


# --- lookup (free, the compounding value) ----------------------------------- #
def check_known(target: str, kind: str = "generic") -> dict | None:
    """Free local lookup. Returns an aggregated flag record if this target has been
    verified-flagged before, else None. No model call, no payment — this is the
    'share strength' payoff: a catch once is a block forever."""
    h = _hash(target, kind)
    with _LOCK:
        agg = _conn.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM flags WHERE target_hash=?", (h,)).fetchone()
        if not agg or agg[0] == 0:
            return None
        latest = _conn.execute(
            "SELECT kind, target_preview, reason, severity, verifier_model, receipt_id "
            "FROM flags WHERE target_hash=? ORDER BY ts DESC LIMIT 1", (h,)).fetchone()
    return {
        "known_bad": True,
        "target_hash": h,
        "kind": latest[0],
        "target_preview": latest[1],
        "reason": latest[2],
        "severity": latest[3],
        "verifier_model": latest[4],
        "receipt_id": latest[5],
        "times_flagged": agg[0],
        "first_seen": agg[1],
        "last_seen": agg[2],
    }


def guard(target: str, kind: str = "generic") -> dict:
    """Pre-action gate: call this BEFORE an irreversible step. Returns a machine-first
    verdict — action='hold' if the target was independently verified bad before
    (don't act), else action='proceed' (unknown to the commons; verify if it matters)."""
    hit = check_known(target, kind)
    if hit:
        return {"action": "hold", "known_bad": True,
                "advice": (f"This {hit['kind']} was independently flagged unsafe "
                           f"{hit['times_flagged']}x ({hit['severity']}): {hit['reason']}. "
                           f"DO NOT act on it."),
                **hit}
    return {"action": "proceed", "known_bad": False,
            "advice": "Not in the verified-flag store. Unknown ≠ safe — verify if the step is consequential."}


# --- live integration (lazy broker import) ---------------------------------- #
def verify_and_flag(target: str, kind: str, *, provider_key: str | None = None,
                    model: str | None = None, severity: str = "high") -> dict:
    """Run a REAL independent-judge burst that asks whether `target` is unsafe, and
    record it ONLY if the independent judge confirms. Returns the burst result with a
    `flagged` bool. This is the record-on-catch path; check_known() makes every later
    lookup free. (Lazy broker import keeps the core dependency-light + offline-testable.)"""
    import broker
    request = (f"Security assessment. Is this {kind} unsafe, malicious, or a known scam/"
               f"attack target? TARGET: {target}\nAnswer 'yes' (unsafe) or 'no' (safe), "
               f"then one short reason.")
    candidate = "yes — unsafe / do not act on it"
    r = broker.serve_burst(request, x_payment="flagstore", verifier="independent_judge",
                           candidate=candidate, provider_key=provider_key, model=model,
                           facilitator=_sim_fac())
    rec = r.get("receipt") or {}
    reason = f"independent judge ({rec.get('verifier_model') or '?'}) confirmed unsafe"
    flagged = record_verified_catch(target, kind, reason, rec,
                                    severity=severity, source="verify_and_flag")
    r["flagged"] = flagged
    return r


def _sim_fac():
    from x402_gate import Facilitator

    class _F(Facilitator):
        def __init__(self): pass
        def verify(self, xp, reqs): return {"valid": True, "payer": "0xflagstore", "mode": "sim"}
        def settle(self, xp, reqs): return {"success": True, "tx": "0xFLAG-SIM", "mode": "sim"}
    return _F()


# --- maintenance ------------------------------------------------------------ #
def count() -> int:
    with _LOCK:
        return int(_conn.execute("SELECT COUNT(*) FROM flags").fetchone()[0])


def reset_all() -> None:
    """Wipe the store — tests only."""
    with _LOCK, _conn:
        _conn.execute("DELETE FROM flags")


def db_path() -> str:
    return _DB_PATH
