"""Executable transport contracts; every broker response and secret is synthetic."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from oandafly.adapters.endpoints import V20Endpoints
from oandafly.adapters.governor import LocalRateGovernor
from oandafly.adapters.http import MAX_STREAM_LINE, BoundedHTTPReader, decode_document
from oandafly.adapters.rates import HistoricalFixingQuery, RatesAdapter, RatesEntitlement
from oandafly.adapters.v20 import V20ReadAdapter
from oandafly.domain.errors import Conflict, RateLimited, SafetyFence, Unavailable, ValidationFailure
from oandafly.security.credentials import CredentialReference, token_bytes
from oandafly.security.vault import MacKeychainVault, WindowsCredentialVault, native_vault
from tests.support import supported_database

ACCOUNT = "001-001-1234567-001"
NOW = datetime.now(timezone.utc)
CUT = datetime(2025, 1, 3, 23, 45, tzinfo=timezone.utc)
TEST_SECRET = "synthetic-test-token-never-issued-by-oanda"


async def test_small_heartbeat_arrives_before_next_network_chunk(db):
    # A quiet live stream must deliver each newline immediately, without filling 16 KiB.
    release = asyncio.Event()

    class PausedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"type":"HEARTBEAT","lastTransactionID":"500"}\n'
            await release.wait()

    instance = adapter(db, lambda request: httpx.Response(200,
        headers={"content-type": "application/octet-stream"}, stream=PausedStream()))
    events = instance.transactions_stream(ACCOUNT)
    try:
        first = await asyncio.wait_for(anext(events), 0.5)
        assert first["lastTransactionID"] == "500"
    finally:
        release.set()
        await events.aclose()
        await instance.close()
    with db.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM egress_streams").fetchone()[0] == 0


@pytest.mark.parametrize("streaming", [False, True])
async def test_service_rate_limit_is_typed_redacted_and_not_retried(db, streaming):
    # A provider 429 never triggers hidden retries or retains a private stream slot.
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "60"}, text=TEST_SECRET)

    instance = adapter(db, handler)
    try:
        with pytest.raises(RateLimited) as error:
            if streaming:
                _ = [event async for event in instance.transactions_stream(ACCOUNT)]
            else:
                await instance.since_id(ACCOUNT, "0")
        assert len(calls) == 1 and TEST_SECRET not in str(error.value)
        with db.read() as connection:
            assert connection.execute("SELECT COUNT(*) FROM egress_streams").fetchone()[0] == 0
    finally:
        await instance.close()


async def test_fragmented_stream_disconnect_requires_rest_catchup_without_double_cash(db):
    # Wire fragments and a lost connection exercise the real parser and ledger;
    # only subsequent unfiltered REST coverage establishes a durable frontier.
    from oandafly.storage.ledger import Ledger

    ledger = Ledger(db)
    ledger.establish_baseline("SANDBOX", ACCOUNT, "EUR", Decimal("100"), "0", NOW)
    fact = {"id": "7", "accountID": ACCOUNT, "type": "DAILY_FINANCING", "financing": "-2", "accountBalance": "98"}

    class BrokenStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            raw = json.dumps(fact).encode() + b"\n"
            for offset in range(0, len(raw), 3):
                yield raw[offset:offset + 3]
            raise httpx.ReadError(TEST_SECRET)

        async def aclose(self):
            self.closed = True

    wire = BrokenStream()

    def handler(request):
        if request.url.path.endswith("/stream"):
            return httpx.Response(200, headers={"content-type": "application/octet-stream"}, stream=wire)
        assert dict(request.url.params) == {"id": "0"}
        return httpx.Response(200, json={"transactions": [fact], "lastTransactionID": "7"})

    instance = adapter(db, handler)
    try:
        with pytest.raises(Unavailable, match="REST catch-up") as error:
            async for event in instance.transactions_stream(ACCOUNT):
                ledger.ingest("SANDBOX", ACCOUNT, event)
        assert TEST_SECRET not in str(error.value) and wire.closed
        observed = ledger.balance("SANDBOX", ACCOUNT)
        assert observed.balance == 98 and observed.covered_cursor == "0" and not observed.history_caught_up
        history = await instance.since_id(ACCOUNT, "0")
        covered = ledger.apply_history("SANDBOX", ACCOUNT, history.from_transaction_id,
            history.transactions, history.last_transaction_id, unfiltered=history.unfiltered)
        assert covered.balance == 98 and covered.history_caught_up
        with db.read() as connection:
            assert connection.execute("SELECT COUNT(*) FROM egress_streams").fetchone()[0] == 0
    finally:
        await instance.close()


@pytest.mark.parametrize("cut", [0, None, [], {}])
async def test_malformed_rates_cutoff_returns_typed_failure_with_audit(db, cut):
    # Arbitrary upstream shapes cannot escape as AttributeError or claim provenance.
    body = rate_body()
    body["meta"]["effective_params"]["date_time"] = cut
    instance = rates_adapter(db, rates_handler(body))
    try:
        with pytest.raises(Unavailable):
            await instance.historical(query())
        with db.read() as connection:
            assert connection.execute("SELECT status FROM rates_audit_queries").fetchone()[0] == "UNVERIFIED_OR_UNKNOWN"
    finally:
        await instance.close()


class TestVault:
    """Test-only injectable vault. This class is outside the distributed package."""

    __test__ = False

    def store(self, reference: CredentialReference, secret: SecretStr) -> None:
        token_bytes(secret)

    def load(self, reference: CredentialReference) -> SecretStr:
        return SecretStr(TEST_SECRET)

    def delete(self, reference: CredentialReference) -> None:
        self.deleted = reference.credential_id


@pytest.fixture
def db(tmp_path: Path):
    database = supported_database(tmp_path / "state.sqlite3")
    yield database
    database.close()


def credential(purpose="V20_OBSERVATION", **changes) -> CredentialReference:
    values = dict(
        credential_id=uuid4(),
        authority_id="authority",
        environment="SANDBOX",
        purpose=purpose,
        account_ids=(ACCOUNT,),
        expires_at=NOW + timedelta(days=1),
    )
    if purpose == "RATES_OBSERVATION":
        values.update(account_ids=(), entitlement_evidence_sha256="1" * 64)
    values.update(changes)
    return CredentialReference(**values)


def adapter(db, handler, ref=None) -> V20ReadAdapter:
    return V20ReadAdapter(
        environment="SANDBOX",
        credential=ref or credential(),
        vault=TestVault(),
        governor=LocalRateGovernor(db),
        transport=httpx.MockTransport(handler),
    )


def query(**changes) -> HistoricalFixingQuery:
    values = dict(base_currency="EUR", quote_currency="USD", business_date=CUT.date(), cutoff=CUT)
    values.update(changes)
    return HistoricalFixingQuery(**values)


def rates_adapter(db, handler, limit=20, **entitlement_changes) -> RatesAdapter:
    ref = credential("RATES_OBSERVATION")
    values = dict(
        credential_id=ref.credential_id,
        evidence_sha256="1" * 64,
        period_id="fixture-period",
        period_start=NOW - timedelta(days=1),
        period_end=NOW + timedelta(days=1),
        quote_limit=limit,
        intraday_history_allowed=True,
    )
    values.update(entitlement_changes)
    return RatesAdapter(
        db=db,
        credential=ref,
        entitlement=RatesEntitlement(**values),
        vault=TestVault(),
        governor=LocalRateGovernor(db, "OANDA_RATES"),
        transport=httpx.MockTransport(handler),
    )


def rate_body(codes=("USD",), source="2025-01-03T15:00:00Z", **changes):
    body = {
        "meta": {
            "effective_params": {
                "data_set": "EUCB",
                "base_currencies": ["EUR"],
                "quote_currencies": list(codes),
                "date_time": CUT.isoformat(),
            },
            "endpoint": "spot",
            "request_time": NOW.isoformat(),
            "skipped_currency_pairs": [],
        },
        "quotes": [
            {
                "base_currency": "EUR",
                "quote_currency": code,
                "bid": "2" if code == "USD" else "4",
                "ask": "2" if code == "USD" else "4",
                "midpoint": "2" if code == "USD" else "4",
                "source_date": source,
            }
            for code in codes
        ],
    }
    body.update(changes)
    return body


def rates_handler(body):
    def handler(request):
        if request.url.path == "/v2/datasets/EUCB.json":
            return httpx.Response(
                200,
                json={
                    "datasets": [
                        {
                            "code": "EUCB",
                            "cross_currency": "EUR",
                            "description": "Synthetic contract fixture",
                            "quotables": ["EUR/USD", "EUR/JPY"],
                        }
                    ]
                },
            )
        return httpx.Response(200, json=body, headers={"X-Rate-Limit-Remaining": "100"})

    return handler


def test_exact_origins():
    assert V20Endpoints.for_environment("LIVE").rest == "https://api-fxtrade.oanda.com"
    assert V20Endpoints.for_environment("SANDBOX").stream == "https://stream-fxpractice.oanda.com"
    with pytest.raises(ValidationFailure):
        V20Endpoints.for_environment("PRACTICE")


@pytest.mark.parametrize("body", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b"[]", b"{}x", b"\xff"])
def test_json_ambiguity_rejected(body):
    with pytest.raises(Unavailable):
        decode_document(body)


def test_nested_json_limit():
    with pytest.raises(Unavailable):
        decode_document(b'{"x":' * 34 + b"1" + b"}" * 34)


@pytest.mark.parametrize("value", ["", "x" * 4097, "synthetic-token\r\ninjected", "a b" * 20])
def test_token_header_injection_rejected(value):
    with pytest.raises(ValidationFailure):
        token_bytes(SecretStr(value))


def test_opaque_bearer_is_not_assumed_jwt():
    assert token_bytes(SecretStr(TEST_SECRET)) == TEST_SECRET.encode()
    assert TEST_SECRET not in repr(SecretStr(TEST_SECRET))


@pytest.mark.parametrize("scope", ["../../secret", ACCOUNT + "/orders", ACCOUNT + "%2forders", "", "a?b"])
def test_account_path_injection_rejected(scope):
    with pytest.raises((ValueError, ValidationFailure)):
        credential(account_ids=(scope,))


def test_purpose_and_environment_capabilities():
    ref = credential()
    with pytest.raises(SafetyFence):
        ref.authorize(purpose="V20_EXECUTION", environment="SANDBOX", account_id=ACCOUNT)
    with pytest.raises(SafetyFence):
        ref.authorize(purpose="V20_OBSERVATION", environment="LIVE", account_id=ACCOUNT)
    with pytest.raises(SafetyFence):
        ref.authorize(purpose="V20_OBSERVATION", environment="SANDBOX", account_id="other")
    assert ref.vault_key != ref.model_copy(update={"environment": "LIVE"}).vault_key


def test_native_vault_absence_is_explicit():
    if sys.platform not in {"darwin", "win32"}:
        with pytest.raises(Unavailable):
            native_vault()
    if sys.platform != "darwin":
        with pytest.raises(Unavailable):
            MacKeychainVault()
    if sys.platform != "win32":
        with pytest.raises(Unavailable):
            WindowsCredentialVault()


async def test_enrolled_account_discovery_does_not_expand_scope(db):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"accounts": [{"id": ACCOUNT}, {"id": "other"}]})

    instance = adapter(db, handler)
    try:
        assert await instance.accounts() == (ACCOUNT,)
        assert requests[0].headers["Authorization"] == "Bearer " + TEST_SECRET
        assert requests[0].url.host == "api-fxpractice.oanda.com"
        assert not requests[0].url.query
    finally:
        await instance.close()


async def test_revoked_subaccount_membership_fences(db):
    instance = adapter(db, lambda request: httpx.Response(200, json={"accounts": []}))
    try:
        with pytest.raises(SafetyFence):
            await instance.accounts()
    finally:
        await instance.close()


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 404, 500])
async def test_response_errors_never_follow_redirects_or_leak_tokens(db, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"location": "https://attacker.invalid/"}, text=TEST_SECRET)

    instance = adapter(db, handler)
    try:
        with pytest.raises(Unavailable) as error:
            await instance.accounts()
        assert TEST_SECRET not in str(error.value)
        assert len(calls) == 1
    finally:
        await instance.close()


async def test_network_timeout_is_not_retried(db):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout(TEST_SECRET, request=request)

    instance = adapter(db, handler)
    try:
        with pytest.raises(Unavailable) as error:
            await instance.accounts()
        assert TEST_SECRET not in str(error.value)
        assert len(calls) == 1
    finally:
        await instance.close()


async def test_sparse_sinceid_is_unfiltered_and_account_bound(db):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "lastTransactionID": "120",
                "transactions": [
                    {"id": "103", "accountID": ACCOUNT, "type": "DAILY_FINANCING", "financing": "-2"},
                    # Complete unfiltered history includes its declared head.
                    {"id": "120", "accountID": ACCOUNT, "type": "ORDER_FILL", "instrument": "XAU_USD"},
                ],
            },
        )

    instance = adapter(db, handler)
    try:
        result = await instance.since_id(ACCOUNT, "100")
        assert result.unfiltered and result.last_transaction_id == "120"
        assert [row["id"] for row in result.transactions] == ["103", "120"]
        assert dict(requests[0].url.params) == {"id": "100"}
        assert result.transactions[1]["instrument"] == "XAU_USD"  # retained for quarantine/cash accounting.
    finally:
        await instance.close()


@pytest.mark.parametrize(
    "rows,cut",
    [
        ([{"id": "101", "accountID": "other"}], "101"),
        ([{"id": "100", "accountID": ACCOUNT}], "101"),
        ([{"id": "103", "accountID": ACCOUNT}], "102"),
        ([], "99"),
        ([], "101"),  # Successful HTTP is insufficient proof of complete history.
        ([{"id": "101", "accountID": ACCOUNT}], "102"),
        ([{"id": "101", "accountID": ACCOUNT}] * 2, "102"),
    ],
)
async def test_invalid_history_fences(db, rows, cut):
    instance = adapter(
        db, lambda request: httpx.Response(200, json={"transactions": rows, "lastTransactionID": cut})
    )
    try:
        with pytest.raises(SafetyFence):
            await instance.since_id(ACCOUNT, "100")
    finally:
        await instance.close()


async def test_private_stream_fresh_connection_no_history_query(db):
    seen = []

    def handler(request):
        seen.append(request)
        content = json.dumps({"type": "HEARTBEAT", "lastTransactionID": "500"}).encode() + b"\n"
        return httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=content)

    instance = adapter(db, handler)
    try:
        events = [event async for event in instance.transactions_stream(ACCOUNT)]
        assert events[0]["lastTransactionID"] == "500"
        assert seen[0].url.host == "stream-fxpractice.oanda.com"
        assert seen[0].url.query == b""
        with db.read() as connection:
            assert connection.execute("SELECT COUNT(*) FROM egress_streams").fetchone()[0] == 0
    finally:
        await instance.close()


async def test_stream_oversize_cleans_lease(db):
    instance = adapter(
        db,
        lambda request: httpx.Response(
            200, headers={"content-type": "application/octet-stream"}, content=b"x" * (MAX_STREAM_LINE + 1)
        ),
    )
    try:
        with pytest.raises(Unavailable):
            _ = [event async for event in instance.transactions_stream(ACCOUNT)]
        with db.read() as connection:
            assert connection.execute("SELECT COUNT(*) FROM egress_streams").fetchone()[0] == 0
    finally:
        await instance.close()


async def test_instrument_universe_filters_non_currency_and_metal_names(db):
    row = dict(
        name="EUR_USD",
        type="CURRENCY",
        tradeUnitsPrecision=0,
        displayPrecision=5,
        pipLocation=-4,
        minimumTradeSize="1",
        maximumOrderUnits="100000",
        marginRate="0.05",
    )
    rows = [row, dict(row, name="XAU_USD"), dict(row, name="BTC_USD"), dict(row, name="GBP_USD", type="CFD")]
    instance = adapter(db, lambda request: httpx.Response(200, json={"instruments": rows}))
    try:
        result = await instance.instruments(ACCOUNT)
        assert [item.name for item in result.admitted] == ["EUR_USD"]
        assert set(result.excluded) == {"XAU_USD", "BTC_USD", "GBP_USD"}
    finally:
        await instance.close()


async def test_account_nav_and_cash_are_distinct_from_notional(db):
    row = dict(
        id=ACCOUNT,
        currency="EUR",
        balance="100",
        unrealizedPL="-3",
        NAV="97",
        marginUsed="10",
        marginAvailable="80",
        marginCloseoutPercent="0.1",
        lastTransactionID="3",
        trades=[],
        positions=[{"instrument": "XAU_USD"}],
        orders=[],
    )
    instance = adapter(
        db, lambda request: httpx.Response(200, json={"account": row, "lastTransactionID": "3"})
    )
    try:
        result = await instance.account(ACCOUNT)
        assert result.summary.nav == Decimal("97")
        assert result.summary.margin_available == Decimal("80")  # never redefined as NAV-marginUsed.
        assert result.unsupported_instruments == ("XAU_USD",)
    finally:
        await instance.close()


def test_shared_token_bucket_and_clock_fence(db):
    clock = [1_000_000_000]
    a = LocalRateGovernor(db, monotonic_ns=lambda: clock[0])
    b = LocalRateGovernor(db, monotonic_ns=lambda: clock[0])
    assert a.try_acquire() == 0
    assert b.try_acquire() > 0
    clock[0] += 12_000_000
    assert b.try_acquire() == 0
    clock[0] = 1
    with pytest.raises(SafetyFence):
        a.try_acquire()
    clock[0] = 10_000_000_000
    with pytest.raises(SafetyFence):
        b.try_acquire()


def test_shared_quota_policy_cannot_expand(db):
    LocalRateGovernor(db, requests_per_second=10)
    with pytest.raises(Conflict):
        LocalRateGovernor(db, requests_per_second=90)


async def test_rate_deadline_rejected_without_wait(db):
    governor = LocalRateGovernor(db)
    with pytest.raises(RateLimited):
        await governor.acquire(deadline=time.monotonic() - 1)
    with pytest.raises(ValidationFailure):
        await governor.acquire(deadline=float("inf"))


async def test_private_stream_reservation_and_cancellation(db):
    governor = LocalRateGovernor(db)
    leases = [governor._claim_stream(False) for _ in range(18)]
    with pytest.raises(RateLimited):
        governor._claim_stream(False)
    leases.extend(governor._claim_stream(True) for _ in range(2))
    with pytest.raises(RateLimited):
        governor._claim_stream(True)
    for lease in leases:
        governor._release_stream(lease)

    async def task():
        async with governor.stream(private=True):
            await asyncio.Event().wait()

    running = asyncio.create_task(task())
    await asyncio.sleep(0)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    with db.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM egress_streams").fetchone()[0] == 0


@pytest.mark.parametrize(
    "changes",
    [
        dict(base_currency="XAU"),
        dict(quote_currency="BTC"),
        dict(data_set="CRYPTO"),
        dict(base_currency="USD", quote_currency="JPY"),
        dict(cutoff=CUT.replace(minute=44)),
        dict(cutoff=CUT.replace(second=1)),
        dict(business_date=date(2025, 1, 2)),
        dict(carry_forward_days=8),
    ],
)
def test_invalid_fixing_queries_rejected(changes):
    with pytest.raises((ValidationError, ValidationFailure)):
        query(**changes)


async def test_official_eucb_request_mechanics_and_durable_source(db):
    seen = []
    handler = rates_handler(rate_body())

    def capture(request):
        seen.append(request)
        return handler(request)

    instance = rates_adapter(db, capture)
    try:
        result = await instance.historical(query())
        assert result.rate == Decimal("2")
        assert result.derivation == "PUBLISHED_EUR_LEG"
        assert result.coverage == "COMPLETE_VERIFIED"
        assert seen[-1].url.host == "exchange-rates-api.oanda.com"
        assert dict(seen[-1].url.params) == {
            "data_set": "EUCB",
            "base": "EUR",
            "quote": "USD",
            "date_time": "2025-01-03T23:45+00:00",
            "source_date": "true",
        }
        assert "api_key" not in str(seen[-1].url)
        with db.read() as connection:
            audit = connection.execute("SELECT * FROM rates_audit_queries").fetchone()
            assert audit["status"] == "VERIFIED" and audit["response_sha256"] == result.response_sha256
            assert TEST_SECRET not in audit["query_json"]
    finally:
        await instance.close()


@pytest.mark.parametrize(
    "base,quote,expected,kind,codes",
    [
        ("USD", "EUR", "0.5", "DERIVED_INVERSE", ("USD",)),
        ("USD", "JPY", "2", "DERIVED_CROSS", ("JPY", "USD")),
    ],
)
async def test_explicit_derived_rates_use_matching_eur_legs(db, base, quote, expected, kind, codes):
    instance = rates_adapter(db, rates_handler(rate_body(codes)))
    try:
        result = await instance.historical(
            query(base_currency=base, quote_currency=quote, allow_derived=True)
        )
        assert result.rate == Decimal(expected) and result.derivation == kind
    finally:
        await instance.close()


async def test_date_only_publication_never_invents_exact_timestamp(db):
    instance = rates_adapter(db, rates_handler(rate_body(source="2025-01-03")))
    try:
        result = await instance.historical(query())
        assert result.coverage == "PARTIAL" and not result.point_in_time_publication_verified
        assert result.legs[0].source_date == "2025-01-03"
    finally:
        await instance.close()


async def test_carry_forward_requires_explicit_policy(db):
    instance = rates_adapter(db, rates_handler(rate_body(source="2025-01-02T15:00:00Z")))
    try:
        with pytest.raises(Unavailable):
            await instance.historical(query())
        result = await instance.historical(query(carry_forward_days=1))
        assert result.legs[0].carried_days == 1
    finally:
        await instance.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "dataset",
        "cutoff",
        "future_publication",
        "missing_quote",
        "duplicate_quote",
        "skipped",
        "numeric_rate",
    ],
)
async def test_rates_provenance_anomalies_fail_closed(db, mutation):
    body = rate_body()
    if mutation == "dataset":
        body["meta"]["effective_params"]["data_set"] = "OANDA"
    elif mutation == "cutoff":
        body["meta"]["effective_params"]["date_time"] = "2025-01-02T23:45:00Z"
    elif mutation == "future_publication":
        body["quotes"][0]["source_date"] = "2025-01-04T15:00:00Z"
    elif mutation == "missing_quote":
        body["quotes"] = []
    elif mutation == "duplicate_quote":
        body["quotes"] *= 2
    elif mutation == "skipped":
        body["meta"]["skipped_currency_pairs"] = [{"base_currency": "EUR", "quote_currency": "USD"}]
    elif mutation == "numeric_rate":
        body["quotes"][0]["midpoint"] = 2
    instance = rates_adapter(db, rates_handler(body))
    try:
        with pytest.raises(Unavailable):
            await instance.historical(query())
        with db.read() as connection:
            assert (
                connection.execute("SELECT status FROM rates_audit_queries").fetchone()[0]
                == "UNVERIFIED_OR_UNKNOWN"
            )
    finally:
        await instance.close()


async def test_rates_ambiguous_response_consumes_conservative_budget(db):
    handler = rates_handler(rate_body())
    calls = []

    def timed_out(request):
        if request.url.path.endswith("spot.json"):
            calls.append(request)
            raise httpx.ReadTimeout("synthetic ambiguous response", request=request)
        return handler(request)

    instance = rates_adapter(db, timed_out, limit=1)
    try:
        with pytest.raises(Unavailable):
            await instance.historical(query())
        with pytest.raises(RateLimited):
            await instance.historical(query())
        assert len(calls) == 1
    finally:
        await instance.close()


async def test_intraday_entitlement_not_inferred_from_key(db):
    seen = []
    instance = rates_adapter(db, lambda request: seen.append(request), intraday_history_allowed=False)
    try:
        with pytest.raises(SafetyFence):
            await instance.historical(query())
        assert not seen
    finally:
        await instance.close()


async def test_http_origin_is_fixed(db):
    ref = credential()
    reader = BoundedHTTPReader(
        origins=frozenset({"https://api-fxpractice.oanda.com"}),
        credential=ref,
        vault=TestVault(),
        governor=LocalRateGovernor(db),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )
    try:
        with pytest.raises(ValidationFailure):
            await reader.get("https://attacker.invalid", "/v3/accounts")
    finally:
        await reader.close()


async def test_midnight_effective_date_is_normalized_by_documented_contract(db):
    body = rate_body()
    body["meta"]["effective_params"]["date_time"] = "2025-01-03"
    body["quotes"][0]["source_date"] = "2025-01-02T15:00:00Z"
    instance = rates_adapter(db, rates_handler(body), intraday_history_allowed=False)
    try:
        result = await instance.historical(query(cutoff=CUT.replace(hour=0, minute=0), carry_forward_days=1))
        assert result.requested_cutoff.hour == 0 and result.legs[0].carried_days == 1
    finally:
        await instance.close()


async def test_rejected_provenance_retains_original_raw_response(db):
    body = rate_body()
    body["meta"]["effective_params"]["data_set"] = "OANDA"
    instance = rates_adapter(db, rates_handler(body))
    try:
        with pytest.raises(Unavailable):
            await instance.historical(query())
        with db.read() as connection:
            row = connection.execute("SELECT * FROM rates_audit_queries").fetchone()
            assert row["response_sha256"] and b'"OANDA"' in row["response_body"]
            assert row["status"] == "UNVERIFIED_OR_UNKNOWN"
    finally:
        await instance.close()
