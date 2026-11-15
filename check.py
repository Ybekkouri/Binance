"""Preflight check: verify every connection before running the bot.

    python3 check.py            # checks config, keys, data, account, telegram
    python3 check.py --order    # ALSO does a test order round-trip
                                # (Binance testnet; on MEXC it asks first,
                                # because MEXC has no testnet)

Each check prints a clear PASS/FAIL with a plain-language fix. Run this after
any setup change; when everything passes, the bot is safe to start.
"""

import argparse
import sys

PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "

results = {"fail": 0, "warn": 0}


def ok(msg):
    print(f"{PASS} {msg}")


def bad(msg, fix):
    results["fail"] += 1
    print(f"{FAIL} {msg}\n   FIX: {fix}")


def warn(msg):
    results["warn"] += 1
    print(f"{WARN}{msg}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--order", action="store_true",
                        help="test order round-trip (Binance testnet; on "
                             "MEXC live it asks for confirmation)")
    args = parser.parse_args()

    print("Trading bot preflight check\n" + "=" * 40)

    # 1 ---- config loads
    try:
        from bot.config import load_config
        cfg = load_config(args.config, require_keys=False)
        ok(f"config loads (exchange: {cfg.exchange_name}, mode: {cfg.mode}, "
           f"symbols: {', '.join(cfg.symbols)})")
    except SystemExit as e:
        bad(f"config problem: {e}", "fix config.yaml and retry")
        return finish()
    except Exception as e:                                   # noqa: BLE001
        bad(f"config.yaml failed to load: {e}",
            "restore config.yaml from the repository")
        return finish()

    if cfg.mode == "live":
        warn("mode is LIVE — this checker will read your REAL account "
             "(it never places live orders).")

    from bot.config import EXCHANGES
    name, key_var, secret_var = EXCHANGES[cfg.exchange]
    is_mexc = cfg.exchange == "mexc"

    # 2 ---- API keys present
    needs_keys = cfg.mode != "paper"
    if not needs_keys:
        ok(f"mode is paper — no {name} keys needed")
    elif cfg.api_key and cfg.api_secret:
        ok(f"{key_var} and {secret_var} found in .env")
    elif is_mexc:
        bad("MEXC API keys missing",
            "add MEXC_API_KEY and MEXC_SECRET_KEY to the .env file in the "
            "bot's folder (keys from mexc.com with Futures trading enabled "
            "and withdrawals DISABLED)")
    else:
        bad("Binance API keys missing",
            "re-run the installer and enter your keys, or edit "
            "/opt/binance-bot/.env "
            "(testnet keys from https://testnet.binancefuture.com)")

    # 3 ---- public market data
    try:
        from bot.market_data import MarketData
        market = MarketData(cfg)
        ticker = market.ticker(cfg.symbols[0])
        ok(f"market data works — {cfg.symbols[0]} last price "
           f"{float(ticker['last']):,.2f}")
        data_ok = True
    except Exception as e:                                   # noqa: BLE001
        bad(f"cannot reach {name} market data: {e}",
            f"check the server's internet connection / firewall; {name} may "
            "also be geo-blocked in some regions")
        data_ok = False

    # 4 ---- authenticated account access
    broker = None
    if needs_keys and cfg.api_key and data_ok:
        try:
            from bot.broker import make_live_broker
            broker = make_live_broker(cfg)
            equity = broker.equity_usdt()
            ok(f"account access works — futures balance {equity:,.2f} USDT "
               f"({'TESTNET' if cfg.mode == 'testnet' else 'LIVE'})")
            if equity <= 0:
                warn("balance is 0 — testnet accounts start with play money; "
                     "on live, transfer USDT into the FUTURES wallet "
                     f"({name} app: Wallet -> Transfer -> Futures)")
        except Exception as e:                               # noqa: BLE001
            bad(f"API keys rejected: {e}",
                ("wrong key/secret, or the key lacks Futures permission "
                 "(create it on mexc.com with Futures trading enabled). "
                 if is_mexc else
                 "wrong key/secret, or keys from the wrong place: testnet "
                 "mode needs keys from testnet.binancefuture.com, live mode "
                 "needs keys from binance.com with Futures permission "
                 "enabled. ")
                + "If you IP-restricted the key, the restriction must "
                  "include THIS machine's IP")
    elif cfg.mode == "paper":
        try:
            from bot.broker import PaperBroker
            paper = PaperBroker(cfg)
            ok(f"paper account ready — virtual balance "
               f"{paper.equity_usdt():,.2f} USDT")
        except Exception as e:                               # noqa: BLE001
            bad(f"paper broker failed: {e}", "check file permissions in the "
                "bot directory")

    # 5 ---- symbol setup (leverage/margin) — proves trading permission
    if broker is not None:
        try:
            broker.setup_symbol(cfg.symbols[0])
            if is_mexc:
                ok("MEXC account is in Hedge position mode (required). "
                   "Trading permission is only proven by: "
                   "python3 check.py --order")
            else:
                ok(f"trading permission confirmed — leverage/margin set on "
                   f"{cfg.symbols[0]}")
        except Exception as e:                               # noqa: BLE001
            bad(f"cannot configure {cfg.symbols[0]}: {e}",
                "the API key likely lacks 'Enable Futures' permission — "
                "recreate it with Futures trading enabled (and NOTHING else)")

    # 6 ---- optional order round-trip
    if args.order and is_mexc:
        if cfg.mode != "live" or broker is None:
            warn("--order skipped: on MEXC it needs live mode with working keys")
        else:
            mexc_order_test(cfg, market, broker)
    elif args.order:
        if cfg.mode != "testnet" or broker is None:
            warn("--order skipped: only allowed in testnet mode with working keys")
        else:
            symbol = cfg.symbols[0]
            last = float(market.ticker(symbol)["last"])
            try:
                price = broker.price_to_precision(symbol, last * 0.5)
                amount = broker.amount_to_precision(
                    symbol, max(120.0 / price, 0.001))
                order = broker.client.create_order(
                    symbol, "limit", "buy", amount, price)
                broker.client.cancel_order(order["id"], symbol)
                ok("regular order round-trip works (limit placed + cancelled)")
            except Exception as e:                           # noqa: BLE001
                bad(f"test order failed: {e}",
                    "check futures permission and testnet balance")
            # Conditional (trigger) orders are the path real stop-loss /
            # take-profit brackets use — test it explicitly.
            try:
                trigger = broker.price_to_precision(symbol, last * 0.3)
                sl = broker.client.create_order(
                    symbol, "market", "sell", None, None,
                    {"stopLossPrice": trigger, "closePosition": True})
                broker.client.cancel_order(sl["id"], symbol, {"trigger": True})
                ok("conditional order round-trip works (bracket-style stop "
                   "placed + cancelled)")
            except Exception as e:                           # noqa: BLE001
                bad(f"conditional (stop/TP) test order failed: {e}",
                    "this is the exact mechanism real brackets use — do NOT "
                    "go live until this passes on the testnet")

    # 7 ---- telegram
    if not cfg.telegram_enabled:
        warn("telegram disabled in config.yaml — you'll have no phone alerts")
    elif not (cfg.telegram_token and cfg.telegram_chat_id):
        # skipping Telegram is a valid choice, not a failure
        warn("no Telegram token/chat id in .env — the bot will run but "
             "you'll get no phone alerts. To add them later, re-run the "
             "installer (see START_HERE.md)")
    else:
        try:
            from bot.notify import Telegram
            tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id)
            resp = tg._api("sendMessage", {
                "chat_id": tg.chat_id,
                "text": "✅ Connection test from your trading bot — "
                        "if you can read this, Telegram works."})
            if resp.get("ok"):
                ok("telegram works — check your phone for the test message")
            else:
                bad(f"telegram API said: {resp.get('description', resp)}",
                    "token or chat id is wrong; redo the BotFather steps in "
                    "docs/VPS_SETUP.md and make sure you messaged your bot once")
        except Exception as e:                               # noqa: BLE001
            bad(f"telegram unreachable: {tg.redact(e)}",
                "check internet connection")

    finish()


