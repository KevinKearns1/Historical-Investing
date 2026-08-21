# Historical-Investing — context for Claude Code

A point-in-time trading simulator. Ten strategies (five equity day-trading, five
options), $25,000 starting capital, originally targeting 2000-01-01 → 2000-12-31,
now able to run across decades.

**Read `README.md` for the honest limitations and `docs/DATA_SOURCES.md` for
what data actually exists.** This file is the working context: the decisions,
the traps, and the current state.

---

## The invariant everything rests on

**On any simulated date, nothing can see data that had not yet printed.**

This is enforced structurally, not by convention:

- `SimClock.now` moves forward only; advancing backwards raises `LookaheadError`.
- Every market-data read passes through `PointInTimeFeed._visible_slice`, which
  clamps to the clock. There is one gate, so there is no way around it.
- **During a session, today's daily bar does not exist yet** — only yesterday's
  and older. This kills the most common backtest bug: using today's close to
  decide something at 11:00 this morning.
- Fundamentals key on `available_on` (the filing date), never `period_end`.

`tests/test_no_lookahead.py` walks a session minute by minute asserting the
invariant at every step. **If you touch the data layer, run these first.** The
guard is real enough that it caught a diagnostic script during development that
tried to step the clock backwards.

---

## Current state — read this before claiming any result

**The engine has NEVER been run against real market data.** Every run so far
used `scripts/make_demo_data.py`, which generates clearly-labelled synthetic
prices. Those runs prove the plumbing works and say **nothing** about whether
any strategy would have made money.

The cloud environment this was built in blocks every financial data host at its
egress proxy. Running locally fixes it — `scripts/preflight.py` verifies a
machine can reach the data and distinguishes a proxy denial from a genuine
provider block.

So: **any P&L number in this repo's history is a self-test, not a finding.**
When real data lands, expect to re-tune. Several strategy thresholds were
calibrated against synthetic data and are placeholders.

---

## Layout

```
engine/
  clock.py           SimClock, 2000 holiday/half-day calendar
  data.py            PointInTimeFeed — THE visibility gate
  intraday.py        minute-path reconstruction from daily OHLCV
  intraday_source.py recorded bars if present, reconstruction if not
  provenance.py      bar-source tagging, vendor gap-fill detection
  options.py         BSM, greeks, vol surface, rate curve, expiries
  microstructure.py  spreads, commissions, fees (delegates to era.py)
  era.py             date-aware rules: ticks, shorting law, leverage, costs
  fundamentals.py    point-in-time KPIs keyed on filing date
  portfolio.py       cash, positions, Reg T margin, P&L, assignment
  broker.py          order types, pessimistic fills
  risk.py            sizing, loss limits, drawdown kill switch
  backtest.py        the session loop
  metrics.py         Sharpe vs T-bill, attribution
strategies/          eq01-05, op01-05, base.py, filters.py
scripts/             preflight · fetch_data · fetch_intraday ·
                     fetch_fundamentals · fetch_earnings_edgar ·
                     run_backtest · walk_forward · make_demo_data
```

---

## Decisions that look wrong until you know why

**OP-05 is a put spread, not a collar.** A collar sells a call against stock you
own. This account owns no index, so the short call would be *naked* — undefined
loss on a $25k account. Worse, it backtests *beautifully* through 2000 because
the market fell, then gives it all back in one squeeze (Nasdaq rose 14% in four
sessions from 2000-05-24). Don't "fix" it back into a collar.

**Screens treat unknown as PASS, not fail** (`strategies/filters.py`). If a
missing filing counted as a failure, the screen would become a filter on *data
coverage* — worst for exactly the small, distressed and delisted names — and
reintroduce survivorship bias wearing a fundamental screen as a disguise.

**Earnings vol is added as variance, not as a multiplier** (`VolSurface.iv`). A
multiplicative bump inflates a 6-month option as much as a 2-week one, which is
not how an event prices. Adding a fixed lump of variance makes it dilute with
tenor, which is also what makes OP-04's front-vs-back extraction recoverable.

