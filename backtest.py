"""Backtest the engine on historical Binance futures data.

Runs the SAME strategy code as the live bot (bot/strategy.py) bar by bar,
with the same sizing, ATR brackets, partial take-profits, breakeven move,
ATR trailing, daily/weekly risk limits, taker fees, slippage, and funding
costs. Order book and open interest factors vote neutral in backtests
(no historical data), which only makes the backtest MORE conservative
than live.

Usage:
    python backtest.py --days 90 --equity 1000
    python backtest.py --symbol ETH/USDT --days 60 --config config.yaml

Strategy comparison: run once per config file and compare the reports.
No API keys needed — public market data only.
"""

import argparse
import logging
from datetime import datetime

import ccxt
import pandas as pd

from bot import metrics, strategy
from bot.config import load_config
from bot.decision import LONG, NO_TRADE
from bot.indicators import atr as atr_series, ema
from bot.market_data import MarketSnapshot, _to_df
from bot.risk import size_position

log = logging.getLogger("backtest")

WINDOW = 300  # bars of context per decision, mirroring the live fetch limit


# ------------------------------------------------------------ data
def fetch_history(client, symbol: str, timeframe: str, days: int) -> pd.DataFrame:
    tf_ms = client.parse_timeframe(timeframe) * 1000
    since = client.milliseconds() - days * 86_400_000
    rows = []
    while since < client.milliseconds():
        batch = client.fetch_ohlcv(symbol, timeframe, since=since, limit=1500)
        if not batch:
            break
        rows.extend(batch)
        since = batch[-1][0] + tf_ms
    return _to_df(rows)


def fetch_funding(client, symbol: str, days: int) -> pd.Series:
    since = client.milliseconds() - days * 86_400_000
    rows, out = [], {}
    try:
        rows = client.fetch_funding_rate_history(symbol, since=since, limit=1000)
    except ccxt.BaseError as e:
        log.warning("funding history unavailable: %s", e)
    for r in rows:
        out[pd.Timestamp(r["timestamp"], unit="ms", tz="UTC")] = float(r["fundingRate"])
    return pd.Series(out).sort_index()


