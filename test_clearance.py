"""Prove the clearance cert's security properties — offline, no model calls.

  * a valid cert clears its OWN action;
  * REPLAY: the cert does NOT clear a different action (content binding);
  * FORGERY: tampering the cert, or a wrong trusted issuer, fails the signature check;
  * a non-independent decision is never `cleared`;
  * REVOCATION: a target on the verified-flag commons fails clearance;
  * SETTLEMENT: verify_settlement=True requires settle_tx to have paid USDC to the seller
    on-chain — placeholder/reverted/wrong-payee/underpaid/missing all fail (mocked receipt).
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

    # 6) SETTLEMENT (opt-in) — settle_tx must have moved USDC to the seller on-chain.
    PAY_TO = "0x0000000000000000000000000000000000005e11"
    OTHER = "0x000000000000000000000000000000000000beef"
    TXOK = "0x" + "ab" * 32                       # a valid-shaped tx hash
    SETTLED = {"answer": "Paris", "tx": TXOK,
               "receipt": {"verified": True, "independent": True, "verifier_model": "zai-glm-4.7",
                           "generator_model": "gpt-oss-120b", "answer": "Paris", "settle_tx": TXOK}}
    scert = clearance.sign_clearance(REQ, SETTLED)
    usdc = clearance._USDC[clearance._network()]
    def _tt(addr): return "0x" + "00" * 12 + addr[2:]
    def receipt(status, to_addr, amount_usd):
        return {"status": status, "logs": [{"address": usdc,
                "topics": [clearance._TRANSFER_TOPIC, _tt(PAY_TO), _tt(to_addr)],
                "data": hex(int(amount_usd * 1_000_000))}]}
    def V(**kw):
        return clearance.verify_clearance(scert, REQ, trusted_issuer=scert["issuer"],
                                          verify_settlement=True, pay_to=PAY_TO, **kw)
    assert V(min_amount_usd=0.004, _receipt_fetch=lambda tx: receipt(1, PAY_TO, 0.004))["cleared"] is True
    v = clearance.verify_clearance(cert, REQ, trusted_issuer=issuer, verify_settlement=True,
                                   pay_to=PAY_TO, _receipt_fetch=lambda tx: receipt(1, PAY_TO, 1))
    assert v["cleared"] is False and any("valid on-chain tx hash" in r for r in v["reasons"]), v
    v = V(_receipt_fetch=lambda tx: receipt(0, PAY_TO, 0.004))
    assert v["cleared"] is False and any("reverted" in r for r in v["reasons"]), v
    v = V(_receipt_fetch=lambda tx: receipt(1, OTHER, 0.004))
    assert v["cleared"] is False and any("no USDC" in r for r in v["reasons"]), v
    v = V(min_amount_usd=1.00, _receipt_fetch=lambda tx: receipt(1, PAY_TO, 0.004))
    assert v["cleared"] is False and any("want >=" in r for r in v["reasons"]), v
    v = V(_receipt_fetch=lambda tx: None)
    assert v["cleared"] is False and any("no reachable on-chain receipt" in r for r in v["reasons"]), v
    print("[settle] on-chain settlement: confirmed OK; placeholder/reverted/wrong-payee/underpaid/missing all rejected")

    print("\nCLEARANCE OK — content-bound (no replay), signed (no forgery), independence-gated, "
          "revocable via the flag commons, settlement verifiable on-chain.")


if __name__ == "__main__":
    main()
