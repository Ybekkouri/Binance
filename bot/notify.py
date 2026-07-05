"""Telegram notifications and remote commands.

The engine runs on a server; your phone is the dashboard. The bot messages
you on startup, every real trade opened/closed, daily limit halts, data
outages, and shutdown — and answers commands sent from the configured chat:

  /status  positions, equity, and today's PnL for both tracks
  /kill    activate the kill switch (cancel orders, flatten, stop)
  /help    list commands

Safety properties:
  - Only the configured chat id is obeyed; strangers are ignored.
  - Every network call is wrapped: a Telegram outage can never affect
    trading, only silence the messages.
  - `once()` deduplicates repeating events (e.g. a daily-halt reason that
    re-fires every candle) so your phone isn't spammed.
"""

import logging

import requests

log = logging.getLogger("bot.notify")

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 5  # short: a slow Telegram must not stall the trading loop


class Telegram:
    def __init__(self, token: str, chat_id: str, enabled: bool = True):
        self.token = token
        self.chat_id = str(chat_id) if chat_id else ""
        self.enabled = bool(enabled and token and chat_id)
        self.offset = 0
        self._sent_once: set = set()
        if enabled and not self.enabled:
            log.warning("Telegram enabled in config but TELEGRAM_BOT_TOKEN / "
                        "TELEGRAM_CHAT_ID missing in .env — notifications off.")

    # ---- transport (overridable in tests) ----
    def _api(self, method: str, params: dict) -> dict:
        r = requests.post(API.format(token=self.token, method=method),
                          json=params, timeout=TIMEOUT)
        return r.json()

    # ---- outbound ----
    def send(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            self._api("sendMessage", {"chat_id": self.chat_id, "text": text})
        except Exception as e:      # noqa: BLE001 — never break trading
            log.warning("Telegram send failed: %s", e)

    def once(self, key: str, text: str) -> None:
        """Send at most once per key (kept in memory; restarts may resend)."""
        if key in self._sent_once:
            return
        self._sent_once.add(key)
        self.send(text)

    # ---- inbound commands ----
    def poll_commands(self) -> list[str]:
        """Fetch new messages from the configured chat; return command texts."""
        if not self.enabled:
            return []
        try:
            resp = self._api("getUpdates",
                             {"offset": self.offset, "timeout": 0})
        except Exception as e:      # noqa: BLE001
            log.warning("Telegram poll failed: %s", e)
            return []
        commands = []
        for upd in resp.get("result", []):
            self.offset = max(self.offset, upd["update_id"] + 1)
            msg = upd.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            text = (msg.get("text") or "").strip()
            if chat != self.chat_id:
                log.warning("Ignoring Telegram message from unknown chat %s", chat)
                continue
            if text.startswith("/"):
                commands.append(text.split("@")[0].lower())
        return commands

    def ack(self) -> None:
        """Confirm the current offset server-side so processed commands are
        never replayed after a restart (critical for /kill: without this,
        Telegram redelivers it on the next startup and the bot re-kills)."""
        if not self.enabled:
            return
        try:
            self._api("getUpdates", {"offset": self.offset, "timeout": 0,
                                     "limit": 1})
        except Exception as e:      # noqa: BLE001
            log.warning("Telegram ack failed: %s", e)


class NullNotifier:
    """Used when Telegram is not configured — every call is a no-op."""

    enabled = False

    def send(self, text: str) -> None:
        pass

    def once(self, key: str, text: str) -> None:
        pass

    def poll_commands(self) -> list[str]:
        return []


def make_notifier(cfg):
    if cfg.telegram_enabled and cfg.telegram_token and cfg.telegram_chat_id:
        return Telegram(cfg.telegram_token, cfg.telegram_chat_id)
    return NullNotifier()
