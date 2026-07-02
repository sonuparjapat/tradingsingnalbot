"""
=============================================================
SENSEX BACKTESTER v1.0 — same proven engine as NIFTY
=============================================================
Identical strategy logic to nifty_zerodha_backtest.py, adapted for SENSEX:
- Exchange: BSE (spot) / BFO (futures) instead of NSE/NFO
- Expiry day: Thursday instead of Tuesday (EXPIRY_WEEKDAY=3)
- Strike gap: 100 instead of 50 (SENSEX trades ~80,000+ levels)
- SENSEX instrument token looked up dynamically (not hardcoded)

Everything else — Tier 1 (5 conditions), Tier 2 scoring, S&R filter,
expiry-day caution, breakeven/weak-exit — is byte-for-byte the same
logic that's proven profitable on NIFTY. SL/Target use percentages
(not fixed points), so they naturally scale to SENSEX's price level.
=============================================================
"""

from kiteconnect import KiteConnect
import pandas as pd
import numpy as np
import webbrowser, os, json, time
from datetime import datetime, timedelta, time as dtime
from dotenv import load_dotenv
from urllib.parse import urlparse, parse_qs
import requests
import pyotp
import warnings
warnings.filterwarnings('ignore')

load_dotenv()

# ─────────────────────────────────────────
#   CREDENTIALS (from .env) — same Kite account as NIFTY bot
# ─────────────────────────────────────────
API_KEY    = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
KITE_USER_ID     = os.getenv("KITE_USER_ID")
KITE_PASSWORD    = os.getenv("KITE_PASSWORD")
KITE_TOTP_SECRET = os.getenv("KITE_TOTP_SECRET")

# ─────────────────────────────────────────
#   CONFIG — identical to NIFTY except where noted
# ─────────────────────────────────────────
SL_PCT      = 0.00063   # same % as NIFTY — scales naturally to SENSEX price level
TARGET_PCT  = 0.00071
BREAKEVEN_PCT = 0.00034
MOMENTUM_CANDLES = 3
MOMENTUM_MIN = 5
MAX_TRADES  = 3
CAPITAL     = 100000
STRIKE_GAP  = 100       # SENSEX strikes are 100pt apart (NIFTY was 50)
LOT_SIZE    = int(os.getenv("SENSEX_LOT_SIZE", os.getenv("LOT_SIZE", 20)))

# ── OPTION PREMIUM ESTIMATE (replaces flat spot-% P&L) ──
# Same model as NIFTY — % of spot price, so it auto-scales without separate
# calibration (see nifty_zerodha_backtest.py for the full rationale).
DELTA_BASE        = 0.5
DELTA_SCALE_PCT   = 0.0048
EXPIRY_THETA_HAIRCUT_PCT = 0.15

# ── TRAILING STOP (locks in profit on big winners instead of round-tripping to BE) ──
TRAIL_TRIGGER_MULT = 1.5
TRAIL_STEP_MULT    = 0.6

RSI_BUY_MIN  = 48; RSI_BUY_MAX  = 63
RSI_SELL_MIN = 37; RSI_SELL_MAX = 53

TRADE_START = dtime(9, 30)
TRADE_END   = dtime(14, 0)
HARD_EXIT   = dtime(15, 10)

CE_MIN_T2 = 3
PE_MIN_T2 = 4

VOL_MULTI   = 1.2

# ── EXPIRY DAY CAUTION ──
EXPIRY_WEEKDAY      = 3            # Thursday for SENSEX (NIFTY uses 1=Tuesday)
EXPIRY_VOL_MULTI    = 1.6
EXPIRY_CE_MIN_T2    = 4
EXPIRY_TRADE_END    = dtime(13, 0)
EXPIRY_MOMENTUM_CANDLES = 2
EXPIRY_BREAKOUT_BUFFER  = 3

# ── ATR-BASED DYNAMIC RISK (EXPERIMENTAL — A/B toggle, default OFF) ──
# Same multipliers as NIFTY — ATR naturally scales to SENSEX's point-level,
# so no separate calibration needed. Set True to test vs the fixed-% baseline.
USE_ATR_DYNAMIC   = False
ATR_PERIOD        = 14
SL_ATR_MULT       = 0.50
TARGET_ATR_MULT   = 0.5635
BE_ATR_MULT       = 0.270
MOMENTUM_ATR_MULT = 0.167
BUFFER_ATR_MULT   = 0.10

# ── BREAKOUT CONFIRMATION (EXPERIMENTAL — A/B toggle, default OFF) ──
# Same model as NIFTY — see nifty_zerodha_backtest.py for full rationale.
USE_BREAKOUT_CONFIRM = False

