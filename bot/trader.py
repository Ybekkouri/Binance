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


def stopped_marker(cfg) -> str:
    """Path of the marker that keeps the bot off after a kill switch."""
    return os.path.join(os.path.dirname(cfg.kill_file) or ".", "STOPPED")


def sd_notify(message: str) -> None:
    """Ping systemd (READY=1 / WATCHDOG=1). No-op outside systemd.

    With Type=notify + WatchdogSec in the unit, a HUNG process (not just a
    crashed one) gets killed and restarted automatically."""
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        return
    import socket
    if sock_path.startswith("@"):
        sock_path = "\0" + sock_path[1:]
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            s.sendto(message.encode(), sock_path)
        finally:
            s.close()
    except OSError as e:
        log.debug("sd_notify failed: %s", e)

log = logging.getLogger("bot.trader")


class Track:
    """One complete pipeline: thresholds -> risk checks -> execution ->
    management -> settlement. Instantiated once for real, once for shadow."""

    def __init__(self, name, cfg, broker, risk, manager, journal,
                 datastore=None, min_confidence=None, min_aligned_factors=None,
                 notifier=None, events=None):
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
        self.events = events
        self.last_eval_candle: dict = {}
        self.entry_failures: dict = {}   # (symbol, candle) -> failed attempts

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
        if self.events is not None:
            fails += self.events.check(snap)
        # Exchange minimum order value: the TP1 partial is the smallest order
        # in the bracket, so IT must clear the minimum — measured on the
        # ROUNDED size actually sent, not the unrounded fraction.
        tp1_amount = self.broker.amount_to_precision(
            symbol, amount * self.cfg.exits.tp1_fraction
        ) if self.cfg.exits.tp1_fraction > 0 else 0.0
        min_notional = getattr(self.broker, "min_notional", lambda s: 0.0)(symbol)
        smallest = (tp1_amount if tp1_amount > 0 else amount) * decision.entry_price
        if self.cfg.exits.tp1_fraction > 0 and tp1_amount <= 0:
            fails.append("TP1 partial rounds to zero — position too small")
        elif min_notional and smallest < min_notional:
            fails.append(
                f"position too small for exchange minimums (smallest bracket "
                f"order {smallest:.0f} < {min_notional:.0f} USDT min) — "
                "account equity too low for this symbol")
        margin_needed = notional / max(self.cfg.leverage, 1)
        if margin_needed > equity * 0.95:
            fails.append("insufficient margin for position")
        if fails:
            log.info("[%s] NO TRADE %s: %s", self.name, symbol, "; ".join(fails))
            self.journal.risk_block(symbol, fails, track=self.name)
            return

        decision.position_size = amount

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
        candle_key = (symbol, str(snap.candles.index[-1])
                      if len(snap.candles) else "")
        # a few seconds of slack covers clock skew vs Binance timestamps
        opened_ms = int(time.time() * 1000) - 5000
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
                                  sl_order_id=response.get("sl_order_id"),
                                  opened_ms=opened_ms)
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
            self._adopt_leftover(decision, symbol, equity, decision_id,
                                 opened_ms)
            # One retry per candle: a transient failure deserves a second
            # chance, but a deterministic rejection must not become an
            # enter-and-flatten loop paying fees every 30 seconds.
            failures = self.entry_failures.get(candle_key, 0) + 1
            self.entry_failures = {candle_key: failures}
            if failures < 2:
                self.last_eval_candle.pop(symbol, None)
            if not self.is_shadow:
                raise           # real-track errors bubble up to the loop
            log.exception("[shadow] entry failed on %s", symbol)

    def _adopt_leftover(self, decision, symbol, equity, decision_id,
                        opened_ms) -> None:
        """If a failed entry left a position behind (fill succeeded, bracket
        and flatten failed), take ownership of it so the manager restores
        its stop on the next tick instead of ignoring it as 'unmanaged'."""
        try:
            pos = next((p for p in self.broker.open_positions()
                        if p["symbol"] == symbol), None)
        except Exception:               # noqa: BLE001
            return
        if pos is None or symbol in self.risk.state.managed:
            return
        log.warning("[%s] failed entry left a %s position on %s — adopting "
                    "it and restoring its stop.", self.name, pos["side"], symbol)
        self.manager.on_entry(decision, pos["contracts"], equity,
                              decision_id=decision_id, opened_ms=opened_ms,
                              needs_protection=True)
        if self.notifier is not None and not self.is_shadow:
            self.notifier.send(
                f"⚠️ Entry on {symbol} failed part-way and left an open "
                "position. The bot has taken control of it and is placing "
                "its stop-loss now.")


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
        sd_notify("READY=1")
        try:
            while True:
                if self._kill_requested():
                    if not self._shutdown("kill switch", persistent=True):
                        # flatten failed: exit non-zero so systemd restarts
                        # us and the kill (file still present) is retried
                        raise SystemExit(1)
                    return
                self.tick()
                time.sleep(self.cfg.poll_seconds)
        except KeyboardInterrupt:
            self._shutdown("keyboard interrupt")

    def tick(self) -> None:
        sd_notify("WATCHDOG=1")   # heartbeat: a hung loop gets restarted
        self._maybe_daily_summary()
        self._handle_commands()
        try:
            btc_trend = self.market.btc_trend()
            self.data_failures = 0
        except ccxt.BaseError as e:
            self._data_failure(f"BTC trend fetch failed: {e}")
            return

        for symbol in self.cfg.symbols:
            sd_notify("WATCHDOG=1")   # a slow tick must not trip the watchdog
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

        # Shock detection runs on EVERY candle, before either track and
        # regardless of whether anything wants to trade.
        events = self.real.events
        if events is not None:
            events.observe(snap)

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
    def _maybe_daily_summary(self) -> None:
        """First tick of a new UTC day: message yesterday's results for both
        tracks, read from risk state BEFORE the day-roll wipes the counters."""
        import datetime as _dt
        today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        if getattr(self, "_summary_date", None) is None:
            self._summary_date = today          # first tick after start: skip
            return
        if self._summary_date == today:
            return
        self._summary_date = today
        yesterday = (_dt.datetime.now(_dt.timezone.utc)
                     - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
        lines = [f"🌅 Daily report for {yesterday} ({self.cfg.mode})"]
        for track in filter(None, [self.real, self.shadow]):
            st = track.risk.state
            # risk state only rolls on activity, so its date tells us whether
            # yesterday actually saw any; an older date means a quiet day
            if st.date == yesterday:
                lines.append(
                    f"[{track.name}] {st.daily_pnl:+.2f} USDT over "
                    f"{st.trades_today} trade(s)"
                    + (f", {st.consecutive_losses} loss streak"
                       if st.consecutive_losses else ""))
            else:
                lines.append(f"[{track.name}] no trades")
        self.notifier.send("\n".join(lines))

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
            elif cmd == "/senddata":
                self._send_dataset()
            elif cmd == "/help":
                self.notifier.send(
                    "/status — positions, equity, today's PnL\n"
                    "/senddata — receive the research dataset as a file "
                    "(forward it to whoever merges the pooled data)\n"
                    "/kill — emergency stop: cancel orders, flatten, shut down\n"
                    "/help — this message")

    def _send_dataset(self) -> None:
        path = self.cfg.datastore_file
        if not os.path.isfile(path):
            self.notifier.send("No dataset file yet — the bot hasn't "
                               "recorded anything.")
            return
        counts = self.datastore.counts() if self.datastore else {}
        caption = ("Research dataset (no keys or secrets inside). "
                   f"{counts.get('trades', '?')} trades, "
                   f"{counts.get('decisions', '?')} decisions, "
                   f"{counts.get('snapshots', '?')} snapshots. "
                   "Merge with: python3 research.py merge <this file>")
        if self.notifier.send_document(path, caption):
            log.info("Dataset sent via Telegram (%d bytes).",
                     os.path.getsize(path))
        else:
            self.notifier.send(
                "Could not upload the dataset (too large for Telegram's "
                "~50 MB bot limit, or a network error). Fallback: copy "
                f"{path} off the server manually.")

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

    def _shutdown(self, why: str, persistent: bool = False) -> bool:
        """Stop trading. Returns True on a clean stop, False when positions
        could not be flattened (the caller then exits non-zero so systemd
        restarts the bot and the kill is retried).

        persistent=True (the kill switch) also writes a STOPPED marker so a
        reboot or crash-restart does NOT silently resume trading."""
        log.warning("Shutdown: %s", why)
        self.journal.write("shutdown", reason=why,
                           close_positions=self.cfg.close_positions_on_kill)
        still_open = []
        if self.cfg.close_positions_on_kill:
            try:
                still_open = self.real.broker.close_all() or []
            except Exception as e:      # noqa: BLE001
                log.exception("close_all failed")
                still_open = [f"unknown ({e})"]
        if still_open:
            self.notifier.send(
                "⚠️ Emergency stop could NOT close: "
                f"{', '.join(map(str, still_open))}. Their stop-losses are "
                "still on Binance. Retrying automatically — also check the "
                "Binance app yourself.")
            return False

        if os.path.isfile(self.cfg.kill_file):
            os.remove(self.cfg.kill_file)
        if persistent:
            with open(stopped_marker(self.cfg), "w") as f:
                f.write(f"stopped by {why}\n")
        self.notifier.send(
            f"🤖 Engine stopped ({why}). "
            + ("All real positions closed."
               if self.cfg.close_positions_on_kill else
               "Positions left open with their exchange-side stops.")
            + (" It will stay off — even after a reboot — until you restart "
               "it by re-running the installer line." if persistent else ""))
        return True

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
