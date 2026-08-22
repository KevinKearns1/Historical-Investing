"""Resolve a period-correct CIK for symbols EDGAR's current ticker map cannot.

Why this exists
---------------
`fetch_earnings_edgar.py` resolves tickers through EDGAR's company_tickers.json,
which lists only *active* registrants and maps each ticker to whoever owns it
TODAY. For a year-2000 simulation that fails in two directions:

  missing   -- WorldCom, CMGI, Ariba, JDS Uniphase, Sun, Nortel, Yahoo!, Broadcom
               went bankrupt or were acquired and are simply absent.
  reassigned-- ORCL, DELL and XOM now point at later holding entities, and LU
               points at Lufax Holding, which in 2000 was Lucent.

A wrong CIK does not error. It silently attaches another company's filing dates
to your symbol. So this script does NOT write config/cik_overrides.yml. It
searches EDGAR by company name, then pulls each candidate's submissions record
and prints the evidence -- legal name, SIC, filing window, former tickers -- so
a human confirms before anything is trusted.

    python scripts/resolve_ciks.py --start 1999-01-01 --end 2001-06-30
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime

UA = os.environ.get("EDGAR_USER_AGENT", "")
SEARCH = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company={q}"
          "&type=8-K&dateb=&owner=include&count=40&output=atom")
SUBS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# The company name to search EDGAR for. The ticker is useless here -- that is
# the whole problem -- so each is the name the company filed under in 2000.
NAMES = {
    "YHOO": "YAHOO",
    "JDSU": "JDS UNIPHASE",
    "CMGI": "CMGI",
    "BRCM": "BROADCOM",
    "SUNW": "SUN MICROSYSTEMS",
    "NT":   "NORTEL NETWORKS",
    "WCOM": "WORLDCOM",
    "ARBA": "ARIBA",
    "LU":   "LUCENT TECHNOLOGIES",
    "ORCL": "ORACLE",
    "DELL": "DELL COMPUTER",
    "XOM":  "EXXON MOBIL",
}


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
        if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
            data = gzip.decompress(data)
    time.sleep(0.12)                      # EDGAR fair access: <=10 req/sec
    return data


def search_ciks(name: str) -> list[int]:
    """CIKs whose company name matches. The atom feed's name attribute is
    mangled by an EDGAR-side bug ("ARRAY(0x...)"), so only the CIK is taken
    from here -- the real name comes from the submissions record below."""
    xml = _get(SEARCH.format(q=urllib.parse.quote(name))).decode("utf-8", "replace")
    seen, out = set(), []
    for m in re.finditer(r"<cik>(\d+)</cik>", xml):
        c = int(m.group(1))
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def profile(cik: int) -> dict | None:
    try:
        sub = json.loads(_get(SUBS.format(cik=cik)))
    except Exception:                                            # noqa: BLE001
        return None
    dates = [d for d in sub.get("filings", {}).get("recent", {}).get("filingDate", []) if d]
    for extra in sub.get("filings", {}).get("files", []) or []:
        try:
            blk = json.loads(_get("https://data.sec.gov/submissions/" + extra["name"]))
            dates.extend([d for d in blk.get("filingDate", []) if d])
        except Exception:                                        # noqa: BLE001
            pass
    return {
        "cik": cik,
        "name": sub.get("name", ""),
        "sic": f'{sub.get("sic","")} {sub.get("sicDescription","")}'.strip(),
        "tickers": sub.get("tickers", []),
        "former": [f.get("name") for f in sub.get("formerNames", []) or []],
        "first": min(dates) if dates else None,
        "last": max(dates) if dates else None,
        "n": len(dates),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="1999-01-01")
    ap.add_argument("--end", default="2001-06-30")
    ap.add_argument("--symbols", nargs="*")
    a = ap.parse_args()

    if not UA or "example.com" in UA:
        print("set EDGAR_USER_AGENT='Your Name you@example.com' first")
        return 2

    start = datetime.strptime(a.start, "%Y-%m-%d").date()
    end = datetime.strptime(a.end, "%Y-%m-%d").date()
    syms = [s.upper() for s in (a.symbols or NAMES)]

    print(f"searching EDGAR for filers active {start} .. {end}\n")
    for sym in syms:
        q = NAMES.get(sym, sym)
        print(f"{sym}  (searching \"{q}\")")
        try:
            ciks = search_ciks(q)
        except Exception as e:                                   # noqa: BLE001
            print(f"    search failed: {e}\n")
            continue
        if not ciks:
            print("    no name match\n")
            continue
        hits = []
        for c in ciks[:12]:
            p = profile(c)
            if not p or not p["first"]:
                continue
            f = datetime.strptime(p["first"], "%Y-%m-%d").date()
            l = datetime.strptime(p["last"], "%Y-%m-%d").date()
            if f <= end and l >= start:            # filing history overlaps window
                hits.append(p)
        if not hits:
            print("    name matched, but no candidate filed inside the window\n")
            continue
        for p in hits:
            print(f"    CIK {p['cik']:<9} {p['name']}")
            print(f"        SIC     {p['sic']}")
            print(f"        filings {p['first']} .. {p['last']}  ({p['n']} total)")
            if p["former"]:
                print(f"        former  {'; '.join(p['former'][:3])}")
        print("    ^ confirm the right entity, then add to config/cik_overrides.yml\n")

    print("Nothing was written. A wrong CIK does not error -- it attaches another")
    print("company's filing dates to your symbol -- so cik_overrides.yml is filled")
    print("in by hand, from the evidence above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
