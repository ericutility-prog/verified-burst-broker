"""Durable, atomic per-payer ledger for the verified-burst broker.

Replaces the in-process dicts (_SPENT / _RESERVED / _TRIAL / _IJ_MISSES / the global
judge pool) that reset on every restart. Backed by sqlite so spend caps, free-trial
counts and the anti-abuse breakers SURVIVE A RESTART and stay correct under
concurrency. Every read-modify-write runs in a single transaction under a process
lock, so the strict budget governor and the Sybil breakers can't be raced.

Why sqlite (not Redis/Postgres): zero ops, single file, ACID, ships with Python —
right-sized for a single-process broker. The API below is storage-agnostic, so
swapping in Postgres later is a one-file change, not a broker change.

DB path: $LEDGER_DB (default ledger.db beside this file). Tests point it at a temp
file so they never touch the production ledger.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

_DB_PATH = os.environ.get(
    "LEDGER_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger.db"))

# One connection per process, serialized by a lock. sqlite + WAL gives durable,
# crash-safe writes and lets reads not block writes; the lock makes each
# check-and-write atomic across the broker's request threads.
_LOCK = threading.Lock()
_conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA synchronous=NORMAL")
_conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS ledger (
        payer    TEXT PRIMARY KEY,
        spent    REAL NOT NULL DEFAULT 0,
        reserved REAL NOT NULL DEFAULT 0,
        trial    INTEGER NOT NULL DEFAULT 0,
        misses   INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS global_judge (
        id   INTEGER PRIMARY KEY CHECK (id = 1),
        day  INTEGER NOT NULL,
        used INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS global_trial (
        id   INTEGER PRIMARY KEY CHECK (id = 1),
        day  INTEGER NOT NULL,
        used INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS pending_settle (
        nonce  TEXT PRIMARY KEY,   -- x402 authorization dedup key
        payer  TEXT NOT NULL,
        amount REAL NOT NULL,      -- held fee (money MAY have moved on-chain)
        tx     TEXT,               -- broadcast tx hash if surfaced (often empty)
        reason TEXT,               -- the settle failure reason (why it's ambiguous)
        ts     REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS seen_nonce (
        k  TEXT PRIMARY KEY,   -- dedup key for a signed x402 authorization
        ts REAL NOT NULL       -- when first claimed (for optional pruning)
    );
    """
)
_conn.commit()

# >>> EXTENSION POINT (durability / scale): this whole module is the storage seam.
# The API below is storage-agnostic — reimplement these functions against
# Postgres/Redis (row locks / atomic INCR) for multi-process or multi-host scale;
# nothing in broker.py changes.
#
# CONCURRENCY CONTRACT — read before adding a breaker.
# _LOCK is a process-local threading.Lock, so every check-and-write below is atomic
# only WITHIN this process. That is sufficient today (server.py runs one
# ThreadingHTTPServer) and becomes wrong the moment a second worker process exists.
# A breaker whose CHECK and WRITE are separate calls is not protected by any of this:
# it must be expressed as ONE function here (see trial_claim / judge_enter), because
# the gap between two calls is where 8 concurrent requests all read zero.
_INFLIGHT_JUDGE = {}   # payer -> judged bursts currently running in THIS process


# --- reads ------------------------------------------------------------------ #
def spent(payer: str) -> float:
    with _LOCK:
        row = _conn.execute("SELECT spent FROM ledger WHERE payer=?", (payer,)).fetchone()
    return float(row[0]) if row else 0.0


def reserved(payer: str) -> float:
    with _LOCK:
        row = _conn.execute("SELECT reserved FROM ledger WHERE payer=?", (payer,)).fetchone()
    return float(row[0]) if row else 0.0


def remaining(payer: str, cap: float) -> float:
    """Spendable budget = cap minus settled spend AND outstanding holds, so it's
    honest while other bursts for this wallet are mid-flight."""
    with _LOCK:
        row = _conn.execute("SELECT spent, reserved FROM ledger WHERE payer=?", (payer,)).fetchone()
    used = (row[0] + row[1]) if row else 0.0
    return max(0.0, cap - used)


def is_proven(payer: str) -> bool:
    """A wallet that has settled >=1 payment (spent > 0) is exempt from the breakers."""
    return spent(payer) > 0.0


def miss_count(payer: str) -> int:
    with _LOCK:
        row = _conn.execute("SELECT misses FROM ledger WHERE payer=?", (payer,)).fetchone()
    return int(row[0]) if row else 0


def trial_count(payer: str) -> int:
    with _LOCK:
        row = _conn.execute("SELECT trial FROM ledger WHERE payer=?", (payer,)).fetchone()
    return int(row[0]) if row else 0


# --- atomic budget ops ------------------------------------------------------ #
def reserve(payer: str, amount: float, cap: float) -> bool:
    """Atomically HOLD `amount` against the payer's remaining budget. Returns True if
    held, False if it would breach the cap. The check and the hold are one transaction,
    so two concurrent bursts from one wallet can't both pass before either settles."""
    with _LOCK, _conn:
        row = _conn.execute("SELECT spent, reserved FROM ledger WHERE payer=?", (payer,)).fetchone()
        used = (row[0] + row[1]) if row else 0.0
        if amount > max(0.0, cap - used):
            return False
        _conn.execute(
            "INSERT INTO ledger(payer, reserved) VALUES(?, ?) "
            "ON CONFLICT(payer) DO UPDATE SET reserved = reserved + excluded.reserved",
            (payer, amount))
        return True


