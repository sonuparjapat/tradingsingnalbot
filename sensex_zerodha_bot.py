"""
=============================================================
SENSEX SIGNAL BOT — SIGNAL-ONLY, NO AUTO-EXECUTION
=============================================================
Same engine as nifty_zerodha_bot.py, adapted for SENSEX:
- Exchange: BSE (spot) / BFO (futures) instead of NSE/NFO
- Expiry day: Thursday instead of Tuesday (EXPIRY_WEEKDAY=3)
- Strike gap: 100 instead of 50
- SENSEX instrument token looked up dynamically at startup

UNLIKE the NIFTY bot, this bot NEVER places real orders. It only
sends Telegram alerts with full entry/SL/target details — you decide
whether to enter manually. There is no /start_auto, no GTT, no
position tracking, no funds check. Signal logic is otherwise
byte-for-byte the same as sensex_zerodha_backtest.py.
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

# ─── CREDENTIALS (from .env) — same Kite account as NIFTY bot ───
API_KEY    = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
# Separate bot token strongly recommended: Telegram's getUpdates offset is a
# global per-bot-token cursor, not per-process. If NIFTY/SENSEX/morning-scanner
# all poll the SAME bot token, a command meant for one can be silently consumed
# by another's poll call and never arrive. Falls back to BOT_TOKEN if unset,
# but then commands race across all 3 processes — fine for low-stakes signal-only
# bots, just be aware a command can occasionally go to the wrong process.
BOT_TOKEN  = os.getenv("SENSEX_BOT_TOKEN", os.getenv("BOT_TOKEN"))
CHAT_ID    = os.getenv("CHAT_ID")
KITE_USER_ID    = os.getenv("KITE_USER_ID")
KITE_PASSWORD   = os.getenv("KITE_PASSWORD")
KITE_TOTP_SECRET = os.getenv("KITE_TOTP_SECRET")
LOT_SIZE = int(os.getenv("SENSEX_LOT_SIZE", os.getenv("LOT_SIZE", "20")))

# ─── CONFIG — must match sensex_zerodha_backtest.py exactly ───
SL_PCT         = 0.00063
TARGET_PCT     = 0.00071
BREAKEVEN_PCT  = 0.00034
MOMENTUM_MIN   = 5
MAX_TRADES     = 3
STRIKE_GAP     = 100
VIX_LIMIT      = 20

telegram_offset = 0
backtest_running = False

RSI_BUY_MIN = 48; RSI_BUY_MAX = 63
RSI_SELL_MIN = 37; RSI_SELL_MAX = 53
CE_MIN_T2 = 3; PE_MIN_T2 = 4
VOL_MULTI = 1.2

ORB_START    = dtime(9, 15)
MARKET_START = dtime(9, 30)
MARKET_END   = dtime(14, 0)
HARD_EXIT    = dtime(15, 10)

# ── EXPIRY DAY CAUTION — must match sensex_zerodha_backtest.py exactly ──
EXPIRY_WEEKDAY      = 3            # Thursday for SENSEX (NIFTY uses 1=Tuesday)
EXPIRY_VOL_MULTI    = 1.6
EXPIRY_CE_MIN_T2    = 4
EXPIRY_MARKET_END   = dtime(13, 0)
EXPIRY_MOMENTUM_MIN_MINUTES = 10
EXPIRY_BREAKOUT_BUFFER = 3

# ─── KITE ───
kite = KiteConnect(api_key=API_KEY)
TOKEN_FILE = "kite_token.json"  # shared with NIFTY scripts — same session, same day

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
               "Fix KITE_USER_ID / KITE_PASSWORD / KITE_TOTP_SECRET in .env and restart the bot.")
        print(msg)
        try: send_telegram(f"🆘 <b>SENSEX BOT COULD NOT LOG IN</b>\n\n{msg}")
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

def send_telegram_document(file_path, caption=""):
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print(f"[TG] Would send document: {file_path}"); return
    try:
        with open(file_path, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                files={"document": f}, timeout=30)
    except Exception as e:
        print(f"TG document send error: {e}")
        send_telegram(f"⚠️ Backtest finished but couldn't send the CSV file: {e}")

# ─── TELEGRAM COMMAND POLLING (signal-only: just /status, /backtest, /help) ───
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
    """Run the SENSEX backtest for N days and send a condensed report via Telegram."""
    global backtest_running
    try:
        import sensex_zerodha_backtest as bt
        bt.kite.set_access_token(kite.access_token)

        send_telegram(f"⏳ Running SENSEX {days}-day backtest... this can take 20-60 seconds.")

        sensex_token = bt.find_sensex_token()
        if not sensex_token:
            send_telegram("❌ Backtest failed — could not find SENSEX token.")
            return

        df5 = bt.fetch_data(sensex_token, "5minute", days=days, label="[5min spot]")
        if df5 is None or df5.empty:
            send_telegram("❌ Backtest failed — could not fetch price data.")
            return
        df15 = bt.fetch_data(sensex_token, "15minute", days=days, label="[15min spot]")
        fut_token = bt.find_sensex_fut_token()
        fut_vol = bt.fetch_data(fut_token, "5minute", days=days, label="[5min futures]") if fut_token else None

        trades = bt.run_backtest(df5, df15, fut_vol)
        if not trades:
            send_telegram(f"📊 <b>SENSEX {days}-day backtest</b>: No trades found in this period.")
            return

        tdf = pd.DataFrame(trades)
        total = len(tdf)
        win_outcomes = ['TARGET','TRAIL']
        wins  = len(tdf[tdf['outcome'].isin(win_outcomes)])
        loss  = len(tdf[tdf['outcome']=='SL'])
        bes   = len(tdf[tdf['outcome']=='BE'])
        weak  = len(tdf[tdf['outcome']=='WEAK'])
        wr    = wins/total*100
        net   = tdf['pnl_rs'].sum()
        days_traded = len(set(tdf['date']))

        bdf = tdf[tdf['signal']=='BUY']; sdf = tdf[tdf['signal']=='SELL']
        bwr = len(bdf[bdf['outcome'].isin(win_outcomes)])/len(bdf)*100 if len(bdf) else 0
        swr = len(sdf[sdf['outcome'].isin(win_outcomes)])/len(sdf)*100 if len(sdf) else 0

        if wr >= 55 and net > 0: verdict = "✅ PROFITABLE"
        elif net > 0: verdict = "⚡ MARGINAL"
        else: verdict = "❌ Needs work"

        msg = (
            f"📊 <b>SENSEX {days}-DAY BACKTEST RESULT</b>\n\n"
            f"Period: {df5.index[0].date()} → {df5.index[-1].date()}\n"
            f"Days with signals: {days_traded}\n\n"
            f"<b>Total Trades: {total}</b>\n"
            f"✅ Wins (Target+Trail): {wins} ({wr:.1f}%)\n"
            f"❌ SL: {loss}\n"
            f"⚖️ Breakeven: {bes}\n"
            f"⚠️ Weak exit: {weak}\n\n"
            f"CE (BUY): {len(bdf)} trades, {bwr:.0f}% win\n"
            f"PE (SELL): {len(sdf)} trades, {swr:.0f}% win\n\n"
            f"💰 <b>Net P&L: ₹{net:,.0f}</b>\n\n"
            f"{verdict}"
        )
        send_telegram(msg)

        csv_path = f"backtest_telegram_sensex_{days}d.csv"
        tdf.to_csv(csv_path, index=False)
        send_telegram_document(csv_path, caption=f"📁 Full SENSEX {days}-day trade log ({total} trades)")
    except Exception as e:
        send_telegram(f"❌ Backtest error: {e}")
    finally:
        backtest_running = False

def process_telegram_commands():
    """Signal-only bot: /status /backtest /help. No /start_auto — this bot never trades."""
    global backtest_running
    updates = get_telegram_updates()
    for u in updates:
        msg = u.get("message", {})
        text = msg.get("text", "").strip().lower()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != str(CHAT_ID):
            continue

        if text == "/status":
            send_telegram(
                "📊 <b>SENSEX Bot Status</b>\n\n"
                "🔵 Mode: SIGNAL-ONLY — this bot never places real orders\n"
                "Watching Sensex 5 min chart for signals.\n\n"
                "Send /help for all commands."
            )
        elif text == "/backtest" or text.startswith("/backtest "):
            if backtest_running:
                send_telegram("⏳ A backtest is already running — please wait for it to finish.")
            else:
                parts = text.split()
                days = 60
                if len(parts) > 1:
                    try:
                        days = max(5, min(100, int(parts[1])))
                    except ValueError:
                        send_telegram("⚠️ Usage: /backtest 60  (days must be a number, 5-100)")
                        continue
                backtest_running = True
                threading.Thread(target=run_remote_backtest, args=(days,), daemon=True).start()
        elif text == "/help":
            send_telegram(
                "🤖 <b>SENSEX Bot Commands</b>\n\n"
                "🔵 This bot is SIGNAL-ONLY — it never places real orders.\n\n"
                "/status — confirm bot is running\n"
                "/backtest [days] — run backtest, e.g. /backtest 60 (default 60, max 100)\n"
                "/help — this message"
            )

def sleep_poll(seconds):
    elapsed = 0
    while elapsed < seconds:
        chunk = min(5, seconds - elapsed)
        time.sleep(chunk)
        elapsed += chunk
        process_telegram_commands()

# ─── DATA ───
def fetch_data(token, interval="5minute", days=2):
    try:
        candles = kite.historical_data(token, datetime.now()-timedelta(days=days),
                                       datetime.now(), interval)
        if not candles: return None
        df = pd.DataFrame(candles)
        df.columns = ['date','Open','High','Low','Close','Volume']
        df.set_index('date', inplace=True)
        df.index = pd.to_datetime(df.index)
        return df.dropna()
    except Exception as e:
        print(f"  Data error: {e}"); return None

def find_sensex_token():
    """Find SENSEX spot index instrument token dynamically (BSE)."""
    try:
        instruments = kite.instruments("BSE")
        df = pd.DataFrame(instruments)
        idx = df[(df['tradingsymbol'] == 'SENSEX')]
        if idx.empty:
            print("❌ Could not find SENSEX in BSE instruments")
            return None
        token = int(idx.iloc[0]['instrument_token'])
        print(f"  SENSEX token: {token}")
        return token
    except Exception as e:
        print(f"  SENSEX token lookup error: {e}")
        return None

def find_sensex_fut_token():
    """SENSEX futures live on BFO (BSE F&O), not NFO."""
    try:
        instruments = kite.instruments("BFO")
        df = pd.DataFrame(instruments)
        nf = df[(df['name']=='SENSEX') & (df['instrument_type']=='FUT')].copy()
        if nf.empty: return None, None
        nf['expiry'] = pd.to_datetime(nf['expiry'])
        future = nf[nf['expiry'].dt.date >= datetime.now().date()].sort_values('expiry')
        if future.empty: return None, None
        return int(future.iloc[0]['instrument_token']), future.iloc[0]['tradingsymbol']
    except:
        return None, None

def get_vix():
    try: return kite.quote(["NSE:INDIA VIX"])["NSE:INDIA VIX"]["last_price"]
    except: return None

# ─── INDICATORS (same as backtest) ───
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

def get_orb(df):
    today = datetime.now().date()
    td = df[df.index.date == today]
    if td.empty: return None, None
    orb = td[(td.index.time >= dtime(9,15)) & (td.index.time < dtime(9,30))]
    if orb.empty: return None, None
    return float(orb['High'].max()), float(orb['Low'].min())

EXPIRY_DAY_NAMES = {0:"Monday", 1:"Tuesday", 2:"Wednesday", 3:"Thursday", 4:"Friday"}

def get_expiry_info():
    day = datetime.now().weekday()
    name = EXPIRY_DAY_NAMES.get(day, "Weekend")
    if day == EXPIRY_WEEKDAY:
        return False, f"{name} Expiry ⛔ — fake breakouts + theta risk"
    return True, f"{name} 🟢"

def get_prev_day_hl(df):
    today = datetime.now().date()
    prev = df[df.index.date < today]
    if prev.empty: return None, None
    last_date = prev.index.date[-1]
    day_data = prev[prev.index.date == last_date]
    return float(day_data['High'].max()), float(day_data['Low'].min())

def get_strike(price, signal):
    atm = round(price / STRIKE_GAP) * STRIKE_GAP
    if signal == "BUY": return f"{atm} CE"
    else: return f"{atm} PE"

# ─── SIGNAL ENGINE (same logic as sensex_zerodha_backtest.py) ───
def check_signals(df5, df15, fut_vol):
    if len(df5) < 30:
        return None, None, {}, {}, None, None, None

    df5 = df5.copy()
    df5['EMA9']  = ema(df5['Close'], 9)
    df5['EMA20'] = ema(df5['Close'], 20)
    df5['VWAP']  = calculate_vwap(df5)
    df5['Supertrend'] = calculate_supertrend(df5)
    df5['RSI']   = calculate_rsi(df5['Close'])

    if fut_vol is not None and fut_vol['Volume'].sum() > 0:
        va = fut_vol['Volume'].reindex(df5.index, method='nearest', tolerance='5min')
        df5['Vol'] = va.fillna(df5['Volume'])
    else:
        df5['Vol'] = df5['Volume']
    df5['AvgVol'] = df5['Vol'].rolling(20).mean()

    trend15 = None
    if df15 is not None and len(df15) >= 25:
        e9  = float(ema(df15['Close'], 9).iloc[-1])
        e20 = float(ema(df15['Close'], 20).iloc[-1])
        trend15 = e9 > e20

    df5 = df5.dropna(subset=['EMA9','EMA20','VWAP','AvgVol'])
    if len(df5) < 2:
        return None, None, {}, {}, None, None, None

    curr = df5.iloc[-1]; prev = df5.iloc[-2]
    price = float(curr['Close'])
    o,h,l,c = float(curr['Open']),float(curr['High']),float(curr['Low']),float(curr['Close'])
    vwap  = float(curr['VWAP'])
    ema9  = float(curr['EMA9']); ema20 = float(curr['EMA20'])
    pe9   = float(prev['EMA9']); pe20  = float(prev['EMA20'])
    st    = bool(curr['Supertrend'])
    vol   = float(curr['Vol']); avg_vol = float(curr['AvgVol'])
    ph    = float(prev['High']); pl = float(prev['Low'])
    rsi   = float(curr['RSI'])

    is_doji, bull_clean, bear_clean = analyze_candle(o,h,l,c)
    expiry_safe, expiry_label = get_expiry_info()
    is_expiry_day = not expiry_safe
    day_vol_multi = EXPIRY_VOL_MULTI if is_expiry_day else VOL_MULTI
    day_ce_min_t2 = EXPIRY_CE_MIN_T2 if is_expiry_day else CE_MIN_T2
    day_breakout_buffer = EXPIRY_BREAKOUT_BUFFER if is_expiry_day else 0

    orb_high, orb_low = get_orb(df5)
    orb_bull = (orb_high is not None) and (price > orb_high)
    orb_bear = (orb_low  is not None) and (price < orb_low)

    vol_spike = vol > (avg_vol * day_vol_multi) if avg_vol > 0 else False
    ema_gap = ema9 - ema20; prev_gap = pe9 - pe20
    cross_up   = (pe9<=pe20 and ema9>ema20) or (ema9>ema20 and ema_gap > prev_gap > 0)
    cross_down = (pe9>=pe20 and ema9<ema20) or (ema9<ema20 and ema_gap < prev_gap < 0)
    breakout  = price > ph + day_breakout_buffer
    breakdown = price < pl - day_breakout_buffer

    buy_t1  = all([price>vwap, st==True,  cross_up,   vol_spike, breakout])
    sell_t1 = all([price<vwap, st==False, cross_down, vol_spike, breakdown])

    if not buy_t1 and not sell_t1:
        return None, price, {}, {}, None, rsi, expiry_label
    if is_doji:
        return "SKIP", price, {}, {}, None, rsi, expiry_label

    pdh, pdl = get_prev_day_hl(df5)
    tgt_dist = price * TARGET_PCT

    if buy_t1:
        if not (RSI_BUY_MIN <= rsi <= RSI_BUY_MAX):
            return None, price, {}, {}, None, rsi, expiry_label
        if pdh and 0 < (pdh - price) < tgt_dist:
            return None, price, {}, {}, None, rsi, expiry_label
        tier1 = {
            "Price > VWAP": True, "Supertrend Green": True,
            "EMA 9/20 momentum": True, "Volume Spike": True,
            "Breakout": True,
        }
        tier2 = {
            f"RSI ({rsi:.1f})": True,
            "15min trend UP": trend15 == True,
            "Bullish candle": bull_clean,
            "Safe expiry": expiry_safe,
            "ORB breakout": orb_bull,
        }
        score = sum(tier2.values())
        if score < day_ce_min_t2:
            return None, price, {}, {}, None, rsi, expiry_label
        conf = "HIGH" if score == 5 else "NORMAL"
        return "BUY", price, tier1, tier2, conf, rsi, expiry_label

    else:
        if not (RSI_SELL_MIN <= rsi <= RSI_SELL_MAX):
            return None, price, {}, {}, None, rsi, expiry_label
        if pdl and 0 < (price - pdl) < tgt_dist:
            return None, price, {}, {}, None, rsi, expiry_label
        tier1 = {
            "Price < VWAP": True, "Supertrend Red": True,
            "EMA 9/20 momentum": True, "Volume Spike": True,
            "Breakdown": True,
        }
        tier2 = {
            f"RSI ({rsi:.1f})": True,
            "15min trend DOWN": trend15 == False,
            "Bearish candle": bear_clean,
            "Safe expiry": expiry_safe,
            "ORB breakdown": orb_bear,
        }
        score = sum(tier2.values())
        if score < PE_MIN_T2:
            return None, price, {}, {}, None, rsi, expiry_label
        conf = "HIGH" if score == 5 else "NORMAL"
        return "SELL", price, tier1, tier2, conf, rsi, expiry_label

# ─── ALERT FORMAT ───
def format_alert(signal, price, tier1, tier2, conf, rsi, expiry_label, vix, orb_high, orb_low, trade_num, pdh=None, pdl=None):
    now = datetime.now().strftime("%d %b %Y %I:%M %p")
    score = sum(tier2.values())
    strike = get_strike(price, signal)

    sl_pts  = round(price * SL_PCT, 1)
    tgt_pts = round(price * TARGET_PCT, 1)
    be_pts  = round(price * BREAKEVEN_PCT, 1)

    if signal == "BUY":
        sl = round(price - sl_pts, 2); tgt = round(price + tgt_pts, 2)
        be = round(price + be_pts, 2)
        action = f"BUY {strike}"; side = "CE 📈"
    else:
        sl = round(price + sl_pts, 2); tgt = round(price - tgt_pts, 2)
        be = round(price - be_pts, 2)
        action = f"BUY {strike}"; side = "PE 📉"

    header = f"🔥 <b>{conf} {signal} {side}</b> 🔥" if conf == "HIGH" else f"⚡ <b>{conf} {signal} {side}</b>"

    msg = f"""{header}

