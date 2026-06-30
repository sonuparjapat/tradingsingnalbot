# Trading Bot — Commands Reference

## Running the Bots

```bash
python dev.py bot              # NIFTY bot (full strategy, can auto-execute)
python dev.py sensex_bot       # SENSEX bot (signal-only), auto-restart on code changes
python dev.py morning_scanner  # NIFTY morning scanner (relaxed filters, signal-only)
python dev.py both             # NIFTY bot + SENSEX bot together, Ctrl+C stops both
                                # (morning scanner not included in 'both' — run it separately
                                #  locally, since it only does anything 9:30-11:00 AM anyway)
```

```bash
python dev.py backtest          # NIFTY backtest
python dev.py sensex_backtest   # SENSEX backtest
```

**SENSEX bot and the morning scanner are signal-only — they never place real
orders, no matter what.** Neither has `/start_auto`. They send Telegram alerts
and you decide whether to act manually. Only `nifty_zerodha_bot.py` can ever
place real orders, and only after you explicitly send `/start_auto`.

### NIFTY Morning Scanner — what it actually is

The main NIFTY bot runs the full proven rule set all day (Tier1 5/5 + Tier2
3-4/5 + S&R filter + expiry caution) and can auto-trade. The morning scanner
is a **separate, additional** tool — it doesn't touch or replace the main bot.

It only checks 4 mandatory conditions (VWAP, Supertrend, EMA momentum,
breakout vs previous candle), drops the volume-spike/RSI-band/Tier2-score/S&R/
ORB confirmation filters, and only runs **9:30-11:00 AM** — the window where
you've observed the strongest clean moves get filtered out by the full rule
set. It will fire more often and more loosely than the main bot by design.
Every alert is labeled "relaxed filters, signal-only" so it's never confused
with a real trade-worthy main-bot signal. Capped at 6 alerts/day.

Since this hasn't been backtested (it's a live-only exploratory tool for now),
treat its alerts as ideas to evaluate yourself, not validated signals — let me
know if you want it backtested over the 9:30-11:00 window for supporting data.

---

## Server Management — Push Changes / Check Logs Yourself

Run these from the project folder in Git Bash (`cd "c:\Users\Sonu\Desktop\tradingsingnalbot"`).
No need to ask Claude — these are plain shell commands.

| Task | NIFTY | SENSEX | Morning Scanner |
|---|---|---|---|
| **Push code changes live** | `bash deploy/push_live.sh` | `bash deploy/push_sensex_live.sh` | `bash deploy/push_morning_live.sh` |
| **Watch live logs** | `bash deploy/logs.sh` | `bash deploy/sensex_logs.sh` | `bash deploy/morning_logs.sh` |
| **Check for errors** | `bash deploy/errors.sh` | `bash deploy/sensex_errors.sh` | `bash deploy/morning_errors.sh` |
| **Quick health check** | `bash deploy/status.sh` | `bash deploy/sensex_status.sh` | `bash deploy/morning_status.sh` |
| **Start (or restart) this one only** | `bash deploy/start_nifty.sh` | `bash deploy/start_sensex.sh` | `bash deploy/start_morning.sh` |
| **Stop this one only** | `bash deploy/stop_nifty.sh` | `bash deploy/stop_sensex.sh` | `bash deploy/stop_morning.sh` |

| Task | Command |
|---|---|
| **Push .env changes** (e.g. changed `LOT_SIZE`, rotated a password) | `bash deploy/push_env.sh` |
| **Start ALL THREE bots together, one command** (first-time deploy, or restart everything) | `bash deploy/start_all.sh` |
| **Stop ALL THREE bots together, one command** | `bash deploy/stop_all.sh` |

`push_live.sh` / `push_sensex_live.sh` / `push_morning_live.sh` each do
everything in one go for that bot: upload the `.py` files → restart that
bot's service → wait 8s → show fresh log output, so you immediately see if
the new code started cleanly or errored out.