def release(payer: str, amount: float) -> None:
    """Return an unspent hold to the budget (miss / burst failure / non-settle)."""
    with _LOCK, _conn:
        _conn.execute("UPDATE ledger SET reserved = MAX(0.0, reserved - ?) WHERE payer=?",
                      (amount, payer))


def commit(payer: str, amount: float) -> None:
    """Convert a hold into settled spend (a verified, settled burst). Upserts so a
    direct settle with no prior reserve (e.g. best-price search) also works."""
    with _LOCK, _conn:
        _conn.execute(
            "INSERT INTO ledger(payer, spent) VALUES(?, ?) "
            "ON CONFLICT(payer) DO UPDATE SET spent = spent + excluded.spent, "
            "reserved = MAX(0.0, reserved - ?)",
            (payer, amount, amount))


# --- abuse breakers --------------------------------------------------------- #
def record_miss(payer: str) -> int:
    """Increment the consecutive independent-judge miss streak; return the new count."""
    with _LOCK, _conn:
        _conn.execute(
            "INSERT INTO ledger(payer, misses) VALUES(?, 1) "
            "ON CONFLICT(payer) DO UPDATE SET misses = misses + 1", (payer,))
        row = _conn.execute("SELECT misses FROM ledger WHERE payer=?", (payer,)).fetchone()
    return int(row[0])


def trial_inc(payer: str) -> None:
    with _LOCK, _conn:
        _conn.execute(
            "INSERT INTO ledger(payer, trial) VALUES(?, 1) "
            "ON CONFLICT(payer) DO UPDATE SET trial = trial + 1", (payer,))


def trial_claim(payer: str, cap: int) -> bool:
    """Atomically CLAIM one free-trial slot. Returns True if a slot was taken.

    Replaces read-trial_count-now / trial_inc-later: those are two calls with a whole
    burst between them, so N concurrent requests all read the same count and all pass a
    cap of 1. The claim happens up front and is refunded by trial_unclaim if the burst
    never runs. Conditional UPDATE + rowcount is the check and the write in one statement.
    """
    if cap <= 0:
        return False
    with _LOCK, _conn:
        _conn.execute("INSERT OR IGNORE INTO ledger(payer) VALUES(?)", (payer,))
        cur = _conn.execute(
            "UPDATE ledger SET trial = trial + 1 WHERE payer=? AND trial < ?", (payer, cap))
        return cur.rowcount > 0


def trial_unclaim(payer: str) -> None:
    """Give back a claimed trial slot when the burst never happened."""
    with _LOCK, _conn:
        _conn.execute("UPDATE ledger SET trial = MAX(0, trial - 1) WHERE payer=?", (payer,))


def judge_enter(payer: str, limit: int) -> bool:
    """Atomically admit ONE broker-paid judged burst, counting settled misses AND the
    bursts this process already has in flight. Without the in-flight term the breaker is
    a read-then-act: miss_count is read before the burst and record_miss written after,
    so N concurrent requests all see the pre-burst count and all pass.

    In-flight state is deliberately in-memory: it describes work running in THIS process
    and must vanish if the process dies, which is exactly what a restart should do.
    """
    with _LOCK:
        row = _conn.execute("SELECT misses FROM ledger WHERE payer=?", (payer,)).fetchone()
        settled = int(row[0]) if row else 0
        if settled + _INFLIGHT_JUDGE.get(payer, 0) >= limit:
            return False
        _INFLIGHT_JUDGE[payer] = _INFLIGHT_JUDGE.get(payer, 0) + 1
        return True


def judge_exit(payer: str) -> None:
    with _LOCK:
        n = _INFLIGHT_JUDGE.get(payer, 0) - 1
        if n > 0:
            _INFLIGHT_JUDGE[payer] = n
        else:
            _INFLIGHT_JUDGE.pop(payer, None)      # bounded memory: drop empty entries


# --- ambiguous settlements (money MAY have moved) --------------------------- #
def pending_add(nonce: str, payer: str, amount: float, tx: str, reason: str) -> None:
    """Record a settlement we could not prove either way.

    The dangerous state is not "settle failed" — it is "settle reported success and the
    chain did not confirm it." Releasing the hold and telling the buyer they were not
    charged is a CLAIM, and on this branch we cannot support it. Writing the row first
    means the ambiguity survives a restart and can be reconciled against the chain.
    """
    with _LOCK, _conn:
        _conn.execute(
            "INSERT INTO pending_settle(nonce, payer, amount, tx, reason, ts) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(nonce) DO UPDATE SET "
            "tx = excluded.tx, reason = excluded.reason, ts = excluded.ts",
            (nonce or f"{payer}:{time.time()}", payer, float(amount), tx or "", reason,
             time.time()))


