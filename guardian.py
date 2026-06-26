"""The Guardian — a security watchdog that improves the broker's security layer
by spending VERIFIED BURSTS OF ITS OWN.

How it works (and why it's the safe kind of "self-improving security"):

  1. DETERMINISTIC backbone — runs security_probe.py (authoritative invariants).
  2. INDEPENDENT review — the Guardian becomes a buyer of our own product: for each
     security-sensitive code region it spends a verified burst with the
     `independent_judge` verifier, so a DIFFERENT model family (the external anchor
     that defeats the Trusting-Trust problem — code can't vet itself) weighs in on
     whether the live code actually holds the property. It gates on the burst's
     proceed/hold verdict and KEEPS the receipt as evidence.
  3. It ALERTS — it never rewrites the security code. A human applies fixes. A
     self-rewriting security layer can silently weaken a protection; this can't.

Dogfooding bonus: every run is real, repeated, useful usage of the verified-burst
product on a task an agent genuinely can't DIY (an independent check of itself).

Run:  .venv/bin/python guardian.py        (exit 1 on any ALERT — CI/timer ready)
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

# The Guardian's self-review bursts settle to an ISOLATED ledger (it's auditing the
# code, not earning revenue) — keep its bookkeeping out of the production ledger.
os.environ.setdefault("LEDGER_DB", os.path.join(tempfile.gettempdir(), "vb_guardian.db"))
import env; env.load_env()
import broker
import burst
from x402_gate import Facilitator

HERE = os.path.dirname(os.path.abspath(__file__))
HOST_KEY = os.environ.get("CEREBRAS_API_KEY", "")


class _OKFac(Facilitator):
    """The Guardian settles its own bursts in-process (sim) — it's exercising the
    verification path on itself, not moving real USDC. Swap for the live facilitator
    + a funded Guardian wallet to make it a fully on-chain paying customer."""
    def __init__(self, payer="0xguardian"): self.payer = payer
    def verify(self, xp, reqs): return {"valid": True, "payer": self.payer, "mode": "sim"}
    def settle(self, xp, reqs): return {"success": True, "tx": "0xGUARDIAN-SIM", "mode": "sim"}


def _excerpt(rel_path, marker, before=3, n=14):
    """Pull the CURRENT code around `marker` from a live source file, so the
    Guardian reviews what's actually deployed — not a stale paste."""
    path = os.path.join(HERE, rel_path)
    lines = open(path).read().splitlines()
    idx = next((i for i, ln in enumerate(lines) if marker in ln), None)
    if idx is None:
        return None
    lo = max(0, idx - before)
    return "\n".join(lines[lo:lo + n])


# Each item: a real code region + a yes/no security property where we KNOW the safe
# answer. The base model assesses; the independent judge (different family) confirms.
REVIEWS = [
    {"id": "charge_only_on_pass", "file": "broker.py",
     "marker": "settle ONLY if the verifier passed",
     "q": "Looking ONLY at this code, can a buyer be charged (settlement run) when the "
          "verifier did NOT pass / res.passed is False? Answer 'yes' or 'no'.",
     "safe": "no"},
    {"id": "no_host_token_burn", "file": "broker.py",
     "marker": "never run the broker-paid judge on the host key",
     "q": "In this code, can a caller with NO provider key (no BYOK) and no candidate run "
          "the broker-paid independent judge on the host key? Answer 'yes' or 'no'.",
     "safe": "no"},
    {"id": "budget_reservation", "file": "broker.py",
     "marker": "HOLD the fee up front",
     "q": "Does this reserve the fee against the budget cap BEFORE running the burst, so "
          "concurrent bursts from one wallet can't each pass the check then overspend? "
          "Answer 'yes' or 'no'.",
     "safe": "yes"},
    {"id": "no_stack_leak", "file": "server.py",
     "marker": "fail closed, no stack leak",
     "q": "Does this error handler return a full stack trace or the raw exception message "
          "to the client? Answer 'yes' or 'no'.",
     "safe": "no"},
    {"id": "redos_screen", "file": "burst.py",
     "marker": "ReDoS screen",
     "q": "Does this reject caller-supplied regex with nested quantifiers like (a+)+ before "
          "calling re.search? Answer 'yes' or 'no'.",
     "safe": "yes"},
]


def _yn(text):
    m = re.search(r"\b(yes|no)\b", (text or "").lower())
    return m.group(1) if m else "?"


