"""Main trading loop.

State machine per poll tick:
  FLAT  -> if daily limits allow and the last closed candle gives a signal,
           size the position and open it with SL/TP brackets.
  OPEN  -> wait until the position is gone (SL or TP filled), then cancel the
           leftover bracket order and record the realized PnL against the
           daily limits.
"""

import logging
import time
from datetime import datetime, timezone

import ccxt

from .exchange import FuturesExchange
from .risk import RiskManager
from . import strategy

log = logging.getLogger("bot.trader")


class Trader:
    def __init__(self, cfg):
        self.cfg = cfg
        self.exchange = FuturesExchange(cfg)
        self.risk = RiskManager(cfg)
        self.entry_time_ms: int | None = None
        self.last_signal_candle = None

    def run(self) -> None:
        log.info(
            "Bot started: %s %s | target +%.0f / max loss -%.0f USDT per day",
            self.cfg.symbol, self.cfg.timeframe,
            self.cfg.daily_profit_target, self.cfg.daily_max_loss,
        )
        while True:
            try:
                self.tick()
            except ccxt.NetworkError as e:
                log.warning("Network error, retrying next tick: %s", e)
            except ccxt.BaseError:
                log.exception("Exchange error")
            time.sleep(self.cfg.poll_seconds)

    def tick(self) -> None:
        position = self.exchange.current_position()
        if position is not None:
            return  # brackets manage the exit; nothing to do until it closes

        # If we had a position and now we're flat, it was closed by SL or TP.
        if self.entry_time_ms is not None:
            self._settle_closed_trade()

        ok, reason = self.risk.can_trade()
        if not ok:
            log.info(reason)
            return

        candles = self.exchange.fetch_closed_candles()
        signal = strategy.evaluate(candles, self.cfg)
        if signal is None:
            return

        # One entry per candle: don't re-fire on the same crossover.
        signal_candle = candles.index[-1]
        if signal_candle == self.last_signal_candle:
            return
        self.last_signal_candle = signal_candle

        equity = self.exchange.equity_usdt()
        amount = self.risk.position_size(equity, signal.entry_price, signal.stop_price)
        amount = self.exchange.amount_to_precision(amount)
        if amount <= 0:
            log.info("Signal %s but computed size is 0 — equity too small.", signal.side)
            return

        log.info(
            "Signal %s: entry ~%.2f SL %.2f TP %.2f size %s (equity %.2f USDT)",
            signal.side, signal.entry_price, signal.stop_price,
            signal.take_profit_price, amount, equity,
        )
        self.entry_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        self.exchange.open_bracket(
            signal.side, amount, signal.stop_price, signal.take_profit_price
        )

    def _settle_closed_trade(self) -> None:
        self.exchange.cancel_all_orders()  # remove the surviving bracket leg
        try:
            pnl = self.exchange.realized_pnl_since(self.entry_time_ms)
        except ccxt.BaseError:
            log.exception("Could not fetch realized PnL; recording 0.")
            pnl = 0.0
        self.risk.record_trade(pnl)
        log.info(
            "Trade closed: %+.2f USDT | today: %+.2f USDT over %d trade(s)",
            pnl, self.risk.state.realized_pnl, self.risk.state.trades,
        )
        self.entry_time_ms = None
