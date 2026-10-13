"""Data-only Ed25519 approvals with explicit signer roles and revocation.

Trust is established by a deployment operator, outside training and inference.
A signature authenticates an assertion; it does not establish its scientific or
legal truth. The issuer must actually perform the checks described by the report.
"""
from __future__ import annotations

import base64
import hashlib
import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import Field, field_validator, model_validator

from oandafly.domain.errors import SafetyFence
from oandafly.domain.primitives import StrictModel, canonical_json, require_utc, utc_now

Role = Literal["ACCOUNT_ELIGIBILITY", "TRADING_AUTHORIZATION", "DATASET_REVIEW", "MODEL_VALIDATION", "DEPLOYMENT"]
MAX_APPROVAL_BYTES = 256 * 1024


def private_document(path: Path, *, maximum: int = MAX_APPROVAL_BYTES) -> bytes:
    """Read a bounded unaliased owner-only document without following symlinks."""
    path = path.absolute()
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise SafetyFence("Approval paths must not contain symbolic links")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
            raise SafetyFence("Approval is not a bounded regular file")
        if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise SafetyFence("Approval documents must be private to their owner (0600)")
        data = bytearray()
        while len(data) <= maximum:
            chunk = os.read(fd, min(65536, maximum + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(fd)
        if len(data) > maximum or (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise SafetyFence("Approval changed during bounded read")
        return bytes(data)
    finally:
        os.close(fd)


class TrustedSigner(StrictModel):
    key_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    role: Role
    public_key_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid_from: datetime
    valid_until: datetime

    _utc = field_validator("valid_from", "valid_until")(require_utc)


class TrustPolicy(StrictModel):
    format: Literal["oandafly.trust.1"] = "oandafly.trust.1"
    keys: tuple[TrustedSigner, ...] = Field(min_length=1, max_length=64)
    revoked_key_ids: tuple[str, ...] = ()
    revoked_document_sha256: tuple[str, ...] = ()

    @model_validator(mode="after")
    def separation(self) -> TrustPolicy:
        if len({k.key_id for k in self.keys}) != len(self.keys):
            raise ValueError("Key IDs must be unique")
        # Prevent a training/validation key being reused as an execution grant key.
        by_key: dict[str, set[str]] = {}
        for key in self.keys:
            if key.valid_until <= key.valid_from:
                raise ValueError("Invalid signer validity interval")
            by_key.setdefault(key.public_key_hex, set()).add(key.role)
        if any(len(roles) > 1 for roles in by_key.values()):
            raise ValueError("Each approval role requires a distinct public key")
        return self


class SignedDocument(StrictModel):
    format: Literal["oandafly.approval.1"] = "oandafly.approval.1"
    key_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    role: Role
    payload: dict[str, Any]
    signature_b64: str = Field(min_length=80, max_length=100)

    def signing_bytes(self) -> bytes:
        return canonical_json({"format": self.format, "key_id": self.key_id,
                               "role": self.role, "payload": self.payload}).encode("utf-8")

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.signing_bytes()).hexdigest()


class ApprovalVerifier:
    def __init__(self, policy_path: Path) -> None:
        self.policy_path = policy_path

    def verify(self, path: Path, *, role: Role, now: datetime | None = None) -> SignedDocument:
        # Re-read the trust policy: revocation must take effect without restarting.
        current = require_utc(now or utc_now())
        policy = TrustPolicy.model_validate_json(private_document(self.policy_path))
        document = SignedDocument.model_validate_json(private_document(path))
        if document.role != role:
            raise SafetyFence("Approval has the wrong role")
        if document.key_id in policy.revoked_key_ids or document.digest in policy.revoked_document_sha256:
            raise SafetyFence("Approval has been revoked")
        key = next((key for key in policy.keys if key.key_id == document.key_id and key.role == role), None)
        if key is None or not key.valid_from <= current < key.valid_until:
            raise SafetyFence("No currently trusted signer for this approval")
        try:
            signature = base64.b64decode(document.signature_b64, validate=True)
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(key.public_key_hex)).verify(
                signature, document.signing_bytes(),
            )
        except (InvalidSignature, ValueError) as exc:
            raise SafetyFence("Approval signature is invalid") from exc
        return document

    def require_report(self, path: Path, *, role: Role, artifact_sha256: str) -> dict[str, Any]:
        payload = self.verify(path, role=role).payload
        if payload.get("artifact_sha256") != artifact_sha256:
            raise SafetyFence("Approval is bound to a different artifact")
        return payload


def write_private_json(path: Path, value: object, *, replace: bool = False) -> None:
    """Atomic metadata persistence. Never use this for plaintext credentials."""
    from uuid import uuid4
    from oandafly.paths import secure_directory
    secure_directory(path.parent)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        body = canonical_json(value).encode("utf-8")
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            if path.is_symlink():
                raise SafetyFence("Cannot replace symlink metadata")
            os.replace(temporary, path)
        else:
            # Link is atomic and refuses to overwrite; unlink temporary immediately.
            os.link(temporary, path)
            temporary.unlink()
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