📡 <b>SENSEX — Full Strategy</b> (Tier1 5/5 + Tier2 + S&R + Expiry caution)
🔵 SIGNAL-ONLY — decide entry yourself
📅 {now}
💹 {action}
📊 Sensex: {price:.2f}
🛑 SL: {sl} ({sl_pts:.0f} pts)
🎯 Target: {tgt} ({tgt_pts:.0f} pts)
⚖️ Breakeven: SL → entry at {be} (+{be_pts:.0f} pts)
🔢 Trade #{trade_num}/{MAX_TRADES}

━━━━━━━━━━━━━━━━━━━━━━━━
<b>TIER 1 — All 5 ✅</b>"""
    for k in tier1:
        msg += f"\n  ✅ {k}"

    msg += f"\n\n<b>TIER 2 — {score}/5:</b>"
    for k, v in tier2.items():
        msg += f"\n  {'✅' if v else '❌'} {k}"

    sr_info = ""
    if pdh and pdl:
        dist_pdh = round(pdh - price, 0)
        dist_pdl = round(price - pdl, 0)
        if signal == "BUY":
            warn = " ⚠️ RESISTANCE NEAR" if 0 < dist_pdh < 30 else ""
            sr_info = f"\n\n🏗️ <b>Support & Resistance:</b>\n  📊 PDH: {pdh:.0f} ({dist_pdh:+.0f}pts){warn}\n  📊 PDL: {pdl:.0f} ({dist_pdl:+.0f}pts below)"
        else:
            warn = " ⚠️ SUPPORT NEAR" if 0 < dist_pdl < 30 else ""
            sr_info = f"\n\n🏗️ <b>Support & Resistance:</b>\n  📊 PDH: {pdh:.0f} ({dist_pdh:+.0f}pts above)\n  📊 PDL: {pdl:.0f} ({dist_pdl:+.0f}pts){warn}"

    msg += f"""{sr_info}

