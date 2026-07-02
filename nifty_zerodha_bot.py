"""
=============================================================
NIFTY SIGNAL BOT — MATCHES BACKTEST EXACTLY
=============================================================
Tier 1 (ALL 4): VWAP + Supertrend + Volume spike + Breakout
Tier 2: 15min trend, candle, expiry, ORB
Target: ~17pts | SL: ~15pts | Breakeven: ~8pts
Weak exit: if <5pts after 15min
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

POSITION_FILE = "position_state.json"

load_dotenv()

# ─── CREDENTIALS (from .env) ───
API_KEY    = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
BOT_TOKEN  = os.getenv("BOT_TOKEN")
CHAT_ID    = os.getenv("CHAT_ID")
KITE_USER_ID    = os.getenv("KITE_USER_ID")
KITE_PASSWORD   = os.getenv("KITE_PASSWORD")
KITE_TOTP_SECRET = os.getenv("KITE_TOTP_SECRET")
LOT_SIZE = int(os.getenv("LOT_SIZE", "75"))

# ─── CONFIG — same as backtest ───
NIFTY_TOKEN = 256265
SL_PCT         = 0.00063
TARGET_PCT     = 0.00071
BREAKEVEN_PCT  = 0.00034  # ~8pts — matches backtest
MOMENTUM_MIN   = 5         # weak-exit threshold (pts) — matches backtest
MAX_TRADES     = 3
STRIKE_GAP     = 50
VIX_LIMIT      = 20
OPTION_DELTA   = 0.5      # ATM approx delta, used to convert spot SL/target to premium

# ── TRAILING STOP — matches nifty_zerodha_backtest.py exactly (proven: rescues
# trades that would otherwise round-trip back to breakeven, never makes a trade
# worse since it only ever ratchets the SL forward). ──
TRAIL_TRIGGER_MULT = 1.5  # activates once favorable move >= 1.5x the breakeven distance
TRAIL_STEP_MULT    = 0.6  # trailing SL follows this far (x breakeven distance) behind the peak

# ─── AUTO-TRADING STATE (in-memory only — resets to OFF on every restart) ───
AUTO_ARMED = False
position = None            # holds dict of open position, or None
telegram_offset = 0        # for polling telegram updates
backtest_running = False   # prevents overlapping /backtest runs on the small server

RSI_BUY_MIN = 48; RSI_BUY_MAX = 63
RSI_SELL_MIN = 37; RSI_SELL_MAX = 53
CE_MIN_T2 = 3; PE_MIN_T2 = 4
VOL_MULTI = 1.2

ORB_START    = dtime(9, 15)
MARKET_START = dtime(9, 30)
MARKET_END   = dtime(14, 0)
HARD_EXIT    = dtime(15, 10)

# ── EXPIRY DAY CAUTION (fake breakouts + extreme theta decay) ──
# Not blocked entirely — expiry day can be very profitable on real moves.
# Just requires stronger confirmation and exits faster if it's not working.
# Must match nifty_zerodha_backtest.py exactly.
EXPIRY_WEEKDAY      = 1            # Tuesday for NIFTY (change to 3=Thursday for SENSEX later)
EXPIRY_VOL_MULTI    = 1.6          # was 1.2 — filter out fake breakout candles with weak volume
EXPIRY_CE_MIN_T2    = 4            # was 3 — need stronger confirmation on CE too
EXPIRY_MARKET_END   = dtime(13, 0) # was 14:00 — stop new entries earlier, theta accelerates after
EXPIRY_MOMENTUM_MIN_MINUTES = 10   # was 15 — check momentum sooner, exit dead trades faster
EXPIRY_BREAKOUT_BUFFER = 3         # extra pts above prev high/below prev low — filters fake pokes

# ─── KITE ───
kite = KiteConnect(api_key=API_KEY)
TOKEN_FILE = "kite_token.json"

def load_cached_token():
    """Kite tokens are valid until ~6 AM the next day. Reuse today's token
    instead of logging in again on every restart (e.g. dev.py auto-restarts)."""
    if not os.path.exists(TOKEN_FILE):
        return False
    try:
        with open(TOKEN_FILE, "r") as f:
            data = json.load(f)
        if data.get("date") != datetime.now().strftime("%Y-%m-%d"):
            return False  # token is from a previous day — expired
        kite.set_access_token(data["access_token"])
        kite.profile()  # verify it actually still works
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
    """Auto-login using TOTP — no manual token needed."""
    try:
        print("🔐 Auto-login with TOTP...")
        sess = requests.Session()
        # Realistic browser headers — bare 'python-requests' UA gets flagged by
        # Zerodha's anti-bot checks, especially from new/datacenter IPs.
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://kite.zerodha.com/",
            "Origin": "https://kite.zerodha.com",
            "X-Kite-Version": "3.0.0",
        })

        # Step 1: Get login page
        resp = sess.get(kite.login_url(), allow_redirects=True)

        # Step 2: Submit user_id + password
        resp = sess.post("https://kite.zerodha.com/api/login", data={
            "user_id": KITE_USER_ID,
            "password": KITE_PASSWORD
        })
        data = resp.json()
        if data.get("status") != "success":
            print(f"❌ Login step 1 failed: {data.get('message','Unknown error')}")
            return False
        request_id = data["data"]["request_id"]

        # Step 3: Submit TOTP
        totp = pyotp.TOTP(KITE_TOTP_SECRET).now()
        resp = sess.post("https://kite.zerodha.com/api/twofa", data={
            "user_id": KITE_USER_ID,
            "request_id": request_id,
            "twofa_value": totp,
            "twofa_type": "totp"
        })
        data = resp.json()
        if data.get("status") != "success":
            print(f"❌ TOTP failed: {data.get('message','Unknown error')}")
            return False

        time.sleep(1)  # let the session fully register server-side before the redirect fetch

        # Step 4: Get request_token — Kite redirects twice:
        # /connect/login -> /connect/finish?sess_id=... -> <your redirect_url>?request_token=...
        # Follow both hops manually (allow_redirects=False each time) to capture the final URL.
        redirect_url = ""
        next_url = kite.login_url()
        for _ in range(3):  # at most 2 hops expected, +1 spare
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

        # Step 5: Generate session
        session_data = kite.generate_session(request_token, api_secret=API_SECRET)
        kite.set_access_token(session_data["access_token"])
        save_cached_token(session_data["access_token"])
        print("✅ Auto-login successful!\n")
        return True

    except Exception as e:
        print(f"❌ Auto-login failed: {e}")
        return False

def manual_login():
    """Fallback: manual token paste. Skipped on headless/non-interactive runs
    (e.g. a cloud server) so the bot fails loudly instead of hanging on input()."""
    if not sys.stdin.isatty():
        msg = ("❌ Auto-login failed AND no interactive terminal available to paste a token.\n"
               "Fix KITE_USER_ID / KITE_PASSWORD / KITE_TOTP_SECRET in .env and restart the bot.")
        print(msg)
        try: send_telegram(f"🆘 <b>BOT COULD NOT LOG IN</b>\n\n{msg}")
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
    """Reuse today's cached token if valid. Otherwise auto-login, fallback to manual."""
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

