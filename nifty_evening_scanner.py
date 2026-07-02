"""
=============================================================
NIFTY EVENING SCANNER — PE/SELL ONLY, SIGNAL-ONLY
=============================================================
Separate tool from the main bot and morning scanner.
Evening window: 1:00 PM – 2:30 PM (13:00-14:30).

Observation: Afternoon market tends to FADE not break out upward.
Signal: VWAP + Supertrend + Breakdown + Bear candle + RSI guard.
PE/SELL only — CE disabled (afternoon breakouts rarely follow through).

Extra caution:
  Theta decay applies — 15% haircut at 1:00 PM, 25% at 2:00 PM.
  Tuesday (expiry): skipped — too volatile for afternoon entries.
  Friday: skipped — consistently negative in backtests.

This scanner NEVER places real orders. It sends Telegram alerts
labeled clearly as EVENING WINDOW so you know exactly where the
signal is coming from and can decide manually.
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

# ─── CREDENTIALS (from .env) ───
API_KEY          = os.getenv("API_KEY")
API_SECRET       = os.getenv("API_SECRET")
BOT_TOKEN        = os.getenv("EVENING_BOT_TOKEN", os.getenv("BOT_TOKEN"))
CHAT_ID          = os.getenv("CHAT_ID")
KITE_USER_ID     = os.getenv("KITE_USER_ID")
KITE_PASSWORD    = os.getenv("KITE_PASSWORD")
KITE_TOTP_SECRET = os.getenv("KITE_TOTP_SECRET")

# ─── CONFIG ───
NIFTY_TOKEN   = 256265
STRIKE_GAP    = 50

# Evening window
EVENING_START  = dtime(13, 0)
EVENING_END    = dtime(14, 30)
HARD_EXIT      = dtime(15, 10)

EXPIRY_WEEKDAY = 1   # Tuesday — skipped (expiry day risk)
FRIDAY         = 4   # Friday — skipped (consistently negative in backtests)

# SL / Target / Breakeven — tighter target vs morning (less runway near EOD)
SL_PCT        = 0.00063   # ~15 pts
TARGET_PCT    = 0.00046   # ~11 pts (morning is 0.00071 ~17 pts)
BREAKEVEN_PCT = 0.00025   # ~6 pts

# RSI guard — prevents entering on extreme RSI (only safe PE zone)
RSI_SELL_MIN = 38
RSI_SELL_MAX = 52

# Theta scaled haircut (shown in alert for awareness)
THETA_EARLY  = 0.15   # 15% haircut at 1:00 PM
THETA_LATE   = 0.25   # 25% haircut at 2:00 PM

MAX_ALERTS      = 4    # smaller window — less noise
HEARTBEAT_MINS  = 20   # status ping every 20 min inside the window

telegram_offset  = 0
backtest_running = False
last_heartbeat   = None

# ─── KITE ───
kite = KiteConnect(api_key=API_KEY)
TOKEN_FILE = "kite_token.json"   # shared with all NIFTY/SENSEX scripts

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
        if resp.json().get("status") != "success":
            print(f"❌ TOTP failed"); return False

        time.sleep(1)
        redirect_url = ""; next_url = kite.login_url()
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
                redirect_url = resp.url; break

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
        try: send_telegram(f"🆘 <b>EVENING SCANNER COULD NOT LOG IN</b>\n\n{msg}")
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

# ─── TELEGRAM ───
def send_telegram(msg):
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print(f"\n{'='*50}\n[TG]\n{msg}\n{'='*50}"); return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"TG error: {e}")

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

# ─── REMOTE BACKTEST (triggered via Telegram command) ───
def run_remote_backtest(days):
    global backtest_running
    try:
        import nifty_evening_backtest as ebt
        ebt.kite.set_access_token(kite.access_token)
        send_telegram(f"⏳ Running {days}-day evening backtest... (20-40 seconds)")

        df5 = ebt.fetch_data(ebt.NIFTY_TOKEN, "5minute", days=days)
        if df5 is None or df5.empty:
            send_telegram("❌ Evening backtest failed — could not fetch 5min data."); return

        df15 = ebt.fetch_data(ebt.NIFTY_TOKEN, "15minute", days=days)

        trades = ebt.run_backtest(df5, df15, days=days)
        if not trades:
            send_telegram(f"📊 Evening backtest ({days}d): No signals found."); return

        tdf = pd.DataFrame(trades)
        total = len(tdf)
        win_outcomes = ['TARGET', 'TRAIL']
        wins  = len(tdf[tdf['outcome'].isin(win_outcomes)])
        loss  = len(tdf[tdf['outcome'] == 'SL'])
        bes   = len(tdf[tdf['outcome'] == 'BE'])
        weak  = len(tdf[tdf['outcome'] == 'WEAK'])
        wr    = wins / total * 100
        net   = tdf['pnl_rs'].sum()
        days_w = len(set(tdf['date']))
        sdf    = tdf[tdf['signal'] == 'SELL']
        swr    = len(sdf[sdf['outcome'].isin(win_outcomes)]) / len(sdf) * 100 if len(sdf) else 0
        verdict = "✅ PROFITABLE" if wr >= 55 and net > 0 else ("⚡ MARGINAL" if net > 0 else "❌ Needs work")

        msg = (
            f"📊 <b>EVENING BACKTEST {days}d</b>\n\n"
            f"Window: 13:00-14:30 | PE/SELL only\n"
            f"Conditions: VWAP + ST + 5min EMA bearish + 15min bearish + Breakdown + Bear candle + RSI(38-52)\n"
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
    global backtest_running
    updates = get_telegram_updates()
    for u in updates:
        msg  = u.get("message", {})
        text = msg.get("text", "").strip().lower()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != str(CHAT_ID):
            continue

        if text == "/evening_status":
            now = datetime.now()
            theta = theta_haircut(now.time())
            theta_pct = round(theta * 100)
            send_telegram(
                "📊 <b>Evening Scanner Status</b>\n\n"
                "🌆 SIGNAL-ONLY — never places real orders\n"
                f"⏰ Active window: {EVENING_START.strftime('%H:%M')}-{EVENING_END.strftime('%H:%M')}\n"
                "📉 PE/SELL only [TIGHT MODE]\n"
                "7 conditions: VWAP + ST + 5min EMA bearish + 15min bearish + Breakdown + Bear candle + RSI(38-52)\n"
                f"⏳ Theta haircut now: ~{theta_pct}% (scales 15%→25% through window)\n"
                "Skip: Tuesday (expiry) + Friday"
            )
        elif text == "/evening_help":
            send_telegram(
                "🤖 <b>Evening Scanner Commands</b>\n\n"
                "🌆 PE/SELL signal-only — runs alongside main bot + morning scanner\n\n"
                "/evening_status — confirm it's running + theta level\n"
                "/backtest_evening [days] — run evening backtest (default 60, max 100)\n"
                "/evening_help — this message"
            )
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
                threading.Thread(target=run_remote_backtest, args=(days,), daemon=True).start()

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
        candles = kite.historical_data(token, datetime.now() - timedelta(days=days),
                                       datetime.now(), interval)
        if not candles: return None
        df = pd.DataFrame(candles)
        df.columns = ['date', 'Open', 'High', 'Low', 'Close', 'Volume']
        df.set_index('date', inplace=True)
        df.index = pd.to_datetime(df.index)
        return df.dropna()
    try:
        return _fetch()
    except Exception as e:
        err = str(e)
        if "access_token" in err or "api_key" in err or "Incorrect" in err:
            print(f"  ⚠️ Auth error — re-logging in automatically...")
            send_telegram("⚠️ <b>Evening Scanner: session expired</b> — re-logging in automatically...")
            if login():
                try:
                    return _fetch()
                except Exception as e2:
                    print(f"  Data error after relogin: {e2}"); return None
        print(f"  Data error: {e}"); return None

# ─── INDICATORS ───
def calculate_vwap(df):
    df = df.copy(); df['Date'] = df.index.date
    df['TP'] = (df['High'] + df['Low'] + df['Close']) / 3
    if df['Volume'].sum() > 0:
        df['TPV']    = df['TP'] * df['Volume']
        df['CumTPV'] = df.groupby('Date')['TPV'].cumsum()
        df['CumVol'] = df.groupby('Date')['Volume'].cumsum()
        return (df['CumTPV'] / df['CumVol']).fillna(df['Close'])
    return df.groupby('Date')['TP'].transform(lambda x: x.expanding().mean()).fillna(df['Close'])

def calculate_supertrend(df, period=10, multiplier=3.0):
    df = df.copy(); hl2 = (df['High'] + df['Low']) / 2
    df['TR'] = np.maximum(df['High'] - df['Low'],
               np.maximum(abs(df['High'] - df['Close'].shift(1)),
                          abs(df['Low'] - df['Close'].shift(1))))
    df['ATR'] = df['TR'].rolling(period).mean()
    upper = (hl2 + multiplier * df['ATR']).values.copy()
    lower = (hl2 - multiplier * df['ATR']).values.copy()
    trend = [True] * len(df); close = df['Close'].values
    for i in range(1, len(df)):
        lower[i] = max(lower[i], lower[i-1]) if close[i-1] > lower[i-1] else lower[i]
        upper[i] = min(upper[i], upper[i-1]) if close[i-1] < upper[i-1] else upper[i]
        if   not trend[i-1] and close[i] > upper[i]: trend[i] = True
        elif trend[i-1]     and close[i] < lower[i]: trend[i] = False
        else:                                         trend[i] = trend[i-1]
    return pd.Series(trend, index=df.index)

def calculate_rsi(s, p=14):
    d = s.diff()
    g = d.where(d > 0, 0).rolling(p).mean()
    l = (-d.where(d < 0, 0)).rolling(p).mean()
    return 100 - (100 / (1 + g / l))

def analyze_candle(o, h, l, c):
    body = abs(c - o); tr = h - l
    if tr == 0: return True, False, False
    uw = h - max(o, c); lw = min(o, c) - l
    doji = (body / tr) < 0.1
    return doji, (not doji and c > o and uw <= body), (not doji and c < o and lw <= body)

def get_strike(price):
    atm = round(price / STRIKE_GAP) * STRIKE_GAP
    return f"{atm} PE"

def theta_haircut(entry_time):
    """Scale haircut linearly: 15% at 1:00 PM → 25% at 2:00 PM+."""
    mins_past_1pm = (entry_time.hour - 13) * 60 + entry_time.minute
    frac = min(max(mins_past_1pm, 0) / 60.0, 1.0)
    return THETA_EARLY + frac * (THETA_LATE - THETA_EARLY)

def get_15min_trend(df15):
    """Return True if 15-min trend is up (EMA9 > EMA20), False if down, None if insufficient data."""
    if df15 is None or len(df15) < 25:
        return None
    df15 = df15.copy()
    df15['EMA9']  = df15['Close'].ewm(span=9,  adjust=False).mean()
    df15['EMA20'] = df15['Close'].ewm(span=20, adjust=False).mean()
    return bool(df15['EMA9'].iloc[-1] > df15['EMA20'].iloc[-1])

# ─── SIGNAL ENGINE — PE/SELL only, 7 conditions (tight mode) ───
def check_signals_evening(df5, df15=None):
    if len(df5) < 30:
        return None, None, None

    df5 = df5.copy()
    df5['VWAP']       = calculate_vwap(df5)
    df5['Supertrend'] = calculate_supertrend(df5)
    df5['RSI']        = calculate_rsi(df5['Close'])
    df5['EMA9']       = df5['Close'].ewm(span=9,  adjust=False).mean()
    df5['EMA20']      = df5['Close'].ewm(span=20, adjust=False).mean()
    df5 = df5.dropna(subset=['VWAP', 'RSI', 'EMA20'])
    if len(df5) < 2:
        return None, None, None

    curr = df5.iloc[-1]; prev = df5.iloc[-2]
    price = float(curr['Close'])
    o, h, l, c = float(curr['Open']), float(curr['High']), float(curr['Low']), float(curr['Close'])
    vwap = float(curr['VWAP'])
    st   = bool(curr['Supertrend'])
    rsi  = float(curr['RSI'])
    ema9  = float(curr['EMA9']); ema20 = float(curr['EMA20'])
    ph   = float(prev['High']); pl = float(prev['Low'])

    is_doji, bull_clean, bear_clean = analyze_candle(o, h, l, c)
    if is_doji:
        return "SKIP", price, None

    breakdown   = price < pl
    rsi_ok_sell = RSI_SELL_MIN <= rsi <= RSI_SELL_MAX
    ema_bearish = ema9 < ema20       # 5-min EMA confirms downtrend
    t15         = get_15min_trend(df15)   # must be bearish (False) to proceed
    t15_bearish = (t15 == False)          # None or True both block the trade

    sell_ok = all([price < vwap, st == False, ema_bearish, breakdown, bear_clean, rsi_ok_sell, t15_bearish])

    info = {
        "vwap":       vwap,
        "st":         st,
        "rsi":        rsi,
        "ema9":       ema9,
        "ema20":      ema20,
        "t15":        t15,
        "bear_clean": bear_clean,
        "cond_vwap":  price < vwap,
        "cond_st":    st == False,
        "cond_ema":   ema_bearish,
        "cond_brk":   breakdown,
        "cond_clean": bear_clean,
        "cond_rsi":   rsi_ok_sell,
        "cond_t15":   t15_bearish,
    }

    if sell_ok:
        return "SELL", price, info
    return None, price, info


def send_market_status(price, info, alerts_today):
    global last_heartbeat
    now = datetime.now()
    if last_heartbeat is not None and (now - last_heartbeat).total_seconds() < HEARTBEAT_MINS * 60:
        return
    last_heartbeat = now

    ck = lambda v: "✅" if v else "❌"
    theta = theta_haircut(now.time())
    theta_pct = round(theta * 100)

    if info and price:
        vwap  = info.get("vwap", 0)
        rsi   = info.get("rsi", 0)
        ema9  = info.get("ema9", 0)
        ema20 = info.get("ema20", 0)
        t15   = info.get("t15")
        t15_label = "Bearish ✅" if t15 == False else ("Bullish ❌" if t15 == True else "N/A ❌")
        score = sum(1 for k in ['cond_vwap', 'cond_st', 'cond_ema', 'cond_brk', 'cond_clean', 'cond_rsi', 'cond_t15']
                    if info.get(k))
        msg = (
            f"🌡️ <b>Evening Window — {now.strftime('%H:%M')}</b>\n\n"
            f"NIFTY: <b>{price:.1f}</b>   VWAP: {vwap:.1f}\n"
            f"⏳ Theta haircut now: ~{theta_pct}% (15%→25% across window)\n\n"
            f"<b>Signal conditions ({score}/7):</b>\n"
            f"  {ck(info.get('cond_vwap'))}  Price below VWAP\n"
            f"  {ck(info.get('cond_st'))}  Supertrend RED (downtrend)\n"
            f"  {ck(info.get('cond_ema'))}  5-min EMA9({ema9:.0f}) < EMA20({ema20:.0f})\n"
            f"  {ck(info.get('cond_brk'))}  Breakdown vs prev candle\n"
            f"  {ck(info.get('cond_clean'))}  Bear clean candle\n"
            f"  {ck(info.get('cond_rsi'))}  RSI: {rsi:.1f} (need {RSI_SELL_MIN}-{RSI_SELL_MAX})\n"
            f"  {ck(info.get('cond_t15'))}  15-min trend: {t15_label}\n\n"
            f"Alerts fired today: {alerts_today}/{MAX_ALERTS}\n"
            f"<i>PE signal fires when all 7 ✅</i>"
        )
    else:
        msg = (
            f"🌡️ <b>Evening Window — {now.strftime('%H:%M')}</b>\n"
            f"NIFTY: {price:.1f if price else 'N/A'}  |  Alerts: {alerts_today}/{MAX_ALERTS}"
        )
    send_telegram(msg)


def get_day_label():
    day = datetime.now().weekday()
    names = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 4: "Friday"}
    if day == EXPIRY_WEEKDAY:
        return f"{names.get(day)} Expiry ⚠️ — extra theta decay, scanner skips today"
    return names.get(day, "Weekend")

def format_alert(price, info, alert_num):
    now = datetime.now()
    now_str = now.strftime("%d %b %Y %I:%M %p")
    strike  = get_strike(price)

    sl_pts  = round(price * SL_PCT, 1)
    tgt_pts = round(price * TARGET_PCT, 1)
    be_pts  = round(price * BREAKEVEN_PCT, 1)
    sl      = round(price + sl_pts, 2)
    tgt     = round(price - tgt_pts, 2)
    be      = round(price - be_pts, 2)
    rsi     = info.get("rsi", 0)

    theta   = theta_haircut(now.time())
    theta_pct = round(theta * 100)
    if theta_pct <= 17:
        theta_note = "1:00-1:30 PM entry — more time remaining"
    elif theta_pct <= 21:
        theta_note = "1:30-2:00 PM — decay increasing"
    else:
        theta_note = "2:00 PM+ — decay accelerating, smaller position"

    msg = f"""🌆 <b>SELL PE 📉</b>

