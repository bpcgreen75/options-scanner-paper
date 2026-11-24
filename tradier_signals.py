"""Detect Phase 1 NVDA signals from Tradier regular-session candles."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal

from paper_preview import ET, money


ENTRY_START = time(9, 45)
ENTRY_END = time(11, 30)


@dataclass(frozen=True)
class Bar:
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def closed_bars(rows, minutes, until):
    """Keep distinct, complete regular-session candles as of the feed's cutoff."""
    bars = {}
    for row in rows:
        stamp = row["timestamp"]
        if type(stamp) is not int:
            raise ValueError("Tradier candle is missing its Unix timestamp")
        start = datetime.fromtimestamp(stamp, ET)
        if (start.weekday() >= 5 or not time(9, 30) <= start.time() < time(16)
                or start.second or start.minute % minutes
                or start + timedelta(minutes=minutes) > until):
            continue
        bar = Bar(start, *(money(row[key]) for key in ("open", "high", "low", "close", "volume")))
        if (not 0 < bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
                or bar.volume < 0):
            raise ValueError("Tradier returned inconsistent candle values")
        if start in bars and bars[start] != bar:
            raise ValueError("Tradier returned conflicting candles for the same minute")
        bars[start] = bar
    return [bars[start] for start in sorted(bars)]


def aggregate_five_minute(bars):
    """Build five-minute bars from five consecutive closed one-minute bars."""
    by_start = {bar.start: bar for bar in bars}
    five = []
    for start in by_start:
        if start.minute % 5:
            continue
        parts = [by_start.get(start + timedelta(minutes=offset)) for offset in range(5)]
        if any(part is None for part in parts):
            continue
        five.append(Bar(start, parts[0].open, max(part.high for part in parts),
                        min(part.low for part in parts), parts[-1].close,
                        sum(part.volume for part in parts)))
    return five


def scores(bar, fast, slow, rsi, vwap, relative_volume):
    price_range = bar.high - bar.low
    volume_points = 2 * (relative_volume >= Decimal("1.5"))
    bullish = (3 * (bar.close > fast > slow) + 2 * (bar.close > vwap) + 2 * (rsi >= 55)
               + volume_points + (bar.close > bar.open
                                  and bar.close - bar.low >= price_range * Decimal("0.75")))
    bearish = (3 * (bar.close < fast < slow) + 2 * (bar.close < vwap) + 2 * (rsi <= 45)
               + volume_points + (bar.close < bar.open
                                  and bar.high - bar.close >= price_range * Decimal("0.75")))
    return bullish, bearish


def setups(bars, trading_date):
    """Calculate 9/21 EMA, Wilder RSI 14, five-bar breakouts and session VWAP."""
    opening = None
    value = volume = gain = loss = Decimal(0)
    for index, bar in enumerate(bars):
        if index == 0:
            fast = slow = bar.close
        else:
            delta = bar.close - bars[index - 1].close
            up, down = max(delta, Decimal(0)), max(-delta, Decimal(0))
            fast += (bar.close - fast) / 5
            slow += (bar.close - slow) / 11
            if index <= 14:
                gain += up / 14
                loss += down / 14
            else:
                gain = (13 * gain + up) / 14
                loss = (13 * loss + down) / 14
        if bar.start.date() != trading_date:
            continue
        if bar.start.time() == time(9, 30):
            opening = bar
        if opening is None:
            continue
        volume += bar.volume
        value += bar.volume * (bar.high + bar.low + bar.close) / 3
        if index < 20 or volume <= 0 or bar == opening or gain == loss == 0:
            continue
        baseline = sum(prior.volume for prior in bars[index - 20:index]) / 20
        if baseline <= 0:
            continue
        rsi = Decimal(100) if loss == 0 else 100 - 100 / (1 + gain / loss)
        bull, bear = scores(bar, fast, slow, rsi, value / volume, bar.volume / baseline)
        preceding = bars[index - 5:index]
        high = max(prior.high for prior in preceding)
        low = min(prior.low for prior in preceding)
        previous = preceding[-1].close
        levels = (
            ("bullish_momentum", "bullish", high, bull, bar.close > high),
            ("bearish_momentum", "bearish", low, bear, bar.close < low),
            ("first_candle_rule", "bullish", opening.high, bull, previous <= opening.high < bar.close),
            ("first_candle_rule", "bearish", opening.low, bear, bar.close < opening.low <= previous),
        )
        yield bar.start + timedelta(minutes=5), levels


def scan(five_minute, one_minute, trading_date, symbol="NVDA"):
    """Return the first qualifying confirmation per setup and strategy."""
    minutes = {bar.start: bar for bar in one_minute}
    signals = []
    for setup_end, levels in setups(five_minute, trading_date):
        for strategy, direction, level, points, breached in levels:
            if not breached or points < 8 or (symbol == "PLTR" and direction == "bullish"):
                continue
            for minute in range(5):
                bar = minutes.get(setup_end + timedelta(minutes=minute))
                if bar is None:
                    # A missing bar could contain the original confirmation.
                    break
                bullish = direction == "bullish"
                if not (bar.close > max(bar.open, level) if bullish else bar.close < min(bar.open, level)):
                    continue
                confirmed = bar.start + timedelta(minutes=1)
                if ENTRY_START <= confirmed.time() <= ENTRY_END:
                    setup_ms, confirmed_ms = (int(item.timestamp() * 1000) for item in (setup_end, confirmed))
                    ticker = f"NASDAQ:{symbol}"
                    signals.append({
                        "schema_version": 1, "event_type": "underlying_signal",
                        "event_id": f"{ticker}|1|{strategy}|{direction}|{setup_ms}",
                        "symbol": ticker, "strategy": strategy, "direction": direction,
                        "setup_timeframe_min": 5, "confirmation_timeframe_min": 1,
                        "setup_close_ms": setup_ms, "confirmation_close_ms": confirmed_ms,
                        "session_timezone": ET.key, "currency": "USD", "score": points,
                        "max_score": 10, "grade": "A" if points >= 9 else "B+",
                        "underlying_close": bar.close, "breakout_level": level,
                    })
                break
    return sorted(signals, key=lambda event: (event["confirmation_close_ms"], event["event_id"]))
