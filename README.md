# Historical-Investing — a point-in-time trading simulator for the year 2000

Ten strategies (five equity day-trading, five options), $25,000 of starting
capital, simulated across 2000-01-01 → 2000-12-31 under a hard rule: **on any
simulated date, the engine can only see data that had already printed.**

That rule is not a convention here, it is enforced in code. Every read of
market data passes through one gate (`PointInTimeFeed._visible_slice`) that
clamps to the simulation clock, and the clock refuses to move backwards. A
strategy cannot reach tomorrow's close even by accident, and the test suite
proves it.

---

## Read this first: what this can and cannot tell you

You asked for a minute-by-minute 2000 simulation using yfinance. Three parts of
that are not possible as stated, and pretending otherwise would produce
confident numbers built on nothing. Here is exactly where the ground is solid
and where it is not.

### 1. Minute data for 2000 is real, but it is not free

yfinance serves intraday bars for the **last 30 days** at 1-minute resolution
and the **last 60 days** at 2–90 minute. There is no path to a January 2000
minute bar through it.

Recorded 2000 intraday data **does exist** — NYSE TAQ reaches back to 1993,
Refinitiv Tick History to ~1996, and QuantQuote and Tick Data LLC sell
1-minute equities from 1998. All are paid. `docs/DATA_SOURCES.md` lists what
each actually carries and what it costs you in coverage.

The engine takes them directly:

```bash
python3 scripts/fetch_intraday.py --format quantquote --src ~/qq/ --symbols MSFT INTC
python3 scripts/run_backtest.py --interval-minutes 2 --require-real-intraday
```

Pull **1-minute once**; `--interval-minutes` resamples at run time, so the
bi-minute simulation and the 1, 5 and 15-minute versions all come off the same
download. `--require-real-intraday` makes the run refuse to reconstruct, so no
result silently mixes recorded and modelled bars.

**Without recorded bars, the engine reconstructs** each session from the true
daily print: real open, real high, real low, real close, real volume on the
U-shaped curve US equities actually traded, seeded Brownian bridge between.

| | Status |
|---|---|
| Session open / high / low / close / volume | **Real** |
| Any daily-resolution signal | **Real** |
| Which of the high or low printed first | **Modelled** |
| The price at 10:47 specifically | **Modelled** |
| Whether a stop inside the day's range hit before the target | **Modelled** |

Every run prints which it used. `--path-seeds 5` measures the cost: on one test
window, three synthetic paths over identical daily data returned −2.9%, −3.1%
and **+3.5%**. That 6.6% spread *is* the error bar. **A strategy whose result
swings across seeds was reading path noise, not signal.**

### 2. A silent-failure trap worth knowing about

A trading API connected to this session was asked for 5-minute MSFT bars
covering January 2000. It returned **780 bars**, every field populated, no
error.

```
interpolated: 780 of 780 (100%)
bars with volume > 0: 0
distinct close prices across all 780 bars: 1   ->  "484.520000"
```

All gap-fill. The provider has no history before ~2013 and silently
interpolates a flat line across anything earlier. Ingested unchecked, that is a
backtest on a **constant price**: no volatility, every mean-reversion and
breakout signal dead, and a clean equity curve meaning nothing.

So nothing enters the engine unvalidated. `engine/provenance.py` catches that
exact series on five independent signals — the vendor flag, zero volume, one
distinct close, zero-range bars, and a 779-bar unchanged run — and a rejected
series falls through to reconstruction rather than poisoning the run. It is a
regression test now (`test_rejects_the_real_broker_gapfill`).

### 3. Historical option quotes for 2000 do not exist in yfinance

Only current chains. Every option price here is a **model price**: BSM on the
real point-in-time underlying with a vol surface carrying put skew, term
structure, a stress term, and an earnings term added as *variance* so it
dilutes with tenor the way a real term structure does.

Model prices are smoother and fairer than the real 2000 tape, where a wide
market and a slow fill often *were* the trade. The cost model overstates
spreads to lean against that; it cannot fully compensate.

### 4. Fundamentals: supported, and gated on the filing date

The KPIs are wired in — EPS, P/E, PEG, ROE, debt/equity, FCF, margins, revenue
growth, EBITDA, price/book, dividend yield and payout, market cap. **The
requirement is not "fundamental data", it is a filing-date column.**

Every record carries two dates: `period_end` (the fiscal period) and
`available_on` (**the date it became public**). Lookups key on the second. Ask
for a company's ROE on 2000-02-01 and you get the last filing published *by*
2000-02-01 — never the 10-K that landed in March, however much it describes
December. That gap ran up to 90 days in 2000; keying on `period_end` would hand
every strategy three months of foresight on every name, every quarter.

