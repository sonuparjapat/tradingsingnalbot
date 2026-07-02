"""
=============================================================
NIFTY MORNING BACKTEST — 9:30-11:00 Window
=============================================================
Backtests the morning scanner's signal set.

Strategy A (ATM): VWAP + Supertrend + Breakout + Clean candle
  - ATM options, delta ~0.5

Strategy B (ITM): Same 4 conditions + RSI confirmation
  - Slightly ITM options: CE = ATM-50, PE = ATM+50
  - Higher delta ~0.62 → more premium per index point
  - RSI >= 55 for BUY, RSI <= 45 for SELL
  - Only enters when momentum is already confirmed

Both are run and compared in one command.
=============================================================
"""

import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from kiteconnect import KiteConnect
import pandas as pd
import numpy as np
import os, json, time, webbrowser
from datetime import datetime, timedelta, time as dtime
from dotenv import load_dotenv
from urllib.parse import urlparse, parse_qs
import requests
import pyotp
import warnings
warnings.filterwarnings('ignore')

load_dotenv()

API_KEY          = os.getenv("API_KEY")
API_SECRET       = os.getenv("API_SECRET")
KITE_USER_ID     = os.getenv("KITE_USER_ID")
KITE_PASSWORD    = os.getenv("KITE_PASSWORD")
KITE_TOTP_SECRET = os.getenv("KITE_TOTP_SECRET")
LOT_SIZE         = int(os.getenv("LOT_SIZE", "75"))

NIFTY_TOKEN = 256265

# Entry window — same as morning scanner
ENTRY_START    = dtime(9, 30)
ENTRY_END      = dtime(11, 0)
HARD_EXIT      = dtime(15, 10)
EXPIRY_WEEKDAY = 1   # Tuesday

# SL / Target / Breakeven — identical to morning scanner + main bot
SL_PCT        = 0.00063
TARGET_PCT    = 0.00071
BREAKEVEN_PCT = 0.00034
MOMENTUM_MIN  = 5
MOMENTUM_CANDLES = 3   # 15 min

# Candle-structure SL: a few pts below prev candle low (CE) / above prev candle high (PE)
CANDLE_SL_BUFFER = 5    # points of buffer beyond prev candle edge

# Premium P&L model
DELTA_SCALE_PCT          = 0.0048
EXPIRY_THETA_HAIRCUT_PCT = 0.15
STRIKE_GAP               = 50    # NIFTY option strike spacing

# Strategy modes
DELTA_ATM = 0.50   # ATM options: delta ~0.5
DELTA_ITM = 0.62   # Slightly ITM (1 strike): delta ~0.62

# RSI confirmation thresholds for ITM mode
RSI_BUY_MIN  = 55   # BUY only when RSI confirms bullish momentum
RSI_SELL_MAX = 45   # SELL only when RSI confirms bearish momentum

# ITM-mode extra filters (missing checks that cause losses)
OR_WINDOW_START   = dtime(9, 30)  # Opening Range window starts
OR_WINDOW_END     = dtime(9, 44)  # Opening Range window ends (first 3 candles)
ENTRY_SKIP_UNTIL  = dtime(9, 45)  # only trade AFTER OR is established
MIN_BREAKOUT_PTS  = 3             # price must clear OR high/low by ≥3 pts (real breakout)

# Trailing stop
TRAIL_TRIGGER_MULT = 1.5
TRAIL_STEP_MULT_ATM = 0.6        # ATM mode — original
TRAIL_STEP_MULT_ITM = 0.35       # ITM mode — tighter, keeps more profit on trail exits

# ─── LOGIN ───
kite = KiteConnect(api_key=API_KEY)
TOKEN_FILE = "kite_token.json"

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
        print("✅ Reused cached token\n")
        return True
    except:
        return False

def save_cached_token(access_token):
    try:
        with open(TOKEN_FILE, "w") as f:
            json.dump({"access_token": access_token, "date": datetime.now().strftime("%Y-%m-%d")}, f)
    except: pass

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
        resp = sess.post("https://kite.zerodha.com/api/login",
                         data={"user_id": KITE_USER_ID, "password": KITE_PASSWORD})
        data = resp.json()
        if data.get("status") != "success": return False
        request_id = data["data"]["request_id"]
        totp = pyotp.TOTP(KITE_TOTP_SECRET).now()
        resp = sess.post("https://kite.zerodha.com/api/twofa", data={
            "user_id": KITE_USER_ID, "request_id": request_id,
            "twofa_value": totp, "twofa_type": "totp"})
        if resp.json().get("status") != "success": return False
        time.sleep(1)
        redirect_url = ""; next_url = kite.login_url()
        for _ in range(3):
            resp = sess.get(next_url, allow_redirects=False)
            if resp.status_code in (301,302,303,307,308):
                next_url = resp.headers.get("Location","")
                if next_url.startswith("/"): next_url = "https://kite.zerodha.com" + next_url
                redirect_url = next_url
                if "request_token=" in next_url: break
            else:
                redirect_url = resp.url; break
        parsed = parse_qs(urlparse(redirect_url).query)
        request_token = parsed.get("request_token",[None])[0]
        if not request_token: return False
        session_data = kite.generate_session(request_token, api_secret=API_SECRET)
        kite.set_access_token(session_data["access_token"])
        save_cached_token(session_data["access_token"])
        print("✅ Auto-login successful!\n"); return True
    except: return False

