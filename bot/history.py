"""Deep history: fetch, cache, and dissect a pair's entire trading life.

Before trading a pair regularly, know its character across full market
cycles. This module pulls the MAXIMUM daily history Binance has (spot
reaches back furthest — BTC/USDT to 2017; USDS-M futures exist since late
2019), caches it in the datastore so re-runs are instant and incremental,
and produces a structural report:

  coverage    — what actually exists (nobody has 10y of a pair listed in 2020)
  phases      — bull / bear segmentation with dates, durations, returns
  yearly      — return, volatility, max drawdown, best/worst day per year
  regimes     — trending/ranging/volatile day classification, and the
                transition matrix: does today's regime predict tomorrow's?
                (this is the empirical justification for trend-following)
  seasonality — monthly and weekday tendencies, significance-tested
  volatility  — ATR% by year, extreme-day frequencies, clustering
  drawdowns   — the five worst, with depth, length, and recovery time

All statistics come with the same honesty rules as the trade analysis:
small effects are reported as noise unless they clear significance.
"""

import logging
import math

import pandas as pd

from .analysis import two_prop_z, Z95

log = logging.getLogger("bot.history")

DAY_MS = 86_400_000


# ---------------------------------------------------------------- fetch/cache
CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles_daily (
    symbol TEXT, source TEXT, ts INTEGER,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, source, ts)
);
"""


def _ensure_cache(conn) -> None:
    conn.executescript(CACHE_SCHEMA)
    conn.commit()


def fetch_daily_history(conn, symbol: str, source: str = "spot",
                        client=None) -> pd.DataFrame:
    """Fetch all available daily candles for `symbol`, incrementally cached.

    source: 'spot' (longest history) or 'futures' (since ~2019).
    """
    import ccxt

    _ensure_cache(conn)
    if client is None:
        client = (ccxt.binance if source == "spot" else ccxt.binanceusdm)(
            {"enableRateLimit": True})

    last = conn.execute(
        "SELECT MAX(ts) FROM candles_daily WHERE symbol=? AND source=?",
        (symbol, source)).fetchone()[0]
    since = (last + DAY_MS) if last else 1483228800000   # 2017-01-01
    fetched = 0
    while True:
        batch = client.fetch_ohlcv(symbol, "1d", since=since, limit=1000)
        if not batch:
            break
        conn.executemany(
            """INSERT OR REPLACE INTO candles_daily VALUES (?,?,?,?,?,?,?,?)""",
            [(symbol, source, r[0], r[1], r[2], r[3], r[4], r[5]) for r in batch],
        )
        conn.commit()
        fetched += len(batch)
        since = batch[-1][0] + DAY_MS
        if len(batch) < 2:
            break
    log.info("fetched %d new daily candles for %s (%s)", fetched, symbol, source)
    return load_daily(conn, symbol, source)


def load_daily(conn, symbol: str, source: str = "spot") -> pd.DataFrame:
    _ensure_cache(conn)
    rows = conn.execute(
        """SELECT ts, open, high, low, close, volume FROM candles_daily
           WHERE symbol=? AND source=? ORDER BY ts""",
        (symbol, source)).fetchall()
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts").astype(float)
    # drop today's still-forming candle
    return df.iloc[:-1] if len(df) > 1 else df


# ---------------------------------------------------------------- analysis
def _returns(df: pd.DataFrame) -> pd.Series:
    return df["close"].pct_change().dropna()


def coverage(df: pd.DataFrame, symbol: str, source: str) -> str:
    years = (df.index[-1] - df.index[0]).days / 365.25
    return "\n".join([
        f"Symbol:   {symbol}  (source: Binance {source})",
        f"Span:     {df.index[0].date()} -> {df.index[-1].date()} "
        f"({len(df)} days, {years:.1f} years — the maximum Binance has)",
        f"First close: {df['close'].iloc[0]:,.2f}   Last close: "
        f"{df['close'].iloc[-1]:,.2f}   "
        f"({(df['close'].iloc[-1]/df['close'].iloc[0]-1)*100:+,.0f}%)",
    ])


def phases(df: pd.DataFrame, ma_days: int = 200, min_len: int = 60) -> str:
    """Bull/bear segmentation: above/below the 200-day MA, short flips merged."""
    if len(df) < ma_days + min_len:
        return "Not enough history for phase segmentation."
    ma = df["close"].rolling(ma_days).mean()
    state = (df["close"] > ma).astype(int).iloc[ma_days:]
    closes = df["close"].iloc[ma_days:]

    # runs, merging runs shorter than min_len into the previous phase
    segments = []
    start = 0
    vals = state.values
    for i in range(1, len(vals) + 1):
        if i == len(vals) or vals[i] != vals[start]:
            segments.append([start, i, vals[start]])
            start = i
    merged = [segments[0]]
    for seg in segments[1:]:
        if seg[1] - seg[0] < min_len:
            merged[-1][1] = seg[1]          # absorb the short flip
        elif seg[2] == merged[-1][2]:
            merged[-1][1] = seg[1]
        else:
            merged.append(seg)

    lines = [f"Market phases (close vs {ma_days}-day MA, flips < {min_len}d merged):",
             f"{'phase':<8}{'start':>12}{'end':>12}{'days':>7}{'return':>10}"]
    lines.append("-" * len(lines[1]))
    for s, e, v in merged:
        seg_close = closes.iloc[s:e]
        ret = (seg_close.iloc[-1] / seg_close.iloc[0] - 1) * 100
        label = "BULL" if v else "BEAR"
        lines.append(f"{label:<8}{str(seg_close.index[0].date()):>12}"
                     f"{str(seg_close.index[-1].date()):>12}"
                     f"{len(seg_close):>7}{ret:>+9.0f}%")
    bull_days = sum(e - s for s, e, v in merged if v)
    total = sum(e - s for s, e, _ in merged)
    lines.append(f"\nTime in bull phases: {bull_days/total*100:.0f}% | "
                 f"bear: {(total-bull_days)/total*100:.0f}%")
    return "\n".join(lines)


def yearly(df: pd.DataFrame) -> str:
    lines = [f"{'year':<6}{'return':>9}{'ann.vol':>9}{'max DD':>9}"
             f"{'best day':>10}{'worst day':>11}{'days':>6}"]
    lines.append("-" * len(lines[0]))
    for year, g in df.groupby(df.index.year):
        r = _returns(g)
        if len(r) < 10:
            continue
        eq = g["close"]
        dd = ((eq - eq.cummax()) / eq.cummax()).min() * 100
        lines.append(
            f"{year:<6}{(eq.iloc[-1]/eq.iloc[0]-1)*100:>+8.0f}%"
            f"{r.std()*math.sqrt(365)*100:>8.0f}%{dd:>+8.0f}%"
            f"{r.max()*100:>+9.1f}%{r.min()*100:>+10.1f}%{len(g):>6}")
    return "Yearly breakdown:\n" + "\n".join(lines)


def classify_days(df: pd.DataFrame) -> pd.Series:
    """Daily regime labels using the engine's logic transplanted to 1d."""
    ema20 = df["close"].ewm(span=20, adjust=False).mean()
    ema50 = df["close"].ewm(span=50, adjust=False).mean()
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    atr_pct = atr / df["close"] * 100
    sep = (ema20 - ema50) / atr.replace(0, pd.NA)

    vol_hi = atr_pct.quantile(0.90)
    labels = pd.Series("ranging", index=df.index)
    labels[sep > 0.5] = "trending_up"
    labels[sep < -0.5] = "trending_down"
    labels[atr_pct > vol_hi] = "volatile"
    return labels.iloc[50:]     # drop EMA warmup


