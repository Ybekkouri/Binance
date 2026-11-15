"""MEXC USDT-M perpetual futures broker (real money — MEXC has no testnet).

Same duck-typed interface as broker.LiveBroker, so the trader, risk engine
and trade manager do not know which exchange they are on. What differs from
Binance, and shapes this file:

  - Sizes are whole CONTRACTS on MEXC (1 BTC_USDT contract = 0.0001 BTC).
    The rest of the bot speaks base-asset units (BTC, ETH); this class
    converts at the boundary and rounds DOWN to whole contracts.
  - There is no "close the whole position" stop. Stop-loss and take-profit
    are trigger ("plan") orders that carry an explicit size, and an order
    larger than the open position may be rejected when it fires. So TP1 and
    TP2 are sized to add up to the position, and the trade manager re-sizes
    the stop whenever the position shrinks (SIZED_STOPS).
  - Plan orders expire (7 days at most), so the manager also re-places the
    stop before that (STOP_REFRESH_S). A trade held longer than 7 days
    loses its take-profit orders; the trailing stop still manages it.
  - Realized PnL comes from the closed-position record (`realised`, which
    is net of trading fees). Funding payments are not included.
"""

import logging
import math

import ccxt

log = logging.getLogger("bot.broker.mexc")


class MexcBroker:
    SIZED_STOPS = True               # stops carry a size; manager keeps it right
    STOP_REFRESH_S = 5 * 86400       # re-place the stop well before the 7d expiry

    def __init__(self, cfg):
        self.cfg = cfg
        self.client = ccxt.mexc({
            "apiKey": cfg.api_key,
            "secret": cfg.api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "swap",
                        "adjustForTimeDifference": True},
        })
        self._prepare(self.client)
        log.warning("MexcBroker on LIVE MEXC Futures — real money.")
        self.client.load_markets()
        self._hedged = False         # hedge position mode confirmed yet?

    @staticmethod
    def _prepare(client) -> None:
        # With keys set, ccxt's market loading also calls a SPOT WALLET
        # endpoint for coin details. A futures-only key is refused there,
        # which would stop the bot at startup — and we never need it.
        client.has["fetchCurrencies"] = False

    # ---- symbol / unit helpers ----
    @staticmethod
    def _s(symbol: str) -> str:
        """Bot symbol 'BTC/USDT' -> ccxt swap symbol 'BTC/USDT:USDT'."""
        return symbol if ":" in symbol else f"{symbol}:{symbol.split('/')[1]}"

    def _market(self, symbol: str) -> dict:
        return self.client.market(self._s(symbol))

    def _contract_size(self, symbol: str) -> float:
        return float(self._market(symbol).get("contractSize") or 1.0)

    def _vol(self, symbol: str, amount: float) -> float:
        """Base-asset amount -> whole contracts, rounded DOWN (0 if below
        the exchange minimum)."""
        m = self._market(symbol)
        step = float((m.get("precision") or {}).get("amount") or 1.0)
        min_vol = float(((m.get("limits") or {}).get("amount") or {})
                        .get("min") or step)
        raw = amount / self._contract_size(symbol) / step
        vol = math.floor(raw + 1e-9) * step      # 1e-9: float division noise
        vol = round(vol, 8)
        return vol if vol >= min_vol else 0.0

    def _params(self, **extra) -> dict:
        """Parameters every MEXC futures order needs.

        The account must be in HEDGE position mode (MEXC's default), where
        MEXC's order sides are unambiguous: 1 open long, 2 close short,
        3 open short, 4 close long. We state the mode explicitly
        (positionMode 1) and deliberately do NOT pass ccxt's `hedged` flag:
        with it, ccxt maps a reduce-only SELL to "close short" — the wrong
        side for closing a long. Without it, sell+reduceOnly -> 4 (close
        long) and buy+reduceOnly -> 2 (close short), as MEXC documents.
        tests/test_mexc_contract.py pins these numbers down."""
        self.ensure_hedge_mode()
        p = {"marginMode": self.cfg.margin_mode, "leverage": self.cfg.leverage,
             "positionMode": 1}
        p.update(extra)
        return p

    def ensure_hedge_mode(self) -> None:
        """Verify (once) that the account is in hedge position mode, and
        switch it if not. Raises if it cannot be confirmed: placing orders
        in the wrong mode could close the wrong side."""
        if self._hedged:
            return
        if not self.client.fetch_position_mode().get("hedged"):
            log.warning("MEXC account is in one-way mode — switching to "
                        "hedge mode (required by this bot).")
            try:
                self.client.set_position_mode(True)
            except ccxt.BaseError as e:
                raise ccxt.ExchangeError(
                    "MEXC account is in One-way position mode and could not "
                    "be switched automatically (close any open futures "
                    "positions/orders, then set Position Mode to Hedge in "
                    f"the MEXC futures settings): {e}") from e
        self._hedged = True

    def setup_symbol(self, symbol: str) -> None:
        self.ensure_hedge_mode()
        open_type = 1 if self.cfg.margin_mode == "isolated" else 2
        for position_type in (1, 2):             # 1 long, 2 short
            try:
                self.client.set_leverage(
                    self.cfg.leverage, self._s(symbol),
                    {"openType": open_type, "positionType": position_type})
            except ccxt.BaseError as e:
                log.debug("set_leverage(%s, %s): %s", symbol, position_type, e)

    # ---- account ----
    def equity_usdt(self) -> float:
        bal = self.client.fetch_balance({"type": "swap"})
        data = (bal.get("info") or {}).get("data") or []
        for row in data if isinstance(data, list) else []:
            if row.get("currency") == "USDT" and row.get("equity") is not None:
                return float(row["equity"])      # includes unrealized PnL
        return float((bal.get("USDT") or {}).get("total") or 0.0)

    def open_positions(self) -> list[dict]:
        out = []
        wanted = [self._s(s) for s in self.cfg.symbols]
        for p in self.client.fetch_positions(wanted):
            vol = float(p.get("contracts") or 0)
            if vol == 0:
                continue
            symbol = p["symbol"].split(":")[0]
            size = float(p.get("contractSize") or self._contract_size(symbol))
            base = round(vol * size, 12)
            entry = float(p.get("entryPrice") or 0)
            out.append({
                "symbol": symbol,
                "side": p["side"],
                "contracts": base,               # base-asset units, like Binance
                "vol": vol,                      # raw MEXC contract count
                "notional": base * entry,
                "entry_price": entry,
                "liquidation_price": float(p.get("liquidationPrice") or 0),
                "margin_ratio": float(p.get("marginRatio") or 0),
            })
        return out

    # ---- precision helpers ----
    def amount_to_precision(self, symbol: str, amount: float) -> float:
        """Round a base-asset amount DOWN to whole contracts (0 when it is
        below one tradable contract — the caller then refuses the trade)."""
        return round(self._vol(symbol, amount) * self._contract_size(symbol), 12)

    def price_to_precision(self, symbol: str, price: float) -> float:
        return float(self.client.price_to_precision(self._s(symbol), price))

    def min_notional(self, symbol: str) -> float:
        return 0.0   # MEXC's minimum is in contracts: amount_to_precision

    # ---- orders ----
    def _plan(self, symbol: str, side: str, price: float, vol: float,
              is_stop: bool) -> str:
        """Reduce-only market trigger order. A long's stop fires when price
        FALLS to it and its take-profit when price RISES to it (mirrored
        for shorts)."""
        close_side = "sell" if side == "long" else "buy"
        falling = (side == "long") == is_stop
        order = self.client.create_order(
            self._s(symbol), "market", close_side, vol, None,
            self._params(
                triggerPrice=self.price_to_precision(symbol, price),
                triggerDirection="descending" if falling else "ascending",
                reduceOnly=True,
                executeCycle=2,                  # 7 days, the longest allowed
            ),
        )
        return order.get("id")

    def enter(self, symbol: str, side: str, amount: float, stop: float,
              tp1: float, tp1_amount: float, tp2: float) -> dict:
        """Market entry + full-size stop + TP1 partial + TP2 for the rest.

        Returns {"id": entry order id, "sl_order_id": stop order id}.
        """
        self.ensure_hedge_mode()     # refuse BEFORE any order if unsure
        vol = self._vol(symbol, amount)
        if vol <= 0:
            raise RuntimeError(f"{symbol}: size below one MEXC contract")
        tp1_vol = self._vol(symbol, tp1_amount) if tp1_amount > 0 else 0.0
        tp1_vol = min(tp1_vol, vol)
        tp2_vol = round(vol - tp1_vol, 8)
        order_side = "buy" if side == "long" else "sell"
        # We only enter when flat, so leftovers from an earlier trade are
        # always safe to clear (and could otherwise close the new position).
        self.cancel_all(symbol)
        try:
            entry = self.client.create_order(
                self._s(symbol), "market", order_side, vol, None,
                self._params())
        except ccxt.BaseError:
            log.exception("Entry order failed on %s — defensive flatten.", symbol)
            try:
                self.close_position(symbol)
            except ccxt.BaseError:
                log.exception("Defensive flatten also failed — CHECK EXCHANGE.")
            raise
        try:
            sl_id = self._plan(symbol, side, stop, vol, is_stop=True)
            if tp1_vol > 0:
                self._plan(symbol, side, tp1, tp1_vol, is_stop=False)
            if tp2_vol > 0:
                self._plan(symbol, side, tp2, tp2_vol, is_stop=False)
        except ccxt.BaseError:
            log.exception("Bracket placement failed for %s — flattening.", symbol)
            self.close_position(symbol)
            raise
        return {"id": entry.get("id"), "sl_order_id": sl_id}

    def place_stop(self, symbol: str, side: str, stop: float,
                   amount: float) -> str:
        vol = self._vol(symbol, amount)
        if vol <= 0:
            raise RuntimeError(f"{symbol}: stop size below one MEXC contract")
        return self._plan(symbol, side, stop, vol, is_stop=True)

    def replace_stop(self, symbol: str, side: str, new_stop: float,
                     old_order_id: str = None, old_stop: float = None,
                     amount: float = None) -> str:
        """New stop first, then cancel the old one, so the position is never
        without a stop. Raises if the new stop cannot be placed."""
        if amount is None:
            pos = next((p for p in self.open_positions()
                        if p["symbol"] == symbol), None)
            if pos is None:
                return None
            amount = pos["contracts"]
        new_id = self.place_stop(symbol, side, new_stop, amount)

        cancelled = False
        if old_order_id:
            try:
                self.client.cancel_order(old_order_id, self._s(symbol),
                                         {"trigger": True})
                cancelled = True
            except ccxt.BaseError as e:
                log.warning("cancel stop %s by id failed (%s); falling back "
                            "to trigger-price match", old_order_id, e)
        if not cancelled and old_stop:
            try:
                for o in self.client.fetch_open_orders(
                        self._s(symbol), params={"trigger": True}):
                    if str(o.get("id")) == str(new_id):
                        continue
                    trig = float(o.get("triggerPrice") or o.get("stopPrice")
                                 or (o.get("info") or {}).get("triggerPrice")
                                 or 0)
                    if trig and abs(trig - old_stop) / old_stop < 0.002:
                        self.client.cancel_order(o["id"], self._s(symbol),
                                                 {"trigger": True})
            except ccxt.BaseError:
                # A leftover looser stop is reduce-only and further away.
                log.exception("old-stop cleanup failed on %s", symbol)
        return new_id

    def cancel_all(self, symbol: str) -> bool:
        """Cancel BOTH regular and trigger (plan) orders on the symbol."""
        ok = True
        request = {"symbol": self._market(symbol)["id"]}
        for name, call in (
                ("orders", self.client.contractPrivatePostOrderCancelAll),
                ("plan orders", self.client.contractPrivatePostPlanorderCancelAll)):
            try:
                call(dict(request))
            except ccxt.OrderNotFound:
                pass
            except ccxt.BaseError as e:
                ok = False
                log.warning("cancel all %s on %s failed: %s", name, symbol, e)
        return ok

    def _market_close(self, position: dict) -> None:
        side = "sell" if position["side"] == "long" else "buy"
        self.client.create_order(
            self._s(position["symbol"]), "market", side, position["vol"],
            None, self._params(reduceOnly=True))
        log.info("Closed %s position on %s.", position["side"],
                 position["symbol"])

    def close_position(self, symbol: str) -> None:
        for p in self.open_positions():
            if p["symbol"] == symbol:
                self._market_close(p)
        self.cancel_all(symbol)

    def close_all(self) -> list:
        """Flatten everything, continuing past per-symbol errors. Returns
        the symbols still open afterwards; orders are cancelled only on
        symbols confirmed flat, so a failed close keeps its stop."""
        for p in self.open_positions():
            try:
                self._market_close(p)
            except ccxt.BaseError:
                log.exception("Failed to close %s", p["symbol"])
        still_open = [p["symbol"] for p in self.open_positions()]
        for symbol in self.cfg.symbols:
            if symbol not in still_open:
                self.cancel_all(symbol)
        return still_open

    def realized_pnl_since(self, symbol: str, since_ms: int) -> float:
        """Net realized PnL (after trading fees) of positions on the symbol
        closed since a time. Raises until the closed-position record
        appears, so the caller retries instead of booking a wrong number."""
        rows = self.client.fetch_positions_history([self._s(symbol)], limit=100)
        total, seen = 0.0, False
        for row in rows:
            info = row.get("info") or {}
            if int(float(info.get("updateTime") or 0)) < since_ms:
                continue
            if float(info.get("holdVol") or 0) != 0:
                continue                         # still open
            total += float(info.get("realised") or 0)
            seen = True
        if not seen:
            raise RuntimeError(f"no closed-position record for {symbol} yet")
        return total