# ─── TELEGRAM COMMAND POLLING (for /start_auto, /stop_auto, /status, /square_off) ───
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
    """Run the backtest for N days and send a condensed report via Telegram.
    Runs in a background thread so it never blocks signal-checking or position
    monitoring (especially important if a real position is open)."""
    global backtest_running
    try:
        import nifty_zerodha_backtest as bt
        # Reuse this bot's already-authenticated session — avoids a second login
        bt.kite.set_access_token(kite.access_token)

        send_telegram(f"⏳ Running {days}-day backtest... this can take 20-60 seconds.")

        df5 = bt.fetch_data(bt.NIFTY_TOKEN, "5minute", days=days)
        if df5 is None or df5.empty:
            send_telegram("❌ Backtest failed — could not fetch price data.")
            return
        df15 = bt.fetch_data(bt.NIFTY_TOKEN, "15minute", days=days)
        fut_token, _ = find_nifty_fut_token()
        fut_vol = bt.fetch_data(fut_token, "5minute", days=days) if fut_token else None

        trades = bt.run_backtest(df5, df15, fut_vol)
        if not trades:
            send_telegram(f"📊 <b>{days}-day backtest</b>: No trades found in this period.")
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
            f"📊 <b>{days}-DAY BACKTEST RESULT</b>\n\n"
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

        # Send the full trade-by-trade CSV as a file attachment
        csv_path = f"backtest_telegram_{days}d.csv"
        tdf.to_csv(csv_path, index=False)
        send_telegram_document(csv_path, caption=f"📁 Full {days}-day trade log ({total} trades)")
    except Exception as e:
        send_telegram(f"❌ Backtest error: {e}")
    finally:
        backtest_running = False

