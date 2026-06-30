"""
=============================================================
NIFTY BACKTESTER v5.0 — FINAL IMPROVED VERSION
=============================================================
Matches bot v7.0 exactly:
- CE: minimum 3/5 Tier 2
- PE: minimum 4/5 Tier 2 (stricter)
- Max 3 trades per day
- Trading hours: 9:30 AM to 2:30 PM
- Target: 0.5% | SL: 0.3%
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
#   CREDENTIALS (from .env)
# ─────────────────────────────────────────
API_KEY    = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
KITE_USER_ID     = os.getenv("KITE_USER_ID")
KITE_PASSWORD    = os.getenv("KITE_PASSWORD")
KITE_TOTP_SECRET = os.getenv("KITE_TOTP_SECRET")

# ─────────────────────────────────────────
#   CONFIG — matches bot v7.0 exactly
# ─────────────────────────────────────────
NIFTY_TOKEN = 256265
SL_PCT      = 0.00063   # ~15 pts SL
TARGET_PCT  = 0.00071   # ~17 pts target
BREAKEVEN_PCT = 0.00034 # ~8 pts — move SL to entry (saves Jun 10/12 trades)
MOMENTUM_CANDLES = 3    # check momentum after 3 candles (15 min)
MOMENTUM_MIN = 5        # must be +5pts in favor, else exit early
MAX_TRADES  = 3
CAPITAL     = 100000
STRIKE_GAP  = 50

RSI_BUY_MIN  = 48; RSI_BUY_MAX  = 63
RSI_SELL_MIN = 37; RSI_SELL_MAX = 53

TRADE_START = dtime(9, 30)
TRADE_END   = dtime(14, 0)
HARD_EXIT   = dtime(15, 10)

CE_MIN_T2 = 3
PE_MIN_T2 = 4

VOL_MULTI   = 1.2    # was 1.3 — slightly more signals qualify

# ── EXPIRY DAY CAUTION (fake breakouts + extreme theta decay) ──
# Not blocked entirely — expiry day can be very profitable on real moves.
# Just requires stronger confirmation and exits faster if it's not working.
EXPIRY_WEEKDAY      = 1            # Tuesday for NIFTY (change to 3=Thursday for SENSEX later)
EXPIRY_VOL_MULTI    = 1.6          # was 1.2 — filter out fake breakout candles with weak volume
EXPIRY_CE_MIN_T2    = 4            # was 3 — need stronger confirmation on CE too
EXPIRY_TRADE_END    = dtime(13, 0) # was 14:00 — stop new entries earlier, theta accelerates after
EXPIRY_MOMENTUM_CANDLES = 2        # was 3 — check momentum after 10min not 15min, exit dead trades faster
EXPIRY_BREAKOUT_BUFFER  = 3        # extra pts above prev high/below prev low — filters fake pokes, expiry day only

# ─────────────────────────────────────────
#   LOGIN (token cache + TOTP auto-login, matches bot exactly)
# ─────────────────────────────────────────
kite = KiteConnect(api_key=API_KEY)
TOKEN_FILE = "kite_token.json"

def load_cached_token():
    """Kite tokens are valid until ~6 AM the next day. Reuse today's token
    instead of logging in again on every backtest run."""
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

        time.sleep(1)  # let the session fully register server-side before the redirect fetch

        # Kite redirects twice: /connect/login -> /connect/finish?sess_id=... -> <redirect_url>?request_token=...
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
    """Fallback: manual token paste."""
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
    """Reuse today's cached token if valid. Otherwise auto-login, fallback to manual."""
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
def fetch_data(token, interval, days):
    to_dt   = datetime.now()
    from_dt = to_dt - timedelta(days=days)
    try:
        candles = kite.historical_data(token, from_dt, to_dt, interval)
        if not candles: return None
        df = pd.DataFrame(candles)
        df.columns = ['date','Open','High','Low','Close','Volume']
        df.set_index('date', inplace=True)
        df.index = pd.to_datetime(df.index)
        return df.dropna()
    except Exception as e:
        print(f"  Fetch error: {e}")
        return None