def resample(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    rule = timeframe.replace("m", "min").replace("h", "h").replace("d", "D")
    return df.resample(rule).agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()


# ------------------------------------------------------------ sim risk
class SimRisk:
    """Mirror of the live RiskEngine counters on simulated time."""

    def __init__(self, cfg, equity: float):
        self.cfg = cfg
        self.day = None
        self.week = None
        self.daily_pnl = 0.0
        self.trades_today = 0
        self.consecutive_losses = 0
        self.equity_day_start = equity
        self.equity_week_start = equity

    def roll(self, ts: pd.Timestamp, equity: float):
        day = ts.strftime("%Y-%m-%d")
        week = f"{ts.isocalendar().year}-W{ts.isocalendar().week:02d}"
        if day != self.day:
            self.day, self.daily_pnl, self.trades_today = day, 0.0, 0
            self.equity_day_start = equity
            self.consecutive_losses = 0  # cooldown ends with the day
        if week != self.week:
            self.week, self.equity_week_start = week, equity

    def can_open(self, equity: float) -> bool:
        rk = self.cfg.risk
        if self.daily_pnl <= -self.equity_day_start * rk.daily_max_loss_pct / 100:
            return False
        if rk.daily_profit_target_pct > 0 and \
                self.daily_pnl >= self.equity_day_start * rk.daily_profit_target_pct / 100:
            return False
        if self.equity_week_start > 0 and \
                (self.equity_week_start - equity) / self.equity_week_start * 100 \
                >= rk.weekly_max_drawdown_pct:
            return False
        if self.consecutive_losses >= rk.max_consecutive_losses:
            return False
        if self.trades_today >= rk.max_trades_per_day:
            return False
        return True

    def on_close(self, pnl: float):
        self.daily_pnl += pnl
        self.consecutive_losses = 0 if pnl > 0 else self.consecutive_losses + 1


# ------------------------------------------------------------ engine
def run_backtest(cfg, df: pd.DataFrame, trend_df: pd.DataFrame,
                 btc_trend_lookup, funding: pd.Series, start_equity: float):
    st, exi = cfg.strategy, cfg.exits
    fee_pct = cfg.taker_fee_pct / 100
    slip_pct = cfg.slippage_pct / 100
    base_delta = df.index[1] - df.index[0]
    trend_delta = trend_df.index[1] - trend_df.index[0]
    trend_close_times = trend_df.index + trend_delta
    atr_full = atr_series(df, st.atr_period)
    bar_hours = base_delta.total_seconds() / 3600
    # 24h rolling quote volume, approximated from base candles
    bars_per_day = max(1, int(24 / bar_hours))
    quote_vol = (df["volume"] * df["close"]).rolling(bars_per_day).sum()

    equity = start_equity
    sim = SimRisk(cfg, equity)
    position = None
    trades = []
    day_pnl: dict = {}

    warmup = max(st.ema_slow, st.swing_lookback, st.breakout_lookback,
                 st.volume_ma, st.atr_period) + 2

    for i in range(warmup, len(df)):
        bar = df.iloc[i]
        ts = df.index[i]
        bar_close_time = ts + base_delta
        sim.roll(ts, equity)
        day = ts.strftime("%Y-%m-%d")
        day_pnl.setdefault(day, 0.0)
        a = float(atr_full.iloc[i])

        # ---- manage open position ----
        if position is not None:
            p = position
            sign = 1 if p["side"] == "long" else -1
            notional = p["contracts"] * float(bar["close"])
            fr = float(funding.asof(ts)) if len(funding) else 0.0
            if fr == fr:  # not NaN
                cost = sign * fr * notional * bar_hours / 8
                p["funding"] += cost
                equity -= cost

            def fill(price, amount, reason):
                nonlocal equity, position
                slipped = price * (1 - sign * slip_pct)
                gross = sign * (slipped - p["entry"]) * amount
                fee = slipped * amount * fee_pct
                equity += gross - fee
                p["realized"] += gross - fee
                p["fees"] += fee
                p["contracts"] -= amount
                if p["contracts"] <= 1e-12:
                    net = p["realized"] - p["funding"]
                    sim.on_close(net)
                    day_pnl[day] += net
                    trades.append({
                        "exit_time": ts, "side": p["side"], "pnl": net,
                        "fees": p["fees"], "funding": p["funding"],
                        "equity": equity, "reason": reason,
                    })
                    position = None

            hi, lo = float(bar["high"]), float(bar["low"])
            hit = lambda level, above: hi >= level if above else lo <= level
            if hit(p["stop"], above=p["side"] == "short"):
                fill(p["stop"], p["contracts"], "stop_loss")          # SL first: conservative
            elif not p["tp1_filled"] and hit(p["tp1"], above=p["side"] == "long"):
                fill(p["tp1"], p["tp1_amount"], "take_profit_1")
                if position:
                    p["tp1_filled"] = True
            elif hit(p["tp2"], above=p["side"] == "long"):
                fill(p["tp2"], p["contracts"], "take_profit_2")

            if position is not None:
                close = float(bar["close"])
                r_gain = sign * (close - p["entry"]) / p["risk_dist"]
                if not p["breakeven_done"] and r_gain >= exi.breakeven_at_r:
                    p["stop"] = p["entry"] if sign > 0 else p["entry"]
                    p["stop"] = p["entry"]
                    p["breakeven_done"] = True
                if r_gain >= exi.tp1_r:
                    trail = close - sign * a * exi.trail_atr_mult
                    if (trail > p["stop"]) == (sign > 0) and trail != p["stop"]:
                        p["stop"] = trail
            continue  # never evaluate a new entry on a bar we were in a trade

        # ---- evaluate entry ----
        if not sim.can_open(equity):
            continue
        n_trend = trend_close_times.searchsorted(bar_close_time, side="right")
        if n_trend < st.trend_ema_slow + 2:
            continue
        snap = MarketSnapshot(
            symbol=cfg.symbols[0],
            candles=df.iloc[max(0, i - WINDOW):i + 1],
            trend_candles=trend_df.iloc[max(0, n_trend - 200):n_trend],
            last_price=float(bar["close"]),
            quote_volume_24h=float(quote_vol.iloc[i]) if quote_vol.iloc[i] == quote_vol.iloc[i] else 0.0,
            funding_rate=float(funding.asof(ts)) if len(funding) and funding.asof(ts) == funding.asof(ts) else None,
            oi_change_pct=None,
            book=None,
            btc_trend=btc_trend_lookup(ts),
        )
        decision = strategy.decide(snap, cfg)
        if decision.direction == NO_TRADE:
            continue
        if snap.quote_volume_24h < cfg.risk.min_quote_volume_24h:
            continue

        amount = size_position(cfg, equity, decision.entry_price,
                               decision.stop_loss, [])
        if amount <= 0:
            continue
        sign = 1 if decision.direction == LONG else -1
        entry_fill = decision.entry_price * (1 + sign * slip_pct)
        fee = entry_fill * amount * fee_pct
        equity -= fee
        sim.trades_today += 1
        position = {
            "side": "long" if sign > 0 else "short",
            "contracts": amount,
            "tp1_amount": amount * exi.tp1_fraction,
            "entry": entry_fill,
            "stop": decision.stop_loss,
            "tp1": decision.take_profit_1,
            "tp2": decision.take_profit_2,
            "risk_dist": abs(entry_fill - decision.stop_loss),
            "tp1_filled": exi.tp1_fraction <= 0,
            "breakeven_done": False,
            "fees": fee, "funding": 0.0, "realized": -fee,
        }

    trades_df = pd.DataFrame(trades)
    daily = pd.Series(day_pnl).sort_index()
    return trades_df, daily


# ------------------------------------------------------------ reusable runner
def run(cfg, symbol: str, days: int, equity: float, client=None):
    """Fetch data and backtest one symbol. Returns (trades, daily, metrics).
    Used by the CLI below and by research.py for config comparison."""
    cfg.symbols = [symbol]
    client = client or ccxt.binanceusdm({"enableRateLimit": True})
    df = fetch_history(client, symbol, cfg.timeframe, days)
    trend_df = resample(df, cfg.trend_timeframe)
    funding = fetch_funding(client, symbol, days)

    if cfg.strategy.btc_filter and symbol != "BTC/USDT":
        btc = resample(fetch_history(client, "BTC/USDT", cfg.timeframe, days),
                       cfg.trend_timeframe)
        fast = ema(btc["close"], cfg.strategy.trend_ema_fast)
        slow = ema(btc["close"], cfg.strategy.trend_ema_slow)
        sig = (fast > slow).astype(int) - (fast < slow).astype(int)

        def btc_trend_lookup(ts):
            v = sig.asof(ts)
            return int(v) if v == v else 0
    else:
        def btc_trend_lookup(ts):
            return 0

    trades, daily = run_backtest(cfg, df, trend_df, btc_trend_lookup,
                                 funding, equity)
    # include flat days so Sharpe/Sortino aren't overstated
    if len(daily):
        all_days = pd.date_range(daily.index[0], daily.index[-1], freq="D")
        daily = daily.reindex([d.strftime("%Y-%m-%d") for d in all_days],
                              fill_value=0.0)
    m = metrics.compute(trades, daily, equity)
    return trades, daily, m


# ------------------------------------------------------------ cli
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--symbol", default=None, help="default: first configured symbol")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--equity", type=float, default=1000)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = load_config(args.config, require_keys=False)
    symbol = args.symbol or cfg.symbols[0]

    print(f"Fetching {args.days} days of {symbol} {cfg.timeframe} data...")
    trades, daily, m = run(cfg, symbol, args.days, args.equity)
    print()
    print(metrics.format_report(m, daily))
    if m.get("trades", 0):
        by_reason = trades.groupby("reason").pnl.agg(["count", "sum"])
        print("\nExits by reason:")
        print(by_reason.to_string())


if __name__ == "__main__":
    main()
