"""Paper-only TradingView receiver and replay. Uses Python's standard library."""

import argparse
import json
import os
import re
import sqlite3
from contextlib import closing
from decimal import Decimal
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from paper_preview import load, money, preview, underlying_key


PATH = "/webhooks/tradingview/v1"
SYMBOLS = {"NASDAQ:NVDA", "NASDAQ:MU", "NASDAQ:PLTR", "NASDAQ:TSLA", "NASDAQ:QQQ",
           "NYSE:NKE", "AMEX:USO", "NASDAQ:SPCX", "NASDAQ:CRWV"}
STRATEGIES = {"bullish_momentum": {"bullish"}, "bearish_momentum": {"bearish"},
              "first_candle_rule": {"bullish", "bearish"}}


def validate(event):
    if not isinstance(event, dict):
        raise ValueError("Expected a JSON object")
    for name in ("schema_version", "setup_timeframe_min", "confirmation_timeframe_min",
                 "setup_close_ms", "confirmation_close_ms", "score", "max_score"):
        if type(event.get(name)) is not int:
            raise ValueError(f"{name} must be an integer")
    if (event["schema_version"] != 1 or event["event_type"] != "underlying_signal"
            or event["session_timezone"] != "America/New_York" or event["currency"] != "USD"):
        raise ValueError("Unsupported signal format")
    if underlying_key(event["symbol"]) not in SYMBOLS:
        raise ValueError("Symbol outside the scanner watchlist")
    strategy, direction = event["strategy"], event["direction"]
    if direction not in STRATEGIES.get(strategy, ()):
        raise ValueError("Strategy and direction do not match")
    start, end = event["setup_close_ms"], event["confirmation_close_ms"]
    tf = event["confirmation_timeframe_min"]
    if (event["setup_timeframe_min"] != 5 or tf not in (1, 5) or start <= 0
            or end >= 4_102_444_800_000 or start % 300_000
            or end - start not in range(tf * 60_000, 300_001, tf * 60_000)):
        raise ValueError("Invalid setup or confirmation time")
    if event["event_id"] != f"{event['symbol']}|{tf}|{strategy}|{direction}|{start}":
        raise ValueError("Invalid event ID")
    score, maximum = event["score"], event["max_score"]
    if not 0 <= score <= maximum <= 10 or maximum == 0:
        raise ValueError("Invalid points")
    grade = next(label for threshold, label in ((90, "A"), (80, "B+"), (70, "B"), (0, "C"))
                 if score * 100 >= maximum * threshold)
    if event["grade"] != grade:
        raise ValueError("Grade does not match the score")
    for field in ("underlying_close", "breakout_level"):
        if type(event.get(field)) not in (int, Decimal) or money(event[field]) <= 0:
            raise ValueError(f"Invalid {field}")
    close, level = event["underlying_close"], event["breakout_level"]
    if not (close > level if direction == "bullish" else close < level):
        raise ValueError("Confirmation did not close beyond its breakout level")


def save(db, event_id, payload):
    with closing(sqlite3.connect(db, timeout=1)) as connection, connection:
        cursor = connection.execute("INSERT OR IGNORE INTO signals(event_id, payload) VALUES (?, ?)",
                                    (event_id, payload))
        if cursor.rowcount == 1:
            return "accepted"
        existing, = connection.execute("SELECT payload FROM signals WHERE event_id = ?", (event_id,)).fetchone()
        if json.loads(existing, parse_float=Decimal) != json.loads(payload, parse_float=Decimal):
            return "conflict"
        return "duplicate"


class Receiver(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # The hosted webhook path includes a secret. Do not log request URLs.
        return

    def do_GET(self):
        if self.path == "/healthz":
            self.reply(200, {"status": "ok", "mode": "paper"})
        else:
            self.reply(404, {"error": "Unknown endpoint"})

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if not compare_digest(self.path.encode(), self.server.webhook_path.encode()):
            self.reply(404, {"error": "Unknown endpoint"})
            return
        if self.headers.get_content_type() != "application/json":
            self.reply(415, {"error": "Expected application/json"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 16_384:
                self.reply(413, {"error": "Expected a body of at most 16 KB"})
                return
            self.connection.settimeout(2)
            payload = self.rfile.read(size).decode("utf-8")
            event = json.loads(payload, parse_float=Decimal)
            validate(event)
            status = save(self.server.database, event["event_id"], payload)
        except TimeoutError:
            self.reply(408, {"error": "Request body timed out"})
            return
        except (ValueError, KeyError, TypeError, ArithmeticError, RecursionError) as exc:
            self.reply(400, {"error": str(exc)})
            return
        except sqlite3.Error:
            self.reply(503, {"error": "Signal storage unavailable"})
            return
        self.reply({"accepted": 201, "duplicate": 200, "conflict": 409}[status],
                   {"status": status, "event_id": event["event_id"]})


def serve(database, port, host="127.0.0.1", token=None):
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS signals (event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, "
            "received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
    with ThreadingHTTPServer((host, port), Receiver) as server:
        server.database = database
        server.webhook_path = f"{PATH}/{token}" if token else PATH
        print(f"Paper receiver listening on {host}:{server.server_port}", flush=True)
        server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("paper_signals.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("serve")
    start.add_argument("--port", type=int, default=8765)
    start.add_argument("--hosted", action="store_true", help="Run behind a managed HTTPS proxy.")
    replay = commands.add_parser("replay")
    replay.add_argument("event_id")
    replay.add_argument("quotes")
    replay.add_argument("--exit-quotes")
    replay.add_argument("--early-close", action="store_true")
    replay.add_argument("--budget", type=money, default=Decimal("1500"))
    replay.add_argument("--entry-fee-per-contract", type=money, required=True)
    replay.add_argument("--exit-fee-per-contract", type=money, default=Decimal("0"))
    args = parser.parse_args()
    if args.command == "serve":
        if args.hosted:
            token = os.environ.get("PAPER_WEBHOOK_TOKEN", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
                parser.error("Set PAPER_WEBHOOK_TOKEN to a randomly generated URL-safe secret of at least 32 characters")
            serve(args.database, int(os.environ.get("PORT", args.port)), "0.0.0.0", token)
        else:
            serve(args.database, args.port)
        return
    try:
        if not args.database.is_file():
            raise ValueError("Start the receiver and record a signal first")
        with closing(sqlite3.connect(args.database)) as connection:
            row = connection.execute("SELECT payload FROM signals WHERE event_id = ?", (args.event_id,)).fetchone()
        if row is None:
            raise ValueError("Event ID not found")
        event = json.loads(row[0], parse_float=Decimal)
        result = preview(event, load(args.quotes), args.budget, Decimal("25"), Decimal("400"),
                         args.entry_fee_per_contract, args.exit_fee_per_contract,
                         load(args.exit_quotes) if args.exit_quotes else (), args.early_close)
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