def mexc_order_test(cfg, market, broker) -> None:
    """MEXC has no testnet, so this runs on the REAL account: it places the
    smallest possible orders far from the market and cancels them at once.
    Nothing is meant to fill; it proves the key can place and cancel both
    order kinds the bot uses (regular and trigger). Only the two test
    orders are cancelled — never anything else on the account."""
    symbol = cfg.symbols[0]
    if any(p["symbol"] == symbol for p in broker.open_positions()):
        warn(f"--order skipped: there is an open {symbol} position. Run "
             "this test only while the bot has no position.")
        return
    print(f"\nThis places two tiny {symbol} orders on your REAL MEXC account,"
          "\nfar from the market price, and cancels them immediately.")
    try:
        answer = input("Type 'yes' to run this test: ")
    except EOFError:
        answer = ""
    if answer.strip().lower() != "yes":
        warn("--order skipped (not confirmed)")
        return
    ex_symbol = broker._s(symbol)
    last = float(market.ticker(symbol)["last"])
    one = broker._contract_size(symbol) * float(
        broker._market(symbol)["limits"]["amount"]["min"] or 1)
    vol = broker._vol(symbol, one)
    try:
        # MEXC rejects limit prices more than ~10% from the market
        price = broker.price_to_precision(symbol, last * 0.92)
        order = broker.client.create_order(
            ex_symbol, "limit", "buy", vol, price, broker._params())
        broker.client.cancel_order(order["id"], ex_symbol)
        ok("regular order round-trip works (limit placed + cancelled)")
    except Exception as e:                                   # noqa: BLE001
        bad(f"test order failed: {e}",
            "check that the key has Futures trading enabled and that there "
            "is USDT in the MEXC Futures wallet")
    try:
        trigger = broker.price_to_precision(symbol, last * 1.5)
        plan = broker.client.create_order(
            ex_symbol, "market", "buy", vol, None,
            broker._params(triggerPrice=trigger,
                           triggerDirection="ascending", executeCycle=1))
        broker.client.cancel_order(plan["id"], ex_symbol, {"trigger": True})
        ok("trigger order round-trip works (placing and cancelling the "
           "order kind that stop-loss and take-profit use)")
    except Exception as e:                                   # noqa: BLE001
        bad(f"trigger (stop/TP) test order failed: {e}",
            "this is the mechanism real stop-losses use — do NOT trade "
            "real money on MEXC until this passes")


def finish() -> None:
    print("=" * 40)
    if results["fail"] == 0:
        print(f"{PASS} ALL CHECKS PASSED"
              + (f" ({results['warn']} warning(s) above)" if results["warn"] else "")
              + " — you can start the bot: python3 main.py")
        sys.exit(0)
    print(f"{FAIL} {results['fail']} problem(s) found — fix them (see FIX "
          "lines above) and run: python3 check.py")
    sys.exit(1)


if __name__ == "__main__":
    main()
