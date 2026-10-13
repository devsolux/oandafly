"""Deterministic, explicitly synthetic EUR-home EUR/USD research broker.

This is not an OANDA execution or liquidity model. Financing is identically zero
under the synthetic contract; there is no claim about real financing or fills.
Native-shaped facts make race and recovery tests use the same ledger and outbox.
"""

from __future__ import annotations

import inspect
import json
import re
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any, cast
from uuid import UUID

from oandafly.domain.errors import Conflict, SafetyFence, ValidationFailure
from oandafly.domain.instruments import currency_pair
from oandafly.domain.primitives import canonical_json, decimal_text, money, require_utc, sha256_json, utc_now
from oandafly.storage.database import Database

ACCOUNT_ID = "SYNTHETIC:PAPER:EUR"
HOME = "EUR"
INSTRUMENT = "EUR_USD"
UNIT_QUANTUM = Decimal("1")
CASH_QUANTUM = Decimal("0.00000001")
PRICE_QUANTUM = Decimal("0.00001")
FactConsumer = Callable[[dict[str, Any]], Awaitable[None] | None]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_account (
    id TEXT PRIMARY KEY, balance TEXT NOT NULL, funded INTEGER NOT NULL,
    funding_command TEXT, funding_hash TEXT, last_id INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS paper_quotes (
    instrument TEXT PRIMARY KEY, bid TEXT NOT NULL, ask TEXT NOT NULL, observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_events (
    id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_trades (
    id TEXT PRIMARY KEY, units TEXT NOT NULL, initial_units TEXT NOT NULL,
    entry_price TEXT NOT NULL, stop_loss TEXT, take_profit TEXT, client_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_orders (
    id TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL, initial_mid TEXT NOT NULL);
"""


class PaperBroker:
    def __init__(self, db: Database, consumer: FactConsumer | None = None,
                 clock: Callable[[], datetime] = utc_now) -> None:
        self.db, self.consumer, self.clock = db, consumer, clock
        db.initialize(_SCHEMA, component=__name__)
        with db.transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO paper_account(id,balance,funded) VALUES(?,?,0)",
                               (ACCOUNT_ID, "0"))

    def seed(self, amount: Decimal, command_id: UUID) -> dict[str, Any]:
        amount = money(amount)
        if amount <= 0 or amount > Decimal("1000000000") or amount % CASH_QUANTUM:
            raise ValidationFailure("Synthetic funding must be positive, at most 1 billion, and exact to 8 decimals")
        digest = sha256_json({"amount": amount, "account": ACCOUNT_ID})
        with self.db.transaction() as connection:
            account = self._account_row(connection)
            if account["funded"]:
                if account["funding_command"] == str(command_id) and account["funding_hash"] == digest:
                    return {"account_id": ACCOUNT_ID, "amount": decimal_text(amount), "duplicate": True}
                raise Conflict("Synthetic account was already funded; funding cannot reset its history")
            connection.execute("UPDATE paper_account SET balance=?,funded=1,funding_command=?,funding_hash=? WHERE id=?",
                               (decimal_text(amount), str(command_id), digest, ACCOUNT_ID))
            event = self._event(connection, "TRANSFER_FUNDS", amount=decimal_text(amount),
                                accountBalance=decimal_text(amount), reason="CLIENT_FUNDING")
        return {"account_id": ACCOUNT_ID, "amount": decimal_text(amount), "event": event, "duplicate": False}

    async def quote(self, bid: Decimal, ask: Decimal, time: datetime | None = None) -> dict[str, Any]:
        bid, ask = money(bid), money(ask)
        observed = require_utc(time or self.clock())
        if bid < PRICE_QUANTUM or ask < bid or ask > Decimal("100") or bid % PRICE_QUANTUM or ask % PRICE_QUANTUM:
            raise ValidationFailure("Synthetic quote must have 0.00001 <= bid <= ask <= 100 at five-place price precision")
        age = (require_utc(self.clock()) - observed).total_seconds()
        if not 0 <= age <= 15:
            raise SafetyFence("Synthetic quotes must not be future-dated or older than 15 seconds")
        events: list[dict[str, Any]] = []
        with self.db.transaction() as connection:
            old = connection.execute("SELECT * FROM paper_quotes WHERE instrument=?", (INSTRUMENT,)).fetchone()
            if old is not None:
                old_time = datetime.fromisoformat(old["observed_at"])
                if observed < old_time or (observed == old_time and (bid != money(old["bid"]) or ask != money(old["ask"]))):
                    raise Conflict("A quote source timestamp cannot regress or equivocate")
            connection.execute("INSERT INTO paper_quotes VALUES(?,?,?,?) ON CONFLICT(instrument) DO UPDATE SET bid=excluded.bid,ask=excluded.ask,observed_at=excluded.observed_at",
                               (INSTRUMENT, decimal_text(bid), decimal_text(ask), observed.isoformat()))
            pending = connection.execute("SELECT * FROM paper_orders WHERE state='PENDING' ORDER BY CAST(id AS INTEGER)").fetchall()
            for row in pending:
                order = json.loads(row["payload"])
                if "tradeID" in order:
                    continue
                if self._triggered(order, bid, ask, money(row["initial_mid"])):
                    events.extend(self._fill_or_cancel(connection, row["id"], order, bid, ask))
            trades = connection.execute("SELECT * FROM paper_trades WHERE units != '0'").fetchall()
            for trade in trades:
                units = money(trade["units"])
                close_price = bid if units > 0 else ask
                stop, take = trade["stop_loss"], trade["take_profit"]
                reason = ""
                if stop is not None and ((units > 0 and close_price <= money(stop)) or (units < 0 and close_price >= money(stop))):
                    reason = "STOP_LOSS_ORDER"
                elif take is not None and ((units > 0 and close_price >= money(take)) or (units < 0 and close_price <= money(take))):
                    reason = "TAKE_PROFIT_ORDER"
                if reason:
                    events.extend(self._close(connection, trade, bid, ask, reason))
        await self._publish(events)
        return {"instrument": INSTRUMENT, "bid": decimal_text(bid), "ask": decimal_text(ask),
                "time": observed.isoformat(), "source": "SYNTHETIC:PAPER", "events": len(events)}

    async def prepare_order(self, account_id: str) -> None:
        self._check_account(account_id)

    async def send_order_bytes(self, account_id: str, payload: bytes) -> dict[str, Any]:
        if type(payload) is not bytes or not 0 < len(payload) <= 65536:
            raise ValidationFailure("PAPER gateway requires bounded immutable bytes")
        decoded = json.loads(payload)
        if not isinstance(decoded, dict) or canonical_json(decoded).encode("utf-8") != payload:
            raise ValidationFailure("PAPER gateway requires canonical JSON bytes")
        return await self.submit_order(account_id, decoded, before_send=lambda: None)

    async def submit_order(self, account_id: str, payload: Mapping[str, Any], *,
                           before_send: Callable[[], None]) -> dict[str, Any]:
        self._check_account(account_id)
        wrapped = payload.get("order")
        if not isinstance(wrapped, Mapping):
            raise ValidationFailure("Synthetic request requires an order object")
        order = dict(wrapped)
        self._validate_order(order)
        events: list[dict[str, Any]] = []
        before_send()
        with self.db.transaction() as connection:
            quote = self._fresh_quote_row(connection)
            bid, ask = money(quote["bid"]), money(quote["ask"])
            if not self._account_row(connection)["funded"]:
                raise SafetyFence("Synthetic account requires explicit funding")
            for pending_order in connection.execute("SELECT payload FROM paper_orders WHERE state='PENDING'"):
                pending_payload = json.loads(pending_order["payload"])
                if pending_payload.get("clientExtensions", {}).get("id") == order["clientExtensions"]["id"]:
                    raise Conflict("An active synthetic order already uses this client ID")
            trade_client_id = order.get("tradeClientExtensions", {}).get("id")
            if trade_client_id is not None and connection.execute(
                "SELECT 1 FROM paper_trades WHERE client_id=? AND units!='0' LIMIT 1", (trade_client_id,),
            ).fetchone() is not None:
                raise Conflict("An open synthetic trade already uses this trade client ID")
            created = self._event(connection, order["type"] + "_ORDER",
                                  **{key: value for key, value in order.items() if key != "type"}, reason="CLIENT_ORDER")
            order_id = created["id"]
            events.append(created)
            initial_side_price = ask if money(order["units"]) > 0 else bid
            connection.execute("INSERT INTO paper_orders VALUES(?,?,'PENDING',?)",
                               (order_id, canonical_json(order), decimal_text(initial_side_price)))
            if self._triggered(order, bid, ask, initial_side_price):
                events.extend(self._fill_or_cancel(connection, order_id, order, bid, ask))
        await self._publish(events)
        return self._response(events)

    async def close_trade(self, account_id: str, trade_id: str, units: Decimal, *,
                          before_send: Callable[[], None]) -> dict[str, Any]:
        self._check_account(account_id)
        amount = money(units)
        if amount <= 0 or amount % UNIT_QUANTUM:
            raise ValidationFailure("Synthetic close size must be a positive integer")
        before_send()
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
            if row is None or money(row["units"]) == 0:
                raise Conflict("Synthetic trade is not open")
            if amount > abs(money(row["units"])):
                raise SafetyFence("Synthetic close cannot exceed the original gross trade exposure")
            quote = self._fresh_quote_row(connection)
            events = self._close(connection, row, money(quote["bid"]), money(quote["ask"]), "MARKET_ORDER_TRADE_CLOSE", amount)
        await self._publish(events)
        return self._response(events)

    async def cancel_order(self, account_id: str, order_id: str, *,
                           before_send: Callable[[], None]) -> dict[str, Any]:
        self._check_account(account_id)
        before_send()
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM paper_orders WHERE id=?", (order_id,)).fetchone()
            if row is None or row["state"] != "PENDING":
                raise Conflict("Synthetic order is not pending")
            connection.execute("UPDATE paper_orders SET state='CANCELLED' WHERE id=?", (order_id,))
            order = json.loads(row["payload"])
            extras: dict[str, Any] = {}
            if "clientExtensions" in order:
                extras["clientOrderID"] = order["clientExtensions"]["id"]
            if "tradeID" in order:
                if order["type"] == "STOP_LOSS_ORDER":
                    connection.execute("UPDATE paper_trades SET stop_loss=NULL WHERE id=?", (order["tradeID"],))
                elif order["type"] == "TAKE_PROFIT_ORDER":
                    connection.execute("UPDATE paper_trades SET take_profit=NULL WHERE id=?", (order["tradeID"],))
            event = self._event(connection, "ORDER_CANCEL", orderID=order_id, reason="CLIENT_REQUEST", **extras)
        await self._publish([event])
        return {"orderCancelTransaction": event, "relatedTransactionIDs": [event["id"]], "lastTransactionID": event["id"]}

    def account(self) -> dict[str, Any]:
        with self.db.read() as connection:
            row = self._account_row(connection)
            quote = connection.execute("SELECT * FROM paper_quotes WHERE instrument=?", (INSTRUMENT,)).fetchone()
            trades = connection.execute("SELECT * FROM paper_trades WHERE units != '0'").fetchall()
            pending = connection.execute("SELECT id,payload FROM paper_orders WHERE state='PENDING'").fetchall()
            upl = Decimal(0)
            gross_units = sum((abs(money(item["units"])) for item in trades), Decimal(0))
            native_trades: list[dict[str, Any]] = []
            for trade in trades:
                if quote is None:
                    raise SafetyFence("Open synthetic trades have no mark")
                units = money(trade["units"])
                bid, ask = money(quote["bid"]), money(quote["ask"])
                value = self._pl(units, money(trade["entry_price"]), bid if units > 0 else ask, bid, ask)
                upl += value
                native_trades.append({"id": trade["id"], "instrument": INSTRUMENT,
                                      "currentUnits": decimal_text(units), "unrealizedPL": decimal_text(value),
                                      "price": trade["entry_price"], "state": "OPEN"})
            balance = money(row["balance"])
            margin = gross_units * Decimal("0.05")
            nav = balance + upl
            closeout_percent = (margin * Decimal("0.5") / nav) if nav > 0 else (Decimal(1) if margin else Decimal(0))
            long_units = sum((money(item["units"]) for item in trades if money(item["units"]) > 0), Decimal(0))
            short_units = sum((money(item["units"]) for item in trades if money(item["units"]) < 0), Decimal(0))
            return {"id": ACCOUNT_ID, "currency": HOME, "balance": decimal_text(balance),
                    "NAV": decimal_text(nav), "unrealizedPL": decimal_text(upl), "marginUsed": decimal_text(margin),
                    "marginAvailable": decimal_text(max(Decimal(0), nav - margin)), "marginRate": "0.05",
                    "marginCloseoutPercent": decimal_text(closeout_percent),
                    "lastTransactionID": str(row["last_id"]), "trades": native_trades,
                    "orders": [{"id": item["id"], **json.loads(item["payload"])} for item in pending],
                    "positions": [{"instrument": INSTRUMENT, "long": {"units": decimal_text(long_units)},
                                   "short": {"units": decimal_text(short_units)}}],
                    "grossUnits": decimal_text(gross_units), "funded": bool(row["funded"]),
                    "quoteObservedAt": None if quote is None else quote["observed_at"],
                    "quoteStale": quote is None or not 0 <= (require_utc(self.clock()) - datetime.fromisoformat(quote["observed_at"])).total_seconds() <= 15,
                    "synthetic": True, "financing_policy": "ZERO_BY_SYNTHETIC_CONTRACT"}

    def since_id(self, cursor: str) -> dict[str, Any]:
        if not cursor.isdecimal() or len(cursor) > 30:
            raise ValidationFailure("Invalid native transaction cursor")
        with self.db.read() as connection:
            account = self._account_row(connection)
            if int(cursor) > int(account["last_id"]):
                raise Conflict("Cursor is ahead of the synthetic history")
            rows = connection.execute("SELECT body FROM paper_events WHERE id>? ORDER BY id", (int(cursor),)).fetchall()
            return {"transactions": [json.loads(row[0]) for row in rows],
                    "lastTransactionID": str(account["last_id"])}

    def _event(self, connection: sqlite3.Connection, kind: str, **values: Any) -> dict[str, Any]:
        row = self._account_row(connection)
        event_id = int(row["last_id"]) + 1
        event: dict[str, Any] = {"id": str(event_id), "accountID": ACCOUNT_ID,
                                 "type": kind, "time": require_utc(self.clock()).isoformat(), **values}
        connection.execute("UPDATE paper_account SET last_id=? WHERE id=?", (event_id, ACCOUNT_ID))
        connection.execute("INSERT INTO paper_events VALUES(?,?,?)", (event_id, event["time"], canonical_json(event)))
        return event

    def _fill_or_cancel(self, connection: sqlite3.Connection, order_id: str, order: dict[str, Any],
                        bid: Decimal, ask: Decimal) -> list[dict[str, Any]]:
        units = money(order["units"])
        price = ask if units > 0 else bid
        bound = money(order["priceBound"]) if "priceBound" in order else None
        reason: str | None = None
        if bound is not None and ((units > 0 and price > bound) or (units < 0 and price < bound)):
            reason = "BOUNDS_VIOLATION"
        stop = order.get("stopLossOnFill", {}).get("price")
        take = order.get("takeProfitOnFill", {}).get("price")
        close_side = bid if units > 0 else ask
        if ((stop is not None and (money(stop) >= close_side if units > 0 else money(stop) <= close_side))
                or (take is not None and (money(take) <= close_side if units > 0 else money(take) >= close_side))):
            reason = "SYNTHETIC_INVALID_PROTECTION"
        existing = connection.execute("SELECT * FROM paper_trades WHERE units != '0'").fetchall()
        gross = sum((abs(money(item["units"])) for item in existing), Decimal(0))
        current_upl = sum((self._pl(money(item["units"]), money(item["entry_price"]),
                           bid if money(item["units"]) > 0 else ask, bid, ask) for item in existing), Decimal(0))
        new_upl = self._pl(units, price, bid if units > 0 else ask, bid, ask)
        if (gross + abs(units)) * Decimal("0.05") > money(self._account_row(connection)["balance"]) + current_upl + new_upl:
            reason = "INSUFFICIENT_MARGIN"
        if reason:
            connection.execute("UPDATE paper_orders SET state='CANCELLED' WHERE id=?", (order_id,))
            return [self._event(connection, "ORDER_CANCEL", orderID=order_id,
                               clientOrderID=order["clientExtensions"]["id"], reason=reason)]
        trade_id = str(int(self._account_row(connection)["last_id"]) + 1)
        connection.execute("INSERT INTO paper_trades VALUES(?,?,?,?,?,?,?)",
                           (trade_id, decimal_text(units), decimal_text(units), decimal_text(price), stop, take,
                            order.get("tradeClientExtensions", {}).get("id", "")))
        connection.execute("UPDATE paper_orders SET state='FILLED' WHERE id=?", (order_id,))
        fill = self._event(connection, "ORDER_FILL", orderID=order_id, clientOrderID=order["clientExtensions"]["id"],
                           instrument=INSTRUMENT, units=decimal_text(units), price=decimal_text(price),
                           pl="0", financing="0", commission="0", guaranteedExecutionFee="0",
                           halfSpreadCost=decimal_text(self._half_spread_cost(units, bid, ask)),
                           accountBalance=self._account_row(connection)["balance"], reason="MARKET_ORDER" if order["type"] == "MARKET" else order["type"] + "_ORDER",
                           tradeOpened={"tradeID": trade_id, "units": decimal_text(units), "price": decimal_text(price),
                                        **({"clientExtensions": order["tradeClientExtensions"]} if "tradeClientExtensions" in order else {})})
        events = [fill]
        for key, native_type in (("stopLossOnFill", "STOP_LOSS_ORDER"),
                                  ("takeProfitOnFill", "TAKE_PROFIT_ORDER")):
            if key in order:
                child = self._event(connection, native_type, tradeID=trade_id,
                                    price=order[key]["price"], timeInForce="GTC", reason="ON_FILL")
                connection.execute("INSERT INTO paper_orders VALUES(?,?,'PENDING',?)",
                    (child["id"], canonical_json({"type": native_type, "tradeID": trade_id,
                                                  "price": order[key]["price"]}), decimal_text((bid + ask) / 2)))
                events.append(child)
        return events

    def _close(self, connection: sqlite3.Connection, trade: sqlite3.Row, bid: Decimal, ask: Decimal,
               reason: str, amount: Decimal | None = None) -> list[dict[str, Any]]:
        initial_units = money(trade["units"])
        units = initial_units if amount is None else (amount if initial_units > 0 else -amount)
        remaining = initial_units - units
        price = bid if units > 0 else ask
        realized = self._pl(units, money(trade["entry_price"]), price, bid, ask)
        balance = money(self._account_row(connection)["balance"]) + realized
        created_events: list[dict[str, Any]] = []
        if reason in {"STOP_LOSS_ORDER", "TAKE_PROFIT_ORDER"}:
            matches = [child for child in connection.execute("SELECT * FROM paper_orders WHERE state='PENDING'")
                       if (json.loads(child["payload"]).get("tradeID") == trade["id"]
                           and json.loads(child["payload"]).get("type") == reason)]
            if len(matches) != 1:
                raise SafetyFence("Triggered protection must bind exactly one pending native child order")
            close_order_id = str(matches[0]["id"])
            connection.execute("UPDATE paper_orders SET state='FILLED' WHERE id=?", (close_order_id,))
        else:
            created = self._event(connection, "MARKET_ORDER", instrument=INSTRUMENT, units=decimal_text(-units),
                                  positionFill="REDUCE_ONLY", reason="TRADE_CLOSE", timeInForce="FOK",
                                  tradeClose={"tradeID": trade["id"], "units": decimal_text(abs(units))})
            created_events.append(created)
            close_order_id = str(created["id"])
        connection.execute("UPDATE paper_trades SET units=? WHERE id=?", (decimal_text(remaining), trade["id"]))
        connection.execute("UPDATE paper_account SET balance=? WHERE id=?", (decimal_text(balance), ACCOUNT_ID))
        reduction = {"tradeID": trade["id"], "units": decimal_text(abs(units)),
                     "realizedPL": decimal_text(realized), "financing": "0"}
        subtree: dict[str, Any] = {"tradesClosed": [reduction]} if remaining == 0 else {"tradeReduced": reduction}
        fill = self._event(connection, "ORDER_FILL", orderID=close_order_id, instrument=INSTRUMENT,
                           units=decimal_text(-units), price=decimal_text(price), pl=decimal_text(realized), financing="0",
                           commission="0", guaranteedExecutionFee="0",
                           halfSpreadCost=decimal_text(self._half_spread_cost(units, bid, ask)), accountBalance=decimal_text(balance),
                           reason=reason, **subtree)
        events = [*created_events, fill]
        if remaining == 0:
            children = connection.execute("SELECT * FROM paper_orders WHERE state='PENDING'").fetchall()
            for child in children:
                payload = json.loads(child["payload"])
                if payload.get("tradeID") == trade["id"]:
                    connection.execute("UPDATE paper_orders SET state='CANCELLED' WHERE id=?", (child["id"],))
                    events.append(self._event(connection, "ORDER_CANCEL", orderID=child["id"],
                                              reason="LINKED_TRADE_CLOSED"))
        return events

    @staticmethod
    def _pl(units: Decimal, entry: Decimal, exit_price: Decimal, bid: Decimal, ask: Decimal) -> Decimal:
        with localcontext() as context:
            context.prec = 128
            quote_pl = units * (exit_price - entry)
            # Positive USD is sold for EUR at the inverse ask; a USD liability costs inverse bid.
            return (quote_pl / (ask if quote_pl >= 0 else bid)).quantize(CASH_QUANTUM, rounding=ROUND_HALF_EVEN)

    @staticmethod
    def _half_spread_cost(units: Decimal, bid: Decimal, ask: Decimal) -> Decimal:
        """Synthetic TCA memorandum; already reflected in entry/exit prices, never a cash fee."""
        with localcontext() as context:
            context.prec = 128
            return (abs(units) * (ask - bid) / (bid + ask)).quantize(CASH_QUANTUM, rounding=ROUND_HALF_EVEN)

    @staticmethod
    def _triggered(order: Mapping[str, Any], bid: Decimal, ask: Decimal, initial_side_price: Decimal) -> bool:
        kind = order["type"]
        if kind == "MARKET":
            return True
        units, trigger = money(order["units"]), money(order["price"])
        current = ask if units > 0 else bid
        if kind == "LIMIT":
            return current <= trigger if units > 0 else current >= trigger
        if kind == "STOP":
            return current >= trigger if units > 0 else current <= trigger
        if kind == "MARKET_IF_TOUCHED":
            return current <= trigger if initial_side_price > trigger else current >= trigger
        raise ValidationFailure("Unsupported synthetic trigger type")

    @staticmethod
    def _account_row(connection: sqlite3.Connection) -> sqlite3.Row:
        result = connection.execute("SELECT * FROM paper_account WHERE id=?", (ACCOUNT_ID,)).fetchone()
        if result is None:
            raise SafetyFence("Synthetic account is missing")
        return cast(sqlite3.Row, result)

    @staticmethod
    def _quote_row(connection: sqlite3.Connection) -> sqlite3.Row:
        result = connection.execute("SELECT * FROM paper_quotes WHERE instrument=?", (INSTRUMENT,)).fetchone()
        if result is None:
            raise SafetyFence("Synthetic execution requires an explicitly supplied quote")
        return cast(sqlite3.Row, result)

    def _fresh_quote_row(self, connection: sqlite3.Connection) -> sqlite3.Row:
        row = self._quote_row(connection)
        age = (require_utc(self.clock()) - datetime.fromisoformat(row["observed_at"])).total_seconds()
        if not 0 <= age <= 15:
            raise SafetyFence("Synthetic execution requires a nonfuture quote no older than 15 seconds")
        return row

    @staticmethod
    def _check_account(account_id: str) -> None:
        if account_id != ACCOUNT_ID:
            raise ValidationFailure("The synthetic transport cannot address an OANDA account")

    @staticmethod
    def _validate_order(order: dict[str, Any]) -> None:
        allowed = {"type", "instrument", "units", "timeInForce", "positionFill", "clientExtensions",
                   "tradeClientExtensions", "price", "priceBound", "stopLossOnFill", "takeProfitOnFill"}
        if set(order) - allowed:
            raise ValidationFailure("Unsupported synthetic order field")
        if order.get("instrument") != INSTRUMENT:
            currency_pair(str(order.get("instrument", "")))
            raise ValidationFailure("This declared synthetic contract only supports EUR_USD")
        kind = order.get("type")
        if kind not in {"MARKET", "LIMIT", "STOP", "MARKET_IF_TOUCHED"}:
            raise ValidationFailure("Unsupported synthetic FX order type")
        units = money(order.get("units", "0"))
        if not units or abs(units) > 100000000 or units % UNIT_QUANTUM:
            raise ValidationFailure("Synthetic units require nonzero integers within size bounds")
        if order.get("positionFill") != "OPEN_ONLY":
            raise ValidationFailure("Synthetic entry orders require explicit OPEN_ONLY")
        if order.get("timeInForce") != ("FOK" if kind == "MARKET" else "GTC"):
            raise ValidationFailure("Synthetic MARKET uses FOK; pending orders use GTC")
        if kind != "MARKET" and money(order.get("price", "0")) <= 0:
            raise ValidationFailure("Pending synthetic orders require a positive trigger price")
        if kind == "LIMIT" and "priceBound" in order:
            raise ValidationFailure("The native LimitOrderRequest does not accept priceBound")
        extensions = order.get("clientExtensions")
        if not isinstance(extensions, dict) or "id" not in extensions or set(extensions) - {"id", "tag"}:
            raise ValidationFailure("Synthetic entries require an exact client UUID")
        try:
            client_id = extensions["id"]
            if not isinstance(client_id, str):
                raise ValueError("Client ID must be text")
            if re.fullmatch(r"oaf_[0-9a-f]{32}", client_id) is None:
                UUID(client_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationFailure("Synthetic client ID must be a UUID") from exc
        if "tag" in extensions and extensions["tag"] != "oandafly":
            raise ValidationFailure("Unexpected synthetic client namespace")
        if "tradeClientExtensions" in order:
            trade_extensions = order["tradeClientExtensions"]
            if trade_extensions != {"id": client_id + "_trade", "tag": "oandafly"}:
                raise ValidationFailure("Trade extensions must bind the prepared intent")
        for child in ("stopLossOnFill", "takeProfitOnFill"):
            if child in order:
                item = order[child]
                if not isinstance(item, dict) or set(item) - {"price", "timeInForce"} or money(item.get("price", "0")) <= 0:
                    raise ValidationFailure("Synthetic protection requires a positive price")
                if item.get("timeInForce", "GTC") != "GTC":
                    raise ValidationFailure("Synthetic attached protection supports GTC only")
                price = money(item["price"])
                if price > 100 or price % PRICE_QUANTUM:
                    raise ValidationFailure("Synthetic protection requires five-place prices up to 100")
                order[child] = {**item, "price": decimal_text(price)}
        for key in ("units", "price", "priceBound"):
            if key in order:
                order[key] = decimal_text(money(order[key]))
                if key != "units" and (money(order[key]) <= 0 or money(order[key]) > 100 or money(order[key]) % PRICE_QUANTUM):
                    raise ValidationFailure("Synthetic order price requires five-place positive precision up to 100")

    async def _publish(self, events: list[dict[str, Any]]) -> None:
        if self.consumer is not None:
            for event in events:
                result = self.consumer(event)
                if inspect.isawaitable(result):
                    await result

    @staticmethod
    def _response(events: list[dict[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {"relatedTransactionIDs": [item["id"] for item in events],
                                 "lastTransactionID": events[-1]["id"]}
        for item in events:
            kind = item["type"]
            if kind in {"STOP_LOSS_ORDER", "TAKE_PROFIT_ORDER", "TRAILING_STOP_LOSS_ORDER"}:
                key = {"STOP_LOSS_ORDER": "stopLossOrderTransaction", "TAKE_PROFIT_ORDER": "takeProfitOrderTransaction",
                       "TRAILING_STOP_LOSS_ORDER": "trailingStopLossOrderTransaction"}[kind]
                result[key] = item
            elif kind == "ORDER_FILL":
                result["orderFillTransaction"] = item
            elif kind == "ORDER_CANCEL":
                result.setdefault("orderCancelTransaction", item)
            else:
                result.setdefault("orderCreateTransaction", item)
        return result
