"""Orchestrator: one or two complete execution tracks over shared snapshots.

  real track   — the strict engine on the configured broker (live / testnet /
                 paper). The only track that can ever touch money.
  shadow track — the learning twin: the IDENTICAL pipeline — risk engine,
                 position sizing, fills with fees and slippage, breakeven,
                 ATR trailing, invalidation exits, settlement — running at
                 relaxed thresholds on a virtual paper account against the
                 same real market prices. It feeds the research dataset and
                 can never influence the real track.

Per tick, per symbol: one market snapshot is taken and both tracks process
it independently, each with its own broker, risk state, and trade manager.

The kill switch (a file named KILL by default, or Ctrl-C) cancels orders,
optionally flattens the REAL account, and stops. Shadow positions are
virtual; they simply persist in their state file.
"""

import logging
import os
import time

import ccxt

from . import strategy
from .decision import LONG, NO_TRADE

log = logging.getLogger("bot.trader")


class Track:
    """One complete pipeline: thresholds -> risk checks -> execution ->
    management -> settlement. Instantiated once for real, once for shadow."""

    def __init__(self, name, cfg, broker, risk, manager, journal,
                 datastore=None, min_confidence=None, min_aligned_factors=None):
        self.name = name
        self.is_shadow = name != "real"
        self.cfg = cfg
        self.broker = broker
        self.risk = risk
        self.manager = manager
        self.journal = journal
        self.datastore = datastore
        self.min_confidence = min_confidence
        self.min_aligned_factors = min_aligned_factors
        self.last_eval_candle: dict = {}

    def process(self, snap, snapshot_id=None) -> None:
        symbol = snap.symbol
        if hasattr(self.broker, "mark"):        # paper fills trigger on price
            self.broker.mark(symbol, snap.last_price)

        decision = strategy.decide(
            snap, self.cfg,
            min_confidence=self.min_confidence,
            min_aligned_factors=self.min_aligned_factors,
        )

        has_position = any(
            p["symbol"] == symbol for p in self.broker.open_positions()
        ) or symbol in self.risk.state.managed
        if has_position:
            self.manager.manage(snap, opposing_decision=decision)
            return

        # Signals come from closed candles: evaluate/record each candle once.
        candle_ts = str(snap.candles.index[-1]) if len(snap.candles) else ""
        if self.last_eval_candle.get(symbol) == candle_ts:
            return
        self.last_eval_candle[symbol] = candle_ts

        decision_id = None
        if not self.is_shadow:
            # The real track journals everything, including NO_TRADE.
            self.journal.decision(decision, snap.summary())
            if self.datastore is not None and snapshot_id is not None:
                decision_id = self.datastore.record_decision(
                    decision, snapshot_id)
        elif (decision.direction != NO_TRADE and self.datastore is not None
              and snapshot_id is not None):
            # The shadow track only records decisions that fire.
            decision_id = self.datastore.record_decision(
                decision, snapshot_id, shadow=True)

        if decision.direction != NO_TRADE:
            self._try_enter(decision, snap, decision_id)

    def _try_enter(self, decision, snap, decision_id=None) -> None:
        symbol = decision.symbol
        equity = self.broker.equity_usdt()
        positions = self.broker.open_positions()

        amount = self.risk.position_size(
            equity, decision.entry_price, decision.stop_loss, positions
        )
        amount = self.broker.amount_to_precision(symbol, amount)
        if amount <= 0:
            self.journal.risk_block(symbol, ["position size rounds to zero"],
                                    track=self.name)
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
            log.info("[%s] NO TRADE %s: %s", self.name, symbol, "; ".join(fails))
            self.journal.risk_block(symbol, fails, track=self.name)
            return

        decision.position_size = amount
        tp1_amount = self.broker.amount_to_precision(
            symbol, amount * self.cfg.exits.tp1_fraction
        )

        log.info(
            "[%s] ENTER %s %s: size %s @ ~%.2f, SL %.2f, TP1 %.2f, TP2 %.2f, "
            "RR %.2f, confidence %.2f",
            self.name, decision.direction, symbol, amount, decision.entry_price,
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
                               {"id": response.get("id")}, track=self.name)
            if self.datastore is not None and decision_id is not None:
                self.datastore.mark_executed(decision_id)
            self.manager.on_entry(decision, amount, equity,
                                  decision_id=decision_id)
        except (ccxt.BaseError, RuntimeError) as e:
            self.journal.error("execution", f"[{self.name}] {symbol}: {e}")
            if not self.is_shadow:
                raise           # real-track errors bubble up to the loop
            log.exception("[shadow] entry failed on %s", symbol)


class Trader:
    def __init__(self, cfg, market, real: Track, journal, datastore=None,
                 shadow: Track = None):
        self.cfg = cfg
        self.market = market
        self.real = real
        self.shadow = shadow
        self.journal = journal
        self.datastore = datastore
        self.last_snap_candle: dict = {}
        self.data_failures = 0

    # ------------------------------------------------------------ loop
    def run(self) -> None:
        for symbol in self.cfg.symbols:
            self.real.broker.setup_symbol(symbol)
        log.info(
            "Engine started (%s): %s on %s/%s | risk %.2f%%/trade, "
            "daily -%.1f%%/+%.1f%%, weekly DD %.1f%% | shadow track: %s",
            self.cfg.mode, ", ".join(self.cfg.symbols), self.cfg.timeframe,
            self.cfg.trend_timeframe, self.cfg.risk.risk_per_trade_pct,
            self.cfg.risk.daily_max_loss_pct, self.cfg.risk.daily_profit_target_pct,
            self.cfg.risk.weekly_max_drawdown_pct,
            "on" if self.shadow else "off",
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

    def _process_symbol(self, symbol: str, btc_trend: int) -> None:
        snap = self.market.snapshot(symbol, btc_trend)
        if snap.age_seconds() > self.cfg.max_stale_data_seconds:
            self.journal.error("data", f"stale snapshot for {symbol}")
            return

        # One snapshot row per closed candle feeds both tracks' decisions.
        snapshot_id = None
        candle_ts = str(snap.candles.index[-1]) if len(snap.candles) else ""
        if (self.datastore is not None
                and self.last_snap_candle.get(symbol) != candle_ts):
            snapshot_id = self.datastore.record_snapshot(snap)
            self.last_snap_candle[symbol] = candle_ts

        self.real.process(snap, snapshot_id)
        if self.shadow is not None:
            self.shadow.process(snap, snapshot_id)

    # ------------------------------------------------------------ safety
    def _kill_requested(self) -> bool:
        return os.path.isfile(self.cfg.kill_file)

    def _shutdown(self, why: str) -> None:
        log.warning("Shutdown: %s", why)
        self.journal.write("shutdown", reason=why,
                           close_positions=self.cfg.close_positions_on_kill)
        try:
            for symbol in self.cfg.symbols:
                self.real.broker.cancel_all(symbol)
            if self.cfg.close_positions_on_kill:
                self.real.broker.close_all()
                log.warning("All real positions flattened. "
                            "(Shadow positions are virtual and persist.)")
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
