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

`observe()` runs on every snapshot (whether or not anything wants to trade)
so a shock is caught on the candle it prints; `check()` is the entry veto.
Both apply to real and shadow tracks alike (events are a property of the
market, not the account). This is deliberately not a headline-sentiment
trader: automated reactions to news text are slower than price and easy to
fool — standing aside during chaos is the edge that survives.
"""

import logging
import time
from datetime import datetime, timezone

from . import indicators as ta

log = logging.getLogger("bot.events")


def is_shock(df, atr_period: int, mult: float) -> tuple[bool, float]:
    """(shocked, range/ATR) for the last candle of `df`. ATR is measured on
    the bars BEFORE it, so the shock candle can't dilute its own yardstick.
    Shared by the live guard and the backtester."""
    if len(df) < atr_period + 2:
        return False, 0.0
    atr = float(ta.atr(df.iloc[:-1], atr_period).iloc[-1])
    if atr <= 0:
        return False, 0.0
    last = df.iloc[-1]
    ratio = (float(last["high"]) - float(last["low"])) / atr
    return ratio >= mult, ratio


class EventGuard:
    def __init__(self, cfg, notifier=None):
        self.cfg = cfg
        self.notifier = notifier
        self.shock_until: dict = {}      # symbol -> unix ts when cooldown ends
        self.seen_candle: dict = {}      # symbol -> last candle observed
        self.blackouts = []
        problems = []
        for w in cfg.events.blackouts:
            try:
                start = _parse(w["start"])
                end = _parse(w["end"])
                if end <= start:
                    raise ValueError("end is not after start")
                self.blackouts.append((start, end, str(w.get("label", "event"))))
            except (KeyError, TypeError, ValueError) as e:
                log.warning("ignoring malformed blackout window %r: %s", w, e)
                problems.append(f"{w!r}: {e}")
        if self.blackouts:
            log.info("Loaded %d event blackout window(s).", len(self.blackouts))
        if problems and notifier is not None:
            # a typo silently disables a protection — say so loudly
            notifier.send("⚠️ Ignored invalid blackout window(s) in "
                          "config.yaml:\n" + "\n".join(problems))

    # ------------------------------------------------------------ observe
    def observe(self, snap) -> None:
        """Called once per snapshot. Arms the cooldown on a shock candle."""
        symbol = snap.symbol
        df = snap.candles
        if not len(df):
            return
        candle = str(df.index[-1])
        if self.seen_candle.get(symbol) == candle:
            return
        self.seen_candle[symbol] = candle
        ev = self.cfg.events
        shocked, ratio = is_shock(df, self.cfg.strategy.atr_period,
                                  ev.shock_atr_mult)
        if not shocked:
            return
        self.shock_until[symbol] = time.time() + ev.shock_cooldown_minutes * 60
        msg = (f"market shock on {symbol}: candle range {ratio:.1f}x ATR — "
               f"no new entries for {ev.shock_cooldown_minutes} min")
        log.warning(msg)
        if self.notifier is not None:
            self.notifier.send(f"⚡ {msg}\nOpen positions stay protected by "
                               "their exchange-side stops.")

    # ------------------------------------------------------------ checks
    def check(self, snap) -> list[str]:
        """Return veto reasons for new entries; empty list = clear."""
        self.observe(snap)       # no-op if this candle was already observed
        fails = self._check_blackout()
        until = self.shock_until.get(snap.symbol, 0)
        if time.time() < until:
            mins = (until - time.time()) / 60
            fails.append(f"volatility shock cooldown ({mins:.0f} min left)")
        return fails

    def _check_blackout(self) -> list[str]:
        now = datetime.now(timezone.utc)
        for start, end, label in self.blackouts:
            if start <= now <= end:
                return [f"event blackout '{label}' until {end:%Y-%m-%d %H:%M} UTC"]
        return []


def _parse(value) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
