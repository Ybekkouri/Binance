"""Binance USDS-M exchangeInfo fixture (shape of the real API response)."""

import json

def symbol(sym, base, tick="0.10", step="0.001", min_notional="100"):
    return {
        "symbol": sym, "pair": sym, "contractType": "PERPETUAL",
        "deliveryDate": 4133404800000, "onboardDate": 1569398400000,
        "status": "TRADING", "maintMarginPercent": "2.5000",
        "requiredMarginPercent": "5.0000", "baseAsset": base,
        "quoteAsset": "USDT", "marginAsset": "USDT", "pricePrecision": 2,
        "quantityPrecision": 3, "baseAssetPrecision": 8, "quotePrecision": 8,
        "underlyingType": "COIN", "underlyingSubType": ["PoW"],
        "settlePlan": 0, "triggerProtect": "0.0500",
        "liquidationFee": "0.012500", "marketTakeBound": "0.05",
        "maxMoveOrderLimit": 10000,
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": "556.80",
             "maxPrice": "4529764", "tickSize": tick},
            {"filterType": "LOT_SIZE", "minQty": step, "maxQty": "1000",
             "stepSize": step},
            {"filterType": "MARKET_LOT_SIZE", "minQty": step,
             "maxQty": "120", "stepSize": step},
            {"filterType": "MAX_NUM_ORDERS", "limit": 200},
            {"filterType": "MAX_NUM_ALGO_ORDERS", "limit": 10},
            {"filterType": "MIN_NOTIONAL", "notional": min_notional},
            {"filterType": "PERCENT_PRICE", "multiplierUp": "1.0500",
             "multiplierDown": "0.9500", "multiplierDecimal": "4"},
        ],
        "orderTypes": ["LIMIT", "MARKET", "STOP", "STOP_MARKET",
                       "TAKE_PROFIT", "TAKE_PROFIT_MARKET",
                       "TRAILING_STOP_MARKET"],
        "timeInForce": ["GTC", "IOC", "FOK", "GTX", "GTD"],
    }

EXCHANGE_INFO = {"timezone": "UTC", "serverTime": 1700000000000,
                 "rateLimits": [], "exchangeFilters": [], "assets": [],
                 "symbols": [symbol("BTCUSDT", "BTC"),
                             symbol("ETHUSDT", "ETH", tick="0.01",
                                    step="0.001", min_notional="20")]}