def manual_login():
    login_url = kite.login_url()
    print(f"\n🌐 Opening Zerodha login...")
    webbrowser.open(login_url)
    request_token = input("\nPaste request_token here: ").strip()
    try:
        data = kite.generate_session(request_token, api_secret=API_SECRET)
        kite.set_access_token(data["access_token"])
        save_cached_token(data["access_token"])
        print("✅ Login successful!\n"); return True
    except Exception as e:
        print(f"❌ Login failed: {e}"); return False

def login():
    if load_cached_token(): return True
    if KITE_USER_ID and KITE_PASSWORD and KITE_TOTP_SECRET and KITE_USER_ID != "YOUR_USER_ID":
        if auto_login(): return True
    return manual_login()

# ─── DATA ───
def fetch_data(token, interval, days):
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
        print(f"  Fetch error: {e}"); return None

# ─── INDICATORS ───
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
               np.maximum(abs(df['High']-df['Close'].shift(1)),
                          abs(df['Low']-df['Close'].shift(1))))
    df['ATR'] = df['TR'].rolling(period).mean()
    upper = (hl2+multiplier*df['ATR']).values.copy()
    lower = (hl2-multiplier*df['ATR']).values.copy()
    trend = [True]*len(df); close = df['Close'].values
    for i in range(1, len(df)):
        lower[i] = max(lower[i],lower[i-1]) if close[i-1]>lower[i-1] else lower[i]
        upper[i] = min(upper[i],upper[i-1]) if close[i-1]<upper[i-1] else upper[i]
        if   not trend[i-1] and close[i]>upper[i]: trend[i]=True
        elif trend[i-1] and close[i]<lower[i]:     trend[i]=False
        else:                                       trend[i]=trend[i-1]
    return pd.Series(trend, index=df.index)

def calculate_rsi(s, p=14):
    d=s.diff(); g=d.where(d>0,0).rolling(p).mean(); l=(-d.where(d<0,0)).rolling(p).mean()
    return 100-(100/(1+g/l))

def analyze_candle(o,h,l,c):
    body=abs(c-o); tr=h-l
    if tr==0: return True,False,False
    uw=h-max(o,c); lw=min(o,c)-l
    doji=(body/tr)<0.1
    return doji,(not doji and c>o and uw<=body),(not doji and c<o and lw<=body)

def get_strike_itm(price, signal):
    """Return 1-strike ITM option: CE = ATM-50, PE = ATM+50"""
    atm = round(price / STRIKE_GAP) * STRIKE_GAP
    return f"{atm - STRIKE_GAP} CE" if signal == "BUY" else f"{atm + STRIKE_GAP} PE"

def build_trend15(df15):
    """Build 15-min EMA9/EMA20 trend lookup — same as evening backtest"""
    df15 = df15.copy()
    df15['EMA9']  = ema(df15['Close'], 9)
    df15['EMA20'] = ema(df15['Close'], 20)
    return df15[['EMA9', 'EMA20']].dropna()

def get_trend15_at(trend15, ts):
    """Return True=bullish / False=bearish / None=no data for 15-min trend at ts"""
    past = trend15[trend15.index <= ts]
    if past.empty: return None
    row = past.iloc[-1]
    return bool(row['EMA9'] > row['EMA20'])

def estimate_premium_pts(entry_price, exit_price, signal, is_expiry_day, delta_base=DELTA_ATM):
    spot_move = (exit_price-entry_price) if signal=="BUY" else (entry_price-exit_price)
    delta_scale_pts = entry_price * DELTA_SCALE_PCT
    delta_exit = delta_base + (1-delta_base)*np.tanh(spot_move/delta_scale_pts)
    avg_delta  = (delta_base+delta_exit)/2
    premium_pts = spot_move*avg_delta
    if is_expiry_day and premium_pts > 0:
        premium_pts *= (1-EXPIRY_THETA_HAIRCUT_PCT)
    return premium_pts

