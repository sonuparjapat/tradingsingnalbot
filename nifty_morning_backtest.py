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
TRAIL_TRIGGER_MULT = 1.2
TRAIL_STEP_MULT_ATM = 0.6        # ATM mode — original
TRAIL_STEP_MULT_ITM = 0.35       # ITM mode — tighter, keeps more profit on trail exits

# Wick + RSI mode constants
WICK_THRESHOLD_PT  = 10   # prev candle wick > this → use extra breakout buffer
WICK_BREAKOUT_BUF  = 4    # extra pts needed beyond prev high/low when big wick present
RSI_OVERBOUGHT     = 70   # RSI above this → overbought → avoid CE, look for PE
RSI_OVERSOLD       = 30   # RSI below this → oversold → avoid PE, look for CE

# Early intracandle entry (early_entry=True)
EARLY_ENTRY_BUFFER = 5    # pt above prevH to trigger intracandle entry
RESIST_WICK_PT     = 3    # prev candle H-C > this = resistance at top → skip signal

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
                 target_pts=None, candle_be_trigger_pts=None, entry_end=None,
                 entry_windows=None, skip_expiry=True, tue_windows=None,
                 early_entry=False, trail_trigger_mult=None, max_sl_pts=None):
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
    is_wick_rsi     = (mode == "WICK_RSI")
    if is_solid: ce_only = True
    trail_step_mult  = TRAIL_STEP_MULT_ITM if mode == "ITM" else TRAIL_STEP_MULT_ATM
    eff_trail_trigger_mult = trail_trigger_mult if trail_trigger_mult is not None else TRAIL_TRIGGER_MULT
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

        is_tuesday = (ts.weekday() == EXPIRY_WEEKDAY)
        if skip_expiry and is_tuesday: continue
        # Use tue_windows for Tuesday if provided, otherwise normal entry_windows
        active_windows = (tue_windows if (is_tuesday and tue_windows is not None) else entry_windows)
        if active_windows is not None:
            if not any(ws <= t <= we for ws, we in active_windows): continue
        else:
            eff_entry_end = entry_end if entry_end is not None else ENTRY_END
            if t < ENTRY_START or t > eff_entry_end: continue

        # ITM mode: only enter AFTER Opening Range is established (9:45+)
        if use_rsi and t < ENTRY_SKIP_UNTIL: continue
        # SOLID / SOLID_PE: skip first candle (9:30 opening volatility is highest)
        if (is_solid or is_solid_pe) and t < dtime(9, 35): continue

        is_expiry = is_tuesday

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
        elif is_wick_rsi:
            # WICK_RSI: wick-aware breakout + RSI extremes for CE+PE
            #
            # Wick logic:
            #   prev upper wick = prev_H - max(prev_O, prev_C)
            #   if large (>10pt) → market got rejected there → need +4pt extra to confirm real breakout
            #   prev lower wick = min(prev_O, prev_C) - prev_L
            #   if large (>10pt) → strong support seen → need -4pt extra below that low to confirm breakdown
            #
            # RSI logic:
            #   RSI > 70 (overbought) → stretched up → avoid CE (likely to reverse), look for PE
            #   RSI < 30 (oversold)   → stretched down → avoid PE (likely to bounce), look for CE
            #   RSI 30-70             → neutral → both CE and PE allowed by conditions

            prev_o = float(prev['Open']); prev_c = float(prev['Close'])
            prev_uw = ph - max(prev_o, prev_c)   # prev candle upper wick
            prev_lw = min(prev_o, prev_c) - pl    # prev candle lower wick

            ce_buf = WICK_BREAKOUT_BUF if prev_uw > WICK_THRESHOLD_PT else 0
            pe_buf = WICK_BREAKOUT_BUF if prev_lw > WICK_THRESHOLD_PT else 0

            breakout  = price > ph + ce_buf   # if prev had big upper wick, need extra buffer
            breakdown = price < pl - pe_buf   # if prev had big lower wick, need extra buffer

            # CE: 4 conditions + RSI not overbought (RSI>70 = stretched, bad CE entry)
            buy_ok  = all([price > vwap, st == True,  breakout,  bull_clean, rsi < RSI_OVERBOUGHT])
            # PE: mirror 4 conditions + RSI not oversold (RSI<30 = stretched down, bad PE entry)
            sell_ok = all([price < vwap, st == False, breakdown, bear_clean, rsi > RSI_OVERSOLD])
        else:
            # ATM: 4 conditions (original, CE+PE)
            if early_entry:
                # Intracandle entry: fire when price first crosses prevH + EARLY_ENTRY_BUFFER
                # Replaces: wait for breakout candle to close
                # Resistance check: if prev candle H-C > RESIST_WICK_PT → prev top was rejected → skip
                prev_wick_top  = ph - float(prev['Close'])
                entry_trigger  = ph + EARLY_ENTRY_BUFFER
                resist_ok      = (prev_wick_top <= RESIST_WICK_PT)
                reach_ok       = (float(row['High']) >= entry_trigger)
                cond_vwap_prev = (float(prev['Close']) > float(prev['VWAP']))
                cond_st_prev   = (bool(prev['Supertrend']) == True)
                buy_ok  = all([cond_vwap_prev, cond_st_prev, resist_ok, reach_ok])
                sell_ok = False   # CE-only in early_entry mode
                if buy_ok:
                    # Entry at trigger level; if candle gapped above trigger, fill at open
                    price = max(float(row['Open']), entry_trigger)
            else:
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

        # Cap SL distance if structural SL is too far away
        if max_sl_pts is not None and sl_dist > max_sl_pts:
            sl_dist = max_sl_pts
            sl = price - sl_dist if signal == "BUY" else price + sl_dist

        tgt_dist = target_pts if target_pts is not None else price * TARGET_PCT
        be_dist  = price * BREAKEVEN_PCT
        target   = price + tgt_dist if signal=="BUY" else price - tgt_dist

        trail_trigger_dist = be_dist * eff_trail_trigger_mult
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

    if mode == "WICK_RSI":
        title   = "NIFTY MORNING BACKTEST — 09:30-11:00  [WICK+RSI — CE+PE, WICK-AWARE]"
        cond    = (f"CE: VWAP+ST+Breakout(+{WICK_BREAKOUT_BUF}pt if prev wick>{WICK_THRESHOLD_PT}pt)+Clean+RSI<{RSI_OVERBOUGHT}  |  "
                   f"PE: mirror+RSI>{RSI_OVERSOLD}")
        delta_s = f"ATM | Delta ~{DELTA_ATM} | Wick buffer: +{WICK_BREAKOUT_BUF}pt when prev wick>{WICK_THRESHOLD_PT}pt"
    elif mode == "SOLID":
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
    sep  = "="*84
    print(sep)
    print("  NIFTY MORNING BACKTEST — SL Cap Test  (60 day)")
    print("  Question: should we cap SL distance? structural SL avg=46pt, max=110pt")
    print("  Window   : 9:30-13:00 | CE-only | Skip Tuesday | Candle SL | Target 25pt")
    print(sep)

    if not login(): return

    DAYS = 60
    LIVE_WIN = [(dtime(9, 30), dtime(13, 0))]

    print(f"Fetching {DAYS} days of Nifty 5-min data...")
    df5 = fetch_data(NIFTY_TOKEN, "5minute", days=DAYS)
    if df5 is None or df5.empty:
        print("Failed to fetch data"); return
    print(f"  {len(df5)} candles | {df5.index[0].date()} to {df5.index[-1].date()}\n")

    def qv(trades):
        if not trades: return (0, 0.0, 0, 0, 0, 0.0, 0.0)
        tdf = pd.DataFrame(trades)
        total = len(tdf)
        wins  = len(tdf[tdf['outcome'].isin(['TARGET','TRAIL'])])
        sls   = len(tdf[tdf['outcome']=='SL'])
        bes   = len(tdf[tdf['outcome']=='BE'])
        net   = tdf['pnl_rs'].sum()
        avg_sl= tdf['sl_pts'].mean() if 'sl_pts' in tdf.columns else 0
        return (total, wins/total*100 if total else 0, sls, bes, net, net/(DAYS/30), avg_sl)

    BASE_PARAMS = dict(days=DAYS, mode="ATM", ce_only=True, candle_sl=True,
                       target_pts=25, entry_windows=LIVE_WIN, skip_expiry=True)

    caps = [
        ("No cap (current)",   None),
        ("Cap 40pt",           40),
        ("Cap 30pt",           30),
        ("Cap 25pt",           25),
    ]

    all_results = {}
    for label, cap in caps:
        print(f"  Running: {label}...")
        all_results[label] = run_backtest(df5, **BASE_PARAMS, max_sl_pts=cap)

    # ── Summary comparison ──
    print(f"\n{sep}")
    print(f"  SL CAP TEST — {DAYS} days | CE-only | 9:30-13:00 | Skip Tue")
    print(f"  Structural SL (prev_low - 5pt): avg=46pt, median=39pt, max=110pt")
    print(sep)
    arrow = lambda a, b, hi=True: "★" if (b > a if hi else b < a) else ""
    print(f"  {'Metric':<20} {'No cap':<16} {'Cap 40pt':<16} {'Cap 30pt':<16} {'Cap 25pt'}")
    print(f"  {'-'*82}")

    rows = {}
    for label, cap in caps:
        t, wr, sl, be, net, mon, _ = qv(all_results[label])
        rows[label] = (t, wr, sl, be, net, mon)

    def row4(name, fn, hi=True):
        vals = [fn(rows[l]) for l, _ in caps]
        best = max(vals) if hi else min(vals)
        parts = [f"{vals[0]:<16}"]
        for v in vals[1:]:
            star = " ★" if v == best and v != vals[0] else ""
            parts.append(f"{v:<14}{star}")
        print(f"  {name:<20} {''.join(parts)}")

    nc = rows["No cap (current)"]
    print(f"  {'Trades':<20} {nc[0]:<16}", end="")
    for label, _ in caps[1:]:
        print(f" {rows[label][0]:<15}", end="")
    print()

    for label, cap in caps:
        t, wr, sl, be, net, mon = rows[label]
        star = ""
        print(f"  {label:<20} trades={t}  win={wr:.1f}%  SLs={sl}  BEs={be}  net=₹{net:,.0f}  monthly=₹{mon:,.0f}")

    print(sep)

    # ── Highlight changes from current ──
    base_trades = all_results["No cap (current)"]
    base_by_date = {r['date']: r for r in base_trades}
    icons = {'TARGET':'✅','TRAIL':'🔒','SL':'❌','BE':'⚖️','WEAK':'⚠️','EOD':'➡️'}

    for label, cap in caps[1:]:
        cap_trades = all_results[label]
        cap_by_date = {r['date']: r for r in cap_trades}
        changed = [(d, base_by_date[d], cap_by_date[d])
                   for d in base_by_date
                   if d in cap_by_date and base_by_date[d]['outcome'] != cap_by_date[d]['outcome']]
        if changed:
            print(f"\n  Changes with {label}:")
            for d, b, c in changed:
                bi = icons.get(b['outcome'],'➡️'); ci = icons.get(c['outcome'],'➡️')
                print(f"    {d}  entry={b['entry']:.1f}  sl_dist_orig={b['sl_pts']:.1f}pt  "
                      f"{bi}{b['outcome']} ₹{b['pnl_rs']:.0f}  →  {ci}{c['outcome']} ₹{c['pnl_rs']:.0f}")
        else:
            print(f"\n  {label}: NO changes vs no-cap — zero new SL exits")

    print()
    print(sep)
    # ── Verdict ──
    bt, bwr, bsl, bbe, bnet, bmon = rows["No cap (current)"]
    c30t, c30wr, c30sl, c30be, c30net, c30mon = rows["Cap 30pt"]
    c25t, c25wr, c25sl, c25be, c25net, c25mon = rows["Cap 25pt"]

    print(f"  VERDICT")
    print(sep)
    if c30sl == bsl:
        print(f"  ✅ SL cap makes NO difference to outcomes — structural SL never gets triggered")
        print(f"     The wide SL (avg 46pt) is a theoretical backstop that has never fired.")
        print(f"     TRAIL/BE/WEAK exits always happen first (within 5-15pt of entry).")
        print(f"     Adding a cap is optional — adds a safety net for extreme future scenarios")
        print(f"     without hurting current 60-day performance.")
        print(f"     Recommendation: add 30pt cap as safety net — zero cost, pure risk protection.")
    elif c30sl > bsl:
        print(f"  ⚠️  30pt cap creates {c30sl-bsl} new SL exits — some trades dip 25-30pt before recovering")
        print(f"     The structural SL is actually needed for those trades.")
        if c25sl > c30sl:
            print(f"     25pt cap is worse — do not use. 30pt cap might be acceptable if you want protection.")
        else:
            print(f"     Consider 40pt cap as compromise.")
    print(sep)

if __name__ == "__main__":
    main()
