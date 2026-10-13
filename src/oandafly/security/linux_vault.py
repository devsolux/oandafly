"""Explicit Linux native Secret Service or systemd runtime credential backends.

No plaintext disk fallback and no generic keyring plugin discovery. systemd
credentials are provisioned outside this process, preferably with encrypted
LoadCredentialEncrypted=; the worker only reads the OS-managed runtime mount.
"""
from __future__ import annotations

import hashlib
import importlib
import os
import sys
from pathlib import Path

from pydantic import SecretStr

from oandafly.domain.errors import SafetyFence, Unavailable
from oandafly.security.attestations import private_document
from oandafly.security.credentials import CredentialReference, token_bytes


class SecretServiceVault:
    def __init__(self) -> None:
        if sys.platform != "linux":
            raise Unavailable("Secret Service is only supported on Linux")
        try:
            backend_type = importlib.import_module("keyring.backends.SecretService").Keyring
            self.backend = backend_type()
            if self.backend.priority <= 0:
                raise Unavailable("Native Secret Service is unavailable")
        except Exception as exc:
            raise Unavailable("An unlocked Linux Secret Service is required; no file fallback is allowed") from exc

    def store(self, reference: CredentialReference, secret: SecretStr) -> None:
        token_bytes(secret)
        try:
            self.backend.set_password(reference.vault_key, reference.authority_id, secret.get_secret_value())
        except Exception as exc:
            raise Unavailable("Native Secret Service denied credential storage") from exc

    def load(self, reference: CredentialReference) -> SecretStr:
        try:
            value = self.backend.get_password(reference.vault_key, reference.authority_id)
            if value is None:
                raise Unavailable("Native credential is missing")
            secret = SecretStr(value)
            token_bytes(secret)
            return secret
        except Exception as exc:
            raise Unavailable("Native Secret Service denied credential access") from exc

    def delete(self, reference: CredentialReference) -> None:
        try:
            if self.backend.get_password(reference.vault_key, reference.authority_id) is not None:
                self.backend.delete_password(reference.vault_key, reference.authority_id)
        except Exception as exc:
            raise Unavailable("Native Secret Service denied credential deletion") from exc


class SystemdCredentialVault:
    @staticmethod
    def credential_name(reference: CredentialReference) -> str:
        return "oandafly-" + hashlib.sha256(reference.vault_key.encode("utf-8")).hexdigest()

    def load(self, reference: CredentialReference) -> SecretStr:
        if sys.platform != "linux":
            raise Unavailable("systemd credentials require Linux")
        directory = Path(os.environ.get("CREDENTIALS_DIRECTORY", ""))
        if not directory.is_absolute() or not directory.is_relative_to("/run/credentials"):
            raise SafetyFence("No systemd-managed runtime credential directory is present")
        raw = private_document(directory / self.credential_name(reference), maximum=4096)
        try:
            secret = SecretStr(raw.decode("ascii"))
            token_bytes(secret)
            return secret
        except (ValueError, UnicodeError) as exc:
            raise Unavailable("OS runtime credential is malformed") from exc

    def store(self, reference: CredentialReference, secret: SecretStr) -> None:
        raise Unavailable("Provision encrypted systemd credentials outside the trading process")

    def delete(self, reference: CredentialReference) -> None:
        raise Unavailable("Retire systemd credentials through the service deployment configuration")