def process_telegram_commands():
    """Check for /start_auto /stop_auto /status /square_off /backtest /help commands."""
    global AUTO_ARMED, backtest_running
    updates = get_telegram_updates()
    for u in updates:
        msg = u.get("message", {})
        text = msg.get("text", "").strip().lower()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != str(CHAT_ID):
            continue  # ignore messages from anyone else

        if text == "/start_auto":
            AUTO_ARMED = True
            print("🟢 AUTO-TRADING ARMED via Telegram")
            send_telegram(
                "✅ <b>AUTO-TRADING ARMED</b> 🟢\n\n"
                "Bot will now place REAL orders when a signal fires.\n"
                f"Lot size: {LOT_SIZE} | One position at a time.\n\n"
                "Send /stop_auto to disarm.\n"
                "Send /status anytime to check state."
            )
        elif text == "/stop_auto":
            AUTO_ARMED = False
            print("🔴 AUTO-TRADING DISARMED via Telegram")
            send_telegram(
                "⛔ <b>AUTO-TRADING DISARMED</b>\n\n"
                "No new orders will be placed.\n"
                "Existing position (if any) is still being monitored for SL/Target/exit.\n"
                "Send /square_off to close it manually."
            )
        elif text == "/status":
            state = "🟢 ARMED — placing real orders" if AUTO_ARMED else "🔴 DISARMED — signal-only"
            if position:
                pos_info = (f"{position['signal']} {position['symbol']}\n"
                            f"Qty: {position['qty']} | Entry: {position['entry_premium']}\n"
                            f"SL: {position['sl_premium']} | Target: {position['target_premium']}\n"
                            f"Breakeven hit: {'Yes' if position['breakeven_hit'] else 'No'}\n"
                            f"Trailing active: {'Yes 🔒' if position.get('trail_active') else 'No'} "
                            f"(peak: +{position.get('peak_favorable',0):.1f}pts)")
            else:
                pos_info = "No open position"
            send_telegram(f"📊 <b>Bot Status</b>\n\nAuto-trade: {state}\n\n<b>Position:</b>\n{pos_info}")
        elif text == "/square_off":
            if position:
                send_telegram("🔻 Manual square-off requested...")
                exit_position("MANUAL")
            else:
                send_telegram("No open position to square off.")
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
                "🤖 <b>Available Commands</b>\n\n"
                "/start_auto — arm auto order execution\n"
                "/stop_auto — disarm (stop new entries)\n"
                "/status — check armed state & position\n"
                "/square_off — emergency close open position\n"
                "/backtest [days] — run backtest, e.g. /backtest 60 (default 60, max 100)\n"
                "/help — this message"
            )

def sleep_poll(seconds):
    """Sleep in small chunks, checking Telegram commands every 5s in between.
    Prevents /status, /start_auto etc. from waiting up to 5 minutes for a reply."""
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
            send_telegram("⚠️ <b>Session expired mid-day</b> — re-logging in automatically...")
            if login():
                try:
                    return _fetch()
                except Exception as e2:
                    print(f"  Data error after relogin: {e2}"); return None
        print(f"  Data error: {e}"); return None

