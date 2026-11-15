"""MEXC market fixtures (shape of the real API responses)."""


def contract(sym, base, size, tick):
    return {
        "symbol": sym, "displayName": sym, "displayNameEn": f"{sym} SWAP",
        "positionOpenType": 3, "baseCoin": base, "quoteCoin": "USDT",
        "settleCoin": "USDT", "contractSize": size, "minLeverage": 1,
        "maxLeverage": 125, "priceScale": 2, "volScale": 0, "amountScale": 4,
        "priceUnit": tick, "volUnit": 1, "minVol": 1, "maxVol": 1000000,
        "bidLimitPriceRate": 0.1, "askLimitPriceRate": 0.1,
        "takerFeeRate": 0.0006, "makerFeeRate": 0.0002,
        "maintenanceMarginRate": 0.004, "initialMarginRate": 0.008,
        "riskBaseVol": 10000, "riskIncrVol": 200000, "riskIncrMmr": 0.004,
        "riskIncrImr": 0.004, "riskLevelLimit": 5,
        "priceCoefficientVariation": 0.1, "indexOrigin": ["BINANCE"],
        "state": 0, "isNew": False, "isHot": True, "isHidden": False,
        "apiAllowed": True,
    }


CONTRACTS = {"success": True, "code": 0, "data": [
    contract("BTC_USDT", "BTC", 0.0001, 0.1),
    contract("ETH_USDT", "ETH", 0.01, 0.01),
]}

# A SPOT market with the same name: the bot must never pick this one.
SPOT = {"timezone": "CST", "serverTime": 1700000000000, "rateLimits": [],
        "exchangeFilters": [], "symbols": [{
            "symbol": "BTCUSDT", "status": "1", "baseAsset": "BTC",
            "baseAssetPrecision": 6, "quoteAsset": "USDT",
            "quotePrecision": 2, "quoteAssetPrecision": 2,
            "baseCommissionPrecision": 6, "quoteCommissionPrecision": 2,
            "orderTypes": ["LIMIT", "MARKET", "LIMIT_MAKER"],
            "isSpotTradingAllowed": True, "isMarginTradingAllowed": False,
            "quoteAmountPrecision": "1", "baseSizePrecision": "0.000001",
            "permissions": ["SPOT"], "filters": [],
            "maxQuoteAmount": "2000000", "makerCommission": "0",
            "takerCommission": "0.0005", "quoteAmountPrecisionMarket": "1",
            "maxQuoteAmountMarket": "100000", "fullName": "Bitcoin",
            "tradeSideType": 1}]}