**OP-04 compares the implied EVENT move, not raw IV vs realized vol.** The naive
comparison dumps the whole vol risk premium into the "event" term and reports a
35% implied move for a stock priced for 8%. It rejects everything, always. The
two-expiry solve requires `T1 >= 0.12` years so the front leg clears the
term-structure kink.

**Reg T 2x in 2000, not 4x.** The 4x day-trade buying power and the $25k PDT
minimum arrived together, effective 2001-09-28. A 2000 backtest granting 4x is
trading on rules that did not exist. `era.py` handles this by date.

**`config/cik_overrides.yml` is deliberately empty.** A wrong CIK does not
error — it silently attaches another company's filing dates to your symbol.
Look them up from the source; never fill them in from memory.

---

## Traps already hit — do not re-introduce

**Vendor gap-fill that looks like data.** A connected broker API returned 780
fully-populated 5-minute bars for January 2000: all flagged `interpolated`, zero
volume, one single price (484.52) because it has no history before ~2013.
Ingested unchecked that is a backtest on a *constant price* with a clean equity
curve and no meaning. `engine/provenance.py` catches it on five independent
signals; `test_rejects_the_real_broker_gapfill` is the regression test.

**`minute_index == N` silently never matches.** With `--minute-step 15` the loop
hits 1, 16, 31… so an equality check on minute 30 never fires and the strategy
is silently disabled. Use `ctx.fire_once(key, at_minute)`.

**`m % cadence == 0` has the same failure.** Step 15 against cadence 30 never
matches a multiple of 30. The scheduler now uses elapsed-minutes-since-last-call.

**One strategy eating the daily trade budget.** EQ-03 consumed the whole
account's allowance and starved the other nine. Hence
`max_new_trades_per_strategy_per_day`.

**Opening-range thresholds calibrated wrong.** An opening 30-minute range is
roughly a third to a half of a daily ATR, not 80%+. The original floor made the
setup unreachable.

---

## Commands

```bash
python3 scripts/preflight.py                    # can this machine reach the data?
python3 -m pytest tests/ -q                     # 49 tests

# Offline smoke test (synthetic data — proves plumbing, proves nothing else)
python3 scripts/make_demo_data.py
python3 scripts/run_backtest.py --cache-dir data/demo_cache --minute-step 15

# Real data
export EDGAR_USER_AGENT="Your Name you@example.com"
python3 scripts/fetch_data.py --start 1999-01-01 --end 2001-01-31
python3 scripts/fetch_earnings_edgar.py --start 1999-01-01 --end 2001-06-30
python3 scripts/run_backtest.py --start 2000-01-01 --end 2000-12-31

python3 scripts/run_backtest.py --path-seeds 5  # how much is path luck?
python3 scripts/walk_forward.py --start 1999 --end 2020 --is-years 5
```

---

## Conventions

- Strategies see the world only through `ctx` and act only through `ctx.broker`.
  Never let a strategy reach `feed._daily` or `today_daily_raw` directly.
- New strategies: one file, thesis and rules in the module docstring, subclass
  `Strategy`, use `ctx.fire_once` for once-a-day timing.
- Period rules belong in `era.py` as functions of date — never hard-code a
  year-2000 constant into a strategy.
- Report what actually happened. If a strategy did not trade, say so and find
  out why rather than loosening a gate until it fires.

---

## Good next steps

1. **Run it on real data.** Nothing here is validated until that happens.
2. Fill in `config/cik_overrides.yml` for the delisted names, then re-run the
   EDGAR earnings fetch so OP-04 has verified dates.
3. Re-tune the thresholds that were set against synthetic data — EQ-01's range
   and volume filters, EQ-03's z-score bands, OP-01's extension threshold.
4. Decide on a real data budget (`docs/DATA_SOURCES.md`). Without recorded
   intraday, treat every intraday result as provisional and always check
   `--path-seeds`.
