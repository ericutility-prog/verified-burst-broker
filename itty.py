#!/usr/bin/env python3
"""THE ITTY BITTY — deterministic fault simulation of the settle-then-deliver money path.

FoundationDB/Antithesis technique at our size: drive the REAL serve_burst, inject one
fault at one exact point, check invariants, and replay any failure from (seed, ordinal).

WHY IT CAN'T TOUCH PRODUCTION
  * LEDGER_DB is repointed at a fresh temp file BEFORE `ledger` is imported, and a guard
    aborts if that repoint didn't take.
  * The facilitator and the model call are INJECTED through serve_burst's own test seams
    (`facilitator=`, `call_fn=`) — no network, no chain, no provider, no wallet.
  * The only production symbol patched is `clearance._default_receipt_fetch` (the RPC
    call), so the REAL _confirm_settlement_onchain / _usdc_to logic runs against a
    synthetic receipt. We stub the network, never the logic under test.

FAULT KINDS
  Fault(Exception)     — an ordinary error; serve_burst's `except Exception` handlers DO
                         run, so this tests the handlers.
  Crash(BaseException) — process death (SIGKILL/power cut); `except Exception` does NOT
                         catch it, so cleanup is skipped exactly as it would be. Tests
                         DURABILITY, which is a different property and the costly one.

INVARIANTS
  I1 no free delivery    — status ok  => spent rose by exactly the quoted price
  I2 no silent charge    — status not ok => spent unchanged
  I3 no leaked hold      — a run that returned cleanly leaves reserved where it started
  I4 no orphaned hold    — a crash must not strand a hold (nothing expires or sweeps it)
  I5 single-use payment  — one authorization can never be accepted twice, IN ANY ENCODING
  I6 no paid-undelivered — settle moved money => ledger recorded it AND the answer shipped
  I7 cap respected       — spent + reserved never exceeds the budget cap
  I8 evidence required   — an answer is delivered only when the settlement was positively
                           confirmed on-chain. _confirm_settlement_onchain's own docstring
                           says the point is that "a facilitator that reports success
                           without a real transfer can't buy a free result" — I8 tests that
                           claim rather than the implementation.
  I9 breaker honoured    — a per-wallet ceiling (trial_cap, miss allowance) is never
                           exceeded, including under concurrency

Run:  python3 itty.py                  full sweep
      python3 itty.py --replay 1729:3  re-run one deterministic failure exactly
"""
import base64
import concurrent.futures
import json
import os
import sys
import tempfile
import threading

SEED = 1729

# --- containment: repoint the ledger BEFORE importing it -------------------- #
_TMPDIR = tempfile.mkdtemp(prefix="itty-")
os.environ["LEDGER_DB"] = os.path.join(_TMPDIR, "itty-ledger.db")
# A real-shaped seller address so the on-chain confirmation path can actually run.
PAY_TO = "0x00000000000000000000000000000000deadbeef"
os.environ["X402_PAY_TO"] = PAY_TO
os.environ["X402_NETWORK"] = "eip155:8453"      # must be a key in clearance._USDC

if os.environ.get("X402_MODE", "").lower() == "live":
    sys.exit("itty: refusing to run with X402_MODE=live")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger                                       # noqa: E402
import broker                                       # noqa: E402
import clearance                                    # noqa: E402

if os.path.abspath(ledger.db_path()) != os.path.abspath(os.environ["LEDGER_DB"]):
    sys.exit("itty: ledger did not honour LEDGER_DB — ABORTING rather than risk the real one")

PRICE = None
CAP = 1.00


class Fault(Exception):
    """A recoverable error. serve_burst's handlers run."""


class Crash(BaseException):
    """Process death. `except Exception` does not catch this, so cleanup is skipped."""


# --- injected doubles -------------------------------------------------------- #
class Trip:
    def __init__(self, at=0, kind=Crash, after=True):
        self.at, self.kind, self.after = at, kind, after
        self.n = 0
        self.fired_at = None

    def step(self, label):
        self.n += 1
        if self.at and self.n == self.at:
            self.fired_at = label
            raise self.kind(label)

    def wrap(self, fn, label):
        def inner(*a, **kw):
            if not self.after:
                self.step(label + ":before")
            r = fn(*a, **kw)
            if self.after:
                self.step(label + ":after")
            return r
        return inner


HASH_TX = "0x" + "ab" * 32          # hash-shaped -> the on-chain check actually runs


