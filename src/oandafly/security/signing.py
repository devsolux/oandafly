"""Offline approval tooling. Signing authenticates a claim; it does not verify it."""
from __future__ import annotations
import base64
import json
import os
from getpass import getpass
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from oandafly.paths import secure_directory
from oandafly.security.attestations import SignedDocument, private_document, write_private_json


def create_key(output: Path) -> str:
    password = getpass("New approval key passphrase (not echoed): ")
    if len(password) < 16 or password != getpass("Repeat passphrase: "):
        raise ValueError("Use matching passphrases of at least 16 characters")
    key = Ed25519PrivateKey.generate()
    encoded = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.BestAvailableEncryption(password.encode()))
    secure_directory(output.parent)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def sign_claim(*, key_path: Path, payload_path: Path, output: Path, key_id: str, role: str) -> str:
    key = serialization.load_pem_private_key(private_document(key_path),
        password=getpass("Approval key passphrase (not echoed): ").encode())
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Only Ed25519 approval keys are supported")
    payload = json.loads(private_document(payload_path))
    document = SignedDocument.from_wire({"key_id": key_id, "role": role,
        "payload": payload, "signature_b64": base64.b64encode(bytes(64)).decode()})
    signature = base64.b64encode(key.sign(document.signing_bytes())).decode()
    signed = SignedDocument.from_wire({**document.model_dump(mode="json"), "signature_b64": signature})
    write_private_json(output, signed)
    return signed.digest
