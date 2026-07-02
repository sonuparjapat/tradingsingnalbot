"""
=============================================================
NIFTY MORNING BACKTEST — 9:30-11:00 Window, Relaxed Filters
=============================================================
Backtests the morning scanner's 4-condition signal set:
  Price vs VWAP + Supertrend + Breakout + Clean candle
Entry window: 9:30-11:00 AM only.
Exits: same SL / Target / Breakeven / Trailing stop / Hard exit
       as the main backtest (proven logic, same constants).
P&L: moneyness-aware delta model × LOT_SIZE (same as main backtest).
=============================================================
"""

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
# Note: NIFTY 50 (token 256265) is a cash index — Kite returns 0 volume for it.
# Volume spike cannot be used here. Signal quality relies on price-based conditions only.

# SL / Target / Breakeven — identical to morning scanner + main bot
SL_PCT        = 0.00063
TARGET_PCT    = 0.00071
BREAKEVEN_PCT = 0.00034
MOMENTUM_MIN  = 5
MOMENTUM_CANDLES = 3   # 15 min

# Premium P&L model — identical to main backtest
DELTA_BASE               = 0.5
DELTA_SCALE_PCT          = 0.0048
EXPIRY_THETA_HAIRCUT_PCT = 0.15

# Trailing stop — identical to main backtest
TRAIL_TRIGGER_MULT = 1.5
TRAIL_STEP_MULT    = 0.6

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

# ─── INDICATORS (identical to main backtest) ───
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

def estimate_premium_pts(entry_price, exit_price, signal, is_expiry_day):
    spot_move = (exit_price-entry_price) if signal=="BUY" else (entry_price-exit_price)
    delta_scale_pts = entry_price * DELTA_SCALE_PCT
    delta_exit = DELTA_BASE + (1-DELTA_BASE)*np.tanh(spot_move/delta_scale_pts)
    avg_delta  = (DELTA_BASE+delta_exit)/2
    premium_pts = spot_move*avg_delta
    if is_expiry_day and premium_pts > 0:
        premium_pts *= (1-EXPIRY_THETA_HAIRCUT_PCT)
    return premium_pts

# ─── BACKTEST ───
def run_backtest(df5, days=60):
    print("Preparing indicators...")
    df5 = df5.copy()
    df5['VWAP']       = calculate_vwap(df5)
    df5['Supertrend'] = calculate_supertrend(df5)
    df5 = df5.dropna(subset=['VWAP'])

    total_days = len(set(df5.index.date))
    print(f"  Candles: {len(df5)} | Days: {total_days}\n")

    trades = []; last_sig = {}

    for i in range(20, len(df5)-1):
        row = df5.iloc[i]; prev = df5.iloc[i-1]
        ts  = df5.index[i]; date = ts.date(); t = ts.time()

        if t < ENTRY_START or t > ENTRY_END: continue
        if ts.weekday() == EXPIRY_WEEKDAY: continue

        # One trade per direction per day
        is_expiry = (ts.weekday() == EXPIRY_WEEKDAY)

        price = float(row['Close'])
        o,h,l,c = float(row['Open']),float(row['High']),float(row['Low']),float(row['Close'])
        vwap  = float(row['VWAP'])
        st    = bool(row['Supertrend'])
        ph    = float(prev['High']); pl = float(prev['Low'])

        is_doji, bull_clean, bear_clean = analyze_candle(o,h,l,c)
        if is_doji: continue

        breakout  = price > ph
        breakdown = price < pl

        buy_ok  = all([price>vwap, st==True,  breakout,  bull_clean])
        sell_ok = all([price<vwap, st==False, breakdown, bear_clean])

        if buy_ok and last_sig.get(date) != "BUY":
            signal = "BUY"
        elif sell_ok and last_sig.get(date) != "SELL":
            signal = "SELL"
        else:
            continue

        last_sig[date] = signal

        sl_dist = price * SL_PCT
        tgt_dist = price * TARGET_PCT
        be_dist  = price * BREAKEVEN_PCT

        sl      = price - sl_dist if signal=="BUY" else price + sl_dist
        target  = price + tgt_dist if signal=="BUY" else price - tgt_dist

        trail_trigger_dist = be_dist * TRAIL_TRIGGER_MULT
        trail_step_dist    = be_dist * TRAIL_STEP_MULT

        current_sl = sl; outcome = "EOD"; exit_price = price
        breakeven_hit = False; max_favorable = 0

        for j in range(i+1, len(df5)):
            ft = df5.index[j]; ft_t = ft.time()
            if ft.date() != date or ft_t > HARD_EXIT:
                exit_price = float(df5.iloc[j]['Close']) if j < len(df5) else price
                outcome = "EOD"; break

            fh = float(df5.iloc[j]['High']); fl = float(df5.iloc[j]['Low'])

            max_favorable = max(max_favorable, fh-price if signal=="BUY" else price-fl)

            # Weak momentum check
            if j-i == MOMENTUM_CANDLES and max_favorable < MOMENTUM_MIN:
                exit_price = float(df5.iloc[j]['Close']); outcome = "WEAK"; break

            # Breakeven
            if not breakeven_hit:
                if signal=="BUY"  and fh >= price + be_dist: current_sl=price; breakeven_hit=True
                if signal=="SELL" and fl <= price - be_dist: current_sl=price; breakeven_hit=True

            # Trailing stop
            if max_favorable >= trail_trigger_dist:
                trail_dist = max_favorable - trail_step_dist
                if signal=="BUY":  current_sl = max(current_sl, price + trail_dist)
                else:              current_sl = min(current_sl, price - trail_dist)
                breakeven_hit = True

            # Target
            if signal=="BUY"  and fh >= target: exit_price=target; outcome="TARGET"; break
            if signal=="SELL" and fl <= target:  exit_price=target; outcome="TARGET"; break

            # SL
            if signal=="BUY"  and fl <= current_sl:
                exit_price=current_sl
                outcome = "TRAIL" if current_sl>price else ("BE" if breakeven_hit else "SL"); break
            if signal=="SELL" and fh >= current_sl:
                exit_price=current_sl
                outcome = "TRAIL" if current_sl<price else ("BE" if breakeven_hit else "SL"); break

        premium_pts = estimate_premium_pts(price, exit_price, signal, is_expiry)
        pnl_rs      = round(premium_pts * LOT_SIZE, 0)

        trades.append({
            "date":    str(date),
            "time":    t.strftime("%H:%M"),
            "day":     ts.strftime("%A"),
            "signal":  signal,
            "entry":   round(price, 2),
            "sl_pts":     round(sl_dist, 1),
            "tgt_pts":    round(tgt_dist, 1),
            "exit":    round(exit_price, 2),
            "outcome": outcome,
            "max_fav": round(max_favorable, 1),
            "premium_pts": round(premium_pts, 1),
            "pnl_rs":  pnl_rs,
            "expiry":  is_expiry,
        })

    return trades

