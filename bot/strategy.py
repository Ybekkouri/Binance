"""Strategy engine: multi-factor confluence with NO_TRADE as the default.

Twelve independent factors each cast a weighted vote (+1 bullish, -1 bearish,
0 neutral). A trade is proposed only when:

  1. no hard gate vetoes (volatility band, BTC filter, funding extremes),
  2. weighted confidence >= min_confidence,
  3. at least min_aligned_factors independent factors agree, and
  4. the blended risk/reward across TP1/TP2 beats the configured minimum.

Anything else returns a NO_TRADE decision with the reasons recorded, so every
"why didn't it trade?" question is answerable from the journal.
"""

import math
from datetime import datetime, timezone

import pandas as pd

from . import indicators as ta
from .decision import LONG, SHORT, NO_TRADE, FactorVote, TradeDecision, blended_rr
from .market_data import MarketSnapshot


def _no_trade(symbol: str, reasons: list, votes: list = None,
              condition: str = "", confidence: float = 0.0) -> TradeDecision:
    return TradeDecision(
        symbol=symbol, direction=NO_TRADE,
        timestamp=datetime.now(timezone.utc).isoformat(),
        market_condition=condition, confidence=confidence,
        reasons=reasons, votes=votes or [],
    )


def collect_votes(snap: MarketSnapshot, cfg) -> list[FactorVote]:
    st = cfg.strategy
    w = st.weights
    df, htf = snap.candles, snap.trend_candles
    close = float(df["close"].iloc[-1])
    a = float(ta.atr(df, st.atr_period).iloc[-1])
    votes: list[FactorVote] = []

    def add(name, vote, detail):
        votes.append(FactorVote(name, int(vote), float(w.get(name, 0)), detail))

    # 1. Higher-timeframe trend
    hf = ta.ema(htf["close"], st.trend_ema_fast).iloc[-1]
    hs = ta.ema(htf["close"], st.trend_ema_slow).iloc[-1]
    add("trend_htf", 1 if hf > hs else -1 if hf < hs else 0,
        f"{cfg.trend_timeframe} EMA{st.trend_ema_fast}={hf:.2f} vs EMA{st.trend_ema_slow}={hs:.2f}")

    # 2. Execution-timeframe trend
    lf = ta.ema(df["close"], st.ema_fast).iloc[-1]
    ls = ta.ema(df["close"], st.ema_slow).iloc[-1]
    add("trend_ltf", 1 if lf > ls else -1 if lf < ls else 0,
        f"{cfg.timeframe} EMA{st.ema_fast}={lf:.2f} vs EMA{st.ema_slow}={ls:.2f}")

    # 3. Market structure (higher highs/lows vs lower highs/lows)
    ms = ta.market_structure(df, st.swing_lookback, st.swing_window)
    add("structure", ms,
        {1: "higher highs and higher lows",
         -1: "lower highs and lower lows"}.get(ms, "no clear structure"))

    # 4. Momentum (RSI regime)
    r = float(ta.rsi(df["close"], st.rsi_period).iloc[-1])
    add("momentum", 1 if r > 55 else -1 if r < 45 else 0, f"RSI={r:.1f}")

    # 5. Breakout confirmation (close beyond the prior N-bar range)
    n = st.breakout_lookback
    range_high = float(df["high"].iloc[-n - 1:-1].max())
    range_low = float(df["low"].iloc[-n - 1:-1].min())
    bo = 1 if close > range_high else -1 if close < range_low else 0
    add("breakout", bo,
        f"close={close:.2f} vs {n}-bar range [{range_low:.2f}, {range_high:.2f}]")

    # 6. Volume expansion behind the last candle's direction
    vol = float(df["volume"].iloc[-1])
    vol_ma = float(df["volume"].rolling(st.volume_ma).mean().iloc[-1])
    candle_dir = 1 if df["close"].iloc[-1] > df["open"].iloc[-1] else -1
    expanded = vol_ma > 0 and vol >= st.volume_expansion * vol_ma
    add("volume", candle_dir if expanded else 0,
        f"volume={vol:.0f} vs {st.volume_expansion}x MA{st.volume_ma}={vol_ma:.0f}")

    # 7. Room to the nearest S/R level relative to the stop distance:
    #    entering long right under resistance (or short on support) is a bad trade.
    support, resistance = ta.nearest_levels(df, close, st.swing_lookback, st.swing_window)
    stop_dist = a * cfg.exits.stop_atr_mult
    room_up = (resistance - close) if not math.isnan(resistance) else float("inf")
    room_down = (close - support) if not math.isnan(support) else float("inf")
    sr = 1 if room_up >= stop_dist and room_down < room_up else \
        -1 if room_down >= stop_dist and room_up < room_down else 0
    add("sr_room", sr,
        f"support={support:.2f} resistance={resistance:.2f} stop_dist={stop_dist:.2f}")

    # 8. Mean-reversion guard: vote AGAINST the direction price is overextended in.
    ext_atr = (close - float(ls)) / a if a > 0 else 0
    mr = -1 if ext_atr > st.extension_atr_max else 1 if ext_atr < -st.extension_atr_max else 0
    add("mean_reversion", mr, f"extension from EMA{st.ema_slow}: {ext_atr:+.2f} ATR")

    # 9. Funding rate: heavily positive funding = crowded longs (contrarian bearish tilt)
    if snap.funding_rate is None:
        add("funding", 0, "funding rate unavailable")
    else:
        fr = snap.funding_rate
        f_vote = -1 if fr > st.max_funding_against / 2 else \
            1 if fr < -st.max_funding_against / 2 else 0
        add("funding", f_vote, f"funding={fr:+.5f}")

    # 10. Open interest confirming the price move
    if snap.oi_change_pct is None:
        add("open_interest", 0, "open interest unavailable")
    else:
        price_dir = 1 if close > float(df["close"].iloc[-12]) else -1
        oi_vote = price_dir if snap.oi_change_pct > 1.0 else 0
        add("open_interest", oi_vote,
            f"OI change {snap.oi_change_pct:+.1f}% with price {'up' if price_dir > 0 else 'down'}")

    # 11. Order book imbalance
    if snap.book is None:
        add("book_imbalance", 0, "order book unavailable")
    else:
        imb = snap.book.imbalance
        b_vote = 1 if imb >= st.book_imbalance_ratio else \
            -1 if imb <= 1 / st.book_imbalance_ratio else 0
        add("book_imbalance", b_vote, f"bid/ask depth ratio={imb:.2f}")

    return votes


