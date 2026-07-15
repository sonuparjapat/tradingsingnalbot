"""
=============================================================
NIFTY MORNING SCANNER — CE-only ATM, with AUTO EXECUTION
=============================================================
Scans the 9:30-13:00 window for high-quality CE (BUY) signals.
Strategy: VWAP + Supertrend + Breakout + Bull clean candle (4 conditions).
SL: prev candle low - 5pt (dynamic candle structure).
Target: 25pt fixed on spot price.
Backtest (90d): 91.2% win rate, 0 SL — best window confirmed (9:30-13:00).

SIGNAL-ONLY by default — starts disarmed. Send /start_auto on Telegram
to arm real order execution. Send /stop_auto to disarm. One position at a time.
GTT OCO set for SL+Target on every entry. Breakeven and trailing stop auto-managed.
Position persists across restarts via morning_position_state.json.
=============================================================
"""

from kiteconnect import KiteConnect
import pandas as pd
import numpy as np
import requests, time, webbrowser, os, json, sys, threading, csv
from datetime import datetime, timedelta, time as dtime
from dotenv import load_dotenv
from urllib.parse import urlparse, parse_qs
import pyotp
import warnings
warnings.filterwarnings('ignore')

load_dotenv()

# ─── CREDENTIALS (from .env) — same Kite account as main NIFTY bot ───
API_KEY    = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
# Separate bot token strongly recommended: getUpdates offset is global per bot token,
# so sharing one token across multiple processes causes commands to be silently swallowed.
# Falls back to BOT_TOKEN if unset.
BOT_TOKEN  = os.getenv("MORNING_BOT_TOKEN", os.getenv("BOT_TOKEN"))
CHAT_ID    = os.getenv("CHAT_ID")
KITE_USER_ID    = os.getenv("KITE_USER_ID")
KITE_PASSWORD   = os.getenv("KITE_PASSWORD")
KITE_TOTP_SECRET = os.getenv("KITE_TOTP_SECRET")

# ─── CONFIG ───
NIFTY_TOKEN = 256265
BREAKEVEN_PCT  = 0.00034   # breakeven activation threshold (fixed %)
STRIKE_GAP     = 50

# Candle-structure SL + fixed target (backtest: 91.2% WR, 0 SL in 90d)
CANDLE_SL_BUFFER  = 5    # spot pts below signal candle low (uses breakout candle's own low)
TARGET_PTS        = 25   # fixed spot pts target
SIDEWAYS_RANGE    = 30   # if last 4 candles' total range < this → sideways, skip entry
MAX_CANDLE_SL_PTS = 50   # skip signal if SL distance > this (risk cap: limits max loss per trade)

# This is the ONLY thing that makes this scanner different in scope from the
# main bot: a narrow morning window, and far fewer required conditions.
MORNING_START  = dtime(9, 30)
MORNING_END    = dtime(13, 0)
MAX_ALERTS     = 6
HEARTBEAT_MINS = 20

EXPIRY_WEEKDAY = 1  # Tuesday — no morning signals on expiry day

# Auto-execution parameters (same as main bot + backtest)
LOT_SIZE     = int(os.getenv("LOT_SIZE", "65"))   # per-lot qty (1 Nifty lot = 65)
OPTION_DELTA = 0.5    # ATM delta approx — converts spot pts to premium pts
MOMENTUM_MIN = 5      # weak-exit threshold: if <5 pts after 15 min → exit
HARD_EXIT    = dtime(15, 10)

TRAIL_TRIGGER_MULT = 1.2
TRAIL_STEP_MULT    = 0.6
BE_PREMIUM_PTS     = 3    # secondary BE: if option LTP rises this many pts above entry → BE regardless of spot
DAILY_LOSS_LIMIT   = float(os.getenv("DAILY_LOSS_LIMIT", "2000"))  # auto-disarm if day loss >= this
SPIKE_SL_PTS       = int(os.getenv("SPIKE_SL_PTS", "30"))  # SL distance for manual/spike entries (wider than CE candle SL)

POSITION_FILE   = "morning_position_state.json"
BOT_CONFIG_FILE = "bot_config.json"
SIGNAL_LOG_FILE = "live_signal_log.csv"
SIGNAL_LOG_FIELDS = [
    "date","time","day","signal",
    "entry_spot","sl_spot","sl_dist_pt","target_spot",
    "vwap","prev_candle_high","prev_candle_low","supertrend","option_ltp",
    "symbol","entry_premium","sl_premium","target_premium","mode",
    "outcome","exit_spot","exit_premium","pnl_rs","max_fav_pt","exit_time"
]

# ─── TUESDAY WINDOW — signal-only log ───
TUESDAY_SIGNAL_LOG_FILE = "tuesday_signal_log.csv"
TUESDAY_SIGNAL_LOG_FIELDS = [
    "date","time","signal","entry_spot","sl_spot","sl_dist_pt","target_spot",
    "vwap","prev_candle_high","prev_candle_low","supertrend","option_ltp","symbol","window","outcome"
]
TUESDAY_END            = dtime(10, 30)   # Morning window closes 10:30
TUESDAY_EVENING_START  = dtime(13,  0)   # Evening window opens 13:00
TUESDAY_EVENING_END    = dtime(14, 30)   # Evening window closes 14:30
TUESDAY_EVENING_TARGET = 20              # 20pt target for evening (backtest: 100% WR)

# ─── BOT CONFIG (persisted to bot_config.json — survives restarts) ───
def _load_bot_config():
    try:
        with open(BOT_CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_bot_config(cfg):
    try:
        with open(BOT_CONFIG_FILE, "w") as f:
            json.dump(cfg, f)
    except Exception as e:
        print(f"  Config save error: {e}")

_cfg         = _load_bot_config()
TRAIL_ENABLED = _cfg.get("trail_enabled", True)   # persists across restarts

# ─── AUTO-TRADING STATE (in-memory — resets to OFF on every restart) ───
AUTO_ARMED         = False
ACTIVE_LOTS        = 1     # set by /start_auto N; qty = ACTIVE_LOTS * LOT_SIZE
_alert_stop_event  = threading.Event()   # set via /stop_alerts to stop signal broadcast loop
_alerts_active     = False               # True while the 30s broadcast loop is running
position     = None  # dict of open position or None

daily_pnl_rs       = 0.0   # running P&L today — resets each new day
eod_report_sent    = False  # True after 3:30 PM report sent today
premarket_sent     = False  # True after 9:25 AM pre-market alert sent today
spike_disarmed_today = False  # True after spike auto-disarmed at 2:40 PM today

SPIKE_ARMED     = False  # separate from AUTO_ARMED — monitors intracandle momentum
SPIKE_THRESHOLD = int(os.getenv("SPIKE_THRESHOLD", "50"))  # pts move within a 5-min candle
_last_spike_candle = None  # prevent double-firing on same candle

telegram_offset   = 0
backtest_running  = False
last_heartbeat    = None

# ─── KITE ───
kite = KiteConnect(api_key=API_KEY)
TOKEN_FILE = "kite_token.json"  # shared with all NIFTY/SENSEX scripts

def load_cached_token():
    if not os.path.exists(TOKEN_FILE):
        return False
    try:
        with open(TOKEN_FILE, "r") as f:
            data = json.load(f)
        if data.get("date") != datetime.now().strftime("%Y-%m-%d"):
            return False
        kite.set_access_token(data["access_token"])
        kite.profile()
        print("✅ Reused cached token from earlier today — no login needed\n")
        return True
    except Exception as e:
        print(f"  Cached token invalid ({e}), logging in fresh...")
        return False

def save_cached_token(access_token):
    try:
        with open(TOKEN_FILE, "w") as f:
            json.dump({"access_token": access_token, "date": datetime.now().strftime("%Y-%m-%d")}, f)
    except Exception as e:
        print(f"⚠️ Could not cache token: {e}")

def auto_login():
    try:
        print("🔐 Auto-login with TOTP...")
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://kite.zerodha.com/",
            "Origin": "https://kite.zerodha.com",
            "X-Kite-Version": "3.0.0",
        })
        sess.get(kite.login_url(), allow_redirects=True)
        resp = sess.post("https://kite.zerodha.com/api/login", data={
            "user_id": KITE_USER_ID, "password": KITE_PASSWORD
        })
        data = resp.json()
        if data.get("status") != "success":
            print(f"❌ Login step 1 failed: {data.get('message','Unknown error')}")
            return False
        request_id = data["data"]["request_id"]

        totp = pyotp.TOTP(KITE_TOTP_SECRET).now()
        resp = sess.post("https://kite.zerodha.com/api/twofa", data={
            "user_id": KITE_USER_ID, "request_id": request_id,
            "twofa_value": totp, "twofa_type": "totp"
        })
        data = resp.json()
        if data.get("status") != "success":
            print(f"❌ TOTP failed: {data.get('message','Unknown error')}")
            return False

        time.sleep(1)
        redirect_url = ""
        next_url = kite.login_url()
        for _ in range(3):
            resp = sess.get(next_url, allow_redirects=False)
            if resp.status_code in (301, 302, 303, 307, 308):
                next_url = resp.headers.get("Location", "")
                if next_url.startswith("/"):
                    next_url = "https://kite.zerodha.com" + next_url
                redirect_url = next_url
                if "request_token=" in next_url:
                    break
            else:
                redirect_url = resp.url
                break

        parsed = parse_qs(urlparse(redirect_url).query)
        request_token = parsed.get("request_token", [None])[0]
        if not request_token:
            print(f"❌ No request_token in redirect. Last URL={redirect_url[:200]}")
            return False

        session_data = kite.generate_session(request_token, api_secret=API_SECRET)
        kite.set_access_token(session_data["access_token"])
        save_cached_token(session_data["access_token"])
        print("✅ Auto-login successful!\n")
        return True
    except Exception as e:
        print(f"❌ Auto-login failed: {e}")
        return False

def manual_login():
    if not sys.stdin.isatty():
        msg = ("❌ Auto-login failed AND no interactive terminal available to paste a token.\n"
               "Fix KITE_USER_ID / KITE_PASSWORD / KITE_TOTP_SECRET in .env and restart.")
        print(msg)
        try: send_telegram(f"🆘 <b>MORNING SCANNER COULD NOT LOG IN</b>\n\n{msg}")
        except Exception: pass
        return False
    login_url = kite.login_url()
    print(f"\n🌐 Opening Zerodha login...\nURL: {login_url}")
    webbrowser.open(login_url)
    print("\nCopy request_token from redirect URL")
    request_token = input("\nPaste request_token: ").strip()
    try:
        data = kite.generate_session(request_token, api_secret=API_SECRET)
        kite.set_access_token(data["access_token"])
        save_cached_token(data["access_token"])
        print("✅ Login successful!\n"); return True
    except Exception as e:
        print(f"❌ Login failed: {e}"); return False

def login():
    if load_cached_token():
        return True
    if KITE_USER_ID and KITE_PASSWORD and KITE_TOTP_SECRET and \
       KITE_USER_ID != "YOUR_USER_ID":
        if auto_login(): return True
        print("⚠️ Auto-login failed, trying manual...")
    return manual_login()

def send_telegram(msg):
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print(f"\n{'='*50}\n[TG]\n{msg}\n{'='*50}"); return
    for attempt in range(3):
        try:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"HTML"}, timeout=10)
            return
        except Exception as e:
            print(f"TG error (attempt {attempt+1}/3): {e}")
            if attempt < 2:
                time.sleep(3)

def send_telegram_file(path, caption=""):
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print(f"[TG] would send file: {path}"); return
    try:
        with open(path, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                data={"chat_id": CHAT_ID, "caption": caption},
                files={"document": f}, timeout=30)
    except Exception as e:
        print(f"TG file send error: {e}")

def get_telegram_updates():
    global telegram_offset
    try:
        resp = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"offset": telegram_offset, "timeout": 1}, timeout=5)
        data = resp.json()
        if not data.get("ok"): return []
        updates = data.get("result", [])
        if updates:
            telegram_offset = updates[-1]["update_id"] + 1
        return updates
    except Exception as e:
        print(f"  TG poll error: {e}")
        return []

def run_remote_backtest(days, trail_enabled=True, bt_lots=1):
    global backtest_running
    try:
        import nifty_morning_backtest as mbt
        mbt.kite.set_access_token(kite.access_token)
        trail_label = "trail ON" if trail_enabled else "NO trail"
        lots_label  = f"{bt_lots} lot{'s' if bt_lots > 1 else ''} ({bt_lots * mbt.LOT_SIZE} qty)"
        send_telegram(f"⏳ Running {days}-day morning backtest ({trail_label}, {lots_label})... (20-40 seconds)")
        df5 = mbt.fetch_data(mbt.NIFTY_TOKEN, "5minute", days=days)
        if df5 is None or df5.empty:
            print("  Backtest fetch failed — re-logging in and retrying...")
            if login():
                mbt.kite.set_access_token(kite.access_token)
                df5 = mbt.fetch_data(mbt.NIFTY_TOKEN, "5minute", days=days)
        if df5 is None or df5.empty:
            send_telegram("❌ Morning backtest failed — could not fetch data."); return
        orig_bepct = mbt.BREAKEVEN_PCT
        orig_ttm   = mbt.TRAIL_TRIGGER_MULT
        if not trail_enabled:
            mbt.BREAKEVEN_PCT      = 999999
            mbt.TRAIL_TRIGGER_MULT = 999999
        trades = mbt.run_backtest(df5, days=days, ce_only=True,
                                       candle_sl=True, target_pts=25,
                                       entry_windows=[(mbt.dtime(9,30), mbt.dtime(13,0))],
                                       skip_expiry=True, sideways_range_pt=30,
                                       signal_candle_sl=True, max_sl_pts=50)
        mbt.BREAKEVEN_PCT      = orig_bepct
        mbt.TRAIL_TRIGGER_MULT = orig_ttm
        if not trades:
            send_telegram(f"📊 Morning backtest ({days}d): No signals found."); return
        tdf = pd.DataFrame(trades)
        total = len(tdf); win_outcomes = ['TARGET','TRAIL']
        wins  = len(tdf[tdf['outcome'].isin(win_outcomes)])
        loss  = len(tdf[tdf['outcome']=='SL'])
        bes   = len(tdf[tdf['outcome']=='BE'])
        weak  = len(tdf[tdf['outcome']=='WEAK'])
        wr    = wins/total*100; net = tdf['pnl_rs'].sum() * bt_lots
        days_w = len(set(tdf['date']))
        verdict = "✅ PROFITABLE" if wr>=75 and net>0 else ("⚡ MARGINAL" if net>0 else "❌ Needs work")
        bdf = tdf[tdf['signal']=='BUY']
        bwr = len(bdf[bdf['outcome'].isin(win_outcomes)])/len(bdf)*100 if len(bdf) else 0
        trail_mode_str = "🔒 Trail ON" if trail_enabled else "🚀 No Trail"
        lots_str = f"{bt_lots} lot{'s' if bt_lots > 1 else ''} × {mbt.LOT_SIZE} qty"
        msg = (
            f"📊 <b>MORNING BACKTEST {days}d</b>  [CE-only, ATM | {trail_mode_str}]\n\n"
            f"Window: 09:30-13:00 | 4 conditions\n"
            f"VWAP + Supertrend + Breakout + Clean candle\n"
            f"Period: {tdf['date'].iloc[0]} → {tdf['date'].iloc[-1]}\n"
            f"Days with signals: {days_w} | Lots: {lots_str}\n\n"
            f"<b>Total Signals: {total}</b> (CE only)\n"
            f"✅ Wins (Tgt+Trail): {wins} ({wr:.1f}%)\n"
            f"❌ SL: {loss} | ⚖️ BE: {bes} | ⚠️ Weak: {weak}\n\n"
            f"💰 <b>Net P&L: Rs{net:,.0f}</b>\n\n{verdict}"
        )
        send_telegram(msg)
        csv_path = f"morning_backtest_{days}d.csv"
        tdf.to_csv(csv_path, index=False)
        with open(csv_path, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                data={"chat_id": CHAT_ID, "caption": f"📁 Morning backtest {days}d ({total} signals)"},
                files={"document": f}, timeout=30)
    except Exception as e:
        send_telegram(f"❌ Morning backtest error: {e}")
    finally:
        backtest_running = False