# ─── BACKTEST ───
def run_backtest(df5, df15=None, days=60, mode="ATM", ce_only=False, candle_sl=False,
                 target_pts=None, candle_be_trigger_pts=None):
    """
    mode='ATM'      : 4-condition, delta 0.5, entry 9:30 (CE+PE)
    mode='SOLID'    : 5-condition, delta 0.5, entry 9:35, CE-only
                      VWAP + ST + EMA9>EMA20 + Breakout(+3pt) + Bull clean
    mode='SOLID_PE' : CE=5 cond + PE=6 cond (adds 15-min bearish for PE)
                      PE only fires on genuinely bearish mornings
    mode='ITM'      : 7-condition, delta 0.62, ORB entry 9:45 (CE+PE)
    ce_only=True    : BUY (CE) signals only — auto-set in SOLID mode
    """
    delta_base      = DELTA_ITM if mode == "ITM" else DELTA_ATM
    use_rsi         = (mode == "ITM")
    is_solid        = (mode == "SOLID")
    is_solid_pe     = (mode == "SOLID_PE")
    if is_solid: ce_only = True          # SOLID is always CE-only
    trail_step_mult = TRAIL_STEP_MULT_ITM if mode == "ITM" else TRAIL_STEP_MULT_ATM
    trend15         = build_trend15(df15) if (df15 is not None and is_solid_pe) else None

    df5 = df5.copy()
    df5['VWAP']       = calculate_vwap(df5)
    df5['Supertrend'] = calculate_supertrend(df5)
    df5['RSI']        = calculate_rsi(df5['Close'])
    df5['EMA9']       = ema(df5['Close'], 9)
    df5['EMA20']      = ema(df5['Close'], 20)
    df5 = df5.dropna(subset=['VWAP', 'RSI', 'EMA20'])

    # Pre-compute Opening Range per day (9:30-9:44, first 3 candles)
    # ORB: the high/low of the first 15 min defines the day's initial range.
    # A break of OR high = market has committed to going up.
    # A break of OR low  = market has committed to going down.
    opening_ranges = {}
    for d in set(df5.index.date):
        or_data = df5[(df5.index.date == d) &
                      (df5.index.time >= OR_WINDOW_START) &
                      (df5.index.time <= OR_WINDOW_END)]
        if len(or_data) > 0:
            opening_ranges[d] = (or_data['High'].max(), or_data['Low'].min())

    trades = []; last_sig = {}

    for i in range(20, len(df5)-1):
        row = df5.iloc[i]; prev = df5.iloc[i-1]
        ts  = df5.index[i]; date = ts.date(); t = ts.time()

        if t < ENTRY_START or t > ENTRY_END: continue
        if ts.weekday() == EXPIRY_WEEKDAY: continue

        # ITM mode: only enter AFTER Opening Range is established (9:45+)
        if use_rsi and t < ENTRY_SKIP_UNTIL: continue
        # SOLID / SOLID_PE: skip first candle (9:30 opening volatility is highest)
        if (is_solid or is_solid_pe) and t < dtime(9, 35): continue

        is_expiry = (ts.weekday() == EXPIRY_WEEKDAY)

        price = float(row['Close'])
        o,h,l,c = float(row['Open']),float(row['High']),float(row['Low']),float(row['Close'])
        vwap  = float(row['VWAP'])
        st    = bool(row['Supertrend'])
        rsi   = float(row['RSI'])
        ema9  = float(row['EMA9'])
        ema20 = float(row['EMA20'])
        ph    = float(prev['High']); pl = float(prev['Low'])

        is_doji, bull_clean, bear_clean = analyze_candle(o,h,l,c)
        if is_doji: continue

        if is_solid:
            # SOLID: 5 tight conditions — VWAP + ST + EMA9>EMA20 + Strong breakout (+3pt) + Bull clean
            # CE-only, entry from 9:35 (skip 9:30 opening volatility)
            breakout = price > ph + MIN_BREAKOUT_PTS
            ema_bull  = ema9 > ema20
            buy_ok  = all([price > vwap, st == True, breakout, bull_clean, ema_bull])
            sell_ok = False  # CE-only
        elif is_solid_pe:
            # SOLID_PE: CE gets same 5 SOLID conditions
            # PE gets 6 conditions — same SOLID standards + 15-min bearish confirmation
            # 15-min bearish = morning genuinely trending down, not just a 5-min dip
            breakout  = price > ph + MIN_BREAKOUT_PTS
            breakdown = price < pl - MIN_BREAKOUT_PTS
            ema_bull  = ema9 > ema20
            ema_bear  = ema9 < ema20
            t15_bull  = get_trend15_at(trend15, ts) if trend15 is not None else True
            buy_ok  = all([price > vwap, st == True,  breakout,  bull_clean, ema_bull])
            sell_ok = all([price < vwap, st == False, breakdown, bear_clean, ema_bear,
                           t15_bull == False])  # PE only when 15-min also bearish
        elif use_rsi:
            # ITM: use Opening Range high/low as breakout level (NOT just prev candle)
            or_high, or_low = opening_ranges.get(date, (ph, pl))
            breakout  = price > or_high + MIN_BREAKOUT_PTS
            breakdown = price < or_low  - MIN_BREAKOUT_PTS
            ema_bull = ema9 > ema20
            ema_bear = ema9 < ema20
            buy_ok  = all([price>vwap, st==True,  breakout,  bull_clean, rsi >= RSI_BUY_MIN,  ema_bull])
            sell_ok = all([price<vwap, st==False, breakdown, bear_clean, rsi <= RSI_SELL_MAX, ema_bear])
        else:
            # ATM: 4 conditions (original, CE+PE)
            breakout  = price > ph
            breakdown = price < pl
            buy_ok  = all([price>vwap, st==True,  breakout,  bull_clean])
            sell_ok = all([price<vwap, st==False, breakdown, bear_clean])

        if buy_ok and last_sig.get(date) != "BUY":
            signal = "BUY"
        elif sell_ok and not ce_only and last_sig.get(date) != "SELL":
            signal = "SELL"
        else:
            continue

        last_sig[date] = signal

        if candle_sl:
            sl      = (pl - CANDLE_SL_BUFFER) if signal=="BUY" else (ph + CANDLE_SL_BUFFER)
            sl_dist = abs(price - sl)
        else:
            sl_dist = price * SL_PCT
            sl      = price - sl_dist if signal=="BUY" else price + sl_dist

        tgt_dist = target_pts if target_pts is not None else price * TARGET_PCT
        be_dist  = price * BREAKEVEN_PCT
        target   = price + tgt_dist if signal=="BUY" else price - tgt_dist

        trail_trigger_dist = be_dist * TRAIL_TRIGGER_MULT
        trail_step_dist    = be_dist * trail_step_mult

        current_sl = sl; outcome = "EOD"; exit_price = price
        breakeven_hit = False; max_favorable = 0

        for j in range(i+1, len(df5)):
            ft = df5.index[j]; ft_t = ft.time()
            if ft.date() != date or ft_t > HARD_EXIT:
                exit_price = float(df5.iloc[j]['Close']) if j < len(df5) else price
                outcome = "EOD"; break

            fh = float(df5.iloc[j]['High']); fl = float(df5.iloc[j]['Low'])
            max_favorable = max(max_favorable, fh-price if signal=="BUY" else price-fl)

            if j-i == MOMENTUM_CANDLES and max_favorable < MOMENTUM_MIN:
                exit_price = float(df5.iloc[j]['Close']); outcome = "WEAK"; break

            if not breakeven_hit:
                if candle_be_trigger_pts is not None:
                    # Candle-B BE: only trigger on the FIRST candle after entry (candle B)
                    # if it moves N pts from its open — confirms direction before protecting entry
                    if j == i + 1:
                        b_open = float(df5.iloc[j]['Open'])
                        if signal=="BUY"  and fh >= b_open + candle_be_trigger_pts:
                            current_sl = price; breakeven_hit = True
                        elif signal=="SELL" and fl <= b_open - candle_be_trigger_pts:
                            current_sl = price; breakeven_hit = True
                    # j > i+1: candle-B window passed, no BE — trailing stop handles it
                else:
                    # Original fixed % BE (~8pt, fires even mid entry-candle)
                    if signal=="BUY"  and fh >= price + be_dist: current_sl=price; breakeven_hit=True
                    if signal=="SELL" and fl <= price - be_dist: current_sl=price; breakeven_hit=True

            if max_favorable >= trail_trigger_dist:
                trail_dist = max_favorable - trail_step_dist
                if signal=="BUY":  current_sl = max(current_sl, price + trail_dist)
                else:              current_sl = min(current_sl, price - trail_dist)
                breakeven_hit = True

            if signal=="BUY"  and fh >= target: exit_price=target; outcome="TARGET"; break
            if signal=="SELL" and fl <= target:  exit_price=target; outcome="TARGET"; break

            if signal=="BUY"  and fl <= current_sl:
                exit_price=current_sl
                outcome = "TRAIL" if current_sl>price else ("BE" if breakeven_hit else "SL"); break
            if signal=="SELL" and fh >= current_sl:
                exit_price=current_sl
                outcome = "TRAIL" if current_sl<price else ("BE" if breakeven_hit else "SL"); break

        premium_pts = estimate_premium_pts(price, exit_price, signal, is_expiry, delta_base)
        pnl_rs      = round(premium_pts * LOT_SIZE, 0)

        trade = {
            "date":    str(date),
            "time":    t.strftime("%H:%M"),
            "day":     ts.strftime("%A"),
            "signal":  signal,
            "entry":   round(price, 2),
            "rsi":     round(rsi, 1),
            "sl_pts":  round(sl_dist, 1),
            "prev_low": round(pl, 1),
            "tgt_pts": round(tgt_dist, 1),
            "exit":    round(exit_price, 2),
            "outcome": outcome,
            "max_fav": round(max_favorable, 1),
            "premium_pts": round(premium_pts, 1),
            "pnl_rs":  pnl_rs,
            "expiry":  is_expiry,
        }
        if mode == "ITM":
            trade["strike"] = get_strike_itm(price, signal)
        trades.append(trade)

    return trades

