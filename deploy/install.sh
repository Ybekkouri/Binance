#!/usr/bin/env bash
# ============================================================================
# One-command installer AND updater for the trading bot (Ubuntu, as root).
#
#   curl -fsSL https://raw.githubusercontent.com/Ybekkouri/Binance/main/deploy/install.sh | bash
#
# It installs dependencies, downloads (or updates) the bot, asks a few
# questions (Enter skips Telegram; Enter on the Binance key = paper mode),
# verifies every connection, and (re)starts the bot as a service that
# survives reboots. Safe to re-run anytime: it updates the code, offers to
# keep your settings, and restarts the bot — including after a /kill.
# ============================================================================
# Everything lives in main() so a half-downloaded script never runs partially.
main() {
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Ybekkouri/Binance.git}"
DIR="${INSTALL_DIR:-/opt/binance-bot}"
# Under `curl | bash`, stdin is the script itself: questions read the terminal.
TTY=/dev/tty
export DEBIAN_FRONTEND=noninteractive

say()  { echo -e "\n\033[1m$*\033[0m"; }
ask()  { local __v; printf "%s" "$2"; read -r __v < "$TTY" || __v=""; printf -v "$1" "%s" "$__v"; }
ask_secret() { local __v; printf "%s" "$2"; read -rs __v < "$TTY" || __v=""; echo; printf -v "$1" "%s" "$__v"; }
# set_env KEY VALUE: replace or append a line in .env
set_env() {
    touch .env
    if grep -q "^$1=" .env; then
        python3 - "$1" "$2" <<'PY'
import sys
k, v = sys.argv[1], sys.argv[2]
lines = open(".env").read().splitlines()
out = [f"{k}={v}" if l.startswith(k + "=") else l for l in lines]
open(".env", "w").write("\n".join(out) + "\n")
PY
    else
        echo "$1=$2" >> .env
    fi
}
get_env() { grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2- || true; }

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run as root (log in as root, or prefix with: sudo bash)"
    exit 1
fi

say "1/6 Installing system packages (python, git)..."
echo "(on a brand-new server this can wait a few minutes for Ubuntu's own"
echo " first-boot updates to finish — that's normal)"
APT="apt-get -o DPkg::Lock::Timeout=600 -y -q"
$APT update < /dev/null
$APT install python3-pip git curl < /dev/null

say "2/6 Downloading the bot to $DIR..."
if [ -d "$DIR/.git" ]; then
    cd "$DIR"
    if ! git diff --quiet; then
        # Older setups edited config.yaml to change mode. Keep that choice
        # (moved into .env) and set the edits aside so the update can apply.
        OLD_MODE="$(grep -m1 '^mode:' config.yaml | awk '{print $2}' || true)"
        git stash push -q -m "installer backup $(date +%F)" || true
        echo "Your local edits were saved aside (git stash) so the update can apply."
        if [ -n "${OLD_MODE:-}" ] && [ -f .env ] && [ -z "$(get_env BOT_MODE)" ]; then
            set_env BOT_MODE "$OLD_MODE"
        fi
    fi
    git pull -q --ff-only
else
    git clone -q "$REPO_URL" "$DIR"
    cd "$DIR"
fi

say "3/6 Installing Python packages..."
pip3 install -q -r requirements.txt --break-system-packages

say "4/6 Settings..."
CONFIGURE=yes
if [ -f .env ]; then
    echo "Current settings:"
    [ -n "$(get_env TELEGRAM_BOT_TOKEN)" ] && echo "  Telegram: set" || echo "  Telegram: not set"
    [ -n "$(get_env BINANCE_API_KEY)" ] && echo "  Binance keys: set" || echo "  Binance keys: not set"
    echo "  Mode: $(get_env BOT_MODE)"
    ask KEEP "Keep these settings? [Y/n] "
    case "${KEEP:-y}" in [nN]*) CONFIGURE=yes ;; *) CONFIGURE=no ;; esac
fi

