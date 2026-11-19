"""Read-only Tradier options data for the paper scanner."""

import argparse
import http.client
import json
import os
from datetime import date, datetime
from decimal import Decimal
from time import time
from urllib.parse import urlencode

from paper_preview import ET


HOSTS = {"sandbox": "sandbox.tradier.com", "live": "api.tradier.com"}
MAX_BODY = 4 * 1024 * 1024


class QuoteError(Exception):
    """A provider response could not be used."""


def request_json(environment, token, resource, parameters, timeout):
    connection = http.client.HTTPSConnection(HOSTS[environment], timeout=timeout)
    try:
        connection.request("GET", f"/v1/markets/options/{resource}?{urlencode(parameters)}",
                           headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        with connection.getresponse() as response:
            if response.status != 200:
                raise QuoteError(f"Tradier returned HTTP {response.status}")
            body = response.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                raise QuoteError("Tradier response exceeds the size limit")
            return json.loads(body, parse_float=Decimal)
    except (OSError, http.client.HTTPException):
        raise QuoteError("Tradier connection failed or timed out") from None
    except ValueError:
        raise QuoteError("Tradier returned invalid JSON") from None
    finally:
        connection.close()


def records(response, container, key):
    """Tradier can return a single object or an array for the same field."""
    if not isinstance(response, dict) or container not in response:
        raise QuoteError(f"Tradier response is missing {container}")
    outer = response[container]
    if outer is None or outer == "null":
        return []
    if not isinstance(outer, dict) or key not in outer:
        raise QuoteError(f"Tradier response is missing {key}")
    value = outer[key]
    if value is None or value == "null":
        return []
    return value if isinstance(value, list) else [value]


def chain_quotes(response, ticker, expiry, alert):
    after = alert["confirmation_close_ms"]
    before = min(after + 5000, int(time() * 1000))
    option_type = {"bullish": "call", "bearish": "put"}[alert["direction"]]
    for item in records(response, "options", "option"):
        if not isinstance(item, dict):
            continue
        if (item.get("underlying") != ticker or item.get("root_symbol") != ticker
                or item.get("option_type") != option_type or item.get("contract_size") != 100
                or item.get("expiration_date") != expiry):
            continue
        contract = item.get("symbol")
        bid, ask = item.get("bid"), item.get("ask")
        bid_ms, ask_ms = item.get("bid_date"), item.get("ask_date")
        if not isinstance(contract, str) or not contract:
            continue
        if any(type(stamp) is not int or not after <= stamp <= before
               for stamp in (bid_ms, ask_ms)):
            continue
        if not all(type(price) in (int, Decimal) and Decimal(price).is_finite() for price in (bid, ask)):
            continue
        yield dict(underlying=alert["symbol"], contract=contract, type=option_type,
                   multiplier=100, bid=bid, ask=ask, quote_ms=max(bid_ms, ask_ms))


class TradierOptions:
    def __init__(self, environment, token):
        if not token or not token.strip():
            raise QuoteError(f"Set TRADIER_{environment.upper()}_TOKEN")
        self.environment = environment
        self.token = token

    def get(self, resource, timeout=3, **parameters):
        return request_json(self.environment, self.token, resource, parameters, timeout)

    def expirations(self, ticker, timeout=3):
        response = self.get("expirations", timeout, symbol=ticker)
        try:
            return sorted({date.fromisoformat(value) for value in records(response, "expirations", "date")})
        except (TypeError, ValueError):
            raise QuoteError("Tradier returned an invalid expiration date") from None

    def chain(self, ticker, expiry, timeout=3):
        return self.get("chains", timeout, symbol=ticker, expiration=expiry, greeks="false")

    def for_signal(self, event):
        if self.environment != "live":
            raise QuoteError("Delayed sandbox quotes cannot be used for signal evaluation")
        confirmation = event["confirmation_close_ms"] / 1000
        day = datetime.fromtimestamp(confirmation, ET).date()
        ticker = event["symbol"].split(":")[-1]
        deadline = confirmation + 5
        now = time()
        remaining = deadline - now
        if not confirmation <= now < deadline:
            return []
        expirations = self.expirations(ticker, min(remaining, 3))
        dates = [expiry.isoformat() for expiry in expirations if 2 <= (expiry - day).days <= 14]
        quotes = []
        for expiry in dates[:2]:
            remaining = deadline - time()
            if remaining <= 0:
                break
            try:
                response = self.chain(ticker, expiry, min(remaining, 3))
                if time() <= deadline:
                    quotes.extend(chain_quotes(response, ticker, expiry, event))
            except QuoteError:
                if not quotes:
                    raise
                break
        return quotes


def main():
    parser = argparse.ArgumentParser(description="Check Tradier connectivity using a sample options chain.")
    parser.add_argument("ticker", nargs="?", default="NVDA")
    parser.add_argument("--environment", choices=HOSTS, default="sandbox")
    args = parser.parse_args()
    ticker = args.ticker.upper()
    try:
        api = TradierOptions(args.environment, os.getenv(f"TRADIER_{args.environment.upper()}_TOKEN"))
        today = datetime.now(ET).date()
        expiry = next((value.isoformat() for value in api.expirations(ticker) if value >= today), None)
        count = len(records(api.chain(ticker, expiry), "options", "option")) if expiry else 0
    except QuoteError as exc:
        parser.error(str(exc))
    print(json.dumps({"environment": args.environment, "symbol": ticker, "expiration": expiry,
                      "contracts": count, "mode": "connectivity_check"}))


if __name__ == "__main__":
    main()
