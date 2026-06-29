"""
=============================================================
NIFTY SIGNAL BOT — MATCHES BACKTEST EXACTLY
=============================================================
Tier 1 (ALL 5): VWAP + Supertrend + EMA cross/expanding
                 + Volume spike + Breakout
Tier 2: 15min trend, candle, expiry, ORB
Target: ~17pts | SL: ~15pts | Breakeven: ~8pts
Weak exit: if <5pts after 15min
=============================================================
"""

from kiteconnect import KiteConnect
import pandas as pd
import numpy as np
import requests, time, webbrowser, os
from datetime import datetime, timedelta, time as dtime
from dotenv import load_dotenv
import warnings
warnings.filterwarnings('ignore')

load_dotenv()

# ─── CREDENTIALS (from .env) ───
API_KEY    = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
BOT_TOKEN  = os.getenv("BOT_TOKEN")
CHAT_ID    = os.getenv("CHAT_ID")

# ─── CONFIG — same as backtest ───
NIFTY_TOKEN = 256265
SL_PCT         = 0.00063
TARGET_PCT     = 0.00071
BREAKEVEN_PCT  = 0.00034  # ~8pts — matches backtest
MAX_TRADES     = 3
STRIKE_GAP     = 50
VIX_LIMIT      = 20

RSI_BUY_MIN = 48; RSI_BUY_MAX = 63
RSI_SELL_MIN = 37; RSI_SELL_MAX = 53
CE_MIN_T2 = 3; PE_MIN_T2 = 4
VOL_MULTI = 1.2

ORB_START    = dtime(9, 15)
MARKET_START = dtime(9, 30)
MARKET_END   = dtime(14, 0)
HARD_EXIT    = dtime(15, 10)

# ─── KITE ───
kite = KiteConnect(api_key=API_KEY)

def login():
    login_url = kite.login_url()
    print(f"\n🌐 Opening Zerodha login...\nURL: {login_url}")
    webbrowser.open(login_url)
    print("\n" + "="*55)
    print("Copy request_token from redirect URL")
    print("="*55)
    request_token = input("\nPaste request_token: ").strip()
    try:
        data = kite.generate_session(request_token, api_secret=API_SECRET)
        kite.set_access_token(data["access_token"])
        print("✅ Login successful!\n"); return True
    except Exception as e:
        print(f"❌ Login failed: {e}"); return False

def send_telegram(msg):
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print(f"\n{'='*50}\n[TG]\n{msg}\n{'='*50}"); return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"HTML"}, timeout=10)
    except Exception as e:
        print(f"TG error: {e}")

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

def get_expiry_info():
    day = datetime.now().weekday()
    if day == 0: return True,  "Monday 🟡"
    elif day == 1: return False, "Tuesday Expiry ⛔"
    elif day == 2: return True,  "Wednesday 🟢"
    elif day == 3: return True,  "Thursday 🟢"
    else: return True, "Friday 🟢"

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
    orb_high, orb_low = get_orb(df5)
    orb_bull = (orb_high is not None) and (price > orb_high)
    orb_bear = (orb_low  is not None) and (price < orb_low)

    vol_spike = vol > (avg_vol * VOL_MULTI) if avg_vol > 0 else False
    ema_gap = ema9 - ema20; prev_gap = pe9 - pe20
    cross_up   = (pe9<=pe20 and ema9>ema20) or (ema9>ema20 and ema_gap > prev_gap > 0)
    cross_down = (pe9>=pe20 and ema9<ema20) or (ema9<ema20 and ema_gap < prev_gap < 0)
    breakout  = price > ph
    breakdown = price < pl

    buy_t1  = all([price>vwap, st==True,  cross_up,   vol_spike, breakout])
    sell_t1 = all([price<vwap, st==False, cross_down, vol_spike, breakdown])

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
        if score < CE_MIN_T2:
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
    print("="*55)
    print("  NIFTY SIGNAL BOT — MATCHES BACKTEST")
    print("  Target ~17pts | SL ~15pts | BE ~10pts")
    print("  Tier1: VWAP+ST+EMA+Vol+Breakout")
    print("  Tier2: 15min+Candle+Expiry+ORB")
    print("="*55)

    if not login(): return

    send_telegram("🧪 TEST — Bot working!\n"
        f"🕒 {datetime.now().strftime('%d %b %Y %I:%M:%S %p')}")

    fut_token, fut_sym = find_nifty_fut_token()
    if fut_token: print(f"✅ Futures: {fut_sym}")
    else: print("⚠️ No futures — using spot volume")

    send_telegram(
        "🤖 <b>Nifty Signal Bot Started</b>\n\n"
        "📊 Tier1: VWAP + Supertrend + EMA + Volume + Breakout\n"
        "🎯 Target: ~17pts | SL: ~15pts\n"
        "⚖️ Breakeven at ~8pts\n"
        "⏰ 9:30 AM — 2:00 PM\n\n"
        "Watching Nifty 5 min chart...")

    trades_today = 0; last_date = None; last_dir = None
    orb_high = None; orb_low = None; orb_sent = False
    fut_vol = None; df15 = None

    while True:
        try:
            now = datetime.now(); ct = now.time(); cd = now.date()

            if last_date != cd:
                trades_today = 0; last_date = cd; last_dir = None
                orb_high = None; orb_low = None; orb_sent = False
                fut_vol = None; df15 = None
                print(f"\n📅 New day: {cd}")

            if ct < ORB_START:
                print(f"⏳ [{now.strftime('%H:%M')}] Before market")
                time.sleep(60); continue
            if ct > HARD_EXIT:
                print(f"🔕 Market closed"); time.sleep(300); continue
            if trades_today >= MAX_TRADES:
                print(f"🚫 Max trades done"); time.sleep(300); continue
            if ct > MARKET_END:
                print(f"⏰ No new trades after 2:00 PM"); time.sleep(300); continue

            df5 = fetch_data(NIFTY_TOKEN, "5minute", days=5)
            if df5 is None:
                print("❌ Data failed"); time.sleep(60); continue

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
                time.sleep(60); continue

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
                print(f"⚠️ VIX {vix:.1f} > {VIX_LIMIT}"); time.sleep(300); continue

            result = check_signals(df5, df15, fut_vol)
            signal, price, tier1, tier2, conf, rsi, expiry_label = result

            if price:
                print(f"  [{now.strftime('%H:%M')}] Nifty:{price:.2f} | RSI:{rsi:.1f if rsi else '?'} | Signal:{signal or 'None'}")

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

            time.sleep(300)

        except KeyboardInterrupt:
            print("\n⛔ Bot stopped.")
            send_telegram("⛔ Bot stopped."); break
        except Exception as e:
            print(f"❌ Error: {e}"); time.sleep(60)

if __name__ == "__main__":
    run_bot()