`start_all.sh` is the "in one go" command — it installs the `sensex-bot` and
`morning-scanner` systemd services if not already installed (idempotent, safe
to re-run), then restarts all three and shows all three logs. Use this for
the first-ever deploy, or any time you want everything running fresh.
`stop_all.sh` is the reverse — stops all three, services stay installed.

Stopping one bot never affects the others — they're fully independent
systemd services. Stopping NIFTY while a real position is open does **not**
close it; the broker-side GTT keeps protecting SL/Target, you just lose the
bot's breakeven/trailing/weak-exit/hard-exit management until you start it
again (use `/square_off` via Telegram first if you want to actually close
the position before stopping the bot).

**If something looks broken:** run the matching `errors.sh` script first —
shows the error log and how many times the service has auto-restarted
(`NRestarts`). A climbing restart count means something is crash-looping and
needs a code fix.

**Raw SSH** (if you ever need a direct shell on the server):
```bash
ssh -i deploy/ssh-key-2026-06-30.key ubuntu@92.4.94.88
```

---

## Telegram Commands

### NIFTY bot (auto-execution capable)

| Command | What it does |
|---|---|
| `/start_auto` | **Arms auto-trading.** Bot will place REAL orders (entry + GTT SL/Target) on the next signal. |
| `/stop_auto` | **Disarms auto-trading.** Stops placing NEW orders. Does NOT close an already-open position — it keeps monitoring that one until it exits normally. |
| `/status` | Shows current armed state (🟢 ARMED / 🔴 DISARMED) and full details of any open position (symbol, entry, SL, target, breakeven status). |
| `/square_off` | **Emergency exit.** Immediately cancels the GTT and market-sells the open position, regardless of SL/target. Use this if something looks wrong. |
| `/backtest [days]` | Runs a backtest (default 60, max 100 days) and sends a summary + the full trade CSV as a file. Runs in the background, won't block live trading. |
| `/help` | Lists all commands again. |

### SENSEX bot (signal-only — no `/start_auto` exists)

| Command | What it does |
|---|---|
| `/status` | Confirms the bot is running in signal-only mode. No position tracking — there's never a real position. |
| `/backtest [days]` | Same as NIFTY's — runs the SENSEX backtest (default 60, max 100 days), sends summary + CSV. |
| `/help` | Lists SENSEX bot commands. |

Both bots send the same kind of signal alert (entry, SL, target, breakeven,
full Tier1/Tier2 breakdown, S&R context). The only difference: NIFTY can act
on it automatically if armed; SENSEX always waits for you.

### NIFTY Morning Scanner (signal-only — separate process, separate commands)

| Command | What it does |
|---|---|
| `/morning_status` | Confirms it's running and shows its active window. |
| `/morning_help` | Lists its commands. |

These commands only exist if `nifty_morning_scanner.py` is running. It's a
distinct process from the main NIFTY bot — its alerts are clearly prefixed
"🌅 MORNING SCANNER" so they're never confused with main-bot signals.

---

## Safety Behavior — How Auto-Trading Actually Works

1. **Every restart starts DISARMED.** The bot never remembers `/start_auto` across restarts — you must re-arm each session. This is intentional.
2. **Signal-only by default.** Without `/start_auto`, the bot only sends Telegram alerts — no orders, ever.
3. **One position at a time.** While a position is open, the bot won't take new signals even if `MAX_TRADES` (3/day) hasn't been hit.
4. **Funds check before every order.** Before placing an entry, the bot checks your available margin via Kite. If insufficient (or the check itself fails), the trade is **skipped** and you get a Telegram alert — no order is placed.
5. **GTT (broker-side) for SL/Target.** Once filled, a GTT OCO order is placed on Zerodha's servers for SL and Target. This means your SL/Target still execute even if your bot or PC crashes or loses internet.
6. **Bot-side breakeven + trailing stop + weak-exit.** These need the bot running:
   - At **+8pts** spot move → SL is moved to entry (GTT modified).
   - Once the move extends to **1.5x** the breakeven distance (~+12pts), the SL starts **trailing** behind the peak (staying ~0.6x the breakeven distance back from the highest favorable move reached), instead of sitting flat at breakeven. This locks in profit on trades that run further before pulling back, instead of round-tripping all the way to breakeven. `/status` shows whether trailing is currently active and the peak favorable move reached.
   - After **15 min**, if move is **<5pts** → position is force-exited (weak momentum).
   - At **3:10 PM** → hard exit regardless of P&L.
