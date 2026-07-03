"""
=============================================================
NIFTY MORNING SCANNER — CE-only ATM, with AUTO EXECUTION
=============================================================
Scans the 9:30-13:00 window for high-quality CE (BUY) signals.
Strategy: VWAP + Supertrend + Breakout + Bull clean candle (4 conditions).
SL: prev candle low - 5pt (dynamic candle structure).
Target: 25pt fixed on spot price.
Backtest (60d): 92.3% win rate, 0 SL — best window confirmed (9:30-13:00).

SIGNAL-ONLY by default — starts disarmed. Send /start_auto on Telegram
to arm real order execution. Send /stop_auto to disarm. One position at a time.
GTT OCO set for SL+Target on every entry. Breakeven and trailing stop auto-managed.
Position persists across restarts via morning_position_state.json.
=============================================================
"""

from kiteconnect import KiteConnect
import pandas as pd
import numpy as np
import requests, time, webbrowser, os, json, sys, threading
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
# Separate bot token strongly recommended — see sensex_zerodha_bot.py for why
# (Telegram's getUpdates offset is global per bot token, not per-process; with
# 3 processes sharing one token, a command can be silently swallowed by the
# wrong one). Falls back to BOT_TOKEN if unset.
BOT_TOKEN  = os.getenv("MORNING_BOT_TOKEN", os.getenv("BOT_TOKEN"))
CHAT_ID    = os.getenv("CHAT_ID")
KITE_USER_ID    = os.getenv("KITE_USER_ID")
KITE_PASSWORD   = os.getenv("KITE_PASSWORD")
KITE_TOTP_SECRET = os.getenv("KITE_TOTP_SECRET")

# ─── CONFIG ───
NIFTY_TOKEN = 256265
BREAKEVEN_PCT  = 0.00034   # breakeven activation threshold (fixed %)
STRIKE_GAP     = 50

# Candle-structure SL + fixed target (backtest winner: 90.5% win, 0 SL in 60d)
CANDLE_SL_BUFFER = 5    # spot pts below prev candle low
TARGET_PTS       = 25   # fixed spot pts target

# This is the ONLY thing that makes this scanner different in scope from the
# main bot: a narrow morning window, and far fewer required conditions.
MORNING_START  = dtime(9, 30)
MORNING_END    = dtime(13, 0)
MAX_ALERTS     = 6
HEARTBEAT_MINS = 20

EXPIRY_WEEKDAY = 1  # Tuesday — no morning signals on expiry day

# Auto-execution parameters (same as main bot + backtest)
LOT_SIZE     = int(os.getenv("LOT_SIZE", "75"))
OPTION_DELTA = 0.5    # ATM delta approx — converts spot pts to premium pts
MOMENTUM_MIN = 5      # weak-exit threshold: if <5 pts after 15 min → exit
HARD_EXIT    = dtime(15, 10)

TRAIL_TRIGGER_MULT = 1.5
TRAIL_STEP_MULT    = 0.6

POSITION_FILE = "morning_position_state.json"

# ─── AUTO-TRADING STATE (in-memory — resets to OFF on every restart) ───
AUTO_ARMED = False
position   = None   # dict of open position or None

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