```bash
python3 scripts/fetch_fundamentals.py --format sharadar --src sf1.csv --dimension ARQ
python3 scripts/run_backtest.py --fundamentals data/fundamentals/fundamentals.csv
```

Sharadar SF1 (`datekey` is the filing date, covers ~1998+ including delisted
names) is the practical choice; Compustat Point-in-Time is the academic
standard. **yfinance `.info` is not usable** — it returns today's ratios. The
loader **refuses to write** an export with no filing-date column, and warns
when the median reporting lag is implausibly short.

OP-03 and EQ-04 screen on these. One design rule matters:
**unknown is not fail.** A missing filing passes the screen, because counting
it as a failure would turn the screen into a filter on *data coverage* — worst
for exactly the small, distressed and delisted names — and reintroduce
survivorship bias wearing a fundamental screen as a disguise.

### 5. Verified earnings dates, from the primary record

`config/earnings_2000.yml` held approximate dates. The replacement pulls them
from **SEC EDGAR 8-K filings**, where the acceptance timestamp is the moment the
announcement became public — free and authoritative back to 1993.

```bash
export EDGAR_USER_AGENT="Your Name your@email.com"    # EDGAR requires a contact
python3 scripts/fetch_earnings_edgar.py --start 1999-01-01 --end 2001-06-30
```

Two honest caveats, both handled rather than hidden:

1. **Item 2.02 did not exist before 2004-08-23.** Earnings 8-Ks in 2000 were
   filed under Item 5 or Item 12 and cannot be identified by item code with
   confidence. Pre-2004 filings are written with `confidence: medium` plus the
   accession number, so every date traces back to its filing.
2. **Delisted companies are absent from EDGAR's ticker map** — it lists only
   active registrants, so WorldCom, CMGI, Ariba, JDS Uniphase and the rest are
   missing. Their filings are still there; look up each CIK by company name and
   add it to `config/cik_overrides.yml`. That file is deliberately **empty**
   rather than pre-filled from memory: a wrong CIK does not error, it silently
   attaches another company's filing dates to your symbol.

### 6. Survivorship bias, unusually severe for this year

yfinance only resolves tickers that still exist, and for 2000 the interesting
names are precisely the ones that died. `config/universe_2000.yml` lists them
deliberately; the fetcher reports every name it cannot retrieve, and **that
report is the bias measurement for your run.**

- for **long** strategies, missing failures bias results **upward**
- for **short** strategies, the missing names are the best shorts of the year,
  so results are biased **downward**

QuantQuote's survivorship-bias-free constituent lists are the direct fix.

### 7. Testing through time, not just through 2000

Period rules are now functions of the simulated date (`engine/era.py`), so the
same strategy faces the rules that actually applied:

| | 2000 | 2008 | 2021 |
|---|---|---|---|
| Tick size | $0.0625 | $0.01 | $0.01 |
| Uptick rule | **on** | off | off (Rule 201 only) |
| Day-trade buying power | **2x** | 4x | 4x |
| PDT rule | **none** | yes | yes |
| Commission | $12/trade | $9.99 | **$0** |
| Weekly options | **none** | yes | yes |

```bash
python3 scripts/walk_forward.py --start 1999 --end 2020 --is-years 5
```

Reports each year separately, splits in-sample from out-of-sample, and judges
every year against **that year's T-bill** — because cash paid 5.8% in 2000 and
0.05% in 2015, and a raw return hides that completely. In testing it flagged a
+2.81% year as a *loss* against 4.64% T-bills, which is the correct reading.

### 8. This session still could not fetch real data

Every external data host is blocked by this environment's egress policy —
Yahoo, SEC EDGAR, stooq, Polygon, Alpha Vantage, Tiingo and FMP all return 403
at the proxy, while GitHub returns 200. So the engine has **never run against
real 2000 prices**, only against the clearly-labelled synthetic data in
`scripts/make_demo_data.py`.

Run the fetch scripts anywhere with normal network access and every result
becomes real. Until then, treat any P&L you see as a self-test.

---

## Quick start

```bash
pip install -r requirements.txt

# 1. Daily bars. Starts in 1999 so a 250-day lookback exists on 2000-01-03.
python3 scripts/fetch_data.py --start 1999-01-01 --end 2001-01-31

# 2. Verified earnings dates from SEC EDGAR (free, authoritative).
export EDGAR_USER_AGENT="Your Name your@email.com"
python3 scripts/fetch_earnings_edgar.py --start 1999-01-01 --end 2001-06-30

# 3. Optional: recorded intraday, and point-in-time fundamentals.
python3 scripts/fetch_intraday.py --format quantquote --src ~/qq/
python3 scripts/fetch_fundamentals.py --format sharadar --src sf1.csv --dimension ARQ

# 4. Run the year, on 2-minute bars.
python3 scripts/run_backtest.py --start 2000-01-01 --end 2000-12-31 \
    --interval-minutes 2 --fundamentals data/fundamentals/fundamentals.csv

# How much of the result is intraday-path luck?
python3 scripts/run_backtest.py --path-seeds 5

# Does it hold up across regimes?
python3 scripts/walk_forward.py --start 1999 --end 2020 --is-years 5
```

