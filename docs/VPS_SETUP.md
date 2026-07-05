# Running the bot 24/7 on a cheap cloud server — the complete beginner guide

No coding knowledge needed. You will copy-paste commands, one block at a
time. Total time: about 30 minutes. Total cost: **$4–6 per month**.

The end result: the bot runs day and night in a datacenter, restarts itself
after crashes and reboots, and messages your phone on Telegram about every
trade. You can check status or emergency-stop it from anywhere by texting it.

---

## Step 1 — Rent the server (~5 min)

Any provider works. Two easy options:

- **Hetzner** (hetzner.com/cloud) — CX11/CPX11 plan, ~€4/month. Recommended.
- **DigitalOcean** (digitalocean.com) — Basic Droplet, $6/month.

When creating the server, choose:

- Image / OS: **Ubuntu 24.04**
- Size: the **smallest** (1 CPU, 1–2 GB RAM is plenty)
- Location: **Europe** (close to Binance's servers, but anywhere works)
- Authentication: set a **root password** you'll remember (or SSH key if
  offered and you know how)

When it's done you'll see the server's **IP address** (like `65.108.4.21`).
Write it down.

## Step 2 — Connect to the server (~2 min)

**From a laptop** (Mac/Windows/Linux — all have a terminal built in):

```
ssh root@YOUR_SERVER_IP
```

Type your password when asked. You're now "inside" the server — everything
you type next runs there, not on your laptop.

**From your phone**: install the free app **Termius** (iPhone/Android), add a
new host with your IP + user `root` + your password. Same terminal, in your
pocket.

## Step 3 — Install the bot (~5 min)

Paste these blocks one at a time:

```bash
apt update && apt install -y python3-pip git
```

```bash
git clone https://github.com/Ybekkouri/Binance.git /opt/binance-bot
cd /opt/binance-bot
pip3 install -r requirements.txt --break-system-packages
```

## Step 4 — Create your Telegram bot (~5 min, on your phone)

1. Open Telegram, search for **@BotFather**, press Start.
2. Send `/newbot`. Give it a name (e.g. "My Trading Bot") and a username
   (must end in `bot`, e.g. `yassine_trader_bot`).
3. BotFather replies with a **token** like `7123456789:AAF...xyz`. Copy it.
4. Open a chat with **your new bot** and send it any message (e.g. "hi") —
   this is required so it's allowed to message you back.
5. In a browser, open (replace TOKEN with yours):
   `https://api.telegram.org/botTOKEN/getUpdates`
   Find `"chat":{"id":123456789` in the response — that number is your
   **chat id**. Copy it.

## Step 5 — Configure the bot (~5 min)

Still on the server:

```bash
cd /opt/binance-bot
cp .env.example .env
nano .env
```

`nano` is a simple text editor. Fill in the four values (arrow keys to move,
then `Ctrl+O` `Enter` to save, `Ctrl+X` to exit):

```
BINANCE_API_KEY=...       <- from testnet.binancefuture.com to start
BINANCE_SECRET_KEY=...
TELEGRAM_BOT_TOKEN=...    <- from BotFather
TELEGRAM_CHAT_ID=...      <- from getUpdates
```

The default `config.yaml` runs in **testnet** mode (fake money) — the right
place to start. To run with no Binance account at all, edit `config.yaml`
and set `mode: paper`.

## Step 6 — Verify every connection (~2 min)

The bot ships with a checker that tests each link in the chain — config,
API keys, market data, account access, trading permission, Telegram — and
tells you exactly what to fix if something is wrong:

```bash
cd /opt/binance-bot && python3 check.py
```

You want to see `ALL CHECKS PASSED` (it also sends a test message to your
phone). On the testnet you can additionally prove order placement works
end-to-end — it places a far-away limit order and immediately cancels it:

```bash
python3 check.py --order
```

If anything shows ❌, read its `FIX:` line, fix it, and run the checker
again. Do not start the bot until everything passes.

## Step 7 — Test run (~2 min)

```bash
cd /opt/binance-bot && python3 main.py
```

Within a few seconds your phone should buzz: *"🤖 Engine started..."*.
Send it `/status` and it answers. When you're satisfied, stop it with
`Ctrl+C` (your phone gets a shutdown message too).

## Step 8 — Make it permanent (~3 min)

This makes the bot start automatically at boot and restart itself if it
ever crashes:

```bash
cp /opt/binance-bot/deploy/binance-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now binance-bot
```

That's it. Check on it anytime:

```bash
systemctl status binance-bot        # is it running?
journalctl -u binance-bot -f        # live logs (Ctrl+C to leave)
systemctl restart binance-bot       # restart it
systemctl stop binance-bot          # stop it
```

You can now close everything. The bot lives in the datacenter; your phone
gets every trade.

---

## Everyday phone usage

- **`/status`** — positions, equity, today's PnL, anytime.
- **`/kill`** — emergency stop: cancels orders, closes positions, shuts the
  engine down. (Restart later with `systemctl start binance-bot`.)
- The bot messages you on: startup, every real trade opened and closed,
  daily profit target / loss limit reached, loss-streak cooldowns, market
  data outages, and shutdown.

## Updating the bot when the code changes

```bash
cd /opt/binance-bot && git pull && systemctl restart binance-bot
```

## Security — do these, they take 5 minutes

1. **Binance API key permissions**: enable *futures trading* only. NEVER
   enable withdrawals. The key can trade but can never move money out.
2. **Restrict the API key to your server's IP** (option in Binance's API
   management page). Then the key is useless to anyone who steals it.
3. Use a strong root password (or SSH keys if you know how).
4. Keep the server updated now and then: `apt update && apt upgrade -y`.

## Going live later (only after weeks of good testnet results)

1. Create real Binance API keys (futures-only, IP-restricted, no withdrawal).
2. Put them in `.env`, set `mode: live` in `config.yaml`.
3. systemd can't type a confirmation, so you must explicitly add the line
   `Environment=BOT_CONFIRM_LIVE=yes` in
   `/etc/systemd/system/binance-bot.service` (it's there, commented out),
   then `systemctl daemon-reload && systemctl restart binance-bot`.
   That extra step is deliberate — going live should never happen by accident.

## Running the research tools on the server

```bash
cd /opt/binance-bot
python3 research.py report            # the complete trade analysis
python3 research.py history --symbol BTC/USDT   # deep pair history
python3 backtest.py --days 90 --equity 1000     # strategy backtest
```