def pending_list():
    with _LOCK:
        rows = _conn.execute(
            "SELECT nonce, payer, amount, tx, reason, ts FROM pending_settle "
            "ORDER BY ts").fetchall()
    return [{"nonce": r[0], "payer": r[1], "amount": r[2], "tx": r[3],
             "reason": r[4], "ts": r[5]} for r in rows]


def pending_clear(nonce: str) -> None:
    with _LOCK, _conn:
        _conn.execute("DELETE FROM pending_settle WHERE nonce=?", (nonce,))


# --- boot recovery ---------------------------------------------------------- #
def recover_holds():
    """Release every outstanding hold. Call ONCE at boot, never while serving.

    A hold only exists between reserve() and release()/commit(), which both live inside a
    single request. So at process start no hold can legitimately be outstanding: anything
    non-zero was stranded by a crash, and nothing else in the system will ever free it —
    it silently shrinks that payer's cap forever. Returns the rows it cleared so the
    caller can log them; a silent recovery would hide the crash that caused it.

    NOT safe if a second worker process is ever added (it would free live holds). See the
    concurrency contract at the top of this module.
    """
    with _LOCK, _conn:
        rows = _conn.execute(
            "SELECT payer, reserved FROM ledger WHERE reserved > 0").fetchall()
        _conn.execute("UPDATE ledger SET reserved = 0 WHERE reserved > 0")
    return [{"payer": r[0], "amount": r[1]} for r in rows]


def prune_nonces(max_age_s: float = 30 * 86400) -> int:
    """Drop dedup keys older than max_age_s. seen_nonce grows without bound otherwise —
    an x402 authorization is long dead well before this, so pruning cannot enable a
    replay that the chain would still honour."""
    with _LOCK, _conn:
        cur = _conn.execute("DELETE FROM seen_nonce WHERE ts < ?", (time.time() - max_age_s,))
        return cur.rowcount


def global_judge_reserve(judges: int, daily_cap: int) -> bool:
    """Reserve `judges` calls against today's global pool for UNPROVEN wallets (the
    Sybil-rotation cap). Atomic check-and-reserve with a UTC-day rollover. Returns
    False once the day's pool is exhausted."""
    day = int(time.time() // 86400)
    with _LOCK, _conn:
        row = _conn.execute("SELECT day, used FROM global_judge WHERE id=1").fetchone()
        if not row or row[0] != day:
            _conn.execute(
                "INSERT INTO global_judge(id, day, used) VALUES(1, ?, 0) "
                "ON CONFLICT(id) DO UPDATE SET day = excluded.day, used = 0", (day,))
            used = 0
        else:
            used = int(row[1])
        if used + judges > daily_cap:
            return False
        _conn.execute("UPDATE global_judge SET used = used + ? WHERE id=1", (judges,))
        return True


def global_trial_reserve(bursts: int, daily_cap: int) -> bool:
    """Reserve `bursts` free-trial host-key bursts against today's global pool for
    UNPROVEN wallets. This is the Sybil-rotation cap for the self_consistency trial
    path (the per-wallet trial_cap alone can be defeated by rotating wallets, since a
    failed trial burst never settles, so its USDC is never spent and recycles). Atomic
    check-and-reserve with a UTC-day rollover; returns False once the day's pool is
    exhausted. Separate pool from global_judge so trial and judge budgets don't share."""
    day = int(time.time() // 86400)
    with _LOCK, _conn:
        row = _conn.execute("SELECT day, used FROM global_trial WHERE id=1").fetchone()
        if not row or row[0] != day:
            _conn.execute(
                "INSERT INTO global_trial(id, day, used) VALUES(1, ?, 0) "
                "ON CONFLICT(id) DO UPDATE SET day = excluded.day, used = 0", (day,))
            used = 0
        else:
            used = int(row[1])
        if used + bursts > daily_cap:
            return False
        _conn.execute("UPDATE global_trial SET used = used + ? WHERE id=1", (bursts,))
        return True


# --- single-use payment authorizations -------------------------------------- #
def claim_nonce(key: str) -> bool:
    """Atomically record a signed-payment dedup key as USED. Returns True if newly
    claimed (this request may proceed), False if it was already seen (replay / a
    concurrent fan-out of the same authorization → reject). INSERT OR IGNORE under the
    lock makes the claim race-free, so K parallel requests sharing one payment yield
    exactly one True. This is what stops a single payment from buying many results
    before it settles on-chain."""
    with _LOCK, _conn:
        cur = _conn.execute(
            "INSERT OR IGNORE INTO seen_nonce(k, ts) VALUES(?, ?)", (key, time.time()))
        return cur.rowcount > 0


# --- test / maintenance ----------------------------------------------------- #
def reset_all() -> None:
    """Wipe the ledger — for tests only (production never calls this)."""
    with _LOCK, _conn:
        _conn.execute("DELETE FROM ledger")
        _conn.execute("DELETE FROM global_judge")
        _conn.execute("DELETE FROM global_trial")
        _conn.execute("DELETE FROM seen_nonce")


def db_path() -> str:
    return _DB_PATH
