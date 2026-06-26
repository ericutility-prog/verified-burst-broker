"""Prove the verified-flag store's security properties — offline, no model calls.

  * poisoning resistance: a flag with NO independent-verified receipt is REJECTED;
  * admission: an independent+verified receipt is admitted;
  * free lookup: check_known/guard block a known-bad target with no burst;
  * normalization: a case-different spelling of the same address still matches;
  * corroboration: repeat catches accumulate (append-only).
"""
import os, tempfile
os.environ.setdefault("FLAGSTORE_DB", os.path.join(tempfile.gettempdir(), "vb_test_flagstore.db"))
import flagstore


# receipts shaped exactly like broker burst receipts
VERIFIED = {"independent": True, "verified": True, "verifier_model": "zai-glm-4.7",
            "receipt_id": "rcpt-1"}
NOT_INDEPENDENT = {"independent": False, "verified": True, "verifier_model": "self"}
NOT_VERIFIED = {"independent": True, "verified": False, "verifier_model": "zai-glm-4.7"}
BAD_ADDR = "0xBADc0ffee0000000000000000000000000000001"


def main():
    flagstore.reset_all()

    # 1) poisoning resistance: unverified / non-independent claims are NOT writable
    assert flagstore.record_verified_catch(BAD_ADDR, "address", "claim", {}) is False
    assert flagstore.record_verified_catch(BAD_ADDR, "address", "claim", NOT_INDEPENDENT) is False
    assert flagstore.record_verified_catch(BAD_ADDR, "address", "claim", NOT_VERIFIED) is False
    assert flagstore.count() == 0
    print("[poison] unverified / non-independent flags REJECTED (count=0) OK")

    # 2) admission: an independently-verified catch is written
    assert flagstore.record_verified_catch(
        BAD_ADDR, "address", "drainer contract", VERIFIED, severity="critical") is True
    assert flagstore.count() == 1
    print("[admit] independent+verified catch ADMITTED OK")

    # 3) free lookup blocks it — and a DIFFERENT-CASE spelling still matches
    hit = flagstore.check_known(BAD_ADDR.upper(), "address")
    assert hit and hit["known_bad"] and hit["severity"] == "critical", hit
    g = flagstore.guard(BAD_ADDR.lower(), "address")
    assert g["action"] == "hold" and g["known_bad"], g
    print(f"[lookup] known-bad blocked free (action=hold, matched case-insensitively, "
          f"reason={hit['reason']!r}) OK")

    # 4) unknown target -> proceed (unknown != safe, but not flagged)
    g2 = flagstore.guard("0xCLEAN0000000000000000000000000000000001", "address")
    assert g2["action"] == "proceed" and not g2["known_bad"]
    print("[lookup] unknown target -> proceed (not in store) OK")

    # 5) corroboration: a second verified catch on the same target accumulates
    flagstore.record_verified_catch(BAD_ADDR, "address", "seen again", VERIFIED)
    hit2 = flagstore.check_known(BAD_ADDR, "address")
    assert hit2["times_flagged"] == 2, hit2
    print(f"[corroborate] repeat catch accumulates (times_flagged={hit2['times_flagged']}) OK")

    # 6) kind namespacing: same string under a different kind is a different target
    assert flagstore.check_known(BAD_ADDR, "url") is None
    print("[namespace] same string under a different kind is distinct OK")

    print("\nFLAG STORE OK — verified-only admission (no poisoning), free case-insensitive "
          "lookup, corroboration, kind-namespaced.")


if __name__ == "__main__":
    main()