def run_remote_tuesday_backtest(days, bt_lots=1):
    """Tuesday backtest — both morning (9:30-10:30) and evening (13:00-14:30) windows."""
    global backtest_running
    try:
        import nifty_morning_backtest as mbt
        mbt.kite.set_access_token(kite.access_token)
        lots_label = f"{bt_lots} lot{'s' if bt_lots > 1 else ''} ({bt_lots * mbt.LOT_SIZE} qty)"
        send_telegram(f"⏳ Running {days}-day <b>TUESDAY</b> backtest (both windows, {lots_label})... (30-60 seconds)")
        df5 = mbt.fetch_data(mbt.NIFTY_TOKEN, "5minute", days=days)
        if df5 is None or df5.empty:
            if login():
                mbt.kite.set_access_token(kite.access_token)
                df5 = mbt.fetch_data(mbt.NIFTY_TOKEN, "5minute", days=days)
        if df5 is None or df5.empty:
            send_telegram("❌ Tuesday backtest failed — could not fetch data."); return

        lots_str = f"{bt_lots} lot{'s' if bt_lots > 1 else ''} × {mbt.LOT_SIZE} qty"

        m_trades, ms = mbt.run_tuesday_backtest(df5, days=days, bt_lots=bt_lots, pe_only=True, window="morning")
        e_trades, es = mbt.run_tuesday_backtest(df5, days=days, bt_lots=bt_lots, pe_only=True, window="evening")

        if not m_trades and not e_trades:
            send_telegram(f"📊 Tuesday backtest ({days}d): No Tuesday PE signals found in either window."); return

        def trade_lines(trades, lot_mult):
            lines = []
            for r in trades:
                icon = {'TARGET':'✅','TRAIL':'🔒','SL':'❌','BE':'⚖️','WEAK':'⚠️'}.get(r['outcome'],'➡️')
                lines.append(f"{r['date']}  {r['time']}  {icon}{r['outcome']}  Rs{r['pnl_rs']*lot_mult:+,.0f}")
            return "\n".join(lines) if lines else "(no signals)"

        all_trades = m_trades + e_trades
        at_total = len(all_trades)
        at_wins  = sum(1 for t in all_trades if t['outcome'] in ('TARGET','TRAIL'))
        at_wr    = at_wins / at_total * 100 if at_total else 0
        at_net   = sum(t['pnl_rs'] for t in all_trades) * bt_lots
        at_sl    = sum(1 for t in all_trades if t['outcome'] == 'SL')

        m_block = trade_lines(m_trades, bt_lots)
        e_block = trade_lines(e_trades, bt_lots)

        m_section = (f"🌅 <b>Morning 09:30-10:30 | 25pt</b>\n"
                     f"{ms['total']} trades | {ms['wr']:.0f}% WR | SL:{ms['sl']} | Rs{ms['net']:,.0f}\n"
                     f"<code>{m_block}</code>") if m_trades else "🌅 <b>Morning 09:30-10:30</b>: No signals"

        e_section = (f"🌆 <b>Evening 13:00-14:30 | 20pt</b>\n"
                     f"{es['total']} trades | {es['wr']:.0f}% WR | SL:{es['sl']} | Rs{es['net']:,.0f}\n"
                     f"<code>{e_block}</code>") if e_trades else "🌆 <b>Evening 13:00-14:30</b>: No signals"

        verdict = "✅ BOTH PROFITABLE" if (m_trades and ms['wr'] >= 65 and e_trades and es['wr'] >= 65) \
                  else ("✅ PROFITABLE" if at_net > 0 and at_wr >= 65 else ("⚡ MARGINAL" if at_net > 0 else "❌"))

        msg = (
            f"📊 <b>TUESDAY BACKTEST {days}d</b>  [PE Only — Both Windows]\n\n"
            f"4 conditions: VWAP + Supertrend + Breakdown + Bear clean\n"
            f"Lots: {lots_str}\n\n"
            f"{m_section}\n\n"
            f"{e_section}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"<b>COMBINED: {at_total} trades | {at_wr:.0f}% WR | SL:{at_sl} | Rs{at_net:,.0f}</b>\n\n"
            f"{verdict}"
        )
        send_telegram(msg)

        if all_trades:
            csv_path = f"tuesday_backtest_{days}d.csv"
            pd.DataFrame(all_trades).to_csv(csv_path, index=False)
            with open(csv_path, "rb") as f:
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                    data={"chat_id": CHAT_ID, "caption": f"📁 Tuesday backtest {days}d ({at_total} signals)"},
                    files={"document": f}, timeout=30)
    except Exception as e:
        send_telegram(f"❌ Tuesday backtest error: {e}")
    finally:
        backtest_running = False




def process_telegram_commands():
    global backtest_running, AUTO_ARMED, ACTIVE_LOTS, TRAIL_ENABLED
    global SPIKE_ARMED, SPIKE_THRESHOLD, _last_spike_candle
    updates = get_telegram_updates()
    for u in updates:
        msg = u.get("message", {})
        text = msg.get("text", "").strip().lower()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != str(CHAT_ID):
            continue

        if text.startswith("/start_auto"):
            parts = text.split()
            try:
                lots = int(parts[1]) if len(parts) > 1 else 1
                lots = max(1, min(lots, 10))  # clamp 1-10
            except (ValueError, IndexError):
                lots = 1
            ACTIVE_LOTS = lots
            AUTO_ARMED = True
            qty = ACTIVE_LOTS * LOT_SIZE
            print(f"🟢 MORNING AUTO-TRADING ARMED — {lots} lot(s) × {LOT_SIZE} = {qty} qty")
            send_telegram(
                f"✅ <b>MORNING AUTO-TRADING ARMED</b> 🟢\n\n"
                f"Lots: <b>{lots}</b> × {LOT_SIZE} = <b>{qty} qty</b>\n"
                "Bot will now place REAL orders when a BUY CE signal fires.\n"
                f"Strategy: VWAP + Supertrend + Breakout + Clean candle\n"
                f"GTT OCO: SL + Target set automatically on entry.\n"
                f"Breakeven & trailing stop managed automatically.\n\n"
                "Send /stop_auto to disarm."
            )
        elif text == "/stop_auto":
            AUTO_ARMED = False
            print("🔴 MORNING AUTO-TRADING DISARMED via Telegram")
            send_telegram(
                "⛔ <b>MORNING AUTO-TRADING DISARMED</b>\n\n"
                "No new orders will be placed. Existing position (if any) continues monitoring.\n"
                "Send /square_off to close any open position manually."
            )
        elif text == "/stop_alerts":
            if not _alerts_active:
                # No broadcast running — just acknowledge
                pos_note = f"Position open: {position['symbol']}" if position else "No open position."
                send_telegram(f"ℹ️ No active alerts running.\n{pos_note}")
            else:
                _alert_stop_event.set()
                if position is not None:
                    # Execution succeeded — bot has the position, keep monitoring
                    send_telegram(
                        "🔕 <b>Alerts stopped.</b>\n"
                        f"Position tracked: {position['symbol']}\n"
                        "SL/target monitoring continues automatically."
                    )
                elif AUTO_ARMED:
                    # Armed but no position — execution failed, user entered manually
                    AUTO_ARMED = False
                    send_telegram(
                        "🔕 <b>Alerts stopped. Auto-trading DISARMED.</b>\n\n"
                        "Bot has no position recorded.\n"
                        "If you entered manually on Kite — manage SL/target yourself.\n\n"
                        "• /start_auto to re-arm when ready for next signal\n"
                        "• /status to check bot state"
                    )
                else:
                    # Signal-only mode, no position
                    send_telegram(
                        "🔕 <b>Alerts stopped.</b>\n"
                        "No open position. Bot continues scanning."
                    )
        elif text == "/status":
            lot_info    = f"{ACTIVE_LOTS} lot(s) × {LOT_SIZE} = {ACTIVE_LOTS*LOT_SIZE} qty"
            armed_state = f"🟢 ARMED — {lot_info}" if AUTO_ARMED else "🔴 DISARMED — signal-only"
            trail_state = "🔒 Trail ON" if TRAIL_ENABLED else "🚀 Trail OFF"
            if position:
                entry_time_str = position["entry_time"].strftime("%H:%M") if isinstance(position["entry_time"], datetime) else str(position["entry_time"])
                pos_info = (
                    f"\n\n📦 <b>Open Position:</b>\n"
                    f"{position['signal']} {position['symbol']}\n"
                    f"Entry time: {entry_time_str}\n"
                    f"Entry premium: {position['entry_premium']}\n"
                    f"SL premium: {position['sl_premium']}\n"
                    f"Target premium: {position['target_premium']}\n"
                    f"Breakeven: {'✅ Hit' if position['breakeven_hit'] else '⏳ Not yet'}\n"
                    f"Trailing: {'🔒 Active' if position.get('trail_active') else 'Not yet'}\n"
                    f"Peak spot move: +{position.get('peak_favorable', 0):.1f} pts\n\n"
                    f"To exit now: send /square_off"
                )
            else:
                pos_info = "\n\nNo open position."
            pnl_color = "🟢" if daily_pnl_rs >= 0 else "🔴"
            _s = get_live_stats()
            _perf = _stats_line(_s) if _s else "No closed trades yet"
            send_telegram(
                f"📊 <b>Morning Scanner Status</b>\n\n"
                f"Auto-trade: {armed_state}\n"
                f"Trail mode: {trail_state}\n"
                f"Window: {MORNING_START.strftime('%H:%M')}-{MORNING_END.strftime('%H:%M')}\n"
                f"Strategy: CE (BUY) only | 4 conditions | ATM\n"
                f"📈 {_perf}\n"
                f"{pnl_color} Today's P&L: <b>₹{daily_pnl_rs:+,.0f}</b>{pos_info}"
            )
        elif text == "/square_off":
            if position:
                send_telegram(f"🔴 Manual square-off requested for {position['symbol']}...")
                try:
                    df_sq = fetch_data(NIFTY_TOKEN, "5minute", days=1)
                    sq_spot = float(df_sq.iloc[-1]['Close']) if df_sq is not None and not df_sq.empty else 0
                except Exception:
                    sq_spot = 0
                exit_position("MANUAL", exit_spot=sq_spot)
            else:
                send_telegram("ℹ️ No open morning position to square off.")

        elif text in ("/buy_ce", "/buy_pe"):
            opt_type = "CE" if text == "/buy_ce" else "PE"
            if position is not None:
                send_telegram(f"⚠️ Position already open ({position['symbol']}) — /square_off first")
            else:
                send_telegram(f"⏳ Processing manual {opt_type} entry...")
                threading.Thread(target=execute_manual_entry, args=(opt_type,), daemon=True).start()

        elif text == "/morning_status":
            armed_state  = "🟢 ARMED" if AUTO_ARMED else "🔴 DISARMED (signal-only)"
            trail_state  = "🔒 Trail ON" if TRAIL_ENABLED else "🚀 Trail OFF (run to target)"
            _s = get_live_stats()
            _perf = _stats_line(_s) if _s else "No closed trades yet"
            send_telegram(
                "📊 <b>Morning Scanner Status</b>\n\n"
                f"Auto-trade: {armed_state}\n"
                f"Trail mode: {trail_state}\n"
                f"🌅 Window: {MORNING_START.strftime('%H:%M')}-{MORNING_END.strftime('%H:%M')}\n"
                "Strategy: <b>CE (BUY) only — ATM, 4 conditions</b>\n"
                "  VWAP + Supertrend + Breakout + Clean candle\n\n"
                f"📈 {_perf}\n"
                "Send /start_auto to arm | /stop_auto to disarm\n"
                "Send /trail on|off to toggle trail"
            )
        elif text == "/today":
            rows = _load_signal_log()
            today_str = datetime.now().strftime('%Y-%m-%d')
            today_rows = [r for r in rows if r.get('date','') == today_str]
            if not today_rows:
                send_telegram(f"No signals today ({datetime.now().strftime('%d %b')}).")
            else:
                closed = [r for r in today_rows if r.get('outcome','')]
                total_pnl = sum(float(r.get('pnl_rs') or 0) for r in closed)
                msg = f"<b>Today — {datetime.now().strftime('%d %b')}</b>\n\n"
                for r in today_rows:
                    outcome = r.get('outcome','⏳ open')
                    pnl_str = f"  ₹{float(r.get('pnl_rs') or 0):+,.0f}" if r.get('pnl_rs') else ''
                    msg += f"{r.get('time','')}  {r.get('signal','')}  {outcome}{pnl_str}\n"
                if closed:
                    msg += f"\nTotal P&L: <b>₹{total_pnl:+,.0f}</b>"
                if position:
                    msg += f"\n📦 Position open: {position['symbol']}"
                send_telegram(msg)
        elif text.startswith("/arm_spike"):
            parts = text.split()
            try:
                SPIKE_THRESHOLD = int(parts[1]) if len(parts) > 1 else SPIKE_THRESHOLD
            except ValueError:
                pass
            SPIKE_ARMED = True
            _last_spike_candle = None
            send_telegram(
                f"⚡ <b>SPIKE DETECTOR ARMED</b>\n\n"
                f"Threshold: <b>{SPIKE_THRESHOLD}pt</b> move within a 5-min candle\n"
                f"Direction: auto-detected (UP → CE alert, DOWN → PE alert)\n"
                f"Active window: <b>9:30 AM – 2:40 PM</b>\n"
                f"Mode: <b>SIGNAL ONLY</b> — no auto orders placed\n"
                f"You decide: enter on Kite manually if move looks real.\n\n"
                f"Send /disarm_spike to stop."
            )
        elif text == "/disarm_spike":
            SPIKE_ARMED = False
            send_telegram("⛔ Spike detector disarmed.")
        elif text == "/morning_help":
            _s = get_live_stats()
            _perf = _stats_line(_s) if _s else "No closed trades yet"
            send_telegram(
                f"🤖 <b>Morning Scanner Commands</b>\n\n"
                f"CE-only ATM | 4 conditions | 📈 {_perf}\n\n"
                "<b>Auto CE (morning 9:30-13:00):</b>\n"
                "/start_auto [N] — arm auto execution (N lots)\n"
                "/stop_auto — disarm auto execution\n\n"
                "<b>Spike detector (9:30-14:40, signal only):</b>\n"
                "/arm_spike [pts] — arm spike alerts (default 50pt)\n"
                "/disarm_spike — disarm spike alerts\n\n"
                "<b>Position & Info:</b>\n"
                "/status — armed state + open position\n"
                "/today — today's trades + P&L\n"
                "/live_log — full signal log + CSV\n"
                "/square_off — emergency close open position\n"
                "/buy_ce — manual CE entry at current ATM price\n"
                "/buy_pe — manual PE entry at current ATM price\n"
                "/morning_status — scanner status\n"
                "/backtest [days] [notail|trail] [lots=N] — CE strategy backtest\n"
                "  e.g. /backtest 90 notail lots=2\n"
                "/backtest tue [days] [lots=N] — Tuesday PE backtest (morning + evening)\n"
                "  e.g. /backtest tue 90 lots=2\n"
                "/trail on — enable trail (persists across restarts)\n"
                "/trail off — disable trail (persists across restarts)\n"
                "/restart — force fresh Kite login\n"
                "/morning_help — this message"
            )
        elif text == "/restart":
            send_telegram("🔄 <b>RESTART requested</b> — forcing fresh Kite login...")
            try:
                import os
                if os.path.exists(TOKEN_FILE):
                    os.remove(TOKEN_FILE)
            except Exception: pass
            if auto_login():
                send_telegram("✅ <b>Re-login successful</b> — new token active. Bot is running normally.")
            else:
                send_telegram(
                    "❌ <b>Auto re-login FAILED</b>\n\n"
                    "Check KITE_USER_ID / KITE_PASSWORD / KITE_TOTP_SECRET in .env on server.\n"
                    "SSH in and run: sudo systemctl restart morning-scanner.service"
                )

        elif text == "/backtest" or text.startswith("/backtest "):
            if backtest_running:
                send_telegram("⏳ A backtest is already running — please wait.")
            else:
                parts = text.split()
                # Tuesday-window backtest: /backtest tue [days] [lots=N]
                if len(parts) > 1 and parts[1].lower() == "tue":
                    days = 90; bt_lots = 1
                    for p in parts[2:]:
                        pl = p.lower()
                        if pl.startswith("lots="):
                            try: bt_lots = max(1, int(pl.split("=")[1]))
                            except ValueError: pass
                        else:
                            try: days = max(5, min(100, int(p)))
                            except ValueError: pass
                    backtest_running = True
                    threading.Thread(target=run_remote_tuesday_backtest, args=(days, bt_lots), daemon=True).start()
                else:
                    # Main strategy backtest (Mon/Wed/Thu/Fri)
                    days = 60; use_trail = TRAIL_ENABLED; bt_lots = 1
                    for p in parts[1:]:
                        pl = p.lower()
                        if pl == "notail":
                            use_trail = False
                        elif pl == "trail":
                            use_trail = True
                        elif pl.startswith("lots="):
                            try: bt_lots = max(1, int(pl.split("=")[1]))
                            except ValueError: pass
                        else:
                            try: days = max(5, min(100, int(p)))
                            except ValueError:
                                send_telegram("⚠️ Usage: /backtest [days] [notail|trail] [lots=N]\nOr: /backtest tue [days] [lots=N]"); continue
                    backtest_running = True
                    threading.Thread(target=run_remote_backtest, args=(days, use_trail, bt_lots), daemon=True).start()
        elif text in ("/trail on", "/trail off"):
            TRAIL_ENABLED = text.endswith("on")
            _save_bot_config({**_load_bot_config(), "trail_enabled": TRAIL_ENABLED})
            status = "🔒 Trail ON — SL moves to breakeven then trails" if TRAIL_ENABLED \
                     else "🚀 Trail OFF — position runs freely to target (original SL only)"
            send_telegram(f"<b>TRAIL MODE CHANGED</b>\n{status}\n\n✅ Saved — survives restarts.\nBacktest will also use this mode by default.")
        elif text == '/live_log':
            rows = _load_signal_log()
            if not rows:
                send_telegram("No live signals recorded yet.")
            else:
                total  = len(rows)
                closed = [r for r in rows if r.get("outcome","") not in ("", "SIGNAL", "OPEN")]
                WIN_OUTCOMES = ("TARGET", "TRAIL", "GTT")  # GTT = legacy label for TARGET/TRAIL
                wins   = [r for r in closed if r.get("outcome","") in WIN_OUTCOMES]
                losses = [r for r in closed if r.get("outcome","") == "SL"]
                wr     = round(len(wins)/len(closed)*100, 1) if closed else 0
                msg    = (f"<b>Live Signal Log</b> (last {total} signals)\n"
                          f"Closed: {len(closed)} | Win: {len(wins)} ({wr}%) | SL: {len(losses)}\n")
                last5  = closed[-5:] if len(closed) >= 5 else closed
                for r in reversed(last5):
                    pnl_val = float(r.get('pnl_rs') or 0)
                    pnl_str = f"₹{pnl_val:+,.0f}"
                    msg += f"\n{r.get('date','')} {r.get('signal','')} {r.get('outcome','')} {pnl_str}"
                send_telegram(msg)
                # Also send the CSV file
                import os
                if os.path.exists(SIGNAL_LOG_FILE):
                    send_telegram_file(SIGNAL_LOG_FILE, caption="live_signal_log.csv")