━━━━━━━━━━━━━━━━━━━━━━━━
📉 VIX: {vix} | Expiry: {expiry_label}
⏰ Weak momentum if <5pts after 15min (informational — you manage your own exit)
⚠️ Hard exit reference: 3:10 PM"""
    return msg.strip()

# ─── MAIN BOT ───
def run_bot():
    print("="*55)
    print("  SENSEX SIGNAL BOT — SIGNAL-ONLY, NO AUTO-EXECUTION")
    print("  Target ~54pts | SL ~47pts | BE ~25pts")
    print("  Tier1: VWAP+ST+EMA+Vol+Breakout")
    print("  Tier2: 15min+Candle+Expiry+ORB")
    print("="*55)

    if not login(): return

    print("📥 Finding SENSEX index token...")
    sensex_token = find_sensex_token()
    if not sensex_token:
        send_telegram("🆘 <b>SENSEX BOT FAILED TO START</b>\n\nCould not find SENSEX instrument token.")
        return

    send_telegram("🧪 TEST — Sensex bot working!\n"
        f"🕒 {datetime.now().strftime('%d %b %Y %I:%M:%S %p')}")

    fut_token, fut_sym = find_sensex_fut_token()
    if fut_token: print(f"✅ Futures: {fut_sym}")
    else: print("⚠️ No futures — using spot volume")

    send_telegram(
        "🤖 <b>Sensex Signal Bot Started</b>\n\n"
        "📊 Tier1: VWAP + Supertrend + EMA + Volume + Breakout\n"
        "🎯 Target: ~54pts | SL: ~47pts\n"
        "⚖️ Breakeven at ~25pts\n"
        "⏰ 9:30 AM — 2:00 PM | Expiry: Thursday\n\n"
        "🔵 <b>SIGNAL-ONLY — no auto-execution, ever.</b>\n"
        "You decide whether to enter on each alert.\n"
        "Send /help for all commands\n\n"
        "Watching Sensex 5 min chart...")

    trades_today = 0; last_date = None; last_dir = None
    orb_high = None; orb_low = None; orb_sent = False
    fut_vol = None; df15 = None

    while True:
        try:
            process_telegram_commands()

            now = datetime.now(); ct = now.time(); cd = now.date()

            if last_date != cd:
                trades_today = 0; last_date = cd; last_dir = None
                orb_high = None; orb_low = None; orb_sent = False
                fut_vol = None; df15 = None
                print(f"\n📅 New day: {cd}")
                print("🔐 New day — refreshing Kite login...")
                if login():
                    send_telegram(f"🔄 SENSEX bot: new day ({cd}) — re-logged in automatically.")
                else:
                    send_telegram(f"🆘 <b>SENSEX BOT — NEW DAY LOGIN FAILED</b>\n\nCannot fetch data today until this is fixed.\nCheck KITE_USER_ID/PASSWORD/TOTP_SECRET in .env on the server.")

            if ct < ORB_START:
                print(f"⏳ [{now.strftime('%H:%M')}] Before market")
                sleep_poll(60); continue
            if ct > HARD_EXIT:
                print(f"🔕 Market closed"); sleep_poll(300); continue
            if trades_today >= MAX_TRADES:
                print(f"🚫 Max trades done"); sleep_poll(300); continue
            today_market_end = EXPIRY_MARKET_END if now.weekday() == EXPIRY_WEEKDAY else MARKET_END
            if ct > today_market_end:
                print(f"⏰ No new trades after {today_market_end.strftime('%H:%M')}"
                      f"{' (expiry day — earlier cutoff)' if now.weekday()==EXPIRY_WEEKDAY else ''}")
                sleep_poll(300); continue

            df5 = fetch_data(sensex_token, "5minute", days=5)
            if df5 is None:
                print("❌ Data failed"); sleep_poll(60); continue

            if df15 is None:
                df15 = fetch_data(sensex_token, "15minute", days=10)
                if df15 is not None: print("✅ 15min data loaded")

            if fut_token and fut_vol is None:
                fut_vol = fetch_data(fut_token, "5minute", days=2)
                if fut_vol is not None: print("✅ Futures volume loaded")

            if ct >= ORB_START and ct < MARKET_START:
                oh, ol = get_orb(df5)
                if oh: orb_high, orb_low = oh, ol
                print(f"⏳ [{now.strftime('%H:%M')}] ORB: H={orb_high} L={orb_low}")
                sleep_poll(60); continue

            if not orb_sent:
                oh, ol = get_orb(df5)
                if oh: orb_high, orb_low = oh, ol
                orb_sent = True
                if orb_high:
                    send_telegram(f"📐 <b>SENSEX ORB FORMED</b>\n\n"
                        f"🟢 High: {orb_high:.2f}\n🔴 Low: {orb_low:.2f}\n\n"
                        f"Watching for signals...")
                    print(f"📐 ORB: {orb_high:.0f}—{orb_low:.0f}")

            vix = get_vix()
            if vix and vix > VIX_LIMIT:
                print(f"⚠️ VIX {vix:.1f} > {VIX_LIMIT}"); sleep_poll(300); continue

            result = check_signals(df5, df15, fut_vol)
            signal, price, tier1, tier2, conf, rsi, expiry_label = result

            if price:
                rsi_str = f"{rsi:.1f}" if rsi else "?"
                print(f"  [{now.strftime('%H:%M')}] Sensex:{price:.2f} | RSI:{rsi_str} | Signal:{signal or 'None'}")

            if signal == "SKIP":
                print(f"  ⛔ Doji — skipping")

            elif signal in ("BUY", "SELL"):
                if signal == last_dir:
                    print(f"  ⏭️ Duplicate {signal} — skipping")
                else:
                    trades_today += 1; last_dir = signal
                    score = sum(tier2.values())
                    tgt_pts = round(price * TARGET_PCT, 1)
                    sl_pts = round(price * SL_PCT, 1)
                    print(f"  🚨 {conf} {signal} | T2:{score}/5 | "
                          f"TGT:{tgt_pts:.0f}pts SL:{sl_pts:.0f}pts | #{trades_today}")
                    pdh, pdl = get_prev_day_hl(df5)
                    alert = format_alert(signal, price, tier1, tier2, conf, rsi,
                        expiry_label, round(vix,1) if vix else "N/A",
                        orb_high, orb_low, trades_today, pdh, pdl)
                    send_telegram(alert)

            sleep_poll(300)

        except KeyboardInterrupt:
            print("\n⛔ Sensex bot stopped.")
            send_telegram("⛔ Sensex bot stopped."); break
        except Exception as e:
            print(f"❌ Error: {e}"); sleep_poll(60)

if __name__ == "__main__":
    run_bot()
