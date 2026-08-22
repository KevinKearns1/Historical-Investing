"""The simulation loop.

One pass per session. Within a session the clock steps minute by minute from
09:30 to the close, and at each step every strategy whose cadence is due gets
woken. Strategies read through `Context`, which is the only surface they have,
and every read on it goes through the point-in-time feed.

Order of operations in a session, matching a real trading day:
    1. 09:30 open       -- regime computed from data through YESTERDAY's close
    2. minute loop      -- signals, entries, exits
    3. 15:50 flatten    -- day trading sleeve goes flat
    4. 16:00 close      -- mark to market, expiries, assignment
    5. overnight        -- margin interest and short borrow accrue
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import numpy as np

from engine.clock import ET, SimClock, sessions_between, session_close_time
from engine.data import PointInTimeFeed
from engine.microstructure import CostModel, round_to_tick
from engine.options import OptionPricer, VolSurface
from engine.portfolio import Portfolio
from engine.risk import RiskManager, RiskLimits
from engine.broker import Broker
from engine.era import Era, leverage_rules
from engine.fundamentals import FundamentalsStore
from engine.intraday_source import IntradaySource
from engine.provenance import ProvenanceLedger


@dataclass
class Context:
    """Everything a strategy is allowed to see."""

    clock: SimClock
    feed: PointInTimeFeed
    broker: Broker
    pf: Portfolio
    risk: RiskManager
    universe: list[str] = field(default_factory=list)
    regime: dict = field(default_factory=lambda: {"trend": 0, "stress": 0.0})
    fundamentals: FundamentalsStore | None = None
    era: Era | None = None
    minute_index: int = 0
    day_number: int = 0
    logs: list[str] = field(default_factory=list)
    verbose: bool = False
    _fired: set = field(default_factory=set)

    @property
    def now(self) -> datetime:
        return self.clock.now

    @property
    def date(self) -> date:
        return self.clock.session_date

    def kpis(self, symbol: str) -> dict:
        """Point-in-time fundamental ratios for `symbol`, as of right now.

        Returns {} when no filing was public yet -- which is the correct answer
        early in a company's coverage, and a strategy must treat it as "unknown"
        rather than as "fails the filter".
        """
        if self.fundamentals is None:
            return {}
        return self.fundamentals.kpis(symbol, self.feed.last_price(symbol), self.date)

    def fire_once(self, key: str, at_minute: int) -> bool:
        """True exactly once per session, on the first bar at or after
        `at_minute`. Strategies must use this rather than `minute_index == N`,
        which silently never matches when the loop runs at a coarser step.
        """
        if self.minute_index < at_minute or key in self._fired:
            return False
        self._fired.add(key)
        return True

    def reset_day(self) -> None:
        self._fired = set()

    def log(self, msg: str) -> None:
        line = f"{self.now.astimezone(ET):%Y-%m-%d %H:%M} {msg}"
        self.logs.append(line)
        if self.verbose:
            print(line)

    def net_liq(self) -> float:
        return self.pf.net_liq(self._prices(), self._premiums())

    def open_option_premium(self) -> float:
        return sum(abs(p.contracts) * p.avg_premium * 100 for p in self.pf.options.values())

    def _prices(self) -> dict:
        out = {}
        for s in self.pf.equities:
            p = self.feed.last_price(s)
            if p:
                out[s] = p
        return out

    def _premiums(self) -> dict:
        out = {}
        for k, p in self.pf.options.items():
            q = self.broker.option_quote(p.contract, self.regime["stress"])
            if q:
                out[k] = (q[0] + q[1]) / 2.0
            else:
                out[k] = p.contract.intrinsic(self.feed.last_price(p.contract.symbol) or 0.0)
        return out


class Backtest:
    def __init__(self, start: date, end: date, universe: list[str],
                 strategies: list, starting_cash: float = 25_000.0,
                 benchmark: str = "^IXIC", costs: CostModel | None = None,
                 limits: RiskLimits | None = None, hard_to_borrow: set | None = None,
                 minute_step: int = 1, verbose: bool = False,
                 cache_dir: str | None = None, interval_minutes: int = 1,
                 require_real_intraday: bool = False,
                 fundamentals_csv: str | None = None,
                 era_rules: bool = True):
        self.start, self.end = start, end
        self.universe = universe
        self.strategies = strategies
        self.benchmark = benchmark
        self.minute_step = minute_step
        self.hard_to_borrow = hard_to_borrow or set()

        self.clock = SimClock(datetime.combine(start, time(9, 30), tzinfo=ET))
        self.ledger = ProvenanceLedger()
        self.era_rules = era_rules
        self.interval_minutes = max(int(interval_minutes), 1)
        src = IntradaySource(interval_minutes=self.interval_minutes,
                             ledger=self.ledger, require_real=require_real_intraday)
        self.feed = PointInTimeFeed(self.clock, cache_dir, src) if cache_dir else \
            PointInTimeFeed(self.clock, intraday_source=src)
        self.fundamentals = FundamentalsStore(self.clock)
        if fundamentals_csv:
            n = self.fundamentals.load_csv(fundamentals_csv)
            print(f"fundamentals: {n:,} point-in-time records loaded")
        self.pf = Portfolio(starting_cash=starting_cash)
        self.costs = costs or CostModel()
        self.pricer = OptionPricer(VolSurface())
        self.broker = Broker(self.feed, self.pf, self.costs, self.pricer)
        self.risk = RiskManager(limits or RiskLimits())
        self.ctx = Context(self.clock, self.feed, self.broker, self.pf, self.risk,
                           universe, verbose=verbose)
        self.ctx.fundamentals = self.fundamentals
        self.equity_curve: list[tuple[date, float]] = []
        self.daily_rows: list[dict] = []

    # -- setup ------------------------------------------------------------
    def prepare(self) -> list[str]:
        needed = set(self.universe) | {self.benchmark, "QQQ", "SPY"}
        for s in self.strategies:
            for attr in ("symbol", "benchmark"):
                v = getattr(s, attr, None)
                if isinstance(v, str):
                    needed.add(v)
            for v in getattr(s, "value_universe", []) or []:
                needed.add(v)
        found = self.feed.load(sorted(needed))
        missing = sorted(set(needed) - set(found))
        self.universe = [s for s in self.universe if s in found]
        # Strategies read ctx.universe, and prepare() rebinds self.universe to a
        # NEW list -- so the Context would keep the unpruned one and hand a
        # strategy a symbol the feed never loaded. Prune every copy, including
        # the private lists strategies carry (OP-03's value sleeve).
        self.ctx.universe = list(self.universe)
        for s in self.strategies:
            vu = getattr(s, "value_universe", None)
            if vu:
                s.value_universe = [x for x in vu if x in found]
        return missing

    # -- regime -----------------------------------------------------------
    def _regime(self) -> dict:
        """Computed only from bars that closed BEFORE today."""
        c = self.feed.last_close(self.benchmark)
        m10 = self.feed.sma(self.benchmark, 10)
        m20 = self.feed.sma(self.benchmark, 20)
        m50 = self.feed.sma(self.benchmark, 50)
        rv = self.feed.realized_vol(self.benchmark, 21)
        if None in (c, m20):
            return {"trend": 0, "stress": 0.0}
        trend = 1 if (m10 and c > m10 and c > m20) else (-1 if c < m20 else 0)
        stress = 0.0
        if m50 and c < m50:
            stress += 0.4
        if rv:
            stress += min(max((rv - 0.20) / 0.40, 0.0), 0.6)
        h = self.feed.history(self.benchmark, 60)
        if len(h) >= 40:
            peak = float(h["close"].max())
            dd = 1.0 - c / peak if peak > 0 else 0.0
            stress += min(max(dd / 0.30, 0.0), 0.5)
        return {"trend": trend, "stress": float(min(stress, 1.0))}

    # -- expiry and assignment -------------------------------------------
    def _settle_options(self) -> None:
        """Expire options at the close of the third Friday, and handle early
        assignment on short in-the-money contracts."""
        today = self.clock.session_date
        for key, pos in list(self.pf.options.items()):
            c = pos.contract
            S = self.feed.last_price(c.symbol)
            if S is None:
                continue
            expiring = c.expiry <= today
            itm = c.intrinsic(S) > 0

            if expiring:
                if itm:
                    # Physical settlement into shares.
                    if pos.contracts > 0:
                        qty = pos.contracts * 100 * (1 if c.kind == "call" else -1)
                    else:
                        qty = abs(pos.contracts) * 100 * (-1 if c.kind == "call" else 1)
                    fees = self.costs.equity_fees(qty, c.strike, is_sale=qty < 0)
                    self.pf.apply_equity_fill(self.clock.now, c.symbol, qty, c.strike,
                                              fees, pos.strategy, "option exercise/assignment")
                    self.ctx.log(f"[settle] {'exercise' if pos.contracts>0 else 'assigned'} "
                                 f"{c} -> {qty:+.0f} {c.symbol} @ {c.strike:g}")
                self.pf.apply_option_fill(self.clock.now, c, -pos.contracts,
                                          c.intrinsic(S), 0.0, pos.strategy, pos.leg_id,
                                          "expiry")
                continue

            # Early assignment on short American options that are deep ITM
            # with little time value left -- the realistic 2000 risk.
            if pos.contracts < 0 and itm and c.dte(today) <= 7:
                tv = 0.0
                q = self.broker.option_quote(c, self.ctx.regime["stress"])
                if q:
                    tv = max((q[0] + q[1]) / 2.0 - c.intrinsic(S), 0.0)
                if tv < 0.10:
                    qty = abs(pos.contracts) * 100 * (-1 if c.kind == "call" else 1)
                    fees = self.costs.equity_fees(qty, c.strike, is_sale=qty < 0)
                    self.pf.apply_equity_fill(self.clock.now, c.symbol, qty, c.strike,
                                              fees, pos.strategy, "early assignment")
                    self.pf.apply_option_fill(self.clock.now, c, -pos.contracts,
                                              c.intrinsic(S), 0.0, pos.strategy,
                                              pos.leg_id, "early assignment")
                    self.ctx.log(f"[settle] EARLY assignment {c} -> {qty:+.0f} {c.symbol}")

    # -- main loop --------------------------------------------------------
    def run(self) -> dict:
        missing = self.prepare()
        sessions = sessions_between(self.start, self.end)
        if not sessions:
            raise ValueError("no trading sessions in range")

        for i, d in enumerate(sessions):
            self.ctx.day_number = i
            close_t = session_close_time(d)
            self.clock.advance_to(datetime.combine(d, time(9, 30), tzinfo=ET))

            # Regime uses only completed bars -- the feed guarantees it.
            if self.era_rules:
                nl_probe = self.pf.net_liq({}, {})
                self.ctx.era = Era.on(d, 50.0, nl_probe)
                self.costs.apply_era(d)
                lev = leverage_rules(d, nl_probe)
                self.pf.initial_margin = lev.initial_margin
                self.pf.maintenance_margin = lev.maintenance_margin
            self.ctx.regime = self._regime()
            self.ctx.reset_day()
            nl_open = self.ctx.net_liq()
            self.risk.start_day(d, nl_open)

            if self.risk.halted:
                self._record_day(d, nl_open)
                continue

            for s in self.strategies:
                if s.enabled:
                    s.on_session_start(self.ctx)

            total_minutes = int((datetime.combine(d, close_t) -
                                 datetime.combine(d, time(9, 30))).seconds / 60)
            flatten_at = total_minutes - 10

            last_call: dict[str, int] = {}
            for m in range(1, total_minutes + 1, self.minute_step):
                self.clock.advance_to(
                    datetime.combine(d, time(9, 30), tzinfo=ET) + timedelta(minutes=m))
                self.ctx.minute_index = m
                if m == flatten_at:
                    for s in self.strategies:
                        if s.enabled:
                            s.on_session_end(self.ctx)
                    continue
                if m > flatten_at:
                    continue
                for s in self.strategies:
                    if not s.enabled:
                        continue
                    # Elapsed-time scheduling, NOT `m % cadence`: with a coarse
                    # --minute-step the modulo can never match (step 15 against
                    # cadence 30 hits 1,16,31,... and never a multiple of 30),
                    # which silently disables the strategy for the whole run.
                    last = last_call.get(s.name)
                    if last is None or (m - last) >= s.cadence:
                        last_call[s.name] = m
                        s.on_bar(self.ctx)
                if m % 30 == 0:
                    self.risk.update(self.ctx.net_liq())
                    if self.risk.halted:
                        self._liquidate("risk halt")
                        break

            # Close: settle, mark, accrue.
            self.clock.advance_to(datetime.combine(d, close_t, tzinfo=ET))
            self._settle_options()
            nl_close = self.ctx.net_liq()
            self.risk.update(nl_close)

            nxt = sessions[i + 1] if i + 1 < len(sessions) else d + timedelta(days=1)
            self.pf.accrue_overnight(self.ctx._prices(), self.costs.margin_rate_annual,
                                     self.costs.borrow_rate_annual, self.hard_to_borrow,
                                     self.costs.hard_to_borrow_rate, max((nxt - d).days, 1))
            self._record_day(d, self.ctx.net_liq())

        return {"provenance": self.ledger.summary(),
                "fundamentals_coverage": self.fundamentals.coverage(),
                "missing_symbols": missing,
                "equity_curve": self.equity_curve,
                "daily": self.daily_rows,
                "portfolio": self.pf,
                "rejects": self.broker.rejects,
                "risk": self.risk,
                "logs": self.ctx.logs}

    def _liquidate(self, why: str) -> None:
        for sym in list(self.pf.equities):
            self.broker.close_equity(sym, "RISK", why)
        for key in list(self.pf.options):
            self.broker.close_option(key, "RISK", why, self.ctx.regime["stress"])

    def _record_day(self, d: date, nl: float) -> None:
        self.equity_curve.append((d, nl))
        self.daily_rows.append({
            "date": d, "net_liq": nl, "cash": self.pf.cash,
            "n_equity": len(self.pf.equities), "n_options": len(self.pf.options),
            "trend": self.ctx.regime["trend"], "stress": round(self.ctx.regime["stress"], 3),
            "realized_pnl": self.pf.realized_pnl, "fees": self.pf.fees_paid,
            "halted": self.risk.halted,
        })
