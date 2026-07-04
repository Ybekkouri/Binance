"""Trend-following strategy: EMA crossover with an RSI filter.

Signals are evaluated on *closed* candles only, so the bot never acts on a
candle that is still forming.

- LONG when the fast EMA crosses above the slow EMA and RSI is not overbought.
- SHORT when the fast EMA crosses below the slow EMA and RSI is not oversold.
- Stop-loss and take-profit distances are derived from ATR so they adapt to
  current volatility.
"""

from dataclasses import dataclass
from typing import Optional

import pandas as pd


@dataclass
class Signal:
    side: str          # "long" or "short"
    entry_price: float
    stop_price: float
    take_profit_price: float


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, pd.NA)
    return (100 - 100 / (1 + rs)).fillna(100.0)


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def add_indicators(df: pd.DataFrame, cfg) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = ema(df["close"], cfg.ema_fast)
    df["ema_slow"] = ema(df["close"], cfg.ema_slow)
    df["rsi"] = rsi(df["close"], cfg.rsi_period)
    df["atr"] = atr(df, cfg.atr_period)
    return df


def evaluate(df: pd.DataFrame, cfg) -> Optional[Signal]:
    """Return a Signal if the last closed candle produced an entry, else None.

    `df` must be OHLCV with columns open/high/low/close/volume, oldest first,
    containing only closed candles.
    """
    min_bars = max(cfg.ema_slow, cfg.rsi_period, cfg.atr_period) + 2
    if len(df) < min_bars:
        return None

    df = add_indicators(df, cfg)
    last, prev = df.iloc[-1], df.iloc[-2]

    crossed_up = prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]
    crossed_down = prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]

    price = float(last["close"])
    stop_dist = float(last["atr"]) * cfg.stop_atr_mult
    tp_dist = float(last["atr"]) * cfg.take_profit_atr_mult
    if stop_dist <= 0:
        return None

    if crossed_up and last["rsi"] < cfg.rsi_overbought:
        return Signal("long", price, price - stop_dist, price + tp_dist)
    if crossed_down and last["rsi"] > cfg.rsi_oversold:
        return Signal("short", price, price + stop_dist, price - tp_dist)
    return None