No network? Exercise the engine on synthetic data:

```bash
python3 scripts/make_demo_data.py
python3 scripts/run_backtest.py --cache-dir data/demo_cache --minute-step 15
```

Outputs land in `reports/`: `summary.json`, `daily.csv`, `trades.csv`,
`by_strategy.csv`, `monthly.csv`.

---

## The ten strategies

Each lives in one file, with its thesis, its rules and its known weaknesses
written at the top. The 2000-specific reasoning is the point — these are not
generic strategies with the dates changed.

### Equity day trading — $15,000 sleeve

| | Strategy | Core idea |
|---|---|---|
| EQ-01 | Opening Range Breakout | The 09:30–10:00 range carried an enormous share of 2000's daily information. Break it on expanding volume, flat by the bell. |
| EQ-02 | Gap Continuation / Gap Fade | Two trades in one: moderate gaps with volume behind them continue; extreme gaps without it decay back toward the prior close. |
| EQ-03 | VWAP Mean Reversion | Institutions were benchmarked to VWAP and mechanically leaned against extensions. The counterweight that earns on the chop that stops out the breakout book. |
| EQ-04 | Momentum Rotation | Buy relative strength, but **only** while the index is above its 10- and 30-day averages. The regime filter *is* the strategy — unfiltered momentum was the best trade on earth until 2000-03-10 and one of the worst for the nine months after. |
| EQ-05 | Downtrend Bounce Short | The strategy built for what 2000 actually was. Sell the violent bounces into a declining VWAP rather than shorting new lows, which the uptick rule made nearly unexecutable. |

### Options — $10,000 sleeve

| | Strategy | Core idea |
|---|---|---|
| OP-01 | Long puts on extended high-beta | Convexity on a stretched Nasdaq name in a turning market. Sidesteps the uptick rule, caps the loss at the premium, survives the squeezes that destroyed outright shorts. |
| OP-02 | Bear put debit spreads (QQQ) | Long puts are right about direction and wrong about cost. Selling the deeper strike halves the bill and caps at a level the index genuinely reached repeatedly. |
| OP-03 | Cash-secured puts / the wheel | The forgotten half of 2000: money leaving tech went into old-economy value, whose options carried an index-wide vol bid unrelated to their own realized vol. The one short-vol trade that belonged in a 2000 book. |
| OP-04 | Earnings strangles on cheap vol | Buy the event only when the market's implied one-day move is below what the stock has *historically actually done*. Gated hard — it passes about a quarter of the events it looks at. |
| OP-05 | Index tail hedge | Defined-risk put spread on SPY, sized to real overnight exposure. Expected to lose money most months; judged on the drawdown it prevents. |

**OP-05 was going to be a collar, and that was a bug.** A collar sells a call
against stock you own. This account owns no index, so the short call would have
been *naked* — undefined loss on a $25,000 account. Worse, it backtests
*beautifully* through most of 2000 because the market fell, then hands back
years of premium in one squeeze (the Nasdaq rose over 14% in four sessions from
2000-05-24). It is now a put spread. The reasoning is preserved in the file
because the trap is more instructive than the fix.

---

## What the engine models that most 2000 backtests skip

Each of these changes results materially. Leaving them out is the usual way a
backtest of this era invents profit that never existed.

- **Sixteenths.** US equities traded in $1/16 = $0.0625 increments for all of
  2000; decimalization was an NYSE pilot from 2000-08-28 and did not finish
  until 2001. A minimum tick six times today's penny puts a hard floor under
  the spread — exactly the cost a scalping strategy lives or dies on.
- **The uptick rule.** SEC Rule 10a-1 (in force until 2007) barred shorting on
  a downtick. The broker *rejects* short entries that would have violated it,
  and the reject count is in every report. It is frequently binding.
- **No Pattern Day Trader rule — and no 4x buying power.** The $25,000 PDT
  minimum was approved in 2001 and effective 2001-09-28. In 2000 a $25,000
  account faced no day-trade limit, but also got none of the 4x day-trading
  buying power that arrived with the same rule set. **Plain Reg T 2x is
  enforced.** A 2000 backtest granting 4x is trading on rules that did not exist.
- **No weekly options.** Weeklys launched in 2005. Only the monthly third-Friday
  cycle existed, which is why OP-04 must buy ~50 days of time value to trade a
  one-day event.
- **2000-era commissions.** $10 a stock trade; $15 + $1.75/contract on options.
  A two-contract options round trip costs ~$37 — nearly 4% friction on a $1,000
  position. This is why the options sleeve holds few, larger positions.