📡 <b>EVENING WINDOW</b> ({EVENING_START.strftime('%H:%M')}-{EVENING_END.strftime('%H:%M')} only)
🔴 SIGNAL-ONLY — decide entry yourself
📅 {now_str}
💹 SELL {strike}
📊 Nifty: {price:.2f}
🛑 SL: {sl} (+{sl_pts:.0f} pts)
🎯 Target: {tgt} (-{tgt_pts:.0f} pts)  [tighter — near EOD]
⚖️ Breakeven: move SL → entry at {be} (-{be_pts:.0f} pts)
⏳ Theta haircut: ~{theta_pct}% ({theta_note})
🔢 Alert #{alert_num}/{MAX_ALERTS}

━━━━━━━━━━━━━━━━━━━━━━━━
<b>All 7 conditions ✅ [TIGHT MODE]</b>
  ✅ Price below VWAP (bearish bias)
  ✅ Supertrend RED (downtrend)
  ✅ 5-min EMA9 &lt; EMA20 (short-term bearish)
  ✅ Breakdown vs prev candle
  ✅ Bear clean candle (body &gt; tail)
  ✅ RSI: {rsi:.1f} (range {RSI_SELL_MIN}-{RSI_SELL_MAX})
  ✅ 15-min trend bearish (EMA9 &lt; EMA20)

