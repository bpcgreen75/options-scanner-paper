"""Run an NVDA signal check or a one-contract Tradier sandbox pilot."""

import argparse
import gzip
import http.client
import io
import json
import os
import re
import sqlite3
import zlib
from contextlib import closing
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from time import sleep
from urllib.parse import urlencode

from paper_preview import ET, money
from tradier_quotes import MAX_BODY, QuoteError, TradierOptions, records
from tradier_signals import ENTRY_END, ENTRY_START, Bar, aggregate_five_minute, closed_bars, scan


SYMBOL = "NVDA"
DELAY = timedelta(minutes=15)
FRESH = timedelta(seconds=90)
TERMINAL = {"canceled", "expired", "rejected", "error", "filled"}
JOURNAL = Path("/app/storage/tradier-nvda-pilot.sqlite3")
CANDLES = Path("/app/storage/tradier-nvda-candles.sqlite3")
EVENT_LOG = None


class Sandbox(TradierOptions):
    def __init__(self):
        super().__init__("sandbox", os.getenv("TRADIER_SANDBOX_TOKEN"))
        self.calendars = {}

    def request(self, path, *, method="GET", params=None, fields=None):
        route = re.sub(r"(?<=accounts/)VA\d+", "[paper account]", path)
        if path == "markets/timesales" and params:
            route += f" ({params['interval']})"
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if params:
            path += "?" + urlencode(params)
        if fields is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        attempts = 2 if method == "GET" else 1
        for attempt in range(attempts):
            retryable = False
            try:
                with closing(http.client.HTTPSConnection("sandbox.tradier.com", timeout=15)) as connection:
                    connection.request(method, f"/v1/{path}",
                                       urlencode(fields) if fields is not None else None, headers)
                    with connection.getresponse() as response:
                        if response.status == 200:
                            return self.decode_response(response)
                        reason = f"HTTP {response.status}"
                        retryable = response.status in (408, 502, 503, 504)
            except (OSError, EOFError, http.client.HTTPException, zlib.error) as exc:
                reason = type(exc).__name__
                retryable = not isinstance(exc, gzip.BadGzipFile)
            except (ValueError, QuoteError) as exc:
                reason = str(exc) if isinstance(exc, QuoteError) else "Invalid JSON response"
            if not retryable or attempt + 1 == attempts:
                raise QuoteError(f"Tradier sandbox {method} {route} | {reason}") from None
            sleep(1)

    @staticmethod
    def decode_response(response):
        body = response.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            raise QuoteError("Response exceeded the size limit")
        if response.getheader("Content-Encoding", "").lower() == "gzip" or body.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as archive:
                body = archive.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                raise QuoteError("Decompressed response exceeded the size limit")
        if not body.strip():
            raise QuoteError("Empty response")
        return json.loads(body, parse_float=Decimal)

    def candles(self, minutes, start, until):
        params = {"symbol": SYMBOL, "interval": f"{minutes}min", "session_filter": "open",
                  "start": start.strftime("%Y-%m-%d %H:%M"), "end": until.strftime("%Y-%m-%d %H:%M")}
        reply = self.request("markets/timesales", params=params)
        try:
            return closed_bars(records(reply, "series", "data"), minutes, until)
        except (QuoteError, KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise QuoteError(f"Tradier sandbox {minutes}min candles had an unexpected format "
                             f"({type(exc).__name__})") from None

    def session(self, day):
        month = (day.year, day.month)
        if month not in self.calendars:
            calendar = self.request("markets/calendar", params={"year": day.year, "month": day.month})
            self.calendars[month] = {item["date"]: item for item in records(calendar["calendar"], "days", "day")}
        record = self.calendars[month].get(day.isoformat())
        if record is None:
            raise QuoteError("Tradier did not return the requested trading date")
        if record["status"] == "closed":
            return None
        return tuple(datetime.combine(day, time.fromisoformat(record["open"][bound]), ET)
                     for bound in ("start", "end"))

    def account(self):
        profile = records(self.request("user/profile"), "profile", "account")
        active = [item["account_number"] for item in profile if item.get("status") == "active"
                  and re.fullmatch(r"VA\d+", item.get("account_number", ""))]
        if len(active) != 1:
            raise QuoteError("Expected exactly one active Tradier virtual account")
        return f"accounts/{active[0]}"


def emit(**result):
    if EVENT_LOG is not None:
        result = {"timestamp_et": datetime.now(ET).isoformat(), **result}
    line = json.dumps(result, default=str)
    print(line, flush=True)
    if EVENT_LOG is not None:
        try:
            with EVENT_LOG.open("a", encoding="utf-8") as log:
                log.write(line + "\n")
        except OSError:
            print("Could not append to the paper pilot event log.", flush=True)


class CandleCache:
    """Keep completed windows on Render's disk and refresh short recent slices."""

    def __init__(self, api, db, session):
        self.api, self.db = api, db
        self.opened, self.closed = session
        self.history = None
        self.recent = None
        self.db.execute("""CREATE TABLE IF NOT EXISTS candle_windows (
            minutes INTEGER, start TEXT, end TEXT, bars TEXT,
            PRIMARY KEY (minutes, start, end))""")

    def window(self, minutes, start, end, fixed):
        key = minutes, start.isoformat(), end.isoformat()
        row = self.db.execute(
            "SELECT bars FROM candle_windows WHERE minutes=? AND start=? AND end=?", key
        ).fetchone()
        if row is not None:
            return [Bar(datetime.fromisoformat(item[0]), *(money(value) for value in item[1:]))
                    for item in json.loads(row[0])]
        bars = [bar for bar in self.api.candles(minutes, start, end) if start <= bar.start < end]
        expected = (end - start) // timedelta(minutes=minutes)
        if fixed and len(bars) == expected:
            payload = json.dumps([[bar.start.isoformat(), str(bar.open), str(bar.high),
                                  str(bar.low), str(bar.close), str(bar.volume)] for bar in bars])
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO candle_windows VALUES (?, ?, ?, ?)",
                                (*key, payload))
        return bars

    def previous_sessions(self):
        if self.history is None:
            history = []
            for days_ago in range(7, 0, -1):
                day = self.opened.date() - timedelta(days=days_ago)
                session = self.api.session(day) if day.weekday() < 5 else None
                if session is None:
                    continue
                start, end = session
                midpoint = min(start + timedelta(minutes=195), end)
                history.extend(self.window(5, start, midpoint, True))
                if midpoint < end:
                    history.extend(self.window(5, midpoint, end, True))
            if len(history) < 21:
                raise QuoteError("Too few previous five-minute candles to initialize the scanner")
            self.history = history
        return self.history

    def evaluate(self, now):
        day = self.opened.date()
        cutoff = min(now - DELAY, self.closed, datetime.combine(day, ENTRY_END, ET))
        cutoff = cutoff.replace(second=0, microsecond=0)
        if cutoff <= self.opened:
            return [], [], [], 0

        # Fetch the newest slice first. Older data is normally already on disk.
        block = timedelta(minutes=15)
        active = self.opened + ((cutoff - self.opened - timedelta(minutes=1)) // block) * block
        if self.recent is not None and self.recent[:2] == (active, cutoff):
            latest = self.recent[2]
        else:
            latest = self.window(1, active, cutoff, cutoff - active == block)
            if len(latest) == (cutoff - active) // timedelta(minutes=1):
                self.recent = (active, cutoff, latest)

        one = []
        start = self.opened
        while start < active:
            one.extend(self.window(1, start, start + block, True))
            start += block
        one.extend(latest)

        five = self.previous_sessions() + aggregate_five_minute(one)
        missing = (cutoff - self.opened) // timedelta(minutes=1) - len(one)
        if missing:
            return one, five, [], missing
        return one, five, scan(five, one, day, SYMBOL), 0


def quote_is_current(quote, as_of, minimum=None, sides=("bid", "ask")):
    latest = int((as_of + timedelta(seconds=15)).timestamp() * 1000)
    earliest = int((as_of - FRESH).timestamp() * 1000)
    if minimum is not None:
        earliest = max(earliest, minimum)
    return all(type(quote.get(f"{side}_date")) is int
               and earliest <= quote[f"{side}_date"] <= latest for side in sides)


def choose_contract(api, event, as_of, occupied):
    option_type = "call" if event["direction"] == "bullish" else "put"
    dates = [day for day in api.expirations(SYMBOL) if 2 <= (day - as_of.date()).days <= 14]
    eligible = []
    for expiry in dates[:2]:
        for item in records(api.chain(SYMBOL, expiry.isoformat()), "options", "option"):
            symbol = item.get("symbol") or ""
            if (symbol in occupied or not re.fullmatch(r"NVDA\d{6}[CP]\d{8}", symbol)
                    or symbol[10] != ("C" if option_type == "call" else "P")
                    or item.get("contract_size") != 100 or item.get("option_type") != option_type
                    or item.get("bid") is None or item.get("ask") is None
                    or not quote_is_current(item, as_of, event["confirmation_close_ms"])):
                continue
            bid, ask = money(item["bid"]), money(item["ask"])
            if 0 < bid <= ask <= 7:
                spread = (ask - bid) / ((ask + bid) / 2)
                if spread <= Decimal("0.10"):
                    eligible.append((spread, ask, symbol))
    return min(eligible)[1:] if eligible else None


def orders(api, account):
    reply = api.request(f"{account}/orders", params={"limit": 1000, "includeTags": "true"})
    return records(reply, "orders", "order")


def positions(api, account):
    return records(api.request(f"{account}/positions"), "positions", "position")


def available_funds(balance):
    for section, field in ((balance, "option_buying_power"),
                           (balance.get("pdt") or {}, "option_buying_power"),
                           (balance.get("margin") or {}, "option_buying_power"),
                           (balance.get("cash") or {}, "cash_available")):
        if section.get(field) is not None:
            return money(section[field])
    raise QuoteError("Tradier did not report available options buying power")


def save(db, state):
    with db:
        db.execute("INSERT OR REPLACE INTO session_state VALUES (1, ?)", (json.dumps(state),))


def load_state(db):
    row = db.execute("SELECT payload FROM session_state WHERE id = 1").fetchone()
    return json.loads(row[0]) if row else None


def preview_order(api, account, fields):
    preview = api.request(f"{account}/orders", method="POST",
                          fields=dict(fields, preview="true"))["order"]
    if preview.get("result") is not True or preview.get("status") != "ok":
        raise QuoteError("Tradier rejected the paper order preview")
    return preview


def submit(api, db, state, leg, deadline, limit=None):
    now = datetime.now(ET)
    tag = f"nvdapilot{now:%Y%m%d}{'in' if leg == 'entry' else 'out'}"
    fields = {"class": "option", "symbol": SYMBOL, "option_symbol": state["contract"],
              "side": "buy_to_open" if leg == "entry" else "sell_to_close",
              "quantity": "1", "type": "market" if limit is None else "limit",
              "duration": "day", "tag": tag}
    if limit is not None:
        fields["price"] = str(limit)
    preview = preview_order(api, state["account"], fields)
    if leg == "entry":
        cost = money(preview.get("order_cost", preview.get("cost")))
        balance = api.request(f"{state['account']}/balances")["balances"]
        if not 0 < cost <= min(Decimal(1500), available_funds(balance)):
            raise QuoteError("One-contract paper entry failed the cost or buying-power check")
    if datetime.now(ET) >= deadline:
        raise QuoteError(f"The paper {leg} submission window closed during preview")
    # Persist the tag before submitting. On interruption, look it up without resending.
    state.update(phase=leg, tag=tag, order_id=None, submitted_at=now.isoformat())
    save(db, state)
    reply = api.request(f"{state['account']}/orders", method="POST", fields=fields)["order"]
    order_id = reply.get("id")
    if type(order_id) is not int or order_id <= 0:
        raise QuoteError("Tradier did not return an order ID. The next check will look up its tag")
    state["order_id"] = order_id
    save(db, state)
    emit(step=f"{leg}_submitted", order_id=order_id, contract=state["contract"])


def find_order(api, db, state):
    account = state["account"]
    if state["order_id"] is None:
        matches = [order for order in orders(api, account) if order.get("tag") == state["tag"]]
        if len(matches) != 1:
            return None
        state["order_id"] = matches[0]["id"]
        save(db, state)
    order = api.request(f"{account}/orders/{state['order_id']}")["order"]
    side = "buy_to_open" if state["phase"] == "entry" else "sell_to_close"
    if order.get("option_symbol") != state["contract"] or order.get("side") != side:
        raise QuoteError("Paper order does not match the stored contract and side")
    return order


def update_position(api, db, state, now, closed):
    flatten = closed - timedelta(minutes=15)
    phase, account, contract = (state[key] for key in ("phase", "account", "contract"))
    if phase in {"entry", "exit"}:
        order = find_order(api, db, state)
        if order is None:
            return
        status = order["status"]
        if status not in TERMINAL:
            expired_entry = now >= datetime.fromisoformat(state["submitted_at"]) + timedelta(minutes=1)
            if phase == "entry" and status != "pending_cancel" and (expired_entry or now >= flatten):
                api.request(f"{account}/orders/{state['order_id']}", method="DELETE")
            return
        filled = money(order.get("exec_quantity") or 0)
        held = sum(money(row["quantity"]) for row in positions(api, account) if row["symbol"] == contract)
        if filled not in (0, 1):
            raise QuoteError("Unexpected filled quantity. Check the paper order and position")
        if phase == "entry":
            if held != filled:
                return  # The positions endpoint can lag the order endpoint.
            new_phase = "open" if filled == 1 else "done"
            price = money(order["avg_fill_price"]) if filled else Decimal(0)
            if filled and price <= 0:
                raise QuoteError("Tradier did not report a positive entry fill price")
            state.update(phase=new_phase, entry_price=str(price))
            if not filled:
                state["outcome"] = "entry_unfilled"
        elif filled == 1 and held == 0:
            state.update(phase="done", outcome="paper_round_trip")
        elif filled == 1:
            return
        else:
            raise QuoteError("The paper exit did not fill. Check the paper position before proceeding")
        save(db, state)
        emit(step=f"{phase}_confirmed", status=status, filled=filled, remaining_contracts=held,
             price=order.get("avg_fill_price"))
        return

    reason = "same_day_cutoff" if now >= flatten else None
    if reason is None:
        reply = api.request("markets/quotes", params={"symbols": contract})
        for quote in records(reply, "quotes", "quote"):
            if quote.get("symbol") != contract or not quote_is_current(quote, now - DELAY, sides=("bid",)):
                continue
            bid, entry = money(quote.get("bid") or 0), money(state["entry_price"])
            if 0 < bid <= entry * Decimal("0.75"):
                reason = "stop_loss"
            elif bid >= entry * Decimal("1.20"):
                reason = "profit_target"
    if not reason:
        return
    held = sum(money(row["quantity"]) for row in positions(api, account) if row["symbol"] == contract)
    if held != 1:
        raise QuoteError("Expected one confirmed paper contract before submitting an exit")
    if any(row.get("option_symbol") == contract and row.get("status") not in TERMINAL
           for row in orders(api, account)):
        raise QuoteError("An order for this paper contract is already working")
    state["exit_reason"] = reason
    emit(step="exit_signal", reason=reason, contract=contract)
    submit(api, db, state, "exit", closed)


def watch(api, session, journal=JOURNAL):
    import fcntl

    opened, closed = session
    flatten = closed - timedelta(minutes=15)
    last_entry_check = datetime.combine(opened.date(), ENTRY_END, ET) + FRESH
    with journal.with_suffix(".lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (closing(sqlite3.connect(journal)) as db,
              closing(sqlite3.connect(journal.with_name(CANDLES.name))) as candles_db):
            candle_cache = CandleCache(api, candles_db, session)
            db.execute("CREATE TABLE IF NOT EXISTS session_state(id INTEGER PRIMARY KEY, payload TEXT)")
            state = load_state(db)
            account = api.account()
            if state and state["account"] != account:
                raise QuoteError("The paper account has changed since this pilot started")
            if state and state["day"] == opened.date().isoformat() and state["phase"] == "done":
                emit(step="complete", remaining_contracts=0, outcome=state["outcome"])
                return
            if state and state["day"] != opened.date().isoformat():
                if state["phase"] != "done":
                    raise QuoteError("A prior paper run is unresolved. Reconcile its orders and position first")
                state = None
            emit(step="watching", environment="tradier_sandbox", day=opened.date(), stop_at=closed)
            if state:
                emit(step="resuming_paper_position", phase=state["phase"], contract=state["contract"])
            last_bar = None
            while opened <= (now := datetime.now(ET)) < closed:
                try:
                    if state:
                        if state["phase"] == "done":
                            emit(step="complete", remaining_contracts=0, outcome=state["outcome"])
                            return
                        update_position(api, db, state, now, closed)
                    else:
                        as_of = now - DELAY
                        if now >= flatten or as_of > last_entry_check:
                            emit(step="complete", reason="no_eligible_entry")
                            return
                        minute = as_of.replace(second=0, microsecond=0)
                        if as_of.time() >= ENTRY_START and minute != last_bar:
                            _, _, signals, missing = candle_cache.evaluate(now)
                            if missing:
                                emit(step="waiting_for_candles", missing_one_minute_bars=missing)
                            for event in signals:
                                confirmed = datetime.fromtimestamp(event["confirmation_close_ms"] / 1000, ET)
                                if not timedelta(0) <= as_of - confirmed <= FRESH:
                                    continue
                                held = {row["symbol"] for row in positions(api, account)}
                                working = {row.get("option_symbol") for row in orders(api, account)
                                           if row.get("status") not in TERMINAL}
                                candidate = choose_contract(api, event, as_of, held | working)
                                if candidate:
                                    ask, contract = candidate
                                    state = {"account": account, "contract": contract, "day": opened.date().isoformat(),
                                             "event_id": event["event_id"]}
                                    emit(step="entry_signal", event_id=state["event_id"], contract=contract,
                                         grade=event["grade"], limit=ask)
                                    try:
                                        submit(api, db, state, "entry",
                                               min(flatten, confirmed + DELAY + FRESH), limit=ask)
                                    finally:
                                        if "phase" not in state:
                                            state = None
                                    break
                                emit(step="entry_skipped", event_id=event["event_id"], reason="no_eligible_quote")
                            # Retry a fresh signal on the next new minute if the options feed lagged.
                            if not missing:
                                last_bar = minute
                except QuoteError as exc:
                    emit(step="paper_api_issue", message=str(exc))
                sleep(15)
            if state and state["phase"] != "done":
                raise QuoteError("Paper run unresolved at the session close. Check the paper order history")
            emit(step="complete", reason="session_ended")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("scan", "watch"))
    parser.add_argument("--date", type=date.fromisoformat, help="Eastern date for a read-only scan")
    args = parser.parse_args()
    now = datetime.now(ET)
    if args.date and (args.command != "scan" or args.date > now.date()):
        parser.error("--date is for read-only scans through today")
    try:
        api = Sandbox()
        day = args.date or now.date()
        session = api.session(day)
        if session is None:
            emit(step="market_closed", day=day)
        elif args.command == "scan":
            with closing(sqlite3.connect(CANDLES)) as db:
                one, five, signals, missing = CandleCache(api, db, session).evaluate(now)
            emit(environment="tradier_sandbox", mode="scan_only", day=day, one_minute_bars=len(one),
                 five_minute_bars=len(five), missing_one_minute_bars=missing, signals=len(signals))
            for signal in signals:
                emit(**signal)
        elif session[0] <= now < session[1]:
            watch(api, session)
        else:
            emit(step="market_closed", day=day)
    except BlockingIOError:
        parser.exit(1, "A paper pilot is already running.\n")
    except QuoteError as exc:
        parser.exit(1, f"{exc}\n")
    except (KeyError, TypeError, ArithmeticError, ValueError, OSError) as exc:
        parser.exit(1, f"Unexpected paper API or local state error ({type(exc).__name__}).\n")


if __name__ == "__main__":
    main()
