"""Bounded, origin-pinned, secret-redacted HTTP reads without retries."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import ssl
import time
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from oandafly.adapters.governor import ObservationGovernor
from oandafly.domain.errors import RateLimited, Unavailable, ValidationFailure
from oandafly.domain.primitives import utc_now
from oandafly.security.credentials import CredentialReference, NativeVault, token_bytes

MAX_DOCUMENT = 4 * 1024 * 1024
MAX_STREAM_LINE = 256 * 1024


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object member")
        result[key] = value
    return result


def _invalid_number(_: str) -> Any:
    raise ValueError("Non-finite JSON number")


def decode_document(raw: bytes, *, maximum: int = MAX_DOCUMENT) -> dict[str, Any]:
    if not raw or len(raw) > maximum:
        raise Unavailable("OANDA response exceeds document bounds or is empty")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_pairs,
            parse_float=Decimal,
            parse_constant=_invalid_number,
        )
        if not isinstance(value, dict):
            raise ValueError("Expected object")
        # JSON strings also have bounded aggregate size; cap shape independently.
        stack: list[tuple[Any, int]] = [(value, 0)]
        while stack:
            item, depth = stack.pop()
            if depth > 32:
                raise ValueError("Nested document limit")
            if isinstance(item, dict):
                stack.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                stack.extend((child, depth + 1) for child in item)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise Unavailable("OANDA response is malformed or violates the document contract") from None


@dataclass(frozen=True, slots=True)
class ReadDocument:
    raw: bytes
    body: dict[str, Any]
    headers: Mapping[str, str]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()


class BoundedHTTPReader:
    def __init__(
        self,
        *,
        origins: frozenset[str],
        credential: CredentialReference,
        vault: NativeVault,
        governor: ObservationGovernor,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        if not origins or any(not origin.startswith("https://") for origin in origins):
            raise ValidationFailure("Only fixed HTTPS service origins are permitted")
        if transport is not None and not isinstance(transport, httpx.MockTransport):
            raise ValidationFailure("Only explicit HTTP mock transports may replace the native transport")
        self.origins = origins
        self.credential = credential
        self.vault = vault
        self.governor = governor
        tls = ssl.create_default_context()
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        self._client = httpx.AsyncClient(
            transport=transport,
            verify=tls,
            trust_env=False,
            follow_redirects=False,
            http2=False,
            timeout=httpx.Timeout(10.0, connect=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _url(self, origin: str, path: str) -> str:
        if (
            origin not in self.origins
            or not path.startswith("/")
            or any(c in path for c in ("?", "#", "%", "\\"))
        ):
            raise ValidationFailure("Unapproved service origin or path")
        return origin + path

    async def _headers(self, deadline: float) -> dict[str, str]:
        secret = await asyncio.to_thread(self.vault.load, self.credential)
        value = token_bytes(secret).decode("ascii")
        if time.monotonic() >= deadline or self.credential.expires_at <= utc_now():
            raise Unavailable("Credential read exceeded its validity or command deadline")
        return {
            "Authorization": f"Bearer {value}",
            "Accept": "application/json",
            "Accept-Datetime-Format": "RFC3339",
            "Accept-Encoding": "identity",
        }

    async def _trace(self, name: str, info: dict[str, Any], deadline: float) -> None:
        if name == "connection.connect_tcp.started":
            await self.governor.acquire(kind="connection", deadline=deadline)

    @staticmethod
    def _status(response: httpx.Response) -> None:
        if response.status_code == 429:
            raise RateLimited("OANDA service quota rejected the request")
        if response.status_code != 200:
            # Never include body, URL, Authorization, request ID or upstream prose.
            raise Unavailable(f"OANDA read rejected with HTTP {response.status_code}")
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise Unavailable("Compressed service responses are outside the bounded transport contract")
        media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media not in {"application/json", "application/octet-stream", "application/x-ndjson"}:
            raise Unavailable("OANDA response has an unsupported content type")

    async def get(self, origin: str, path: str, *, params: Mapping[str, str] | None = None) -> ReadDocument:
        url = self._url(origin, path)
        deadline = time.monotonic() + 15

        async def trace(name: str, info: dict[str, Any]) -> None:
            await self._trace(name, info, deadline)

        try:
            async with asyncio.timeout_at(deadline):
                await self.governor.acquire(kind="request", deadline=deadline)
                headers = await self._headers(deadline)
                async with self._client.stream(
                    "GET", url, params=params, headers=headers, extensions={"trace": trace}
                ) as response:
                    self._status(response)
                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        if not content_length.isdecimal() or int(content_length) > MAX_DOCUMENT:
                            raise Unavailable("OANDA response exceeds its declared document bounds")
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(chunks) + len(chunk) > MAX_DOCUMENT:
                            raise Unavailable("OANDA response exceeds document bounds")
                        chunks.extend(chunk)
                    raw = bytes(chunks)
                    remaining = response.headers.get("X-Rate-Limit-Remaining", "")
                    safe_headers = (
                        {"remaining_quotes": remaining} if re.fullmatch(r"[0-9]{1,12}", remaining) else {}
                    )
                    return ReadDocument(raw=raw, body=decode_document(raw), headers=safe_headers)
        except (httpx.HTTPError, TimeoutError):
            raise Unavailable("OANDA read did not complete; historical coverage remains unverified") from None

    async def stream(self, origin: str, path: str, *, private: bool, params: Mapping[str, str] | None = None) -> AsyncGenerator[dict[str, Any], None]:
        url = self._url(origin, path)
        connect_deadline = time.monotonic() + 15

        async def trace(name: str, info: dict[str, Any]) -> None:
            await self._trace(name, info, connect_deadline)

        try:
            async with self.governor.stream(private=private):
                await self.governor.acquire(kind="request", deadline=connect_deadline)
                headers = await self._headers(connect_deadline)
                async with self._client.stream(
                    "GET",
                    url,
                    params=params,
                    headers=headers,
                    timeout=httpx.Timeout(10, connect=5),
                    extensions={"trace": trace},
                ) as response:
                    self._status(response)
                    buffer = bytearray()
                    # HTTPX buffers an explicit chunk_size until it is full.
                    # Consume arriving bytes so small live heartbeats are timely.
                    async for chunk in response.aiter_bytes():
                        if self.credential.expires_at <= utc_now():
                            raise Unavailable("Credential enrollment expired during stream")
                        # Bound each partial line before copying it; a transport
                        # chunk can contain many lines or one oversized record.
                        offset = 0
                        while offset < len(chunk):
                            end = chunk.find(b"\n", offset)
                            stop = len(chunk) if end < 0 else end
                            if len(buffer) + stop - offset > MAX_STREAM_LINE:
                                raise Unavailable("OANDA stream event exceeds line bounds")
                            buffer.extend(chunk[offset:stop])
                            if end < 0:
                                break
                            if buffer.strip():
                                yield decode_document(bytes(buffer), maximum=MAX_STREAM_LINE)
                            buffer.clear()
                            offset = end + 1
                    if buffer.strip():
                        raise Unavailable("OANDA stream ended with an incomplete event")
        except (httpx.HTTPError, TimeoutError):
            raise Unavailable("OANDA stream disconnected; REST catch-up is required") from None
