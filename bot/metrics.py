"""Performance metrics for backtests and trade-history analysis."""

import math

import pandas as pd

TRADING_DAYS_PER_YEAR = 365  # crypto trades every day


def compute(trades: pd.DataFrame, daily_pnl: pd.Series,
            start_equity: float) -> dict:
    """`trades` needs columns: pnl, fees, funding, equity (after each trade).
    `daily_pnl` is net PnL per calendar day (0 for flat days included)."""
    if trades.empty:
        return {"trades": 0}

    wins = trades[trades.pnl > 0]
    losses = trades[trades.pnl <= 0]
    gross_win = wins.pnl.sum()
    gross_loss = abs(losses.pnl.sum())

    equity_curve = pd.concat(
        [pd.Series([start_equity]), trades.equity]
    ).reset_index(drop=True)
    running_max = equity_curve.cummax()
    drawdown = (equity_curve - running_max) / running_max
    max_dd_pct = abs(drawdown.min()) * 100

    daily_returns = daily_pnl / start_equity
    mean, std = daily_returns.mean(), daily_returns.std()
    # downside deviation over ALL days (the Sortino convention), not the
    # std of losing days only — which is zero when losses are identical
    downside = math.sqrt(float((daily_returns.clip(upper=0) ** 2).mean()))
    ann = math.sqrt(TRADING_DAYS_PER_YEAR)
    sharpe = (mean / std * ann) if std and std > 0 else 0.0
    sortino = (mean / downside * ann) if downside > 0 else 0.0

    return {
        "trades": len(trades),
        "win_rate_pct": len(wins) / len(trades) * 100,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else float("inf"),
        "avg_win": wins.pnl.mean() if len(wins) else 0.0,
        "avg_loss": losses.pnl.mean() if len(losses) else 0.0,
        "expectancy": trades.pnl.mean(),
        "total_pnl": trades.pnl.sum(),
        "total_fees": trades.fees.sum(),
        "total_funding": trades.funding.sum(),
        "max_drawdown_pct": max_dd_pct,
        "sharpe": sharpe,
        "sortino": sortino,
        "final_equity": float(trades.equity.iloc[-1]),
        "return_pct": (float(trades.equity.iloc[-1]) - start_equity)
                      / start_equity * 100,
    }


def format_report(m: dict, daily_pnl: pd.Series = None) -> str:
    if m.get("trades", 0) == 0:
        return "No trades were generated over this period."
    lines = [
        "===== PERFORMANCE REPORT =====",
        f"Trades:            {m['trades']}",
        f"Win rate:          {m['win_rate_pct']:.1f}%",
        f"Profit factor:     {m['profit_factor']:.2f}",
        f"Average win:       {m['avg_win']:+.2f} USDT",
        f"Average loss:      {m['avg_loss']:+.2f} USDT",
        f"Expectancy/trade:  {m['expectancy']:+.2f} USDT",
        f"Total PnL:         {m['total_pnl']:+.2f} USDT ({m['return_pct']:+.2f}%)",
        f"Fees paid:         {m['total_fees']:.2f} USDT",
        f"Funding paid:      {m['total_funding']:+.2f} USDT",
        f"Max drawdown:      {m['max_drawdown_pct']:.2f}%",
        f"Sharpe ratio:      {m['sharpe']:.2f}",
        f"Sortino ratio:     {m['sortino']:.2f}",
        f"Final equity:      {m['final_equity']:.2f} USDT",
    ]
    if daily_pnl is not None and len(daily_pnl):
        lines += [
            f"Best day:          {daily_pnl.max():+.2f} USDT",
            f"Worst day:         {daily_pnl.min():+.2f} USDT",
            f"Average day:       {daily_pnl.mean():+.2f} USDT",
            f"Losing days:       {(daily_pnl < 0).sum()} / {len(daily_pnl)}",
        ]
    return "\n".join(lines)
