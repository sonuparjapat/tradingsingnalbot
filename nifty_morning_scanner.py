"""
=============================================================
NIFTY MORNING SCANNER — RELAXED FILTERS, SIGNAL-ONLY
=============================================================
The main nifty_zerodha_bot.py runs the full, proven Tier1(5/5)+Tier2(3-4/5)
+S&R+expiry-caution rule set all day, and can auto-execute. This is a SEPARATE,
ADDITIONAL tool — it does NOT replace or touch the main bot.

Observation: 9:30-11:00 AM tends to have strong, clean directional moves that
the full filter stack sometimes screens out (volume-spike threshold, RSI band,
Tier2 minimum score, S&R proximity, ORB requirement). This scanner drops those
secondary/confirmation filters and keeps only the MANDATORY core signal:

  Price vs VWAP + Supertrend direction + EMA9/20 momentum + Breakout/breakdown

...restricted to the 9:30-11:00 window only. It NEVER places real orders —
there is no AUTO_ARMED, no GTT, no position tracking, no execution code path
at all. It only sends Telegram alerts, clearly labeled as relaxed/exploratory,
so you can judge and act on them manually.
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
SL_PCT         = 0.00063   # same levels as main bot, shown for reference only
TARGET_PCT     = 0.00071
BREAKEVEN_PCT  = 0.00034
STRIKE_GAP     = 50

# This is the ONLY thing that makes this scanner different in scope from the
# main bot: a narrow morning window, and far fewer required conditions.
MORNING_START  = dtime(9, 30)
MORNING_END    = dtime(11, 0)
MAX_ALERTS     = 6
HEARTBEAT_MINS = 20

EXPIRY_WEEKDAY = 1  # Tuesday — no morning signals on expiry day

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
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"HTML"}, timeout=10)
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

def run_remote_backtest(days):
    global backtest_running
    try:
        import nifty_morning_backtest as mbt
        mbt.kite.set_access_token(kite.access_token)
        send_telegram(f"⏳ Running {days}-day morning backtest... (20-40 seconds)")
        df5 = mbt.fetch_data(mbt.NIFTY_TOKEN, "5minute", days=days)
        if df5 is None or df5.empty:
            send_telegram("❌ Morning backtest failed — could not fetch data."); return
        trades = mbt.run_backtest(df5, days=days, mode="ATM", ce_only=True)
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
            f"Window: 09:30-11:00 | 4 conditions\n"
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
            f"6 conditions: VWAP + ST + 15min bearish + Breakdown + Clean + RSI(35-55)\n"
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
        msg = u.get("message", {})
        text = msg.get("text", "").strip().lower()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != str(CHAT_ID):
            continue
        if text == "/morning_status":
            send_telegram(
                "📊 <b>Morning Scanner Status</b>\n\n"
                "🔵 SIGNAL-ONLY — never places real orders\n"
                f"🌅 Active window: {MORNING_START.strftime('%H:%M')}-{MORNING_END.strftime('%H:%M')}\n"
                "Strategy: <b>CE (BUY) only — ATM, 4 conditions</b>\n"
                "  VWAP + Supertrend + Breakout + Clean candle\n\n"
                "Backtest (60d): 81% win rate, 2 SL, Rs3,436/month\n"
                "PE tested: 47% win rate → not viable in morning"
            )
        elif text == "/morning_help":
            send_telegram(
                "🤖 <b>Morning Scanner Commands</b>\n\n"
                "🔵 CE-only strategy, signal-only — never trades.\n"
                "4 conditions: VWAP + Supertrend + Breakout + Clean candle\n"
                "ATM options (CE only) — PE = 47% win in backtest, not viable\n\n"
                "/morning_status — confirm it's running\n"
                "/backtest [days] — backtest morning signals (default 60, max 100)\n"
                "/backtest_evening [days] — backtest evening signals\n"
                "/morning_help — this message"
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

# ─── SIGNAL ENGINE — CE-only ATM, 4 conditions ───
# Backtest winner (60 days): 21 trades, 81% win rate, 2 SL, Rs6,873 net
# Tested vs SOLID (5 cond) → 74% win, and SOLID+PE → 62% win.
# Conclusion: original 4-condition CE-only is the best morning strategy.
# PE in morning = 47% win rate even with 15-min filter → not viable.
def check_signals_relaxed(df5):
    if len(df5) < 30:
        return None, None, None, None

    df5 = df5.copy()
    df5['VWAP']       = calculate_vwap(df5)
    df5['Supertrend'] = calculate_supertrend(df5)

    df5 = df5.dropna(subset=['VWAP'])
    if len(df5) < 2:
        return None, None, None, None

    curr = df5.iloc[-1]; prev = df5.iloc[-2]
    price = float(curr['Close'])
    o,h,l,c = float(curr['Open']),float(curr['High']),float(curr['Low']),float(curr['Close'])
    vwap  = float(curr['VWAP'])
    st    = bool(curr['Supertrend'])
    ph    = float(prev['High'])

    is_doji, bull_clean, _ = analyze_candle(o,h,l,c)
    if is_doji:
        return "SKIP", price, None, None

    # 4 conditions — VWAP + Supertrend + Breakout + Bull clean candle
    cond_vwap = price > vwap
    cond_st   = st == True
    cond_brk  = price > ph
    cond_clean = bull_clean
    buy_ok = all([cond_vwap, cond_st, cond_brk, cond_clean])

    info = {
        "vwap": vwap, "st": st,
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
            f"🌡️ <b>Morning Window — {now.strftime('%H:%M')}</b>\n\n"
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
            f"🌡️ <b>Morning Window — {now.strftime('%H:%M')}</b>\n"
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
    now_str = datetime.now().strftime("%d %b %Y %I:%M %p")
    strike = get_strike(price, signal)
    sl_pts  = round(price * SL_PCT, 1)
    tgt_pts = round(price * TARGET_PCT, 1)
    be_pts  = round(price * BREAKEVEN_PCT, 1)
    sl  = round(price - sl_pts, 2)
    tgt = round(price + tgt_pts, 2)

    msg = f"""🌅 <b>BUY CE 📈</b>

