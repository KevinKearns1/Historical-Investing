# Where 2000 market data actually lives

Tested from this session on 2026-08-19. Every external data host is blocked by
this environment's egress policy, so the reachability column records the policy
result, not the provider's own availability. The **coverage** column is the part
that matters, and it does not change with the network.

## What was tested from here

| Host | Result | Note |
|---|---|---|
| `query1/query2.finance.yahoo.com` | 403 at proxy | egress policy denial |
| `fc.yahoo.com` | 403 at proxy | yfinance's cookie/crumb bootstrap |
| `www.sec.gov`, `data.sec.gov` | 403 at proxy | EDGAR |
| `stooq.com` | 403 at proxy | |
| `api.polygon.io` | 403 at proxy | |
| `www.alphavantage.co` | 403 at proxy | |
| `api.tiingo.com` | 403 at proxy | |
| `financialmodelingprep.com` | 403 at proxy | |
| `raw.githubusercontent.com` | 200 | so egress works; data hosts specifically are denied |

Run the fetch scripts anywhere with normal network access and they work.

## The connected broker API: a warning worth reading

The trading API connected to this session was asked for **5-minute MSFT bars
covering January 2000**. It returned **780 bars**. Every field populated. No
error.

```
total bars: 780
interpolated: 780 (100.0%)
bars with volume > 0: 0
distinct close prices across all 780 bars: 1   -> "484.520000"
```

All 780 were vendor gap-fill — a flat line, because the provider has no history
before roughly 2013 and silently interpolates across anything earlier. Its own
`get_earnings_results` likewise returns only a trailing 8 quarters (2025–2027
when queried), so it is no help for historical earnings dates either.

Ingested without checking, this produces a backtest on a **constant price**:
zero volatility, every mean-reversion and breakout signal dead, and a clean
equity curve that means nothing. The failure is silent and total.

This is why `engine/provenance.py` exists and why every ingest path validates
before use. The validator catches this exact series on five independent signals.

## Intraday (1-minute or finer) for the year 2000

**There is no free source.** The consolidated tape for that era is a commercial
product. Verify current pricing and coverage directly — these change.

| Source | 2000 intraday? | Notes |
|---|---|---|
| **NYSE TAQ** (via WRDS) | **Yes — from 1993** | The authoritative consolidated tape: trades *and* quotes. What academic microstructure work uses. Institutional/academic subscription. |
| **Refinitiv Tick History (TRTH)** | **Yes — from ~1996** | Global, tick level. Enterprise pricing. |
| **Tick Data LLC** | **Yes** | 1-minute US equities into the 1990s. Commercial, per-symbol pricing. |
| **QuantQuote** | **Yes — from 1998** | 1-minute, and notably offers *survivorship-bias-free* S&P 500 historical constituents, which matters enormously for 2000. |
| **Kibot** | **Partly — from ~1998** | 1-minute for a subset of symbols. Cheapest of the real options; coverage is uneven, so check your specific names. |
| AlgoSeek | No | starts ~2007 |
| Polygon.io | No | stock aggregates start 2003 |
| Nasdaq TotalView-ITCH | No | historical archives start ~2007 |
| IQFeed / DTN | No | rolling ~8 years of intraday |
| Bloomberg | No | intraday tick capped around 140 days |

**Recommendation for your use case:** QuantQuote or Tick Data for 1-minute
equities, because both reach 1998 and QuantQuote's survivorship-free constituent
list directly addresses the bias that hurts a 2000 study most. Pull **1-minute**
and let the engine resample — `--interval-minutes 2` gives you the bi-minute
simulation off the same download, and you can re-run at 1, 5 or 15 without
re-fetching.

Load an export with:

```bash
python3 scripts/fetch_intraday.py --format quantquote --src ~/qq/ --symbols MSFT INTC CSCO
python3 scripts/run_backtest.py --interval-minutes 2 --require-real-intraday
```

`--require-real-intraday` makes the run **refuse to reconstruct**: any symbol
without validated recorded bars is skipped rather than modelled. Use it once you
have real data, so no result silently mixes the two.

### One caveat about 2-minute bars in 2000

Bi-minute resolution is period-appropriate but noisier than it looks:

- **Sixteenth ticks.** A 2-minute bar on a mid-cap could be one or two ticks
  wide. Bid-ask bounce dominates the signal at that scale in a way it does not
  post-decimalization.
- **Nasdaq was a dealer market.** SOES and SelectNet, not a central limit order
  book. "The price" at a given minute is genuinely ambiguous between the inside
  quote and the last sale, and they diverged. TAQ gives you both; most 1-minute
  bar products give you last-sale only.
- **The tape was slower.** Late prints and out-of-sequence trades are more
  common in 2000 than a modern feed would lead you to expect.

None of this makes the exercise invalid. It does mean a strategy whose edge
lives entirely inside a 2-minute bar is probably reading microstructure noise,
and the `--path-seeds` sensitivity check is the right way to find out.

## Earnings dates

**Use SEC EDGAR.** When a company announced results it filed an 8-K, and EDGAR
stamps the exact acceptance date — which is the moment the information became
public, which is exactly what a point-in-time simulation needs. Free,
authoritative, back to 1993–1996.

```bash
export EDGAR_USER_AGENT="Your Name your@email.com"   # EDGAR requires a contact
python3 scripts/fetch_earnings_edgar.py --start 1999-01-01 --end 2001-06-30
```

Two things to know:

1. **Item 2.02 did not exist before 2004-08-23.** Earnings 8-Ks from 2000 were
   filed under Item 5 or Item 12, so they cannot be identified by item code with
   confidence. The fetcher marks pre-2004 filings `confidence: medium` and
   writes the accession number for every one, so you can trace any date back to
   the filing and spot-check it.
