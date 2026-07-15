"""
=============================================================
NIFTY MORNING BACKTEST
=============================================================
Backtests the CE-only morning scanner strategy:
  VWAP + Supertrend + Breakout + Bull clean candle (4 conditions)
  ATM options, delta ~0.5 | 9:30-13:00 | Skip Tuesday
  SL: signal candle low - 5pt | Target: 25pt fixed

Tuesday windows also backtested:
  Morning PE: 9:30-10:30 | Evening PE: 13:00-14:30 (signal-only)
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
LOT_SIZE         = int(os.getenv("LOT_SIZE", "65"))

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

# When SL is at entry (BE mode, no trail yet), options premium doesn't drop to entry premium
# the instant spot touches entry spot — IV expansion protects it slightly.
# Require spot to fall 3pt BELOW entry before triggering BE exit (more realistic).
BE_EXIT_BUFFER = 3

# Premium P&L model
DELTA_SCALE_PCT          = 0.0048
EXPIRY_THETA_HAIRCUT_PCT = 0.15
STRIKE_GAP               = 50    # NIFTY option strike spacing

# Delta for ATM options
DELTA_ATM = 0.50   # ATM options: delta ~0.5

# Trailing stop
TRAIL_TRIGGER_MULT  = 1.2
TRAIL_STEP_MULT_ATM = 0.6

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
def run_backtest(df5, days=60, ce_only=False, candle_sl=False,
                 target_pts=None, entry_end=None,
                 entry_windows=None, skip_expiry=True, tue_windows=None,
                 trail_trigger_mult=None, max_sl_pts=None,
                 sideways_range_pt=None, entry_slippage_pts=0, signal_candle_sl=False,
                 rejection_uw_min=None, rejection_zone_pt=None, rejection_lookback_n=24,
                 green_bias_n=None, green_bias_min_pct=0.5):
    """
    ATM CE (BUY) strategy: VWAP + Supertrend + Breakout + Bull clean candle.
    ce_only=True  : BUY signals only (production default)
    ce_only=False : CE+PE both (used for Tuesday PE backtest)
    """
    delta_base      = DELTA_ATM
    trail_step_mult = TRAIL_STEP_MULT_ATM
    eff_trail_trigger_mult = trail_trigger_mult if trail_trigger_mult is not None else TRAIL_TRIGGER_MULT

    df5 = df5.copy()
    df5['VWAP']       = calculate_vwap(df5)
    df5['Supertrend'] = calculate_supertrend(df5)
    df5['RSI']        = calculate_rsi(df5['Close'])
    df5 = df5.dropna(subset=['VWAP', 'RSI'])

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

        is_expiry = is_tuesday

        price = float(row['Close'])
        o,h,l,c = float(row['Open']),float(row['High']),float(row['Low']),float(row['Close'])
        vwap  = float(row['VWAP'])
        st    = bool(row['Supertrend'])
        rsi   = float(row['RSI'])
        ph    = float(prev['High']); pl = float(prev['Low'])

        is_doji, bull_clean, bear_clean = analyze_candle(o,h,l,c)
        if is_doji: continue

        # ── Sideways filter: skip if last 4 candles' range is too narrow ──
        if sideways_range_pt is not None and i >= 4:
            recent = df5.iloc[i-4:i]
            recent_range = float(recent['High'].max()) - float(recent['Low'].min())
            if recent_range < sideways_range_pt:
                continue

        # ATM: 4 conditions — VWAP + Supertrend + Breakout + Clean candle
        breakout  = price > ph
        breakdown = price < pl
        buy_ok  = all([price > vwap, st == True,  breakout,  bull_clean])
        sell_ok = all([price < vwap, st == False, breakdown, bear_clean])

        if buy_ok and last_sig.get(date) != "BUY":
            signal = "BUY"
        elif sell_ok and not ce_only and last_sig.get(date) != "SELL":
            signal = "SELL"
        else:
            continue

        # ── Rejection zone: skip BUY if entry is within zone_pt of a recent large upper-wick high ──
        # Does NOT set last_sig — allows later candle on same day to still fire if zone clears
        if signal == "BUY" and rejection_uw_min is not None and rejection_zone_pt is not None:
            lookback_start = max(0, i - rejection_lookback_n)
            near_rejection = any(
                (float(df5.iloc[ri]['High']) - max(float(df5.iloc[ri]['Open']), float(df5.iloc[ri]['Close']))) >= rejection_uw_min
                and abs(price - float(df5.iloc[ri]['High'])) <= rejection_zone_pt
                for ri in range(lookback_start, i)
            )
            if near_rejection:
                continue

        # ── Green bias: skip BUY if fewer than min_pct of recent N candles are green ──
        if signal == "BUY" and green_bias_n is not None and i >= green_bias_n:
            recent_n = df5.iloc[i - green_bias_n:i]
            green_count = int((recent_n['Close'] > recent_n['Open']).sum())
            if green_count < green_bias_n * green_bias_min_pct:
                continue

        last_sig[date] = signal

        # Simulate delayed fill: enter N pts above signal price
        if entry_slippage_pts:
            price = price + entry_slippage_pts if signal == "BUY" else price - entry_slippage_pts

        if candle_sl:
            if signal_candle_sl:
                # CE: use signal candle Low (breakout candle's own low = tighter, always above prev Low)
                # PE: keep prev candle High — signal candle High for PE is lower → SL easier to hit
                sl  = (l - CANDLE_SL_BUFFER) if signal=="BUY" else (ph + CANDLE_SL_BUFFER)
            else:
                sl  = (pl - CANDLE_SL_BUFFER) if signal=="BUY" else (ph + CANDLE_SL_BUFFER)
            sl_dist = abs(price - sl)
        else:
            sl_dist = price * SL_PCT
            sl      = price - sl_dist if signal=="BUY" else price + sl_dist

        # (max_sl_pts kept as parameter for testing only — backtest shows wide SL still wins)
        if max_sl_pts is not None and sl_dist > max_sl_pts:
            continue

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
            fo_j = float(df5.iloc[j]['Open']); fc_j = float(df5.iloc[j]['Close'])
            max_favorable = max(max_favorable, fh-price if signal=="BUY" else price-fl)

            if j-i == MOMENTUM_CANDLES and max_favorable < MOMENTUM_MIN:
                exit_price = float(df5.iloc[j]['Close']); outcome = "WEAK"; break


            if not breakeven_hit:
                if signal=="BUY"  and fh >= price + be_dist: current_sl=price; breakeven_hit=True
                if signal=="SELL" and fl <= price - be_dist: current_sl=price; breakeven_hit=True

            if max_favorable >= trail_trigger_dist:
                trail_dist = max_favorable - trail_step_dist
                if signal=="BUY":  current_sl = max(current_sl, price + trail_dist)
                else:              current_sl = min(current_sl, price - trail_dist)
                breakeven_hit = True

            if signal=="BUY"  and fh >= target: exit_price=target; outcome="TARGET"; break
            if signal=="SELL" and fl <= target:  exit_price=target; outcome="TARGET"; break

            # BE SL: when current_sl == entry price, apply a 3pt buffer below entry
            # (options premium doesn't drop to entry level the instant spot touches entry)
            # Trail SL and hard SL: use exact level — no buffer needed
            be_buy_sl  = (price - BE_EXIT_BUFFER) if (breakeven_hit and current_sl == price) else current_sl
            be_sell_sl = (price + BE_EXIT_BUFFER) if (breakeven_hit and current_sl == price) else current_sl
            if signal=="BUY"  and fl <= be_buy_sl:
                exit_price=current_sl
                outcome = "TRAIL" if current_sl>price else ("BE" if breakeven_hit else "SL"); break
            if signal=="SELL" and fh >= be_sell_sl:
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
        trades.append(trade)

    return trades


# ─── TUESDAY WINDOW (separate, independent strategy) ───────────────────────
# ATM CE+PE | 9:30-10:30 | 25pt target | trail ON
# Backtested: 75% WR, Rs1,602 over 90 days (13 Tuesdays)
# Completely separate from the main Mon/Wed/Thu/Fri strategy.

TUESDAY_ENTRY_WIN          = (dtime(9, 30),  dtime(10, 30))
TUESDAY_TARGET_PTS         = 25
TUESDAY_SIDEWAYS_PT        = 30
TUESDAY_EVENING_WIN        = (dtime(13, 0), dtime(14, 30))
TUESDAY_EVENING_TARGET_PTS = 20


def run_tuesday_backtest(df5, days=90, bt_lots=1, pe_only=True, window="morning"):
    """
    Tuesday-only backtest — ATM, PE-only.
    window="morning" → 9:30-10:30, 25pt target (100% WR)
    window="evening" → 13:00-14:30, 20pt target (100% WR)
    pe_only=True  → PE (SELL) signals only — default, best WR
    pe_only=False → CE+PE both
    Returns (trades_list, summary_dict).
    """
    if window == "evening":
        tue_win    = [TUESDAY_EVENING_WIN]
        target_pts = TUESDAY_EVENING_TARGET_PTS
    else:
        tue_win    = [TUESDAY_ENTRY_WIN]
        target_pts = TUESDAY_TARGET_PTS

    trades = run_backtest(
        df5,
        days=days,
        ce_only=False,              # run both so PE signals can fire
        candle_sl=True,
        signal_candle_sl=True,
        target_pts=target_pts,
        entry_windows=None,         # no normal-day window — only tue_windows fires
        tue_windows=tue_win,
        skip_expiry=False,          # include Tuesdays
        sideways_range_pt=TUESDAY_SIDEWAYS_PT,
    )

    if not trades:
        return [], {}

    tdf = pd.DataFrame(trades)
    tdf['_wd'] = pd.to_datetime(tdf['date']).dt.weekday
    tdf = tdf[tdf['_wd'] == EXPIRY_WEEKDAY].drop(columns=['_wd']).reset_index(drop=True)

    if pe_only:
        tdf = tdf[tdf['signal'] == 'SELL'].reset_index(drop=True)

    if tdf.empty:
        return [], {}

    total = len(tdf)
    wins  = len(tdf[tdf['outcome'].isin(['TARGET','TRAIL'])])
    tgt   = len(tdf[tdf['outcome']=='TARGET'])
    trail = len(tdf[tdf['outcome']=='TRAIL'])
    be    = len(tdf[tdf['outcome']=='BE'])
    weak  = len(tdf[tdf['outcome']=='WEAK'])
    sl    = len(tdf[tdf['outcome']=='SL'])
    wr    = wins / total * 100
    net   = tdf['pnl_rs'].sum() * bt_lots
    ce_df = tdf[tdf['signal']=='BUY']
    pe_df = tdf[tdf['signal']=='SELL']
    ce_wr = (len(ce_df[ce_df['outcome'].isin(['TARGET','TRAIL'])]) / len(ce_df) * 100) if len(ce_df) else 0
    pe_wr = (len(pe_df[pe_df['outcome'].isin(['TARGET','TRAIL'])]) / len(pe_df) * 100) if len(pe_df) else 0

    summary = dict(
        total=total, wins=wins, tgt=tgt, trail=trail, be=be, weak=weak, sl=sl,
        wr=wr, net=net, ce_trades=len(ce_df), pe_trades=len(pe_df),
        ce_wr=ce_wr, pe_wr=pe_wr, pe_only=pe_only,
        period_start=str(tdf.iloc[0]['date']), period_end=str(tdf.iloc[-1]['date']),
        tuesdays_traded=len(set(tdf['date'])),
    )
    return tdf.to_dict('records'), summary


# ─── MAIN ───
def main():
    sep = "="*72
    print(sep)
    print("  NIFTY MORNING BACKTEST — Live Strategy (90 day)")
    print("  CE-only ATM | 4 conditions | 9:30-13:00 | Skip Tuesday")
    print(sep)

    if not login(): return

    DAYS     = 90
    LIVE_WIN = [(dtime(9, 30), dtime(13, 0))]

    print(f"Fetching {DAYS} days of Nifty 5-min data...")
    df5 = fetch_data(NIFTY_TOKEN, "5minute", days=DAYS)
    if df5 is None or df5.empty:
        print("Failed to fetch data"); return
    print(f"  {len(df5)} candles | {df5.index[0].date()} to {df5.index[-1].date()}\n")

    # ── Main strategy (Mon/Wed/Thu/Fri) ──────────────────────────────────
    MAX_CANDLE_SL_PTS = 50   # must match nifty_morning_scanner.py
    BASE_PARAMS = dict(days=DAYS, ce_only=True, candle_sl=True,
                       target_pts=25, entry_windows=LIVE_WIN, skip_expiry=True,
                       sideways_range_pt=30, max_sl_pts=MAX_CANDLE_SL_PTS,
                       signal_candle_sl=True)

    trades = run_backtest(df5, **BASE_PARAMS)
    if not trades:
        print("No signals found."); return

    tdf   = pd.DataFrame(trades)
    total = len(tdf)
    tgt   = len(tdf[tdf['outcome']=='TARGET'])
    trail = len(tdf[tdf['outcome']=='TRAIL'])
    be    = len(tdf[tdf['outcome']=='BE'])
    weak  = len(tdf[tdf['outcome']=='WEAK'])
    sl    = len(tdf[tdf['outcome']=='SL'])
    wins  = tgt + trail
    wr    = wins / total * 100
    net   = tdf['pnl_rs'].sum()

    print(sep)
    print(f"  Period  : {tdf.iloc[0]['date']} to {tdf.iloc[-1]['date']}")
    print(f"  Trades  : {total}")
    print(f"  Win Rate: {wr:.1f}%  (TARGET={tgt}  TRAIL={trail}  BE={be}  WEAK={weak}  SL={sl})")
    print(f"  Net PnL : Rs{net:,.0f}  (1 lot of {LOT_SIZE})")
    print(sep)
    print(f"\n  {'Date':<12} {'Day':<10} {'Signal':<8} {'Entry':>8} {'Outcome':<8} {'PnL':>8} {'MaxFav':>8}")
    print(f"  {'-'*68}")
    for _, r in tdf.iterrows():
        print(f"  {str(r['date']):<12} {str(r.get('day','')):<10} {r.get('signal',''):<8} "
              f"{r['entry']:>8.1f} {r['outcome']:<8} Rs{r['pnl_rs']:>6.0f} {r['max_fav']:>7.1f}pt")
    print(sep)

    # ── SL-cap impact comparison ──────────────────────────────────────────
    print()
    print(sep)
    print(f"  SL-CAP ANALYSIS — with vs without {MAX_CANDLE_SL_PTS}pt cap")
    print(sep)
    trades_nocap = run_backtest(df5, **{**BASE_PARAMS, 'max_sl_pts': None})
    if trades_nocap:
        nc = pd.DataFrame(trades_nocap)
        nc_total = len(nc)
        nc_wins  = len(nc[nc['outcome'].isin(['TARGET','TRAIL'])])
        nc_wr    = nc_wins / nc_total * 100
        nc_sl_pts = nc['sl_pts'].tolist() if 'sl_pts' in nc.columns else []
        nc_max_sl = max(nc_sl_pts) if nc_sl_pts else 0
        nc_avg_sl = sum(nc_sl_pts)/len(nc_sl_pts) if nc_sl_pts else 0
        c_total  = len(tdf)
        c_wins   = tgt + trail
        c_wr     = wr
        c_sl_pts = tdf['sl_pts'].tolist() if 'sl_pts' in tdf.columns else []
        c_max_sl = max(c_sl_pts) if c_sl_pts else 0
        c_avg_sl = sum(c_sl_pts)/len(c_sl_pts) if c_sl_pts else 0
        filtered = nc_total - c_total
        print(f"  {'':30} {'No cap':>12} {'Cap={:}pt'.format(MAX_CANDLE_SL_PTS):>12}  {'Impact'}")
        print(f"  {'Trades':30} {nc_total:>12} {c_total:>12}")
        print(f"  {'Filtered out by cap':30} {'':>12} {filtered:>12}")
        print(f"  {'Win Rate':30} {nc_wr:>11.1f}% {c_wr:>11.1f}%")
        print(f"  {'Max SL distance (pts)':30} {nc_max_sl:>12.1f} {c_max_sl:>12.1f}")
        print(f"  {'Avg SL distance (pts)':30} {nc_avg_sl:>12.1f} {c_avg_sl:>12.1f}")
        max_loss_nocap = round(nc_max_sl * 0.5 * LOT_SIZE, 0)
        max_loss_cap   = round(c_max_sl  * 0.5 * LOT_SIZE, 0)
        print(f"  {'Max Rs loss/lot at SL':30} Rs{max_loss_nocap:>10,.0f} Rs{max_loss_cap:>10,.0f}")
        if filtered > 0:
            print(f"\n  Filtered signals (SL > {MAX_CANDLE_SL_PTS}pt):")
            filtered_df = nc[~nc.index.isin(tdf.index)] if len(nc) != len(tdf) else pd.DataFrame()
            # Compare by date+entry to find skipped rows
            nc_keys = set(zip(nc['date'].astype(str), nc['entry'].round(1)))
            c_keys  = set(zip(tdf['date'].astype(str), tdf['entry'].round(1)))
            skip_keys = nc_keys - c_keys
            skip_rows = nc[nc.apply(lambda r: (str(r['date']), round(r['entry'],1)) in skip_keys, axis=1)]
            for _, r in skip_rows.iterrows():
                sl_d = r.get('sl_pts', 0)
                print(f"    {str(r['date']):<12} {str(r.get('day','')):<10} "
                      f"entry={r['entry']:.1f}  SL-dist={sl_d:.1f}pt  outcome={r['outcome']}  "
                      f"PnL=Rs{r['pnl_rs']:.0f}")
    print(sep)

    # ── Tuesday MORNING strategy (9:30-10:30) ────────────────────────────
    print()
    print(sep)
    print("  TUESDAY MORNING WINDOW — PE only | 9:30-10:30 | 25pt target")
    print(sep)
    m_trades, m_sum = run_tuesday_backtest(df5, days=DAYS, window="morning")
    if not m_trades:
        print("  No Tuesday morning PE signals found.")
    else:
        ms = m_sum
        print(f"  Period  : {ms['period_start']} to {ms['period_end']}")
        print(f"  Trades  : {ms['total']}")
        print(f"  Win Rate: {ms['wr']:.1f}%  (TARGET={ms['tgt']}  TRAIL={ms['trail']}  BE={ms['be']}  WEAK={ms['weak']}  SL={ms['sl']})")
        print(f"  Net PnL : Rs{ms['net']:,.0f}  (1 lot of {LOT_SIZE})")
        print(sep)
        print(f"\n  {'Date':<12} {'Time':<6} {'Entry':>8} {'Outcome':<8} {'PnL':>8} {'MaxFav':>8}")
        print(f"  {'-'*58}")
        for r in m_trades:
            print(f"  {str(r['date']):<12} {str(r.get('time','')):<6} "
                  f"{r['entry']:>8.1f} {r['outcome']:<8} Rs{r['pnl_rs']:>6.0f} {r['max_fav']:>7.1f}pt")
        print(sep)

    # ── Tuesday EVENING strategy (13:00-14:30) ───────────────────────────
    print()
    print(sep)
    print("  TUESDAY EVENING WINDOW — PE only | 13:00-14:30 | 20pt target")
    print("  (Expiry day max theta decay — selling pressure window)")
    print(sep)
    e_trades, e_sum = run_tuesday_backtest(df5, days=DAYS, window="evening")
    if not e_trades:
        print("  No Tuesday evening PE signals found.")
    else:
        es = e_sum
        print(f"  Period  : {es['period_start']} to {es['period_end']}")
        print(f"  Trades  : {es['total']}")
        print(f"  Win Rate: {es['wr']:.1f}%  (TARGET={es['tgt']}  TRAIL={es['trail']}  BE={es['be']}  WEAK={es['weak']}  SL={es['sl']})")
        print(f"  Net PnL : Rs{es['net']:,.0f}  (1 lot of {LOT_SIZE})")
        print(sep)
        print(f"\n  {'Date':<12} {'Time':<6} {'Entry':>8} {'Outcome':<8} {'PnL':>8} {'MaxFav':>8}")
        print(f"  {'-'*58}")
        for r in e_trades:
            print(f"  {str(r['date']):<12} {str(r.get('time','')):<6} "
                  f"{r['entry']:>8.1f} {r['outcome']:<8} Rs{r['pnl_rs']:>6.0f} {r['max_fav']:>7.1f}pt")
        print(sep)

    # ── Tuesday COMBINED (both windows) ──────────────────────────────────
    all_tue = m_trades + e_trades
    if all_tue:
        at_total = len(all_tue)
        at_wins  = sum(1 for t in all_tue if t['outcome'] in ('TARGET','TRAIL'))
        at_wr    = at_wins / at_total * 100
        at_net   = sum(t['pnl_rs'] for t in all_tue)
        at_sl    = sum(1 for t in all_tue if t['outcome'] == 'SL')
        print()
        print(sep)
        print("  TUESDAY COMBINED (both windows)")
        print(sep)
        print(f"  Morning  : {len(m_trades)} trades | {m_sum.get('wr',0):.1f}% WR | Rs{m_sum.get('net',0):,.0f}")
        print(f"  Evening  : {len(e_trades)} trades | {e_sum.get('wr',0):.1f}% WR | Rs{e_sum.get('net',0):,.0f}")
        print(f"  Combined : {at_total} trades | {at_wr:.1f}% WR | SL:{at_sl} | Rs{at_net:,.0f}")
        print(sep)
    else:
        at_total = at_wins = at_net = at_sl = 0; at_wr = 0

    # ── Slippage impact test: +3pt entry vs baseline ──────────────────────
    print()
    print(sep)
    print("  SLIPPAGE TEST — +3pt delayed entry vs perfect-fill baseline")
    print("  (Simulates filling 3pts above breakout candle close)")
    print(sep)
    trades_slip = run_backtest(df5, **BASE_PARAMS, entry_slippage_pts=3)
    if trades_slip:
        sdf = pd.DataFrame(trades_slip)
        s_wins = len(sdf[sdf['outcome'].isin(['TARGET','TRAIL'])])
        s_sl   = len(sdf[sdf['outcome']=='SL'])
        s_wr   = s_wins / len(sdf) * 100
        s_net  = sdf['pnl_rs'].sum()
        print(f"  Baseline : {total} trades | WR={wr:.1f}% | SL={sl} | Net=Rs{net:,.0f}")
        print(f"  +3pt slip: {len(sdf)} trades | WR={s_wr:.1f}% | SL={s_sl} | Net=Rs{s_net:,.0f}")
        wr_delta  = s_wr  - wr
        net_delta = s_net - net
        arrow = "🟢" if wr_delta >= 0 else "🔴"
        print(f"  Delta    : WR{wr_delta:+.1f}%  {arrow}  Net{net_delta:+,.0f}")
        print(f"  Verdict  : {'No material impact — strategy robust to 3pt slippage ✅' if abs(wr_delta) < 3 else 'Slippage hurts — execution speed matters ⚠️'}")
    else:
        print("  No trades returned for slippage test.")
    print(sep)

    # ── Lower Wick Filter Test ────────────────────────────────────────────
    print()
    print(sep)
    print("  LOWER WICK FILTER TEST — lw <= body added to bull_clean signal candle")
    print("  (Does adding lower-wick check improve signal quality?)")
    print(sep)
    import sys as _sys
    _mod = _sys.modules[__name__]
    _orig_ac = _mod.analyze_candle
    def _ac_lw(o, h, l, c):
        body = abs(c - o); tr = h - l
        if tr == 0: return True, False, False
        uw = h - max(o, c); lw = min(o, c) - l
        doji = (body / tr) < 0.1
        return doji, (not doji and c > o and uw <= body and lw <= body), (not doji and c < o and lw <= body and uw <= body)
    _mod.analyze_candle = _ac_lw
    trades_lw = run_backtest(df5, **BASE_PARAMS)
    _mod.analyze_candle = _orig_ac
    if trades_lw:
        lw_df   = pd.DataFrame(trades_lw)
        lw_tot  = len(lw_df)
        lw_wins = len(lw_df[lw_df['outcome'].isin(['TARGET','TRAIL'])])
        lw_sl   = len(lw_df[lw_df['outcome']=='SL'])
        lw_be   = len(lw_df[lw_df['outcome']=='BE'])
        lw_wr   = lw_wins / lw_tot * 100
        lw_net  = lw_df['pnl_rs'].sum()
        filtered_lw = total - lw_tot
        wr_delta_lw  = lw_wr  - wr
        net_delta_lw = lw_net - net
        arrow_lw = "+" if wr_delta_lw >= 0 else "-"
        print(f"  {'':30} {'Baseline':>12} {'LW Filter':>12}  {'Delta'}")
        print(f"  {'Trades':30} {total:>12} {lw_tot:>12}  {-filtered_lw:+d}")
        print(f"  {'Filtered by lw<=body':30} {'':>12} {filtered_lw:>12}")
        print(f"  {'Win Rate':30} {wr:>11.1f}% {lw_wr:>11.1f}%  {wr_delta_lw:+.1f}%")
        print(f"  {'SL count':30} {sl:>12} {lw_sl:>12}  {lw_sl-sl:+d}")
        print(f"  {'BE count':30} {be:>12} {lw_be:>12}  {lw_be-be:+d}")
        print(f"  {'Net PnL':30} Rs{net:>9,.0f} Rs{lw_net:>9,.0f}  Rs{net_delta_lw:+,.0f}")
        verdict = "IMPROVES results — worth implementing" if (wr_delta_lw >= 0 and net_delta_lw >= 0) else \
                  "Mixed — check filtered trades before deciding" if (wr_delta_lw >= 0 or net_delta_lw >= 0) else \
                  "HURTS results — do NOT implement"
        print(f"  Verdict : {verdict}")
        if filtered_lw > 0:
            print(f"\n  Signals filtered by lw>body:")
            base_keys = set(zip(tdf['date'].astype(str), tdf['entry'].round(1)))
            lw_keys   = set(zip(lw_df['date'].astype(str), lw_df['entry'].round(1)))
            skip_keys = base_keys - lw_keys
            skip_rows = tdf[tdf.apply(lambda r: (str(r['date']), round(r['entry'],1)) in skip_keys, axis=1)]
            for _, r in skip_rows.iterrows():
                print(f"    {str(r['date']):<12} {str(r.get('day','')):<10} "
                      f"entry={r['entry']:.1f}  outcome={r['outcome']}  PnL=Rs{r['pnl_rs']:.0f}")
    else:
        print("  No trades returned with lw filter — filter is too aggressive.")
    print(sep)

    # ── Rejection Zone + Green Bias Filter Tests ─────────────────────────
    def _run_filter_test(label, **extra):
        t = run_backtest(df5, **BASE_PARAMS, **extra)
        if not t:
            return None
        d  = pd.DataFrame(t)
        tw = len(d[d['outcome'].isin(['TARGET','TRAIL'])])
        ts_ = len(d[d['outcome']=='SL'])
        tb  = len(d[d['outcome']=='BE'])
        tw2 = len(d[d['outcome']=='WEAK'])
        tw_r = tw / len(d) * 100
        tn  = d['pnl_rs'].sum()
        return dict(label=label, total=len(d), wr=tw_r, sl=ts_, be=tb, weak=tw2, net=tn, df=d)

    print()
    print(sep)
    print("  REJECTION ZONE + GREEN BIAS FILTER TESTS")
    print("  (Do cautious filters improve signal quality?)")
    print(sep)

    # Test parameters — tuned for Nifty 5-min bars
    REJ_UW   = 10   # upper wick >= 10pt counts as a rejection
    REJ_ZONE = 20   # entry within 20pt of rejection high → skip
    REJ_LB   = 24   # look back 24 candles (~2 hours)
    GB_N     = 5    # last 5 candles (25 min)
    GB_PCT   = 0.5  # need ≥ 50% green

    variants = [
        ("Rejection zone only",   dict(rejection_uw_min=REJ_UW, rejection_zone_pt=REJ_ZONE, rejection_lookback_n=REJ_LB)),
        ("Green bias only",       dict(green_bias_n=GB_N, green_bias_min_pct=GB_PCT)),
        ("Rejection + Green",     dict(rejection_uw_min=REJ_UW, rejection_zone_pt=REJ_ZONE, rejection_lookback_n=REJ_LB,
                                       green_bias_n=GB_N, green_bias_min_pct=GB_PCT)),
    ]

    header = f"  {'Filter':<28} {'Trades':>7} {'WR':>7} {'SL':>4} {'BE':>4} {'Net PnL':>10}  {'Delta WR':>9}  Verdict"
    print(header)
    print(f"  {'-'*90}")
    base_row = f"  {'Baseline':<28} {total:>7} {wr:>6.1f}% {sl:>4} {be:>4} Rs{net:>8,.0f}"
    print(base_row)
    for vlabel, vparams in variants:
        r = _run_filter_test(vlabel, **vparams)
        if r is None:
            print(f"  {vlabel:<28}  No trades — too aggressive")
            continue
        dwr  = r['wr']  - wr
        dnet = r['net'] - net
        dtrd = r['total'] - total
        verdict = ("BETTER" if dwr >= 0 and dnet >= 0 else
                   "MIXED"  if dwr >= 0 or  dnet >= 0 else
                   "WORSE")
        print(f"  {vlabel:<28} {r['total']:>7} {r['wr']:>6.1f}% {r['sl']:>4} {r['be']:>4} "
              f"Rs{r['net']:>8,.0f}  {dwr:>+7.1f}%  {verdict}  (trades{dtrd:+d} net{dnet:+,.0f})")
        # show which signals were filtered
        base_keys = set(zip(tdf['date'].astype(str), tdf['entry'].round(1)))
        var_keys  = set(zip(r['df']['date'].astype(str), r['df']['entry'].round(1)))
        skip_keys = base_keys - var_keys
        if skip_keys:
            skip_rows = tdf[tdf.apply(lambda row: (str(row['date']), round(row['entry'],1)) in skip_keys, axis=1)]
            for _, sr in skip_rows.iterrows():
                print(f"      FILTERED: {str(sr['date']):<12} {str(sr.get('day','')):<10} "
                      f"entry={sr['entry']:.1f}  outcome={sr['outcome']}  PnL=Rs{sr['pnl_rs']:.0f}")
    print(sep)

    # ── GRAND TOTAL (main + both Tuesday windows) ─────────────────────────
    print()
    print(sep)
    print("  GRAND TOTAL — MAIN + TUESDAY (MORNING + EVENING)")
    print(sep)
    grand_total = total + at_total
    grand_wins  = wins  + at_wins
    grand_net   = net   + at_net
    grand_wr    = grand_wins / grand_total * 100 if grand_total else 0
    print(f"  Trades  : {grand_total}  (Main={total}  Tue-Morn={len(m_trades)}  Tue-Eve={len(e_trades)})")
    print(f"  Win Rate: {grand_wr:.1f}%")
    print(f"  Net PnL : Rs{grand_net:,.0f}  (1 lot x {LOT_SIZE})")
    print(f"  Monthly : Rs{grand_net/3:,.0f}/month")
    print(sep)

if __name__ == "__main__":
    main()