def sleep_poll(seconds):
    elapsed = 0
    while elapsed < seconds:
        chunk = min(5, seconds - elapsed)
        time.sleep(chunk)
        elapsed += chunk
        process_telegram_commands()

# ─── DATA ───
def fetch_data(token, interval="5minute", days=2):
    def _fetch():
        candles = kite.historical_data(token, datetime.now()-timedelta(days=days),
                                       datetime.now(), interval)
        if not candles: return None
        df = pd.DataFrame(candles)
        df.columns = ['date','Open','High','Low','Close','Volume']
        df.set_index('date', inplace=True)
        df.index = pd.to_datetime(df.index)
        return df.dropna()
    try:
        return _fetch()
    except Exception as e:
        err = str(e)
        if "access_token" in err or "api_key" in err or "Incorrect" in err:
            print(f"  ⚠️ Auth error — re-logging in automatically...")
            send_telegram("⚠️ <b>Morning Scanner: session expired</b> — re-logging in automatically...")
            if login():
                try:
                    return _fetch()
                except Exception as e2:
                    print(f"  Data error after relogin: {e2}"); return None
        print(f"  Data error: {e}"); return None

# ─── INDICATORS (same as main bot) ───
def ema(s, p): return s.ewm(span=p, adjust=False).mean()

def calculate_vwap(df):
    df = df.copy(); df['Date'] = df.index.date
    df['TP'] = (df['High']+df['Low']+df['Close'])/3
    if df['Volume'].sum() > 0:
        df['TPV'] = df['TP']*df['Volume']
        df['CumTPV'] = df.groupby('Date')['TPV'].cumsum()
        df['CumVol'] = df.groupby('Date')['Volume'].cumsum()
        return (df['CumTPV']/df['CumVol']).fillna(df['Close'])
    return df.groupby('Date')['TP'].transform(lambda x: x.expanding().mean()).fillna(df['Close'])

def calculate_supertrend(df, period=10, multiplier=3.0):
    df = df.copy(); hl2 = (df['High']+df['Low'])/2
    df['TR'] = np.maximum(df['High']-df['Low'],
               np.maximum(abs(df['High']-df['Close'].shift(1)), abs(df['Low']-df['Close'].shift(1))))
    df['ATR'] = df['TR'].rolling(period).mean()
    upper = (hl2+multiplier*df['ATR']).values.copy()
    lower = (hl2-multiplier*df['ATR']).values.copy()
    trend = [True]*len(df); close = df['Close'].values
    for i in range(1,len(df)):
        lower[i] = max(lower[i],lower[i-1]) if close[i-1]>lower[i-1] else lower[i]
        upper[i] = min(upper[i],upper[i-1]) if close[i-1]<upper[i-1] else upper[i]
        if not trend[i-1] and close[i]>upper[i]: trend[i]=True
        elif trend[i-1] and close[i]<lower[i]: trend[i]=False
        else: trend[i]=trend[i-1]
    return pd.Series(trend, index=df.index)

def calculate_rsi(s, p=14):
    d=s.diff(); g=d.where(d>0,0).rolling(p).mean(); l=(-d.where(d<0,0)).rolling(p).mean()
    return 100-(100/(1+g/l))

def analyze_candle(o,h,l,c):
    body=abs(c-o); tr=h-l
    if tr==0: return True, False, False
    uw=h-max(o,c); lw=min(o,c)-l
    doji=(body/tr)<0.1
    return doji, (not doji and c>o and uw<=body), (not doji and c<o and lw<=body)

def get_strike(price, signal):
    """ATM strike — backtest confirmed ATM outperforms ITM in morning window"""
    atm = round(price / STRIKE_GAP) * STRIKE_GAP
    if signal == "BUY": return f"{atm} CE"
    else: return f"{atm} PE"

# ─── ORDER EXECUTION (only runs when AUTO_ARMED == True) ───
_symbol_cache = {}   # (date, atm, opt_type) → tradingsymbol — avoids repeated full instruments download

def find_option_symbol(price, signal):
    """Find ATM CE/PE tradingsymbol for the nearest weekly expiry. Cached per strike per day."""
    try:
        atm = round(price / STRIKE_GAP) * STRIKE_GAP
        opt_type = "CE" if signal == "BUY" else "PE"
        key = (datetime.now().date(), atm, opt_type)
        if key in _symbol_cache:
            return _symbol_cache[key]
        instruments = kite.instruments("NFO")
        df = pd.DataFrame(instruments)
        opts = df[(df['name'] == 'NIFTY') & (df['instrument_type'] == opt_type) &
                  (df['strike'] == atm)].copy()
        if opts.empty: return None
        opts['expiry'] = pd.to_datetime(opts['expiry'])
        today = datetime.now().date()
        opts = opts[opts['expiry'].dt.date >= today].sort_values('expiry')
        if opts.empty: return None
        result = opts.iloc[0]['tradingsymbol']
        _symbol_cache[key] = result
        return result
    except Exception as e:
        print(f"❌ Symbol lookup error: {e}")
        return None

def check_sufficient_funds(estimated_premium, qty):
    try:
        margins = kite.margins()
        available_cash = margins["equity"]["available"]["live_balance"]
        required = estimated_premium * qty
        buffer = required * 0.05
        sufficient = available_cash >= (required + buffer)
        return sufficient, available_cash, required
    except Exception as e:
        print(f"⚠️ Margin check failed: {e} — assuming funds available")
        return True, 0, 0  # let Zerodha reject if truly insufficient; don't block on API error

def place_entry_order(symbol, qty, ltp):
    def _place():
        # 2% above LTP gives room to fill in fast-moving options (market orders blocked by Kite)
        limit_price = round(round(ltp * 1.02 / 0.05) * 0.05, 2)
        return kite.place_order(
            variety=kite.VARIETY_REGULAR, exchange=kite.EXCHANGE_NFO,
            tradingsymbol=symbol, transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=qty, product=kite.PRODUCT_MIS, order_type=kite.ORDER_TYPE_LIMIT,
            price=limit_price
        )
    try:
        return _place()
    except Exception as e:
        err = str(e)
        if any(k in err for k in ("access_token", "Incorrect", "api_key", "Invalid token")):
            print(f"⚠️ Auth error on entry order — re-logging in and retrying...")
            if login():
                try:
                    return _place()
                except Exception as e2:
                    print(f"❌ Entry order failed after relogin: {e2}")
                    send_telegram(f"❌ <b>ENTRY ORDER FAILED (after relogin)</b>\n{symbol}\n{e2}")
                    return None
        print(f"❌ Entry order failed: {e}")
        send_telegram(f"❌ <b>ENTRY ORDER FAILED</b>\n{symbol}\n{e}")
        return None

def get_order_avg_price(order_id, timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        try:
            history = kite.order_history(order_id)
            last = history[-1]
            if last['status'] == 'COMPLETE':
                return float(last['average_price'])
            elif last['status'] in ('REJECTED', 'CANCELLED'):
                print(f"  Order {order_id} {last['status']}: {last.get('status_message','')}")
                send_telegram(f"❌ Entry order {last['status']}: {last.get('status_message','')}")
                return None
        except Exception as e:
            print(f"  Order status check error: {e}")
        time.sleep(1)
    return None

def place_exit_order(symbol, qty):
    try:
        ltp = kite.quote([f"NFO:{symbol}"])[f"NFO:{symbol}"]["last_price"]
        # LIMIT order slightly below LTP — market orders blocked by Kite API for options
        limit_price = round(round(ltp * 0.99 / 0.05) * 0.05, 2)
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR, exchange=kite.EXCHANGE_NFO,
            tradingsymbol=symbol, transaction_type=kite.TRANSACTION_TYPE_SELL,
            quantity=qty, product=kite.PRODUCT_MIS, order_type=kite.ORDER_TYPE_LIMIT,
            price=limit_price
        )
        return order_id
    except Exception as e:
        print(f"❌ Exit order failed: {e}")
        send_telegram(f"❌ <b>EXIT ORDER FAILED — CLOSE {symbol} MANUALLY NOW</b>\n{e}")
        return None

