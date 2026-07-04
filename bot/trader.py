"""Orchestrator: the main loop tying data, strategy, risk, execution and
management together.

Per tick, per symbol:
  1. refresh market snapshot (staleness-guarded),
  2. if a position is open -> hand to TradeManager (with the fresh decision
     for invalidation checks),
  3. if flat -> evaluate strategy; on LONG/SHORT run every risk and market
     check; only if ALL pass, size and execute with brackets.

The kill switch (a file named KILL by default, or Ctrl-C) cancels orders,
optionally flattens everything, and stops the bot.
"""

import logging
import os
import time

import ccxt

from . import strategy
from .decision import LONG, NO_TRADE

log = logging.getLogger("bot.trader")


class Trader:
    def __init__(self, cfg, market, broker, risk, manager, journal):
        self.cfg = cfg
        self.market = market
        self.broker = broker
        self.risk = risk
        self.manager = manager
        self.journal = journal
        self.last_entry_candle: dict = {}   # symbol -> candle ts of last entry
        self.data_failures = 0

    # ------------------------------------------------------------ loop
    def run(self) -> None:
        for symbol in self.cfg.symbols:
            self.broker.setup_symbol(symbol)
        log.info(
            "Engine started (%s): %s on %s/%s | risk %.2f%%/trade, "
            "daily -%.1f%%/+%.1f%%, weekly DD %.1f%%",
            self.cfg.mode, ", ".join(self.cfg.symbols), self.cfg.timeframe,
            self.cfg.trend_timeframe, self.cfg.risk.risk_per_trade_pct,
            self.cfg.risk.daily_max_loss_pct, self.cfg.risk.daily_profit_target_pct,
            self.cfg.risk.weekly_max_drawdown_pct,
        )
        try:
            while True:
                if self._kill_requested():
                    self._shutdown("kill switch file detected")
                    return
                self.tick()
                time.sleep(self.cfg.poll_seconds)
        except KeyboardInterrupt:
            self._shutdown("keyboard interrupt")

    def tick(self) -> None:
        try:
            btc_trend = self.market.btc_trend()
            self.data_failures = 0
        except ccxt.BaseError as e:
            self._data_failure(f"BTC trend fetch failed: {e}")
            return

        for symbol in self.cfg.symbols:
            try:
                self._process_symbol(symbol, btc_trend)
            except ccxt.NetworkError as e:
                self._data_failure(f"{symbol}: network error: {e}")
            except ccxt.BaseError:
                log.exception("Exchange error on %s", symbol)
                self.journal.error("tick", f"exchange error on {symbol}")

    # ------------------------------------------------------------ symbol
    def _process_symbol(self, symbol: str, btc_trend: int) -> None:
        snap = self.market.snapshot(symbol, btc_trend)
        if hasattr(self.broker, "mark"):            # paper: trigger simulated fills
            self.broker.mark(symbol, snap.last_price)

        if snap.age_seconds() > self.cfg.max_stale_data_seconds:
            self.journal.error("data", f"stale snapshot for {symbol}")
            return

        decision = strategy.decide(snap, self.cfg)
        has_position = any(
            p["symbol"] == symbol for p in self.broker.open_positions()
        ) or symbol in self.risk.state.managed

        if has_position:
            self.manager.manage(snap, opposing_decision=decision)
            return

        # Journal every decision — including NO_TRADE — for auditability.
        self.journal.decision(decision, snap.summary())
        if decision.direction == NO_TRADE:
            return

        # New candle guard: one attempt per signal candle.
        candle_ts = str(snap.candles.index[-1])
        if self.last_entry_candle.get(symbol) == candle_ts:
            return

        self._try_enter(decision, snap, candle_ts)

    def _try_enter(self, decision, snap, candle_ts: str) -> None:
        symbol = decision.symbol
        equity = self.broker.equity_usdt()
        positions = self.broker.open_positions()

        amount = self.risk.position_size(
            equity, decision.entry_price, decision.stop_loss, positions
        )
        amount = self.broker.amount_to_precision(symbol, amount)
        if amount <= 0:
            self.journal.risk_block(symbol, ["position size rounds to zero"])
            return
        notional = amount * decision.entry_price

        fails = self.risk.account_checks(equity, positions, notional, symbol)
        fails += self.risk.market_checks(snap, amount)
        if decision.leverage > self.cfg.risk.max_leverage:
            fails.append("leverage above maximum")
        margin_needed = notional / max(self.cfg.leverage, 1)
        if margin_needed > equity * 0.95:
            fails.append("insufficient margin for position")
        if fails:
            log.info("NO TRADE %s: %s", symbol, "; ".join(fails))
            self.journal.risk_block(symbol, fails)
            return

        decision.position_size = amount
        tp1_amount = self.broker.amount_to_precision(
            symbol, amount * self.cfg.exits.tp1_fraction
        )
        self.last_entry_candle[symbol] = candle_ts

        log.info(
            "ENTER %s %s: size %s @ ~%.2f, SL %.2f, TP1 %.2f, TP2 %.2f, "
            "RR %.2f, confidence %.2f",
            decision.direction, symbol, amount, decision.entry_price,
            decision.stop_loss, decision.take_profit_1, decision.take_profit_2,
            decision.risk_reward, decision.confidence,
        )
        request = {
            "side": "long" if decision.direction == LONG else "short",
            "amount": amount, "stop": decision.stop_loss,
            "tp1": decision.take_profit_1, "tp1_amount": tp1_amount,
            "tp2": decision.take_profit_2,
        }
        try:
            response = self.broker.enter(symbol, request["side"], amount,
                                         decision.stop_loss,
                                         decision.take_profit_1, tp1_amount,
                                         decision.take_profit_2)
            self.journal.order("entry_bracket", symbol, request,
                               {"id": response.get("id")})
            self.manager.on_entry(decision, amount, equity)
        except ccxt.BaseError as e:
            self.journal.error("execution", f"{symbol}: {e}")
            raise

    # ------------------------------------------------------------ safety
    def _kill_requested(self) -> bool:
        return os.path.isfile(self.cfg.kill_file)

    def _shutdown(self, why: str) -> None:
        log.warning("Shutdown: %s", why)
        self.journal.write("shutdown", reason=why,
                           close_positions=self.cfg.close_positions_on_kill)
        try:
            for symbol in self.cfg.symbols:
                self.broker.cancel_all(symbol)
            if self.cfg.close_positions_on_kill:
                self.broker.close_all()
                log.warning("All positions flattened.")
        except ccxt.BaseError:
            log.exception("Error during emergency shutdown — check the exchange!")
        if os.path.isfile(self.cfg.kill_file):
            os.remove(self.cfg.kill_file)

    def _data_failure(self, message: str) -> None:
        self.data_failures += 1
        log.warning("Data failure #%d: %s", self.data_failures, message)
        if self.data_failures >= 3:
            self.journal.error("data_outage", message)
            log.warning(
                "Repeated data failures — no new entries until data recovers. "
                "Open positions remain protected by exchange-side brackets."
            )