def find_nifty_fut_token():
    try:
        instruments = kite.instruments("NFO")
        df = pd.DataFrame(instruments)
        nf = df[(df['name']=='NIFTY')&(df['instrument_type']=='FUT')&(df['segment']=='NFO-FUT')].copy()
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

# ─── ORDER EXECUTION (only runs when AUTO_ARMED == True) ───
def find_option_symbol(price, signal):
    """Find ATM CE/PE tradingsymbol for the nearest weekly expiry."""
    try:
        atm = round(price / STRIKE_GAP) * STRIKE_GAP
        opt_type = "CE" if signal == "BUY" else "PE"
        instruments = kite.instruments("NFO")
        df = pd.DataFrame(instruments)
        opts = df[(df['name']=='NIFTY') & (df['instrument_type']==opt_type) &
                  (df['strike']==atm)].copy()
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
    """Check available cash covers the trade + 5% buffer. Fail-safe: blocks trade if margin check itself fails."""
    try:
        margins = kite.margins()
        available_cash = margins["equity"]["available"]["live_balance"]
        required = estimated_premium * qty
        buffer = required * 0.05
        sufficient = available_cash >= (required + buffer)
        return sufficient, available_cash, required
    except Exception as e:
        print(f"⚠️ Margin check failed: {e}")
        return False, 0, 0  # can't verify funds → block the trade, don't risk it

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
    """Poll order status until COMPLETE, return average fill price."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            history = kite.order_history(order_id)
            last = history[-1]
            if last['status'] == 'COMPLETE':
                return float(last['average_price'])
            elif last['status'] in ('REJECTED', 'CANCELLED'):
                print(f"  Order {order_id} {last['status']}: {last.get('status_message','')}")
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
    """Modify the GTT's SL leg. Used for both breakeven move and trailing-stop ratchets."""
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

def save_position_state():
    """Persist the open position to disk so it survives a bot crash/restart."""
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
    """Recover an open position after a crash/restart, if one exists on disk."""
    global position
    if not os.path.exists(POSITION_FILE):
        return
    try:
        with open(POSITION_FILE, "r") as f:
            data = json.load(f)
        data["entry_time"] = datetime.fromisoformat(data["entry_time"])
        position = data
        print(f"🔄 Recovered open position from disk: {position['symbol']}")
        send_telegram(
            f"🔄 <b>POSITION RECOVERED AFTER RESTART</b>\n\n"
            f"{position['symbol']}\nEntry: {position['entry_premium']}\n"
            f"SL: {position['sl_premium']} | Target: {position['target_premium']}\n"
            f"Breakeven hit: {'Yes' if position['breakeven_hit'] else 'No'}\n\n"
            f"Resuming monitoring. The GTT order was untouched while the bot was down."
        )
    except Exception as e:
        print(f"⚠️ Could not load position state: {e}")
        send_telegram(f"⚠️ <b>Found a saved position but couldn't load it!</b>\n{e}\n\nCheck Kite manually for any open NIFTY option position.")

def execute_entry(signal, price, conf):
    """Place a real order. Only called when AUTO_ARMED and no open position."""
    global position
    symbol = find_option_symbol(price, signal)
    if not symbol:
        send_telegram(f"❌ Could not find option symbol for {signal} ATM — auto-entry skipped")
        return

    try:
        quote = kite.quote([f"NFO:{symbol}"])
        ltp = quote[f"NFO:{symbol}"]["last_price"]
    except Exception as e:
        send_telegram(f"❌ Could not fetch LTP for {symbol} — auto-entry skipped\n{e}")
        return

    # Funds check — block trade if insufficient margin (or can't verify)
    has_funds, available, required = check_sufficient_funds(ltp, LOT_SIZE)
    if not has_funds:
        send_telegram(
            f"❌ <b>INSUFFICIENT FUNDS — TRADE SKIPPED</b>\n\n"
            f"{symbol}\nRequired: ₹{required:,.0f} (+5% buffer)\n"
            f"Available: ₹{available:,.0f}\n\n"
            f"Add funds or reduce LOT_SIZE in .env"
        )
        return

    send_telegram(f"🤖 <b>AUTO-EXECUTING ENTRY</b>\n{signal} {symbol}\nQty: {LOT_SIZE} | LTP: {ltp}")

    order_id = place_entry_order(symbol, LOT_SIZE)
    if not order_id:
        return

    avg_price = get_order_avg_price(order_id)
    if avg_price is None:
        send_telegram(f"⚠️ Order {order_id} for {symbol} not confirmed filled — CHECK KITE MANUALLY")
        return

    # Convert spot SL/target (backtest logic) to option premium via delta approx
    spot_sl_pts = price * SL_PCT
    spot_tgt_pts = price * TARGET_PCT
    sl_premium = round(avg_price - spot_sl_pts * OPTION_DELTA, 1)
    target_premium = round(avg_price + spot_tgt_pts * OPTION_DELTA, 1)

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
        f"✅ <b>POSITION OPEN</b>\n\n"
        f"{symbol}\nEntry: {avg_price}\nSL: {sl_premium}\nTarget: {target_premium}\n"
        f"GTT: {gtt_status}"
    )