def place_gtt_oco(symbol, qty, sl_premium, target_premium, last_price):
    try:
        # SL limit price set 1.5pt below trigger so it fills even with a small gap
        sl_limit = round(sl_premium - 1.5, 1)
        gtt = kite.place_gtt(
            trigger_type=kite.GTT_TYPE_OCO, tradingsymbol=symbol, exchange=kite.EXCHANGE_NFO,
            trigger_values=[sl_premium, target_premium], last_price=last_price,
            orders=[
                {"transaction_type": kite.TRANSACTION_TYPE_SELL, "quantity": qty,
                 "order_type": kite.ORDER_TYPE_LIMIT, "product": kite.PRODUCT_MIS, "price": sl_limit},
                {"transaction_type": kite.TRANSACTION_TYPE_SELL, "quantity": qty,
                 "order_type": kite.ORDER_TYPE_LIMIT, "product": kite.PRODUCT_MIS, "price": target_premium},
            ]
        )
        return gtt["trigger_id"]
    except Exception as e:
        print(f"❌ GTT placement failed: {e}")
        send_telegram(f"⚠️ <b>GTT FAILED — manage SL/Target manually for {symbol}!</b>\n{e}")
        return None

def modify_gtt_sl(gtt_id, symbol, qty, new_sl, target_premium, last_price):
    try:
        sl_limit = round(new_sl - 1.5, 1)
        kite.modify_gtt(
            trigger_id=gtt_id, trigger_type=kite.GTT_TYPE_OCO,
            tradingsymbol=symbol, exchange=kite.EXCHANGE_NFO,
            trigger_values=[new_sl, target_premium], last_price=last_price,
            orders=[
                {"transaction_type": kite.TRANSACTION_TYPE_SELL, "quantity": qty,
                 "order_type": kite.ORDER_TYPE_LIMIT, "product": kite.PRODUCT_MIS, "price": sl_limit},
                {"transaction_type": kite.TRANSACTION_TYPE_SELL, "quantity": qty,
                 "order_type": kite.ORDER_TYPE_LIMIT, "product": kite.PRODUCT_MIS, "price": target_premium},
            ]
        )
        return True
    except Exception as e:
        print(f"⚠️ GTT modify failed: {e}")
        return False

def cancel_gtt(gtt_id):
    try:
        kite.delete_gtt(gtt_id)
        return True
    except Exception as e:
        print(f"⚠️ GTT cancel failed: {e}")
        return False

def check_gtt_triggered():
    """Detect if our GTT fired (SL or target hit by Zerodha) and clear the position."""
    global position
    if position is None or not position.get("gtt_id"):
        return
    try:
        gtts = kite.get_gtts()
        gtt_map = {g['id']: g for g in gtts}
        our_gtt = gtt_map.get(position["gtt_id"])
        if our_gtt is None:
            return  # GTT not visible yet - don't clear (we cancel it ourselves on exit)
        if our_gtt['status'] == 'triggered':
            global daily_pnl_rs
            max_fav       = position.get("peak_favorable", 0)
            qty           = position.get("qty", LOT_SIZE)
            entry_premium = position.get("entry_premium", 0)
            sl_premium    = position.get("sl_premium", 0)
            tgt_premium   = position.get("target_premium", 0)
            # Determine which OCO leg fired: check which triggered order's price
            # is closer to target vs SL. Options can hit target with < TARGET_PTS
            # spot move (gamma/delta), so max_fav spot comparison is unreliable.
            tgt_hit = False
            for ord_info in our_gtt.get('orders', []):
                result = ord_info.get('result') or {}
                if result.get('order_id'):  # this order was actually placed/executed
                    tp = float(ord_info.get('trigger_price', 0))
                    tgt_hit = (abs(tp - tgt_premium) < abs(tp - sl_premium))
                    break
            else:
                # Fallback: estimate from spot peak move vs target premium distance
                tgt_hit = (tgt_premium > sl_premium and max_fav * OPTION_DELTA >= (tgt_premium - entry_premium) * 0.8)
            if tgt_hit:
                gtt_pnl = round((tgt_premium - entry_premium) * qty, 0)
                gtt_leg = "TARGET"
                header  = "\ud83c\udfaf <b>TARGET HIT \ud83d\udcb0\ud83c\udf89</b>"
            else:
                gtt_pnl = round((sl_premium - entry_premium) * qty, 0)
                gtt_leg = "TRAIL" if sl_premium > entry_premium else ("BE" if sl_premium == entry_premium else "SL")
                if gtt_leg == "TRAIL":
                    header = "\ud83c\udfc3 <b>TRAIL EXIT \u2014 PROFIT LOCKED \ud83d\udc9a</b>"
                elif gtt_leg == "BE":
                    header = "\u2696\ufe0f <b>BREAKEVEN EXIT \u2014 Capital Safe \ud83d\udee1\ufe0f</b>"
                else:
                    header = "\u274c <b>STOP LOSS HIT \ud83d\udcc9</b>"
            daily_pnl_rs += gtt_pnl
            pnl_icon = "\ud83d\udcb0" if gtt_pnl >= 0 else "\ud83d\udd34"
            send_telegram(
                f"{header}\n\n"
                f"\ud83d\udccc {position['symbol']}\n"
                f"Zerodha closed automatically.\n"
                f"{pnl_icon} Est. P&L: \u20b9{gtt_pnl:+,.0f}  |  Day: \u20b9{daily_pnl_rs:+,.0f}\n"
                f"<i>Check Kite app for exact fill price.</i>"
            )
            log_live_exit(gtt_leg, exit_spot=0, pnl_rs=gtt_pnl, max_fav_pt=max_fav)
            position = None
            save_position_state()
    except Exception as e:
        print(f"GTT status check error: {e}")

def save_position_state():
    global position
    if position is None:
        if os.path.exists(POSITION_FILE):
            try: os.remove(POSITION_FILE)
            except Exception as e: print(f"⚠️ Could not remove position file: {e}")
        return
    try:
        data = position.copy()
        data["entry_time"] = data["entry_time"].isoformat()
        with open(POSITION_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"⚠️ Could not save position state: {e}")

# ─── LIVE PERFORMANCE STATS (real numbers, never hardcoded) ───
def get_live_stats():
    """Read live_signal_log.csv and return actual win rate, P&L, trade count."""
    rows = _load_signal_log() if os.path.exists(SIGNAL_LOG_FILE) else []
    closed = [r for r in rows
              if r.get("mode") in ("LIVE", "MANUAL")
              and r.get("outcome") not in ("", "SIGNAL", "OPEN")]
    if not closed:
        return None
    total = len(closed)
    wins  = sum(1 for r in closed if r.get("outcome") in ("TARGET", "TRAIL", "GTT"))
    sl    = sum(1 for r in closed if r.get("outcome") == "SL")
    wr    = wins / total * 100
    net   = sum(float(r.get("pnl_rs") or 0) for r in closed)
    dates = sorted(r["date"] for r in closed if r.get("date"))
    return dict(total=total, wins=wins, sl=sl, wr=wr, net=net,
                first=dates[0] if dates else "", last=dates[-1] if dates else "")

def get_tuesday_stats():
    """Read tuesday_signal_log.csv and return actual Tuesday PE performance."""
    rows = _load_tuesday_log() if os.path.exists(TUESDAY_SIGNAL_LOG_FILE) else []
    closed = [r for r in rows if r.get("outcome") not in ("", "SIGNAL")]
    if not closed:
        return None
    total = len(closed)
    wins  = sum(1 for r in closed if r.get("outcome") in ("TARGET", "TRAIL"))
    sl    = sum(1 for r in closed if r.get("outcome") == "SL")
    wr    = wins / total * 100
    return dict(total=total, wins=wins, sl=sl, wr=wr)

def _stats_line(stats, label="Live"):
    """Format a one-line performance summary from get_live_stats() or get_tuesday_stats()."""
    if not stats or stats["total"] == 0:
        return "No closed trades yet"
    net_str = f" | Net: ₹{stats['net']:+,.0f}" if "net" in stats else ""
    return (f"{label} ({stats['total']} trades): "
            f"<b>{stats['wr']:.1f}% WR</b> | SL: {stats['sl']}{net_str}")

