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
    """
)
_conn.commit()

# >>> EXTENSION POINT (durability / scale): this whole module is the storage seam.
# The API below is storage-agnostic — reimplement these functions against
# Postgres/Redis (row locks / atomic INCR) for multi-process or multi-host scale;
# nothing in broker.py changes.
_EPS = 1e-9  # float dust threshold so released holds settle cleanly back to ~0


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


def clear_misses(payer: str) -> None:
    """A pass clears the streak."""
    with _LOCK, _conn:
        _conn.execute("UPDATE ledger SET misses = 0 WHERE payer=?", (payer,))


def trial_inc(payer: str) -> None:
    with _LOCK, _conn:
        _conn.execute(
            "INSERT INTO ledger(payer, trial) VALUES(?, 1) "
            "ON CONFLICT(payer) DO UPDATE SET trial = trial + 1", (payer,))


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


# --- test / maintenance ----------------------------------------------------- #
def reset_all() -> None:
    """Wipe the ledger — for tests only (production never calls this)."""
    with _LOCK, _conn:
        _conn.execute("DELETE FROM ledger")
        _conn.execute("DELETE FROM global_judge")


def db_path() -> str:
    return _DB_PATH