class FakeFacilitator:
    """settle_mode: ok | ok_no_tx | ok_hash | fail | raise | expired

    _coerce MIRRORS x402_live._coerce_payload (lines 300-307): base64-of-JSON first,
    then RAW JSON. That dual acceptance is not an artifact of this harness — it is what
    the live facilitator does, and it is the precondition for finding #2.
    """

    def __init__(self, settle_mode="ok"):
        self.settle_mode = settle_mode
        self.settled = []
        self.lock = threading.Lock()

    @staticmethod
    def _coerce(x_payment):
        for decode in (lambda v: base64.b64decode(v), lambda v: v):
            try:
                return json.loads(decode(x_payment))
            except Exception:
                continue
        raise ValueError("undecodable X-PAYMENT")

    def verify(self, x_payment, requirements):
        p = self._coerce(x_payment)
        return {"valid": True, "reason": "itty", "payer": p["payload"]["from"], "mode": "itty"}

    def settle(self, x_payment, requirements):
        if self.settle_mode == "raise":
            raise Fault("facilitator settle exploded")
        if self.settle_mode == "fail":
            return {"success": False, "tx": "", "mode": "itty", "reason": "not confirmed"}
        if self.settle_mode == "expired":
            return {"success": False, "tx": "", "mode": "itty", "reason": "authorization expired"}
        if self.settle_mode == "ok_no_tx":
            # success reported, NO transaction hash surfaced
            with self.lock:
                self.settled.append("<no-tx>")
            return {"success": True, "tx": "", "mode": "itty"}
        if self.settle_mode == "ok_hash":
            with self.lock:
                self.settled.append(HASH_TX)
            return {"success": True, "tx": HASH_TX, "mode": "itty"}
        with self.lock:
            tx = "itty-tx-%04d" % len(self.settled)
            self.settled.append(tx)
        return {"success": True, "tx": tx, "mode": "itty"}


# --- synthetic on-chain receipts (network stubbed, real logic exercised) ----- #
def _receipt(status=1, to=PAY_TO, units=None, token=None):
    units = 4000 if units is None else units          # $0.004 at 6 decimals
    token = token or clearance._USDC[clearance._network()]
    topic_to = "0x" + "00" * 12 + to[2:].lower()
    return {"status": status,
            "logs": [{"address": token,
                      "topics": ["0x" + clearance._TRANSFER_TOPIC, "0x" + "00" * 32, topic_to],
                      "data": hex(units)}]}


class receipts:
    """Context manager: swap the RPC fetch only. _confirm_settlement_onchain and
    _usdc_to are untouched and do the real work."""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.orig = clearance._default_receipt_fetch
        clearance._default_receipt_fetch = lambda tx: self.value
        return self

    def __exit__(self, *a):
        clearance._default_receipt_fetch = self.orig


