# START HERE — Your complete guide (no coding knowledge needed)

This is the only file you need to read. It takes you from nothing to a
trading bot that runs itself day and night, messages your phone about every
trade, and can be stopped from anywhere with one text message.

Follow the phases **in order**. Don't skip ahead — each phase proves the
previous one worked. Where you see a grey box like this:

```bash
python3 check.py
```

it's a command: you copy it, paste it into the black terminal window, and
press Enter. That's the only "coding" you will ever do.

---

## What you actually have here (in plain words)

A disciplined, automatic futures trader. Every 30 seconds it looks at the
market from eleven different angles (trend, momentum, volume, how crowded
the trade is, and more). Only when enough evidence points the same way does
it trade — with a safety stop placed on Binance itself the moment it enters,
so even if the bot loses power mid-trade, the position is protected.

It refuses to trade more than 6 times a day, stops for the day after
winning +2% or losing −1.5%, stops for the week if things go badly, and
never risks more than 0.5% of the account on a single idea. **Most hours it
does nothing — that's the discipline working, not a bug.**

It also keeps a second, virtual trader running alongside (the "shadow
track") that trades pretend money on real prices to gather learning data
faster, and it writes every decision to a database you can analyze later.

It has ears on the world, too — the smart way: big events (crashes, hacks,
surprise announcements) hit prices within seconds, faster than any news
site. When the bot sees a violently abnormal candle it stands aside for an
hour and messages you. And for events you know are coming (US Fed decisions,
inflation reports), you can list "blackout windows" in the settings during
which it simply won't open trades.

## The safety ladder — where you are at each phase

| Phase | Money at risk | What it proves |
|---|---|---|
| 1. Paper | none, no Binance account needed | the bot runs and behaves |
| 2. Testnet | none — Binance's fake-money playground | orders really work end to end |
| 3. Live | real, starting small | everything, for real |

You should spend **days** in phase 1, **weeks** in phase 2, and only then
consider phase 3.

---

# PHASE 0 — What you need (one-time, ~1 hour total)

1. **A cloud server** (~$4–6/month). This is a tiny computer in a
   datacenter that stays on 24/7 so your laptop doesn't have to.
   → Follow **Step 1 and Step 2** of [docs/VPS_SETUP.md](docs/VPS_SETUP.md)
   — it shows exactly which buttons to click at Hetzner or DigitalOcean and
   how to connect from your laptop or phone.

2. **The bot installed on it** → **Step 3** of the same guide
   (two copy-paste blocks).

3. **Your Telegram bot** (free, 5 minutes, done on your phone) →
   **Step 4** of the same guide. At the end you'll have two codes:
   a *token* and a *chat id*. Keep them handy.

---

# PHASE 1 — Paper mode: watch it work, risk nothing

No Binance account needed. The bot trades a pretend 1,000 USDT on live
market prices.

**1.** Connect to your server (Termius app on your phone, or `ssh` from a
laptop) and open the bot's settings file:

```bash
cd /opt/binance-bot && nano config.yaml
```

**2.** Near the top, find the line `mode: testnet` and change it to
`mode: paper`. Save and exit: press `Ctrl+O`, `Enter`, then `Ctrl+X`.

**3.** Add your Telegram codes:

```bash
cp .env.example .env && nano .env
```

Fill in the two Telegram lines (leave the Binance ones empty for now).
Save and exit the same way.

**4.** Check everything:

```bash
python3 check.py
```

You want green checkmarks and a test message on your phone. Any ❌ comes
with a `FIX:` line telling you what to do.

**5.** Make it run forever (auto-starts after reboots and crashes):

```bash
cp deploy/binance-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now binance-bot
```

Your phone buzzes: *"🤖 Engine started (paper)"*. **You're done.** Close
everything and live your life — the bot reports to your phone.

**What to expect:** possibly nothing for hours or even a day or two. The
strict trader waits for real alignment. The shadow trader will act more
often. Text `/status` to the bot anytime to see equity and positions.

---

# PHASE 2 — Testnet: real orders, fake money

This proves the full machinery — orders, stops, take-profits — on Binance's
practice system.

**1.** On your phone or laptop, go to **testnet.binancefuture.com** and log
in (Google/GitHub login works). You get ~15,000 fake USDT automatically.

**2.** On that page find your **API Key** and **API Secret** (bottom panel,
"API Key" tab). These are the codes that let the bot trade *that* account.

**3.** On the server, put them into the settings:

```bash
cd /opt/binance-bot && nano .env
```

Fill in `BINANCE_API_KEY=` and `BINANCE_SECRET_KEY=` with the testnet
codes. Save, exit.

**4.** Switch the mode back:

```bash
nano config.yaml
```

Change `mode: paper` to `mode: testnet`. Save, exit.

**5.** Verify — including a real order test (places a far-away order and
cancels it instantly; this is the moment you *know* the connection works):

```bash
python3 check.py --order
```

**6.** Restart the bot with the new settings:

```bash
systemctl restart binance-bot
```

Phone buzzes: *"🤖 Engine started (testnet)"*. Leave it running for
**at least 2–4 weeks**.

---

# YOUR ROUTINE while it runs (5 minutes a day, 15 a week)

**Daily** — just read your Telegram. Every trade arrives as a message.
Curious? Text `/status`.

**Weekly** — connect to the server and ask the bot what it has learned:

```bash
cd /opt/binance-bot
python3 research.py report
```

This prints the full analysis. You don't need to understand every number.
Look for three things:

1. **Trades count** — is data accumulating? (The shadow track speeds this up.)
2. **Confidence calibration** — the report literally tells you in a
   sentence whether the bot's confidence score is meaningful yet.
3. **Stars (`*`)** — anything starred is a real, statistically-proven
   pattern. No stars yet? Normal. Keep collecting.

Before trading a new coin regularly, check its whole life story first:

```bash
python3 research.py history --symbol ETH/USDT
```

**Updating the bot** when improvements are pushed to GitHub:

```bash
cd /opt/binance-bot && git pull && systemctl restart binance-bot
```

---

# PHASE 3 — Live (only after weeks of green testnet results)

**The go-live checklist — every box, no exceptions:**

- [ ] The testnet ran 2+ weeks without technical problems
- [ ] `research.py report` shows results you'd accept with real money
- [ ] You're funding it with money you can genuinely afford to lose
- [ ] You've read the Security section of docs/VPS_SETUP.md

**1.** On **binance.com**: profile icon → **API Management** → Create API.
   - Tick **Enable Futures**. Nothing else. **Never enable withdrawals** —
     this makes it impossible for the key (or anyone who steals it) to take
     money out of your account.
   - Choose **Restrict access to trusted IPs** and enter your server's IP.
     Now the key only works from your server.

**2.** Move your trading money into the **Futures wallet**: Binance app →
Wallets → Transfer → from Spot to USDⓈ-M Futures. Start small — even
200–500 USDT. The bot's percentages adapt to any size.

**3.** On the server: put the real keys in `.env` (same as before), and in
`config.yaml` change mode to `live`.

**4.** Run `python3 check.py` — it must pass and show your real balance.

**5.** Going live is deliberately hard to do by accident. Open the service
file and remove the `#` in front of the marked line:

```bash
nano /etc/systemd/system/binance-bot.service
```

Find `# Environment=BOT_CONFIRM_LIVE=yes` and delete the leading `# `.
Save, exit, then:

```bash
systemctl daemon-reload && systemctl restart binance-bot
```

Phone: *"🤖 Engine started (live)"*. You are trading. The same daily loss
caps, weekly halts, and per-trade limits that protected the fake money now
protect the real money.

---

# If something goes wrong

| Problem | What to do |
|---|---|
| Want everything stopped NOW | Text **`/kill`** to your bot. Orders cancelled, positions closed, engine off. |
| Phone silent for days | Normal if no trades fired. Text `/status`. No reply? See next row. |
| Bot not replying | On the server: `systemctl status binance-bot`. If it says "inactive": `systemctl start binance-bot`. |
| Any ❌ anywhere | `cd /opt/binance-bot && python3 check.py` and follow its FIX lines. |
| What is it doing right now? | `journalctl -u binance-bot -f` shows the live log (Ctrl+C to leave). |
| Server rebooted | Nothing to do — the bot auto-starts and remembers its daily limits. |
| Want to start over in paper mode | Set `mode: paper` in config.yaml, `systemctl restart binance-bot`. |
| Locked out / lost the server | Your money is safe on Binance, not the server. Positions keep their exchange-side stops. Log into Binance directly to manage them; delete the API key to cut the bot off entirely. |

**The two numbers that matter if you ever panic:** your money lives on
*Binance*, not on the server — and every position always has a stop-loss
*on Binance itself*. Killing the server never leaves a position naked.

---

# Glossary (the only jargon you'll meet)

- **API key/secret** — two codes that let a program trade your account
  (with only the permissions you chose). Like a valet key: drives the car,
  can't open the trunk.
- **Futures** — contracts that let you profit from prices falling (short)
  as well as rising (long). More flexible, more dangerous than spot.
- **Leverage** — trading with borrowed size. 3x means a 1% market move is
  ~3% for you, both directions. The bot keeps it low deliberately.
- **Stop-loss / take-profit** — pre-placed orders that close your trade
  automatically at a chosen loss or profit level.
- **Testnet** — Binance's full practice copy with fake money.
- **Paper trading** — the bot simulating trades internally; nothing touches
  any exchange.
- **Shadow track** — the bot's built-in second brain trading virtual money
  at looser standards to gather learning data faster.
- **Drawdown** — how far the account has fallen from its peak.
- **VPS** — the small rented cloud computer the bot lives on.
- **SSH / Termius** — the way you open the server's terminal from your
  laptop / phone.
- **`.env` file** — where your secret codes live (never shared, never on GitHub).
- **`config.yaml`** — the bot's settings: mode, coins, risk percentages.

---

# The golden rules

1. **Climb the ladder in order.** Paper → testnet → live. Weeks, not hours.
2. **Never enable withdrawals on an API key.** Ever.
3. **Only trade money you can afford to lose completely.**
4. **Silence is discipline.** A bot that trades rarely is doing its job.
5. **No strategy earns every day.** The daily target is a stopping rule,
   not a salary. Judge months, not days.
6. **When in doubt: `/kill` first, questions later.** Restarting costs
   nothing (`systemctl start binance-bot`).