# ─── LIVE SIGNAL LOG ───
def _load_signal_log():
    if not os.path.exists(SIGNAL_LOG_FILE):
        return []
    try:
        with open(SIGNAL_LOG_FILE, "r", newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []

def _save_signal_log(rows):
    try:
        with open(SIGNAL_LOG_FILE, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=SIGNAL_LOG_FIELDS)
            w.writeheader()
            w.writerows(rows[-100:])   # keep max 100 rows
    except Exception as e:
        print(f"  [log] Save error: {e}")

def log_live_signal(signal, price, prev_low, symbol="", entry_premium=0,
                    sl_premium=0, target_premium=0, mode="SIGNAL", info=None, option_ltp=None):
    now = datetime.now()
    sl_spot = round(prev_low - CANDLE_SL_BUFFER, 2)
    vwap_val      = round(info["vwap"], 2)     if info and "vwap"      in info else ""
    prev_high_val = round(info["prev_high"], 2) if info and "prev_high" in info else ""
    prev_low_val  = round(info["prev_low"],  2) if info and "prev_low"  in info else round(prev_low, 2)
    st_val        = "UP" if (info and info.get("st")) else "DOWN" if info else ""
    row = {
        "date":             now.strftime("%Y-%m-%d"),
        "time":             now.strftime("%H:%M"),
        "day":              now.strftime("%A"),
        "signal":           signal,
        "entry_spot":       round(price, 2),
        "sl_spot":          sl_spot,
        "sl_dist_pt":       round(price - sl_spot, 1),
        "target_spot":      round(price + TARGET_PTS, 2),
        "vwap":             vwap_val,
        "prev_candle_high": prev_high_val,
        "prev_candle_low":  prev_low_val,
        "supertrend":       st_val,
        "option_ltp":       round(option_ltp, 2) if option_ltp else "",
        "symbol":           symbol,
        "entry_premium":    entry_premium,
        "sl_premium":       sl_premium,
        "target_premium":   target_premium,
        "mode":             mode,
        "outcome":          "SIGNAL",   # stays SIGNAL until execute_entry succeeds → updated to OPEN
        "exit_spot":        "",
        "exit_premium":     "",
        "pnl_rs":           "",
        "max_fav_pt":       "",
        "exit_time":        "",
    }
    rows = _load_signal_log()
    rows.append(row)
    _save_signal_log(rows)
    print(f"  [log] Signal logged: {signal} @ {price:.2f} ({mode})")

def update_log_last_open(**kwargs):
    """Promote the most recent SIGNAL row to OPEN — called after order actually fills."""
    rows = _load_signal_log()
    for r in reversed(rows):
        if r.get("outcome") == "SIGNAL":
            r["outcome"] = "OPEN"
            r.update({k: v for k, v in kwargs.items() if k in SIGNAL_LOG_FIELDS})
            break
    _save_signal_log(rows)

def log_live_exit(outcome, exit_spot=0, exit_premium=0, pnl_rs=0, max_fav_pt=0):
    rows = _load_signal_log()
    for r in reversed(rows):
        if r.get("outcome") == "OPEN":
            r["outcome"]      = outcome
            r["exit_spot"]    = round(float(exit_spot), 2) if exit_spot else ""
            r["exit_premium"] = exit_premium
            r["pnl_rs"]       = pnl_rs
            r["max_fav_pt"]   = round(float(max_fav_pt), 1) if max_fav_pt else ""
            r["exit_time"]    = datetime.now().strftime("%H:%M")
            break
    _save_signal_log(rows)
    print(f"  [log] Exit logged: {outcome}")

# ─── TUESDAY SIGNAL LOG ──────────────────────────────────────────────────────
def _load_tuesday_log():
    if not os.path.exists(TUESDAY_SIGNAL_LOG_FILE):
        return []
    try:
        with open(TUESDAY_SIGNAL_LOG_FILE, "r", newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []

def _save_tuesday_log(rows):
    try:
        with open(TUESDAY_SIGNAL_LOG_FILE, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=TUESDAY_SIGNAL_LOG_FIELDS, extrasaction='ignore')
            w.writeheader()
            for row in rows:
                row.setdefault("window", "morning")  # backward compat for old rows
            w.writerows(rows)
    except Exception as e:
        print(f"  [tue log] Save error: {e}")

def log_tuesday_signal(price, info, option_ltp=None, outcome="SIGNAL", window="morning"):
    now = datetime.now()
    prev_high = info.get("prev_high", 0) if info else 0
    prev_low  = info.get("prev_low",  0) if info else 0
    sl_spot   = round(prev_high + CANDLE_SL_BUFFER, 2)   # PE SL: prev candle high + buffer (tighter than signal candle high)
    sl_dist   = round(sl_spot - price, 1)
    tgt_pts   = TUESDAY_EVENING_TARGET if window == "evening" else TARGET_PTS
    tgt_spot  = round(price - tgt_pts, 2)
    vwap_val  = round(info["vwap"], 2) if info and "vwap" in info else ""
    st_val    = "DOWN" if info and not info.get("st") else ""

    symbol = ""
    try:
        sym = find_option_symbol(price, "SELL")
        if sym:
            symbol = sym
    except Exception:
        pass

    row = {
        "date":             now.strftime("%Y-%m-%d"),
        "time":             now.strftime("%H:%M"),
        "signal":           "PE",
        "entry_spot":       round(price, 2),
        "sl_spot":          sl_spot,
        "sl_dist_pt":       sl_dist,
        "target_spot":      tgt_spot,
        "vwap":             vwap_val,
        "prev_candle_high": round(prev_high, 2),
        "prev_candle_low":  round(prev_low, 2),
        "supertrend":       st_val,
        "option_ltp":       round(option_ltp, 2) if option_ltp else "",
        "symbol":           symbol,
        "window":           window,
        "outcome":          outcome,
    }
    rows = _load_tuesday_log()
    rows.append(row)
    _save_tuesday_log(rows)
    print(f"  [tue log] PE signal logged @ {price:.2f} ({window})")

def update_tuesday_log_outcome(outcome):
    """Update the most recent Tuesday SIGNAL row with final outcome."""
    rows = _load_tuesday_log()
    for r in reversed(rows):
        if r.get("outcome") == "SIGNAL":
            r["outcome"] = outcome
            break
    _save_tuesday_log(rows)


# ─── TUESDAY SIGNAL DETECTION (PE/SELL — bearish conditions) ────────────────
def check_tuesday_signals(df5):
    """
    Tuesday PE scanner — identical conditions to main scanner but bearish side:
    price < VWAP, Supertrend bearish, breakdown below prev candle low, bear clean candle.
    Window: 9:30-10:30 only. Signal-only, no orders.
    """
    if len(df5) < 30:
        return None, None, None

    df5 = df5.copy()
    df5['VWAP']       = calculate_vwap(df5)
    df5['Supertrend'] = calculate_supertrend(df5)
    df5 = df5.dropna(subset=['VWAP'])
    if len(df5) < 3:
        return None, None, None

    last_ts = df5.index[-1]
    if hasattr(last_ts, 'tzinfo') and last_ts.tzinfo is not None:
        last_ts = last_ts.replace(tzinfo=None)
    if (datetime.now() - last_ts).total_seconds() < 300:
        curr = df5.iloc[-2]; prev = df5.iloc[-3]
    else:
        curr = df5.iloc[-1]; prev = df5.iloc[-2]

    price = float(curr['Close'])
    o,h,l,c = float(curr['Open']),float(curr['High']),float(curr['Low']),float(curr['Close'])
    vwap  = float(curr['VWAP'])
    st    = bool(curr['Supertrend'])
    ph    = float(prev['High'])
    pl    = float(prev['Low'])

    is_doji, _, bear_clean = analyze_candle(o,h,l,c)
    if is_doji:
        return "SKIP", price, None

    # Sideways filter — same as main scanner
    curr_idx = len(df5) - 2 if (datetime.now() - last_ts).total_seconds() < 300 else len(df5) - 1
    recent = df5.iloc[curr_idx-4:curr_idx]
    recent_range = float(recent['High'].max()) - float(recent['Low'].min())
    if recent_range < SIDEWAYS_RANGE:
        return "SIDEWAYS", price, {"recent_range": round(recent_range, 1), "vwap": vwap, "st": st,
                                    "prev_low": pl, "prev_high": ph,
                                    "curr_low": l, "curr_high": h}

    # 4 conditions — bearish side (mirrors CE conditions exactly)
    cond_vwap  = price < vwap
    cond_st    = st == False
    cond_brk   = price < pl          # breakdown below prev candle low
    cond_clean = bear_clean

    info = {
        "vwap": vwap, "st": st,
        "prev_low": pl, "prev_high": ph,
        "curr_low": l, "curr_high": h,
        "cond_vwap_bear": cond_vwap,
        "cond_st_bear":   cond_st,
        "cond_brk_bear":  cond_brk,
    }

    if all([cond_vwap, cond_st, cond_brk, cond_clean]):
        return "SELL", price, info
    return None, price, info


def format_tuesday_alert(price, info, alert_num, option_ltp=None):
    now_str   = datetime.now().strftime("%d %b %Y %I:%M %p")
    prev_high = info.get("prev_high", price * 1.002) if info else price * 1.002
    sl_level  = round(prev_high + CANDLE_SL_BUFFER, 2)
    sl_pts    = round(sl_level - price, 1)
    tgt       = round(price - TARGET_PTS, 2)
    strike    = get_strike(price, "SELL")
    ltp_line  = f"💰 Option LTP: <b>₹{option_ltp}</b> (buy around this price)\n" if option_ltp else ""

    return f"""📉 <b>BUY PE 🔴</b>

📡 <b>TUESDAY WINDOW</b> (09:30-10:30 | Expiry Day)
🔵 SIGNAL ONLY — place order manually on Kite
📅 {now_str}
💹 BUY <b>{strike}</b>
📊 Nifty: {price:.2f}
{ltp_line}🛑 SL: {sl_level} (prev high {prev_high:.1f} + {CANDLE_SL_BUFFER}pt = {sl_pts:.0f}pt risk)
🎯 Target: {tgt} (-{TARGET_PTS}pt)
🔢 Alert #{alert_num}

━━━━━━━━━━━━━━━━━━━━━━━━
<b>All 4 conditions ✅</b>
  ✅ Price below VWAP
  ✅ Supertrend bearish
  ✅ Breakdown below prev candle
  ✅ Bear clean candle (no wick trap)

⚡ Expiry Day — theta decay accelerating
⚠️ <i>SIGNAL ONLY — no auto-execution on Tuesday</i>""".strip()


def format_tuesday_evening_alert(price, info, alert_num, option_ltp=None):
    now_str   = datetime.now().strftime("%d %b %Y %I:%M %p")
    prev_high = info.get("prev_high", price * 1.002) if info else price * 1.002
    sl_level  = round(prev_high + CANDLE_SL_BUFFER, 2)
    sl_pts    = round(sl_level - price, 1)
    tgt       = round(price - TUESDAY_EVENING_TARGET, 2)
    strike    = get_strike(price, "SELL")
    ltp_line  = f"💰 Option LTP: <b>₹{option_ltp}</b> (buy around this price)\n" if option_ltp else ""

    return f"""📉 <b>BUY PE 🔴 — EVENING WINDOW</b>

📡 <b>TUESDAY EVENING WINDOW</b> (13:00-14:30 | Expiry Day)
🔵 SIGNAL ONLY — place order manually on Kite
📅 {now_str}
💹 BUY <b>{strike}</b>
📊 Nifty: {price:.2f}
{ltp_line}🛑 SL: {sl_level} (prev high {prev_high:.1f} + {CANDLE_SL_BUFFER}pt = {sl_pts:.0f}pt risk)
🎯 Target: {tgt} (-{TUESDAY_EVENING_TARGET}pt)
🔢 Alert #{alert_num}

━━━━━━━━━━━━━━━━━━━━━━━━
<b>All 4 conditions ✅</b>
  ✅ Price below VWAP
  ✅ Supertrend bearish
  ✅ Breakdown below prev candle
  ✅ Bear clean candle (no wick trap)

⚡ Expiry Day — MAXIMUM theta decay in final 2 hours
⚠️ <i>SIGNAL ONLY — no auto-execution on Tuesday</i>""".strip()


def load_position_state():
    global position
    if not os.path.exists(POSITION_FILE):
        return
    try:
        with open(POSITION_FILE, "r") as f:
            data = json.load(f)
        data["entry_time"] = datetime.fromisoformat(data["entry_time"])
        position = data
        print(f"🔄 Recovered open position: {position['symbol']}")
        send_telegram(
            f"🔄 <b>MORNING POSITION RECOVERED AFTER RESTART</b>\n\n"
            f"{position['symbol']}\nEntry: {position['entry_premium']}\n"
            f"SL: {position['sl_premium']} | Target: {position['target_premium']}\n"
            f"Breakeven hit: {'Yes' if position['breakeven_hit'] else 'No'}\n\n"
            f"Resuming monitoring. GTT was untouched while bot was down."
        )
    except Exception as e:
        print(f"⚠️ Could not load position state: {e}")
        send_telegram(f"⚠️ <b>Found a saved morning position but couldn't load it!</b>\n{e}\n\nCheck Kite manually.")

def _signal_alert_loop(symbol, ltp, signal, sl_spot, duration=30, interval=5):
    """
    30-second broadcast after a signal — fires every 5s with live execution status.
    Runs whether auto-execution succeeded or failed (user always notified for 30s).
    Stops early if /stop_alerts is sent.
    """
    global _alerts_active
    _alerts_active = True
    opt_type = "CE" if signal == "BUY" else "PE"
    end_time = time.time() + duration
    count = 0
    try:
        while True:
            remaining = end_time - time.time()
            if remaining <= 0 or _alert_stop_event.is_set():
                break
            time.sleep(min(interval, remaining))
            if _alert_stop_event.is_set() or time.time() >= end_time:
                break
            count += 1
            if position is not None:
                status = f"✅ Executed: {position['symbol']} @ ₹{position.get('entry_premium','?')}"
            elif not AUTO_ARMED:
                status = "📌 Signal only — place manually on Kite"
            else:
                status = "🚨 NOT YET EXECUTED — open Kite NOW!"
            send_telegram(
                f"🔔 <b>Signal alert #{count} ({count*interval}s ago)</b>\n"
                f"BUY {symbol} ({opt_type}) @ ~₹{ltp}\n"
                f"SL spot ≈ {sl_spot:.1f}  |  Target: +{TARGET_PTS}pt spot\n"
                f"{status}\n"
                f"<i>/stop_alerts to stop reminders</i>"
            )
    finally:
        _alerts_active = False

def start_signal_alerts(symbol, ltp, signal, sl_spot):
    _alert_stop_event.clear()
    threading.Thread(
        target=_signal_alert_loop,
        args=(symbol, ltp, signal, sl_spot),
        daemon=True
    ).start()

def execute_entry(signal, price, curr_low):
    """Place a real BUY CE order. Called only when AUTO_ARMED and position is None."""
    global position, AUTO_ARMED
    qty = ACTIVE_LOTS * LOT_SIZE

    if daily_pnl_rs <= -DAILY_LOSS_LIMIT:
        AUTO_ARMED = False
        send_telegram(
            f"🛑 <b>Daily Loss Limit Hit — Auto-trading DISARMED</b>\n\n"
            f"Loss today: ₹{abs(daily_pnl_rs):,.0f} ≥ limit ₹{DAILY_LOSS_LIMIT:,.0f}\n"
            f"No new orders will be placed today.\n"
            f"Send /start_auto to re-arm manually if needed."
        )
        return False

    symbol = find_option_symbol(price, signal)
    if not symbol:
        send_telegram(f"❌ Could not find ATM CE symbol — auto-entry skipped")
        return False

    try:
        quote = kite.quote([f"NFO:{symbol}"])
        ltp = quote[f"NFO:{symbol}"]["last_price"]
    except Exception as e:
        send_telegram(f"❌ Could not fetch LTP for {symbol} — auto-entry skipped\n{e}")
        return False

    has_funds, available, required = check_sufficient_funds(ltp, qty)
    if not has_funds:
        send_telegram(
            f"❌ <b>INSUFFICIENT FUNDS — TRADE SKIPPED</b>\n\n"
            f"{symbol}\nRequired: ₹{required:,.0f} (+5% buffer)\n"
            f"Available: ₹{available:,.0f}\n\n"
            f"Add funds or reduce lots in /start_auto"
        )
        return False

    send_telegram(f"🤖 <b>AUTO-EXECUTING MORNING ENTRY</b>\nBUY {symbol}\nLots: {ACTIVE_LOTS} × {LOT_SIZE} = {qty} qty | LTP: {ltp}")

    order_id = place_entry_order(symbol, qty, ltp)
    if not order_id:
        return False  # broadcast loop already running from main — shows "NOT EXECUTED"

    # Wait up to 8s for fill — fast fail so we can cancel and retry with fresh price
    avg_price = get_order_avg_price(order_id, timeout=8)
    if avg_price is None:
        # Cancel the pending order before retrying — prevents ghost order on Kite
        try:
            kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=order_id)
            send_telegram(f"⚠️ Order {order_id} not filled in 8s — cancelled. Retrying with fresh price...")
        except Exception as ce:
            send_telegram(f"⚠️ Could not cancel order {order_id}: {ce}\nCheck Kite before retrying!")
        time.sleep(1)
        # Fetch fresh LTP before retry (price may have moved)
        try:
            ltp = kite.quote([f"NFO:{symbol}"])[f"NFO:{symbol}"]["last_price"]
        except Exception:
            pass  # use previous ltp if quote fails
        order_id = place_entry_order(symbol, qty, ltp)
        if not order_id:
            return False  # broadcast loop shows "NOT EXECUTED"
        avg_price = get_order_avg_price(order_id, timeout=8)
        if avg_price is None:
            try:
                kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=order_id)
            except Exception:
                pass
            send_telegram(f"❌ Both order attempts failed — EXECUTE MANUALLY on Kite")
            return False  # broadcast loop shows "NOT EXECUTED"

    # Candle-structure SL: few pts below signal candle low (tighter, more precise)
    sl_spot       = curr_low - CANDLE_SL_BUFFER
    sl_spot_dist  = price - sl_spot          # spot pts at risk
    sl_premium     = round(avg_price - sl_spot_dist * OPTION_DELTA, 1)
    target_premium = round(avg_price + TARGET_PTS    * OPTION_DELTA, 1)

    gtt_id = place_gtt_oco(symbol, qty, sl_premium, target_premium, avg_price)

    # Update live signal log with real fill details
    update_log_last_open(symbol=symbol, entry_premium=avg_price,
                         sl_premium=sl_premium, target_premium=target_premium,
                         mode="LIVE")

    position = {
        "symbol": symbol, "signal": signal, "qty": qty,
        "entry_spot": price, "entry_premium": avg_price,
        "entry_time": datetime.now(), "sl_premium": sl_premium,
        "target_premium": target_premium, "gtt_id": gtt_id,
        "breakeven_hit": False, "weak_checked": False, "order_id": order_id,
        "peak_favorable": 0.0, "trail_active": False
    }
    save_position_state()

    gtt_status = "✅ Active — Zerodha managing exits" if gtt_id else "⚠️ FAILED — manage SL/target manually!"
    max_rs_risk = round(sl_spot_dist * OPTION_DELTA * qty, 0)
    send_telegram(
        f"✅ <b>POSITION OPEN 🚀</b>\n\n"
        f"📌 <b>{symbol}</b>\n"
        f"💰 Entry: ₹<b>{avg_price}</b>  |  Qty: {qty}\n\n"
        f"🛑 SL: ₹{sl_premium}  (spot {sl_spot:.1f} = {sl_spot_dist:.1f}pt risk | max ₹{max_rs_risk:,.0f})\n"
        f"🎯 Target: ₹{target_premium}  (+{TARGET_PTS}pt spot)\n"
        f"⚖️ Breakeven triggers at +{round(price * BREAKEVEN_PCT, 1)}pt spot\n\n"
        f"🔒 GTT OCO: {gtt_status}"
    )
    return True

