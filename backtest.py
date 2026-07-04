"""Backtest the bot's strategy on historical Binance futures data.

Runs the exact same signal logic as the live bot (bot/strategy.py), with the
same ATR stop/take-profit brackets and %-risk position sizing, and reports
daily PnL so you can judge the "X USDT per day" goal against real history.

Usage:
    python backtest.py --days 90 --equity 5000
    python backtest.py --symbol ETH/USDT --timeframe 15m --days 30

No API keys needed — public market data only.
"""

import argparse
import logging

import ccxt
import pandas as pd

from bot.config import Config
from bot import strategy

TAKER_FEE = 0.0005  # 0.05% per side, standard Binance futures taker fee


def make_cfg(args) -> Config:
    return Config(
        testnet=True, api_key="", api_secret="",
        symbol=args.symbol, timeframe=args.timeframe,
        leverage=args.leverage, margin_mode="isolated",
        ema_fast=20, ema_slow=50,
        rsi_period=14, rsi_overbought=70, rsi_oversold=30,
        atr_period=14, stop_atr_mult=1.5, take_profit_atr_mult=2.25,
        risk_per_trade_pct=args.risk, daily_profit_target_pct=args.target_pct,
        daily_max_loss_pct=args.max_loss_pct, max_position_notional_mult=args.leverage,
        poll_seconds=0, state_file="",
    )


def fetch_history(symbol: str, timeframe: str, days: int) -> pd.DataFrame:
    client = ccxt.binanceusdm({"enableRateLimit": True})
    tf_ms = client.parse_timeframe(timeframe) * 1000
    since = client.milliseconds() - days * 86_400_000
    rows = []
    while since < client.milliseconds():
        batch = client.fetch_ohlcv(symbol, timeframe, since=since, limit=1500)
        if not batch:
            break
        rows.extend(batch)
        since = batch[-1][0] + tf_ms
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.set_index("timestamp")


def run_backtest(df: pd.DataFrame, cfg: Config, start_equity: float):
    """Bar-by-bar simulation. Conservative assumption: if a bar touches both
    the stop and the take-profit, the stop is filled first.

    Daily limits are percentages of the equity at the start of each day,
    mirroring the live RiskManager."""
    ind = strategy.add_indicators(df, cfg)
    equity = start_equity
    trades = []
    position = None  # dict(side, entry, stop, tp, amount)
    day_pnl: dict = {}
    day_equity_start: dict = {}

    warmup = max(cfg.ema_slow, cfg.rsi_period, cfg.atr_period) + 2
    for i in range(warmup, len(ind)):
        bar = ind.iloc[i]
        day = bar.name.strftime("%Y-%m-%d")
        day_pnl.setdefault(day, 0.0)
        day_equity_start.setdefault(day, equity)

        if position is not None:
            hit_stop = (bar["low"] <= position["stop"] if position["side"] == "long"
                        else bar["high"] >= position["stop"])
            hit_tp = (bar["high"] >= position["tp"] if position["side"] == "long"
                      else bar["low"] <= position["tp"])
            exit_price = position["stop"] if hit_stop else position["tp"] if hit_tp else None
            if exit_price is not None:
                direction = 1 if position["side"] == "long" else -1
                gross = direction * (exit_price - position["entry"]) * position["amount"]
                fees = (position["entry"] + exit_price) * position["amount"] * TAKER_FEE
                pnl = gross - fees
                equity += pnl
                day_pnl[day] += pnl
                trades.append({
                    "exit_time": bar.name, "side": position["side"],
                    "pnl": pnl, "equity": equity,
                })
                position = None

        day_target = day_equity_start[day] * cfg.daily_profit_target_pct / 100.0
        day_max_loss = day_equity_start[day] * cfg.daily_max_loss_pct / 100.0
        if position is None and day_pnl[day] < day_target and day_pnl[day] > -day_max_loss:
            window = df.iloc[: i + 1]
            sig = strategy.evaluate(window, cfg)
            if sig is not None:
                stop_dist = abs(sig.entry_price - sig.stop_price)
                amount = (equity * cfg.risk_per_trade_pct / 100.0) / stop_dist
                amount = min(
                    amount,
                    equity * cfg.max_position_notional_mult / sig.entry_price,
                    equity * cfg.leverage * 0.95 / sig.entry_price,
                )
                if amount > 0:
                    position = {
                        "side": sig.side, "entry": sig.entry_price,
                        "stop": sig.stop_price, "tp": sig.take_profit_price,
                        "amount": amount,
                    }

    daily = pd.DataFrame({
        "pnl": pd.Series(day_pnl),
        "target": pd.Series(day_equity_start) * cfg.daily_profit_target_pct / 100.0,
    })
    return pd.DataFrame(trades), daily


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--equity", type=float, default=5000)
    parser.add_argument("--risk", type=float, default=1.0, help="%% risk per trade")
    parser.add_argument("--leverage", type=int, default=3)
    parser.add_argument("--target-pct", type=float, default=2.0,
                        help="daily profit target as %% of equity")
    parser.add_argument("--max-loss-pct", type=float, default=1.5,
                        help="daily max loss as %% of equity")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = make_cfg(args)

    print(f"Fetching {args.days} days of {args.symbol} {args.timeframe} candles...")
    df = fetch_history(args.symbol, args.timeframe, args.days)
    print(f"Got {len(df)} candles. Running backtest with {args.equity:.0f} USDT...")

    trades, daily = run_backtest(df, cfg, args.equity)
    if trades.empty:
        print("No trades were generated over this period.")
        return

    wins = trades[trades.pnl > 0]
    print("\n===== RESULTS =====")
    print(f"Trades:            {len(trades)}")
    print(f"Win rate:          {len(wins) / len(trades) * 100:.1f}%")
    print(f"Total PnL:         {trades.pnl.sum():+.2f} USDT")
    print(f"Final equity:      {trades.equity.iloc[-1]:.2f} USDT")
    print(f"Best day:          {daily.pnl.max():+.2f} USDT")
    print(f"Worst day:         {daily.pnl.min():+.2f} USDT")
    print(f"Average day:       {daily.pnl.mean():+.2f} USDT")
    print(f"Target-hit days:   {(daily.pnl >= daily.target).sum()} / {len(daily)} "
          f"(target = +{args.target_pct:.1f}% of that day's equity)")
    print(f"Losing days:       {(daily.pnl < 0).sum()} / {len(daily)}")


if __name__ == "__main__":
    main()
