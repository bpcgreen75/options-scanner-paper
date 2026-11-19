# AI Options Trading Scanner · Phase 1

This TradingView indicator scores and alerts on underlying prices for **NVDA, MU, PLTR, TSLA, QQQ, NKE, USO, SPCX and CRWV**. The three strategies are bullish momentum, bearish momentum and the First Candle Rule. PLTR is puts-only, so its chart signals and alerts are bearish only. Phase 2 will select options using an external chain and quote API. Phase 1 emits directional observations and runs without brokerage credentials or order execution.

## Set up in TradingView

The watchlist compares ticker names independently of the chart's data-feed prefix. The regular-hours, Eastern timezone, USD, and stock/ETF checks still apply. The alert's symbol field retains TradingView's qualified chart identifier.

1. Open one of the five NASDAQ symbols on a standard candlestick chart. Select **Regular trading hours** and **1 minute**. Setting the chart clock to New York makes manual checks easier. The script uses New York time regardless of the chart clock.
2. In Pine Editor, create a blank indicator and paste all of [phase1_scanner.pine](phase1_scanner.pine), beginning with the version directive. Save and choose **Add to chart**. Check the Pine Editor for compilation errors and the chart for runtime errors. Load prior regular-session history for the EMA, RSI, and volume baseline.
3. For live testing, create an alert with this indicator as the condition and choose **Any alert() function call**. Start with the alert log or an app notification. Create a separate alert for each symbol you want to monitor. Check your TradingView account's active-alert allowance. Saved server-side alerts use a snapshot. Recreate a PLTR alert made with the original version to apply the puts-only rule. The existing NVDA alert can stay active for the initial NVDA test. [TradingView alert docs](https://www.tradingview.com/pine-script-docs/concepts/alerts/)
4. Use a **5-minute chart** to inspect standalone confirmation. Its alerts are disabled by default to avoid parallel notifications. Enable **Also send standalone 5-minute alerts** only if you want that separate stream. A 5-minute alert and a 1-minute alert can refer to the same setup and have distinct event IDs.
5. When you have a test receiver, enter its public HTTPS URL under **Webhook URL** in the alert dialog. TradingView requires two-factor authentication for webhook alerts. Valid JSON is sent as an HTTP POST with `application/json`. Use HTTPS on port 443, keep credentials out of the body, and check the alert log's webhook status column. TradingView cancels requests that take longer than three seconds. [TradingView webhook setup](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/)

## Signal rules

| Strategy | Completed 5-minute setup bar |
| --- | --- |
| Bullish momentum | Close strictly above the highest high of the previous five regular-session bars. |
| Bearish momentum | Close strictly below the lowest low of the previous five regular-session bars. |
| First Candle Rule | After the first candle closes, the 5-minute close crosses from at/below its high to above, or from at/above its low to below. Returning inside or exactly to the breached level can rearm that direction for a new breakout. |

