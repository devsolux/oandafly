"""Private owner-only durable control mailbox; no UI owns a trading scheduler."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import field_validator

from oandafly.domain.primitives import StrictModel, require_utc, utc_now
from oandafly.paths import application_directory, secure_directory, state_directory
from oandafly.security.attestations import private_document, write_private_json


class ControlRequest(StrictModel):
    format: Literal["oandafly.control.1"] = "oandafly.control.1"
    request_id: UUID
    authority_id: str
    operation: Literal["HALT", "FLATTEN", "RESUME", "STOP"]
    issued_at: datetime
    expires_at: datetime
    _utc = field_validator("issued_at", "expires_at")(require_utc)


def authority_storage_name(authority_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", authority_id):
        raise ValueError("Invalid authority identity")
    return hashlib.sha256(authority_id.encode("utf-8")).hexdigest()


def authority_directory(authority_id: str) -> Path:
    return secure_directory(state_directory() / "authorities" / authority_storage_name(authority_id))


def send_control(authority_id: str, operation: str) -> str:
    request = ControlRequest(request_id=uuid4(), authority_id=authority_id, operation=operation,
                             issued_at=utc_now(), expires_at=utc_now()+timedelta(seconds=60))
    path = secure_directory(authority_directory(authority_id) / "commands") / f"{request.request_id}.json"
    write_private_json(path, request)
    return str(request.request_id)


def read_status(authority_id: str) -> dict:
    import json
    # A status read never creates account directories or a database.
    path = application_directory() / "authorities" / authority_storage_name(authority_id) / "status.json"
    value = json.loads(private_document(path))
    if not isinstance(value, dict) or value.get("authority_id") != authority_id:
        raise ValueError("Invalid or cross-authority status document")
    return value
