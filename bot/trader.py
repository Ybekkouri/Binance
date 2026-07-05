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
                 datastore=None, min_confidence=None, min_aligned_factors=None,
                 notifier=None):
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
        self.notifier = notifier
        self.last_eval_candle: dict = {}

    def process(self, snap, snapshot_id=None) -> None:
        symbol = snap.symbol
        if hasattr(self.broker, "mark"):        # paper fills trigger on price
            self.broker.mark(symbol, snap.last_price)

        # One positions fetch per tick, shared with management and entry.
        positions = self.broker.open_positions()
        position = next((p for p in positions if p["symbol"] == symbol), None)
        has_position = position is not None or symbol in self.risk.state.managed

        if has_position:
            # decide() is needed here for invalidation (opposing signal).
            decision = strategy.decide(
                snap, self.cfg,
                min_confidence=self.min_confidence,
                min_aligned_factors=self.min_aligned_factors,
            )
            self.manager.manage(snap, opposing_decision=decision,
                                position=position)
            return

        # Signals come from closed candles: evaluate/record each candle once
        # (checked BEFORE decide, so intra-candle ticks skip the indicator work).
        candle_ts = str(snap.candles.index[-1]) if len(snap.candles) else ""
        if self.last_eval_candle.get(symbol) == candle_ts:
            return
        self.last_eval_candle[symbol] = candle_ts

        decision = strategy.decide(
            snap, self.cfg,
            min_confidence=self.min_confidence,
            min_aligned_factors=self.min_aligned_factors,
        )
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
            self._try_enter(decision, snap, decision_id, positions)

    def _try_enter(self, decision, snap, decision_id=None,
                   positions=None) -> None:
        symbol = decision.symbol
        equity = self.broker.equity_usdt()
        if positions is None:
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
                                  decision_id=decision_id,
                                  sl_order_id=response.get("sl_order_id"))
            if self.notifier is not None and (
                    not self.is_shadow or self.cfg.telegram_notify_shadow):
                tag = "" if not self.is_shadow else " [shadow]"
                self.notifier.send(
                    f"📈 Opened {decision.direction} {symbol}{tag}\n"
                    f"size {amount} @ ~{decision.entry_price:.2f}\n"
                    f"stop {decision.stop_loss:.2f} | "
                    f"TP1 {decision.take_profit_1:.2f} | "
                    f"TP2 {decision.take_profit_2:.2f}\n"
                    f"confidence {decision.confidence:.2f}, RR {decision.risk_reward:.1f}")
        except (ccxt.BaseError, RuntimeError) as e:
            self.journal.error("execution", f"[{self.name}] {symbol}: {e}")
            # allow a retry on the next tick of the same candle — the signal
            # is still valid and the failure may have been transient
            self.last_eval_candle.pop(symbol, None)
            if not self.is_shadow:
                raise           # real-track errors bubble up to the loop
            log.exception("[shadow] entry failed on %s", symbol)