- **Opening range.** Use the exact 09:30–09:35 ET time-based bar. Wick extremes define the range, closing prices trigger breakouts. The range is fixed through that session and unavailable if the required opening bar is missing. The earliest possible First Candle Rule setup closes at 09:40 ET, with the first possible 1-minute confirmation at 09:41.
- **Confirmation.** Within the next five clock minutes, the first eligible bar must close strictly beyond the setup level and have a body in the breakout direction. On 1m, this gives up to five closed 1-minute bars. On 5m, only the next contiguous 5-minute bar can confirm. Equality, an intrabar excursion, a doji, an expired window and an overnight carryover all fail confirmation. Each strategy/direction emits at most once per setup. Distinct strategies can confirm together and generate separate messages. The chart shows one grade label per direction per bar. The Data Window shows which strategy levels confirmed.
- **Session.** Weekdays, 09:30–16:00 ET in `America/New_York`, intersected with the symbol's regular market session. The default latest confirmation close is 11:30 ET inclusive. Set it as late as 16:00 ET if desired. Holidays and early closes depend on exchange-session bars from the feed. The IANA timezone handles daylight-saving changes. [Pine time and timezones](https://www.tradingview.com/pine-script-docs/concepts/time/)
- **Safeguards.** The indicator rejects nonstandard charts, unsupported resolutions, extended-hours settings, non-Eastern or non-USD symbols, and assets other than stocks or funds. It limits symbols to the initial five by default. It uses one higher-timeframe request on a 1m chart and local 5m data on a 5m chart. It never reads a developing 5m setup from a 1m chart. The offset plus lookahead pattern exposes completed values in the following 5m interval. Markers and alerts require the confirmation bar to close. Feed corrections and changes to the available history can still affect results. [Higher-timeframe requests](https://www.tradingview.com/pine-script-docs/concepts/other-timeframes-and-data/) · [Confirmed bars](https://www.tradingview.com/pine-script-docs/concepts/bar-states/)

### Setup scoring

| Component | Weight | Bullish pass | Bearish pass |
| --- | ---: | --- | --- |
| EMA trend | 3 | Close > EMA 9 > EMA 21 | Close < EMA 9 < EMA 21 |
| VWAP | 2 | Close above VWAP | Close below VWAP |
| RSI 14 | 2 | RSI ≥ 55 | RSI ≤ 45 |
| Relative volume | 2 | At least 1.5× baseline | At least 1.5× baseline |
| Candle direction and location | 1 | Green body, close in top quarter | Red body, close in bottom quarter |

VWAP accumulates 5m typical price × volume from the opening bar, divided by cumulative volume. Relative volume divides setup-bar volume by the average volume of the previous 20 regular 5m bars, excluding the setup bar. EMA, RSI, momentum lookback and relative volume can use prior-session regular bars. Relative volume uses a rolling baseline, so intraday seasonality can affect it. Missing required indicator values suppress the setup.

Grades use points divided by enabled points. **A ≥ 90%, B+ ≥ 80%, B ≥ 70%, C below 70%.** With full scoring the maximum is 10. Disabling components changes the denominator and can make grades unreachable. **Require every enabled component to pass** is an optional hard filter. The default alert minimum is B+. Chart labels show all confirmed grades independently of the alert threshold. Grades express technical agreement and carry no return or win-rate estimate.

## Webhook contract · version 1

One JSON object per event. Every field is required. [Example JSON](example_alert.json) uses illustrative prices and scores.

| Field | Type and meaning |
| --- | --- |
| `schema_version` | Integer, `1`. |
| `event_type` | String, `underlying_signal`. |
| `event_id` | String formed as `symbol\|confirmation_timeframe_min\|strategy\|direction\|setup_close_ms`. |
| `symbol` | Exchange-qualified underlying string, e.g. `NASDAQ:NVDA`. |
| `strategy` | `bullish_momentum`, `bearish_momentum`, or `first_candle_rule`. |
| `direction` | `bullish` or `bearish`. Momentum strategy and direction must agree. |
| `setup_timeframe_min` | Integer, `5`. |
| `confirmation_timeframe_min` | Integer, `1` or `5`. |
| `setup_close_ms`, `confirmation_close_ms` | Unix epoch milliseconds as integers. Confirmation is later than setup by at most 300,000 ms. |
| `session_timezone`, `currency` | Strings, `America/New_York` and `USD`. |
| `underlying_close`, `breakout_level` | JSON numbers, rounded to the chart symbol's tick size. They represent the confirmation close and its required breakout level. |
| `score`, `max_score` | Integers, with `0 ≤ score ≤ max_score ≤ 10` and `max_score ≥ 1`. |
| `grade` | `A`, `B+`, `B` or `C`, derived from the integer score ratio. |

For FastAPI, accept the object as a Pydantic request body at a versioned endpoint such as `POST /webhooks/tradingview/v1`. Validate field types and cross-field rules, authenticate incoming deliveries at the endpoint, record receipt time, and deduplicate by `event_id`. Persist or queue accepted events and return promptly. If both chart resolutions are active, group equivalent setups by symbol, strategy, direction and setup close. The durable receiver and its hosted endpoint are Phase 2 deliverables. [FastAPI request bodies](https://fastapi.tiangolo.com/tutorial/body/)

## Acceptance checklist

Use historical charts or Bar Replay for visual checks and a live session for actual alert delivery. Record symbol, chart resolution, input settings, setup close, confirmation close, strategy and expected grade for each observation.

- [ ] Paste, save and add the script on both resolutions. Check all five qualified symbols. Try a 15m chart, Heikin Ashi and extended hours to verify the error messages.
- [ ] On a regular session, match the plotted levels to the 09:30–09:35 candle. Confirm that levels first appear on the 09:35-start interval, remain fixed, and never carry into the next morning. Repeat across a DST change and check a half-day or a chart with a missing opening bar.
- [ ] Find a bullish and bearish example for both momentum and the opening range. Check wick-only, equal-to-level, doji and failed confirmation cases. On 1m verify the first qualifying close fires once in the following five minutes. On 5m verify the next bar must confirm. Check that 11:30 ET can qualify with default inputs and 11:31 ET cannot.
- [ ] Confirm grades for 9/10, 8/10, 7/10 and 6/10 as A, B+, B and C. Toggle each strategy and scoring component, inspect the denominator in the Data Window, and test strict filtering and minimum alert grade.
- [ ] Capture confirmed markers on a live chart, reload with the same inputs and feed, then compare timestamps and levels. Check that a temporary intrabar break generates no marker or alert. Confirm the 5m view stays silent with standalone alerts off.
- [ ] At a live graded confirmation, compare the alert log and captured webhook body with the contract. Verify POST content type, timestamp ordering, unique IDs for simultaneous strategies and idempotent handling of repeated delivery. Recreate an alert after changing its minimum grade and confirm the updated snapshot.

The code was reviewed against TradingView's Pine v6 documentation. The local payload check exercised serialization with sample values. TradingView Editor compilation, chart behavior and live delivery require the checks above.

## Offline paper preview

The preview matches supported feed aliases to the same underlying and preserves the original alert ID. It rejects bullish PLTR signals even if they come from an older TradingView alert. The preview evaluates one signal at a time. The $10,000 shared capital limit and simultaneous-position tracking require a portfolio layer.

`paper_preview.py` runs locally with Python 3. From the folder containing the files, try this example with synthetic quotes and illustrative settings.

```powershell
python paper_preview.py example_alert.json paper_quotes_example.json --entry-fee-per-contract 0 --exit-quotes paper_exit_quotes_example.json
```

The preview should show three synthetic calls, a $1,200 estimated entry, a $300 loss limit and a $3.00 illustrative stop trigger. The default budget is $1,500, with a hard ceiling of $1,750 including entry fees. Use `--budget 1750` to test the ceiling. The position loss limit is the smaller of $400 and 25% of the assumed entry premium across all contracts. Estimated entry and exit fees use part of that loss allowance. Enter the per-contract entry fee and optionally supply `--exit-fee-per-contract`, which defaults to zero for the fixture.

The preview accepts A/B+ signals, standard 100-share contracts, asks up to $7, quotes timestamped zero to five seconds after confirmation and spreads no wider than 10% of midpoint. It ranks candidates by narrowest percentage spread, then lowest ask, and sizes in whole contracts. The ask is used as a hypothetical fill and the stop is an unrounded calculation. Actual fills, valid tick rounding, expiration and liquidity filters, live quote retrieval, and order handling remain future work. A stop trigger does not guarantee an exit price or maximum realized loss.

The paper entry window opens at 9:45 a.m. ET to model Robinhood's stop-market entry restriction. The replay compares 15%, 20%, and 25% profit targets on the same entry, with the same stop for each. A supplied bid at or below the stop simulates a fill at that bid. Before the closing window, a bid at or above the target simulates a fill at the target. At or after the closing cutoff, the first usable bid before the close simulates a same-day exit for any remaining scenario. Net results deduct the estimated round-trip fees. Each scenario exits at most once.

The closing window starts 15 minutes before regular close. For a 1 p.m. early-close day, supply `--early-close` to start it at 12:45 p.m. ET. The offline tool relies on the supplied session and quote history, and has no exchange holiday calendar or live fill notifications. With no eligible exit observation, it reports the scenario as unresolved and ignores overnight observations. The synthetic example hits the 15% and 20% targets and exits the 25% scenario at the time cutoff.

## Local webhook test

The standard-library Python receiver runs on your computer and records alerts in SQLite. It accepts valid Phase 1 JSON, acknowledges exact repeat deliveries without creating a second record, and flags conflicting payloads with the same event ID. From the files' folder, start it with:

```powershell
python paper_receiver.py serve
```

In a second PowerShell window in the same folder, submit and replay the synthetic example:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/webhooks/tradingview/v1 -ContentType application/json -InFile example_alert.json
python paper_receiver.py replay 'NASDAQ:NVDA|1|first_candle_rule|bullish|1795016400000' paper_quotes_example.json --entry-fee-per-contract 0 --exit-quotes paper_exit_quotes_example.json
```

This receiver binds to your computer only. Live TradingView delivery requires a hosted, authenticated HTTPS endpoint. Replays use recorded quote files. Automatic quote retrieval and portfolio limits remain separate integration work. Keep the existing NVDA alert active until a hosted endpoint is ready.

## Host the paper receiver

One deployment option is a Render Docker web service. Use the folder containing the Dockerfile and both Python files as the service's root directory. Choose an always-on service with a persistent disk mounted at `/app/storage` so the SQLite history survives restarts. Set the health check path to `/healthz`. The service listens on Render's `PORT`; Render serves it over HTTPS. Hosting and disk selection may incur charges. [Render web services](https://render.com/docs/web-services) · [Render persistent disks](https://render.com/docs/disks)

Generate a private URL token locally with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Set it as the service's secret environment variable `PAPER_WEBHOOK_TOKEN`. The webhook address is `https://YOUR-SERVICE.onrender.com/webhooks/tradingview/v1/YOUR-TOKEN`. Treat the full webhook URL as a secret, especially in screenshots and proxy logs. The receiver does not write request URLs to its own logs. A missing or invalid token prevents hosted mode from starting.

After deployment, check `/healthz` and send the example alert to the hosted URL twice. Verify the first returns HTTP 201 and the repeat returns HTTP 200 with duplicate status. Then update the existing NVDA alert's Webhook URL, save, and check TradingView's webhook status after a qualifying signal. This service only validates and records alerts. Quote intake, portfolio controls, and order execution are separate work.

## Development roadmap

1. **Phase 1 · Signal engine and paper observation.** Complete the acceptance checklist and log the original three strategies by symbol, timeframe and grade. Treat fair-value-gap and breakout/retest rules as separately versioned variants.
2. **Phase 2 · FastAPI and options selection.** Validate and store signals, then fetch timestamped chains and quotes through an external options API, with Tradier as the initial candidate. Map bullish to calls and bearish to puts. For a long candidate, enforce a live ask of **$7.00 or less per underlying share** ($700 premium for a standard 100-share contract, before fees). Evaluate spread, quote age, expiration, delta, open interest and volume separately. Record when no contract qualifies.
3. **Phase 3 · AI explanations.** Generate explanations from recorded signals and verified quotes. Keep explanations downstream of deterministic qualification.
4. **Phase 4 · Independent validation.** Evaluate underlying signals first, then reconstruct historical option selection and fills using timestamped quotes, realistic bid/ask assumptions, fees and slippage. Use chronological holdouts and forward paper results. Track sample sizes, fill rate, expectancy and drawdown by strategy. Phase 1 reports no backtest results.
5. **Phase 5 · Deployment.** Add delivery monitoring, a dashboard, mobile notifications and a journal for manually approved trades.