def check_spike(df5=None):
    """Detect intracandle momentum spike — fires for CE (up) or PE (down) instantly."""
    global _last_spike_candle
    if not SPIKE_ARMED or position is not None:
        return
    ct = datetime.now().time()
    if ct < dtime(9, 30) or ct > dtime(14, 40):
        # If there's a spike outside the window, notify but don't trade
        try:
            if df5 is None:
                df5 = fetch_data(NIFTY_TOKEN, "5minute", days=1)
            if df5 is not None and not df5.empty:
                candle_ts   = df5.index[-1]
                spike       = float(df5.iloc[-1]['Close']) - float(df5.iloc[-1]['Open'])
                if abs(spike) >= SPIKE_THRESHOLD and candle_ts != _last_spike_candle:
                    _last_spike_candle = candle_ts
                    send_telegram(
                        f"⚡ Spike detected: <b>{spike:+.0f}pt</b> — but outside spike window (9:30–14:40)\n"
                        f"No entry placed. Time: {ct.strftime('%H:%M')}"
                    )
        except Exception:
            pass
        return
    try:
        if df5 is None:
            df5 = fetch_data(NIFTY_TOKEN, "5minute", days=1)
        if df5 is None or df5.empty:
            return
        candle_ts    = df5.index[-1]
        candle_open  = float(df5.iloc[-1]['Open'])
        candle_close = float(df5.iloc[-1]['Close'])
        spike        = candle_close - candle_open   # +ve = up, -ve = down

        if abs(spike) >= SPIKE_THRESHOLD and candle_ts != _last_spike_candle:
            _last_spike_candle = candle_ts
            direction = "CE" if spike > 0 else "PE"
            signal    = "BUY" if direction == "CE" else "SELL"
            print(f"  ⚡ SPIKE {spike:+.0f}pt detected on {candle_ts} → {direction}")

            atm      = round(candle_close / STRIKE_GAP) * STRIKE_GAP
            sl_spot  = round(candle_close - SPIKE_SL_PTS, 0) if direction == "CE" else round(candle_close + SPIKE_SL_PTS, 0)
            tgt_spot = round(candle_close + TARGET_PTS,   0) if direction == "CE" else round(candle_close - TARGET_PTS,   0)
            sl_side  = "below" if direction == "CE" else "above"

            ltp_line = ""
            try:
                sym = find_option_symbol(candle_close, signal)
                if sym:
                    ltp      = kite.quote([f"NFO:{sym}"])[f"NFO:{sym}"]["last_price"]
                    sl_prem  = round(ltp - SPIKE_SL_PTS * OPTION_DELTA, 1)
                    tgt_prem = round(ltp + TARGET_PTS   * OPTION_DELTA, 1)
                    ltp_line = (
                        f"💰 Current LTP: ₹<b>{ltp}</b>\n"
                        f"🛑 SL: ₹{sl_prem}  (Nifty {sl_side} {sl_spot:.0f}, -{SPIKE_SL_PTS}pt)\n"
                        f"🎯 Target: ₹{tgt_prem}  (Nifty {tgt_spot:.0f}, +{TARGET_PTS}pt)\n"
                    )
            except Exception:
                ltp_line = f"🛑 SL: Nifty {sl_side} {sl_spot:.0f}  🎯 Target: Nifty {tgt_spot:.0f}\n"

            send_telegram(
                f"⚡ <b>MOMENTUM SPIKE — {direction} SIGNAL</b>\n\n"
                f"Candle: <b>{spike:+.0f}pt</b>  ({candle_open:.0f} → {candle_close:.0f})\n"
                f"Nifty Spot: <b>{candle_close:.0f}</b>\n\n"
                f"📌 Strike: <b>{atm} {direction}</b> (ATM — best liquidity & premium response)\n"
                f"{ltp_line}\n"
                f"⚠️ Verify on chart — spike can reverse. Enter on Kite manually if move looks real."
            )
    except Exception as e:
        print(f"Spike check error: {e}")


def execute_manual_entry(opt_type):
    """Manual CE or PE entry — triggered by /buy_ce or /buy_pe command anytime."""
    global position, daily_pnl_rs, AUTO_ARMED
    qty    = ACTIVE_LOTS * LOT_SIZE
    signal = "BUY" if opt_type == "CE" else "SELL"

    if position is not None:
        send_telegram(f"⚠️ Already have open position ({position['symbol']}) — close first with /square_off")
        return
    if daily_pnl_rs <= -DAILY_LOSS_LIMIT:
        send_telegram(f"🛑 Daily loss limit hit (₹{abs(daily_pnl_rs):,.0f}) — manual entry blocked.")
        return

    df = fetch_data(NIFTY_TOKEN, "5minute", days=2)
    if df is None or df.empty:
        send_telegram("❌ Cannot fetch market data — entry skipped"); return

    price      = float(df.iloc[-1]['Close'])
    prev_high  = float(df.iloc[-2]['High'])
    prev_low   = float(df.iloc[-2]['Low'])

    symbol = find_option_symbol(price, signal)
    if not symbol:
        send_telegram(f"❌ Cannot find ATM {opt_type} symbol"); return

    try:
        ltp = kite.quote([f"NFO:{symbol}"])[f"NFO:{symbol}"]["last_price"]
    except Exception as e:
        send_telegram(f"❌ Cannot fetch LTP for {symbol}\n{e}"); return

    has_funds, available, required = check_sufficient_funds(ltp, qty)
    if not has_funds:
        send_telegram(
            f"❌ <b>INSUFFICIENT FUNDS</b>\nRequired: ₹{required:,.0f} | Available: ₹{available:,.0f}"
        ); return

    send_telegram(f"🤖 <b>MANUAL {opt_type} ENTRY</b>\nBUY {symbol}\nLots: {ACTIVE_LOTS} × {LOT_SIZE} = {qty} | LTP: {ltp}")

    order_id = place_entry_order(symbol, qty, ltp)
    if not order_id: return

    avg_price = get_order_avg_price(order_id)
    if avg_price is None:
        try:
            kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=order_id)
            send_telegram(f"⚠️ Order {order_id} not filled in 30s — cancelled. Retry /buy_{opt_type.lower()} if still needed.")
        except Exception as ce:
            send_telegram(f"⚠️ Order {order_id} not filled and cancel failed: {ce}\nCHECK KITE MANUALLY — order may still be live!")
        return

    # Fixed SL distance (wider than morning candle SL — gives market room to test)
    # CE: SL if Nifty falls SPIKE_SL_PTS below entry
    # PE: SL if Nifty rises SPIKE_SL_PTS above entry
    sl_spot_dist   = SPIKE_SL_PTS
    sl_premium     = round(avg_price - sl_spot_dist * OPTION_DELTA, 1)
    target_premium = round(avg_price + TARGET_PTS * OPTION_DELTA, 1)
    sl_spot        = (price - sl_spot_dist) if opt_type == "CE" else (price + sl_spot_dist)

    gtt_id = place_gtt_oco(symbol, qty, sl_premium, target_premium, avg_price)
    log_live_signal(signal, price, prev_low, option_ltp=ltp)
    update_log_last_open(symbol=symbol, entry_premium=avg_price,
                         sl_premium=sl_premium, target_premium=target_premium, mode="MANUAL")

    position = {
        "symbol": symbol, "signal": signal, "qty": qty,
        "entry_spot": price, "entry_premium": avg_price,
        "entry_time": datetime.now(), "sl_premium": sl_premium,
        "target_premium": target_premium, "gtt_id": gtt_id,
        "breakeven_hit": False, "weak_checked": False, "order_id": order_id,
        "peak_favorable": 0.0, "trail_active": False
    }
    save_position_state()

    sl_side = f"below {sl_spot:.0f}" if opt_type == "CE" else f"above {sl_spot:.0f}"
    send_telegram(
        f"✅ <b>MANUAL {opt_type} POSITION OPEN</b>\n\n"
        f"{symbol}\nEntry: ₹{avg_price}\n"
        f"SL: ₹{sl_premium} (Nifty {sl_side}, {sl_spot_dist}pt room)\n"
        f"Target: ₹{target_premium} (+{TARGET_PTS}pt Nifty move)\n"
        f"Qty: {qty} | GTT: {'✅' if gtt_id else '⚠️ FAILED'}"
    )


def monitor_position(current_spot, option_ltp=None):
    """Breakeven, trailing stop, weak exit, hard exit — matches backtest logic exactly."""
    global position
    if position is None: return

    spot_move = (current_spot - position["entry_spot"]) if position["signal"] == "BUY" \
                else (position["entry_spot"] - current_spot)
    be_threshold = position["entry_spot"] * BREAKEVEN_PCT
    position["peak_favorable"] = max(position.get("peak_favorable", 0.0), spot_move)

    if TRAIL_ENABLED:
        # Dual BE check:
        # 1. Spot-based (mirrors backtest OHLC logic)
        # 2. Option LTP-based (catches intracandle moves the spot poll may miss)
        be_by_spot   = spot_move >= be_threshold
        be_by_option = (option_ltp is not None and
                        option_ltp - position["entry_premium"] >= BE_PREMIUM_PTS)
        if not position["breakeven_hit"] and (be_by_spot or be_by_option):
            trigger_src = "spot" if be_by_spot else f"option LTP {option_ltp}"
            if position["gtt_id"]:
                ok = modify_gtt_sl(position["gtt_id"], position["symbol"], position["qty"],
                                   position["entry_premium"], position["target_premium"],
                                   position["entry_premium"])
                if ok:
                    position["breakeven_hit"] = True
                    position["sl_premium"]    = position["entry_premium"]
                    save_position_state()
                    send_telegram(
                        f"⚖️ <b>BREAKEVEN ACTIVATED ✅</b>\n\n"
                        f"📌 {position['symbol']}\n"
                        f"🛡️ SL moved to entry: ₹{position['entry_premium']}\n"
                        f"Trigger: {trigger_src}\n"
                        f"<i>Worst case now: ₹0 exit — trade is risk-free 🎯</i>"
                    )

        trail_trigger_dist = be_threshold * TRAIL_TRIGGER_MULT
        trail_step_dist    = be_threshold * TRAIL_STEP_MULT
        if position["peak_favorable"] >= trail_trigger_dist and position["gtt_id"]:
            new_sl_spot_dist = position["peak_favorable"] - trail_step_dist
            new_sl_premium = round(position["entry_premium"] + new_sl_spot_dist * OPTION_DELTA, 1)
            is_better = new_sl_premium > position["sl_premium"]
            if is_better:
                est_current_premium = round(position["entry_premium"] + spot_move * OPTION_DELTA, 1)
                ok = modify_gtt_sl(position["gtt_id"], position["symbol"], position["qty"],
                                   new_sl_premium, position["target_premium"], est_current_premium)
                if ok:
                    old_sl_premium = position["sl_premium"]
                    locked_pts = round(new_sl_premium - position["entry_premium"], 1)
                    locked_rs  = round(locked_pts * position.get("qty", LOT_SIZE), 0)
                    position["sl_premium"] = new_sl_premium
                    position["breakeven_hit"] = True
                    position["trail_active"] = True
                    save_position_state()
                    send_telegram(
                        f"🔒 <b>TRAILING STOP UPDATED 📈</b>\n\n"
                        f"📌 {position['symbol']}\n"
                        f"🆕 New SL: ₹{new_sl_premium}  (was ₹{old_sl_premium})\n"
                        f"📊 Peak move: +{position['peak_favorable']:.1f}pt\n"
                        f"💚 Min profit locked: ₹{locked_rs:+,.0f}"
                    )

    elapsed_min = (datetime.now() - position["entry_time"]).total_seconds() / 60
    if elapsed_min >= 15 and not position["weak_checked"]:
        position["weak_checked"] = True
        if spot_move < MOMENTUM_MIN:
            send_telegram(
                f"😴 <b>WEAK MOMENTUM EXIT 📉</b>\n\n"
                f"📌 {position['symbol']}\n"
                f"Only +{spot_move:.1f}pt after 15 min (need {MOMENTUM_MIN}pt)\n"
                f"Exiting to protect capital — better safe than sorry."
            )
            exit_position("WEAK", exit_spot=current_spot)
            return

    if datetime.now().time() >= HARD_EXIT:
        send_telegram(
            f"⏰ <b>HARD EXIT — 3:10 PM 🔔</b>\n\n"
            f"📌 {position['symbol']}\n"
            f"End-of-day — closing position now."
        )
        exit_position("EOD", exit_spot=current_spot)

def exit_position(reason, exit_spot=0):
    """Cancel GTT + limit exit. Retries once on failure. Position only cleared if order succeeds."""
    global position, daily_pnl_rs
    if position is None: return
    max_fav    = position.get("peak_favorable", 0)
    entry_spot = position.get("entry_spot", 0)
    qty        = position.get("qty", LOT_SIZE)
    direction  = 1 if position.get("signal", "BUY") == "BUY" else -1
    pnl_rs     = round((exit_spot - entry_spot) * direction * OPTION_DELTA * qty, 0) if exit_spot else 0
    # daily_pnl_rs updated only after exit succeeds — prevents double-count if both attempts fail
    if position["gtt_id"]:
        cancel_gtt(position["gtt_id"])
    order_id = place_exit_order(position["symbol"], qty)
    if not order_id:
        send_telegram(
            f"🚨 <b>EXIT ORDER FAILED — retrying in 10s</b>\n"
            f"{position['symbol']}\nClose manually on Kite if retry also fails!"
        )
        time.sleep(10)
        order_id = place_exit_order(position["symbol"], qty)
        if not order_id:
            send_telegram(
                f"❌ <b>BOTH EXIT ATTEMPTS FAILED</b>\n"
                f"CLOSE {position['symbol']} MANUALLY ON KITE NOW!\n"
                f"Bot will keep monitoring until you do."
            )
            return  # position stays set — bot keeps monitoring
    daily_pnl_rs += pnl_rs
    pnl_icon = "💰" if pnl_rs >= 0 else "🔴"
    send_telegram(
        f"🔚 <b>POSITION CLOSED ({reason})</b>\n\n"
        f"📌 {position['symbol']}\n"
        f"{pnl_icon} P&L: ₹{pnl_rs:+,.0f}  |  Day: ₹{daily_pnl_rs:+,.0f}"
    )
    log_live_exit(reason, exit_spot=exit_spot, pnl_rs=pnl_rs, max_fav_pt=max_fav)
    position = None
    save_position_state()

