"""Offline options preview. Accepts saved quotes and never submits orders."""

import argparse
import json
from datetime import datetime, timedelta, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")


def underlying_key(symbol):
    exchange, _, ticker = symbol.partition(":")
    if exchange in ("BATS", "NASDAQ") and ticker in ("NVDA", "MU", "PLTR", "TSLA", "QQQ", "SPCX", "CRWV"):
        return f"NASDAQ:{ticker}"
    if ticker == "NKE" and exchange in ("BATS", "NYSE"):
        return "NYSE:NKE"
    if ticker == "USO" and exchange in ("BATS", "AMEX", "NYSEARCA"):
        return "AMEX:USO"
    return symbol


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_float=Decimal)


def money(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Prices and limits must be finite.")
    return result


def preview(signal, quotes, budget, loss_pct, loss_dollars, fee, exit_fee=Decimal("0"),
            exit_quotes=(), early_close=False):
    if not 0 < budget <= 1750 or not 0 < loss_pct <= 25 or not 0 < loss_dollars <= 400:
        raise ValueError("Maximums are $1,750 per trade, 25% loss, and $400 loss.")
    if fee < 0 or exit_fee < 0:
        raise ValueError("Fees must be nonnegative.")
    if signal["schema_version"] != 1 or signal["event_type"] != "underlying_signal":
        raise ValueError("Expected a Phase 1 underlying signal.")
    option_type = {"bullish": "call", "bearish": "put"}[signal["direction"]]
    result = {"event_id": signal["event_id"], "mode": "offline_paper_preview"}
    underlying = underlying_key(signal["symbol"])
    if underlying == "NASDAQ:PLTR" and option_type == "call":
        return {**result, "status": "skipped", "reason": "PLTR is puts only"}
    if signal["grade"] not in ("A", "B+"):
        return {**result, "status": "skipped", "reason": "Grade below B+"}

    confirmation_ms = signal["confirmation_close_ms"]
    confirmation = datetime.fromtimestamp(confirmation_ms / 1000, ET)
    session_close = confirmation.replace(hour=13 if early_close else 16, minute=0, second=0, microsecond=0)
    flatten = session_close - timedelta(minutes=15)
    if confirmation.weekday() >= 5 or confirmation.time() < time(9, 45) or confirmation >= flatten:
        return {**result, "status": "skipped", "reason": "Outside the paper entry window"}
    flatten_ms = int(flatten.timestamp() * 1000)
    close_ms = int(session_close.timestamp() * 1000)

    candidates = []
    for quote in quotes:
        if (underlying_key(quote["underlying"]) != underlying or quote["type"] != option_type
                or quote["multiplier"] != 100):
            continue
        quote_ms = quote["quote_ms"]
        if not 0 <= quote_ms - confirmation_ms <= 5000 or quote_ms >= flatten_ms:
            continue
        ask, bid = money(quote["ask"]), money(quote["bid"])
        if not 0 < bid <= ask <= Decimal("7.00"):
            continue
        spread = (ask - bid) / ((ask + bid) / 2)
        if spread > Decimal("0.10"):
            continue
        cost = ask * 100 + fee
        if cost <= budget:
            candidates.append((spread, ask, quote["contract"], quote_ms, cost))

    if not candidates:
        return {**result, "status": "skipped", "reason": "No quote meets the paper filters and budget"}
    _, ask, contract, entry_ms, cost = min(candidates)
    quantity = int(budget // cost)
    shares = quantity * 100
    premium = ask * shares
    loss_limit = min(loss_dollars, premium * loss_pct / 100)
    estimated_fees = quantity * (fee + exit_fee)
    if estimated_fees >= loss_limit:
        return {**result, "status": "skipped", "reason": "Estimated round-trip fees exhaust the loss limit"}
    stop = ask - (loss_limit - estimated_fees) / shares
    observations = sorted(
        (quote["quote_ms"], money(quote["bid"]))
        for quote in exit_quotes
        if quote["contract"] == contract and entry_ms < quote["quote_ms"] < close_ms
    )
    scenarios = []
    for pct in (15, 20, 25):
        target = ask * (1 + Decimal(pct) / 100)
        outcome = {"profit_target_pct": pct, "target_per_share": str(target),
                   "status": "unresolved", "reason": "No qualifying same-session exit quote supplied"}
        for stamp, bid in observations:
            if bid <= 0:
                continue
            if bid <= stop:
                reason, fill = "stop_loss", bid
            elif stamp >= flatten_ms:
                reason, fill = "same_day_cutoff", bid
            elif bid >= target:
                reason, fill = "profit_target", target
            else:
                continue
            outcome.update(status="simulated_exit", reason=reason, exit_quote_ms=stamp,
                           assumed_exit_per_share=str(fill),
                           net_pnl=str((fill - ask) * shares - estimated_fees))
            break
        scenarios.append(outcome)

    return {
        **result,
        "status": "candidate",
        "contract": contract,
        "option_type": option_type,
        "quantity": quantity,
        "assumed_fill_per_share": str(ask),
        "estimated_entry_cost_including_fees": str(quantity * cost),
        "position_loss_limit_including_estimated_fees": str(loss_limit),
        "estimated_round_trip_fees": str(estimated_fees),
        "illustrative_stop_trigger_per_share": str(stop),
        "same_day_exit_starts_et": flatten.isoformat(),
        "regular_session_closes_et": session_close.isoformat(),
        "exit_scenarios": scenarios,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("signal")
    parser.add_argument("quotes")
    parser.add_argument("--budget", type=money, default=Decimal("1500"))
    parser.add_argument("--loss-pct", type=money, default=Decimal("25"))
    parser.add_argument("--loss-dollars", type=money, default=Decimal("400"))
    parser.add_argument("--entry-fee-per-contract", type=money, required=True)
    parser.add_argument("--exit-fee-per-contract", type=money, default=Decimal("0"))
    parser.add_argument("--exit-quotes", help="JSON array of contract, quote_ms and bid observations.")
    parser.add_argument("--early-close", action="store_true", help="Use a 1 p.m. ET session close for this replay.")
    args = parser.parse_args()
    try:
        result = preview(load(args.signal), load(args.quotes), args.budget,
                         args.loss_pct, args.loss_dollars, args.entry_fee_per_contract,
                         args.exit_fee_per_contract, load(args.exit_quotes) if args.exit_quotes else (),
                         args.early_close)
    except (OSError, ValueError, TypeError, KeyError, ArithmeticError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