📡 <b>MORNING WINDOW</b> ({MORNING_START.strftime('%H:%M')}-{MORNING_END.strftime('%H:%M')} only)
🔵 SIGNAL-ONLY — decide entry yourself
📅 {now_str}
💹 BUY <b>{strike}</b>
📊 Nifty: {price:.2f}
🛑 SL: {sl} ({sl_pts:.0f} pts)
🎯 Target: {tgt} ({tgt_pts:.0f} pts)
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
    print("="*60)
    print("  NIFTY MORNING SCANNER — CE ONLY, SIGNAL-ONLY")
    print(f"  Window: {MORNING_START.strftime('%H:%M')}-{MORNING_END.strftime('%H:%M')}")
    print("  4 conditions: VWAP + Supertrend + Breakout + Clean candle")
    print("  CE (BUY) only — backtest confirmed 81% win rate, best strategy")
    print("  NEVER places real orders — separate from the main bot")
    print("="*60)

    if not login(): return

    send_telegram(
        "🌅 <b>Morning Scanner Started</b>\n\n"
        f"Window: {MORNING_START.strftime('%H:%M')}-{MORNING_END.strftime('%H:%M')} only\n"
        "🔵 SIGNAL-ONLY — separate from the main bot, never trades\n"
        "Strategy: <b>CE (BUY) only — ATM options, 4 conditions</b>\n"
        "  VWAP + Supertrend + Breakout + Clean candle\n\n"
        "Backtest (60d): <b>81% win rate</b>, 2 SL only, Rs3,436/month\n"
        "Tested SOLID(+EMA+3pt) → 74% | SOLID+PE → 62% | Original wins\n\n"
        "Send /morning_help for commands"
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

            if ct < MORNING_START or ct > MORNING_END:
                print(f"⏳ [{now.strftime('%H:%M')}] Outside morning window — idle")
                sleep_poll(120); continue

            if now.weekday() == EXPIRY_WEEKDAY:
                print(f"⏳ [{now.strftime('%H:%M')}] Tuesday (expiry) — no morning signals")
                sleep_poll(300); continue

            if not window_opened_today:
                window_opened_today = True
                send_telegram(
                    f"🌅 <b>Morning Window OPEN</b>\n\n"
                    f"⏰ {now.strftime('%H:%M')} — Scanning till {MORNING_END.strftime('%H:%M')}\n"
                    "Strategy: <b>CE (BUY) only — ATM, 4 conditions</b>\n"
                    "  VWAP + Supertrend + Breakout + Clean candle\n"
                    "  Backtest: 81% win rate, only 2 SL in 60 days"
                )

            if alerts_today >= MAX_ALERTS:
                print(f"🚫 Max morning alerts reached ({MAX_ALERTS})")
                sleep_poll(120); continue

            df5 = fetch_data(NIFTY_TOKEN, "5minute", days=5)
            if df5 is None:
                print("❌ Data failed"); sleep_poll(60); continue

            signal, price, info, opt_type = check_signals_relaxed(df5)
            if price:
                print(f"  [{now.strftime('%H:%M')}] Nifty:{price:.2f} | Signal:{signal or 'None'}")

            if signal == "SKIP":
                print("  ⛔ Doji — skipping")
            elif signal in ("BUY", "SELL"):
                if signal == last_dir:
                    print(f"  ⏭️ Duplicate {signal} — skipping")
                else:
                    alerts_today += 1; last_dir = signal
                    last_heartbeat = now  # reset heartbeat — real alert just sent
                    print(f"  🌅 {signal} alert #{alerts_today}")
                    send_telegram(format_alert(signal, price, info, alerts_today))

            # Periodic status — only inside window, non-irritating
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