# ─── SIGNAL ENGINE — CE-only ATM, 4 conditions ───
# Backtest (90 days): 34 trades, 91.2% win rate, 0 SL, Rs11,147 net
# SL: prev candle low - 5pt (dynamic). Target: 25pt fixed.
# PE in morning = 41% win rate → CE-only confirmed as best.
def check_signals_relaxed(df5):
    if len(df5) < 30:
        return None, None, None, None

    df5 = df5.copy()
    df5['VWAP']       = calculate_vwap(df5)
    df5['Supertrend'] = calculate_supertrend(df5)

    df5 = df5.dropna(subset=['VWAP'])
    if len(df5) < 3:
        return None, None, None, None

    # Always use COMPLETED candles only — skip the in-progress candle.
    # Kite returns the current forming candle as the last row. At 09:32 the
    # 09:30 candle has no upper wick yet (looks clean), but by 09:35 close it
    # may have a large wick that fails the clean-candle check. Using a partial
    # candle causes false signals that the backtest (completed candles only)
    # would never take. Fix: if the last candle started < 5 min ago, skip it.
    last_ts = df5.index[-1]
    if hasattr(last_ts, 'tzinfo') and last_ts.tzinfo is not None:
        last_ts = last_ts.replace(tzinfo=None)
    if (datetime.now() - last_ts).total_seconds() < 300:
        curr = df5.iloc[-2]; prev = df5.iloc[-3]
    else:
        curr = df5.iloc[-1]; prev = df5.iloc[-2]

    price = float(curr['Close'])
    o,h,l,c = float(curr['Open']),float(curr['High']),float(curr['Low']),float(curr['Close'])
    vwap  = float(curr['VWAP'])
    st    = bool(curr['Supertrend'])
    ph    = float(prev['High'])
    pl    = float(prev['Low'])

    is_doji, bull_clean, _ = analyze_candle(o,h,l,c)
    if is_doji:
        return "SKIP", price, None, None

    # ── Sideways check: 4 candles BEFORE the signal candle (not including it) ──
    curr_idx = len(df5) - 2 if (datetime.now() - last_ts).total_seconds() < 300 else len(df5) - 1
    recent = df5.iloc[curr_idx-4:curr_idx]
    recent_range = float(recent['High'].max()) - float(recent['Low'].min())
    if recent_range < SIDEWAYS_RANGE:
        return "SIDEWAYS", price, {"recent_range": round(recent_range, 1), "vwap": vwap, "st": st,
                                   "prev_low": pl, "prev_high": ph,
                                   "curr_low": l, "curr_high": h,
                                   "cond_vwap_bull": price > vwap, "cond_st_bull": st,
                                   "cond_brk_bull": price > ph}, None

    # 4 conditions — VWAP + Supertrend + Breakout + Bull clean candle
    cond_vwap  = price > vwap
    cond_st    = st == True
    cond_brk   = price > ph
    cond_clean = bull_clean
    buy_ok = all([cond_vwap, cond_st, cond_brk, cond_clean])

    info = {
        "vwap": vwap, "st": st,
        "prev_low": pl, "prev_high": ph,
        "curr_low": l, "curr_high": h,
        "cond_vwap_bull": cond_vwap,
        "cond_st_bull":   cond_st,
        "cond_brk_bull":  cond_brk,
    }

    if buy_ok:
        return "BUY", price, info, "CE"
    return None, price, info, None

def send_market_status(price, info, alerts_today):
    global last_heartbeat
    now = datetime.now()
    if last_heartbeat is not None and (now - last_heartbeat).total_seconds() < HEARTBEAT_MINS * 60:
        return
    last_heartbeat = now

    ck = lambda v: "✅" if v else "❌"
    if info and price:
        vwap  = info.get("vwap", 0)
        c_vwap = ck(info.get("cond_vwap_bull"))
        c_st   = ck(info.get("cond_st_bull"))
        c_brk  = ck(info.get("cond_brk_bull"))
        score  = sum(1 for k in ["cond_vwap_bull","cond_st_bull","cond_brk_bull"] if info.get(k))
        rng    = info.get("recent_range", None)
        market_note = (f"📊 Market sideways ({rng:.0f}pt range in last 4 candles) — waiting for breakout"
                       if rng is not None and rng < SIDEWAYS_RANGE else "")
        msg = (
            f"🌡️ <b>Scan Window — {now.strftime('%H:%M')}</b>\n\n"
            f"NIFTY: <b>{price:.1f}</b>   VWAP: {vwap:.1f}\n"
            f"CE-only scan (BUY signals only)\n\n"
            f"<b>Conditions ({score}/3):</b>\n"
            f"  {c_vwap} Price above VWAP\n"
            f"  {c_st} Supertrend bullish\n"
            f"  {c_brk} Breakout above prev candle\n\n"
            f"Alerts fired today: {alerts_today}/{MAX_ALERTS}\n"
            + (f"\n{market_note}\n" if market_note else "")
            + f"<i>Signal fires when all 3 + bull clean candle + sideways range > {SIDEWAYS_RANGE}pt</i>"
        )
    else:
        msg = (
            f"🌡️ <b>Scan Window — {now.strftime('%H:%M')}</b>\n"
            f"NIFTY: {price:.1f if price else 'N/A'}  |  Alerts: {alerts_today}/{MAX_ALERTS}"
        )
    send_telegram(msg)


def get_expiry_label():
    day = datetime.now().weekday()
    names = {0:"Monday",1:"Tuesday",2:"Wednesday",3:"Thursday",4:"Friday"}
    if day == EXPIRY_WEEKDAY:
        return f"{names.get(day)} Expiry ⚠️ — extra caution, fast theta decay"
    return names.get(day, "Weekend")

def format_alert(signal, price, info, alert_num, option_ltp=None):
    now_str  = datetime.now().strftime("%d %b %Y %I:%M %p")
    strike   = get_strike(price, signal)
    curr_low = info.get("curr_low", price * 0.998) if info else price * 0.998
    sl_level = round(curr_low - CANDLE_SL_BUFFER, 2)
    sl_pts   = round(price - sl_level, 1)
    tgt      = round(price + TARGET_PTS, 2)
    be_pts   = round(price * BREAKEVEN_PCT, 1)

    mode_line = "🟢 AUTO-ARMED — order placed automatically" if AUTO_ARMED else "🔵 SIGNAL-ONLY — place order manually on Kite"
    ltp_line  = f"💰 Option LTP: <b>₹{option_ltp}</b> (buy around this price)\n" if option_ltp else ""

    rr = round(TARGET_PTS / sl_pts, 1) if sl_pts else "?"
    msg = f"""🚀 <b>CE SIGNAL — BUY NOW 🟢</b>

📡 <b>MORNING WINDOW</b> ({MORNING_START.strftime('%H:%M')}–{MORNING_END.strftime('%H:%M')})
{mode_line}
📅 {now_str}
💹 BUY <b>{strike}</b>
📊 Nifty: <b>{price:.2f}</b>
{ltp_line}🛑 SL: <b>{sl_level}</b>  (candle low {curr_low:.1f} − {CANDLE_SL_BUFFER}pt = <b>{sl_pts:.0f}pt risk</b>)
🎯 Target: <b>{tgt}</b>  (+{TARGET_PTS}pt)
📐 R:R = 1 : {rr}
⚖️ Breakeven activates at +{be_pts:.0f}pt spot move
🔢 Signal #{alert_num}/{MAX_ALERTS}

━━━━━━━━━━━━━━━━━━━━━━━━
💎 <b>All 4 conditions confirmed ✅</b>
  ✅ Price above VWAP
  ✅ Supertrend bullish
  ✅ Breakout above prev candle high
  ✅ Bull clean candle (tight wick)

📅 {get_expiry_label()}"""
    return msg.strip()



# ─── EOD REPORT ─────────────────────────────────────────────────────────────
def send_eod_report():
    rows = _load_signal_log()
    today_str = datetime.now().strftime('%Y-%m-%d')
    today_rows = [r for r in rows if r.get('date','') == today_str]
    closed = [r for r in today_rows if r.get('outcome','') not in ('', 'SIGNAL', 'OPEN')]
    date_label = datetime.now().strftime('%d %b')

    if not today_rows:
        send_telegram(f"📊 <b>EOD Report — {date_label}</b>\n\nNo signals today. 😴")
        return

    WIN_OUTCOMES = ('TARGET', 'TRAIL', 'GTT')
    wins      = [r for r in closed if r.get('outcome','') in WIN_OUTCOMES]
    losses    = [r for r in closed if r.get('outcome','') == 'SL']
    total_pnl = sum(float(r.get('pnl_rs') or 0) for r in closed)

    if total_pnl > 0:
        day_icon = "🟢 <b>PROFIT DAY</b> 🎉"
    elif total_pnl < 0:
        day_icon = "🔴 <b>LOSS DAY</b>"
    else:
        day_icon = "⚪ <b>BREAKEVEN DAY</b>"

    OUTCOME_ICONS = {'TARGET': '🎯', 'TRAIL': '🏃', 'BE': '⚖️', 'WEAK': '😴', 'SL': '❌', 'EOD': '⏰'}
    msg = (f"📊 <b>EOD Report — {date_label}</b>\n"
           f"{day_icon}\n\n"
           f"Signals: {len(today_rows)} | Closed: {len(closed)}\n"
           f"✅ Wins: {len(wins)}  ❌ SL: {len(losses)}\n"
           f"💰 <b>P&L: ₹{total_pnl:+,.0f}</b>\n")
    if closed:
        msg += "\n<b>Trade log:</b>"
        for r in closed:
            oc   = r.get('outcome','')
            icon = OUTCOME_ICONS.get(oc, '•')
            pnl  = float(r.get('pnl_rs') or 0)
            pnl_str = f"₹{pnl:+,.0f}"
            msg += f"\n  {icon} {r.get('time','')}  {oc:<7}  {pnl_str}"
    else:
        msg += "\nNo closed trades yet."
    send_telegram(msg)


