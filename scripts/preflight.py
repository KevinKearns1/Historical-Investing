#!/usr/bin/env python3
"""Check that this machine can actually fetch the data, BEFORE a long run.

Run this first on any new machine:

    python3 scripts/preflight.py

It distinguishes the two failure modes that look identical in a log and have
completely different fixes:

  PROXY DENIAL   the CONNECT tunnel is refused by a proxy on your own side.
                 Nothing ever reaches the data provider. Browser headers,
                 user-agents and request pacing are irrelevant, because none of
                 them are ever sent -- they live inside a tunnel that was never
                 opened. Fix: run somewhere without that egress policy.

  PROVIDER BLOCK the TLS handshake succeeded, the request was delivered, and
                 the SERVER returned 403/429. Now headers and pacing matter.
                 Fix: set a proper User-Agent and slow down.

Telling them apart is the whole point. Chasing a provider-block fix for a proxy
denial wastes real money on proxy services that cannot help.
"""
from __future__ import annotations

import os
import shutil
import socket
import ssl
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHECKS = [
    ("Yahoo Finance (daily bars)", "https://query1.finance.yahoo.com/v8/finance/chart/MSFT?range=5d&interval=1d"),
    ("Yahoo cookie/crumb host", "https://fc.yahoo.com/"),
    ("SEC EDGAR ticker map", "https://www.sec.gov/files/company_tickers.json"),
    ("SEC EDGAR submissions", "https://data.sec.gov/submissions/CIK0000789019.json"),
    ("PyPI (package installs)", "https://pypi.org/simple/"),
]

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = RESET = ""


def classify(url: str, timeout: int = 20) -> tuple[str, str]:
    """Returns (status, detail). status in {ok, proxy_denied, provider_block, dns, tls, error}."""
    host = urlparse(url).hostname
    ua = os.environ.get("EDGAR_USER_AGENT") if "sec.gov" in (host or "") else None
    headers = {"User-Agent": ua or "HistoricalInvesting/1.0 preflight"}

    try:
        socket.getaddrinfo(host, 443)
    except socket.gaierror as e:
        return "dns", f"DNS lookup failed: {e}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return "ok", f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        # The tunnel opened and the SERVER answered. Headers/pacing matter here.
        return "provider_block", f"server returned HTTP {e.code}"
    except urllib.error.URLError as e:
        msg = str(e.reason)
        low = msg.lower()
        if "tunnel" in low or "403" in low or "407" in low or "proxy" in low:
            return "proxy_denied", f"CONNECT refused by local proxy: {msg}"
        if isinstance(e.reason, ssl.SSLError):
            return "tls", f"TLS failure: {msg}"
        if "timed out" in low:
            return "error", "timed out"
        return "error", msg
    except Exception as e:                                       # noqa: BLE001
        return "error", str(e)