- **High rates.** Fed funds went 5.50% → 6.50% across 2000. Carry is not
  negligible, unlike in a zero-rate era, and the Sharpe ratio is computed
  against the ~5.95% T-bill — **in a year when cash paid nearly 6%, a strategy
  has to clear a real hurdle before it has earned anything.**
- **Short borrow, margin interest, Section 31 fees, early assignment** on short
  American options, and physical settlement into shares at expiry.

---

## How the no-lookahead guarantee works

```
SimClock.now ──► PointInTimeFeed._visible_slice ──► every strategy read
     │                        │
     │                        └─ during a session, TODAY's daily bar does not
     │                           exist yet — only yesterday's and older
     └─ moves forward only; advancing backwards raises LookaheadError
```

A daily bar for date D becomes visible at D's closing bell, not before. This
kills the single most common backtest bug — using today's close to make a
decision at 11:00 this morning.

Minute bars follow the same rule: the bar stamped 09:31 covers 09:30:00–
09:30:59 and is visible at 09:31:00.

`tests/test_no_lookahead.py` walks a session minute by minute and asserts the
invariant at every step. It also verifies that already-printed bars are never
revised, and that a 20-day SMA computed at noon matches one computed only from
bars that closed yesterday. **If you change the data layer, run these first.**

```bash
python3 -m pytest tests/ -q      # 49 tests
```

The guarantee is real enough that it caught a diagnostic script of mine trying
to step the clock backwards during development.

---

## Layout

```
engine/
  clock.py           SimClock, the 2000 holiday and half-day calendar
  data.py            PointInTimeFeed — the one visibility gate
  intraday.py        minute-path reconstruction from daily OHLCV
  options.py         BSM, greeks, the vol surface, 2000 rate curve, expiries
  microstructure.py  sixteenths, uptick rule, spreads, commissions, fees
  portfolio.py       cash, positions, Reg T margin, P&L, assignment
  broker.py          order types and pessimistic fills
  risk.py            sizing, loss limits, the drawdown kill switch
  backtest.py        the session loop
  metrics.py         Sharpe vs T-bill, Sortino, drawdown, per-strategy attribution
  era.py             date-aware rules: ticks, shorting law, leverage, commissions
  provenance.py      bar-source tagging and vendor gap-fill detection
  intraday_source.py recorded bars if present, reconstruction if not
  fundamentals.py    point-in-time KPIs keyed on filing date
strategies/          the ten strategies, one file each, plus filters.py
scripts/             fetch_data · fetch_intraday · fetch_fundamentals ·
                     fetch_earnings_edgar · run_backtest · walk_forward ·
                     make_demo_data
config/              universe · earnings · CIK overrides · sleeve and risk
docs/DATA_SOURCES.md what actually carries 2000 data, and what was tested
tests/               no-lookahead proofs, engine mechanics, data integrity
```

## Risk controls

1% of net liq risked per equity trade, 2% premium per option trade, 10% total
open premium, 3% daily loss limit, 6% weekly, and a **20% program drawdown kill
switch** that liquidates and halts. Three new trades per strategy per day, so
one signal-happy strategy cannot eat the whole account's daily budget — a
failure mode the engine actually exhibited during development.

## Known limitations

1. Without a paid intraday feed, intraday paths are **modelled** (§1). Always
   check `--path-seeds`; use `--require-real-intraday` once you have data.
2. Option prices are modelled, not recorded (§3). No 2000 chains exist free.
3. Fundamentals need a source with a **filing-date column** (§4). The engine
   supports them; it cannot conjure them.
4. Survivorship bias, unusually severe for this year (§6).
5. Pre-2004 earnings 8-Ks cannot be identified by item code, so EDGAR dates for
   2000 carry `confidence: medium` and should be spot-checked against the
   filing text. `config/cik_overrides.yml` must be filled in by hand for
   delisted names — deliberately, since a wrong CIK fails silently.
6. `VolSurface.event_move_multiple` is the single most important free parameter
   in the options sleeve — it alone decides whether OP-04 ever sees cheap vol.
   Set to a defensible value, not a tuned one.
7. No borrow *availability* modelling: hard-to-borrow names are charged a 25%
   rate, but some genuinely could not be shorted at all in 2000.
8. Fills assume your order does not move the market beyond the square-root
   impact term. For a $25,000 account in liquid names that is reasonable.
9. **2-minute bars in 2000 are noisier than they look.** Sixteenth ticks meant a
   2-minute bar on a mid-cap could be one or two ticks wide, Nasdaq was a dealer
   market where the inside quote and the last sale genuinely diverged, and the
   tape ran slower. An edge living entirely inside a 2-minute bar is probably
   microstructure noise. See `docs/DATA_SOURCES.md`.