# ── MORNING WINDOW — candle-structure SL (same concept as NIFTY morning) ──
# SENSEX ~80k vs NIFTY ~24k (~3.3x), so buffer scales proportionally
CANDLE_SL_BUFFER = 15   # spot pts below prev candle low (CE) / above high (PE)

# ─────────────────────────────────────────
#   LOGIN (identical to NIFTY bot/backtest — same Kite account/session)
# ─────────────────────────────────────────
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
    login_url = kite.login_url()
    print(f"\n🌐 Opening Zerodha login...")
    webbrowser.open(login_url)
    print("\nAfter login copy request_token from redirect URL")
    request_token = input("\nPaste request_token here: ").strip()
    try:
        data = kite.generate_session(request_token, api_secret=API_SECRET)
        kite.set_access_token(data["access_token"])
        save_cached_token(data["access_token"])
        print("✅ Login successful!\n")
        return True
    except Exception as e:
        print(f"❌ Login failed: {e}")
        return False

def login():
    if load_cached_token():
        return True
    if KITE_USER_ID and KITE_PASSWORD and KITE_TOTP_SECRET and \
       KITE_USER_ID != "YOUR_USER_ID":
        if auto_login(): return True
        print("⚠️ Auto-login failed, trying manual...")
    return manual_login()

# ─────────────────────────────────────────
#   DATA
# ─────────────────────────────────────────
def fetch_data(token, interval, days, label=""):
    to_dt   = datetime.now()
    from_dt = to_dt - timedelta(days=days)
    try:
        candles = kite.historical_data(token, from_dt, to_dt, interval)
        if not candles:
            print(f"  ⚠️ {label} Requested {from_dt.date()}→{to_dt.date()} ({days}d) but got ZERO candles back")
            return None
        df = pd.DataFrame(candles)
        df.columns = ['date','Open','High','Low','Close','Volume']
        df.set_index('date', inplace=True)
        df.index = pd.to_datetime(df.index)
        df = df.dropna()
        actual_days = (df.index[-1].date() - df.index[0].date()).days
        if actual_days < days - 10:  # meaningfully short of what we asked for
            print(f"  ⚠️ {label} Requested {days}d ({from_dt.date()}→{to_dt.date()}) "
                  f"but Kite only returned {actual_days}d ({df.index[0].date()}→{df.index[-1].date()}). "
                  f"This is Kite's data, not a bug here — likely limited history for this token.")
        return df
    except Exception as e:
        print(f"  ❌ Fetch error ({label}): {e}")
        return None

def find_sensex_token():
    """Find SENSEX spot index instrument token dynamically (BSE) —
    not hardcoded since it can differ/change, unlike NIFTY's well-known 256265."""
    try:
        instruments = kite.instruments("BSE")
        df = pd.DataFrame(instruments)
        idx = df[(df['tradingsymbol'] == 'SENSEX')]
        if idx.empty:
            print("❌ Could not find SENSEX in BSE instruments")
            # Diagnostic: show anything close, to help debug naming mismatches
            close = df[df['tradingsymbol'].astype(str).str.contains('SENSEX', case=False, na=False)]
            if not close.empty:
                print("  Found similar symbols instead:")
                print(close[['tradingsymbol','instrument_token','segment','exchange']].to_string(index=False))
            return None
        row = idx.iloc[0]
        token = int(row['instrument_token'])
        print(f"  SENSEX token: {token} | segment={row.get('segment')} | exchange={row.get('exchange')} "
              f"| name={row.get('name')}")
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
        if nf.empty:
            print("  No SENSEX futures found on BFO")
            return None
        nf['expiry'] = pd.to_datetime(nf['expiry'])
        future = nf[nf['expiry'].dt.date >= datetime.now().date()].sort_values('expiry')
        if future.empty: return None
        nearest = future.iloc[0]
        print(f"  Futures: {nearest['tradingsymbol']}")
        return int(nearest['instrument_token'])
    except Exception as e:
        print(f"  Futures lookup error: {e}")
        return None

# ─────────────────────────────────────────
#   INDICATORS (identical to NIFTY)
# ─────────────────────────────────────────
def ema(s, p): return s.ewm(span=p, adjust=False).mean()

def calculate_vwap(df):
    df = df.copy()
    df['Date'] = df.index.date
    df['TP']   = (df['High']+df['Low']+df['Close'])/3
    if df['Volume'].sum() > 0:
        df['TPV']    = df['TP']*df['Volume']
        df['CumTPV'] = df.groupby('Date')['TPV'].cumsum()
        df['CumVol'] = df.groupby('Date')['Volume'].cumsum()
        vwap = df['CumTPV']/df['CumVol']
    else:
        vwap = df.groupby('Date')['TP'].transform(lambda x: x.expanding().mean())
    return vwap.fillna(df['Close'])

