"""Account state: cash, equity positions, option positions, margin, P&L."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from engine.options import OptionContract


@dataclass
class EquityPosition:
    symbol: str
    shares: float = 0.0          # negative = short
    avg_price: float = 0.0
    opened: datetime | None = None
    strategy: str = ""
    stop: float | None = None
    target: float | None = None

    def market_value(self, px: float) -> float:
        return self.shares * px

    def unrealized(self, px: float) -> float:
        return self.shares * (px - self.avg_price)


@dataclass
class OptionPosition:
    contract: OptionContract
    contracts: int = 0           # negative = short
    avg_premium: float = 0.0
    opened: datetime | None = None
    strategy: str = ""
    leg_id: str = ""             # groups the legs of a spread

    def market_value(self, premium: float) -> float:
        return self.contracts * premium * 100

    def unrealized(self, premium: float) -> float:
        return self.contracts * (premium - self.avg_premium) * 100


@dataclass
class Trade:
    ts: datetime
    strategy: str
    symbol: str
    side: str
    qty: float
    price: float
    fees: float
    kind: str = "equity"         # "equity" | "option"
    note: str = ""
    pnl: float = 0.0             # realized on closing trades


@dataclass
class Portfolio:
    """Reg T cash-and-margin account, 2000 rules.

    Note on leverage: the 4x day trading buying power that day traders know
    today came in with the 2001 rule set. In 2000 the correct constraint is
    plain Reg T -- 50% initial margin, i.e. 2x -- so that is what is enforced.
    """

    starting_cash: float = 25_000.0
    cash: float = field(init=False)
    equities: dict[str, EquityPosition] = field(default_factory=dict)
    options: dict[str, OptionPosition] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    interest_paid: float = 0.0
    borrow_paid: float = 0.0
    initial_margin: float = 0.50
    maintenance_margin: float = 0.25

    def __post_init__(self) -> None:
        self.cash = self.starting_cash

    # -- valuation --------------------------------------------------------
    def equity_value(self, prices: dict[str, float]) -> float:
        return sum(p.market_value(prices.get(s, p.avg_price))
                   for s, p in self.equities.items())

    def option_value(self, premiums: dict[str, float]) -> float:
        return sum(p.market_value(premiums.get(k, p.avg_premium))
                   for k, p in self.options.items())

    def net_liq(self, prices: dict[str, float], premiums: dict[str, float] | None = None) -> float:
        return self.cash + self.equity_value(prices) + self.option_value(premiums or {})

    def gross_exposure(self, prices: dict[str, float]) -> float:
        return sum(abs(p.market_value(prices.get(s, p.avg_price)))
                   for s, p in self.equities.items())

    def buying_power(self, prices: dict[str, float], premiums: dict[str, float] | None = None) -> float:
        """Reg T: net liq / initial margin, less what is already committed."""
        nl = self.net_liq(prices, premiums)
        return max(nl / self.initial_margin - self.gross_exposure(prices), 0.0)

    def margin_debit(self, prices: dict[str, float]) -> float:
        """Borrowed cash carrying long positions."""
        longs = sum(p.market_value(prices.get(s, p.avg_price))
                    for s, p in self.equities.items() if p.shares > 0)
        return max(longs - self.cash, 0.0)

    def maintenance_call(self, prices: dict[str, float]) -> bool:
        gross = self.gross_exposure(prices)
        if gross <= 0:
            return False
        return self.net_liq(prices) < gross * self.maintenance_margin

    # -- bookkeeping ------------------------------------------------------
    def record(self, t: Trade) -> None:
        self.trades.append(t)
        self.fees_paid += t.fees
        self.realized_pnl += t.pnl

    def apply_equity_fill(self, ts: datetime, symbol: str, qty: float, price: float,
                          fees: float, strategy: str, note: str = "") -> float:
        """qty > 0 buys, qty < 0 sells. Returns realized P&L on the fill."""
        pos = self.equities.get(symbol) or EquityPosition(symbol, strategy=strategy)
        realized = 0.0
        # Closing or reducing?
        if pos.shares != 0 and (pos.shares > 0) != (qty > 0):
            closing = min(abs(qty), abs(pos.shares))
            direction = 1 if pos.shares > 0 else -1
            realized = direction * closing * (price - pos.avg_price)
            remaining = pos.shares + qty
            if abs(remaining) < 1e-9:
                pos.shares = 0.0
            elif (remaining > 0) != (pos.shares > 0):
                pos.avg_price = price       # flipped through zero
                pos.shares = remaining
                pos.opened = ts
            else:
                pos.shares = remaining
        else:
            total = pos.shares + qty
            if abs(total) > 1e-9:
                pos.avg_price = (pos.avg_price * pos.shares + price * qty) / total
            pos.shares = total
            if pos.opened is None:
                pos.opened = ts
            pos.strategy = strategy

        self.cash -= qty * price
        self.cash -= fees
        self.record(Trade(ts, strategy, symbol, "BUY" if qty > 0 else "SELL",
                          abs(qty), price, fees, "equity", note, realized))
        if abs(pos.shares) < 1e-9:
            self.equities.pop(symbol, None)
        else:
            self.equities[symbol] = pos
        return realized

    def apply_option_fill(self, ts: datetime, contract: OptionContract, qty: int,
                          premium: float, fees: float, strategy: str,
                          leg_id: str = "", note: str = "") -> float:
        key = contract.key
        pos = self.options.get(key) or OptionPosition(contract, strategy=strategy, leg_id=leg_id)
        realized = 0.0
        if pos.contracts != 0 and (pos.contracts > 0) != (qty > 0):
            closing = min(abs(qty), abs(pos.contracts))
            direction = 1 if pos.contracts > 0 else -1
            realized = direction * closing * (premium - pos.avg_premium) * 100
            pos.contracts += qty
        else:
            total = pos.contracts + qty
            if total != 0:
                pos.avg_premium = (pos.avg_premium * pos.contracts + premium * qty) / total
            pos.contracts = total
            if pos.opened is None:
                pos.opened = ts
            pos.strategy = strategy
            pos.leg_id = leg_id or pos.leg_id

        self.cash -= qty * premium * 100
        self.cash -= fees
        self.record(Trade(ts, strategy, str(contract), "BTO/BTC" if qty > 0 else "STO/STC",
                          abs(qty), premium, fees, "option", note, realized))
        if pos.contracts == 0:
            self.options.pop(key, None)
        else:
            self.options[key] = pos
        return realized

    # -- carry ------------------------------------------------------------
    def accrue_overnight(self, prices: dict[str, float], margin_rate: float,
                         borrow_rate: float, htb: set[str] | None = None,
                         htb_rate: float = 0.25, days: int = 1) -> None:
        htb = htb or set()
        debit = self.margin_debit(prices)
        if debit > 0:
            i = debit * margin_rate * days / 360.0
            self.cash -= i
            self.interest_paid += i
        for s, p in self.equities.items():
            if p.shares < 0:
                notional = abs(p.shares) * prices.get(s, p.avg_price)
                rate = htb_rate if s in htb else borrow_rate
                b = notional * rate * days / 360.0
                self.cash -= b
                self.borrow_paid += b
