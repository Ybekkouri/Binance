"""Public market data client and the MarketSnapshot the strategy consumes.

Everything here uses public endpoints only (no API keys), so it is shared by
live, testnet, and paper modes. Each snapshot records when it was taken so
the trader can refuse to act on stale data.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import ccxt
import pandas as pd

log = logging.getLogger("bot.data")


@dataclass
class BookSummary:
    bid: float
    ask: float
    spread_pct: float
    bid_depth: float       # base-asset volume, top N levels
    ask_depth: float
    imbalance: float       # bid_depth / ask_depth


@dataclass
class MarketSnapshot:
    symbol: str
    candles: pd.DataFrame            # execution timeframe, closed candles
    trend_candles: pd.DataFrame      # higher timeframe, closed candles
    last_price: float = 0.0
    quote_volume_24h: float = 0.0
    funding_rate: Optional[float] = None      # current period rate
    oi_change_pct: Optional[float] = None     # open interest change, recent window
    book: Optional[BookSummary] = None
    btc_trend: int = 0               # +1/-1/0, higher-timeframe BTC direction
    long_short_ratio: Optional[float] = None      # global long/short accounts
    taker_buy_sell_ratio: Optional[float] = None  # aggressive buy vs sell volume
    taken_at: float = field(default_factory=time.time)

    def age_seconds(self) -> float:
        return time.time() - self.taken_at

    def summary(self) -> dict:
        """Compact form for the journal."""
        return {
            "symbol": self.symbol,
            "last_price": self.last_price,
            "quote_volume_24h": self.quote_volume_24h,
            "funding_rate": self.funding_rate,
            "oi_change_pct": self.oi_change_pct,
            "spread_pct": self.book.spread_pct if self.book else None,
            "book_imbalance": self.book.imbalance if self.book else None,
            "long_short_ratio": self.long_short_ratio,
            "taker_buy_sell_ratio": self.taker_buy_sell_ratio,
            "btc_trend": self.btc_trend,
            "candle_time": str(self.candles.index[-1]) if len(self.candles) else None,
        }


def _to_df(raw: list) -> pd.DataFrame:
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.set_index("timestamp").astype(float)


class MarketData:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = ccxt.binanceusdm({"enableRateLimit": True})
        self.client.load_markets()

    def candles(self, symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
        raw = self.client.fetch_ohlcv(symbol, timeframe, limit=limit)
        return _to_df(raw).iloc[:-1]  # drop the still-forming candle

    def book_summary(self, symbol: str, levels: int) -> BookSummary:
        book = self.client.fetch_order_book(symbol, limit=max(levels, 5))
        bids, asks = book["bids"][:levels], book["asks"][:levels]
        bid, ask = bids[0][0], asks[0][0]
        bid_depth = sum(v for _, v in bids)
        ask_depth = sum(v for _, v in asks)
        mid = (bid + ask) / 2
        return BookSummary(
            bid=bid, ask=ask,
            spread_pct=(ask - bid) / mid * 100,
            bid_depth=bid_depth, ask_depth=ask_depth,
            imbalance=bid_depth / ask_depth if ask_depth > 0 else float("inf"),
        )

    def funding_rate(self, symbol: str) -> Optional[float]:
        try:
            fr = self.client.fetch_funding_rate(symbol)
            return float(fr["fundingRate"])
        except ccxt.BaseError as e:
            log.debug("funding rate unavailable for %s: %s", symbol, e)
            return None

    def oi_change_pct(self, symbol: str, timeframe: str = "1h", points: int = 12
                      ) -> Optional[float]:
        """Open interest % change over the last `points` periods."""
        try:
            hist = self.client.fetch_open_interest_history(
                symbol, timeframe, limit=points
            )
            values = [float(h["openInterestAmount"] or h["openInterestValue"] or 0)
                      for h in hist]
            values = [v for v in values if v > 0]
            if len(values) < 2:
                return None
            return (values[-1] - values[0]) / values[0] * 100
        except ccxt.BaseError as e:
            log.debug("open interest unavailable for %s: %s", symbol, e)
            return None

    def long_short_ratio(self, symbol: str) -> Optional[float]:
        """Global long/short account ratio (Binance futures data endpoint).
        Recorded for research; >1 means more accounts are long."""
        try:
            rows = self.client.fapiDataGetGlobalLongShortAccountRatio({
                "symbol": self.client.market_id(symbol),
                "period": "1h", "limit": 1,
            })
            return float(rows[-1]["longShortRatio"]) if rows else None
        except (ccxt.BaseError, KeyError, ValueError) as e:
            log.debug("long/short ratio unavailable for %s: %s", symbol, e)
            return None

    def taker_buy_sell_ratio(self, symbol: str) -> Optional[float]:
        """Taker buy/sell volume ratio — aggressive flow direction.
        Recorded for research; >1 means aggressive buying dominates."""
        try:
            rows = self.client.fapiDataGetTakerlongshortRatio({
                "symbol": self.client.market_id(symbol),
                "period": "1h", "limit": 1,
            })
            return float(rows[-1]["buySellRatio"]) if rows else None
        except (ccxt.BaseError, KeyError, ValueError) as e:
            log.debug("taker ratio unavailable for %s: %s", symbol, e)
            return None

    def btc_trend(self) -> int:
        """Higher-timeframe BTC direction used as the market filter."""
        from . import indicators
        df = self.candles("BTC/USDT", self.cfg.trend_timeframe, limit=200)
        fast = indicators.ema(df["close"], self.cfg.strategy.trend_ema_fast)
        slow = indicators.ema(df["close"], self.cfg.strategy.trend_ema_slow)
        if fast.iloc[-1] > slow.iloc[-1]:
            return 1
        if fast.iloc[-1] < slow.iloc[-1]:
            return -1
        return 0

    def snapshot(self, symbol: str, btc_trend: int) -> MarketSnapshot:
        st = self.cfg.strategy
        ticker = self.client.fetch_ticker(symbol)
        return MarketSnapshot(
            symbol=symbol,
            candles=self.candles(symbol, self.cfg.timeframe, limit=300),
            trend_candles=self.candles(symbol, self.cfg.trend_timeframe, limit=200),
            last_price=float(ticker["last"]),
            quote_volume_24h=float(ticker.get("quoteVolume") or 0),
            funding_rate=self.funding_rate(symbol),
            oi_change_pct=self.oi_change_pct(symbol),
            book=self.book_summary(symbol, st.book_depth_levels),
            btc_trend=btc_trend,
            long_short_ratio=self.long_short_ratio(symbol),
            taker_buy_sell_ratio=self.taker_buy_sell_ratio(symbol),
        )