⚠️ PE ONLY — afternoon favours fades, not upside breakouts
📉 {get_day_label()}"""
    return msg.strip()


# ─── MAIN ───
def run_scanner():
    print("=" * 55)
    print("  NIFTY EVENING SCANNER — PE/SELL ONLY  [TIGHT MODE]")
    print(f"  Window: {EVENING_START.strftime('%H:%M')}-{EVENING_END.strftime('%H:%M')}")
    print("  7 conditions: VWAP + ST + 5min EMA bearish + 15min bearish + Breakdown + Clean candle + RSI(38-52)")
    print("  Skip: Tuesday (expiry) + Friday")
    print("  NEVER places real orders — separate from all other bots")
    print("=" * 55)

    if not login(): return

    send_telegram(
        "🌆 <b>Evening Scanner Started</b>\n\n"
        f"Window: {EVENING_START.strftime('%H:%M')}-{EVENING_END.strftime('%H:%M')} only\n"
        "🔴 PE/SELL only [TIGHT MODE]\n"
        "SIGNAL-ONLY — separate from main bot + morning scanner, never trades\n"
        "7 conditions: VWAP + ST + 5min EMA bearish + 15min bearish + Breakdown + Clean candle + RSI(38-52)\n"
        "⏳ Theta haircut: 15% at 1 PM → 25% at 2 PM (shown in each alert)\n"
        "Skip days: Tuesday (expiry) + Friday\n\n"
        "Send /evening_help for commands"
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
                    send_telegram("🆘 <b>EVENING SCANNER — NEW DAY LOGIN FAILED</b>")

            if ct < EVENING_START or ct > EVENING_END:
                print(f"⏳ [{now.strftime('%H:%M')}] Outside evening window — idle")
                sleep_poll(120); continue

            if now.weekday() == EXPIRY_WEEKDAY:
                print(f"⏳ [{now.strftime('%H:%M')}] Tuesday (expiry) — no evening signals")
                sleep_poll(300); continue

            if now.weekday() == FRIDAY:
                print(f"⏳ [{now.strftime('%H:%M')}] Friday — no evening signals")
                sleep_poll(300); continue

            if not window_opened_today:
                window_opened_today = True
                theta = theta_haircut(now.time())
                send_telegram(
                    f"🌆 <b>Evening Window OPEN</b>\n\n"
                    f"⏰ {now.strftime('%H:%M')} — Scanning till {EVENING_END.strftime('%H:%M')}\n"
                    f"📉 PE/SELL only [TIGHT] | 7 conditions: VWAP + ST + 5min EMA bearish + 15min bearish + Breakdown + Clean + RSI(38-52)\n"
                    f"⏳ Theta haircut: ~{round(theta*100)}% (rises to 25% by 2 PM)"
                )

            if alerts_today >= MAX_ALERTS:
                print(f"🚫 Max evening alerts reached ({MAX_ALERTS})")
                sleep_poll(120); continue

            df5  = fetch_data(NIFTY_TOKEN, "5minute",  days=5)
            df15 = fetch_data(NIFTY_TOKEN, "15minute", days=2)
            if df5 is None:
                print("❌ Data failed"); sleep_poll(60); continue

            signal, price, info = check_signals_evening(df5, df15)
            if price:
                print(f"  [{now.strftime('%H:%M')}] Nifty:{price:.2f} | Signal:{signal or 'None'}")

            if signal == "SKIP":
                print("  ⛔ Doji — skipping")
            elif signal == "SELL":
                if last_dir == "SELL":
                    print("  ⏭️ Duplicate SELL — skipping")
                else:
                    alerts_today += 1; last_dir = "SELL"
                    last_heartbeat = now
                    print(f"  🌆 SELL (PE) alert #{alerts_today}")
                    send_telegram(format_alert(price, info, alerts_today))

            if price and info:
                send_market_status(price, info, alerts_today)

            sleep_poll(60)

        except KeyboardInterrupt:
            print("\n⛔ Evening scanner stopped.")
            send_telegram("⛔ Evening scanner stopped."); break
        except Exception as e:
            print(f"❌ Error: {e}"); sleep_poll(60)

if __name__ == "__main__":
    run_scanner()