# ─── REPORT ───
def print_report(trades, days, mode="ATM", ce_only=False, candle_sl=False, target_pts=None):
    if not trades:
        print(f"\n❌ No morning signals found ({mode} mode)."); return

    tdf = pd.DataFrame(trades)
    total = len(tdf)
    wins  = len(tdf[tdf['outcome'].isin(['TARGET','TRAIL'])])
    trails  = len(tdf[tdf['outcome']=='TRAIL'])
    targets = len(tdf[tdf['outcome']=='TARGET'])
    loss  = len(tdf[tdf['outcome']=='SL'])
    bes   = len(tdf[tdf['outcome']=='BE'])
    weaks = len(tdf[tdf['outcome']=='WEAK'])
    eods  = len(tdf[tdf['outcome']=='EOD'])
    wr    = wins/total*100; net = tdf['pnl_rs'].sum()
    aw    = tdf[tdf['outcome'].isin(['TARGET','TRAIL'])]['pnl_rs'].mean() if wins>0 else 0
    al    = tdf[tdf['outcome']=='SL']['pnl_rs'].mean() if loss>0 else 0
    avg_sl_pts = tdf['sl_pts'].mean() if 'sl_pts' in tdf.columns else 0

    mws=mls=cw=cl=0
    for o in tdf['outcome']:
        if o in ('TARGET','TRAIL'): cw+=1;cl=0;mws=max(mws,cw)
        elif o=='SL':               cl+=1;cw=0;mls=max(mls,cl)
        else:                       cw=0;cl=0

    bdf=tdf[tdf['signal']=='BUY']; sdf=tdf[tdf['signal']=='SELL']
    bwr=len(bdf[bdf['outcome'].isin(['TARGET','TRAIL'])])/len(bdf)*100 if len(bdf) else 0
    swr=len(sdf[sdf['outcome'].isin(['TARGET','TRAIL'])])/len(sdf)*100 if len(sdf) else 0

    days_traded = len(set(tdf['date']))
    sep = "="*70

    if mode == "SOLID":
        title   = "NIFTY MORNING BACKTEST — 09:35-11:00  [SOLID — CE-only, 5 CONDITIONS]"
        cond    = "VWAP + ST + EMA9>EMA20 + Breakout(+3pt) + Bull clean | CE-only | Skip 9:30"
        delta_s = f"ATM | Delta ~{DELTA_ATM} | Strong breakout only (+{MIN_BREAKOUT_PTS}pt)"
    elif mode == "SOLID_PE":
        title   = "NIFTY MORNING BACKTEST — 09:35-11:00  [SOLID+PE — CE+PE, PROPER ANALYSIS]"
        cond    = ("CE: VWAP+ST+EMA9>EMA20+Breakout(+3pt)+Clean  |  "
                   "PE: same+15min bearish (genuine bearish morning only)")
        delta_s = f"ATM | Delta ~{DELTA_ATM} | CE=5 cond, PE=6 cond | PE fires only on bearish mornings"
    elif mode == "ITM":
        title   = "NIFTY MORNING BACKTEST — 09:45-11:00  [ITM — ORB + 7 CONDITIONS]"
        cond    = "Opening Range Breakout (9:30-9:44) + VWAP + ST + EMA9>EMA20 + Clean + RSI"
        delta_s = (f"ITM: CE=ATM-50, PE=ATM+50 | Delta ~{DELTA_ITM} | RSI>=55/<=45"
                   f" | Trail step: {TRAIL_STEP_MULT_ITM}")
    elif ce_only:
        title   = "NIFTY MORNING BACKTEST — 09:30-11:00  [CE-ONLY — 4 CONDITIONS]"
        cond    = "VWAP + Supertrend + Breakout + Clean candle  |  BUY (CE) signals only"
        delta_s = f"ATM options | Delta ~{DELTA_ATM} | PE removed (35% win rate)"
    else:
        title   = "NIFTY MORNING BACKTEST — 09:30-11:00  [ATM — CE+PE both]"
        cond    = "VWAP + Supertrend + Breakout + Clean candle  |  CE and PE both"
        delta_s = f"ATM options | Delta ~{DELTA_ATM}"

    print(f"\n{sep}")
    print(f"  {title}")
    print(f"  {cond}")
    print(f"  {delta_s}")
    print(f"  Exits: SL/Target/Breakeven/Trailing/Hard exit 3:10 PM")
    print(f"  P&L model: delta premium × LOT_SIZE={LOT_SIZE}")
    print(sep)
    print(f"""
📊 OVERALL ({days}-day period):
  Total Signals   : {total} over {days_traded} days ({total/days_traded:.1f}/day)
  Wins (Tgt+Trail): {wins} ({wr:.1f}%)  [Target: {targets}, Trail: {trails}]
  Losses (SL)     : {loss} ({loss/total*100:.1f}%)
  Breakeven       : {bes} ({bes/total*100:.1f}%)
  Weak Exit       : {weaks} ({weaks/total*100:.1f}%)
  EOD Exits       : {eods} ({eods/total*100:.1f}%)
  Net P&L         : ₹{net:,.0f}
  Avg Win         : ₹{aw:,.0f}
  Avg Loss        : ₹{al:,.0f}
  Max Win Streak  : {mws}
  Max Loss Streak : {mls}
""")

    sl_label  = f"candle-structure (prev low - {CANDLE_SL_BUFFER}pt, dynamic)" if candle_sl else f"fixed {SL_PCT*100:.4f}%"
    tgt_label = f"{target_pts}pt fixed" if target_pts is not None else f"~{(24000*TARGET_PCT):.0f}pt (fixed {TARGET_PCT*100:.4f}%)"
    print(f"  SL              : {sl_label} | Avg SL: {avg_sl_pts:.1f} pts")
    print(f"  Target          : {tgt_label}\n")

    if ce_only or mode == "SOLID":
        # CE-only mode — no PE trades exist, skip the breakdown
        print(f"📈 CE (BUY) only: {len(bdf)} trades | Win: {bwr:.1f}% | P&L: ₹{bdf['pnl_rs'].sum():,.0f}")
    else:
        print(f"📉 CE vs PE:")
        print(f"  CE (BUY)  : {len(bdf)} trades | Win: {bwr:.1f}% | P&L: ₹{bdf['pnl_rs'].sum():,.0f}")
        print(f"  PE (SELL) : {len(sdf)} trades | Win: {swr:.1f}% | P&L: ₹{sdf['pnl_rs'].sum():,.0f}")

    print(f"\n📅 DAY WISE:")
    dpnl = tdf.groupby('day')['pnl_rs'].sum()
    for d in ['Monday','Tuesday','Wednesday','Thursday','Friday']:
        if d in dpnl.index:
            p=dpnl[d]; dc=len(tdf[tdf['day']==d])
            e="🟢" if p>0 else "🔴"
            print(f"  {e} {d:<12}: ₹{p:,.0f} ({dc} trades)")

    print(f"\n📋 ALL TRADES:")
    if mode == "ITM":
        print(f"{'Date':<12}{'Time':<7}{'Day':<12}{'Sig':<6}{'Strike':<12}{'Entry':<10}{'RSI':<6}{'TGT':<7}{'Exit':<10}{'MaxFav':<8}{'Prem':<7}{'P&L₹':<9}Result")
        print("-"*105)
        icons = {'TARGET':'✅','TRAIL':'🔒','SL':'❌','BE':'⚖️','WEAK':'⚠️','EOD':'➡️'}
        for _, r in tdf.iterrows():
            icon = icons.get(r['outcome'],'➡️')
            exp  = " ⚡" if r['expiry'] else ""
            strike = r.get('strike', '-')
            print(f"{r['date']:<12}{r['time']:<7}{r['day']:<12}{r['signal']:<6}{strike:<12}{r['entry']:<10}"
                  f"{r['rsi']:<6}{r['tgt_pts']:<7}{r['exit']:<10}"
                  f"+{r['max_fav']:<7}{r['premium_pts']:<7}{r['pnl_rs']:<9}"
                  f"{icon}{r['outcome']}{exp}")
    else:
        print(f"{'Date':<12}{'Time':<7}{'Day':<12}{'Sig':<6}{'Entry':<10}{'RSI':<6}{'TGT':<6}{'Exit':<10}{'MaxFav':<8}{'Prem':<7}{'P&L₹':<9}Result")
        print("-"*95)
        icons = {'TARGET':'✅','TRAIL':'🔒','SL':'❌','BE':'⚖️','WEAK':'⚠️','EOD':'➡️'}
        for _, r in tdf.iterrows():
            icon = icons.get(r['outcome'],'➡️')
            exp  = " ⚡" if r['expiry'] else ""
            print(f"{r['date']:<12}{r['time']:<7}{r['day']:<12}{r['signal']:<6}{r['entry']:<10}"
                  f"{r['rsi']:<6}{r['tgt_pts']:<6}{r['exit']:<10}"
                  f"+{r['max_fav']:<7}{r['premium_pts']:<7}{r['pnl_rs']:<9}"
                  f"{icon}{r['outcome']}{exp}")

    print(f"\n{sep}")
    print("  VERDICT")
    print(sep)
    if wr>=55 and net>0:   print("  ✅ PROFITABLE — morning window signals are valid")
    elif wr>=45 and net>0: print("  ⚡ MARGINAL — signals fire but edge is thin")
    elif net>0:            print("  ⚡ POSITIVE returns but low win rate")
    else:                  print("  ❌ Morning window not profitable as-is — needs refinement")

    if bwr > swr+15 and len(sdf)>0:
        print(f"  💡 BUY signals better ({bwr:.0f}% vs {swr:.0f}%) — consider CE-only")
    if mls >= 3:
        print(f"  ⚠️  Stop after 3 consecutive losses")
    print(sep)

    # Save CSV only for the final CE-only strategy (not test modes)
    if mode == "ATM" and ce_only:
        out_file = 'backtest_results_morning_final.csv'
        tdf.to_csv(out_file, index=False)
        print(f"\n  📁 Saved → {out_file}")
    print(sep)

