#!/usr/bin/env python3
"""Verified earnings announcement dates, from SEC EDGAR primary filings.

WHY EDGAR AND NOT AN EARNINGS API
---------------------------------
Almost every free earnings-calendar API carries only recent quarters. The one
connected to this session returns a trailing 8 quarters -- useful for 2025, no
help at all for 2000. Paid calendars that claim deep history are usually
back-filled from secondary sources and carry silent errors on exactly the
delisted names a 2000 study cares about.

EDGAR is the primary record. When a company announced results it filed an 8-K,
and EDGAR stamps that filing with the exact date it was ACCEPTED. That
acceptance timestamp is the moment the information became public -- which is
precisely the date a point-in-time simulation needs, and it is free and
authoritative back to 1993-1996.

WHAT THIS WRITES
----------------
config/earnings_verified.yml, with, per symbol, one entry per announcement:
    date, accession number, form type, and the acceptance datetime.
The accession number means every date can be traced back to the filing it came
from. Nothing in the output is inferred.

A NOTE ON THE 8-K ITEM NUMBER
-----------------------------
"Results of Operations and Financial Condition" is Item 2.02 today, but that
numbering only began 2004-08-23. Before then earnings 8-Ks were filed under
Item 5 ("Other Events") or Item 12. So for 2000 you cannot filter on 2.02 --
this script instead takes 8-K filings and, where an item list is present, keeps
the earnings-bearing items for the era. Filings it cannot classify are written
with `confidence: low` rather than dropped, so you can see what it was unsure
about instead of it silently guessing.

USAGE
    python3 scripts/fetch_earnings_edgar.py --start 1999-01-01 --end 2001-06-30

EDGAR requires a descriptive User-Agent with a contact address, and rate-limits
to 10 requests/second. Both are respected below.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import gzip
import time
import zlib
import urllib.request
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = os.environ.get("EDGAR_USER_AGENT", "Historical-Investing research contact@example.com")
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# Earnings-bearing 8-K items, by era. Item 2.02 exists only from 2004-08-23.
ITEMS_MODERN = {"2.02"}
ITEMS_LEGACY = {"5", "12", "9"}
ITEM_RENUMBER_DATE = date(2004, 8, 23)


def _get(url: str, retries: int = 4) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Encoding": "gzip, deflate", "Host": url.split("/")[2]})
    delay = 1.0
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
                # We advertise gzip above, and EDGAR takes us up on it. Without
                # this the JSON parse dies on byte 0x8b of the gzip magic.
                enc = (r.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip":
                data = gzip.decompress(data)
            elif enc == "deflate":
                data = zlib.decompress(data, -zlib.MAX_WBITS)
            time.sleep(0.11)          # EDGAR fair-access: <=10 req/sec
            return data
        except Exception as e:                                   # noqa: BLE001
            if attempt == retries - 1:
                raise
            print(f"    retry {attempt+1} after {e}")
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def load_ticker_cik_map() -> dict:
    raw = json.loads(_get(TICKER_MAP_URL))
    return {v["ticker"].upper(): int(v["cik_str"]) for v in raw.values()}


def resolve_cik(symbol: str, tmap: dict, overrides: dict) -> int | None:
    """Current tickers resolve from EDGAR's own map. Companies that were
    acquired or went bankrupt are NOT in it -- WorldCom, CMGI, Ariba and the
    rest of the interesting 2000 names -- so those need an explicit CIK, which
    is what config/cik_overrides.yml is for."""
    if symbol.upper() in overrides:
        return int(overrides[symbol.upper()])
    return tmap.get(symbol.upper())


def earnings_filings(cik: int, start: date, end: date) -> tuple[list[dict], dict]:
    """Returns (earnings 8-Ks in window, entity metadata).

    The metadata exists so the caller can check that this CIK is the entity
    that actually traded under the ticker during the window -- see
    `entity_covers_window`.
    """
    sub = json.loads(_get(SUBMISSIONS_URL.format(cik=cik)))
    out = []
    all_dates: list[str] = []
    blocks = [sub.get("filings", {}).get("recent", {})]
    # Older filings live in separate index files EDGAR points at.
    for extra in sub.get("filings", {}).get("files", []) or []:
        try:
            blocks.append(json.loads(_get("https://data.sec.gov/submissions/" + extra["name"])))
        except Exception as e:                                   # noqa: BLE001
            print(f"    could not read {extra.get('name')}: {e}")

    for b in blocks:
        all_dates.extend([d for d in b.get("filingDate", []) if d])
        forms = b.get("form", [])
        for i, form in enumerate(forms):
            if not str(form).startswith("8-K"):
                continue
            fdate = b.get("filingDate", [None] * len(forms))[i]
            if not fdate:
                continue
            d = datetime.strptime(fdate, "%Y-%m-%d").date()
            if not (start <= d <= end):
                continue
            items = str(b.get("items", [""] * len(forms))[i] or "")
            item_set = {x.strip() for x in items.split(",") if x.strip()}
            if d >= ITEM_RENUMBER_DATE:
                hit = bool(item_set & ITEMS_MODERN)
                conf = "high" if hit else "low"
            else:
                # Pre-2004 item numbering does not identify earnings reliably.
                hit = bool(item_set & ITEMS_LEGACY) or not item_set
                conf = "medium" if item_set else "low"
            if not hit:
                continue
            out.append({
                "date": d.isoformat(),
                "accession": b.get("accessionNumber", [""] * len(forms))[i],
                "form": str(form),
                "items": items,
                "acceptance": b.get("acceptanceDateTime", [""] * len(forms))[i],
                "confidence": conf,
            })
    out.sort(key=lambda x: x["date"])
    meta = {
        "name": sub.get("name", ""),
        "first_filing": min(all_dates) if all_dates else None,
        "last_filing": max(all_dates) if all_dates else None,
    }
    return out, meta


def entity_covers_window(meta: dict, start: date, end: date) -> bool:
    """Does this EDGAR entity actually have a filing history overlapping the
    simulated window?

    EDGAR's ticker map resolves a ticker to whoever owns it TODAY, which is not
    who traded under it in 2000. Observed on a real run:

        ORCL -> Oracle Corp        (the 2005 holding entity, first filing 2016)
        DELL -> Dell Technologies  (the 2013 re-IPO entity, not Dell Computer)
        XOM  -> ExxonMobil Holdings Corp        (first filing 2026)
        LU   -> Lufax Holding Ltd  -- in 2000 LU was LUCENT

    The first three merely return nothing. LU is the dangerous one: a totally
    unrelated company. Had Lufax filed anything in 1999-2001 those dates would
    have been attached to Lucent silently -- the same failure mode a wrong CIK
    in cik_overrides.yml causes, arriving instead through EDGAR's own map.
    """
    if not meta.get("first_filing") or not meta.get("last_filing"):
        return False
    first = datetime.strptime(meta["first_filing"], "%Y-%m-%d").date()
    last = datetime.strptime(meta["last_filing"], "%Y-%m-%d").date()
    return first <= end and last >= start


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="config/universe_2000.yml")
    ap.add_argument("--start", default="1999-01-01")
    ap.add_argument("--end", default="2001-06-30")
    ap.add_argument("--out", default="config/earnings_verified.yml")
    a = ap.parse_args()

    if "example.com" in UA:
        print("WARNING: set EDGAR_USER_AGENT to 'Your Name your@email' -- EDGAR\n"
              "         rate-limits and may block anonymous automated access.\n")

    with open(os.path.join(ROOT, a.universe)) as f:
        uni = yaml.safe_load(f)
    symbols = (uni.get("tech") or []) + (uni.get("value") or [])

    ov_path = os.path.join(ROOT, "config/cik_overrides.yml")
    overrides = {}
    if os.path.exists(ov_path):
        with open(ov_path) as f:
            overrides = {k.upper(): v for k, v in (yaml.safe_load(f) or {}).items()
                         if isinstance(v, int)}

    start = datetime.strptime(a.start, "%Y-%m-%d").date()
    end = datetime.strptime(a.end, "%Y-%m-%d").date()

    print("resolving tickers to CIKs via EDGAR...")
    tmap = load_ticker_cik_map()

    result, unresolved, reassigned = {}, [], {}
    for sym in symbols:
        cik = resolve_cik(sym, tmap, overrides)
        if cik is None:
            unresolved.append(sym)
            print(f"  {sym:8s} NO CIK -- add it to config/cik_overrides.yml")
            continue
        try:
            fil, meta = earnings_filings(cik, start, end)
        except Exception as e:                                   # noqa: BLE001
            print(f"  {sym:8s} ERROR {e}")
            unresolved.append(sym)
            continue
        if not entity_covers_window(meta, start, end):
            unresolved.append(sym)
            reassigned[sym] = {"cik": cik, "edgar_name": meta.get("name", ""),
                               "first_filing": meta.get("first_filing"),
                               "last_filing": meta.get("last_filing")}
            print(f"  {sym:8s} CIK {cik:<10d} WRONG ENTITY -- "
                  f"{meta.get('name','?')!r} filed "
                  f"{meta.get('first_filing')}..{meta.get('last_filing')}, "
                  f"nothing in window. Ticker was reassigned; find the "
                  f"period CIK and put it in cik_overrides.yml")
            continue
        result[sym] = fil
        hi = sum(1 for x in fil if x["confidence"] in ("high", "medium"))
        print(f"  {sym:8s} CIK {cik:<10d} {len(fil):3d} candidate 8-Ks ({hi} confident)")

    out = {
        "verified": True,
        "source": "SEC EDGAR 8-K filings (data.sec.gov submissions API)",
        "generated": datetime.utcnow().isoformat() + "Z",
        "range": [a.start, a.end],
        "note": ("Dates are EDGAR filing dates -- the date the announcement became "
                 "public. Pre-2004-08-23 filings cannot be identified by item 2.02 "
                 "because that numbering did not exist; those carry confidence "
                 "medium/low and should be spot-checked against the filing text."),
        "unresolved_symbols": unresolved,
        "ticker_reassigned": reassigned,
        "filings": result,
    }
    path = os.path.join(ROOT, a.out)
    with open(path, "w") as f:
        yaml.safe_dump(out, f, sort_keys=False, default_flow_style=False)
    print(f"\nwrote {path}")
    if unresolved:
        print(f"\n{len(unresolved)} symbols unresolved: {', '.join(unresolved)}")
        print("These are mostly companies that were acquired or went bankrupt and")
        print("no longer appear in EDGAR's current ticker map. Look up each CIK at")
        print("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany and add")
        print("it to config/cik_overrides.yml -- their filings are still on EDGAR.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
