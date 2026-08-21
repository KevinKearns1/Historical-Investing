# Running this on your own machine

The engine is designed so that **only the fetch scripts touch the network**.
Everything else runs off `data/cache/`, which means once you have pulled the
data, backtests are fully reproducible and offline.

## Why local, and not the cloud session

The cloud session this was built in sits behind an egress allowlist that denies
every financial data host at the **proxy**, before any connection to the
provider is attempted. Your own machine has no such policy, so everything just
works. Nothing about the code needs to change.

Run `python3 scripts/preflight.py` on any machine to see which situation you
are in — it distinguishes a proxy denial from a genuine provider block, which
look identical in a log and have opposite fixes.

## Setup

```bash
git clone https://github.com/KevinKearns1/Historical-Investing.git
cd Historical-Investing
git checkout claude/day-trading-backtester-2000-dc7lqg

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python3 scripts/preflight.py       # expect "ALL CLEAR"
python3 -m pytest tests/ -q        # expect 49 passed
```

## Pull the data

```bash
# EDGAR requires a descriptive User-Agent with a REAL contact address,
# or it returns 403. This is a genuine provider rule, not a workaround.
export EDGAR_USER_AGENT="Kevin Kearns aspiringactuary111@gmail.com"

# Daily bars. 1999 start so a 250-day lookback exists on 2000-01-03.
python3 scripts/fetch_data.py --start 1999-01-01 --end 2001-01-31

# Verified earnings dates from SEC 8-K filings.
python3 scripts/fetch_earnings_edgar.py --start 1999-01-01 --end 2001-06-30
```

`fetch_data.py` prints every symbol it could not retrieve. **Do not skip that
list** — it is the survivorship-bias measurement for your run. The names that
fail are the companies that went bankrupt or were acquired, and their absence
biases long strategies up and short strategies down.

`fetch_earnings_edgar.py` prints symbols whose CIK it could not resolve.
EDGAR's ticker map only lists *active* registrants, so delisted names need a
CIK added by hand to `config/cik_overrides.yml`. Look each one up by company
name at <https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany>.

## Run it

```bash
# The year, minute by minute.
python3 scripts/run_backtest.py --start 2000-01-01 --end 2000-12-31

# Faster sweep while iterating.
python3 scripts/run_backtest.py --minute-step 15

# How much of the result is intraday-path luck rather than signal?
python3 scripts/run_backtest.py --path-seeds 5

# Does it survive across regimes?
python3 scripts/walk_forward.py --start 1999 --end 2020 --is-years 5
```

## Optional paid data

Neither is required; both make the results materially more trustworthy.

**Recorded intraday** (for a genuine bi-minute simulation). See
`docs/DATA_SOURCES.md`. Pull 1-minute once — `--interval-minutes` resamples at
run time, so one download serves the 1, 2, 5 and 15-minute runs.

```bash
python3 scripts/fetch_intraday.py --format quantquote --src ~/qq/
python3 scripts/run_backtest.py --interval-minutes 2 --require-real-intraday
```

**Point-in-time fundamentals** (Sharadar SF1, dimension `ARQ`):

```bash
python3 scripts/fetch_fundamentals.py --format sharadar --src sf1.csv --dimension ARQ
python3 scripts/run_backtest.py --fundamentals data/fundamentals/fundamentals.csv
```

## Working with Claude Code locally

Install Claude Code, then from the repo directory:

```bash
npm install -g @anthropic-ai/claude-code
cd Historical-Investing
claude
```

It has the same filesystem and network access you do, so it can run the
fetchers, inspect the results, and iterate on the strategies against real data.
Commit and push as normal — this repo and branch are already set up.

## Rough resource expectations

| | Size / time |
|---|---|
| Daily bars, 35 symbols, 1999–2001 | a few MB, under a minute |
| EDGAR earnings, 30 symbols | a few MB, several minutes (rate-limited to 10 req/s) |
| Full year, `--minute-step 1` | tens of minutes, CPU-bound |
| Full year, `--minute-step 15` | a few minutes |
| Vendor 1-minute bars, 1 year, 30 symbols | 5–20 GB depending on vendor |

## Troubleshooting

**`No cached data in data/cache`** — run `fetch_data.py`, or
`make_demo_data.py` for a synthetic offline smoke test. The demo data is
invented and produces numbers that mean nothing about 2000; it exists to prove
the plumbing works.

**EDGAR 403** — set `EDGAR_USER_AGENT` to a real name and email. This is
EDGAR's documented requirement.

**yfinance returns nothing for a symbol** — usually a delisted company. Expected
for the 2000 universe, and reported rather than hidden.

**A backtest runs but nothing trades** — check the `order rejects` line in the
report. Frequent `uptick_rule` rejects are correct behaviour for 2000, not a
bug.