def hard_gates(snap: MarketSnapshot, direction: str, cfg) -> list[str]:
    """Return veto reasons; empty list means all gates pass."""
    st = cfg.strategy
    df = snap.candles
    close = float(df["close"].iloc[-1])
    a = float(ta.atr(df, st.atr_period).iloc[-1])
    vetoes = []

    atr_pct = a / close * 100
    if atr_pct < st.min_atr_pct:
        vetoes.append(f"volatility too low (ATR {atr_pct:.2f}% < {st.min_atr_pct}%)")
    if atr_pct > st.max_atr_pct:
        vetoes.append(f"volatility too high (ATR {atr_pct:.2f}% > {st.max_atr_pct}%)")

    if st.btc_filter and snap.symbol != "BTC/USDT":
        if direction == LONG and snap.btc_trend < 0:
            vetoes.append("BTC filter: BTC higher-timeframe trend is down")
        if direction == SHORT and snap.btc_trend > 0:
            vetoes.append("BTC filter: BTC higher-timeframe trend is up")

    if snap.funding_rate is not None:
        if direction == LONG and snap.funding_rate > st.max_funding_against:
            vetoes.append(f"funding {snap.funding_rate:+.5f} too expensive for longs")
        if direction == SHORT and snap.funding_rate < -st.max_funding_against:
            vetoes.append(f"funding {snap.funding_rate:+.5f} too expensive for shorts")
    return vetoes


def decide(snap: MarketSnapshot, cfg) -> TradeDecision:
    st = cfg.strategy
    df = snap.candles
    min_bars = max(st.ema_slow, st.swing_lookback, st.breakout_lookback,
                   st.volume_ma, st.atr_period) + 2
    if len(df) < min_bars or len(snap.trend_candles) < st.trend_ema_slow + 2:
        return _no_trade(snap.symbol, ["not enough candle history"])

    votes = collect_votes(snap, cfg)
    condition = ta.classify_market(df, st)

    total_weight = sum(v.weight for v in votes)
    score = sum(v.vote * v.weight for v in votes) / total_weight if total_weight else 0
    confidence = abs(score)
    direction = LONG if score > 0 else SHORT if score < 0 else NO_TRADE
    aligned = [v for v in votes if v.vote != 0 and v.weight > 0
               and (v.vote > 0) == (direction == LONG)]

    if direction == NO_TRADE or confidence < st.min_confidence:
        return _no_trade(
            snap.symbol,
            [f"confidence {confidence:.2f} below minimum {st.min_confidence:.2f}"],
            votes, condition, confidence,
        )
    if len(aligned) < st.min_aligned_factors:
        return _no_trade(
            snap.symbol,
            [f"only {len(aligned)} factors aligned; need {st.min_aligned_factors}"],
            votes, condition, confidence,
        )

    vetoes = hard_gates(snap, direction, cfg)
    if vetoes:
        return _no_trade(snap.symbol, ["hard gate: " + v for v in vetoes],
                         votes, condition, confidence)

    # Build the trade levels
    close = float(df["close"].iloc[-1])
    a = float(ta.atr(df, st.atr_period).iloc[-1])
    stop_dist = a * cfg.exits.stop_atr_mult
    sign = 1 if direction == LONG else -1
    entry = close
    stop = entry - sign * stop_dist
    tp1 = entry + sign * stop_dist * cfg.exits.tp1_r
    tp2 = entry + sign * stop_dist * cfg.exits.tp2_r
    rr = blended_rr(entry, stop, tp1, tp2, cfg.exits.tp1_fraction)

    if rr < cfg.risk.min_risk_reward:
        return _no_trade(
            snap.symbol,
            [f"risk/reward {rr:.2f} below minimum {cfg.risk.min_risk_reward:.2f}"],
            votes, condition, confidence,
        )

    against = [v for v in votes if v.vote != 0 and v.weight > 0
               and (v.vote > 0) != (direction == LONG)]
    return TradeDecision(
        symbol=snap.symbol,
        direction=direction,
        timestamp=datetime.now(timezone.utc).isoformat(),
        entry_price=entry,
        stop_loss=stop,
        take_profit_1=tp1,
        take_profit_2=tp2,
        leverage=cfg.leverage,
        risk_reward=rr,
        confidence=confidence,
        market_condition=condition,
        reasons=[f"{v.name}: {v.detail}" for v in aligned],
        risks=[f"{v.name} against: {v.detail}" for v in against] or
              ["no factor currently against the trade"],
        invalidation=(
            f"close beyond {stop:.2f} (stop) or opposing signal with "
            f"confidence >= {st.min_confidence:.2f}"
        ),
        votes=votes,
    )