def _run_probe():
    """Deterministic backbone. Returns (ok, summary_dict)."""
    try:
        subprocess.run([sys.executable, os.path.join(HERE, "security_probe.py")],
                       cwd=HERE, timeout=90, capture_output=True)
    except subprocess.TimeoutExpired:
        pass
    try:
        rep = json.load(open(os.path.join(HERE, "security_report.json")))
        s = rep.get("summary", {})
        return (s.get("high_failed", 1) == 0 and s.get("med_failed", 1) == 0), s
    except Exception as e:
        return False, {"error": str(e)}


def _review_one(item):
    """Spend ONE verified burst (independent_judge) to review a code region."""
    code = _excerpt(item["file"], item["marker"])
    if code is None:
        return {**item, "status": "ERROR", "detail": f"marker not found in {item['file']}",
                "receipt": None}
    request = (f"You are a security reviewer. CODE from {item['file']}:\n\n{code}\n\n"
               f"QUESTION: {item['q']}")
    r = broker.serve_burst(request, x_payment="guardian", facilitator=_OKFac(),
                           verifier="independent_judge", strategy="fast",
                           provider_key=HOST_KEY, model="gpt-oss-120b",
                           budget_cap=10**9)   # self-audit, not revenue — cap must not bite
    if r["status"] not in ("ok", "not_verified"):
        return {**item, "status": "ERROR", "detail": f"burst status={r['status']}",
                "receipt": None}
    ans = _yn(r.get("answer"))
    confirmed = r.get("gate", {}).get("action") == "proceed"   # independent judge agrees
    # CLEAR only when the answer is the SAFE one AND an independent family confirmed it.
    if ans == item["safe"] and confirmed:
        status = "CLEAR"
    elif ans != item["safe"] and ans != "?":
        status = "ALERT"            # the model itself thinks the property is violated
    else:
        status = "REVIEW"           # unconfirmed by the independent judge — eyeball it
    return {"id": item["id"], "file": item["file"], "status": status,
            "answer": ans, "safe_answer": item["safe"], "independently_confirmed": confirmed,
            "verifier_model": r.get("receipt", {}).get("verifier_model"),
            "receipt": r.get("receipt")}


def main():
    t0 = time.monotonic()
    probe_ok, probe_summary = _run_probe()
    reviews = [_review_one(it) for it in REVIEWS]

    alerts = [r for r in reviews if r["status"] == "ALERT"]
    needs_eye = [r for r in reviews if r["status"] in ("REVIEW", "ERROR")]
    verdict = "GREEN" if (probe_ok and not alerts and not needs_eye) else \
              ("ALERT" if (not probe_ok or alerts) else "REVIEW")

    print(f"\n  ┌─ GUARDIAN · verified-burst security · {verdict}")
    print(f"  ├─ deterministic probe: "
          f"{'PASS' if probe_ok else 'FAIL'} ({probe_summary.get('total','?')} checks, "
          f"{probe_summary.get('failed','?')} failed)")
    print(f"  ├─ independent review (bursts of its own, judged by a different model family):")
    for r in reviews:
        ic = "confirmed" if r.get("independently_confirmed") else "unconfirmed"
        print(f"  │   [{r['status']:>6}] {r['id']:<22} answer={r.get('answer','?'):>3} "
              f"(safe={r.get('safe_answer','?')}) · {ic} by {r.get('verifier_model','?')}")
    print(f"  └─ {len(reviews)} bursts spent · receipts kept · {time.monotonic()-t0:.1f}s")

    if verdict != "GREEN":
        print("\n  REMEDIATION NEEDED (Guardian alerts; it does NOT auto-edit):")
        for r in alerts + needs_eye:
            print(f"    - {r['id']} ({r['file']}): {r['status']} "
                  f"— model says '{r.get('answer')}', expected safe='{r.get('safe_answer')}'"
                  f"{'' if r.get('independently_confirmed') else ', not independently confirmed'}")

    out = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "verdict": verdict, "probe_ok": probe_ok, "probe_summary": probe_summary,
           "reviews": reviews, "bursts_spent": len(reviews)}
    with open(os.path.join(HERE, "guardian_report.json"), "w") as f:
        json.dump(out, f, indent=2)
    print(f"\n  [wrote guardian_report.json]")

    sys.exit(0 if verdict == "GREEN" else 1)


if __name__ == "__main__":
    main()