def scripted(text):
    def call_fn(msgs, temperature=0.0):
        return {"text": text, "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                "latency_s": 0.0}
    return call_fn


def payment(payer, nonce, encoding="base64"):
    """A decodable x402-shaped authorization. encoding='raw' skips base64 — the shape
    x402_live accepts but broker._payment_key cannot key (finding #2)."""
    body = json.dumps({"payload": {"from": payer, "signature": "0xsig-" + nonce,
                                   "authorization": {"nonce": nonce}}})
    return body if encoding == "raw" else base64.b64encode(body.encode()).decode()


# --- one run ----------------------------------------------------------------- #
def run_once(*, payer, nonce, answer="Answer: yes", settle_mode="ok", trip=None,
             budget_cap=CAP, encoding="base64", verifier="self_consistency",
             answer_key=("regex", r"(yes|no)"), require_byok=False, trial_cap=0,
             fac=None, instrument=True):
    trip = trip or Trip()
    fac = fac or FakeFacilitator(settle_mode)
    before = (ledger.spent(payer), ledger.reserved(payer))
    pend0 = len(ledger.pending_list()) if hasattr(ledger, "pending_list") else 0

    originals = {}
    if instrument:
        originals = {n: getattr(ledger, n) for n in
                     ("claim_nonce", "reserve", "release", "commit", "record_miss", "trial_inc")}
        for name, fn in originals.items():
            setattr(ledger, name, trip.wrap(fn, "ledger." + name))
        fac.settle = trip.wrap(fac.settle, "fac.settle")

    outcome, exc = None, None
    try:
        outcome = broker.serve_burst(
            "pick yes or no", x_payment=payment(payer, nonce, encoding),
            strategy="best_of_n", n=3, verifier=verifier, answer_key=answer_key,
            call_fn=scripted(answer), facilitator=fac, budget_cap=budget_cap,
            require_byok=require_byok, trial_cap=trial_cap)
    except Crash as e:
        exc = ("crash", str(e))
    except Fault as e:
        exc = ("fault", str(e))
    except Exception as e:
        exc = ("unexpected:" + type(e).__name__, str(e))
    finally:
        for name, fn in originals.items():
            setattr(ledger, name, fn)

    after = (ledger.spent(payer), ledger.reserved(payer))
    pend1 = len(ledger.pending_list()) if hasattr(ledger, "pending_list") else 0
    return {"payer": payer, "nonce": nonce, "outcome": outcome, "exc": exc,
            "fired_at": trip.fired_at, "ordinal": trip.at, "claimed": list(fac.settled),
            "before": before, "after": after, "cap": budget_cap, "fac": fac,
            "pending_delta": pend1 - pend0}


# --- invariants -------------------------------------------------------------- #
def check(obs, *, confirmed=None):
    """confirmed: True/False/None — whether the settlement was positively confirmed
    on-chain for this scenario (None = not applicable / not exercised)."""
    v = []
    out, (s0, r0), (s1, r1) = obs["outcome"], obs["before"], obs["after"]
    status = (out or {}).get("status")
    delivered = bool(out) and status == "ok" and out.get("answer") is not None
    # The facilitator CLAIMED a capture. Whether funds truly moved is exactly what we
    # cannot know from here — which is the point. A reverted tx moves nothing; an
    # underpaid or misdirected one moves something. The system must be conservative
    # about all three identically, so the invariant is written against the CLAIM.
    claimed = bool(obs["claimed"])
    ds, dr = s1 - s0, r1 - r0

    if status == "ok" and abs(ds - PRICE) > 1e-9:
        v.append(("I1", f"delivered but spent moved {ds:+.6f}, expected {PRICE:+.6f}"))
    if status and status != "ok" and abs(ds) > 1e-9:
        v.append(("I2", f"status={status} but spent moved {ds:+.6f}"))
    if obs["exc"] is None and abs(dr) > 1e-9:
        v.append(("I3", f"returned cleanly but reserved moved {dr:+.6f} — hold leaked"))
    # NOTE: a hold outstanding immediately after a crash is EXPECTED and not a violation —
    # the process died mid-request. The real property is that it is recoverable at boot,
    # which is a property of crash+restart and so is tested in sec_recovery, not here.
    # I6 — a claimed capture must end in exactly one of two states: charged and
    # delivered, or UNRESOLVED AND RECORDED. Releasing the hold and asserting "you were
    # not charged" on a capture we could not disprove is the state that must not exist.
    if claimed and not delivered and obs["pending_delta"] < 1 and not obs["exc"]:
        v.append(("I6", f"capture claimed ({obs['claimed']}) and the answer withheld, but "
                        f"nothing was written to pending_settle — this settlement is now "
                        f"unreconcilable and the buyer was told they weren't charged"))
    if claimed and delivered and abs(ds - PRICE) > 1e-9:
        v.append(("I6", f"delivered on a claimed capture but spent moved {ds:+.6f}"))
    if obs["exc"] and obs["exc"][0] == "crash" and claimed and abs(ds) < 1e-9 \
            and obs["pending_delta"] < 1:
        v.append(("I6", "crashed after a claimed capture leaving no spend AND no pending "
                        "row — a dead process cannot write one after the fact, so the "
                        "intent must be recorded BEFORE the capture is attempted"))
    if s1 + r1 - obs["cap"] > 1e-9:
        v.append(("I7", f"spent+reserved {s1 + r1:.6f} exceeds cap {obs['cap']:.6f}"))
    if delivered and confirmed is False:
        v.append(("I8", "answer delivered though the settlement was NOT confirmed on-chain "
                        "— a facilitator reporting success with no real transfer bought a "
                        "free result, which is what defense #6 exists to prevent"))
    if obs["exc"] and obs["exc"][0].startswith("unexpected"):
        v.append(("BUG", f"uninjected exception escaped serve_burst: {obs['exc']}"))
    return v


def report(name, vio, detail="", fails=None):
    print(f"  {'FAIL' if vio else 'ok  '}  {name:<48} {detail}")
    for vid, msg in vio:
        print(f"          {vid}: {msg}")
        if fails is not None:
            fails.append((name, vid, msg))
    return vio


# --- sections ---------------------------------------------------------------- #
def sec_deterministic(fails):
    print("== named scenarios + crash sweep " + "=" * 40)
    for i, (nm, kw) in enumerate([
            ("S1 happy path", dict()),
            ("S2 verifier miss (no charge)", dict(answer="Answer: maybe")),
            ("S3 settle returns failure", dict(settle_mode="fail")),
            ("S4 settle raises", dict(settle_mode="raise")),
            ("S5 auth expired (clock skew)", dict(settle_mode="expired"))]):
        obs = run_once(payer=f"0xs{i}", nonce=f"s{i}", **kw)
        report(nm, check(obs), f"-> {(obs['outcome'] or {}).get('status') or obs['exc']}", fails)

    for o in range(1, 9):
        obs = run_once(payer=f"0xc{o}", nonce=f"c{o}", trip=Trip(at=o, kind=Crash))
        if obs["fired_at"] is None:
            continue
        report(f"crash after {obs['fired_at']}", check(obs),
               f"spent{obs['after'][0]-obs['before'][0]:+.6f} "
               f"held{obs['after'][1]-obs['before'][1]:+.6f}  [replay {SEED}:{o}]", fails)


def sec_encoding(fails):
    """#2 — single-use enforcement vs. a raw-JSON authorization."""
    print("\n== #2 single-use payment, per encoding " + "=" * 34)
    for enc in ("base64", "raw"):
        p, n = f"0xenc-{enc}", f"enc-{enc}"
        first = run_once(payer=p, nonce=n, encoding=enc)
        second = run_once(payer=p, nonce=n, encoding=enc)
        st1 = (first["outcome"] or {}).get("status")
        st2 = (second["outcome"] or {}).get("status")
        # BOTH encodings must block. x402_live._coerce_payload authorizes either shape, so
        # any shape the facilitator accepts has to be a shape _payment_key can key.
        blocked = st2 == "payment_already_used"
        vio = [] if blocked else [("I5", f"the SAME authorization was accepted twice as "
                                         f"{enc} (1st={st1}, 2nd={st2}) — broker._payment_key "
                                         f"cannot key this encoding, so ledger.claim_nonce is "
                                         f"skipped entirely and the single-use control is off")]
        report(f"replay same authorization as {enc}", vio, f"2nd -> {st2}", fails)


def sec_onchain(fails):
    """#3 — does delivery actually require on-chain evidence?"""
    print("\n== #3 on-chain settlement confirmation " + "=" * 34)
    cases = [
        ("confirmed transfer",        "ok_hash", _receipt(),                        True),
        ("reverted tx (status 0)",    "ok_hash", _receipt(status=0),                False),
        ("underpaid",                 "ok_hash", _receipt(units=3999),              False),
        ("wrong payee",               "ok_hash", _receipt(to="0x" + "11" * 20),     False),
        ("RPC unreachable",           "ok_hash", None,                              None),
        ("success reported, NO tx",   "ok_no_tx", None,                             False),
    ]
    for i, (nm, mode, rcpt, confirmed) in enumerate(cases):
        with receipts(rcpt):
            obs = run_once(payer=f"0xoc{i}", nonce=f"oc{i}", settle_mode=mode)
        st = (obs["outcome"] or {}).get("status")
        report(nm, check(obs, confirmed=confirmed), f"-> {st}", fails)
    print("     ('RPC unreachable' delivering is DELIBERATE — the code trusts the")
    print("      facilitator rather than block revenue on RPC flakiness. 'NO tx' takes")
    print("      the same branch, which is the finding: absence of evidence == pass.)")


def sec_concurrency(fails):
    """#5 — per-wallet breakers are read-then-act across a long gap.
    NON-DETERMINISTIC by nature: this is a race, not a replayable fault. A barrier
    maximises the window; a passing run is not proof the race is absent."""
    print("\n== #5 breaker TOCTOU under concurrency (NON-deterministic) " + "=" * 14)
    K = 8

    def fire(payer, tag, **kw):
        bar = threading.Barrier(K)
        fac = FakeFacilitator("ok")

        def one(i):
            bar.wait()
            return run_once(payer=payer, nonce=f"{tag}-{i}", fac=fac,
                            instrument=False, **kw)
        with concurrent.futures.ThreadPoolExecutor(max_workers=K) as ex:
            return [f.result() for f in [ex.submit(one, i) for i in range(K)]]

    # (a) free-trial slots: trial_count() is read before the burst, trial_inc() after
    res = fire("0xtoctou-trial", "tt", require_byok=True, trial_cap=1)
    used = ledger.trial_count("0xtoctou-trial")
    ran = sum(1 for r in res if (r["outcome"] or {}).get("trial") is True)
    vio = [] if used <= 1 else [("I9", f"trial_cap=1 but {ran} concurrent bursts ran on the "
                                       f"host key and trial_count ended at {used} — the check "
                                       f"and the increment are not one transaction")]
    report(f"trial_cap=1 vs {K} concurrent", vio, f"host-key bursts={ran} trial_count={used}", fails)

    # (b) miss allowance: miss_count() read before the burst, record_miss() after
    res = fire("0xtoctou-miss", "tm", verifier="independent_judge",
               answer='{"adequate": false, "reason": "no"}', answer_key=None)
    misses = ledger.miss_count("0xtoctou-miss")
    locked = sum(1 for r in res if (r["outcome"] or {}).get("status") == "verifier_locked")
    limit = broker.IJ_MISS_LIMIT
    vio = [] if misses <= limit else [("I9", f"miss allowance is {limit} but {misses} judged "
                                             f"bursts completed concurrently ({locked} blocked) "
                                             f"— each read miss_count before any wrote it, so "
                                             f"the breaker was bypassed by {misses - limit}")]
    report(f"miss allowance={limit} vs {K} concurrent", vio,
           f"misses={misses} locked={locked}", fails)


def sec_recovery(fails):
    """#4 — a crash strands a hold. The property under test is that a RESTART frees it,
    and that an interrupted capture is still reconcilable afterwards."""
    print("\n== boot recovery after a crash " + "=" * 42)

    p = "0xrecover"
    run_once(payer=p, nonce="rec-1", trip=Trip(at=2, kind=Crash))   # die after reserve
    stranded = ledger.reserved(p)
    freed = ledger.recover_holds()                                   # what boot would do
    vio = []
    if stranded <= 0:
        vio = [("I4", "expected a stranded hold to recover, but none was left")]
    elif ledger.reserved(p) > 1e-9:
        vio = [("I4", f"recover_holds() left {ledger.reserved(p):.6f} still held — the "
                      f"payer's cap stays permanently reduced")]
    elif not any(f["payer"] == p for f in freed):
        vio = [("I4", "hold was cleared but not reported — a silent recovery hides the "
                      "crash that caused it")]
    report("stranded hold freed at boot, loudly", vio,
           f"stranded={stranded:.6f} freed={len(freed)} now={ledger.reserved(p):.6f}", fails)

    p2 = "0xrecover2"
    before = len(ledger.pending_list())
    run_once(payer=p2, nonce="rec-2", trip=Trip(at=3, kind=Crash))   # die after settle
    rows = [r for r in ledger.pending_list() if r["payer"] == p2]
    vio = [] if rows else [("I6", "crash mid-capture left nothing to reconcile")]
    report("interrupted capture is reconcilable", vio,
           f"pending rows for payer: {len(rows)} (total {len(ledger.pending_list()) - before:+d})",
           fails)


def sweep():
    global PRICE
    ledger.reset_all()
    PRICE = run_once(payer="0xprobe", nonce="probe-0")["outcome"]["price_usd"]
    print(f"quote price = ${PRICE:.6f}   cap = ${CAP:.2f}   ledger = {ledger.db_path()}\n")
    fails = []
    sec_deterministic(fails)
    sec_encoding(fails)
    sec_onchain(fails)
    sec_recovery(fails)
    sec_concurrency(fails)
    print("\n" + "=" * 74)
    if fails:
        print(f"{len(fails)} invariant violation(s)\n")
        for scen, vid, msg in fails:
            print(f"  [{vid}] {scen}")
    else:
        print("all invariants held")
    return fails


def replay(spec):
    global PRICE
    _, _, ordinal = spec.partition(":")
    ordinal = int(ordinal)
    ledger.reset_all()
    PRICE = run_once(payer="0xprobe", nonce="probe-0")["outcome"]["price_usd"]
    obs = run_once(payer=f"0xc{ordinal}", nonce=f"c{ordinal}", trip=Trip(at=ordinal, kind=Crash))
    print(json.dumps({"replay": spec, "fired_at": obs["fired_at"],
                      "spent_delta": obs["after"][0] - obs["before"][0],
                      "held_delta": obs["after"][1] - obs["before"][1],
                      "settled": obs["settled"],
                      "status": (obs["outcome"] or {}).get("status"),
                      "violations": check(obs)}, indent=2, default=str))
    return check(obs)


if __name__ == "__main__":
    if "--replay" in sys.argv:
        sys.exit(1 if replay(sys.argv[sys.argv.index("--replay") + 1]) else 0)
    f = sweep()
    print(f"\ntemp ledger: {ledger.db_path()}  (delete when done)")
    sys.exit(1 if f else 0)