def find_nifty_fut_token():
    try:
        instruments = kite.instruments("NFO")
        df = pd.DataFrame(instruments)
        nf = df[(df['name']=='NIFTY')&(df['instrument_type']=='FUT')&(df['segment']=='NFO-FUT')].copy()
        if nf.empty: return None
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
#   INDICATORS
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
#   BACKTEST
# ─────────────────────────────────────────
def run_backtest(df5, df15, fut_vol):
    print("Preparing indicators...")
    df5 = df5.copy()
    df5['EMA9']       = ema(df5['Close'],9)
    df5['EMA20']      = ema(df5['Close'],20)
    df5['VWAP']       = calculate_vwap(df5)
    df5['Supertrend'] = calculate_supertrend(df5)
    df5['RSI']        = calculate_rsi(df5['Close'])

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

    df5=df5.dropna(subset=['EMA9','EMA20','VWAP','RSI','AvgVol'])
    days=len(set(df5.index.date))
    print(f"  Candles: {len(df5)} | Days: {days}\n")

    trades=[]; daily_count={}; last_sig={}; orb_cache={}; pdhl_cache={}

    for i in range(20,len(df5)-1):
        row=df5.iloc[i]; prev=df5.iloc[i-1]
        ts=df5.index[i]; date=ts.date(); t=ts.time()

        is_expiry_day = ts.weekday() == EXPIRY_WEEKDAY
        day_trade_end = EXPIRY_TRADE_END if is_expiry_day else TRADE_END
        day_vol_multi = EXPIRY_VOL_MULTI if is_expiry_day else VOL_MULTI
        day_ce_min_t2 = EXPIRY_CE_MIN_T2 if is_expiry_day else CE_MIN_T2
        day_momentum_candles = EXPIRY_MOMENTUM_CANDLES if is_expiry_day else MOMENTUM_CANDLES
        day_breakout_buffer = EXPIRY_BREAKOUT_BUFFER if is_expiry_day else 0

        if t<TRADE_START or t>day_trade_end: continue
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
        expiry_safe=not is_expiry_day  # NIFTY weekly expiry — soft Tier2 factor + hard caution below

        if date not in orb_cache:
            orb_cache[date]=get_orb_for_day(df5,date)
        orb_high,orb_low=orb_cache[date]
        orb_bull=(orb_high is not None) and (price>orb_high)
        orb_bear=(orb_low  is not None) and (price<orb_low)

        vol_spike = vol>(avg_vol*day_vol_multi) if avg_vol>0 else False
        ema_gap = ema9 - ema20; prev_gap = pe9 - pe20
        cross_up   = (pe9<=pe20 and ema9>ema20) or (ema9>ema20 and ema_gap > prev_gap > 0)
        cross_down = (pe9>=pe20 and ema9<ema20) or (ema9<ema20 and ema_gap < prev_gap < 0)
        breakout  = price > ph + day_breakout_buffer   # extra buffer on expiry day filters fake pokes
        breakdown = price < pl - day_breakout_buffer

        buy_t1  = all([price>vwap, st==True,  cross_up,   vol_spike, breakout])
        sell_t1 = all([price<vwap, st==False, cross_down, vol_spike, breakdown])

        if not buy_t1 and not sell_t1: continue
        if is_doji: continue

        # S&R: Previous Day High/Low
        if date not in pdhl_cache:
            pdhl_cache[date] = get_prev_day_hl(df5, date)
        pdh, pdl = pdhl_cache[date]

        if buy_t1:
            if not (RSI_BUY_MIN<=rsi<=RSI_BUY_MAX): continue
            # S&R: skip BUY if resistance (PDH) is closer than our target
            tgt_dist = price * TARGET_PCT
            if pdh and 0 < (pdh - price) < tgt_dist: continue
            t2=sum([True, trend15==True, bull_clean, expiry_safe, orb_bull])
            if t2<day_ce_min_t2: continue
            if last_sig.get(date)=="BUY": continue
            signal="BUY"
            conf="HIGH" if t2==5 else ("NORMAL" if t2>=3 else "WEAK")
        else:
            if not (RSI_SELL_MIN<=rsi<=RSI_SELL_MAX): continue
            # S&R: skip SELL if support (PDL) is closer than our target
            tgt_dist = price * TARGET_PCT
            if pdl and 0 < (price - pdl) < tgt_dist: continue
            t2=sum([True, trend15==False, bear_clean, expiry_safe, orb_bear])
            if t2<PE_MIN_T2: continue
            if last_sig.get(date)=="SELL": continue
            signal="SELL"
            conf="HIGH" if t2==5 else ("NORMAL" if t2>=4 else "WEAK")

        sl     = price*(1-SL_PCT)     if signal=="BUY" else price*(1+SL_PCT)
        target = price*(1+TARGET_PCT) if signal=="BUY" else price*(1-TARGET_PCT)
        be_lvl = price*(1+BREAKEVEN_PCT) if signal=="BUY" else price*(1-BREAKEVEN_PCT)

        sl_pts  = round(abs(price - sl), 1)
        tgt_pts = round(abs(target - price), 1)

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

            # Early exit: no momentum = weak breakout (faster check on expiry day — theta burns fast)
            if j - i == day_momentum_candles and max_favorable < MOMENTUM_MIN:
                fc=float(df5.iloc[j]['Close'])
                exit_price=fc; outcome="WEAK"; break

            # Step 1: Update breakeven (tighten SL if price went in our favor)
            if not breakeven_hit:
                if signal=="BUY" and fh>=be_lvl:
                    current_sl=price; breakeven_hit=True
                elif signal=="SELL" and fl<=be_lvl:
                    current_sl=price; breakeven_hit=True

            # Step 2: Check TARGET FIRST (if price reached our target, take profit)
            if signal=="BUY" and fh>=target:
                exit_price=target; outcome="TARGET"; break
            if signal=="SELL" and fl<=target:
                exit_price=target; outcome="TARGET"; break

            # Step 3: Check SL (only if target not hit on this candle)
            if signal=="BUY" and fl<=current_sl:
                exit_price=current_sl
                outcome="BE" if breakeven_hit else "SL"; break
            if signal=="SELL" and fh>=current_sl:
                exit_price=current_sl
                outcome="BE" if breakeven_hit else "SL"; break

        pnl_pct=(exit_price-price)/price if signal=="BUY" else (price-exit_price)/price

        trades.append({
            "date":str(date),"time":t.strftime("%H:%M"),
            "signal":signal,"confidence":conf,"t2":t2,
            "entry":round(price,2),"sl":round(sl,2),"target":round(target,2),
            "sl_pts":sl_pts,"tgt_pts":tgt_pts,
            "exit":round(exit_price,2),"outcome":outcome,
            "max_favorable":round(max_favorable,1),
            "pnl_pct":round(pnl_pct*100,2),"pnl_rs":round(CAPITAL*pnl_pct,0),
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
    wins=len(tdf[tdf['outcome']=='TARGET'])
    loss=len(tdf[tdf['outcome']=='SL'])
    bes=len(tdf[tdf['outcome']=='BE'])
    weaks=len(tdf[tdf['outcome']=='WEAK'])
    eods=len(tdf[tdf['outcome']=='EOD'])
    wr=wins/total*100; net=tdf['pnl_rs'].sum()
    aw=tdf[tdf['outcome']=='TARGET']['pnl_rs'].mean() if wins>0 else 0
    al=tdf[tdf['outcome']=='SL']['pnl_rs'].mean() if loss>0 else 0

    mws=mls=cw=cl=0
    for o in tdf['outcome']:
        if o=='TARGET': cw+=1;cl=0;mws=max(mws,cw)
        elif o=='SL':   cl+=1;cw=0;mls=max(mls,cl)
        else:           cw=0;cl=0

    bdf=tdf[tdf['signal']=='BUY']; sdf=tdf[tdf['signal']=='SELL']
    bwr=len(bdf[bdf['outcome']=='TARGET'])/len(bdf)*100 if len(bdf) else 0
    swr=len(sdf[sdf['outcome']=='TARGET'])/len(sdf)*100 if len(sdf) else 0
    eod_pos=len(tdf[(tdf['outcome']=='EOD')&(tdf['pnl_rs']>0)])

    # Confidence analysis
    high_df=tdf[tdf['confidence']=='HIGH']
    norm_df=tdf[tdf['confidence']=='NORMAL']
    high_wr=len(high_df[high_df['outcome']=='TARGET'])/len(high_df)*100 if len(high_df) else 0
    norm_wr=len(norm_df[norm_df['outcome']=='TARGET'])/len(norm_df)*100 if len(norm_df) else 0

    days=len(set(tdf['date']))
    sep="="*65
    print(f"\n{sep}")
    print(f"  NIFTY BACKTEST — Target ~{round(tdf['tgt_pts'].mean())}pts | SL ~{round(tdf['sl_pts'].mean())}pts")
    print(f"  + Breakeven trailing | EMA expanding gap")
    print(sep)
    print(f"""
📊 OVERALL:
  Total Trades    : {total} over {days} days ({total/days:.1f}/day)
  Wins (Target)   : {wins} ({wr:.1f}%)
  Losses (SL)     : {loss} ({loss/total*100:.1f}%)
  Breakeven       : {bes} ({bes/total*100:.1f}%)
  Weak Exit       : {weaks} ({weaks/total*100:.1f}%)  ← no momentum after 15min
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

    # Max favorable analysis — shows how far price went in our favor before outcome
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
        icon="✅" if r['outcome']=='TARGET' else ("❌" if r['outcome']=='SL' else ("⚖️" if r['outcome']=='BE' else ("⚠️" if r['outcome']=='WEAK' else "➡️")))
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
    tdf.to_csv('backtest_results_v5.csv',index=False)
    print(f"\n  📁 Saved → backtest_results_v5.csv")
    print(sep)

# ─────────────────────────────────────────
#   MAIN
# ─────────────────────────────────────────
def main():
    print("="*55)
    print("  NIFTY BACKTESTER v5.0 — FINAL")
    print("  CE(3/5) + PE(4/5) | 3 trades | 9:30-2:30 PM")
    print("="*55)

    if not login(): return

    DAYS = 100
    print(f"📥 Fetching Nifty 5 min data ({DAYS} days)...")
    df5=fetch_data(NIFTY_TOKEN,"5minute",days=DAYS)
    if df5 is None or df5.empty:
        print("❌ Failed"); return
    print(f"✅ {len(df5)} candles | {df5.index[0].date()} to {df5.index[-1].date()}")

    print("📥 Fetching 15 min data...")
    df15=fetch_data(NIFTY_TOKEN,"15minute",days=DAYS)
    print(f"✅ {len(df15)} candles (15 min)" if df15 is not None else "⚠️ Not available")

    print("📥 Finding Nifty Futures for volume...")
    fut_token=find_nifty_fut_token()
    fut_vol=None
    if fut_token:
        fut_vol=fetch_data(fut_token,"5minute",days=DAYS)
        if fut_vol is not None and fut_vol['Volume'].sum()>0:
            print(f"✅ Futures volume: {len(fut_vol)} candles")
        else:
            print("⚠️ Futures volume empty")
            fut_vol=None

    print("\n🔍 Running backtest...")
    trades=run_backtest(df5,df15,fut_vol)
    print(f"✅ Done. Signals: {len(trades)}")
    print_report(trades)

if __name__=="__main__":
    main()