def monitor_position(current_spot):
    """Check breakeven trail, weak-exit, hard-exit for the open position."""
    global position
    if position is None: return

    spot_move = (current_spot - position["entry_spot"]) if position["signal"]=="BUY" \
                else (position["entry_spot"] - current_spot)
    be_threshold = position["entry_spot"] * BREAKEVEN_PCT
    position["peak_favorable"] = max(position.get("peak_favorable", 0.0), spot_move)

    # Breakeven: move SL to entry premium
    if not position["breakeven_hit"] and spot_move >= be_threshold:
        if position["gtt_id"]:
            ok = modify_gtt_sl(position["gtt_id"], position["symbol"], position["qty"],
                position["entry_premium"], position["target_premium"], position["entry_premium"])
            if ok:
                position["breakeven_hit"] = True
                save_position_state()
                send_telegram(f"⚖️ <b>BREAKEVEN ACTIVATED</b>\n{position['symbol']}\nSL moved to entry: {position['entry_premium']}")

    # Trailing stop: once price extends well past breakeven, ratchet SL behind
    # the peak instead of leaving it flat at breakeven (matches the backtest's
    # proven trailing-stop logic — same multipliers, same one-way ratchet).
    trail_trigger_dist = be_threshold * TRAIL_TRIGGER_MULT
    trail_step_dist = be_threshold * TRAIL_STEP_MULT
    if position["peak_favorable"] >= trail_trigger_dist and position["gtt_id"]:
        new_sl_spot_dist = position["peak_favorable"] - trail_step_dist
        new_sl_premium = round(position["entry_premium"] + new_sl_spot_dist * OPTION_DELTA, 1)
        is_better = (new_sl_premium > position["sl_premium"]) if position["signal"]=="BUY" \
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
                send_telegram(f"🔒 <b>TRAILING STOP</b>\n{position['symbol']}\nSL moved to {new_sl_premium} (locking in profit)")

    # Weak exit: faster check on expiry day (theta burns fast)
    weak_window = EXPIRY_MOMENTUM_MIN_MINUTES if datetime.now().weekday() == EXPIRY_WEEKDAY else 15
    elapsed_min = (datetime.now() - position["entry_time"]).total_seconds() / 60
    if elapsed_min >= weak_window and not position["weak_checked"]:
        position["weak_checked"] = True
        if spot_move < MOMENTUM_MIN:
            send_telegram(f"⚠️ <b>WEAK MOMENTUM</b> — exiting {position['symbol']} (only {spot_move:.1f}pts after {weak_window}min)")
            exit_position("WEAK")
            return

    # Hard exit time
    if datetime.now().time() >= HARD_EXIT:
        send_telegram(f"⏰ <b>HARD EXIT TIME (3:10 PM)</b> — closing {position['symbol']}")
        exit_position("EOD")

