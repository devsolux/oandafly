"""Concrete fixed-origin v20 mutation transport. There are NO write retries.

The durable gateway owns the dispatch opportunity and immutable entry bytes.
Recovery invokes its final guard immediately before sending the exact native
TradeID close or OrderID cancel. A missing authoritative transaction response
remains UNKNOWN; HTTP status, request IDs and client extensions are not proof of
exactly-once broker execution.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

import httpx

from oandafly.adapters.endpoints import V20Endpoints
from oandafly.adapters.governor import ObservationGovernor
from oandafly.adapters.http import BoundedHTTPReader, MAX_DOCUMENT, decode_document
from oandafly.domain.capabilities import RuntimeMode
from oandafly.domain.errors import SafetyFence, TransportAmbiguous
from oandafly.domain.primitives import canonical_json, decimal_text, money
from oandafly.security.credentials import CredentialReference, NativeVault, account_segment
from oandafly.storage.ledger import native_id


class NativeExecutionAdapter:
    def __init__(self, *, environment: str, mode: RuntimeMode, credential: CredentialReference,
                 vault: NativeVault, governor: ObservationGovernor,
                 authorize: Callable[[str, str], None], transport: httpx.MockTransport | None = None) -> None:
        mode.require_environment(environment)
        if not mode.broker_mutation:
            raise SafetyFence("Read-only, shadow and simulation modes have no native mutation transport")
        credential.authorize(purpose="V20_EXECUTION", environment=environment)
        self.environment, self.mode, self.credential = environment, mode, credential
        self.endpoints = V20Endpoints.for_environment(environment)
        self._authorize = authorize
        self._http = BoundedHTTPReader(origins=frozenset({self.endpoints.rest}), credential=credential,
                                       vault=vault, governor=governor, transport=transport)
        self._prepared: dict[str, tuple[dict[str, str], float]] = {}

    def _scope(self, account_id: str, action: str) -> None:
        self.mode.require_environment(self.environment)
        self.credential.authorize(purpose="V20_EXECUTION", environment=self.environment, account_id=account_id)
        self._authorize(account_id, action)

    async def close(self) -> None:
        self._prepared.clear()
        await self._http.close()

    async def prepare_order(self, account_id: str) -> None:
        self._scope(account_id, "ENTRY")
        deadline = time.monotonic() + 10
        headers = await self._http._headers(deadline)
        self._prepared[account_id] = (headers, deadline)

    async def _send(self, method: str, path: str, body: bytes, headers: dict[str, str], deadline: float) -> Mapping[str, object]:
        if type(body) is not bytes or len(body) > 16384:
            raise SafetyFence("Native mutation bytes exceed their immutable payload boundary")
        async def trace(name: str, info: dict[str, Any]) -> None:
            await self._http._trace(name, info, deadline)
        headers = {**headers, "Content-Type": "application/json"}
        try:
            async with asyncio.timeout_at(deadline):
                async with self._http._client.stream(method, self.endpoints.rest + path,
                    headers=headers, content=body, extensions={"trace": trace}) as response:
                    if response.status_code not in {200, 201, 400, 404}:
                        raise TransportAmbiguous("Native mutation has no authoritative transaction response")
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise TransportAmbiguous("Compressed mutation response is outside resource bounds")
                    if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                        raise TransportAmbiguous("Native mutation response is not a transaction document")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(raw) + len(chunk) > MAX_DOCUMENT:
                            raise TransportAmbiguous("Native mutation response exceeds resource bounds")
                        raw.extend(chunk)
                    document = decode_document(bytes(raw))
                    if not any(isinstance(value, dict) and "id" in value and "type" in value
                               for value in document.values()):
                        raise TransportAmbiguous("HTTP completion alone does not resolve a mutation")
                    return document
        except (httpx.HTTPError, TimeoutError) as exc:
            raise TransportAmbiguous("Mutation outcome is unknown; native history reconciliation is required") from exc

    async def send_order_bytes(self, account_id: str, payload: bytes) -> Mapping[str, object]:
        self._scope(account_id, "ENTRY")
        prepared = self._prepared.pop(account_id, None)
        if prepared is None or time.monotonic() >= prepared[1]:
            raise SafetyFence("Entry has no current one-shot transport preparation")
        headers, deadline = prepared
        return await self._send("POST", f"/v3/accounts/{account_segment(account_id)}/orders", payload, headers, deadline)

    async def close_trade(self, account_id: str, trade_id: str, units: Decimal, *, before_send: Callable[[], None]) -> Mapping[str, object]:
        self._scope(account_id, "RECOVERY")
        native_id(trade_id)
        if money(units) <= 0:
            raise SafetyFence("Native close requires an exact positive trade quantity")
        body = canonical_json({"units": decimal_text(units)}).encode("utf-8")
        deadline = time.monotonic() + 10
        headers = await self._http._headers(deadline)
        self._scope(account_id, "RECOVERY")
        before_send()
        return await self._send("PUT", f"/v3/accounts/{account_segment(account_id)}/trades/{trade_id}/close", body, headers, deadline)

    async def cancel_order(self, account_id: str, order_id: str, *, before_send: Callable[[], None]) -> Mapping[str, object]:
        self._scope(account_id, "RECOVERY")
        native_id(order_id)
        deadline = time.monotonic() + 10
        headers = await self._http._headers(deadline)
        self._scope(account_id, "RECOVERY")
        before_send()
        return await self._send("PUT", f"/v3/accounts/{account_segment(account_id)}/orders/{order_id}/cancel", b"", headers, deadline)