def regimes(df: pd.DataFrame) -> str:
    labels = classify_days(df)
    counts = labels.value_counts()
    lines = ["Daily regime distribution:"]
    for k, v in counts.items():
        lines.append(f"  {k:<14}{v:>6} days ({v/len(labels)*100:.0f}%)")

    # transition matrix: P(tomorrow | today)
    nxt = labels.shift(-1).dropna()
    cur = labels.iloc[:-1]
    states = sorted(counts.index)
    lines.append("\nRegime persistence — P(tomorrow's regime | today's):")
    label = "today / next"
    header = f"{label:<16}" + "".join(f"{s:>15}" for s in states)
    lines.append(header)
    lines.append("-" * len(header))
    for s in states:
        mask = cur == s
        row = f"{s:<16}"
        for s2 in states:
            p = (nxt[mask.values] == s2).mean() if mask.sum() else 0.0
            row += f"{p*100:>14.0f}%"
        lines.append(row)
    stick = (nxt.values == cur.values).mean()
    lines.append(f"\nOverall regime stickiness: {stick*100:.0f}% "
                 f"(random would be ~{100/len(states):.0f}%) — "
                 + ("regimes persist; trend-following has structural support."
                    if stick > 1.5 / len(states) else
                    "regimes barely persist; be skeptical of trend-following here."))
    return "\n".join(lines)


