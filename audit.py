#!/usr/bin/env python3
"""audit.py — run the whole verified-burst security suite, aggregate, exit nonzero on
any DETERMINISTIC failure. This is what makes the self-audit actually continuous: a
systemd timer runs it on a schedule, and the deploy gate runs it before any restart.

Deterministic by default (no token spend): unit suites + the security probe (which
also exercises the LIVE broker's edge). Set AUDIT_GUARDIAN=1 to also run the live
independent-judge Guardian pass (~a few cents of Cerebras tokens) — its verdict is
ADVISORY (LLM review is noisy), so it's reported but never gates the exit code.

Every check runs against ISOLATED temp ledger/flagstore DBs, so the audit never
touches production state.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, ".venv", "bin", "python")
if not os.path.exists(PY):
    PY = sys.executable

TMP = tempfile.gettempdir()
ENV = {**os.environ,
       "LEDGER_DB": os.path.join(TMP, "vb_audit_ledger.db"),
       "FLAGSTORE_DB": os.path.join(TMP, "vb_audit_flags.db")}

# (label, script, advisory?) — advisory checks are reported but don't fail the gate.
# Deliberately ZERO-TOKEN: every check here is scripted/sim/discovery, so the hourly
# timer costs nothing. test_independence.py (which makes REAL Cerebras calls) is left
# OUT on purpose — run it manually / before a release, not on the clock.
CHECKS = [
    ("unit:abuse", "test_abuse.py", False),
    ("unit:guard", "test_guard.py", False),
    ("unit:flagstore", "test_flagstore.py", False),
    ("unit:clearance", "test_clearance.py", False),
    ("security_probe", "security_probe.py", False),
]


def _run(label, script, advisory, timeout=150):
    t0 = time.monotonic()
    try:
        p = subprocess.run([PY, os.path.join(HERE, script)], cwd=HERE, env=ENV,
                           capture_output=True, text=True, timeout=timeout)
        ok = p.returncode == 0
        out = (p.stdout or p.stderr or "").strip().splitlines()
        tail = out[-1] if out else ""
    except subprocess.TimeoutExpired:
        ok, tail = False, "TIMEOUT"
    return {"name": label, "ok": ok, "advisory": advisory,
            "detail": tail[:200], "s": round(time.monotonic() - t0, 1)}


def main():
    checks = list(CHECKS)
    if os.environ.get("AUDIT_GUARDIAN", "0").lower() in ("1", "true", "yes"):
        checks.append(("guardian(advisory)", "guardian.py", True))

    results = [_run(*c) for c in checks]
    hard_fail = [r for r in results if not r["ok"] and not r["advisory"]]

    print(f"\nverified-burst AUDIT — {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} — "
          f"{sum(r['ok'] for r in results)}/{len(results)} pass")
    for r in results:
        mark = "PASS" if r["ok"] else ("WARN" if r["advisory"] else "FAIL")
        print(f"  [{mark}] {r['name']:<20} {r['s']:>5}s  {'' if r['ok'] else '-> ' + r['detail']}")

    json.dump({"ts": int(time.time()), "results": results,
               "hard_failed": len(hard_fail)},
              open(os.path.join(HERE, "audit_report.json"), "w"), indent=2)

    if hard_fail:
        print(f"\nAUDIT FAILED: {len(hard_fail)} deterministic security check(s) regressed.")
        sys.exit(1)
    print("\nAUDIT GREEN — all deterministic security invariants hold.")


if __name__ == "__main__":
    main()