7. **`/stop_auto` is not an emergency stop.** It only blocks new entries. Use `/square_off` to actually close a position right now.
8. **Crash/restart recovery.** If the bot or PC crashes while a position is open, the position is saved to `position_state.json` on disk. On the next startup, the bot automatically reloads it and resumes monitoring (breakeven/weak-exit/hard-exit) — you'll get a Telegram alert confirming recovery. The GTT order itself is untouched on Zerodha's side the whole time, so SL/Target protection never stops even if the bot is down.

### What still requires a stable internet/PC connection
GTT (SL/Target) is broker-side and survives outages. But breakeven trailing, weak-exit, and the 3:10 PM hard-exit all need the bot actively running — if your PC/internet is down for an extended period, those won't trigger until the bot is back online (the position itself stays safe via GTT, just the extra management is paused).

---

## Token Caching

Kite access tokens are valid until ~6 AM the next day. The bot saves today's token to `kite_token.json` after the first successful login. Every restart after that (e.g. from `dev.py` auto-restarting on code changes) reuses the cached token instantly instead of logging in again. A fresh login only happens once per calendar day, or if the cached token fails verification.

## Config You Can Tune (in `.env`)

| Variable | Meaning |
|---|---|
| `LOT_SIZE` | NIFTY option quantity per trade. **Verify the current exchange lot size before changing this.** |
| `SENSEX_LOT_SIZE` | SENSEX lot size, used only for sizing the displayed P&L in alerts/backtest (no real orders). Falls back to `LOT_SIZE` if unset — **set this explicitly and verify the current exchange lot size**, it's not the same as NIFTY's. Not currently set — defaults to 20. |
| `KITE_USER_ID` / `KITE_PASSWORD` / `KITE_TOTP_SECRET` | Auto-login credentials (no manual token paste needed), shared by all three bots. |
| `API_KEY` / `API_SECRET` | Kite Connect app credentials, shared by all three bots. |
| `BOT_TOKEN` / `CHAT_ID` | Telegram bot + chat for the **NIFTY bot** specifically (the safety-critical one — `/stop_auto`/`/square_off` must always reach it). |
| `SENSEX_BOT_TOKEN` | **Recommended:** a separate Telegram bot token for the SENSEX bot. Falls back to `BOT_TOKEN` if unset. |
| `MORNING_BOT_TOKEN` | **Recommended:** a separate Telegram bot token for the morning scanner. Falls back to `BOT_TOKEN` if unset. |

**Why separate bot tokens matter:** Telegram's `getUpdates` command-fetch cursor
is global per bot token, not per-process. If all three bots poll the *same*
token, a command typed for one (e.g. `/stop_auto` meant for NIFTY) can be
silently consumed by a different bot's poll call and never arrive — a real
risk for a command that's supposed to halt real-money auto-trading. Currently
`SENSEX_BOT_TOKEN`/`MORNING_BOT_TOKEN` aren't set, so all three share `BOT_TOKEN`
and this race is possible (low probability, but non-zero). To eliminate it:
create two more bots via [@BotFather](https://t.me/BotFather) (~1 minute each,
no need for separate chats — same `CHAT_ID` works for all three once you start
a conversation with each new bot), then add `SENSEX_BOT_TOKEN=...` and
`MORNING_BOT_TOKEN=...` to `.env` and push with `deploy/push_env.sh`.

---

## Before Going Live With Real Money

- Test with `LOT_SIZE=75` (1 lot) first run after arming.
- Watch the first 2-3 signals closely with `/status` to confirm GTT placement, breakeven modification, and exits behave as expected on your actual account.
- Only increase `LOT_SIZE` after you've confirmed the full entry → GTT → breakeven → exit cycle works correctly live.
