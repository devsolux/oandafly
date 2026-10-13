"""Deterministic synthetic integration; these tests never address a broker."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from oandafly.domain.errors import Conflict, SafetyFence, ValidationFailure
from oandafly.storage.database import Database
from tests.support import supported_database
from oandafly.storage.ledger import Ledger
from oandafly.simulation.broker import ACCOUNT_ID, PaperBroker

D = Decimal


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int = 1) -> None:
        self.now += timedelta(seconds=seconds)


def guard() -> None:
    """A fixture guard records no real-world trading qualification."""
    return None


def payload(kind: str = "MARKET", *, units: str = "100", price: str | None = None,
            price_bound: str | None = None, protection: bool = True,
            client_id: str | None = None) -> dict[str, Any]:
    client = client_id or "oaf_" + uuid4().hex
    order: dict[str, Any] = {"type": kind, "instrument": "EUR_USD", "units": units,
        "positionFill": "OPEN_ONLY", "timeInForce": "FOK" if kind == "MARKET" else "GTC",
        "clientExtensions": {"id": client, "tag": "oandafly"},
        "tradeClientExtensions": {"id": client + "_trade", "tag": "oandafly"}}
    if price is not None:
        order["price"] = price
    if price_bound is not None:
        order["priceBound"] = price_bound
    if protection:
        order["stopLossOnFill"] = {"price": "1.09" if D(units) > 0 else "1.11", "timeInForce": "GTC"}
        order["takeProfitOnFill"] = {"price": "1.12" if D(units) > 0 else "1.08", "timeInForce": "GTC"}
    return {"order": order}


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[tuple[Database, Ledger, PaperBroker, Clock, list[dict[str, Any]]]]:
    db = supported_database(tmp_path / "paper.sqlite3")
    ledger = Ledger(db)
    clock = Clock()
    observed: list[dict[str, Any]] = []

    def consume(event: dict[str, Any]) -> None:
        ledger.ingest("SANDBOX", ACCOUNT_ID, event, "PAPER")
        observed.append(event)

    broker = PaperBroker(db, consume, clock)
    funding = broker.seed(D(10000), uuid4())
    ledger.establish_baseline("SANDBOX", ACCOUNT_ID, "EUR", D(10000), funding["event"]["id"], clock())
    asyncio.run(broker.quote(D("1.10000"), D("1.10020")))
    yield db, ledger, broker, clock, observed
    db.close()


def catch_up(ledger: Ledger, broker: PaperBroker) -> None:
    cursor = ledger.balance("SANDBOX", ACCOUNT_ID).covered_cursor
    history = broker.since_id(cursor)
    ledger.apply_history("SANDBOX", ACCOUNT_ID, cursor, history["transactions"], history["lastTransactionID"], unfiltered=True)


def test_early_native_fill_cash_ledger_then_full_close_proves_same_scope(setup: tuple[Any, ...]) -> None:
    _, ledger, broker, _, observed = setup
    response = asyncio.run(broker.submit_order(ACCOUNT_ID, payload(price_bound="1.10100"), before_send=guard))
    fill = response["orderFillTransaction"]
    assert any(event["id"] == fill["id"] for event in observed)
    assert fill["financing"] == "0" and D(fill["halfSpreadCost"]) > 0
    assert ledger.balance("SANDBOX", ACCOUNT_ID).balance == 10000
    assert broker.account()["trades"][0]["currentUnits"] == "100"
    closed = asyncio.run(broker.close_trade(ACCOUNT_ID, fill["tradeOpened"]["tradeID"], D(100), before_send=guard))
    assert closed["orderFillTransaction"]["reason"] == "MARKET_ORDER_TRADE_CLOSE"
    catch_up(ledger, broker)
    state = ledger.balance("SANDBOX", ACCOUNT_ID)
    assert state.balance == D("9999.98181818") == D(broker.account()["balance"])
    assert state.history_caught_up and state.known_financing_settled
    assert not broker.account()["trades"] and not broker.account()["orders"]


@pytest.mark.parametrize("kind,price", [("LIMIT", "1.10100"), ("STOP", "1.10000"), ("MARKET_IF_TOUCHED", "1.10020")])
def test_marketable_pending_order_fills_in_initial_create_response(setup: tuple[Any, ...], kind: str, price: str) -> None:
    _, _, broker, _, _ = setup
    response = asyncio.run(broker.submit_order(ACCOUNT_ID, payload(kind, price=price), before_send=guard))
    assert response["orderFillTransaction"]["price"] == "1.10020"


def test_mit_direction_uses_initial_same_side_price_not_midpoint(setup: tuple[Any, ...]) -> None:
    _, _, broker, clock, _ = setup
    response = asyncio.run(broker.submit_order(ACCOUNT_ID, payload("MARKET_IF_TOUCHED", price="1.10010"), before_send=guard))
    assert "orderFillTransaction" not in response
    clock.advance()
    asyncio.run(broker.quote(D("1.09990"), D("1.10010")))
    assert broker.account()["trades"][0]["price"] == "1.10010"


def test_pending_stop_gap_beyond_price_bound_cancels_without_exposure(setup: tuple[Any, ...]) -> None:
    _, ledger, broker, clock, _ = setup
    response = asyncio.run(broker.submit_order(ACCOUNT_ID, payload("STOP", price="1.10100", price_bound="1.10150"), before_send=guard))
    assert "orderFillTransaction" not in response
    clock.advance()
    asyncio.run(broker.quote(D("1.10200"), D("1.10220")))
    catch_up(ledger, broker)
    assert not broker.account()["trades"] and not broker.account()["orders"]
    assert ledger.balance("SANDBOX", ACCOUNT_ID).balance == 10000
    assert broker.since_id("1")["transactions"][-1]["reason"] == "BOUNDS_VIOLATION"


def test_protection_fill_binds_native_child_not_invented_market_order(setup: tuple[Any, ...]) -> None:
    _, ledger, broker, clock, _ = setup
    response = asyncio.run(broker.submit_order(ACCOUNT_ID, payload(), before_send=guard))
    child_id = response["stopLossOrderTransaction"]["id"]
    old_head = broker.account()["lastTransactionID"]
    clock.advance()
    asyncio.run(broker.quote(D("1.08000"), D("1.08020")))
    events = broker.since_id(old_head)["transactions"]
    fills = [event for event in events if event["type"] == "ORDER_FILL"]
    assert len(fills) == 1 and fills[0]["orderID"] == child_id and fills[0]["reason"] == "STOP_LOSS_ORDER"
    assert not any(event["type"] == "MARKET_ORDER" for event in events)
    assert not broker.account()["trades"] and not broker.account()["orders"]
    catch_up(ledger, broker)
    assert ledger.balance("SANDBOX", ACCOUNT_ID).balance == D(broker.account()["balance"])
    assert ledger.balance("SANDBOX", ACCOUNT_ID).history_caught_up


def test_cancelled_protection_never_executes_from_stale_trade_fields(setup: tuple[Any, ...]) -> None:
    _, _, broker, clock, _ = setup
    response = asyncio.run(broker.submit_order(ACCOUNT_ID, payload(), before_send=guard))
    asyncio.run(broker.cancel_order(ACCOUNT_ID, response["stopLossOrderTransaction"]["id"], before_send=guard))
    clock.advance()
    asyncio.run(broker.quote(D("1.08000"), D("1.08020")))
    assert broker.account()["trades"][0]["currentUnits"] == "100"


def test_partial_close_retains_units_and_linked_protection(setup: tuple[Any, ...]) -> None:
    _, ledger, broker, _, _ = setup
    response = asyncio.run(broker.submit_order(ACCOUNT_ID, payload(), before_send=guard))
    trade_id = response["orderFillTransaction"]["tradeOpened"]["tradeID"]
    closed = asyncio.run(broker.close_trade(ACCOUNT_ID, trade_id, D(40), before_send=guard))
    assert closed["orderFillTransaction"]["tradeReduced"]["units"] == "40"
    assert broker.account()["trades"][0]["currentUnits"] == "60" and len(broker.account()["orders"]) == 2
    catch_up(ledger, broker)
    assert ledger.balance("SANDBOX", ACCOUNT_ID).balance == D(broker.account()["balance"])
    with pytest.raises(SafetyFence):
        asyncio.run(broker.close_trade(ACCOUNT_ID, trade_id, D(61), before_send=guard))


def test_future_stale_and_regressing_quotes_cannot_execute(setup: tuple[Any, ...]) -> None:
    _, _, broker, clock, _ = setup
    with pytest.raises(SafetyFence):
        asyncio.run(broker.quote(D("1.1"), D("1.1002"), clock() + timedelta(seconds=1)))
    response = asyncio.run(broker.submit_order(ACCOUNT_ID, payload(), before_send=guard))
    clock.advance(16)
    assert broker.account()["quoteStale"]
    with pytest.raises(SafetyFence):
        asyncio.run(broker.close_trade(ACCOUNT_ID, response["orderFillTransaction"]["tradeOpened"]["tradeID"], D(100), before_send=guard))
    with pytest.raises(SafetyFence):
        asyncio.run(broker.submit_order(ACCOUNT_ID, payload(), before_send=guard))
    with pytest.raises(SafetyFence):
        asyncio.run(broker.quote(D("1.1"), D("1.1002"), clock() - timedelta(seconds=16)))


def test_consumer_failure_after_commit_is_recoverable_only_via_history(setup: tuple[Any, ...]) -> None:
    _, ledger, broker, _, _ = setup

    def fail_on_fill(event: dict[str, Any]) -> None:
        if event["type"] == "ORDER_FILL":
            raise TimeoutError("Injected event-consumer failure after durable native fill")
        ledger.ingest("SANDBOX", ACCOUNT_ID, event, "PAPER")

    broker.consumer = fail_on_fill
    with pytest.raises(TimeoutError):
        asyncio.run(broker.submit_order(ACCOUNT_ID, payload(), before_send=guard))
    assert len(broker.account()["trades"]) == 1
    catch_up(ledger, broker)
    assert ledger.balance("SANDBOX", ACCOUNT_ID).history_caught_up
    assert len(broker.account()["trades"]) == 1


def test_remote_client_id_lifetime_is_active_window_not_infinite_idempotency(setup: tuple[Any, ...]) -> None:
    _, _, broker, _, _ = setup
    client = "oaf_" + uuid4().hex
    order = payload("LIMIT", price="1.09", client_id=client, protection=False)
    response = asyncio.run(broker.submit_order(ACCOUNT_ID, order, before_send=guard))
    with pytest.raises(Conflict):
        asyncio.run(broker.submit_order(ACCOUNT_ID, order, before_send=guard))
    asyncio.run(broker.cancel_order(ACCOUNT_ID, response["orderCreateTransaction"]["id"], before_send=guard))
    repeated = asyncio.run(broker.submit_order(ACCOUNT_ID, order, before_send=guard))
    assert repeated["orderCreateTransaction"]["id"] != response["orderCreateTransaction"]["id"]


def test_exact_precision_and_illegal_limit_field_rejected_before_guard(setup: tuple[Any, ...]) -> None:
    _, _, broker, _, _ = setup
    calls: list[str] = []

    def called() -> None:
        calls.append("called")

    for order in (payload("LIMIT", price="1.09", price_bound="1.1"), payload(price_bound="1.100201"), payload(units="0.5")):
        with pytest.raises(ValidationFailure):
            asyncio.run(broker.submit_order(ACCOUNT_ID, order, before_send=called))
    assert not calls