def seasonality(df: pd.DataFrame) -> str:
    r = _returns(df)
    lines = ["Monthly seasonality (avg daily return, % positive days):"]
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    base_up = (r > 0).sum()
    for m in range(1, 13):
        g = r[r.index.month == m]
        if len(g) < 20:
            continue
        z = two_prop_z((g > 0).sum(), len(g), base_up, len(r))
        star = " *" if abs(z) >= Z95 else ""
        lines.append(f"  {months[m-1]}: avg {g.mean()*100:+.3f}%/day, "
                     f"{(g>0).mean()*100:.0f}% up days (n={len(g)}){star}")
    lines.append("\nWeekday effect (avg daily return, % positive):")
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for d in range(7):
        g = r[r.index.dayofweek == d]
        if len(g) < 20:
            continue
        z = two_prop_z((g > 0).sum(), len(g), base_up, len(r))
        star = " *" if abs(z) >= Z95 else ""
        lines.append(f"  {days[d]}: avg {g.mean()*100:+.3f}%/day, "
                     f"{(g>0).mean()*100:.0f}% up days (n={len(g)}){star}")
    lines.append("\n'*' = up-day rate differs from baseline at 95%. Unstarred "
                 "rows are noise — crypto seasonality is mostly folklore.")
    return "\n".join(lines)


def volatility(df: pd.DataFrame) -> str:
    r = _returns(df)
    abs_r = r.abs()
    lines = ["Volatility structure:"]
    lines.append(f"  Daily return std (all-time): {r.std()*100:.2f}% "
                 f"(annualized {r.std()*math.sqrt(365)*100:.0f}%)")
    for th in (0.03, 0.05, 0.10):
        n = (abs_r >= th).sum()
        per_year = n / (len(r) / 365)
        lines.append(f"  Days with |move| >= {th*100:.0f}%: {n} "
                     f"(~{per_year:.1f} per year)")
    clustering = abs_r.autocorr(lag=1)
    lines.append(f"  Volatility clustering (|r| lag-1 autocorr): {clustering:.2f} — "
                 + ("volatile days cluster; expect storms in groups."
                    if clustering > 0.1 else "little clustering."))
    worst = r.nsmallest(5)
    best = r.nlargest(5)
    lines.append("  Worst days: " + ", ".join(
        f"{d.date()} {v*100:+.0f}%" for d, v in worst.items()))
    lines.append("  Best days:  " + ", ".join(
        f"{d.date()} {v*100:+.0f}%" for d, v in best.items()))
    lines.append("\n  Sizing reality check: a 3-sigma daily move is "
                 f"{r.std()*3*100:.1f}%. With {3}x leverage that is "
                 f"{r.std()*3*300:.0f}% of position margin — this is why the "
                 "engine caps leverage and risks 0.5% per trade.")
    return "\n".join(lines)


def drawdowns(df: pd.DataFrame, top: int = 5) -> str:
    eq = df["close"]
    peak = eq.cummax()
    dd = (eq - peak) / peak
    lines = [f"Top {top} drawdowns:",
             f"{'depth':>8}{'peak':>13}{'trough':>13}{'days down':>11}{'recovered':>12}"]
    lines.append("-" * len(lines[1]))
    used = pd.Series(False, index=eq.index)
    for _ in range(top):
        remaining = dd[~used]
        if remaining.empty or remaining.min() >= -0.05:
            break
        trough_i = remaining.idxmin()
        depth = dd[trough_i]
        peak_i = eq[:trough_i][eq[:trough_i] == peak[trough_i]].index[-1]
        after = eq[trough_i:]
        rec = after[after >= peak[trough_i]]
        rec_str = str(rec.index[0].date()) if len(rec) else "not yet"
        lines.append(f"{depth*100:>+7.0f}%{str(peak_i.date()):>13}"
                     f"{str(trough_i.date()):>13}"
                     f"{(trough_i-peak_i).days:>11}{rec_str:>12}")
        used[peak_i:rec.index[0] if len(rec) else eq.index[-1]] = True
    lines.append("\nSurviving these is the entire point of the daily loss cap "
                 "and weekly drawdown halt.")
    return "\n".join(lines)


# ---------------------------------------------------------------- report
def full_history_report(df: pd.DataFrame, symbol: str, source: str) -> str:
    header = (f"{'='*66}\nDEEP HISTORY ANALYSIS — {symbol}\n{'='*66}")
    if df.empty or len(df) < 260:
        return header + f"\nOnly {len(df)} days of data — not enough to analyze."
    sections = [
        ("COVERAGE", lambda: coverage(df, symbol, source)),
        ("MARKET PHASES", lambda: phases(df)),
        ("YEARLY", lambda: yearly(df)),
        ("REGIMES", lambda: regimes(df)),
        ("SEASONALITY", lambda: seasonality(df)),
        ("VOLATILITY", lambda: volatility(df)),
        ("DRAWDOWNS", lambda: drawdowns(df)),
    ]
    parts = [header]
    for title, fn in sections:
        parts.append(f"\n--- {title} " + "-" * (60 - len(title)))
        parts.append(fn())
    parts.append(
        "\nHow to use this: the regime persistence and phase tables tell you "
        "whether trend-following fits this pair at all; volatility and "
        "drawdowns calibrate leverage and risk caps; seasonality is context, "
        "not a signal. For strategy-level validation on recent data, run "
        "backtest.py; this report is about the pair's character.")
    return "\n".join(parts)
