#!/usr/bin/env python3
"""outside_watch.py — daily "did a genuine OUTSIDE buyer show up?" watch.

Two signals, in order of strength:
  1. LEDGER (authoritative, real money): any payer wallet in ledger.db that is
     NOT our self-test wallet and has spent > 0  ->  a stranger actually paid.
  2. NGINX LOGS (early lead): a POST /v1/burst from an IP that is NOT us and not
     a pure malformed-probe scanner (i.e. it got a 200/402/429, meaning it
     engaged the payment path) -> someone is trying to buy.

Quiet by design: it stages a card on the bridge ONLY when there is a NEW event
(new paying wallet, or new genuinely-engaging outside IP). A seen-state file
makes it idempotent, so a daily timer won't re-alert the same buyer. It always
rewrites a human-readable latest-summary you can `cat` any time.

Env overrides:
  LEDGER_DB       default /root/inference-burst/ledger.db
  SELF_WALLET     comma list of our own wallets to ignore (default = self-test)
  SELF_IPS        comma list of our own IPs to ignore (default = self-test IP)
  OUTBOX          bridge outbox json (default /root/bridge_data/outbox.json)
  ACCESS_LOGS     comma list of nginx logs to scan (default access.log[,.1])
"""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER_DB = os.environ.get("LEDGER_DB", os.path.join(HERE, "ledger.db"))
OUTBOX = os.environ.get("OUTBOX", "/root/bridge_data/outbox.json")
STATE = os.path.join(HERE, ".outside_watch_state.json")
LOGF = os.path.join(HERE, "outside_watch.log")
LATEST = os.path.join(HERE, "outside_watch.latest.txt")

SELF_WALLET = {w.strip().lower() for w in os.environ.get(
    "SELF_WALLET", "0xeEcc837be49c7d717d19C16190D6B92D9b574315").split(",") if w.strip()}
SELF_IPS = {ip.strip() for ip in os.environ.get(
    "SELF_IPS", "2.24.86.189,127.0.0.1,::1").split(",") if ip.strip()}
DEFAULT_LOGS = "/var/log/nginx/access.log,/var/log/nginx/access.log.1"
ACCESS_LOGS = [p.strip() for p in os.environ.get("ACCESS_LOGS", DEFAULT_LOGS).split(",") if p.strip()]

WINDOW = timedelta(hours=26)  # daily run; a little overlap so rotation never drops a day
LOG_RE = re.compile(
    r'^(?P<ip>\S+)\s+\S+\s+\S+\s+\[(?P<ts>[^\]]+)\]\s+'
    r'"(?P<method>\S+)\s+(?P<path>\S+)[^"]*"\s+(?P<status>\d{3})\s+\d+\s+'
    r'"[^"]*"\s+"(?P<ua>[^"]*)"')


def _load_state():
    try:
        s = json.load(open(STATE))
    except Exception:
        s = {}
    s.setdefault("wallets", [])
    s.setdefault("ips", [])
    return s


def _save_state(s):
    tmp = STATE + ".tmp"
    json.dump(s, open(tmp, "w"), indent=2)
    os.replace(tmp, STATE)


# ---- signal 1: the ledger (authoritative) -------------------------------
def ledger_buyers():
    """Return list of (wallet, spent, misses) for non-self payers with spent>0."""
    out = []
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{LEDGER_DB}?mode=ro", uri=True)
        for payer, spent, misses in con.execute(
                "SELECT payer, spent, misses FROM ledger"):
            if not payer:
                continue
            if payer.lower() in SELF_WALLET:
                continue
            if (spent or 0) <= 0:
                continue
            out.append((payer, float(spent or 0), int(misses or 0)))
        con.close()
    except Exception as e:  # noqa: BLE001 — never let a watch crash the timer
        out = [("__error__", 0.0, 0)] if "no such" not in str(e) else []
    return out