def calculate_supertrend(df, period=10, multiplier=3.0):
    df = df.copy()
    hl2 = (df['High']+df['Low'])/2
    df['TR'] = np.maximum(df['High']-df['Low'],
               np.maximum(abs(df['High']-df['Close'].shift(1)),
                          abs(df['Low']-df['Close'].shift(1))))
    df['ATR'] = df['TR'].rolling(period).mean()
    upper = (hl2+multiplier*df['ATR']).values.copy()
    lower = (hl2-multiplier*df['ATR']).values.copy()
    trend = [True]*len(df); close = df['Close'].values
    for i in range(1,len(df)):
        lower[i] = max(lower[i],lower[i-1]) if close[i-1]>lower[i-1] else lower[i]
        upper[i] = min(upper[i],upper[i-1]) if close[i-1]<upper[i-1] else upper[i]
        if not trend[i-1] and close[i]>upper[i]:  trend[i]=True
        elif trend[i-1] and close[i]<lower[i]:    trend[i]=False
        else:                                      trend[i]=trend[i-1]
    return pd.Series(trend, index=df.index)

def calculate_rsi(s, p=14):
    d=s.diff(); g=d.where(d>0,0).rolling(p).mean(); l=(-d.where(d<0,0)).rolling(p).mean()
    return 100-(100/(1+g/l))

def calculate_atr(df, period=14):
    """Average True Range — used only when USE_ATR_DYNAMIC=True."""
    tr = np.maximum(df['High']-df['Low'],
         np.maximum(abs(df['High']-df['Close'].shift(1)), abs(df['Low']-df['Close'].shift(1))))
    return tr.rolling(period).mean()

def estimate_premium_pts(entry_price, exit_price, signal, is_expiry_day):
    """Estimate option premium move (points) from the spot move using a
    moneyness-aware delta (gamma) instead of a flat 0.5 — see NIFTY backtest
    for full rationale. Identical model, % scale auto-adapts to SENSEX."""
    spot_move = (exit_price-entry_price) if signal=="BUY" else (entry_price-exit_price)
    delta_scale_pts = entry_price * DELTA_SCALE_PCT
    delta_exit = DELTA_BASE + (1-DELTA_BASE)*np.tanh(spot_move/delta_scale_pts)
    avg_delta = (DELTA_BASE+delta_exit)/2
    premium_pts = spot_move*avg_delta
    if is_expiry_day and premium_pts>0:
        premium_pts *= (1-EXPIRY_THETA_HAIRCUT_PCT)
    return premium_pts

def analyze_candle(o,h,l,c):
    body=abs(c-o); tr=h-l
    if tr==0: return True,False,False
    uw=h-max(o,c); lw=min(o,c)-l
    doji=(body/tr)<0.1
    return doji,(not doji and c>o and uw<=body),(not doji and c<o and lw<=body)

def get_prev_day_hl(df, date):
    prev = df[df.index.date < date]
    if prev.empty: return None, None
    last_date = prev.index.date[-1]
    day_data = prev[prev.index.date == last_date]
    return float(day_data['High'].max()), float(day_data['Low'].min())

def get_orb_for_day(df, date):
    td = df[df.index.date==date]
    if td.empty: return None,None
    orb = td[(td.index.time>=dtime(9,15))&(td.index.time<dtime(9,30))]
    if orb.empty: return None,None
    return float(orb['High'].max()),float(orb['Low'].min())