def main() -> int:
    print("=" * 68)
    print("  PREFLIGHT")
    print("=" * 68)

    # -- environment ------------------------------------------------------
    print(f"\npython            {sys.version.split()[0]}")
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        print(f"HTTPS_PROXY       {proxy}")
        print(f"{YELLOW}                  an egress proxy is configured. If the checks below")
        print(f"                  report PROXY DENIAL, that proxy is the cause.{RESET}")
    else:
        print(f"HTTPS_PROXY       {DIM}not set (direct connection){RESET}")

    ua = os.environ.get("EDGAR_USER_AGENT")
    if not ua:
        print(f"EDGAR_USER_AGENT  {RED}NOT SET{RESET}")
        print("                  EDGAR requires a descriptive User-Agent with a real")
        print("                  contact address or it returns 403. Set it before")
        print("                  fetching earnings dates:")
        print('                    export EDGAR_USER_AGENT="Your Name you@example.com"')
    elif "@" not in ua or "example.com" in ua:
        print(f"EDGAR_USER_AGENT  {YELLOW}{ua}{RESET}")
        print("                  needs a REAL contact email. EDGAR blocks placeholders.")
    else:
        print(f"EDGAR_USER_AGENT  {GREEN}{ua}{RESET}")

    free = shutil.disk_usage(ROOT).free / 1e9
    print(f"free disk         {free:.1f} GB" +
          (f"  {YELLOW}(1-minute data for a year is tens of GB){RESET}" if free < 20 else ""))

    # -- packages ---------------------------------------------------------
    print("\npackages")
    missing = []
    for mod, label in [("pandas", "pandas"), ("numpy", "numpy"), ("scipy", "scipy"),
                       ("yaml", "PyYAML"), ("yfinance", "yfinance")]:
        try:
            m = __import__(mod)
            v = getattr(m, "__version__", "?")
            print(f"  {GREEN}ok{RESET}    {label:<12} {v}")
        except ImportError:
            print(f"  {RED}MISSING{RESET} {label}")
            missing.append(label)
    if missing:
        print(f"\n  pip install {' '.join(missing)}")

    # -- connectivity -----------------------------------------------------
    print("\nconnectivity")
    results = {}
    for label, url in CHECKS:
        status, detail = classify(url)
        results[label] = status
        mark = {"ok": f"{GREEN}ok{RESET}     ",
                "proxy_denied": f"{RED}BLOCKED{RESET}",
                "provider_block": f"{YELLOW}REFUSED{RESET}",
                "dns": f"{RED}DNS{RESET}    ",
                "tls": f"{RED}TLS{RESET}    ",
                "error": f"{RED}ERROR{RESET}  "}[status]
        print(f"  {mark} {label:<30} {DIM}{detail}{RESET}")

    # -- verdict ----------------------------------------------------------
    print("\n" + "=" * 68)
    denied = [k for k, v in results.items() if v == "proxy_denied"]
    refused = [k for k, v in results.items() if v == "provider_block"]
    yahoo_ok = results.get("Yahoo Finance (daily bars)") == "ok"
    edgar_ok = results.get("SEC EDGAR ticker map") == "ok"

    if denied:
        print(f"{RED}  PROXY DENIAL on {len(denied)} host(s).{RESET}")
        print("\n  The CONNECT tunnel was refused by a proxy on YOUR side of the")
        print("  connection. No packet reached the data provider, no TLS handshake")
        print("  happened, and the provider never saw a request.")
        print("\n  This means the following CANNOT help, because none of them are")
        print("  ever transmitted -- they would live inside a tunnel that was")
        print("  never opened:")
        print("    - browser User-Agent strings and Accept headers")
        print("    - request pacing, jitter, or randomised delays")
        print("    - rotating source IPs or residential proxy services")
        print("\n  The fix is to run where that egress policy does not apply --")
        print("  normally your own machine. See SETUP.md.")
    elif refused:
        print(f"{YELLOW}  PROVIDER REFUSAL on {len(refused)} host(s).{RESET}")
        print("\n  The request WAS delivered and the server chose to reject it.")
        print("  Here headers and pacing genuinely matter:")
        print("    - EDGAR: set EDGAR_USER_AGENT with a real contact email")
        print("    - back off; the fetch scripts already rate-limit, but a shared")
        print("      IP may already be near a provider's limit")
    elif yahoo_ok and edgar_ok:
        print(f"{GREEN}  ALL CLEAR.{RESET} This machine can fetch everything the engine needs.")
        print("\n  Next:")
        print("    python3 scripts/fetch_data.py --start 1999-01-01 --end 2001-01-31")
        print("    python3 scripts/fetch_earnings_edgar.py --start 1999-01-01 --end 2001-06-30")
        print("    python3 scripts/run_backtest.py --start 2000-01-01 --end 2000-12-31")
    else:
        print(f"{YELLOW}  MIXED RESULTS.{RESET} See the per-host detail above.")

    cache = os.path.join(ROOT, "data", "cache")
    n = len([f for f in os.listdir(cache) if f.endswith(".csv")]) if os.path.isdir(cache) else 0
    print(f"\n  cached daily-bar symbols: {n}")
    if not n:
        print("  (empty -- run fetch_data.py, or make_demo_data.py for an offline test)")
    print("=" * 68)
    return 0 if not denied else 1


if __name__ == "__main__":
    raise SystemExit(main())