# ---- signal 2: nginx logs (early lead) ----------------------------------
def log_post_attempts():
    """Group non-self POST /v1/burst by IP within the window.

    Returns {ip: {"statuses": Counter-ish dict, "ua": str, "last": str,
                  "engaged": bool}} where engaged means it hit the payment
    path (200/402/429), i.e. not just a malformed 400 scanner probe.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - WINDOW
    seen = {}
    for path in ACCESS_LOGS:
        if not os.path.exists(path):
            continue
        try:
            fh = open(path, "r", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                m = LOG_RE.match(line)
                if not m:
                    continue
                if m.group("method") != "POST":
                    continue
                if not m.group("path").startswith("/v1/burst"):
                    continue
                ip = m.group("ip")
                if ip in SELF_IPS:
                    continue
                try:
                    ts = datetime.strptime(m.group("ts"), "%d/%b/%Y:%H:%M:%S %z")
                except ValueError:
                    continue
                if ts < cutoff:
                    continue
                rec = seen.setdefault(ip, {"statuses": {}, "ua": "", "last": "", "engaged": False})
                st = m.group("status")
                rec["statuses"][st] = rec["statuses"].get(st, 0) + 1
                rec["ua"] = m.group("ua")
                rec["last"] = ts.isoformat()
                if st in ("200", "402", "429"):
                    rec["engaged"] = True
    return seen


# ---- bridge card --------------------------------------------------------
def stage_card(label, text):
    try:
        items = json.load(open(OUTBOX))
    except Exception:
        items = []
    cid = 720000000 + int(time.time()) % 10000000
    while any(it.get("id") == cid for it in items):
        cid += 1
    items.append({
        "id": cid,
        "category": "💰 Outside buyer",
        "label": label,
        "text": text,
        "ts": time.time(),
    })
    tmp = OUTBOX + ".tmp"
    json.dump(items, open(tmp, "w"), indent=2)
    os.replace(tmp, OUTBOX)
    try:
        os.chmod(OUTBOX, 0o644)
    except OSError:
        pass


def logline(msg):
    with open(LOGF, "a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()}  {msg}\n")


def main():
    state = _load_state()
    buyers = ledger_buyers()
    posts = log_post_attempts()

    new_wallets, new_ips = [], []

    # --- signal 1: new paying wallets ---
    for wallet, spent, misses in buyers:
        if wallet == "__error__":
            logline("WARN: ledger read error")
            continue
        if wallet.lower() not in {w.lower() for w in state["wallets"]}:
            new_wallets.append((wallet, spent, misses))
            state["wallets"].append(wallet)

    # --- signal 2: new genuinely-engaging outside IPs ---
    engaged = {ip: r for ip, r in posts.items() if r["engaged"]}
    for ip, r in engaged.items():
        if ip not in state["ips"]:
            new_ips.append((ip, r))
            state["ips"].append(ip)

    # --- alerts (only on NEW genuine events) ---
    if new_wallets:
        lines = [f"{w}  spent ${s:.4f}  misses {m}" for w, s, m in new_wallets]
        body = ("A wallet that is NOT your self-test just PAID for verified bursts.\n\n"
                + "\n".join(lines)
                + "\n\nThis is real USDC settled on Base. First outside customer. 🎉")
        stage_card("🎉 REAL OUTSIDE BUYER paid — new wallet in the ledger", body)
        logline("ALERT new paying wallet(s): " + "; ".join(l for l in lines))

    if new_ips:
        lines = []
        for ip, r in new_ips:
            sb = ", ".join(f"{k}×{v}" for k, v in sorted(r["statuses"].items()))
            lines.append(f"{ip}  [{sb}]  UA={r['ua'][:60]}  last={r['last']}")
        body = ("An outside IP hit the PAYMENT path on POST /v1/burst (got a 200/402/429 —\n"
                "not just a malformed scanner probe). Could be a real buyer warming up:\n\n"
                + "\n".join(lines)
                + "\n\nCheck the ledger for a settled payment; if only 402/429, they tried but "
                  "didn't complete (no valid X-PAYMENT or hit a limit).")
        stage_card("👀 Outside POST hit the payment path — possible buyer", body)
        logline("ALERT new engaging IP(s): " + "; ".join(l for l in lines))

    _save_state(state)

    # --- always: refresh the human-readable latest summary ---
    probe_ips = {ip: r for ip, r in posts.items() if not r["engaged"]}
    with open(LATEST, "w") as f:
        f.write(f"Outside-buyer watch — {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"window: last {int(WINDOW.total_seconds()//3600)}h of nginx logs + full ledger\n\n")
        f.write("LEDGER (real money):\n")
        if buyers and buyers[0][0] != "__error__":
            for w, s, m in buyers:
                f.write(f"  PAID  {w}  ${s:.4f}  misses {m}\n")
        else:
            f.write("  no outside paying wallet yet (only your self-test)\n")
        f.write("\nOUTSIDE POSTs that engaged the payment path (200/402/429):\n")
        if engaged:
            for ip, r in engaged.items():
                sb = ", ".join(f"{k}×{v}" for k, v in sorted(r["statuses"].items()))
                f.write(f"  {ip}  [{sb}]  UA={r['ua'][:50]}\n")
        else:
            f.write("  none\n")
        f.write("\nMalformed/scanner POST probes (4xx-only, informational):\n")
        if probe_ips:
            for ip, r in probe_ips.items():
                sb = ", ".join(f"{k}×{v}" for k, v in sorted(r["statuses"].items()))
                f.write(f"  {ip}  [{sb}]  UA={r['ua'][:50]}\n")
        else:
            f.write("  none\n")
        verdict = ("NEW outside buyer detected — card staged on the bridge."
                   if (new_wallets or new_ips) else
                   "No new outside buyer since last run.")
        f.write(f"\n{verdict}\n")

    print(f"[outside_watch] new_wallets={len(new_wallets)} new_ips={len(new_ips)} "
          f"engaged={len(engaged)} probes={len(probe_ips)}")


if __name__ == "__main__":
    main()