# ─────────────────────────────────────────
#   BACKTEST — identical logic to NIFTY, index-agnostic
# ─────────────────────────────────────────
def run_backtest(df5, df15, fut_vol, candle_sl=False, target_pts=None, morning_only=False, ce_only=False):
    mode  = "MORNING CE-ONLY" if (morning_only and ce_only) else ("MORNING" if morning_only else "REGULAR")
    sl_lbl  = f"Candle SL ({CANDLE_SL_BUFFER}pt)" if candle_sl else "Fixed %"
    tgt_lbl = f"{target_pts}pt" if target_pts else "Fixed %"
    print(f"Preparing indicators... [{mode} | SL: {sl_lbl} | Target: {tgt_lbl}]")
    print(f"  ATR-based dynamic risk: {'ON' if USE_ATR_DYNAMIC else 'OFF (fixed %, proven baseline)'}")
    df5 = df5.copy()
    df5['EMA9']       = ema(df5['Close'],9)
    df5['EMA20']      = ema(df5['Close'],20)
    df5['VWAP']       = calculate_vwap(df5)
    df5['Supertrend'] = calculate_supertrend(df5)
    df5['RSI']        = calculate_rsi(df5['Close'])
    df5['ATR14']      = calculate_atr(df5, ATR_PERIOD)

    if fut_vol is not None and fut_vol['Volume'].sum()>0:
        va = fut_vol['Volume'].reindex(df5.index, method='nearest', tolerance='5min')
        df5['Vol'] = va.fillna(df5['Volume'])
        print("  Volume: Real futures ✅")
    else:
        df5['Vol'] = df5['Volume']
        print("  Volume: Spot data")
    df5['AvgVol'] = df5['Vol'].rolling(20).mean()

    if df15 is not None and len(df15)>=25:
        df15=df15.copy()
        df15['E9']=ema(df15['Close'],9); df15['E20']=ema(df15['Close'],20)
        df15['T15']=df15['E9']>df15['E20']
        tmap={ts:bool(r['T15']) for ts,r in df15.iterrows()}
        def gt(ts):
            c=[t for t in tmap if t<=ts]
            return tmap[max(c)] if c else None
        df5['Trend15']=df5.index.map(gt)
        print("  15 min trend: ✅")
    else:
        df5['Trend15']=None

    df5=df5.dropna(subset=['EMA9','EMA20','VWAP','RSI','AvgVol','ATR14'])
    days=len(set(df5.index.date))
    print(f"  Candles: {len(df5)} | Days: {days}\n")

    trades=[]; daily_count={}; last_sig={}; orb_cache={}; pdhl_cache={}

    for i in range(20,len(df5)-1):
        row=df5.iloc[i]; prev=df5.iloc[i-1]
        ts=df5.index[i]; date=ts.date(); t=ts.time()

        atr = float(row['ATR14'])

        is_expiry_day = ts.weekday() == EXPIRY_WEEKDAY
        day_trade_end = EXPIRY_TRADE_END if is_expiry_day else TRADE_END
        day_vol_multi = EXPIRY_VOL_MULTI if is_expiry_day else VOL_MULTI
        day_ce_min_t2 = EXPIRY_CE_MIN_T2 if is_expiry_day else CE_MIN_T2
        day_momentum_candles = EXPIRY_MOMENTUM_CANDLES if is_expiry_day else MOMENTUM_CANDLES
        if is_expiry_day:
            day_breakout_buffer = (BUFFER_ATR_MULT * atr) if USE_ATR_DYNAMIC else EXPIRY_BREAKOUT_BUFFER
        else:
            day_breakout_buffer = 0
        day_momentum_min = (MOMENTUM_ATR_MULT * atr) if USE_ATR_DYNAMIC else MOMENTUM_MIN

        if t<TRADE_START or t>(dtime(11,0) if morning_only else day_trade_end): continue
        if daily_count.get(date,0)>=MAX_TRADES: continue

        price=float(row['Close'])
        o,h,l,c=float(row['Open']),float(row['High']),float(row['Low']),float(row['Close'])
        vwap=float(row['VWAP'])
        ema9=float(row['EMA9']); ema20=float(row['EMA20'])
        pe9=float(prev['EMA9']); pe20=float(prev['EMA20'])
        st=bool(row['Supertrend'])
        vol=float(row['Vol']); avg_vol=float(row['AvgVol'])
        ph=float(prev['High']); pl=float(prev['Low'])
        rsi=float(row['RSI'])
        trend15=row.get('Trend15',None)

        is_doji,bull_clean,bear_clean=analyze_candle(o,h,l,c)
        expiry_safe=not is_expiry_day

        if date not in orb_cache:
            orb_cache[date]=get_orb_for_day(df5,date)
        orb_high,orb_low=orb_cache[date]
        orb_bull=(orb_high is not None) and (price>orb_high)
        orb_bear=(orb_low  is not None) and (price<orb_low)

        vol_spike = vol>(avg_vol*day_vol_multi) if avg_vol>0 else False
        ema_gap = ema9 - ema20; prev_gap = pe9 - pe20
        cross_up   = (pe9<=pe20 and ema9>ema20) or (ema9>ema20 and ema_gap > prev_gap > 0)
        cross_down = (pe9>=pe20 and ema9<ema20) or (ema9<ema20 and ema_gap < prev_gap < 0)
        if USE_BREAKOUT_CONFIRM:
            ref_high = float(df5.iloc[i-2]['High']); ref_low = float(df5.iloc[i-2]['Low'])
            prev_close = float(prev['Close'])
            breakout  = (price>ref_high+day_breakout_buffer) and (prev_close>ref_high+day_breakout_buffer)
            breakdown = (price<ref_low -day_breakout_buffer) and (prev_close<ref_low -day_breakout_buffer)
        else:
            breakout  = price > ph + day_breakout_buffer
            breakdown = price < pl - day_breakout_buffer

        if morning_only:
            # 4-condition morning entry: VWAP + ST + Prev-candle breakout + Clean candle
            buy_ok  = all([price > vwap, st == True,  price > ph, bull_clean,  not is_doji])
            sell_ok = all([price < vwap, st == False, price < pl, bear_clean, not is_doji])
            if ce_only: sell_ok = False
            if not buy_ok and not sell_ok: continue
            signal = "BUY" if buy_ok else "SELL"
            if last_sig.get(date) == signal: continue
            conf = "NORMAL"; t2 = 4
            tgt_dist = target_pts if target_pts else (price * TARGET_PCT)
        else:
            buy_t1  = all([price>vwap, st==True,  cross_up,   vol_spike, breakout])
            sell_t1 = all([price<vwap, st==False, cross_down, vol_spike, breakdown])

            if not buy_t1 and not sell_t1: continue
            if is_doji: continue

            if date not in pdhl_cache:
                pdhl_cache[date] = get_prev_day_hl(df5, date)
            pdh, pdl = pdhl_cache[date]

            # Target distance — must match whichever mode (ATR or fixed%) is active,
            # so the S&R filter checks against the SAME distance we'll actually use.
            tgt_dist = (TARGET_ATR_MULT * atr) if USE_ATR_DYNAMIC else (price * TARGET_PCT)

            if buy_t1:
                if not (RSI_BUY_MIN<=rsi<=RSI_BUY_MAX): continue
                if pdh and 0 < (pdh - price) < tgt_dist: continue
                t2=sum([True, trend15==True, bull_clean, expiry_safe, orb_bull])
                if t2<day_ce_min_t2: continue
                if last_sig.get(date)=="BUY": continue
                signal="BUY"
                conf="HIGH" if t2==5 else ("NORMAL" if t2>=3 else "WEAK")
            else:
                if not (RSI_SELL_MIN<=rsi<=RSI_SELL_MAX): continue
                if pdl and 0 < (price - pdl) < tgt_dist: continue
                t2=sum([True, trend15==False, bear_clean, expiry_safe, orb_bear])
                if t2<PE_MIN_T2: continue
                if last_sig.get(date)=="SELL": continue
                signal="SELL"
                conf="HIGH" if t2==5 else ("NORMAL" if t2>=4 else "WEAK")

        if candle_sl:
            sl      = (pl - CANDLE_SL_BUFFER) if signal=="BUY" else (ph + CANDLE_SL_BUFFER)
            sl_dist = abs(price - sl)
            be_dist = price * BREAKEVEN_PCT
        elif USE_ATR_DYNAMIC:
            sl_dist = SL_ATR_MULT * atr
            be_dist = BE_ATR_MULT * atr
            sl      = price - sl_dist if signal=="BUY" else price + sl_dist
        else:
            sl_dist = price * SL_PCT
            be_dist = price * BREAKEVEN_PCT
            sl      = price - sl_dist if signal=="BUY" else price + sl_dist

        target = price + tgt_dist if signal=="BUY" else price - tgt_dist
        be_lvl = price + be_dist if signal=="BUY" else price - be_dist

        sl_pts  = round(abs(price - sl), 1)
        tgt_pts = round(abs(target - price), 1)

        trail_trigger_dist = be_dist * TRAIL_TRIGGER_MULT
        trail_step_dist    = be_dist * TRAIL_STEP_MULT

        current_sl = sl; outcome="EOD"; exit_price=price
        breakeven_hit = False; max_favorable = 0

        for j in range(i+1,len(df5)):
            ft=df5.index[j]; fd=ft.date(); ft_t=ft.time()
            if fd!=date or ft_t>HARD_EXIT:
                exit_price=float(df5.iloc[j]['Close']) if j<len(df5) else price
                outcome="EOD"; break
            fh=float(df5.iloc[j]['High']); fl=float(df5.iloc[j]['Low'])

            if signal=="BUY":
                max_favorable = max(max_favorable, fh - price)
            else:
                max_favorable = max(max_favorable, price - fl)

            if j - i == day_momentum_candles and max_favorable < day_momentum_min:
                fc=float(df5.iloc[j]['Close'])
                exit_price=fc; outcome="WEAK"; break

            if not breakeven_hit:
                if signal=="BUY" and fh>=be_lvl:
                    current_sl=price; breakeven_hit=True
                elif signal=="SELL" and fl<=be_lvl:
                    current_sl=price; breakeven_hit=True

            # Trailing stop — once price extends well past breakeven, ratchet
            # SL behind the peak instead of leaving it flat at breakeven.
            if max_favorable >= trail_trigger_dist:
                trail_dist = max_favorable - trail_step_dist
                if signal=="BUY":
                    current_sl = max(current_sl, price + trail_dist)
                else:
                    current_sl = min(current_sl, price - trail_dist)
                breakeven_hit = True

            if signal=="BUY" and fh>=target:
                exit_price=target; outcome="TARGET"; break
            if signal=="SELL" and fl<=target:
                exit_price=target; outcome="TARGET"; break

            if signal=="BUY" and fl<=current_sl:
                exit_price=current_sl
                if current_sl>price: outcome="TRAIL"
                elif breakeven_hit: outcome="BE"
                else: outcome="SL"
                break
            if signal=="SELL" and fh>=current_sl:
                exit_price=current_sl
                if current_sl<price: outcome="TRAIL"
                elif breakeven_hit: outcome="BE"
                else: outcome="SL"
                break

        pnl_pct=(exit_price-price)/price if signal=="BUY" else (price-exit_price)/price
        premium_pts = estimate_premium_pts(price, exit_price, signal, is_expiry_day)
        pnl_rs = round(premium_pts*LOT_SIZE, 0)

        trades.append({
            "date":str(date),"time":t.strftime("%H:%M"),
            "signal":signal,"confidence":conf,"t2":t2,
            "entry":round(price,2),"sl":round(sl,2),"target":round(target,2),
            "sl_pts":sl_pts,"tgt_pts":tgt_pts,
            "exit":round(exit_price,2),"outcome":outcome,
            "max_favorable":round(max_favorable,1),
            "pnl_pct":round(pnl_pct*100,2),
            "premium_pts":round(premium_pts,1),"pnl_rs":pnl_rs,
            "rsi":round(rsi,1),
            "ema9":round(ema9,1),"ema20":round(ema20,1),
            "vwap":round(vwap,1),"supertrend":"Green" if st else "Red",
            "vol_ratio":round(vol/avg_vol,1) if avg_vol>0 else 0,
            "day":ts.strftime("%A")
        })
        daily_count[date]=daily_count.get(date,0)+1
        last_sig[date]=signal

    return trades

