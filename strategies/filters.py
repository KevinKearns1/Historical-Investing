"""Reusable point-in-time fundamental screens.

DESIGN RULE: UNKNOWN IS NOT FAIL
--------------------------------
Every screen here returns True when the data is absent. That is deliberate and
it is the opposite of what feels safe.

If a missing filing counted as a failure, the screen would quietly become a
filter on DATA COVERAGE rather than on company quality -- and coverage in 2000
is worst exactly for the small, distressed and delisted names. The backtest
would then trade only the large survivors, which is survivorship bias
reintroduced through the back door, wearing a fundamental screen as a disguise.

So a screen only ever REMOVES a name it has seen evidence against. Whether a
name was screened at all is reported, so you can tell a filter that did work
from one that had nothing to work with.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ScreenResult:
    passed: bool
    screened: bool               # was there any data to judge on?
    reasons: list = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.passed


@dataclass
class QualityScreen:
    """A conservative solvency-and-profitability screen.

    Aimed at the question "is it sane to be short a put on this name", which is
    the one place in this book where a fundamental view genuinely belongs: you
    are agreeing to own the stock, so the balance sheet matters.
    """

    max_debt_to_equity: float | None = 2.0
    min_net_margin: float | None = 0.0
    require_positive_eps: bool = True
    require_positive_fcf: bool = False
    max_pe: float | None = None
    min_revenue_growth: float | None = None

    def check(self, kpis: dict) -> ScreenResult:
        if not kpis:
            return ScreenResult(True, False, ["no filing public yet"])
        reasons, seen = [], False

        de = kpis.get("debt_to_equity")
        if de is not None and self.max_debt_to_equity is not None:
            seen = True
            if de > self.max_debt_to_equity:
                reasons.append(f"debt/equity {de:.2f} > {self.max_debt_to_equity}")

        nm = kpis.get("net_margin")
        if nm is not None and self.min_net_margin is not None:
            seen = True
            if nm < self.min_net_margin:
                reasons.append(f"net margin {nm:.1%} < {self.min_net_margin:.1%}")

        eps = kpis.get("eps_ttm")
        if eps is not None and self.require_positive_eps:
            seen = True
            if eps <= 0:
                reasons.append(f"trailing EPS {eps:.2f} not positive")

        fcf = kpis.get("free_cash_flow")
        if fcf is not None and self.require_positive_fcf:
            seen = True
            if fcf <= 0:
                reasons.append("free cash flow negative")

        pe = kpis.get("pe")
        if pe is not None and self.max_pe is not None:
            seen = True
            if pe > self.max_pe:
                reasons.append(f"P/E {pe:.1f} > {self.max_pe}")

        g = kpis.get("revenue_growth_yoy")
        if g is not None and self.min_revenue_growth is not None:
            seen = True
            if g < self.min_revenue_growth:
                reasons.append(f"revenue growth {g:.1%} < {self.min_revenue_growth:.1%}")

        return ScreenResult(not reasons, seen, reasons)


# Ready-made screens.
SOLVENCY = QualityScreen(max_debt_to_equity=2.0, min_net_margin=0.0,
                         require_positive_eps=True, require_positive_fcf=True)
PROFITABLE_GROWTH = QualityScreen(max_debt_to_equity=3.0, min_net_margin=None,
                                  require_positive_eps=False,
                                  min_revenue_growth=0.10)


@dataclass
class ScreenStats:
    """Tracks whether a screen is doing work or just passing everything."""

    checked: int = 0
    with_data: int = 0
    rejected: int = 0
    reasons: dict = field(default_factory=dict)

    def record(self, r: ScreenResult) -> None:
        self.checked += 1
        if r.screened:
            self.with_data += 1
        if not r.passed:
            self.rejected += 1
            for why in r.reasons:
                key = why.split()[0] + " " + (why.split()[1] if len(why.split()) > 1 else "")
                self.reasons[key] = self.reasons.get(key, 0) + 1

    def summary(self) -> str:
        if not self.checked:
            return "screen never ran"
        cov = self.with_data / self.checked
        return (f"{self.checked} checks, {cov:.0%} had a public filing, "
                f"{self.rejected} rejected")
