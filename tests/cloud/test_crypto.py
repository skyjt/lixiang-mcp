"""Golden values computed with the fixed upstream pure functions on synthetic inputs.

No upstream secrets, captured device IDs, credentials or live HTTP are used.
"""

import base64
import hashlib

import bcrypt
import pytest
from nacl.signing import SigningKey

from lixiang_mcp.cloud.crypto import Signer, bcrypt_base64, create_proof, create_seed
from lixiang_mcp.cloud.transport import ProtocolError


def test_seed_proof_golden_and_independent_ed25519_verification():
    password = "synthetic-contract-password"
    challenge = {"option": "bcrypt$2a$04$", "snonce": "fixture:" + "ab" * 16, "seeded": "01" * 16}
    assert create_seed(password) == "a7153c9f45b3cd383914e37b409c48f9"
    assert bcrypt_base64(bytes.fromhex(challenge["seeded"])) == ".OC/.OC/.OC/.OC/.OC/.O"
    proof = create_proof(password, challenge, cnonce="cd" * 16)
    assert proof["proof"] == (
        "6770f1900321c437fc6a08a43fbb4cf7da8f7706e8ab7875ddffcb8d9a752936"
        "65307478c394c6554ac250137ec2bc76b57da56ae3d6952c0ec523bb65ef9200"
    )
    derived = bcrypt.hashpw(password.encode(), b"$2a$04$.OC/.OC/.OC/.OC/.OC/.O")
    verifier = SigningKey(hashlib.sha256(derived).digest()).verify_key
    message = hashlib.sha256(bytes.fromhex("ab" * 16 + "cd" * 16)).digest()
    verifier.verify(message, bytes.fromhex(proof["proof"]))
    # The alternate salted branch has embedded null bytes, as upstream explicitly strips them.
    challenge.pop("seeded")
    challenge["salted"] = b".OC/.OC/.OC/.OC/.OC/.O\x00".hex()
    assert create_proof(password, challenge, cnonce="cd" * 16) == proof


@pytest.mark.parametrize(
    "change",
    [
        {"option": "bcrypt$2a$31$"},
        {"option": "scrypt"},
        {"snonce": "not-hex"},
        {"seeded": "x"},
        {"salted": "ff" * 1000},
        {"salted": []},
        {"snonce": []},
    ],
)
def test_unknown_or_expensive_challenges_rejected(change):
    challenge = {"option": "bcrypt$2a$04$", "snonce": "fixture:" + "ab" * 16, "seeded": "01" * 16}
    challenge.update(change)
    # Empty salted is an upstream fallback, so force absence of seeded for that malformed case.
    if change.get("salted") == []:
        challenge.pop("seeded")
    with pytest.raises(ProtocolError, match="unsupported_login_challenge"):
        create_proof("synthetic-password", challenge)


def test_signer_golden_exact_bytes_and_trailing_newline(config):
    account = config.accounts[0].model_copy(
        update={
            "key_id": type(config.accounts[0].key_id)("fixture-key"),
            "device_id": type(config.accounts[0].device_id)("fixture-device"),
        }
    )
    profile = config.profile.model_copy(update={"sign_app_version": "fixture-version"})
    signer = Signer(profile, account)
    headers = signer.headers(
        "POST",
        b'{"sample":23}',
        bearer="synthetic-scoped",
        timestamp="1700000000000",
        nonce="fixture-nonce",
    )
    assert headers["X-CHJ-Sign"] == "Joc6LjBM62wRMQWiUy5mU3zxb+3qJRf2Ka7+/Z2BEwI="
    assert headers["Content-MD5"] == "Yj9Ptz6cCS7/iz5EOydtnQ=="
    empty = signer.headers("GET", b"", bearer="synthetic-scoped")
    assert empty["Content-MD5"] == base64.b64encode(hashlib.md5(b"").digest()).decode()
    changed = signer.headers(
        "POST",
        b'{"sample": 23}',
        bearer="synthetic-scoped",
        timestamp="1700000000000",
        nonce="fixture-nonce",
    )
    assert changed["X-CHJ-Sign"] != headers["X-CHJ-Sign"]