def exit_position(reason):
    """Cancel GTT and market-exit the open position."""
    global position
    if position is None: return
    if position["gtt_id"]:
        cancel_gtt(position["gtt_id"])
    order_id = place_exit_order(position["symbol"], position["qty"])
    send_telegram(f"🔚 <b>POSITION CLOSED ({reason})</b>\n{position['symbol']}\nExit order: {order_id}")
    position = None
    save_position_state()

# ─── SIGNAL ENGINE (same logic as backtest) ───
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

    df5 = df5.dropna(subset=['VWAP','AvgVol'])
    if len(df5) < 2:
        return None, None, {}, {}, None, None, None

    curr = df5.iloc[-1]; prev = df5.iloc[-2]
    price = float(curr['Close'])
    o,h,l,c = float(curr['Open']),float(curr['High']),float(curr['Low']),float(curr['Close'])
    vwap  = float(curr['VWAP'])
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
    breakout  = price > ph + day_breakout_buffer
    breakdown = price < pl - day_breakout_buffer

    buy_t1  = all([price>vwap, st==True,  vol_spike, breakout])
    sell_t1 = all([price<vwap, st==False, vol_spike, breakdown])

    if not buy_t1 and not sell_t1:
        return None, price, {}, {}, None, rsi, expiry_label
    if is_doji:
        return "SKIP", price, {}, {}, None, rsi, expiry_label

    # S&R: Previous Day High/Low
    pdh, pdl = get_prev_day_hl(df5)
    tgt_dist = price * TARGET_PCT

    if buy_t1:
        if not (RSI_BUY_MIN <= rsi <= RSI_BUY_MAX):
            return None, price, {}, {}, None, rsi, expiry_label
        if pdh and 0 < (pdh - price) < tgt_dist:
            return None, price, {}, {}, None, rsi, expiry_label
        tier1 = {
            "Price > VWAP": True, "Supertrend Green": True,
            "Volume Spike": True, "Breakout": True,
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
            "Volume Spike": True, "Breakdown": True,
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

📡 <b>REGULAR WINDOW — Full Strategy</b> (Tier1 5/5 + Tier2 + S&R + Expiry caution)
📅 {now}
💹 {action}
📊 Nifty: {price:.2f}
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

    # S&R context (info only — you decide)
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
⏰ Weak exit if <5pts after 15min
⚠️ Hard exit 3:10 PM"""
    return msg.strip()

# ─── MAIN BOT ───
def run_bot():
    global position
    print("="*55)
    print("  NIFTY SIGNAL BOT — MATCHES BACKTEST")
    print("  Target ~17pts | SL ~15pts | BE ~8pts")
    print("  Tier1: VWAP+ST+Vol+Breakout")
    print("  Tier2: 15min+Candle+Expiry+ORB")
    print("  Auto-trade: DISARMED (send /start_auto to arm)")
    print("="*55)

    if not login(): return

    load_position_state()  # recover an open position if the bot crashed/restarted

    send_telegram("🧪 TEST — Bot working!\n"
        f"🕒 {datetime.now().strftime('%d %b %Y %I:%M:%S %p')}")

    fut_token, fut_sym = find_nifty_fut_token()
    if fut_token: print(f"✅ Futures: {fut_sym}")
    else: print("⚠️ No futures — using spot volume")

    send_telegram(
        "🤖 <b>Nifty Signal Bot Started</b>\n\n"
        "📊 Tier1: VWAP + Supertrend + Volume + Breakout\n"
        "🎯 Target: ~17pts | SL: ~15pts\n"
        "⚖️ Breakeven at ~8pts\n"
        "⏰ 9:30 AM — 2:00 PM\n\n"
        "🔴 <b>Auto-trade: DISARMED</b> — signal-only mode\n"
        "Send /start_auto to enable real order placement\n"
        "Send /help for all commands\n\n"
        "Watching Nifty 5 min chart...")

    trades_today = 0; last_date = None; last_dir = None
    orb_high = None; orb_low = None; orb_sent = False
    fut_vol = None; df15 = None

    while True:
        try:
            # Always check Telegram commands first — works anytime, any state
            process_telegram_commands()

            now = datetime.now(); ct = now.time(); cd = now.date()

            if last_date != cd:
                trades_today = 0; last_date = cd; last_dir = None
                orb_high = None; orb_low = None; orb_sent = False
                fut_vol = None; df15 = None
                print(f"\n📅 New day: {cd}")
                # Yesterday's token expires ~6 AM today — this process keeps running
                # for weeks (systemd doesn't restart it daily), so re-login is required
                # here or every API call will start failing once the old token expires.
                print("🔐 New day — refreshing Kite login...")
                if login():
                    send_telegram(f"🔄 New day ({cd}) — re-logged in automatically.")
                else:
                    send_telegram(f"🆘 <b>NEW DAY LOGIN FAILED</b>\n\nBot cannot fetch data or trade today until this is fixed.\nCheck KITE_USER_ID/PASSWORD/TOTP_SECRET in .env on the server.")

            # If a position is open, monitor it regardless of market window edge cases
            if position is not None:
                df5_quick = fetch_data(NIFTY_TOKEN, "5minute", days=2)
                if df5_quick is not None and len(df5_quick) > 0:
                    monitor_position(float(df5_quick['Close'].iloc[-1]))

            if ct < ORB_START:
                print(f"⏳ [{now.strftime('%H:%M')}] Before market")
                sleep_poll(60); continue
            if ct > HARD_EXIT:
                print(f"🔕 Market closed"); sleep_poll(300); continue
            if trades_today >= MAX_TRADES:
                print(f"🚫 Max trades done"); sleep_poll(60 if position else 300); continue
            today_market_end = EXPIRY_MARKET_END if now.weekday() == EXPIRY_WEEKDAY else MARKET_END
            if ct > today_market_end:
                print(f"⏰ No new trades after {today_market_end.strftime('%H:%M')}"
                      f"{' (expiry day — earlier cutoff)' if now.weekday()==EXPIRY_WEEKDAY else ''}")
                sleep_poll(60 if position else 300); continue

            df5 = fetch_data(NIFTY_TOKEN, "5minute", days=5)
            if df5 is None:
                print("❌ Data failed"); sleep_poll(60); continue

            if df15 is None:
                df15 = fetch_data(NIFTY_TOKEN, "15minute", days=10)
                if df15 is not None: print("✅ 15min data loaded")

            if fut_token and fut_vol is None:
                fut_vol = fetch_data(fut_token, "5minute", days=2)
                if fut_vol is not None: print("✅ Futures volume loaded")

            # ORB formation
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
                    send_telegram(f"📐 <b>ORB FORMED</b>\n\n"
                        f"🟢 High: {orb_high:.2f}\n🔴 Low: {orb_low:.2f}\n\n"
                        f"Watching for signals...")
                    print(f"📐 ORB: {orb_high:.0f}—{orb_low:.0f}")

            vix = get_vix()
            if vix and vix > VIX_LIMIT:
                print(f"⚠️ VIX {vix:.1f} > {VIX_LIMIT}"); sleep_poll(300); continue

            # Don't look for new signals if a position is already open (one at a time in auto mode)
            if position is not None:
                print(f"  [{now.strftime('%H:%M')}] Position open: {position['symbol']} — monitoring only")
                sleep_poll(60); continue

            result = check_signals(df5, df15, fut_vol)
            signal, price, tier1, tier2, conf, rsi, expiry_label = result

            if price:
                rsi_str = f"{rsi:.1f}" if rsi else "?"
                print(f"  [{now.strftime('%H:%M')}] Nifty:{price:.2f} | RSI:{rsi_str} | Signal:{signal or 'None'} | Auto:{'ON' if AUTO_ARMED else 'OFF'}")

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

                    if AUTO_ARMED:
                        execute_entry(signal, price, conf)

            sleep_poll(60 if position else 300)

        except KeyboardInterrupt:
            print("\n⛔ Bot stopped.")
            send_telegram("⛔ Bot stopped." + (f"\n⚠️ Position still open: {position['symbol']}" if position else "")); break
        except Exception as e:
            print(f"❌ Error: {e}"); sleep_poll(60)

if __name__ == "__main__":
    run_bot()
