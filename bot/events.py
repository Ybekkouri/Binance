"""Event guard: the bot's ears on the world.

World events (macro surprises, hacks, geopolitical shocks) reach crypto
prices faster than any news feed reaches a bot — so the primary detector
listens to the market's own seismograph:

  Shock detection — when a closed candle's range blows past `shock_atr_mult`
  times the prevailing ATR, something just happened. New entries are blocked
  for `shock_cooldown_minutes` while the dust settles, and your phone gets
  one alert. Open positions are untouched: their exchange-side stops,
  breakeven moves and trailing already handle violent moves.

  Blackout calendar — for events known IN ADVANCE (Fed decisions, CPI
  releases, big token unlocks), list windows in config.yaml and the bot
  simply refuses to open new trades inside them.

Both guards apply to real and shadow tracks alike (events are a property of
the market, not the account). This is deliberately not a headline-sentiment
trader: automated reactions to news text are slower than price and easy to
fool — standing aside during chaos is the edge that survives.
"""

import logging
import time
from datetime import datetime, timezone

from . import indicators as ta

log = logging.getLogger("bot.events")


class EventGuard:
    def __init__(self, cfg, notifier=None):
        self.cfg = cfg
        self.notifier = notifier
        self.shock_until: dict = {}      # symbol -> unix ts when cooldown ends
        self.blackouts = []
        for w in cfg.events.blackouts:
            try:
                start = datetime.fromisoformat(str(w["start"]).replace("Z", "+00:00"))
                end = datetime.fromisoformat(str(w["end"]).replace("Z", "+00:00"))
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                if end.tzinfo is None:
                    end = end.replace(tzinfo=timezone.utc)
                self.blackouts.append((start, end, str(w.get("label", "event"))))
            except (KeyError, TypeError, ValueError) as e:
                log.warning("ignoring malformed blackout window %r: %s", w, e)
        if self.blackouts:
            log.info("Loaded %d event blackout window(s).", len(self.blackouts))

    # ------------------------------------------------------------ checks
    def check(self, snap) -> list[str]:
        """Return veto reasons for new entries; empty list = clear."""
        fails = []
        fails += self._check_blackout()
        fails += self._check_shock(snap)
        return fails

    def _check_blackout(self) -> list[str]:
        now = datetime.now(timezone.utc)
        for start, end, label in self.blackouts:
            if start <= now <= end:
                return [f"event blackout '{label}' until {end:%Y-%m-%d %H:%M} UTC"]
        return []

    def _check_shock(self, snap) -> list[str]:
        ev = self.cfg.events
        symbol = snap.symbol

        # still cooling down from a previous shock?
        until = self.shock_until.get(symbol, 0)
        if time.time() < until:
            mins = (until - time.time()) / 60
            return [f"volatility shock cooldown ({mins:.0f} min left)"]

        df = snap.candles
        if len(df) < self.cfg.strategy.atr_period + 2:
            return []
        last = df.iloc[-1]
        # ATR of the bars BEFORE the candle being judged, so the shock
        # candle can't dilute its own yardstick
        atr = float(ta.atr(df.iloc[:-1], self.cfg.strategy.atr_period).iloc[-1])
        if atr <= 0:
            return []
        candle_range = float(last["high"]) - float(last["low"])
        if candle_range >= ev.shock_atr_mult * atr:
            self.shock_until[symbol] = time.time() + ev.shock_cooldown_minutes * 60
            msg = (f"market shock on {symbol}: candle range "
                   f"{candle_range / atr:.1f}x ATR — no new entries for "
                   f"{ev.shock_cooldown_minutes} min")
            log.warning(msg)
            if self.notifier is not None:
                self.notifier.send(
                    f"⚡ {msg}\nOpen positions stay protected by their "
                    "exchange-side stops.")
            return [msg]
        return []
