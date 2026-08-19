"""Performance statistics, computed per strategy and for the whole book."""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np
import pandas as pd


def _returns(curve: list[tuple]) -> np.ndarray:
    v = np.array([x[1] for x in curve], dtype=float)
    if len(v) < 2:
        return np.array([])
    return v[1:] / np.maximum(v[:-1], 1e-9) - 1.0


def summarize(curve: list[tuple], starting: float, rf: float = 0.0595) -> dict:
    """rf defaults to the 2000 average 3-month T-bill, ~5.95%. In a year when
    cash paid nearly 6%, a strategy has to clear a real hurdle before it has
    earned anything at all -- reporting a raw return against zero would be
    flattering to the point of dishonesty."""
    if len(curve) < 2:
        return {}
    v = np.array([x[1] for x in curve], dtype=float)
    r = _returns(curve)
    n = len(v)
    years = n / 252.0
    total = v[-1] / starting - 1.0
    cagr = (v[-1] / starting) ** (1 / years) - 1.0 if years > 0 and v[-1] > 0 else -1.0
    vol = r.std(ddof=1) * math.sqrt(252) if len(r) > 1 else 0.0
    excess = r - rf / 252.0
    sharpe = (excess.mean() / r.std(ddof=1) * math.sqrt(252)) if r.std(ddof=1) > 1e-12 else 0.0
    downside = r[r < 0]
    sortino = (excess.mean() / downside.std(ddof=1) * math.sqrt(252)) if len(downside) > 1 and downside.std(ddof=1) > 1e-12 else 0.0
    peak = np.maximum.accumulate(v)
    dd = v / peak - 1.0
    max_dd = float(dd.min())
    dd_i = int(dd.argmin())
    calmar = cagr / abs(max_dd) if max_dd < -1e-9 else 0.0
    return {
        "starting_equity": starting,
        "ending_equity": float(v[-1]),
        "total_return": float(total),
        "cagr": float(cagr),
        "annual_vol": float(vol),
        "sharpe_vs_tbill": float(sharpe),
        "sortino": float(sortino),
        "max_drawdown": max_dd,
        "max_dd_date": str(curve[dd_i][0]),
        "calmar": float(calmar),
        "best_day": float(r.max()) if len(r) else 0.0,
        "worst_day": float(r.min()) if len(r) else 0.0,
        "pct_up_days": float((r > 0).mean()) if len(r) else 0.0,
        "risk_free_used": rf,
        "excess_over_cash": float(total - rf * years),
        "sessions": n,
    }


def per_strategy(trades: list) -> pd.DataFrame:
    """Realized P&L attribution. Closing trades carry the realized number, so
    summing them by strategy gives a clean attribution, with fees separated."""
    agg = defaultdict(lambda: {"trades": 0, "closes": 0, "gross_pnl": 0.0,
                               "fees": 0.0, "wins": 0, "losses": 0,
                               "win_sum": 0.0, "loss_sum": 0.0, "equity": 0, "options": 0})
    for t in trades:
        a = agg[t.strategy]
        a["trades"] += 1
        a["fees"] += t.fees
        a[t.kind if t.kind in ("equity", "options") else "equity"] += 1 if t.kind == "equity" else 0
        if t.kind == "option":
            a["options"] += 1
        if abs(t.pnl) > 1e-9:
            a["closes"] += 1
            a["gross_pnl"] += t.pnl
            if t.pnl > 0:
                a["wins"] += 1
                a["win_sum"] += t.pnl
            else:
                a["losses"] += 1
                a["loss_sum"] += t.pnl
    rows = []
    for name, a in agg.items():
        wr = a["wins"] / a["closes"] if a["closes"] else 0.0
        pf = a["win_sum"] / abs(a["loss_sum"]) if a["loss_sum"] < -1e-9 else float("inf")
        rows.append({
            "strategy": name,
            "fills": a["trades"],
            "round_trips": a["closes"],
            "gross_pnl": round(a["gross_pnl"], 2),
            "fees": round(a["fees"], 2),
            "net_pnl": round(a["gross_pnl"] - a["fees"], 2),
            "win_rate": round(wr, 3),
            "profit_factor": round(pf, 2) if pf != float("inf") else None,
            "avg_win": round(a["win_sum"] / a["wins"], 2) if a["wins"] else 0.0,
            "avg_loss": round(a["loss_sum"] / a["losses"], 2) if a["losses"] else 0.0,
        })
    df = pd.DataFrame(rows)
    return df.sort_values("net_pnl", ascending=False) if len(df) else df


def monthly_table(curve: list[tuple]) -> pd.DataFrame:
    if len(curve) < 2:
        return pd.DataFrame()
    s = pd.Series([x[1] for x in curve], index=pd.to_datetime([x[0] for x in curve]))
    m = s.resample("ME").last()
    first = pd.Series([s.iloc[0]], index=[s.index[0] - pd.Timedelta(days=1)])
    m = pd.concat([first, m])
    ret = m.pct_change().dropna()
    return pd.DataFrame({"month": ret.index.strftime("%Y-%m"),
                         "return": ret.values.round(4),
                         "ending_equity": m.reindex(ret.index).values.round(2)})