class Trader:
    def __init__(self, cfg, market, real: Track, journal, datastore=None,
                 shadow: Track = None, notifier=None):
        from .notify import NullNotifier
        self.cfg = cfg
        self.market = market
        self.real = real
        self.shadow = shadow
        self.journal = journal
        self.datastore = datastore
        self.notifier = notifier or NullNotifier()
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
        self.notifier.send(
            f"🤖 Engine started ({self.cfg.mode})\n"
            f"{', '.join(self.cfg.symbols)} on {self.cfg.timeframe}\n"
            f"risk {self.cfg.risk.risk_per_trade_pct}%/trade, daily "
            f"-{self.cfg.risk.daily_max_loss_pct}%/+"
            f"{self.cfg.risk.daily_profit_target_pct}%\n"
            f"Commands: /status /kill /help")
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
        self._handle_commands()
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
            except Exception:   # noqa: BLE001 — one bad symbol must never
                # kill the loop: open positions elsewhere still need managing
                log.exception("Unexpected error on %s", symbol)
                self.journal.error("tick", f"unexpected error on {symbol}")

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

    # ------------------------------------------------------------ telegram
    def _handle_commands(self) -> None:
        commands = self.notifier.poll_commands()
        if commands and hasattr(self.notifier, "ack"):
            # confirm the advanced offset server-side immediately, otherwise
            # a /kill followed by shutdown would replay on every restart
            self.notifier.ack()
        for cmd in commands:
            log.info("Telegram command: %s", cmd)
            self.journal.write("telegram_command", command=cmd)
            if cmd == "/kill":
                with open(self.cfg.kill_file, "w") as f:
                    f.write("telegram /kill\n")
                self.notifier.send("🛑 Kill switch activated — cancelling "
                                   "orders and shutting down within one cycle.")
            elif cmd == "/status":
                self.notifier.send(self._status_text())
            elif cmd == "/help":
                self.notifier.send(
                    "/status — positions, equity, today's PnL\n"
                    "/kill — emergency stop: cancel orders, flatten, shut down\n"
                    "/help — this message")

    def _status_text(self) -> str:
        lines = [f"📊 Status ({self.cfg.mode})"]
        for track in filter(None, [self.real, self.shadow]):
            try:
                equity = track.broker.equity_usdt()
                positions = track.broker.open_positions()
                track.risk.roll(equity)
                lines.append(
                    f"\n[{track.name}] equity {equity:.2f} USDT | today "
                    f"{track.risk.state.daily_pnl:+.2f} over "
                    f"{track.risk.state.trades_today} trade(s)")
                for p in positions:
                    lines.append(f"  {p['side']} {p['symbol']} "
                                 f"{p['contracts']} @ {p['entry_price']:.2f}")
                if not positions:
                    lines.append("  no open positions")
            except Exception as e:      # noqa: BLE001 — status must not crash
                lines.append(f"\n[{track.name}] status unavailable: {e}")
        return "\n".join(lines)

    # ------------------------------------------------------------ safety
    def _kill_requested(self) -> bool:
        return os.path.isfile(self.cfg.kill_file)

    def _shutdown(self, why: str) -> None:
        log.warning("Shutdown: %s", why)
        self.journal.write("shutdown", reason=why,
                           close_positions=self.cfg.close_positions_on_kill)
        try:
            if self.cfg.close_positions_on_kill:
                # flattening cancels all orders too
                self.real.broker.close_all()
                log.warning("All real positions flattened. "
                            "(Shadow positions are virtual and persist.)")
            else:
                # positions stay open — their exchange-side brackets MUST
                # stay too, so cancel nothing
                log.warning("Positions left open, exchange-side brackets "
                            "left in place.")
        except ccxt.BaseError:
            log.exception("Error during emergency shutdown — check the exchange!")
            self.notifier.send("⚠️ Error during emergency shutdown — CHECK "
                               "THE EXCHANGE MANUALLY for open positions!")
        if os.path.isfile(self.cfg.kill_file):
            os.remove(self.cfg.kill_file)
        self.notifier.send(f"🤖 Engine stopped ({why}). "
                           + ("Real positions flattened."
                              if self.cfg.close_positions_on_kill else
                              "Positions left open with exchange-side brackets."))

    def _data_failure(self, message: str) -> None:
        self.data_failures += 1
        log.warning("Data failure #%d: %s", self.data_failures, message)
        if self.data_failures >= 3:
            self.journal.error("data_outage", message)
            # alert at most once per hour so repeat outages still notify
            now = time.time()
            if now - getattr(self, "_last_outage_alert", 0) > 3600:
                self._last_outage_alert = now
                self.notifier.send(
                    "⚠️ Repeated market-data failures — no new entries until "
                    "data recovers. Open positions stay protected by "
                    "exchange-side brackets.")
            log.warning(
                "Repeated data failures — no new entries until data recovers. "
                "Open positions remain protected by exchange-side brackets."
            )
