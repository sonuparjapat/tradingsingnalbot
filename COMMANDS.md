# NIFTY Bot — Commands Reference

## Running the Bot

```bash
python dev.py bot          # run with auto-restart on code changes
python nifty_zerodha_bot.py    # run directly, no auto-restart
```

```bash
python dev.py backtest     # run the backtest
python nifty_zerodha_backtest.py   # run backtest directly
```

---

## Server Management — Push Changes / Check Logs Yourself

Run these from the project folder in Git Bash (`cd "c:\Users\Sonu\Desktop\tradingsingnalbot"`).
No need to ask Claude — these are plain shell commands.

| Task | Command |
|---|---|
| **Push code changes live** (after editing bot/backtest .py files) | `bash deploy/push_live.sh` |
| **Push .env changes** (e.g. changed `LOT_SIZE`, rotated a password) | `bash deploy/push_env.sh` |
| **Watch live logs** (streams in real-time, Ctrl+C to stop watching) | `bash deploy/logs.sh` |
| **Check for errors** (error log + restart count) | `bash deploy/errors.sh` |
| **Quick health check** (is it running, last 15 log lines) | `bash deploy/status.sh` |
| **Manually restart** (no code change, just restart) | `bash deploy/restart.sh` |

`push_live.sh` does everything in one go: uploads both `.py` files → restarts the
service → waits 8s → shows you the fresh log output, so you immediately see if
the new code started cleanly or errored out.

**If something looks broken:** run `bash deploy/errors.sh` first — it shows the
error log and how many times the service has auto-restarted (`NRestarts`). A
climbing restart count means something is crash-looping and needs a code fix.

**Raw SSH** (if you ever need a direct shell on the server):
```bash
ssh -i deploy/ssh-key-2026-06-30.key ubuntu@92.4.94.88
```

---

## Telegram Commands (send these in your bot chat)

| Command | What it does |
|---|---|
| `/start_auto` | **Arms auto-trading.** Bot will place REAL orders (entry + GTT SL/Target) on the next signal. |
| `/stop_auto` | **Disarms auto-trading.** Stops placing NEW orders. Does NOT close an already-open position — it keeps monitoring that one until it exits normally. |
| `/status` | Shows current armed state (🟢 ARMED / 🔴 DISARMED) and full details of any open position (symbol, entry, SL, target, breakeven status). |
| `/square_off` | **Emergency exit.** Immediately cancels the GTT and market-sells the open position, regardless of SL/target. Use this if something looks wrong. |
| `/backtest [days]` | Runs a backtest (default 60, max 100 days) and sends a summary + the full trade CSV as a file. Runs in the background, won't block live trading. |
| `/help` | Lists all commands again. |

---

## Safety Behavior — How Auto-Trading Actually Works

1. **Every restart starts DISARMED.** The bot never remembers `/start_auto` across restarts — you must re-arm each session. This is intentional.
2. **Signal-only by default.** Without `/start_auto`, the bot only sends Telegram alerts — no orders, ever.
3. **One position at a time.** While a position is open, the bot won't take new signals even if `MAX_TRADES` (3/day) hasn't been hit.
4. **Funds check before every order.** Before placing an entry, the bot checks your available margin via Kite. If insufficient (or the check itself fails), the trade is **skipped** and you get a Telegram alert — no order is placed.
5. **GTT (broker-side) for SL/Target.** Once filled, a GTT OCO order is placed on Zerodha's servers for SL and Target. This means your SL/Target still execute even if your bot or PC crashes or loses internet.
6. **Bot-side breakeven + weak-exit.** These need the bot running:
   - At **+8pts** spot move → SL is moved to entry (GTT modified).
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
| `KITE_USER_ID` / `KITE_PASSWORD` / `KITE_TOTP_SECRET` | Auto-login credentials (no manual token paste needed). |
| `API_KEY` / `API_SECRET` | Kite Connect app credentials. |
| `BOT_TOKEN` / `CHAT_ID` | Telegram bot credentials for alerts + commands. |

---

## Before Going Live With Real Money

- Test with `LOT_SIZE=75` (1 lot) first run after arming.
- Watch the first 2-3 signals closely with `/status` to confirm GTT placement, breakeven modification, and exits behave as expected on your actual account.
- Only increase `LOT_SIZE` after you've confirmed the full entry → GTT → breakeven → exit cycle works correctly live.
