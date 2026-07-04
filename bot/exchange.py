"""Thin wrapper around ccxt's Binance USDS-M futures client.

Handles testnet switching, leverage/margin setup, bracket orders
(market entry + reduce-only stop-loss and take-profit), and position/PnL
queries. Everything the rest of the bot needs from Binance goes through here.
"""

import logging

import ccxt
import pandas as pd

log = logging.getLogger("bot.exchange")


class FuturesExchange:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = ccxt.binanceusdm({
            "apiKey": cfg.api_key,
            "secret": cfg.api_secret,
            "enableRateLimit": True,
            "options": {"adjustForTimeDifference": True},
        })
        if cfg.testnet:
            self.client.set_sandbox_mode(True)
            log.info("Running on Binance Futures TESTNET (no real money).")
        else:
            log.warning("Running on LIVE Binance Futures with real money.")

        self.client.load_markets()
        self.market = self.client.market(cfg.symbol)
        self._setup_account()

    def _setup_account(self) -> None:
        try:
            self.client.set_margin_mode(self.cfg.margin_mode, self.cfg.symbol)
        except ccxt.BaseError as e:
            # Binance errors if the mode is already set; that's fine.
            log.debug("set_margin_mode: %s", e)
        try:
            self.client.set_leverage(self.cfg.leverage, self.cfg.symbol)
        except ccxt.BaseError as e:
            log.debug("set_leverage: %s", e)

    # ---- market data ----
    def fetch_closed_candles(self, limit: int = 200) -> pd.DataFrame:
        """OHLCV of *closed* candles only, oldest first."""
        raw = self.client.fetch_ohlcv(self.cfg.symbol, self.cfg.timeframe, limit=limit)
        df = pd.DataFrame(
            raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("timestamp")
        return df.iloc[:-1]  # drop the still-forming candle

    # ---- account ----
    def equity_usdt(self) -> float:
        balance = self.client.fetch_balance()
        return float(balance["USDT"]["total"])

    def current_position(self):
        """Return the open position dict for our symbol, or None if flat."""
        positions = self.client.fetch_positions([self.cfg.symbol])
        for pos in positions:
            if float(pos.get("contracts") or 0) != 0:
                return pos
        return None

    # ---- orders ----
    def amount_to_precision(self, amount: float) -> float:
        return float(self.client.amount_to_precision(self.cfg.symbol, amount))

    def open_bracket(self, side: str, amount: float, stop: float, take_profit: float):
        """Market entry plus reduce-only stop-loss and take-profit.

        `side` is "long" or "short". Returns the entry order.
        """
        order_side = "buy" if side == "long" else "sell"
        close_side = "sell" if side == "long" else "buy"

        entry = self.client.create_order(
            self.cfg.symbol, "market", order_side, amount
        )
        log.info("Entry %s %s %s @ market", order_side, amount, self.cfg.symbol)

        try:
            self.client.create_order(
                self.cfg.symbol, "market", close_side, amount, None,
                {
                    "stopPrice": self.client.price_to_precision(self.cfg.symbol, stop),
                    "type": "STOP_MARKET",
                    "reduceOnly": True,
                },
            )
            self.client.create_order(
                self.cfg.symbol, "market", close_side, amount, None,
                {
                    "stopPrice": self.client.price_to_precision(self.cfg.symbol, take_profit),
                    "type": "TAKE_PROFIT_MARKET",
                    "reduceOnly": True,
                },
            )
            log.info("Brackets set: SL %.2f / TP %.2f", stop, take_profit)
        except ccxt.BaseError:
            # If brackets fail we must not hold an unprotected position.
            log.exception("Failed to place SL/TP — closing position immediately.")
            self.close_position()
            raise
        return entry

    def cancel_all_orders(self) -> None:
        try:
            self.client.cancel_all_orders(self.cfg.symbol)
        except ccxt.BaseError:
            log.exception("cancel_all_orders failed")

    def close_position(self) -> None:
        pos = self.current_position()
        if pos is None:
            return
        side = "sell" if pos["side"] == "long" else "buy"
        amount = abs(float(pos["contracts"]))
        self.client.create_order(
            self.cfg.symbol, "market", side, amount, None, {"reduceOnly": True}
        )
        log.info("Closed position with market order.")

    def realized_pnl_since(self, since_ms: int) -> float:
        """Sum of realized PnL and fees from account income history."""
        params = {"incomeType": "REALIZED_PNL"}
        income = self.client.fetch_ledger(since=since_ms, params=params)
        pnl = sum(float(item["amount"]) for item in income)
        fees = self.client.fetch_ledger(
            since=since_ms, params={"incomeType": "COMMISSION"}
        )
        pnl += sum(float(item["amount"]) for item in fees)  # commissions are negative
        return pnl
