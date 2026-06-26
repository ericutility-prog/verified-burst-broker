"""Prove the clearance cert's security properties — offline, no model calls.

  * a valid cert clears its OWN action;
  * REPLAY: the cert does NOT clear a different action (content binding);
  * FORGERY: tampering the cert, or a wrong trusted issuer, fails the signature check;
  * a non-independent decision is never `cleared`;
  * REVOCATION: a target on the verified-flag commons fails clearance.
"""
import os, tempfile
os.environ.setdefault("FLAGSTORE_DB", os.path.join(tempfile.gettempdir(), "vb_test_clearance_flags.db"))
os.environ["CLEARANCE_SIGNER_KEY"] = "0x" + "11" * 32   # fixed test authority key
import clearance
import flagstore

REQ = "What is the capital of France? Reply with one word."
GOOD = {"answer": "Paris", "tx": "0xSETTLE",
        "receipt": {"verified": True, "independent": True, "verifier_model": "zai-glm-4.7",
                    "generator_model": "gpt-oss-120b", "answer": "Paris", "settle_tx": "0xSETTLE"}}
NOT_INDEP = {"answer": "Paris", "tx": "0xSETTLE",
             "receipt": {"verified": True, "independent": False, "verifier_model": "gpt-oss-120b",
                         "generator_model": "gpt-oss-120b", "answer": "Paris"}}


def main():
    flagstore.reset_all()
    cert = clearance.sign_clearance(REQ, GOOD)
    issuer = cert["issuer"]
    assert cert["cleared"] is True
    print(f"[mint] cert issued by {issuer[:10]}…, cleared={cert['cleared']}")

    # 1) clears its own action, under the trusted issuer
    v = clearance.verify_clearance(cert, REQ, trusted_issuer=issuer)
    assert v["cleared"] is True, v
    print("[valid] cert clears its own action OK")

    # 2) REPLAY — same cert, different action -> rejected (content binding)
    v = clearance.verify_clearance(cert, "Transfer 5 ETH to 0xATTACKER. Approve? yes/no")
    assert v["cleared"] is False and any("content_hash" in r for r in v["reasons"]), v
    print(f"[replay] cert rejected for a different action OK -> {v['reasons'][0][:48]}…")

    # 3a) FORGERY — tamper a field after signing -> signature/binding fails
    forged = dict(cert); forged["answer"] = "London"
    v = clearance.verify_clearance(forged, REQ)
    assert v["cleared"] is False, v
    print("[forge] tampered cert rejected OK")

    # 3b) wrong trusted issuer -> rejected even if signature is internally valid
    v = clearance.verify_clearance(cert, REQ, trusted_issuer="0x000000000000000000000000000000000000dEaD")
    assert v["cleared"] is False and any("trusted clearance authority" in r for r in v["reasons"]), v
    print("[issuer] cert from an untrusted issuer rejected OK")

    # 4) non-independent decision -> never cleared
    c2 = clearance.sign_clearance(REQ, NOT_INDEP)
    assert c2["cleared"] is False
    v = clearance.verify_clearance(c2, REQ)
    assert v["cleared"] is False and any("independent" in r for r in v["reasons"]), v
    print("[indep] non-independent decision never cleared OK")

    # 5) REVOCATION — a flagged target fails clearance even with a valid cert
    BAD = "0xBADc0ffee0000000000000000000000000000abc"
    flagstore.record_verified_catch(BAD, "address", "drainer", {
        "independent": True, "verified": True, "verifier_model": "zai-glm-4.7", "receipt_id": "r"})
    v = clearance.verify_clearance(cert, REQ, trusted_issuer=issuer, target=BAD, kind="address")
    assert v["cleared"] is False and any("commons" in r for r in v["reasons"]), v
    print("[revoke] flagged target fails clearance OK")
    # and a clean target still clears
    v = clearance.verify_clearance(cert, REQ, trusted_issuer=issuer,
                                   target="0xC1ean000000000000000000000000000000000001", kind="address")
    assert v["cleared"] is True, v
    flagstore.reset_all()

    print("\nCLEARANCE OK — content-bound (no replay), signed (no forgery), independence-gated, "
          "revocable via the flag commons.")


if __name__ == "__main__":
    main()