# ─── REPORT ───
def print_report(trades, days):
    if not trades:
        print("\n❌ No morning signals found in this period."); return

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

    mws=mls=cw=cl=0
    for o in tdf['outcome']:
        if o in ('TARGET','TRAIL'): cw+=1;cl=0;mws=max(mws,cw)
        elif o=='SL':               cl+=1;cw=0;mls=max(mls,cl)
        else:                       cw=0;cl=0

    bdf=tdf[tdf['signal']=='BUY']; sdf=tdf[tdf['signal']=='SELL']
    bwr=len(bdf[bdf['outcome'].isin(['TARGET','TRAIL'])])/len(bdf)*100 if len(bdf) else 0
    swr=len(sdf[sdf['outcome'].isin(['TARGET','TRAIL'])])/len(sdf)*100 if len(sdf) else 0

    days_traded = len(set(tdf['date']))
    sep = "="*65

    print(f"\n{sep}")
    print(f"  NIFTY MORNING BACKTEST — 09:30-11:00 Window")
    print(f"  Conditions: VWAP + Supertrend + Breakout + Clean candle")
    print(f"  Exits: SL/Target/Breakeven/Trailing/Hard exit 3:10 PM")
    print(f"  P&L: delta+theta premium model, LOT_SIZE={LOT_SIZE}")
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
    print(f"{'Date':<12}{'Time':<7}{'Day':<12}{'Sig':<6}{'Entry':<10}{'SL':<6}{'TGT':<6}{'Exit':<10}{'MaxFav':<8}{'Prem':<7}{'P&L₹':<9}Result")
    print("-"*95)
    icons = {'TARGET':'✅','TRAIL':'🔒','SL':'❌','BE':'⚖️','WEAK':'⚠️','EOD':'➡️'}
    for _, r in tdf.iterrows():
        icon = icons.get(r['outcome'],'➡️')
        exp  = " ⚡Exp" if r['expiry'] else ""
        print(f"{r['date']:<12}{r['time']:<7}{r['day']:<12}{r['signal']:<6}{r['entry']:<10}"
              f"{r['sl_pts']:<6}{r['tgt_pts']:<6}{r['exit']:<10}"
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
        print(f"  💡 BUY signals much better ({bwr:.0f}% vs {swr:.0f}%) — consider CE-only mornings")
    if mls >= 3:
        print(f"  ⚠️  Stop after 3 consecutive losses")

    print(sep)
    out_file = 'backtest_results_morning.csv'
    tdf.to_csv(out_file, index=False)
    print(f"\n  📁 Saved → {out_file}")
    print(sep)

# ─── MAIN ───
def main():
    print("="*55)
    print("  NIFTY MORNING BACKTEST")
    print("  Window: 09:30-11:00 | 4 conditions only")
    print("  VWAP + Supertrend + Breakout + Clean candle")
    print("="*55)

    if not login(): return

    DAYS = 60
    print(f"📥 Fetching {DAYS} days of Nifty 5 min data...")
    df5 = fetch_data(NIFTY_TOKEN, "5minute", days=DAYS)
    if df5 is None or df5.empty:
        print("❌ Failed to fetch data"); return
    print(f"✅ {len(df5)} candles | {df5.index[0].date()} → {df5.index[-1].date()}\n")

    print("🔍 Running morning backtest...")
    trades = run_backtest(df5, days=DAYS)
    print(f"✅ Done. Signals found: {len(trades)}")
    print_report(trades, DAYS)

if __name__ == "__main__":
    main()