# ─────────────────────────────────────────
#   REPORT
# ─────────────────────────────────────────
def print_report(trades):
    if not trades:
        print("\n❌ No trades found.")
        return

    tdf=pd.DataFrame(trades)
    total=len(tdf)
    win_outcomes=['TARGET','TRAIL']
    wins=len(tdf[tdf['outcome'].isin(win_outcomes)])
    trails=len(tdf[tdf['outcome']=='TRAIL'])
    targets=len(tdf[tdf['outcome']=='TARGET'])
    loss=len(tdf[tdf['outcome']=='SL'])
    bes=len(tdf[tdf['outcome']=='BE'])
    weaks=len(tdf[tdf['outcome']=='WEAK'])
    eods=len(tdf[tdf['outcome']=='EOD'])
    wr=wins/total*100; net=tdf['pnl_rs'].sum()
    aw=tdf[tdf['outcome'].isin(win_outcomes)]['pnl_rs'].mean() if wins>0 else 0
    al=tdf[tdf['outcome']=='SL']['pnl_rs'].mean() if loss>0 else 0

    mws=mls=cw=cl=0
    for o in tdf['outcome']:
        if o in win_outcomes: cw+=1;cl=0;mws=max(mws,cw)
        elif o=='SL':         cl+=1;cw=0;mls=max(mls,cl)
        else:                 cw=0;cl=0

    bdf=tdf[tdf['signal']=='BUY']; sdf=tdf[tdf['signal']=='SELL']
    bwr=len(bdf[bdf['outcome'].isin(win_outcomes)])/len(bdf)*100 if len(bdf) else 0
    swr=len(sdf[sdf['outcome'].isin(win_outcomes)])/len(sdf)*100 if len(sdf) else 0
    eod_pos=len(tdf[(tdf['outcome']=='EOD')&(tdf['pnl_rs']>0)])

    high_df=tdf[tdf['confidence']=='HIGH']
    norm_df=tdf[tdf['confidence']=='NORMAL']
    high_wr=len(high_df[high_df['outcome'].isin(win_outcomes)])/len(high_df)*100 if len(high_df) else 0
    norm_wr=len(norm_df[norm_df['outcome'].isin(win_outcomes)])/len(norm_df)*100 if len(norm_df) else 0

    days=len(set(tdf['date']))
    sep="="*65
    print(f"\n{sep}")
    print(f"  SENSEX BACKTEST — Target ~{round(tdf['tgt_pts'].mean())}pts | SL ~{round(tdf['sl_pts'].mean())}pts")
    print(f"  Same proven NIFTY engine | Expiry: Thursday | Trailing stop")
    print(f"  Risk mode: {'ATR-DYNAMIC (experimental)' if USE_ATR_DYNAMIC else 'FIXED % (proven baseline)'}")
    print(f"  P&L mode: Estimated option premium (delta+theta model), LOT_SIZE={LOT_SIZE}")
    print(f"  Breakout mode: {'2-CANDLE CONFIRM (experimental)' if USE_BREAKOUT_CONFIRM else '1-CANDLE (proven baseline)'}")
    print(sep)
    print(f"""
📊 OVERALL:
  Total Trades    : {total} over {days} days ({total/days:.1f}/day)
  Wins (Target+Trail): {wins} ({wr:.1f}%)  [Target: {targets}, Trail: {trails}]
  Losses (SL)     : {loss} ({loss/total*100:.1f}%)
  Breakeven       : {bes} ({bes/total*100:.1f}%)
  Weak Exit       : {weaks} ({weaks/total*100:.1f}%)
  EOD Exits       : {eods} ({eods/total*100:.1f}%)
    → Profitable  : {eod_pos}
    → Loss/Flat   : {eods-eod_pos}
  Net PnL         : ₹{net:,.0f}
  Avg Win         : ₹{aw:,.0f}
  Avg Loss        : ₹{al:,.0f}
  Max Win Streak  : {mws}
  Max Loss Streak : {mls}
""")
    print(f"📉 CE vs PE:")
    print(f"  CE (BUY)  : {len(bdf)} trades | Win: {bwr:.1f}% | PnL: ₹{bdf['pnl_rs'].sum():,.0f}")
    print(f"  PE (SELL) : {len(sdf)} trades | Win: {swr:.1f}% | PnL: ₹{sdf['pnl_rs'].sum():,.0f}")

    print(f"\n🔥 CONFIDENCE:")
    print(f"  HIGH (5/5) : {len(high_df)} trades | Win: {high_wr:.1f}% | PnL: ₹{high_df['pnl_rs'].sum():,.0f}")
    print(f"  NORMAL     : {len(norm_df)} trades | Win: {norm_wr:.1f}% | PnL: ₹{norm_df['pnl_rs'].sum():,.0f}")

    print(f"\n📅 DAY WISE:")
    dpnl=tdf.groupby('day')['pnl_rs'].sum()
    for d in ['Monday','Tuesday','Wednesday','Thursday','Friday']:
        if d in dpnl.index:
            p=dpnl[d]; e="🟢" if p>0 else "🔴"
            dc=len(tdf[tdf['day']==d])
            print(f"  {e} {d:<12}: ₹{p:,.0f} ({dc} trades)")

    if 'max_favorable' in tdf.columns:
        sl_trades = tdf[tdf['outcome']=='SL']
        if len(sl_trades) > 0:
            print(f"\n📊 SL TRADE ANALYSIS (did price go our way first?):")
            for _,r in sl_trades.iterrows():
                went = r['max_favorable']
                print(f"  {r['date']} {r['signal']} entry:{r['entry']} → went +{went:.0f}pts in favor → then SL")

    print(f"\n📋 ALL TRADES:")
    print(f"{'Date':<12}{'Time':<7}{'Sig':<6}{'Conf':<7}{'Entry':<10}{'SL':<6}{'TGT':<6}{'Exit':<10}{'MaxFav':<8}{'PnL₹':<8}{'Result':<6}{'RSI':<6}VolR")
    print("-"*100)
    for _,r in tdf.iterrows():
        icon="✅" if r['outcome']=='TARGET' else ("🔒" if r['outcome']=='TRAIL' else ("❌" if r['outcome']=='SL' else ("⚖️" if r['outcome']=='BE' else ("⚠️" if r['outcome']=='WEAK' else "➡️"))))
        print(f"{r['date']:<12}{r['time']:<7}{r['signal']:<6}{r['confidence']:<7}"
              f"{r['entry']:<10}{r['sl_pts']:<6}{r['tgt_pts']:<6}{r['exit']:<10}"
              f"+{r['max_favorable']:<7}{r['pnl_rs']:<8}"
              f"{icon}{r['outcome']:<5}{r['rsi']:<6}{r['vol_ratio']}")

    print(f"\n{sep}")
    print("  VERDICT")
    print(sep)
    if wr>=55 and net>0:
        print("  ✅ PROFITABLE — Ready for paper trading")
    elif wr>=45 and net>0:
        print("  ⚡ MARGINAL — Paper trade 2 weeks first")
    elif net>0:
        print("  ⚡ POSITIVE but low win% — monitor")
    else:
        print("  ❌ Needs work")

    if bwr>swr+15 and len(sdf)>0:
        print(f"  💡 CE better ({bwr:.0f}% vs {swr:.0f}%)")
    if mls>=3:
        print(f"  ⚠️  Stop after 3 consecutive losses")

    print(sep)
    out_file = 'backtest_results_sensex_atr.csv' if USE_ATR_DYNAMIC else 'backtest_results_sensex.csv'
    tdf.to_csv(out_file,index=False)
    print(f"\n  📁 Saved → {out_file}")
    print(sep)