def print_comparison(trades_atm, trades_itm, days):
    sep = "="*70
    print(f"\n{sep}")
    print(f"  ATM vs ITM — SIDE BY SIDE COMPARISON  ({days}-day period)")
    print(sep)
    print(f"  ATM: 4 cond, delta 0.5, 9:30 start")
    print(f"  ITM: 7 cond, delta 0.62, 9:35 start (EMA + strong breakout + RSI)")
    print(f"\n{'Metric':<28} {'ATM (4 cond)':<20} {'ITM (7 cond)'}")
    print("-"*68)

    def stats(trades):
        if not trades: return (0, 0, 0, 0, 0, 0)
        tdf = pd.DataFrame(trades)
        total = len(tdf)
        wins  = len(tdf[tdf['outcome'].isin(['TARGET','TRAIL'])])
        loss  = len(tdf[tdf['outcome']=='SL'])
        bes   = len(tdf[tdf['outcome']=='BE'])
        net   = tdf['pnl_rs'].sum()
        wr    = wins/total*100 if total else 0
        return (total, wins, wr, loss, bes, net)

    ta, wa, wra, la, bea, neta = stats(trades_atm)
    ti, wi, wri, li, bei, neti = stats(trades_itm)

    arrow = lambda a, b, higher_better=True: "🟢" if (b > a if higher_better else b < a) else ("🔴" if (b < a if higher_better else b > a) else "⚪")

    print(f"  {'Total trades':<26} {ta:<20} {ti}  {arrow(ta, ti, False)}")
    print(f"  {'Win rate':<26} {wra:.1f}%{'':<17} {wri:.1f}%  {arrow(wra, wri)}")
    print(f"  {'SL count':<26} {la:<20} {li}  {arrow(la, li, False)}")
    print(f"  {'Breakeven':<26} {bea:<20} {bei}")
    print(f"  {'Net P&L (₹)':<26} ₹{neta:,.0f}{'':<14} ₹{neti:,.0f}  {arrow(neta, neti)}")

    per_m_a = neta / (days/30); per_m_i = neti / (days/30)
    print(f"  {'Monthly P&L estimate':<26} ₹{per_m_a:,.0f}{'':<13} ₹{per_m_i:,.0f}  {arrow(per_m_a, per_m_i)}")

    print(f"\n{sep}")
    if neti > neta and wri >= wra:
        print("  ✅ ITM strategy is BETTER — higher profit AND better win rate")
        print(f"     Extra profit vs ATM: ₹{neti-neta:,.0f}  (+{((neti-neta)/abs(neta)*100) if neta!=0 else 0:.1f}%)")
    elif neti > neta:
        print("  ✅ ITM strategy has HIGHER PROFIT (fewer but better trades)")
        print(f"     Extra profit vs ATM: ₹{neti-neta:,.0f}")
    elif wri > wra:
        print("  ⚡ ITM strategy has BETTER WIN RATE (tighter, but same/less profit)")
    elif ti < ta and neti >= neta * 0.8:
        print("  ⚡ ITM strategy trades LESS but holds P&L — good for quality focus")
    else:
        print("  ❌ ITM strategy did not improve results — ATM is still better")
    print(sep)


