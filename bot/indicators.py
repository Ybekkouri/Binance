"""Indicator library: pure pandas, no external TA dependencies.

All functions take OHLCV DataFrames (columns open/high/low/close/volume,
oldest first) containing closed candles only.
"""

import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, pd.NA)
    out = 100 - 100 / (1 + rs)
    # zero average loss is only "RSI 100" when there were actual gains;
    # a dead-flat series (gain == loss == 0) is neutral, not overbought
    out[(loss == 0) & (gain > 0)] = 100.0
    return out.fillna(50.0)


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


def swing_points(df: pd.DataFrame, window: int) -> tuple[pd.Series, pd.Series]:
    """(swing_highs, swing_lows): price series indexed at confirmed pivots.

    A swing high is a bar whose high exceeds the `window` bars on each side
    (strictly greater than one side to avoid double-counting flat tops).
    """
    highs, lows = df["high"], df["low"]
    is_high = pd.Series(True, index=df.index)
    is_low = pd.Series(True, index=df.index)
    for k in range(1, window + 1):
        is_high &= (highs > highs.shift(k)) & (highs >= highs.shift(-k))
        is_low &= (lows < lows.shift(k)) & (lows <= lows.shift(-k))
    return highs[is_high], lows[is_low]


def market_structure(df: pd.DataFrame, lookback: int, window: int) -> int:
    """+1 for bullish structure (higher highs AND higher lows over the last
    two swings), -1 for bearish, 0 for unclear."""
    recent = df.iloc[-lookback:]
    sh, sl = swing_points(recent, window)
    if len(sh) < 2 or len(sl) < 2:
        return 0
    hh = sh.iloc[-1] > sh.iloc[-2]
    hl = sl.iloc[-1] > sl.iloc[-2]
    lh = sh.iloc[-1] < sh.iloc[-2]
    ll = sl.iloc[-1] < sl.iloc[-2]
    if hh and hl:
        return 1
    if lh and ll:
        return -1
    return 0


def nearest_levels(df: pd.DataFrame, price: float, lookback: int,
                   window: int) -> tuple[float, float]:
    """(nearest support below price, nearest resistance above price) from
    swing points; returns (nan, nan) components when none exist."""
    recent = df.iloc[-lookback:]
    sh, sl = swing_points(recent, window)
    levels = pd.concat([sh, sl]).sort_values()
    below = levels[levels < price]
    above = levels[levels > price]
    support = float(below.iloc[-1]) if len(below) else float("nan")
    resistance = float(above.iloc[0]) if len(above) else float("nan")
    return support, resistance


def classify_market(df: pd.DataFrame, cfg) -> str:
    """Coarse regime label for the decision record."""
    fast = ema(df["close"], cfg.ema_fast)
    slow = ema(df["close"], cfg.ema_slow)
    a = atr(df, cfg.atr_period)
    atr_pct = float(a.iloc[-1]) / float(df["close"].iloc[-1]) * 100
    if atr_pct > cfg.max_atr_pct:
        return "volatile"
    sep = (float(fast.iloc[-1]) - float(slow.iloc[-1])) / float(a.iloc[-1] or 1)
    if sep > 0.5:
        return "trending_up"
    if sep < -0.5:
        return "trending_down"
    return "ranging"
