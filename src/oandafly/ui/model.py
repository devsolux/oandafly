"""Presentation rules only; no broker calls, floating-point money, or authority."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


AUTHORITY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


def validate_authority(value: str) -> str:
    if not AUTHORITY_PATTERN.fullmatch(value):
        raise ValueError("Enter an enrolled authority identity (1–128 letters, digits, _, ., :, -).")
    return value


def status_view(body: dict[str, Any], authority: str, *, now: datetime | None = None) -> dict[str, str]:
    if body.get("authority_id") != authority:
        raise ValueError("The status belongs to another authority.")
    timestamp = datetime.fromisoformat(str(body.get("updated_at", "")).replace("Z", "+00:00"))
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Status timestamp has no timezone.")
    age = ((now or datetime.now(timezone.utc)) - timestamp).total_seconds()
    fresh = 0 <= age <= 15
    return {"freshness": "CURRENT OBSERVATION" if fresh else "STALE — readiness unknown",
            "environment": str(body.get("environment", "UNKNOWN")),
            "mode": str(body.get("mode", "UNKNOWN")),
            "safety": str(body.get("safety", "UNKNOWN")),
            "model": str(body.get("model_error", "UNKNOWN")),
            "updated": timestamp.isoformat()}


def command_confirmation(operation: str) -> str:
    messages = {
        "HALT": "Queue HALT: block new entries. Existing exposure is not automatically closed.",
        "FLATTEN": "Queue FLATTEN: request closing account exposure through the authorized worker. This can place orders.",
        "RESUME": "Queue RESUME: allow the worker to reevaluate entry authority. Only existing grants and limits apply.",
        "STOP": "Queue STOP: request orderly worker shutdown. This is NOT a flatten instruction; positions may remain open.",
    }
    if operation not in messages:
        raise ValueError("Unknown control operation")
    return messages[operation] + " Continue?"