# ─── MAIN ───
def run_scanner():
    global daily_pnl_rs, eod_report_sent, premarket_sent, spike_disarmed_today, SPIKE_ARMED
    print("="*65)
    print("  NIFTY MORNING SCANNER — CE ONLY + AUTO EXECUTION")
    print(f"  Window: {MORNING_START.strftime('%H:%M')}-{MORNING_END.strftime('%H:%M')}")
    print("  4 conditions: VWAP + Supertrend + Breakout + Clean candle")
    print(f"  SL: signal candle low - {CANDLE_SL_BUFFER}pt (dynamic) | Target: {TARGET_PTS}pt fixed")
    _s = get_live_stats()
    _sl = _stats_line(_s) if _s else "No live trades yet — check live_signal_log.csv"
    print(f"  CE (BUY) only | {_sl}")
    print("  Auto-trade: DISARMED (send /start_auto to arm)")
    print("="*65)

    if not login(): return

    load_position_state()  # recover any position from before a restart

    # Restore today's trade state from live log — prevents a second same-day trade
    # after a manual restart (backtest allows exactly 1 trade per day direction)
    alerts_today = 0
    last_dir     = None
    _today_str   = datetime.now().strftime("%Y-%m-%d")
    _today_live  = [r for r in _load_signal_log()
                    if r.get("date") == _today_str and r.get("mode") == "LIVE"]
    if _today_live:
        last_dir     = _today_live[-1].get("signal")   # BUY or SELL
        alerts_today = len(_today_live)
        print(f"  [restore] {len(_today_live)} LIVE trade(s) already today — last_dir={last_dir} (blocking new same-dir signals)")
        send_telegram(
            f"♻️ <b>Scanner restarted mid-day</b>\n"
            f"{len(_today_live)} LIVE trade(s) found today — same-direction signals BLOCKED\n"
            f"last_dir={last_dir} | alerts={alerts_today}"
        )

    _s = get_live_stats()
    _perf = _stats_line(_s) if _s else "No closed trades yet — first run"
    send_telegram(
        "🌅 <b>Morning Scanner Started ✅</b>\n\n"
        f"⏰ Window: {MORNING_START.strftime('%H:%M')}–{MORNING_END.strftime('%H:%M')} | Skip Tuesday\n"
        "📋 Strategy: <b>CE (BUY) only — ATM, 4 conditions</b>\n"
        "  ✅ VWAP + Supertrend + Breakout + Clean candle\n"
        f"  🛑 SL: signal candle low − {CANDLE_SL_BUFFER}pt  🎯 Target: {TARGET_PTS}pt fixed\n\n"
        f"📊 {_perf}\n\n"
        "🔴 <b>Auto-trade: DISARMED</b> — signal-only mode\n"
        "Send /start_auto to enable real order placement\n"
        "Send /morning_help for all commands"
    )

    # alerts_today and last_dir already restored above from live log (if mid-day restart)
    last_date = None; window_opened_today = False
    last_position_signal_candle = None
    last_signal_candle = None
    no_signal_alerted = False
    tue_alerts_today = 0; last_tue_candle = None; tue_window_opened = False
    tue_eve_alerts_today = 0; last_tue_eve_candle = None; tue_eve_window_opened = False

    while True:
        try:
            process_telegram_commands()
            now = datetime.now(); ct = now.time(); cd = now.date()

            if last_date != cd:
                alerts_today = 0; last_date = cd; last_dir = None; last_position_signal_candle = None
                last_signal_candle = None
                window_opened_today = False
                daily_pnl_rs         = 0.0
                eod_report_sent      = False
                premarket_sent       = False
                spike_disarmed_today = False
                no_signal_alerted    = False
                _symbol_cache.clear()   # fresh instruments lookup each new trading day
                tue_alerts_today = 0; last_tue_candle = None; tue_window_opened = False
                tue_eve_alerts_today = 0; last_tue_eve_candle = None; tue_eve_window_opened = False
                print(f"\n📅 New day: {cd}")
                if login():
                    print("🔐 Re-logged in for new day")
                else:
                    send_telegram("🆘 <b>MORNING SCANNER — NEW DAY LOGIN FAILED</b>")

            if now.weekday() >= 5:  # Saturday=5, Sunday=6
                print(f"⏳ [{now.strftime('%H:%M')}] Weekend — market closed")
                sleep_poll(3600); continue

            if now.weekday() == EXPIRY_WEEKDAY and position is None:
                # ─── TUESDAY MORNING PE WINDOW (9:30-10:30, signal only) ───
                if dtime(9, 30) <= ct <= TUESDAY_END:
                    if not tue_window_opened:
                        tue_window_opened = True
                        _ts = get_tuesday_stats()
                        _tue_perf = _stats_line(_ts, label="Tue PE") if _ts else "No Tuesday trades yet"
                        send_telegram(
                            "📉 <b>Tuesday Morning Window OPEN</b> (09:30-10:30)\n\n"
                            "Strategy: <b>PE (SELL) only — ATM, 4 conditions</b>\n"
                            "  VWAP + Supertrend + Breakdown + Bear clean candle\n"
                            f"  SL: prev high + {CANDLE_SL_BUFFER}pt | Target: {TARGET_PTS}pt\n"
                            f"📊 {_tue_perf}\n\n"
                            "⏰ Evening window opens at 13:00\n"
                            "⚠️ <i>SIGNAL ONLY — no auto-execution on Tuesday</i>"
                        )
                    df5 = fetch_data(NIFTY_TOKEN, "5minute", days=5)
                    if df5 is not None:
                        sig, price, info = check_tuesday_signals(df5)
                        if price:
                            print(f"  [{now.strftime('%H:%M')}] Tue Morn PE scan | Nifty:{price:.2f} | Signal:{sig or 'None'}")
                        sig_candle = df5.index[-2] if len(df5) >= 2 else None
                        if sig == "SELL" and sig_candle != last_tue_candle:
                            last_tue_candle = sig_candle
                            tue_alerts_today += 1
                            opt_ltp = None
                            try:
                                _sym = find_option_symbol(price, "SELL")
                                if _sym:
                                    opt_ltp = kite.quote([f"NFO:{_sym}"])[f"NFO:{_sym}"]["last_price"]
                            except Exception:
                                pass
                            send_telegram(format_tuesday_alert(price, info, tue_alerts_today, opt_ltp))
                            log_tuesday_signal(price, info, option_ltp=opt_ltp, window="morning")
                        elif sig == "SIDEWAYS":
                            rng = info.get("recent_range", 0) if info else 0
                            print(f"  📊 Tue Morn sideways ({rng:.0f}pt) — waiting")
                    sleep_poll(1); continue

                # ─── TUESDAY EVENING PE WINDOW (13:00-14:30, signal only) ───
                elif TUESDAY_EVENING_START <= ct <= TUESDAY_EVENING_END:
                    if not tue_eve_window_opened:
                        tue_eve_window_opened = True
                        _ts = get_tuesday_stats()
                        _tue_perf = _stats_line(_ts, label="Tue PE") if _ts else "No Tuesday trades yet"
                        send_telegram(
                            "📉 <b>Tuesday Evening Window OPEN</b> (13:00-14:30)\n\n"
                            "Strategy: <b>PE (SELL) only — ATM, 4 conditions</b>\n"
                            "  VWAP + Supertrend + Breakdown + Bear clean candle\n"
                            f"  SL: prev high + {CANDLE_SL_BUFFER}pt | Target: {TUESDAY_EVENING_TARGET}pt\n"
                            f"📊 {_tue_perf}\n\n"
                            "⚡ Expiry Day — maximum theta decay in final hours\n"
                            "⚠️ <i>SIGNAL ONLY — no auto-execution on Tuesday</i>"
                        )
                    df5 = fetch_data(NIFTY_TOKEN, "5minute", days=5)
                    if df5 is not None:
                        sig, price, info = check_tuesday_signals(df5)
                        if price:
                            print(f"  [{now.strftime('%H:%M')}] Tue Eve PE scan | Nifty:{price:.2f} | Signal:{sig or 'None'}")
                        sig_candle = df5.index[-2] if len(df5) >= 2 else None
                        if sig == "SELL" and sig_candle != last_tue_eve_candle:
                            last_tue_eve_candle = sig_candle
                            tue_eve_alerts_today += 1
                            opt_ltp = None
                            try:
                                _sym = find_option_symbol(price, "SELL")
                                if _sym:
                                    opt_ltp = kite.quote([f"NFO:{_sym}"])[f"NFO:{_sym}"]["last_price"]
                            except Exception:
                                pass
                            send_telegram(format_tuesday_evening_alert(price, info, tue_eve_alerts_today, opt_ltp))
                            log_tuesday_signal(price, info, option_ltp=opt_ltp, window="evening")
                        elif sig == "SIDEWAYS":
                            rng = info.get("recent_range", 0) if info else 0
                            print(f"  📊 Tue Eve sideways ({rng:.0f}pt) — waiting")
                    sleep_poll(1); continue

                # ─── BETWEEN/AFTER WINDOWS ───────────────────────────────────
                else:
                    # Morning window just closed
                    if tue_window_opened and ct > TUESDAY_END:
                        if tue_alerts_today == 0:
                            send_telegram("📭 <b>Tuesday morning window closed (10:30)</b> — no PE signal.\n⏰ Evening window opens at 13:00")
                        else:
                            send_telegram(f"🔒 <b>Tuesday morning window closed (10:30)</b> — {tue_alerts_today} PE signal(s) sent.\n⏰ Evening window opens at 13:00")
                        tue_window_opened = False
                    # Evening window just closed
                    if tue_eve_window_opened and ct > TUESDAY_EVENING_END:
                        if tue_eve_alerts_today == 0:
                            send_telegram("📭 <b>Tuesday evening window closed (14:30)</b> — no PE signal today.")
                        else:
                            send_telegram(f"🔒 <b>Tuesday evening window closed (14:30)</b> — {tue_eve_alerts_today} PE signal(s) sent today.")
                        tue_eve_window_opened = False
                    print(f"⏳ [{now.strftime('%H:%M')}] Tuesday — outside PE windows (9:30-10:30 / 13:00-14:30)")
                    sleep_poll(300); continue

            # ─── PRE-MARKET ALERT (9:24-9:29) ────────────────────────────────
            if not premarket_sent and dtime(9, 24) <= ct <= dtime(9, 29):
                premarket_sent = True
                try:
                    df_pre = fetch_data(NIFTY_TOKEN, '5minute', days=1)
                    price_now = float(df_pre.iloc[-1]['Close']) if df_pre is not None and not df_pre.empty else 0
                    price_str = f"{price_now:.0f}" if price_now else "N/A"
                except Exception:
                    price_str = "N/A"
                armed_str = f"{ACTIVE_LOTS} lot(s) ARMED" if AUTO_ARMED else "DISARMED — send /start_auto"
                send_telegram(
                    f"⏰ <b>Pre-Market — Scan opens in ~5 min</b>\n\n"
                    f"Nifty: {price_str}\n"
                    f"Window: 9:30–13:00 | Skip Tuesday\n"
                    f"Auto-trade: {armed_str}"
                )

            # ─── SPIKE AUTO-DISARM (2:40 PM) ──────────────────────────────────
            if not spike_disarmed_today and ct >= dtime(14, 40):
                spike_disarmed_today = True
                if SPIKE_ARMED:
                    SPIKE_ARMED = False
                    send_telegram("⚡ Spike detector auto-disarmed — window closed (2:40 PM)")

            # ─── EOD REPORT (3:30 PM) ─────────────────────────────────────────
            if not eod_report_sent and ct >= dtime(15, 30):
                eod_report_sent = True
                send_eod_report()

            # ─── POSITION MONITORING (runs all day regardless of window) ───
            if position is not None:
                check_gtt_triggered()   # detect if GTT (SL/target) fired externally
                if position is not None:
                    # Use real-time LTP — NOT completed-candle close (which is up to 5min stale
                    # and would miss intracandle highs that trigger BE/trail in backtest OHLC)
                    try:
                        nifty_ltp_data = kite.ltp(["NSE:NIFTY 50"])
                        current_price  = float(nifty_ltp_data["NSE:NIFTY 50"]["last_price"])
                    except Exception as ltp_err:
                        print(f"  [{now.strftime('%H:%M')}] Nifty LTP error: {ltp_err} — skipping cycle")
                        sleep_poll(30); continue
                    opt_ltp = None
                    try:
                        opt_data = kite.ltp([f"NFO:{position['symbol']}"])
                        opt_ltp  = float(opt_data[f"NFO:{position['symbol']}"]["last_price"])
                    except Exception:
                        pass
                    print(f"  [{now.strftime('%H:%M')}] Monitoring {position['symbol']} | Nifty: {current_price:.2f} | Option: {opt_ltp or 'N/A'}")
                    monitor_position(current_price, option_ltp=opt_ltp)
                sleep_poll(30); continue  # 30s poll — real-time LTP catches intracandle moves

            # ─── OUTSIDE SCAN WINDOW — idle (spike detector still runs) ───
            if ct < MORNING_START or ct > MORNING_END:
                check_spike()   # runs even outside morning window
                print(f"⏳ [{now.strftime('%H:%M')}] Outside scan window (9:30-13:00) — idle")
                sleep_poll(30 if SPIKE_ARMED else 120); continue

            if not window_opened_today:
                window_opened_today = True
                armed_label = "🟢 AUTO-ARMED — will place real orders" if AUTO_ARMED else "🔴 Signal-only (send /start_auto to arm)"
                _s = get_live_stats()
                _perf = _stats_line(_s) if _s else "No live trades yet"
                send_telegram(
                    f"🌅 <b>Scan Window OPEN 🟢</b>\n\n"
                    f"⏰ {now.strftime('%H:%M')} — Scanning till {MORNING_END.strftime('%H:%M')}\n"
                    "📋 Strategy: <b>CE (BUY) only — ATM, 4 conditions</b>\n"
                    "  VWAP + Supertrend + Breakout + Clean candle\n"
                    f"  🛑 SL: signal low − {CANDLE_SL_BUFFER}pt  🎯 Target: {TARGET_PTS}pt\n"
                    f"📊 {_perf}\n\n"
                    f"{armed_label}"
                )

            # ─── NO SIGNAL ALERT (11:30 AM) ───────────────────────────────────
            if not no_signal_alerted and alerts_today == 0 and ct >= dtime(11, 30):
                no_signal_alerted = True
                send_telegram(
                    f"📭 <b>No CE signal by 11:30 AM</b>\n\n"
                    f"Market may be sideways or conditions not aligning.\n"
                    f"Continuing to scan till {MORNING_END.strftime('%H:%M')} — stay patient."
                )

            if alerts_today >= MAX_ALERTS:
                print(f"🚫 Max alerts reached ({MAX_ALERTS}) — idle till next day")
                sleep_poll(120); continue

            df5 = fetch_data(NIFTY_TOKEN, "5minute", days=5)
            if df5 is None:
                print("❌ Data failed"); sleep_poll(60); continue

            signal, price, info, opt_type = check_signals_relaxed(df5)
            if price:
                print(f"  [{now.strftime('%H:%M')}] Nifty:{price:.2f} | Signal:{signal or 'None'} | Auto:{'ON' if AUTO_ARMED else 'OFF'}")

            if signal == "SKIP":
                print("  ⛔ Doji — skipping")
            elif signal == "SIDEWAYS":
                rng = info.get("recent_range", 0) if info else 0
                print(f"  📊 Sideways ({rng:.0f}pt range) — skipping")
                send_market_status(price, info, alerts_today)
            elif signal in ("BUY", "SELL"):
                if position is not None:
                    # Position open — send full info signal for each NEW candle so user can act manually
                    # Deduplicate by candle timestamp (avoid 30s-poll spam on same candle)
                    sig_candle = df5.index[-2] if len(df5) >= 2 else None
                    if sig_candle != last_position_signal_candle:
                        last_position_signal_candle = sig_candle
                        last_heartbeat = now
                        curr_low = info.get("curr_low", price * 0.998) if info else price * 0.998
                        opt_ltp = None
                        try:
                            _sym = find_option_symbol(price, signal)
                            if _sym:
                                opt_ltp = kite.quote([f"NFO:{_sym}"])[f"NFO:{_sym}"]["last_price"]
                        except Exception:
                            pass
                        alert_msg = format_alert(signal, price, info, alerts_today, option_ltp=opt_ltp)
                        alert_msg += f"\n\n⚠️ <i>Position already open ({position['symbol']}) — no new order. Manual check if needed.</i>"
                        send_telegram(alert_msg)
                        print(f"  🌅 {signal} signal (info only — position open: {position['symbol']})")
                    else:
                        print(f"  ⏭️ Same candle {signal} (position open) — already notified")
                elif signal == last_dir:
                    print(f"  ⏭️ Duplicate {signal} — skipping")
                elif (df5.index[-2] if len(df5) >= 2 else None) == last_signal_candle:
                    print(f"  ⏭️ Same candle as last execution attempt — waiting for new candle")
                else:
                    sig_candle_now = df5.index[-2] if len(df5) >= 2 else None
                    last_signal_candle = sig_candle_now
                    _prev_dir = last_dir   # save before modifying — needed for SL-too-wide restore
                    alerts_today += 1; last_dir = signal
                    last_heartbeat = now
                    curr_low = info.get("curr_low", price * 0.998) if info else price * 0.998
                    _sl_dist = price - curr_low + CANDLE_SL_BUFFER
                    if _sl_dist > MAX_CANDLE_SL_PTS:
                        print(f"  ⚠️ SL too wide ({_sl_dist:.1f}pt > {MAX_CANDLE_SL_PTS}pt) — skipping signal")
                        send_telegram(f"⚠️ Signal detected but SL too wide ({_sl_dist:.1f}pt risk) — skipped (max {MAX_CANDLE_SL_PTS}pt)")
                        alerts_today -= 1
                        last_dir = _prev_dir   # restore — if a trade already ran today, keep it blocked
                        continue
                    print(f"  🌅 {signal} alert #{alerts_today} | AUTO_ARMED={AUTO_ARMED}")
                    # Fetch option LTP so signal message shows it for manual entry
                    opt_ltp = None
                    try:
                        _sym = find_option_symbol(price, signal)
                        if _sym:
                            opt_ltp = kite.quote([f"NFO:{_sym}"])[f"NFO:{_sym}"]["last_price"]
                    except Exception:
                        pass
                    send_telegram(format_alert(signal, price, info, alerts_today, option_ltp=opt_ltp))
                    log_live_signal(signal, price, curr_low, info=info, option_ltp=opt_ltp)

                    # Start 30s broadcast regardless — shows exec status live
                    if AUTO_ARMED:
                        start_signal_alerts(
                            find_option_symbol(price, signal) or signal,
                            opt_ltp or 0, signal, curr_low - CANDLE_SL_BUFFER
                        )
                        success = execute_entry(signal, price, curr_low)
                        if not success:
                            # Order failed — reset so next valid signal can retry
                            last_dir = None
                            alerts_today -= 1

            if price and info:
                send_market_status(price, info, alerts_today)

            check_spike(df5)   # spike detector runs inside window too, reuses fetched df5
            sleep_poll(1)      # 1s poll — max detection delay ~3s (fetch ~2s + sleep 1s)

        except KeyboardInterrupt:
            print("\n⛔ Morning scanner stopped.")
            send_telegram("⛔ Morning scanner stopped."); break
        except Exception as e:
            print(f"❌ Error: {e}"); sleep_poll(60)

if __name__ == "__main__":
    run_scanner()
