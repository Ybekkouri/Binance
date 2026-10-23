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
import math
import logging

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
    df = _to_df(rows)
    # drop the still-forming candle — the live engine never sees it either
    return df.iloc[:-1] if len(df) > 1 else df


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
        # mirror RiskEngine: fee-noise breakeven exits are not "losses"
        noise = self.equity_day_start * 0.0005
        if pnl > 0:
            self.consecutive_losses = 0
        elif pnl < -noise:
            self.consecutive_losses += 1


# ------------------------------------------------------------ engine
def run_backtest(cfg, df: pd.DataFrame, trend_df: pd.DataFrame,
                 btc_trend_lookup, funding: pd.Series, start_equity: float,
                 min_notional: float = 0.0, amount_step: float = 0.0):
    """Bar-by-bar simulation mirroring the live engine: same strategy,
    sizing, risk limits, shock guard, exchange-minimum guard, brackets,
    breakeven, trailing (incl. minimum step) and invalidation exits.

    Intra-bar order is unknowable from OHLC, so conflicts resolve the
    conservative way: the stop is checked first, and a stop gapped through
    at the open fills at the (worse) open."""
    st, exi = cfg.strategy, cfg.exits
    if len(df) < 2 or len(trend_df) < 2:
        return pd.DataFrame(), pd.Series(dtype=float)
    fee_pct = cfg.taker_fee_pct / 100
    slip_pct = cfg.slippage_pct / 100
    base_delta = df.index[1] - df.index[0]
    trend_delta = trend_df.index[1] - trend_df.index[0]
    trend_close_times = trend_df.index + trend_delta
    atr_full = atr_series(df, st.atr_period)
    # funding aligned to bars once, instead of Series.asof per bar
    funding_at_bar = (funding.reindex(df.index, method="ffill")
                      if len(funding) else pd.Series(index=df.index, dtype=float))
    bar_hours = base_delta.total_seconds() / 3600
    # 24h rolling quote volume, approximated from base candles
    bars_per_day = max(1, int(24 / bar_hours))
    quote_vol = (df["volume"] * df["close"]).rolling(bars_per_day).sum()
    # shock guard: same rule as live (bot/events.py), cooldown in bars
    cooldown_bars = max(1, int(cfg.events.shock_cooldown_minutes * 60
                               / base_delta.total_seconds()))
    shock_until_bar = -1

    equity = start_equity
    sim = SimRisk(cfg, equity)
    position = None
    trades = []
    day_pnl: dict = {}

    warmup = max(st.ema_slow, st.swing_lookback, st.breakout_lookback,
                 st.volume_ma, st.atr_period) + 2

    def snapshot_at(i, bar_close_time):
        n_trend = trend_close_times.searchsorted(bar_close_time, side="right")
        if n_trend < st.trend_ema_slow + 2:
            return None
        bar = df.iloc[i]
        fr = funding_at_bar.iloc[i]
        qv = quote_vol.iloc[i]
        return MarketSnapshot(
            symbol=cfg.symbols[0],
            candles=df.iloc[max(0, i - WINDOW):i + 1],
            trend_candles=trend_df.iloc[max(0, n_trend - 200):n_trend],
            last_price=float(bar["close"]),
            quote_volume_24h=float(qv) if qv == qv else 0.0,
            funding_rate=float(fr) if fr == fr else None,
            oi_change_pct=None,
            book=None,
            btc_trend=btc_trend_lookup(bar_close_time),
        )

    for i in range(warmup, len(df)):
        bar = df.iloc[i]
        ts = df.index[i]
        bar_close_time = ts + base_delta
        sim.roll(ts, equity)
        day = ts.strftime("%Y-%m-%d")
        day_pnl.setdefault(day, 0.0)
        a = float(atr_full.iloc[i])

        # shock detection runs on every bar, like EventGuard.observe()
        prev_atr = float(atr_full.iloc[i - 1])
        if prev_atr > 0 and (float(bar["high"]) - float(bar["low"])) \
                >= cfg.events.shock_atr_mult * prev_atr:
            shock_until_bar = i + cooldown_bars

        # ---- manage open position ----
        if position is not None:
            p = position
            sign = 1 if p["side"] == "long" else -1
            notional = p["contracts"] * float(bar["close"])
            fr = float(funding_at_bar.iloc[i]) if len(funding) else 0.0
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
            op = float(bar["open"])
            hit = lambda level, above: hi >= level if above else lo <= level
            if hit(p["stop"], above=p["side"] == "short"):
                # gapped through at the open -> fill at the worse open
                stop_fill = min(p["stop"], op) if sign > 0 else max(p["stop"], op)
                fill(stop_fill, p["contracts"], "stop_loss")
            else:
                if not p["tp1_filled"] and hit(p["tp1"], above=p["side"] == "long"):
                    fill(p["tp1"], p["tp1_amount"], "take_profit_1")
                    if position:
                        p["tp1_filled"] = True
                # like live, TP2 is re-checked right after a TP1 partial
                if position is not None and hit(p["tp2"], above=p["side"] == "long"):
                    fill(p["tp2"], p["contracts"], "take_profit_2")

            if position is not None:
                close = float(bar["close"])
                # invalidation: a confident opposing signal exits at the close
                if exi.exit_on_opposing_signal:
                    snap = snapshot_at(i, bar_close_time)
                    if snap is not None:
                        d = strategy.decide(snap, cfg)
                        if d.direction != NO_TRADE and \
                                (d.direction == LONG) != (sign > 0):
                            fill(close, p["contracts"], "invalidation")
                            continue
                # breakeven / trailing, same rules as bot/manager.py
                r_gain = sign * (close - p["ref_entry"]) / p["risk_dist"]
                new_stop = None
                if not p["breakeven_done"] and r_gain >= exi.breakeven_at_r:
                    new_stop = p["ref_entry"]
                    p["breakeven_done"] = True
                if r_gain >= exi.tp1_r:
                    trail = close - sign * a * exi.trail_atr_mult
                    better = trail > p["stop"] if sign > 0 else trail < p["stop"]
                    min_step = p["stop"] * exi.trail_min_step_pct / 100
                    if better and abs(trail - p["stop"]) >= min_step:
                        new_stop = trail if new_stop is None else \
                            (max(new_stop, trail) if sign > 0 else min(new_stop, trail))
                if new_stop is not None:
                    p["stop"] = new_stop
            continue  # never evaluate a new entry on a bar we were in a trade

        # ---- evaluate entry ----
        if not sim.can_open(equity):
            continue
        if i <= shock_until_bar:
            continue                      # shock cooldown, as live
        snap = snapshot_at(i, bar_close_time)
        if snap is None:
            continue
        decision = strategy.decide(snap, cfg)
        if decision.direction == NO_TRADE:
            continue
        if snap.quote_volume_24h < cfg.risk.min_quote_volume_24h:
            continue

        amount = size_position(cfg, equity, decision.entry_price,
                               decision.stop_loss, [])
        if amount_step > 0:
            amount = math.floor(amount / amount_step) * amount_step
        if amount <= 0:
            continue
        tp1_amount = amount * exi.tp1_fraction
        if amount_step > 0 and exi.tp1_fraction > 0:
            tp1_amount = math.floor(tp1_amount / amount_step) * amount_step
            if tp1_amount <= 0:
                continue
        smallest = (tp1_amount if exi.tp1_fraction > 0 else amount) \
            * decision.entry_price
        if min_notional and smallest < min_notional:
            continue                      # exchange minimum, as live
        sign = 1 if decision.direction == LONG else -1
        entry_fill = decision.entry_price * (1 + sign * slip_pct)
        fee = entry_fill * amount * fee_pct
        equity -= fee
        sim.trades_today += 1
        position = {
            "side": "long" if sign > 0 else "short",
            "contracts": amount,
            "tp1_amount": tp1_amount,
            "entry": entry_fill,
            # live manages breakeven/trailing off the signal price
            "ref_entry": decision.entry_price,
            "stop": decision.stop_loss,
            "tp1": decision.take_profit_1,
            "tp2": decision.take_profit_2,
            "risk_dist": abs(decision.entry_price - decision.stop_loss),
            "tp1_filled": exi.tp1_fraction <= 0,
            "breakeven_done": False,
            "fees": fee, "funding": 0.0, "realized": -fee,
        }

    trades_df = pd.DataFrame(trades)
    daily = pd.Series(day_pnl).sort_index()
    return trades_df, daily