# ─────────────────────────────────────────
#   MAIN
# ─────────────────────────────────────────
def main():
    print("="*65)
    print("  SENSEX BACKTESTER — Morning window CE-only (candle SL + target)")
    print("  Same strategy as NIFTY morning | VWAP+ST+Breakout+Clean | 9:30-11:00")
    print("="*65)

    if not login(): return

    print("📥 Finding SENSEX index token...")
    SENSEX_TOKEN = find_sensex_token()
    if not SENSEX_TOKEN:
        print("❌ Could not find SENSEX token — aborting")
        return

    DAYS = 60
    print(f"📥 Fetching Sensex 5 min data ({DAYS} days)...")
    df5=fetch_data(SENSEX_TOKEN,"5minute",days=DAYS,label="[5min spot]")
    if df5 is None or df5.empty:
        print("❌ Failed"); return
    print(f"✅ {len(df5)} candles | {df5.index[0].date()} to {df5.index[-1].date()}")

    print("📥 Fetching 15 min data...")
    df15=fetch_data(SENSEX_TOKEN,"15minute",days=DAYS,label="[15min spot]")
    print(f"✅ {len(df15)} candles (15 min)" if df15 is not None else "⚠️ Not available")

    print("📥 Finding Sensex Futures for volume...")
    fut_token=find_sensex_fut_token()
    fut_vol=None
    if fut_token:
        fut_vol=fetch_data(fut_token,"5minute",days=DAYS,label="[5min futures]")
        if fut_vol is not None and fut_vol['Volume'].sum()>0:
            print(f"✅ Futures volume: {len(fut_vol)} candles")
        else:
            print("⚠️ Futures volume empty")
            fut_vol=None
    else:
        print("⚠️ No futures token — backtest will use spot volume")

    # ── Run all variants ──
    print("\n[1/4] Regular window baseline (fixed SL, CE+PE, 9:30-14:00)...")
    t_base = run_backtest(df5, df15, fut_vol)

    print("\n[2/4] Morning CE-only, Candle SL + 50pt target (9:30-11:00)...")
    t_50 = run_backtest(df5, df15, fut_vol, candle_sl=True, target_pts=50, morning_only=True, ce_only=True)

    print("\n[3/4] Morning CE-only, Candle SL + 75pt target (9:30-11:00)...")
    t_75 = run_backtest(df5, df15, fut_vol, candle_sl=True, target_pts=75, morning_only=True, ce_only=True)

    print("\n[4/4] Morning CE-only, Candle SL + 100pt target (9:30-11:00)...")
    t_100 = run_backtest(df5, df15, fut_vol, candle_sl=True, target_pts=100, morning_only=True, ce_only=True)

    # ── Comparison table ──
    def var_stats(trades_list):
        if not trades_list: return 0, 0.0, 0, 0.0, 0
        tdf = pd.DataFrame(trades_list)
        wins = len(tdf[tdf['outcome'].isin(['TARGET','TRAIL'])])
        sls  = len(tdf[tdf['outcome']=='SL'])
        wr   = wins / len(tdf) * 100
        net  = int(tdf['pnl_rs'].sum())
        avg_sl = round(tdf['sl_pts'].mean(), 1)
        return len(tdf), wr, sls, avg_sl, net

    variants = [
        ("Fixed SL  + orig target (regular 9:30-14:00)", t_base),
        ("Candle SL + 50pt target (morning CE-only)",    t_50),
        ("Candle SL + 75pt target (morning CE-only)",    t_75),
        ("Candle SL + 100pt target (morning CE-only)",   t_100),
    ]
    all_pnl = [var_stats(t)[4] for _, t in variants]
    best_pnl = max(all_pnl) if any(p > 0 for p in all_pnl) else None

    SEP = "="*80
    print(f"\n{SEP}")
    print("  SENSEX — MORNING CANDLE SL vs REGULAR BASELINE")
    print(f"  NIFTY morning uses 25pt target; SENSEX scaled ~3x (80k vs 24k spot)")
    print(SEP)
    print(f"  {'Variant':<47} {'Trades':>6} {'Win%':>5} {'SLs':>4} {'AvgSL':>7} {'Net P&L':>10}")
    print(f"  {'-'*77}")
    for label, t in variants:
        n, wr, sls, avg_sl, net = var_stats(t)
        star = " ★" if net == best_pnl else ""
        print(f"  {label:<47} {n:>6} {wr:>4.1f}% {sls:>4} {avg_sl:>6.1f}pt  Rs{net:>8,}{star}")
    print(SEP)

    # ── Detailed report for best morning variant ──
    morning_variants = [(t_50,50), (t_75,75), (t_100,100)]
    best = max(morning_variants, key=lambda x: pd.DataFrame(x[0])['pnl_rs'].sum() if x[0] else 0)
    best_trades, best_tgt = best
    if best_trades:
        print(f"\n{'='*55}")
        print(f"  DETAILED: Morning CE-only, Candle SL + {best_tgt}pt target")
        print(f"{'='*55}")
        print_report(best_trades)

if __name__=="__main__":
    main()
