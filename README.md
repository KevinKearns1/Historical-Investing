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

### 1. Minute data for 2000 does not exist in yfinance

yfinance serves intraday bars for the **last 30 days** at 1-minute resolution
and the **last 60 days** at 2–90 minute resolution. There is no path to a
minute bar from January 2000. None.

What *is* available for 2000 is the **daily** OHLCV print, and that is real.

So the engine reconstructs each session's minute path from the true daily bar:
it opens at the real open, touches the real high and the real low, closes at
the real close, distributes the real volume across the U-shaped curve US
equities actually traded, and fills the gaps with a seeded Brownian bridge.

| | Status |
|---|---|
| Session open / high / low / close / volume | **Real** |
| Any daily-resolution signal | **Real** |
| Which of the high or low printed first | **Modelled** |
| The price at 10:47 specifically | **Modelled** |
| Whether a stop inside the day's range was hit before the target | **Modelled** |

The last row is the one that costs you. Every intraday strategy here carries
genuine model error. The engine measures it rather than hiding it: run with
`--path-seeds 5` and the report prints the spread of outcomes across different
synthetic paths. **A strategy whose result swings wildly across seeds was
reading path noise, not signal, and you should not trust its number.**

### 2. Historical option quotes for 2000 do not exist in yfinance either

yfinance exposes only the *current* option chain. There is no 2000 chain at any
date. Every option price here is therefore a **model price**: Black-Scholes-
Merton on the real, point-in-time underlying, with an implied-vol surface built
from point-in-time realized vol plus a put skew, a term structure, a stress
term and an earnings-event term.

This cuts one way, and it is not in your favour when it comes to realism: model
prices are smoother and fairer than the real 2000 tape, where a wide market and
a slow fill often *were* the whole trade. The cost model deliberately
overstates spreads to lean against that, but it cannot fully compensate.

### 3. Point-in-time fundamentals are not available

You listed EPS, PEG, ROE, debt/equity, FCF, margins, revenue growth, EBITDA,
book value, short interest and so on. yfinance's `.info` returns **today's**
values, and its financial statements reach back roughly four to five years.
Using either in a 2000 simulation is lookahead bias in its purest form — you
would be trading 2000 on numbers published in 2026.

So the ten strategies are built on what genuinely *is* reconstructible
point-in-time from the daily record: price, volume, volatility, trend,
relative strength, and the options surface derived from them. The one place
fundamentals-adjacent data enters is OP-04's earnings **dates**, which live in
a static config file precisely so they cannot be back-filled from the future.

If you want true point-in-time fundamentals, they exist — Compustat Point-in-
Time, S&P Capital IQ, Sharadar SF1 — but they are paid, and none of them is
yfinance. The engine takes them as a CSV plug-in if you get them.

### 4. Survivorship bias, and why it is worse than usual for this year

yfinance only resolves tickers that still exist. For 2000 that is a serious
problem, because the interesting names are precisely the ones that died:
WorldCom, CMGI, Ariba, JDS Uniphase, Lucent, Nortel, Sun, Yahoo!.

`config/universe_2000.yml` lists them deliberately. The fetcher reports every
name it cannot retrieve, and **that report is the bias measurement for your
run.** Note which way it cuts:

- for the **long** strategies, missing failures bias results **upward**
- for the **short** strategies, the missing names are the best shorts of the
  year, so results are biased **downward**

Neither bias is small in 2000.

### 5. This session could not run it on real data

The environment this repository was built in blocks Yahoo Finance at the
network egress policy (`fc.yahoo.com`, `query1/query2.finance.yahoo.com` all
return 403 at the proxy; stooq and Alpha Vantage are blocked too). So the
engine has **never been run against real 2000 prices** — only against the
clearly-labelled synthetic data in `scripts/make_demo_data.py`, which exists to
exercise the plumbing and produces numbers that mean nothing about 2000.

Run `scripts/fetch_data.py` somewhere with network access and every result in
this README becomes real. Until then, treat any P&L you see as a self-test.

---

## Quick start

```bash
pip install -r requirements.txt

# 1. Fetch real daily bars (needs network; the only step that does).
#    Starts in 1999 so a 250-day lookback exists on 2000-01-03.
python3 scripts/fetch_data.py --start 1999-01-01 --end 2001-01-31

# 2. Run the year.
python3 scripts/run_backtest.py --start 2000-01-01 --end 2000-12-31

# Faster sweep (every 15 minutes instead of every minute):
python3 scripts/run_backtest.py --minute-step 15

# Measure how much of the result is intraday-path luck:
python3 scripts/run_backtest.py --path-seeds 5
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
python3 -m pytest tests/ -q      # 26 tests
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
strategies/          the ten strategies, one file each
scripts/             fetch_data.py · run_backtest.py · make_demo_data.py
config/              universe · earnings dates · sleeve and risk settings
tests/               no-lookahead proofs and engine mechanics
```

## Risk controls

1% of net liq risked per equity trade, 2% premium per option trade, 10% total
open premium, 3% daily loss limit, 6% weekly, and a **20% program drawdown kill
switch** that liquidates and halts. Three new trades per strategy per day, so
one signal-happy strategy cannot eat the whole account's daily budget — a
failure mode the engine actually exhibited during development.

## Known limitations

1. Intraday paths are modelled, not recorded (§1). Always check `--path-seeds`.
2. Option prices are modelled, not recorded (§2).
3. No point-in-time fundamentals (§3).
4. Survivorship bias, unusually severe for this year (§4).
5. Earnings dates in `config/earnings_2000.yml` are **approximate** and flagged
   `verified: false`. Verify against period sources before trusting OP-04.
6. `VolSurface.event_move_multiple` is the single most important free parameter
   in the options sleeve — it alone decides whether OP-04 ever sees cheap vol.
   It is set to a defensible value, not a tuned one.
7. No borrow *availability* modelling: hard-to-borrow names are charged a 25%
   rate, but in reality some simply could not be shorted at all in 2000.
8. Fills assume your order does not move the market beyond the square-root
   impact term. For a $25,000 account in liquid names that is reasonable.