# ─── MAIN ───
def main():
    sep  = "="*70
    sep2 = "-"*70
    print(sep)
    print("  NIFTY MORNING BACKTEST — 60 day")
    print("  Strategy: CE-only ATM | 4 conditions | 9:30-11:00 window")
    print("  SL: prev candle low - 5pt (dynamic) | Target: 25pt fixed")
    print("  VWAP + Supertrend + Breakout + Bull clean candle | 90.5% win rate")
    print(sep)

    if not login(): return

    DAYS = 60
    print(f"Fetching {DAYS} days of Nifty 5-min data...")
    df5 = fetch_data(NIFTY_TOKEN, "5minute", days=DAYS)
    if df5 is None or df5.empty:
        print("Failed to fetch data"); return
    print(f"  {len(df5)} candles | {df5.index[0].date()} to {df5.index[-1].date()}\n")

    # ── All variants to test ──
    # (label, candle_sl, target_pts, ce_only)
    variants = [
        ("Fixed SL  + orig target (~17pt)",  False, None, True),
        ("Candle SL + orig target (~17pt)",  True,  None, True),
        ("Candle SL + 25pt target",          True,  25,   True),
        ("Candle SL + 30pt target",          True,  30,   True),
        ("Candle SL + 35pt target",          True,  35,   True),
        ("Candle SL + 25pt  CE+PE both",     True,  25,   False),
    ]

    all_trades = {}
    for label, csl, tgt, ceo in variants:
        all_trades[label] = run_backtest(df5, days=DAYS, mode="ATM", ce_only=ceo,
                                         candle_sl=csl, target_pts=tgt)

    # ── Detailed report: baseline (for reference) ──
    print_report(all_trades[variants[0][0]], days=DAYS, mode="ATM", ce_only=True,
                 candle_sl=False, target_pts=None)

    # ── Detailed report: confirmed live strategy (candle SL + 25pt) ──
    print(f"\n{'='*70}")
    print(f"  ★ LIVE STRATEGY — Candle SL + 25pt target")
    print(f"{'='*70}")
    print_report(all_trades["Candle SL + 25pt target"], days=DAYS, mode="ATM",
                 ce_only=True, candle_sl=True, target_pts=25)

    # ── BIG COMPARISON TABLE ──
    def qv(trades):
        if not trades: return (0, 0.0, 0, 0, 0, 0.0)
        tdf = pd.DataFrame(trades)
        total = len(tdf)
        wins  = len(tdf[tdf['outcome'].isin(['TARGET','TRAIL'])])
        sls   = len(tdf[tdf['outcome']=='SL'])
        net   = tdf['pnl_rs'].sum()
        avg_sl= tdf['sl_pts'].mean() if 'sl_pts' in tdf.columns else 0
        return (total, wins/total*100 if total else 0, sls, net, net/(DAYS/30), avg_sl)

    sep = "="*70
    print(f"\n{sep}")
    print(f"  SL STYLE & TARGET COMPARISON — {DAYS} days")
    print(f"  Candle SL = prev candle low/high ± {CANDLE_SL_BUFFER}pt (dynamic)")
    print(sep)
    print(f"  {'Variant':<40} {'Trades':>6} {'Win%':>6} {'SLs':>4} {'AvgSL':>7} {'Net P&L':>10} {'Monthly':>10}")
    print(f"  {'-'*87}")

    best_net = max(qv(t)[3] for t in all_trades.values())
    for label, csl, tgt, ceo in variants:
        t, wr, sl, net, mon, asl = qv(all_trades[label])
        star = " ★" if net == best_net else ""
        print(f"  {label:<40} {t:>6} {wr:>5.1f}% {sl:>4} {asl:>6.1f}pt {net:>10,.0f} {mon:>10,.0f}{star}")

    print(sep)

    # ── Full detailed report for confirmed winner (CE-only candle SL 25pt) ──
    winner_label = "Candle SL + 25pt target"
    print(f"\n  ★ CONFIRMED WINNER: {winner_label}")
    print(f"{'='*70}")
    print_report(all_trades[winner_label], days=DAYS, mode="ATM", ce_only=True,
                 candle_sl=True, target_pts=25)

    # ── PE breakdown for CE+PE variant ──
    cepe_label = "Candle SL + 25pt  CE+PE both"
    cepe_trades = all_trades[cepe_label]
    if cepe_trades:
        tdf_cepe = pd.DataFrame(cepe_trades)
        bdf = tdf_cepe[tdf_cepe['signal']=='BUY']
        sdf = tdf_cepe[tdf_cepe['signal']=='SELL']
        bwr = len(bdf[bdf['outcome'].isin(['TARGET','TRAIL'])])/len(bdf)*100 if len(bdf) else 0
        swr = len(sdf[sdf['outcome'].isin(['TARGET','TRAIL'])])/len(sdf)*100 if len(sdf) else 0
        print(f"\n{sep}")
        print(f"  CE+PE BREAKDOWN (Candle SL + 25pt target)")
        print(sep)
        print(f"  CE (BUY) : {len(bdf)} trades | Win: {bwr:.1f}% | P&L: ₹{bdf['pnl_rs'].sum():,.0f}")
        print(f"  PE (SELL): {len(sdf)} trades | Win: {swr:.1f}% | P&L: ₹{sdf['pnl_rs'].sum():,.0f}")
        print(f"  Combined : {len(tdf_cepe)} trades | Win: {qv(cepe_trades)[1]:.1f}% | P&L: ₹{qv(cepe_trades)[3]:,.0f}")
        print(sep)

    # ── Final verdict ──
    base_net  = qv(all_trades[variants[0][0]])[3]
    base_wr   = qv(all_trades[variants[0][0]])[1]
    win_net   = qv(all_trades[winner_label])[3]
    win_wr    = qv(all_trades[winner_label])[1]
    cepe_net  = qv(cepe_trades)[3]
    cepe_wr   = qv(cepe_trades)[1]

    print(f"\n{sep}")
    print(f"  FINAL VERDICT")
    print(sep)
    print(f"  Current live  : {base_wr:.1f}% win rate | ₹{base_net:,.0f} / 60 days")
    print(f"  Candle SL 25pt (CE-only): {win_wr:.1f}% win rate | ₹{win_net:,.0f} / 60 days  (+{(win_net-base_net)/abs(base_net)*100:.0f}%)")
    print(f"  Candle SL 25pt (CE+PE)  : {cepe_wr:.1f}% win rate | ₹{cepe_net:,.0f} / 60 days")

    if cepe_net > win_net and cepe_wr >= win_wr:
        print(f"\n  ✅ SWITCH TO CE+PE — adding PE makes it BETTER")
        print(f"     Extra vs CE-only: ₹{cepe_net-win_net:,.0f}  |  Win rate: {cepe_wr:.1f}%")
    else:
        print(f"\n  ✅ KEEP CE-ONLY — PE does NOT help morning window")
        print(f"     Stick with: Candle SL (prev low-{CANDLE_SL_BUFFER}pt) + 25pt target, CE-only")
    print(sep)

if __name__ == "__main__":
    main()