def run_remote_backtest(days):
    global backtest_running
    try:
        import nifty_morning_backtest as mbt
        mbt.kite.set_access_token(kite.access_token)
        send_telegram(f"⏳ Running {days}-day morning backtest... (20-40 seconds)")
        df5 = mbt.fetch_data(mbt.NIFTY_TOKEN, "5minute", days=days)
        if df5 is None or df5.empty:
            # Token may have expired — re-login and retry once
            print("  Backtest fetch failed — re-logging in and retrying...")
            if login():
                mbt.kite.set_access_token(kite.access_token)
                df5 = mbt.fetch_data(mbt.NIFTY_TOKEN, "5minute", days=days)
        if df5 is None or df5.empty:
            send_telegram("❌ Morning backtest failed — could not fetch data."); return
        trades = mbt.run_backtest(df5, days=days, mode="ATM", ce_only=True,
                                       candle_sl=True, target_pts=25)
        if not trades:
            send_telegram(f"📊 Morning backtest ({days}d): No signals found."); return
        tdf = pd.DataFrame(trades)
        total = len(tdf); win_outcomes = ['TARGET','TRAIL']
        wins  = len(tdf[tdf['outcome'].isin(win_outcomes)])
        loss  = len(tdf[tdf['outcome']=='SL'])
        bes   = len(tdf[tdf['outcome']=='BE'])
        weak  = len(tdf[tdf['outcome']=='WEAK'])
        wr    = wins/total*100; net = tdf['pnl_rs'].sum()
        days_w = len(set(tdf['date']))
        verdict = "✅ PROFITABLE" if wr>=75 and net>0 else ("⚡ MARGINAL" if net>0 else "❌ Needs work")
        bdf = tdf[tdf['signal']=='BUY']
        bwr = len(bdf[bdf['outcome'].isin(win_outcomes)])/len(bdf)*100 if len(bdf) else 0
        msg = (
            f"📊 <b>MORNING BACKTEST {days}d</b>  [CE-only, ATM]\n\n"
            f"Window: 09:30-13:00 | 4 conditions\n"
            f"VWAP + Supertrend + Breakout + Clean candle\n"
            f"Period: {tdf['date'].iloc[0]} → {tdf['date'].iloc[-1]}\n"
            f"Days with signals: {days_w}\n\n"
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


def run_remote_evening_backtest(days):
    global backtest_running
    try:
        import nifty_evening_backtest as ebt
        ebt.kite.set_access_token(kite.access_token)
        send_telegram(f"⏳ Running {days}-day evening backtest... (20-40 seconds)")
        df5  = ebt.fetch_data(ebt.NIFTY_TOKEN, "5minute",  days=days)
        if df5 is None or df5.empty:
            # Token may have expired — re-login and retry once
            print("  Evening backtest fetch failed — re-logging in and retrying...")
            if login():
                ebt.kite.set_access_token(kite.access_token)
                df5  = ebt.fetch_data(ebt.NIFTY_TOKEN, "5minute",  days=days)
        df15 = ebt.fetch_data(ebt.NIFTY_TOKEN, "15minute", days=days)
        if df5 is None or df5.empty:
            send_telegram("❌ Evening backtest failed — could not fetch data."); return
        trades = ebt.run_backtest(df5, df15, days=days)
        if not trades:
            send_telegram(f"📊 Evening backtest ({days}d): No signals found."); return
        tdf = pd.DataFrame(trades)
        total = len(tdf); win_outcomes = ['TARGET', 'TRAIL']
        wins  = len(tdf[tdf['outcome'].isin(win_outcomes)])
        loss  = len(tdf[tdf['outcome'] == 'SL'])
        bes   = len(tdf[tdf['outcome'] == 'BE'])
        weak  = len(tdf[tdf['outcome'] == 'WEAK'])
        wr    = wins / total * 100; net = tdf['pnl_rs'].sum()
        days_w = len(set(tdf['date']))
        sdf   = tdf[tdf['signal'] == 'SELL']
        swr   = len(sdf[sdf['outcome'].isin(win_outcomes)]) / len(sdf) * 100 if len(sdf) else 0
        verdict = "✅ PROFITABLE" if wr >= 55 and net > 0 else ("⚡ MARGINAL" if net > 0 else "❌ Needs work")
        msg = (
            f"📊 <b>EVENING BACKTEST {days}d</b> [TIGHT MODE]\n\n"
            f"Window: 13:00-14:30 | PE/SELL only\n"
            f"7 conditions: VWAP + ST + 5min EMA bearish + 15min bearish + Breakdown + Clean + RSI(38-52)\n"
            f"Period: {tdf['date'].iloc[0]} → {tdf['date'].iloc[-1]}\n"
            f"Days with signals: {days_w}\n\n"
            f"<b>Total Signals: {total}</b>\n"
            f"✅ Wins (Tgt+Trail): {wins} ({wr:.1f}%)\n"
            f"❌ SL: {loss} | ⚖️ BE: {bes} | ⚠️ Weak: {weak}\n\n"
            f"PE: {len(sdf)} trades, {swr:.0f}% win\n\n"
            f"💰 <b>Net P&L: ₹{net:,.0f}</b>\n\n{verdict}"
        )
        send_telegram(msg)
        csv_path = f"evening_backtest_{days}d.csv"
        tdf.to_csv(csv_path, index=False)
        with open(csv_path, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                data={"chat_id": CHAT_ID, "caption": f"📁 Evening backtest {days}d ({total} signals)"},
                files={"document": f}, timeout=30)
    except Exception as e:
        send_telegram(f"❌ Evening backtest error: {e}")
    finally:
        backtest_running = False


def process_telegram_commands():
    global backtest_running, AUTO_ARMED
    updates = get_telegram_updates()
    for u in updates:
        msg = u.get("message", {})
        text = msg.get("text", "").strip().lower()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != str(CHAT_ID):
            continue

        if text == "/start_auto":
            AUTO_ARMED = True
            print("🟢 MORNING AUTO-TRADING ARMED via Telegram")
            send_telegram(
                "✅ <b>MORNING AUTO-TRADING ARMED</b> 🟢\n\n"
                "Bot will now place REAL orders when a BUY CE signal fires.\n"
                f"Strategy: VWAP + Supertrend + Breakout + Clean candle\n"
                f"Lot size: {LOT_SIZE} | One position at a time.\n"
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
        elif text == "/status":
            armed_state = "🟢 ARMED — placing real orders" if AUTO_ARMED else "🔴 DISARMED — signal-only"
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
            send_telegram(
                f"📊 <b>Morning Scanner Status</b>\n\n"
                f"Auto-trade: {armed_state}\n"
                f"Window: {MORNING_START.strftime('%H:%M')}-{MORNING_END.strftime('%H:%M')}\n"
                f"Strategy: CE (BUY) only | 4 conditions | ATM\n"
                f"Backtest: 90.5% win rate, 0 SL/60d{pos_info}"
            )
        elif text == "/square_off":
            if position:
                send_telegram(f"🔴 Manual square-off requested for {position['symbol']}...")
                exit_position("MANUAL")
            else:
                send_telegram("ℹ️ No open morning position to square off.")

        elif text == "/morning_status":
            armed_state = "🟢 ARMED" if AUTO_ARMED else "🔴 DISARMED (signal-only)"
            send_telegram(
                "📊 <b>Morning Scanner Status</b>\n\n"
                f"Auto-trade: {armed_state}\n"
                f"🌅 Window: {MORNING_START.strftime('%H:%M')}-{MORNING_END.strftime('%H:%M')}\n"
                "Strategy: <b>CE (BUY) only — ATM, 4 conditions</b>\n"
                "  VWAP + Supertrend + Breakout + Clean candle\n\n"
                "Backtest (60d): 90.5% win rate, 0 SL\n"
                "Send /start_auto to arm | /stop_auto to disarm"
            )
        elif text == "/morning_help":
            send_telegram(
                "🤖 <b>Morning Scanner Commands</b>\n\n"
                "CE-only ATM strategy | 4 conditions | 90.5% win rate (60d backtest)\n\n"
                "/start_auto — arm real order execution\n"
                "/stop_auto — disarm (no new entries)\n"
                "/status — show armed state + open position\n"
                "/square_off — emergency close open position\n"
                "/morning_status — scanner status\n"
                "/backtest [days] — morning backtest (default 60)\n"
                "/backtest_evening [days] — evening backtest\n"
                "/restart — force fresh Kite login (fixes token errors)\n"
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
                parts = text.split(); days = 60
                if len(parts) > 1:
                    try: days = max(5, min(100, int(parts[1])))
                    except ValueError:
                        send_telegram("⚠️ Usage: /backtest 60  (5-100 days)"); continue
                backtest_running = True
                threading.Thread(target=run_remote_backtest, args=(days,), daemon=True).start()
        elif text == "/backtest_evening" or text.startswith("/backtest_evening "):
            if backtest_running:
                send_telegram("⏳ A backtest is already running — please wait.")
            else:
                parts = text.split(); days = 60
                if len(parts) > 1:
                    try: days = max(5, min(100, int(parts[1])))
                    except ValueError:
                        send_telegram("⚠️ Usage: /backtest_evening 60  (5-100 days)"); continue
                backtest_running = True
                threading.Thread(target=run_remote_evening_backtest, args=(days,), daemon=True).start()

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
def find_option_symbol(price, signal):
    """Find ATM CE tradingsymbol for the nearest weekly expiry."""
    try:
        atm = round(price / STRIKE_GAP) * STRIKE_GAP
        opt_type = "CE" if signal == "BUY" else "PE"
        instruments = kite.instruments("NFO")
        df = pd.DataFrame(instruments)
        opts = df[(df['name'] == 'NIFTY') & (df['instrument_type'] == opt_type) &
                  (df['strike'] == atm)].copy()
        if opts.empty: return None
        opts['expiry'] = pd.to_datetime(opts['expiry'])
        today = datetime.now().date()
        opts = opts[opts['expiry'].dt.date >= today].sort_values('expiry')
        if opts.empty: return None
        return opts.iloc[0]['tradingsymbol']
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
        print(f"⚠️ Margin check failed: {e}")
        return False, 0, 0

def place_entry_order(symbol, qty):
    try:
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR, exchange=kite.EXCHANGE_NFO,
            tradingsymbol=symbol, transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=qty, product=kite.PRODUCT_MIS, order_type=kite.ORDER_TYPE_MARKET
        )
        return order_id
    except Exception as e:
        print(f"❌ Entry order failed: {e}")
        send_telegram(f"❌ <b>ENTRY ORDER FAILED</b>\n{symbol}\n{e}")
        return None

def get_order_avg_price(order_id, timeout=15):
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
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR, exchange=kite.EXCHANGE_NFO,
            tradingsymbol=symbol, transaction_type=kite.TRANSACTION_TYPE_SELL,
            quantity=qty, product=kite.PRODUCT_MIS, order_type=kite.ORDER_TYPE_MARKET
        )
        return order_id
    except Exception as e:
        print(f"❌ Exit order failed: {e}")
        send_telegram(f"❌ <b>EXIT ORDER FAILED — CLOSE {symbol} MANUALLY NOW</b>\n{e}")
        return None

def place_gtt_oco(symbol, qty, sl_premium, target_premium, last_price):
    try:
        gtt = kite.place_gtt(
            trigger_type=kite.GTT_TYPE_OCO, tradingsymbol=symbol, exchange=kite.EXCHANGE_NFO,
            trigger_values=[sl_premium, target_premium], last_price=last_price,
            orders=[
                {"transaction_type": kite.TRANSACTION_TYPE_SELL, "quantity": qty,
                 "order_type": kite.ORDER_TYPE_LIMIT, "product": kite.PRODUCT_MIS, "price": sl_premium},
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
        kite.modify_gtt(
            trigger_id=gtt_id, trigger_type=kite.GTT_TYPE_OCO,
            tradingsymbol=symbol, exchange=kite.EXCHANGE_NFO,
            trigger_values=[new_sl, target_premium], last_price=last_price,
            orders=[
                {"transaction_type": kite.TRANSACTION_TYPE_SELL, "quantity": qty,
                 "order_type": kite.ORDER_TYPE_LIMIT, "product": kite.PRODUCT_MIS, "price": new_sl},
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
            return  # GTT not visible yet — don't clear (we cancel it ourselves on exit)
        if our_gtt['status'] == 'triggered':
            # Zerodha fired the GTT — SL or target was hit, position auto-closed
            send_telegram(
                f"✅ <b>POSITION CLOSED (GTT triggered)</b>\n\n"
                f"{position['symbol']}\nSL or Target hit — Zerodha closed the position.\n"
                f"Check Kite app for final P&L."
            )
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

def execute_entry(signal, price, prev_low):
    """Place a real BUY CE order. Called only when AUTO_ARMED and position is None."""
    global position
    symbol = find_option_symbol(price, signal)
    if not symbol:
        send_telegram(f"❌ Could not find ATM CE symbol — auto-entry skipped")
        return

    try:
        quote = kite.quote([f"NFO:{symbol}"])
        ltp = quote[f"NFO:{symbol}"]["last_price"]
    except Exception as e:
        send_telegram(f"❌ Could not fetch LTP for {symbol} — auto-entry skipped\n{e}")
        return

    has_funds, available, required = check_sufficient_funds(ltp, LOT_SIZE)
    if not has_funds:
        send_telegram(
            f"❌ <b>INSUFFICIENT FUNDS — TRADE SKIPPED</b>\n\n"
            f"{symbol}\nRequired: ₹{required:,.0f} (+5% buffer)\n"
            f"Available: ₹{available:,.0f}\n\n"
            f"Add funds or reduce LOT_SIZE in .env"
        )
        return

    send_telegram(f"🤖 <b>AUTO-EXECUTING MORNING ENTRY</b>\nBUY {symbol}\nQty: {LOT_SIZE} | LTP: {ltp}")

    order_id = place_entry_order(symbol, LOT_SIZE)
    if not order_id:
        return

    avg_price = get_order_avg_price(order_id)
    if avg_price is None:
        send_telegram(f"⚠️ Order {order_id} for {symbol} not confirmed — CHECK KITE MANUALLY")
        return

    # Candle-structure SL: few pts below previous candle low (dynamic)
    sl_spot       = prev_low - CANDLE_SL_BUFFER
    sl_spot_dist  = price - sl_spot          # spot pts at risk
    sl_premium     = round(avg_price - sl_spot_dist * OPTION_DELTA, 1)
    target_premium = round(avg_price + TARGET_PTS    * OPTION_DELTA, 1)

    gtt_id = place_gtt_oco(symbol, LOT_SIZE, sl_premium, target_premium, avg_price)

    position = {
        "symbol": symbol, "signal": signal, "qty": LOT_SIZE,
        "entry_spot": price, "entry_premium": avg_price,
        "entry_time": datetime.now(), "sl_premium": sl_premium,
        "target_premium": target_premium, "gtt_id": gtt_id,
        "breakeven_hit": False, "weak_checked": False, "order_id": order_id,
        "peak_favorable": 0.0, "trail_active": False
    }
    save_position_state()

    gtt_status = "Set ✅" if gtt_id else "FAILED ⚠️ manage manually!"
    send_telegram(
        f"✅ <b>MORNING POSITION OPEN</b>\n\n"
        f"{symbol}\nEntry premium: {avg_price}\n"
        f"SL: {sl_premium} (prev low {prev_low:.1f} - {CANDLE_SL_BUFFER}pt = {sl_spot:.1f}, ~{sl_spot_dist:.1f}pt risk)\n"
        f"Target: {target_premium} (+{TARGET_PTS}pt spot = +{TARGET_PTS*OPTION_DELTA:.1f}pt premium)\n"
        f"Breakeven: +{round(price * BREAKEVEN_PCT, 1)} pts\n"
        f"GTT OCO: {gtt_status}"
    )

def monitor_position(current_spot):
    """Breakeven, trailing stop, weak exit, hard exit — matches backtest logic exactly."""
    global position
    if position is None: return

    spot_move = (current_spot - position["entry_spot"]) if position["signal"] == "BUY" \
                else (position["entry_spot"] - current_spot)
    be_threshold = position["entry_spot"] * BREAKEVEN_PCT
    position["peak_favorable"] = max(position.get("peak_favorable", 0.0), spot_move)

    if not position["breakeven_hit"] and spot_move >= be_threshold:
        if position["gtt_id"]:
            ok = modify_gtt_sl(position["gtt_id"], position["symbol"], position["qty"],
                               position["entry_premium"], position["target_premium"],
                               position["entry_premium"])
            if ok:
                position["breakeven_hit"] = True
                save_position_state()
                send_telegram(f"⚖️ <b>BREAKEVEN ACTIVATED</b>\n{position['symbol']}\nSL moved to entry: {position['entry_premium']}")

    trail_trigger_dist = be_threshold * TRAIL_TRIGGER_MULT
    trail_step_dist    = be_threshold * TRAIL_STEP_MULT
    if position["peak_favorable"] >= trail_trigger_dist and position["gtt_id"]:
        new_sl_spot_dist = position["peak_favorable"] - trail_step_dist
        new_sl_premium = round(position["entry_premium"] + new_sl_spot_dist * OPTION_DELTA, 1)
        is_better = (new_sl_premium > position["sl_premium"]) if position["signal"] == "BUY" \
                    else (new_sl_premium < position["sl_premium"])
        if is_better:
            est_current_premium = round(position["entry_premium"] + spot_move * OPTION_DELTA, 1)
            ok = modify_gtt_sl(position["gtt_id"], position["symbol"], position["qty"],
                               new_sl_premium, position["target_premium"], est_current_premium)
            if ok:
                position["sl_premium"] = new_sl_premium
                position["breakeven_hit"] = True
                position["trail_active"] = True
                save_position_state()
                send_telegram(f"🔒 <b>TRAILING STOP</b>\n{position['symbol']}\nSL moved to {new_sl_premium} (locking profit)")

    elapsed_min = (datetime.now() - position["entry_time"]).total_seconds() / 60
    if elapsed_min >= 15 and not position["weak_checked"]:
        position["weak_checked"] = True
        if spot_move < MOMENTUM_MIN:
            send_telegram(f"⚠️ <b>WEAK MOMENTUM</b> — exiting {position['symbol']} (only {spot_move:.1f}pts after 15min)")
            exit_position("WEAK")
            return

    if datetime.now().time() >= HARD_EXIT:
        send_telegram(f"⏰ <b>HARD EXIT TIME (3:10 PM)</b> — closing {position['symbol']}")
        exit_position("EOD")

def exit_position(reason):
    """Cancel GTT + market exit. Called for WEAK/EOD/MANUAL exits."""
    global position
    if position is None: return
    if position["gtt_id"]:
        cancel_gtt(position["gtt_id"])
    order_id = place_exit_order(position["symbol"], position["qty"])
    send_telegram(f"🔚 <b>POSITION CLOSED ({reason})</b>\n{position['symbol']}\nExit order: {order_id}")
    position = None
    save_position_state()

# ─── SIGNAL ENGINE — CE-only ATM, 4 conditions ───
# Backtest winner (60 days): 21 trades, 90.5% win rate, 0 SL, Rs9,639 net
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

    # 4 conditions — VWAP + Supertrend + Breakout + Bull clean candle
    cond_vwap  = price > vwap
    cond_st    = st == True
    cond_brk   = price > ph
    cond_clean = bull_clean
    buy_ok = all([cond_vwap, cond_st, cond_brk, cond_clean])

    info = {
        "vwap": vwap, "st": st,
        "prev_low": pl,
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
        msg = (
            f"🌡️ <b>Scan Window — {now.strftime('%H:%M')}</b>\n\n"
            f"NIFTY: <b>{price:.1f}</b>   VWAP: {vwap:.1f}\n"
            f"CE-only scan (BUY signals only)\n\n"
            f"<b>Conditions ({score}/3):</b>\n"
            f"  {c_vwap} Price above VWAP\n"
            f"  {c_st} Supertrend bullish\n"
            f"  {c_brk} Breakout above prev candle\n\n"
            f"Alerts fired today: {alerts_today}/{MAX_ALERTS}\n"
            f"<i>Signal fires when all 3 + bull clean candle</i>"
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

def format_alert(signal, price, info, alert_num):
    now_str  = datetime.now().strftime("%d %b %Y %I:%M %p")
    strike   = get_strike(price, signal)
    prev_low = info.get("prev_low", price * 0.998) if info else price * 0.998
    sl_level = round(prev_low - CANDLE_SL_BUFFER, 2)
    sl_pts   = round(price - sl_level, 1)
    tgt      = round(price + TARGET_PTS, 2)
    be_pts   = round(price * BREAKEVEN_PCT, 1)

    mode_line = "🟢 AUTO-ARMED — order placed automatically" if AUTO_ARMED else "🔵 SIGNAL-ONLY — decide entry yourself"

    msg = f"""🌅 <b>BUY CE 📈</b>

📡 <b>MORNING WINDOW</b> ({MORNING_START.strftime('%H:%M')}-{MORNING_END.strftime('%H:%M')} only)
{mode_line}
📅 {now_str}
💹 BUY <b>{strike}</b>
📊 Nifty: {price:.2f}
🛑 SL: {sl_level} (prev low {prev_low:.1f} - {CANDLE_SL_BUFFER}pt = {sl_pts:.0f}pt risk)
🎯 Target: {tgt} (+{TARGET_PTS}pt)
⚖️ Breakeven: move SL to entry at +{be_pts:.0f} pts
🔢 Alert #{alert_num}/{MAX_ALERTS}

━━━━━━━━━━━━━━━━━━━━━━━━
<b>All 4 conditions ✅</b>
  ✅ Price above VWAP
  ✅ Supertrend bullish
  ✅ Breakout above prev candle
  ✅ Bull clean candle (no wick trap)

📅 {get_expiry_label()}"""
    return msg.strip()



# ─── MAIN ───
def run_scanner():
    print("="*65)
    print("  NIFTY MORNING SCANNER — CE ONLY + AUTO EXECUTION")
    print(f"  Window: {MORNING_START.strftime('%H:%M')}-{MORNING_END.strftime('%H:%M')}")
    print("  4 conditions: VWAP + Supertrend + Breakout + Clean candle")
    print(f"  SL: prev candle low - {CANDLE_SL_BUFFER}pt (dynamic) | Target: {TARGET_PTS}pt fixed")
    print("  CE (BUY) only — 90.5% win rate, 0 SL (60d backtest)")
    print("  Auto-trade: DISARMED (send /start_auto to arm)")
    print("="*65)

    if not login(): return

    load_position_state()  # recover any position from before a restart

    send_telegram(
        "🌅 <b>Morning Scanner Started</b>\n\n"
        f"Window: {MORNING_START.strftime('%H:%M')}-{MORNING_END.strftime('%H:%M')} only\n"
        "Strategy: <b>CE (BUY) only — ATM, 4 conditions</b>\n"
        "  VWAP + Supertrend + Breakout + Clean candle\n"
        f"  SL: prev candle low - {CANDLE_SL_BUFFER}pt | Target: {TARGET_PTS}pt\n\n"
        "Backtest (60d): <b>92.3% win rate, 0 SL</b> in 60d\n\n"
        "🔴 <b>Auto-trade: DISARMED</b> — signal-only mode\n"
        "Send /start_auto to enable real order placement\n"
        "Send /morning_help for all commands"
    )

    alerts_today = 0; last_date = None; last_dir = None; window_opened_today = False

    while True:
        try:
            process_telegram_commands()
            now = datetime.now(); ct = now.time(); cd = now.date()

            if last_date != cd:
                alerts_today = 0; last_date = cd; last_dir = None
                window_opened_today = False
                print(f"\n📅 New day: {cd}")
                if login():
                    print("🔐 Re-logged in for new day")
                else:
                    send_telegram("🆘 <b>MORNING SCANNER — NEW DAY LOGIN FAILED</b>")

            if now.weekday() == EXPIRY_WEEKDAY and position is None:
                print(f"⏳ [{now.strftime('%H:%M')}] Tuesday (expiry) — no morning signals")
                sleep_poll(300); continue

            # ─── POSITION MONITORING (runs all day regardless of window) ───
            if position is not None:
                check_gtt_triggered()   # detect if GTT (SL/target) fired externally
                if position is not None:
                    df5 = fetch_data(NIFTY_TOKEN, "5minute", days=2)
                    if df5 is not None and not df5.empty:
                        current_price = float(df5.iloc[-1]['Close'])
                        print(f"  [{now.strftime('%H:%M')}] Monitoring {position['symbol']} | Nifty: {current_price:.2f}")
                        monitor_position(current_price)
                sleep_poll(60); continue

            # ─── OUTSIDE SCAN WINDOW — idle ───
            if ct < MORNING_START or ct > MORNING_END:
                print(f"⏳ [{now.strftime('%H:%M')}] Outside scan window (9:30-13:00) — idle")
                sleep_poll(120); continue

            if not window_opened_today:
                window_opened_today = True
                armed_label = "🟢 AUTO-ARMED — will place real orders" if AUTO_ARMED else "🔴 Signal-only (send /start_auto to arm)"
                send_telegram(
                    f"🌅 <b>Scan Window OPEN</b>\n\n"
                    f"⏰ {now.strftime('%H:%M')} — Scanning till {MORNING_END.strftime('%H:%M')}\n"
                    "Strategy: <b>CE (BUY) only — ATM, 4 conditions</b>\n"
                    "  VWAP + Supertrend + Breakout + Clean candle\n"
                    f"  SL: prev low - {CANDLE_SL_BUFFER}pt | Target: {TARGET_PTS}pt\n"
                    f"Backtest: <b>92.3% win rate, 0 SL</b> in 60 days\n\n"
                    f"{armed_label}"
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
            elif signal in ("BUY", "SELL"):
                if signal == last_dir:
                    print(f"  ⏭️ Duplicate {signal} — skipping")
                else:
                    alerts_today += 1; last_dir = signal
                    last_heartbeat = now
                    print(f"  🌅 {signal} alert #{alerts_today} | AUTO_ARMED={AUTO_ARMED}")
                    send_telegram(format_alert(signal, price, info, alerts_today))

                    # Auto-execute if armed and no open position
                    if AUTO_ARMED and position is None:
                        prev_low = info.get("prev_low", price * 0.998) if info else price * 0.998
                        execute_entry(signal, price, prev_low)
                    elif AUTO_ARMED and position is not None:
                        send_telegram(f"ℹ️ Signal fired but position already open ({position['symbol']}) — skipping new entry")

            if price and info:
                send_market_status(price, info, alerts_today)

            sleep_poll(60)

        except KeyboardInterrupt:
            print("\n⛔ Morning scanner stopped.")
            send_telegram("⛔ Morning scanner stopped."); break
        except Exception as e:
            print(f"❌ Error: {e}"); sleep_poll(60)

if __name__ == "__main__":
    run_scanner()