# ------------------------------------------------------------ reusable runner
def load_data(cfg, symbol: str, days: int, client=None) -> dict:
    """Fetch everything a backtest of `symbol` needs, once. Reusing the same
    dict for several configs guarantees they are compared on identical data."""
    client = client or ccxt.binanceusdm({"enableRateLimit": True})
    df = fetch_history(client, symbol, cfg.timeframe, days)
    data = {
        "symbol": symbol,
        "df": df,
        "trend_df": resample(df, cfg.trend_timeframe),
        "funding": fetch_funding(client, symbol, days),
        "btc_sig": None,
        "min_notional": 0.0,
        "amount_step": 0.0,
    }
    if symbol != "BTC/USDT":
        btc = resample(fetch_history(client, "BTC/USDT", cfg.timeframe, days),
                       cfg.trend_timeframe)
        fast = ema(btc["close"], cfg.strategy.trend_ema_fast)
        slow = ema(btc["close"], cfg.strategy.trend_ema_slow)
        sig = (fast > slow).astype(int) - (fast < slow).astype(int)
        # index by the bar's CLOSE time: a lookup at time t may only see
        # higher-timeframe bars that had finished by t (no lookahead)
        if len(sig) > 1:
            sig.index = sig.index + (sig.index[1] - sig.index[0])
        data["btc_sig"] = sig
    try:
        client.load_markets()
        market = client.market(symbol)
        data["min_notional"] = float(
            ((market.get("limits") or {}).get("cost") or {}).get("min") or 0)
        step = (market.get("precision") or {}).get("amount")
        data["amount_step"] = float(step) if step and float(step) < 1 else 0.0
    except Exception as e:      # noqa: BLE001 — limits are a refinement
        log.warning("market limits unavailable: %s", e)
    return data


def run(cfg, symbol: str, days: int, equity: float, client=None, data=None):
    """Backtest one symbol. Returns (trades, daily, metrics). Pass `data`
    (from load_data) to reuse already-fetched candles."""
    cfg.symbols = [symbol]
    if data is None:
        data = load_data(cfg, symbol, days, client)
    sig = data["btc_sig"]

    if cfg.strategy.btc_filter and sig is not None:
        def btc_trend_lookup(t):
            v = sig.asof(t)
            return int(v) if v == v else 0
    else:
        def btc_trend_lookup(t):
            return 0

    trades, daily = run_backtest(cfg, data["df"], data["trend_df"],
                                 btc_trend_lookup, data["funding"], equity,
                                 min_notional=data["min_notional"],
                                 amount_step=data["amount_step"])
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
