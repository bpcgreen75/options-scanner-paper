"""Run the HTTP receiver and, when enabled, the scheduled Tradier paper pilot."""

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from threading import Event, Thread

import paper_receiver
import tradier_pilot as pilot
from tradier_quotes import QuoteError


def read_paper_state(journal):
    if not journal.is_file():
        return None
    with closing(sqlite3.connect(journal.as_uri() + "?mode=ro", uri=True)) as db:
        return pilot.load_state(db)


def session_action(now, session, state):
    day = now.date().isoformat()
    if state and state["phase"] != "done":
        if state["day"] != day:
            return "prior_paper_position_unresolved"
        if session is None or now >= session[1]:
            return "paper_position_unresolved_at_close"
        return "watch" if now >= session[0] else "waiting_for_market_open"
    if session is None:
        return "market_closed"
    opened, closed = session
    if now < opened:
        return "waiting_for_market_open"
    if state and state["day"] == day:
        return "paper_session_already_complete"
    latest = datetime.combine(now.date(), pilot.ENTRY_END, pilot.ET) + pilot.DELAY + pilot.FRESH
    if now >= closed - timedelta(minutes=15) or now > latest:
        return "paper_entry_window_closed"
    return "watch"


def schedule_paper(stop, journal=pilot.JOURNAL):
    pilot.EVENT_LOG = journal.with_name("tradier-nvda-pilot.log")
    try:
        api = pilot.Sandbox()
    except QuoteError as exc:
        pilot.emit(step="paper_scheduler_error", message=str(exc))
        return

    pilot.emit(step="paper_scheduler_started", environment="tradier_sandbox")
    finished = None
    last_message = None
    while not stop.is_set():
        now = datetime.now(pilot.ET)
        day = now.date()
        if day != finished:
            try:
                session = api.session(day)
                state = read_paper_state(journal)
                action = session_action(now, session, state)
                if action == "watch":
                    pilot.watch(api, session, journal=journal)
                    finished = day
                elif last_message != (day, action):
                    pilot.emit(step=action, day=day, opens_at=session[0] if session else None)
                    last_message = day, action
            except BlockingIOError:
                if last_message != (day, "paper_pilot_already_running"):
                    pilot.emit(step="paper_pilot_already_running", day=day)
                    last_message = day, "paper_pilot_already_running"
            except QuoteError as exc:
                pilot.emit(step="paper_scheduler_retry", day=day, message=str(exc))
            except (ArithmeticError, KeyError, OSError, TypeError, ValueError, sqlite3.Error) as exc:
                pilot.emit(step="paper_scheduler_retry", day=day, error=type(exc).__name__)
        stop.wait(30)


def main():
    stop = Event()
    if os.getenv("TRADIER_PAPER_AUTO") == "1":
        Thread(target=schedule_paper, args=(stop,), name="tradier-paper", daemon=True).start()
    try:
        paper_receiver.main()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
