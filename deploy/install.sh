#!/usr/bin/env bash
# ============================================================================
# One-command installer for the trading bot (Ubuntu, run as root).
#
#   curl -fsSL https://raw.githubusercontent.com/Ybekkouri/Binance/main/deploy/install.sh | bash
#
# or, if you already cloned the repo:
#
#   bash /opt/binance-bot/deploy/install.sh
#
# It installs dependencies, downloads/updates the bot, asks you 3 questions
# (Telegram token, Telegram chat id, Binance keys — Enter to skip and run in
# paper mode), verifies every connection, and starts the bot as a service
# that survives reboots. Safe to re-run anytime: it updates code and keeps
# your existing answers.
# ============================================================================
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Ybekkouri/Binance.git}"
DIR="${INSTALL_DIR:-/opt/binance-bot}"
# When piped through `curl | bash`, stdin is the script itself — questions
# must read from the terminal directly.
TTY=/dev/tty

say()  { echo -e "\n\033[1m$*\033[0m"; }

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run as root (log in as root, or prefix with: sudo bash)"
    exit 1
fi

say "1/6 Installing system packages (python, git)..."
apt-get update -qq
apt-get install -yqq python3-pip git >/dev/null

say "2/6 Downloading the bot to $DIR..."
if [ -d "$DIR/.git" ]; then
    git -C "$DIR" pull --ff-only
else
    git clone "$REPO_URL" "$DIR"
fi
cd "$DIR"

say "3/6 Installing Python packages..."
pip3 install -q -r requirements.txt --break-system-packages

say "4/6 Configuration..."
if [ -f .env ]; then
    echo "Found existing .env — keeping your settings."
else
    echo "Three questions. Telegram makes your phone the dashboard"
    echo "(create a bot with @BotFather first — see docs/VPS_SETUP.md Step 4)."
    printf "\nTelegram bot token (Enter to skip Telegram): "
    read -r TG_TOKEN < "$TTY" || TG_TOKEN=""
    TG_CHAT=""
    if [ -n "$TG_TOKEN" ]; then
        printf "Telegram chat id: "
        read -r TG_CHAT < "$TTY" || TG_CHAT=""
    fi
    printf "Binance API key (Enter to skip and trade in PAPER mode): "
    read -r B_KEY < "$TTY" || B_KEY=""
    B_SECRET=""
    if [ -n "$B_KEY" ]; then
        printf "Binance API secret: "
        read -r B_SECRET < "$TTY" || B_SECRET=""
    fi
    cat > .env <<EOF
BINANCE_API_KEY=$B_KEY
BINANCE_SECRET_KEY=$B_SECRET
TELEGRAM_BOT_TOKEN=$TG_TOKEN
TELEGRAM_CHAT_ID=$TG_CHAT
EOF
    chmod 600 .env
    if [ -z "$B_KEY" ]; then
        sed -i 's/^mode: .*/mode: paper/' config.yaml
        echo "No Binance keys -> running in PAPER mode (simulated money,"
        echo "real market data). Re-run this installer anytime to add keys."
    fi
fi

say "5/6 Verifying every connection..."
if python3 check.py; then
    echo "All good."
else
    echo ""
    echo "Some checks failed — follow the FIX lines above, then re-run:"
    echo "  bash $DIR/deploy/install.sh"
    exit 1
fi

say "6/6 Installing the 24/7 service..."
cp deploy/binance-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now binance-bot

echo ""
echo "=============================================================="
echo " DONE. The bot is running and will survive reboots and crashes."
echo ""
echo "   Your phone should buzz shortly: 'Engine started'."
echo "   Text /status to it anytime, /kill for emergency stop."
echo ""
echo "   Useful commands on this server:"
echo "     systemctl status binance-bot      is it running?"
echo "     journalctl -u binance-bot -f      live logs"
echo "     bash $DIR/deploy/install.sh       update to latest version"
echo "=============================================================="