if [ "$CONFIGURE" = yes ]; then
    touch .env
    chmod 600 .env

    # --- Telegram -------------------------------------------------------
    echo ""
    echo "TELEGRAM (your phone becomes the dashboard). Create a bot with"
    echo "@BotFather first — see docs/VPS_SETUP.md Step 4. Enter skips it."
    while true; do
        ask_secret TG_TOKEN "Telegram bot token (hidden as you type): "
        if [ -z "$TG_TOKEN" ]; then
            set_env TELEGRAM_BOT_TOKEN ""; set_env TELEGRAM_CHAT_ID ""
            echo "Skipping Telegram — the bot runs without phone alerts."
            break
        fi
        BOT_NAME="$(curl -fsS "https://api.telegram.org/bot${TG_TOKEN}/getMe" 2>/dev/null \
            | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["result"]["username"] if d.get("ok") else "")' 2>/dev/null || true)"
        if [ -z "$BOT_NAME" ]; then
            echo "That token was not accepted by Telegram — please paste it again."
            continue
        fi
        echo "Token OK — your bot is @$BOT_NAME"
        echo "Now open Telegram, send any message (e.g. hi) to @$BOT_NAME,"
        ask _ "then press Enter here... "
        TG_CHAT="$(curl -fsS "https://api.telegram.org/bot${TG_TOKEN}/getUpdates" 2>/dev/null \
            | python3 -c 'import sys,json; r=json.load(sys.stdin).get("result",[]); ids=[u["message"]["chat"]["id"] for u in r if "message" in u]; print(ids[-1] if ids else "")' 2>/dev/null || true)"
        if [ -z "$TG_CHAT" ]; then
            echo "No message found yet. Send your bot a message, then try again."
            ask TG_CHAT "(or type your chat id manually): "
        else
            echo "Found your chat (id $TG_CHAT)."
        fi
        set_env TELEGRAM_BOT_TOKEN "$TG_TOKEN"
        set_env TELEGRAM_CHAT_ID "$TG_CHAT"
        break
    done

    # --- Binance --------------------------------------------------------
    echo ""
    echo "BINANCE. Start with TESTNET keys (fake money) from"
    echo "https://testnet.binancefuture.com — or press Enter for PAPER mode"
    echo "(simulated money, real market data, no account needed)."
    ask_secret B_KEY "Binance TESTNET API key (hidden): "
    if [ -n "$B_KEY" ]; then
        ask_secret B_SECRET "Binance TESTNET API secret (hidden): "
        set_env BINANCE_API_KEY "$B_KEY"
        set_env BINANCE_SECRET_KEY "$B_SECRET"
        set_env BOT_MODE testnet
        echo "Saved — mode: TESTNET."
    else
        set_env BINANCE_API_KEY ""; set_env BINANCE_SECRET_KEY ""
        set_env BOT_MODE paper
        echo "Mode: PAPER. Re-run this installer anytime to add keys."
    fi
fi
chmod 600 .env

say "5/6 Verifying every connection..."
if ! python3 check.py; then
    echo ""
    echo "Some checks failed — read the FIX lines above, then re-run the"
    echo "same install line and answer 'n' to change your settings."
    exit 1
fi

say "6/6 Starting the 24/7 service..."
cp deploy/binance-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q binance-bot
if [ -f STOPPED ]; then
    echo "The bot had been stopped with /kill — starting it again."
    rm -f STOPPED KILL
fi
# restart (not just start): an update must actually load the new code
systemctl restart binance-bot

echo ""
echo "=============================================================="
echo " DONE. The bot is running and will survive reboots and crashes."
echo ""
echo "   Your phone should buzz shortly: 'Engine started'."
echo "   Text /status to it anytime, /kill for emergency stop."
echo ""
echo "   To update, change settings, or restart after /kill, just run"
echo "   the same install line again."
echo ""
echo "   Useful commands on this server:"
echo "     systemctl status binance-bot      is it running?"
echo "     journalctl -u binance-bot -f      live logs"
echo "=============================================================="
}
main "$@"