2. **Delisted companies are not in EDGAR's ticker map.** It only lists active
   registrants, so WorldCom, CMGI, Ariba, JDS Uniphase and the rest are absent —
   the interesting half of a 2000 universe. Their *filings* are still on EDGAR;
   look up each CIK by company name and add it to `config/cik_overrides.yml`.
   That file is deliberately empty rather than pre-filled, because a wrong CIK
   does not error — it silently attaches another company's filing dates to your
   symbol.

## Fundamentals (EPS, PE, PEG, ROE, D/E, FCF, margins, EBITDA, P/B…)

The requirement is not "fundamental data", it is **a filing-date column**. A
dataset stamped only with fiscal period end is not point-in-time: Q4 1999
numbers used on 1999-12-31 hand a strategy up to three months of foresight,
every name, every quarter.

| Source | Point-in-time? | 2000 coverage |
|---|---|---|
| **Sharadar SF1** (Nasdaq Data Link) | **Yes** — `datekey` is the filing date | ~1998+, includes delisted tickers. Affordable; the practical choice. |
| **Compustat Point-in-Time** (WRDS) | **Yes** — `PITDATE` | The academic standard. Institutional. |
| **Refinitiv / S&P Capital IQ PIT** | Yes | Enterprise. |
| SEC EDGAR XBRL `companyfacts` | Filing dates yes, data no | **XBRL only starts ~2009.** For 2000 the filings are unstructured text — the dates are free, the numbers need parsing. |
| yfinance `.info` | **No** | Returns *today's* ratios. Using it for 2000 is pure lookahead. |

With Sharadar, use **dimension `ARQ`** (as-reported quarterly), not `MRQ`.
`MR*` is the current view with restatements back-propagated — the numbers as
they are known *now*, not as they were known then. The loader warns if you pick
one.

```bash
python3 scripts/fetch_fundamentals.py --format sharadar --src sf1.csv --dimension ARQ
```

The loader **refuses to write** if it finds no filing-date column, and warns if
the median reporting lag is implausibly short — the signature of an export that
is not really point-in-time.

## Restatements

A true PIT source keeps what was *originally reported*, including figures later
restated. That is correct: you traded on the original. `FundamentalRecord`
carries `is_restatement`, and lookups prefer the original filing. For 2000 this
is not academic — the era's accounting restatements are a large part of what
made the following two years what they were.


---

# Historical options data — where it actually lives

Tested from a machine with normal network access on 2026-08-22.

## The thing to understand first

**You cannot scrape the past.** Yahoo, CBOE's delayed feed, Tradier and every
broker chain endpoint serve the CURRENT chain only. There is no free endpoint
anywhere that returns what SPY options were quoted at on an arbitrary past
date. Historical option data is either **bought**, or **accumulated going
forward by recording chains daily**. A scraper pointed at a live chain endpoint
starts producing usable history the day you turn it on, and not one day sooner.

That is why this repo models option prices: not an oversight, a market.

## Verified reachable and returning real data

| Source | What it returns | Verified |
|---|---|---|
| **Alpha Vantage `HISTORICAL_OPTIONS`** | Full EOD chain for a past date | **yes — 998 IBM contracts for 2017-11-15** |
| CBOE delayed quotes JSON | Current chain, greeks + OI | yes, 200 |
| OptionsDX | Bulk EOD chain files, free w/ account | site 200, files not tested |
| CBOE DataShop | Paid historical, 2004+ | site 200 |
| historicaloptiondata.com | One-time purchase, ~2002+ | site 200 |
| WRDS / OptionMetrics IvyDB | The academic standard, **1996+** | host 200 |

Yahoo's option endpoint returned **401** when probed and should not be relied
on. Polygon returned 401 without a key.

## Alpha Vantage is the best free option, with one real catch

One record carries everything this engine currently invents:

```
contractID  IBM171117C00075000     bid 70.45   ask 74.10   bid_size 4
strike      75.00                  volume 0    open_interest 0
implied_volatility 4.20990
delta 0.98977  gamma 0.00059  theta -0.31401  vega 0.00296  rho 0.00402
```

Real bid/ask with sizes, IV and a full greek set. Feeding this in would let
`OptionPricer`/`VolSurface` be **replaced by observation** rather than tuned.

The catch is the rate limit. The free tier is ~25 requests/day and one request
is one symbol-day, so **a single year of one symbol is ~252 requests** — around
ten days of free-tier quota, or one paid month. Budget for the premium tier
before planning a multi-symbol study.

Depth is **unverified**: the demo key only answers for its one demo date. Get a
free key and confirm how far back your symbols actually go before committing.

## For the year 2000 specifically

Alpha Vantage, OptionsDX, Polygon and ThetaData all start well after 2000.
**OptionMetrics IvyDB (1996+) is essentially the only source that covers it**,
and via WRDS it is free with a university affiliation and expensive without
one. CBOE DataShop starts around 2004.

So the choice is: buy 2000 coverage, or move the options study to an era where
data is free. The modern era is also friendlier on the merits — zero
commissions and penny ticks, versus the $15 + $1.75/contract that made a
two-contract round trip cost ~4% of a $1,000 position in 2000.

## On scraping, plainly

Recording a live chain daily from a provider whose terms permit it is normal
practice. Bulk-scraping a paid vendor's historical archive to avoid paying is
both a terms violation and how you get your IP banned. Check the terms of any
endpoint before pointing a recorder at it, and rate-limit it.
