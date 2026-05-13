import asyncio
import logging
import json
import httpx
import os
import pytz
import random
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ── CONFIG ─────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ.get(“TELEGRAM_TOKEN”, “YOUR_BOT_TOKEN_HERE”)
ANTHROPIC_KEY  = os.environ.get(“ANTHROPIC_KEY”,  “YOUR_CLAUDE_API_KEY_HERE”)
GEMINI_KEY     = os.environ.get(“GEMINI_KEY”,      “YOUR_GEMINI_API_KEY_HERE”)
OANDA_TOKEN    = os.environ.get(“OANDA_TOKEN”,     “”)
OANDA_ACCOUNT  = os.environ.get(“OANDA_ACCOUNT”,   “”)
SGT            = pytz.timezone(“Asia/Singapore”)
CHAT_ID        = 192844206  # Aden Yang

# ── RISK CONFIG ────────────────────────────────────────────────────────────────

ACCOUNT_BALANCE = float(os.environ.get(“ACCOUNT_BALANCE”, “2000”))
PIP_VALUE       = float(os.environ.get(“PIP_VALUE”,       “0.04”))
MAX_RISK_PCT    = float(os.environ.get(“MAX_RISK_PCT”,    “1.0”))
MAX_DAILY_LOSS  = float(os.environ.get(“MAX_DAILY_LOSS”,  “2.0”))
SL_BUFFER_PIPS  = int(os.environ.get(“SL_BUFFER_PIPS”,   “5”))
runtime_balance = {“value”: ACCOUNT_BALANCE}

logging.basicConfig(format=”%(asctime)s | %(levelname)s | %(message)s”, level=logging.INFO)
logger = logging.getLogger(**name**)

# ── MOTIVATION QUOTES ──────────────────────────────────────────────────────────

QUOTES = [
(“The secret of getting ahead is getting started.”, “Mark Twain”),
(“Success is not final, failure is not fatal: it is the courage to continue.”, “Churchill”),
(“The market transfers money from the impatient to the patient.”, “Warren Buffett”),
(“In investing, what is comfortable is rarely profitable.”, “Robert Arnott”),
(“The goal of a successful trader is to make the best trades. Money is secondary.”, “Alexander Elder”),
(“Small daily improvements over time lead to stunning results.”, “Robin Sharma”),
(“Risk comes from not knowing what you are doing.”, “Warren Buffett”),
(“Compound interest is the eighth wonder of the world.”, “Albert Einstein”),
(“Cut your losses short and let your profits run.”, “Trading Proverb”),
(“The trend is your friend until the end when it bends.”, “Ed Seykota”),
(“Plan the trade and trade the plan.”, “Trading Proverb”),
(“Discipline is the bridge between goals and accomplishment.”, “Jim Rohn”),
(“Trade what you see, not what you think.”, “Trading Proverb”),
(“Losses are tuition fees for the trading school.”, “Unknown”),
(“Your first loss is your best loss.”, “Trading Proverb”),
(”$1M is not a dream. It is a plan executed daily.”, “Unknown”),
(“0.7% a day keeps poverty away.”, “Aden Yang 2026”),
(“Patience is the most valuable trait of a good trader.”, “Jesse Livermore”),
(“It is not the mountain we conquer but ourselves.”, “Edmund Hillary”),
(“Structure your SL — never random, always logical.”, “Aden Yang 2026”),
]

def get_quote() -> str:
q, a = random.choice(QUOTES)
return f’*”{q}”*\n— {a}’

# ── TIME HELPERS ───────────────────────────────────────────────────────────────

def sgt_now() -> str:
return datetime.now(SGT).strftime(”%d %b %H:%M SGT”)

def sgt_full() -> str:
return datetime.now(SGT).strftime(”%B %d, %Y %H:%M SGT”)

def get_session_label(news_filter: bool = False, risk_level: str = “”) -> str:
“”“Returns time + session quality label + news risk override”””
now = datetime.now(SGT)
hour = now.hour
minute = now.minute
time_str = now.strftime(”%d %b %H:%M SGT”)
weekday = now.weekday()

```
# News override — always check first!
if news_filter:
    return f"{time_str} | 🚨 NEWS RISK — DO NOT TRADE!"

if risk_level == "HIGH":
    return f"{time_str} | 🔴 HIGH IMPACT NEWS TODAY — be very careful!"

# Known high impact scheduled times (SGT)
# NFP Friday 8:30PM
if weekday == 4 and hour == 20 and minute >= 15:
    return f"{time_str} | 💥 NFP ZONE — avoid until settled!"
# Any day 8:30PM = Jobless claims Thu or major US data
if weekday == 3 and hour == 20 and 15 <= minute <= 45:
    return f"{time_str} | ⚠️ Jobless Claims zone — caution!"
# ADP Wednesday 8:15PM
if weekday == 2 and hour == 20 and 0 <= minute <= 30:
    return f"{time_str} | ⚠️ ADP Data zone — caution!"

# Normal session windows
if hour == 3 and minute < 30:
    label = "🟢 London Open — prime window!"
elif 3 <= hour < 5:
    label = "🟢 London session — good"
elif hour == 5:
    label = "⚠️ London lunch — low volume"
elif 5 <= hour < 7:
    label = "⚠️ Quiet period — be careful"
elif 7 <= hour < 8:
    label = "🟢 London afternoon — good"
elif hour == 7 and minute >= 30:
    label = "🟡 Pre-NY — wait for open"
elif hour == 8 and minute < 30:
    label = "💎 NY Open — best window!"
elif 8 <= hour < 11:
    label = "💎 NY+London overlap — GOLDEN!"
elif hour == 10 and minute >= 30:
    label = "🟡 London closing — reduce size"
elif 11 <= hour < 12:
    label = "🔴 Post-overlap — stop soon"
elif 12 <= hour < 15:
    label = "🔴 Asian session — avoid"
else:
    label = "🔴 Low volume — wait for London"

return f"{time_str} | {label}"
```

# ── OANDA CANDLES + FULL TECHNICAL INDICATORS ─────────────────────────────────

async def get_oanda_candles(count: int = 250, granularity: str = “H1”) -> dict:
“”“Fetch OANDA candles and return OHLC data”””
if not OANDA_TOKEN:
return {“closes”: [], “highs”: [], “lows”: [], “opens”: []}
try:
async with httpx.AsyncClient(timeout=15) as client:
resp = await client.get(
f”https://api-fxtrade.oanda.com/v3/instruments/XAU_USD/candles”
f”?count={count}&granularity={granularity}&price=M”,
headers={“Authorization”: f”Bearer {OANDA_TOKEN}”}
)
data = resp.json()
candles = [c for c in data.get(“candles”, []) if c.get(“complete”, True)]
return {
“closes”: [float(c[“mid”][“c”]) for c in candles],
“highs”:  [float(c[“mid”][“h”]) for c in candles],
“lows”:   [float(c[“mid”][“l”]) for c in candles],
“opens”:  [float(c[“mid”][“o”]) for c in candles],
}
except Exception as e:
logger.warning(f”Candles failed: {e}”)
return {“closes”: [], “highs”: [], “lows”: [], “opens”: []}

# ── INDICATOR CALCULATIONS ─────────────────────────────────────────────────────

def calc_sma(data: list, period: int) -> float:
if len(data) < period:
return 0.0
return round(sum(data[-period:]) / period, 3)

def calc_ema(data: list, period: int) -> list:
if len(data) < period:
return []
k = 2 / (period + 1)
ema = [sum(data[:period]) / period]
for price in data[period:]:
ema.append(price * k + ema[-1] * (1 - k))
return ema

def calc_rsi(closes: list, period: int = 14) -> float:
if len(closes) < period + 1:
return 50.0
deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
gains  = [d if d > 0 else 0 for d in deltas[-period:]]
losses = [-d if d < 0 else 0 for d in deltas[-period:]]
avg_gain = sum(gains) / period
avg_loss = sum(losses) / period
if avg_loss == 0:
return 100.0
rs = avg_gain / avg_loss
return round(100 - (100 / (1 + rs)), 2)

def calc_macd(closes: list) -> dict:
if len(closes) < 26:
return {“macd”: 0, “signal”: 0, “histogram”: 0, “trend”: “neutral”}
ema12 = calc_ema(closes, 12)
ema26 = calc_ema(closes, 26)
min_len = min(len(ema12), len(ema26))
macd_line = [ema12[-min_len+i] - ema26[-min_len+i] for i in range(min_len)]
signal_line = calc_ema(macd_line, 9) if len(macd_line) >= 9 else [0]
macd_val  = round(macd_line[-1], 3)
signal_val = round(signal_line[-1], 3)
hist = round(macd_val - signal_val, 3)
# Crossover
prev_hist = round(macd_line[-2] - signal_line[-2], 3) if len(macd_line) > 1 and len(signal_line) > 1 else hist
if hist > 0 and prev_hist <= 0:
trend = “BULLISH CROSSOVER”
elif hist < 0 and prev_hist >= 0:
trend = “BEARISH CROSSOVER”
elif macd_val > 0 and hist > 0:
trend = “bullish”
elif macd_val < 0 and hist < 0:
trend = “bearish”
else:
trend = “neutral”
return {“macd”: macd_val, “signal”: signal_val, “histogram”: hist, “trend”: trend}

def calc_bollinger(closes: list, period: int = 20, std_dev: float = 2.0) -> dict:
if len(closes) < period:
return {“upper”: 0, “middle”: 0, “lower”: 0, “position”: “middle”, “squeeze”: False}
sma   = sum(closes[-period:]) / period
std   = (sum((x - sma) ** 2 for x in closes[-period:]) / period) ** 0.5
upper = round(sma + std_dev * std, 3)
lower = round(sma - std_dev * std, 3)
middle = round(sma, 3)
price = closes[-1]
band_width = upper - lower
if price >= upper * 0.999:
position = “AT UPPER BAND — overbought”
elif price <= lower * 1.001:
position = “AT LOWER BAND — oversold”
elif price > middle:
position = “above middle”
else:
position = “below middle”
squeeze = band_width < (middle * 0.01)  # Squeeze if bands < 1% of price
return {“upper”: upper, “middle”: middle, “lower”: lower,
“position”: position, “squeeze”: squeeze, “bandwidth”: round(band_width, 2)}

def calc_fibonacci(highs: list, lows: list, lookback: int = 50) -> dict:
if len(highs) < lookback or len(lows) < lookback:
return {}
recent_high = max(highs[-lookback:])
recent_low  = min(lows[-lookback:])
diff = recent_high - recent_low
return {
“high”: round(recent_high, 2),
“low”:  round(recent_low, 2),
“fib_236”: round(recent_high - 0.236 * diff, 2),
“fib_382”: round(recent_high - 0.382 * diff, 2),
“fib_500”: round(recent_high - 0.500 * diff, 2),
“fib_618”: round(recent_high - 0.618 * diff, 2),
“fib_786”: round(recent_high - 0.786 * diff, 2),
}

def nearest_fib(price: float, fib: dict) -> str:
if not fib:
return “N/A”
levels = {
“23.6%”: fib.get(“fib_236”, 0),
“38.2%”: fib.get(“fib_382”, 0),
“50.0%”: fib.get(“fib_500”, 0),
“61.8%”: fib.get(“fib_618”, 0),
“78.6%”: fib.get(“fib_786”, 0),
}
closest = min(levels.items(), key=lambda x: abs(x[1] - price))
dist = abs(closest[1] - price)
if dist < 15:
return f”${closest[1]} ({closest[0]}) ← NEAR!”
return f”${closest[1]} ({closest[0]})”

def detect_candle_pattern(opens: list, highs: list, lows: list, closes: list) -> str:
if len(closes) < 3:
return “none”
o, h, l, c = opens[-1], highs[-1], lows[-1], closes[-1]
po, ph, pl, pc = opens[-2], highs[-2], lows[-2], closes[-2]
body = abs(c - o)
total_range = h - l if h != l else 0.001
upper_wick = h - max(c, o)
lower_wick = min(c, o) - l

```
# Doji
if body / total_range < 0.1:
    return "doji (indecision)"
# Hammer (bullish)
if lower_wick > body * 2 and upper_wick < body * 0.5 and c > o:
    return "hammer (bullish)"
# Shooting star (bearish)
if upper_wick > body * 2 and lower_wick < body * 0.5 and c < o:
    return "shooting star (bearish)"
# Bullish engulfing
if c > o and pc < po and c > po and o < pc:
    return "bullish engulfing ✅"
# Bearish engulfing
if c < o and pc > po and c < po and o > pc:
    return "bearish engulfing ❌"
# Bullish candle
if c > o and body / total_range > 0.6:
    return "strong bullish candle"
# Bearish candle
if c < o and body / total_range > 0.6:
    return "strong bearish candle"
return "no clear pattern"
```

# ── FULL TECHNICAL ANALYSIS ────────────────────────────────────────────────────

async def get_technical_analysis() -> dict:
“”“Calculate all technical indicators from OANDA candles”””
candles = await get_oanda_candles(250, “H1”)
closes = candles[“closes”]
highs  = candles[“highs”]
lows   = candles[“lows”]
opens  = candles[“opens”]

```
if len(closes) < 50:
    return {"available": False}

price = closes[-1]
prev_price = closes[-2] if len(closes) >= 2 else price

# ── SMA ──
sma20  = calc_sma(closes, 20)
sma50  = calc_sma(closes, 50)
sma200 = calc_sma(closes, 200) if len(closes) >= 200 else 0
prev_sma20 = calc_sma(closes[:-1], 20)
sma20_trend = "rising" if sma20 > calc_sma(closes[:-5], 20) else "falling"
sma50_trend = "rising" if sma50 > calc_sma(closes[:-5], 50) else "falling"
above_sma20  = price > sma20
above_sma50  = price > sma50
above_sma200 = price > sma200 if sma200 > 0 else None
crossed_above_sma20 = price > sma20 and prev_price <= prev_sma20
crossed_below_sma20 = price < sma20 and prev_price >= prev_sma20
false_breakout = crossed_below_sma20

# ── RSI ──
rsi = calc_rsi(closes, 14)
rsi14_prev = calc_rsi(closes[:-1], 14)
if rsi < 30:
    rsi_signal = "OVERSOLD — BUY zone"
    rsi_score = 15
elif rsi < 40:
    rsi_signal = "oversold territory"
    rsi_score = 10
elif rsi > 70:
    rsi_signal = "OVERBOUGHT — caution"
    rsi_score = 5
elif rsi > 60:
    rsi_signal = "overbought territory"
    rsi_score = 7
else:
    rsi_signal = "neutral"
    rsi_score = 5
rsi_trend = "rising" if rsi > rsi14_prev else "falling"

# ── MACD ──
macd = calc_macd(closes)

# ── Bollinger Bands ──
bb = calc_bollinger(closes, 20)

# ── Fibonacci ──
fib = calc_fibonacci(highs, lows, 50)
nearest = nearest_fib(price, fib)

# ── Candlestick Pattern ──
pattern = detect_candle_pattern(opens, highs, lows, closes)

# ── COMPOSITE SCORE ──
tech_score = 0
overall_signal = "NEUTRAL"

# SMA contribution
if above_sma20 and above_sma50:
    tech_score += 5
elif not above_sma20 and not above_sma50:
    tech_score -= 5
if false_breakout:
    tech_score -= 8
elif crossed_above_sma20:
    tech_score += 3

# RSI contribution
tech_score += rsi_score

# MACD contribution
if "BULLISH" in macd["trend"].upper():
    tech_score += 8
elif "BEARISH" in macd["trend"].upper():
    tech_score -= 5
elif macd["trend"] == "bullish":
    tech_score += 4

# Bollinger contribution
if "LOWER" in bb["position"].upper():
    tech_score += 5
elif "UPPER" in bb["position"].upper():
    tech_score -= 3

# Pattern contribution
if "bullish" in pattern.lower():
    tech_score += 5
elif "bearish" in pattern.lower():
    tech_score -= 3

# Overall signal
if tech_score >= 20:
    overall_signal = "STRONG BUY"
elif tech_score >= 10:
    overall_signal = "BUY"
elif tech_score <= -10:
    overall_signal = "STRONG SELL"
elif tech_score <= -5:
    overall_signal = "SELL"
else:
    overall_signal = "NEUTRAL"

# Warning
warning = ""
if false_breakout:
    warning = "FALSE BREAKOUT detected — price crossed above SMA20 then dropped below!"
elif crossed_above_sma20 and rsi > 65:
    warning = "SMA crossover but RSI overbought — wait for pullback"
elif bb["squeeze"]:
    warning = "Bollinger squeeze — big move incoming!"

return {
    "available": True,
    "price": round(price, 3),
    # SMA
    "sma20": sma20, "sma50": sma50, "sma200": sma200,
    "above_sma20": above_sma20, "above_sma50": above_sma50, "above_sma200": above_sma200,
    "sma20_trend": sma20_trend, "sma50_trend": sma50_trend,
    "crossed_above_sma20": crossed_above_sma20, "false_breakout": false_breakout,
    # RSI
    "rsi": rsi, "rsi_signal": rsi_signal, "rsi_score": rsi_score, "rsi_trend": rsi_trend,
    # MACD
    "macd_value": macd["macd"], "macd_signal": macd["signal"],
    "macd_hist": macd["histogram"], "macd_trend": macd["trend"],
    # Bollinger
    "bb_upper": bb["upper"], "bb_middle": bb["middle"], "bb_lower": bb["lower"],
    "bb_position": bb["position"], "bb_squeeze": bb["squeeze"], "bb_bandwidth": bb["bandwidth"],
    # Fibonacci
    "fib": fib, "nearest_fib": nearest,
    # Pattern
    "pattern": pattern,
    # Overall
    "tech_score": tech_score,
    "signal": overall_signal,
    "warning": warning,
}
```

def format_tech_block(t: dict) -> str:
if not t.get(“available”):
return “*Technical data unavailable — add OANDA_TOKEN to Render*”
trend_icon = lambda x: “📈” if x == “rising” else “📉”
chk = lambda x: “✅” if x else “❌”
macd_icon = “🟢” if “bullish” in t[“macd_trend”].lower() else “🔴” if “bearish” in t[“macd_trend”].lower() else “🟡”
rsi_icon = “🟢” if t[“rsi”] < 40 else “🔴” if t[“rsi”] > 65 else “🟡”
bb_icon = “🟢” if “LOWER” in t[“bb_position”].upper() else “🔴” if “UPPER” in t[“bb_position”].upper() else “🟡”

```
return (
    f"📊 *LIVE TECHNICAL INDICATORS (H1):*\n"
    f"━━━━━━━━━━━━━━━━━━\n"
    f"*SMA:*\n"
    f"20: ${t['sma20']} {trend_icon(t['sma20_trend'])} | {chk(t['above_sma20'])} above\n"
    f"50: ${t['sma50']} {trend_icon(t['sma50_trend'])} | {chk(t['above_sma50'])} above\n"
    f"200: ${t['sma200']} | {chk(t['above_sma200'])} above\n"
    f"{'⚠️ FALSE BREAKOUT!' if t['false_breakout'] else '🟢 Fresh crossover!' if t['crossed_above_sma20'] else ''}\n\n"
    f"*RSI (14):* {rsi_icon} {t['rsi']} {trend_icon(t['rsi_trend'])}\n"
    f"_{t['rsi_signal']}_\n\n"
    f"*MACD:* {macd_icon} {t['macd_trend'].upper()}\n"
    f"MACD: {t['macd_value']} | Signal: {t['macd_signal']} | Hist: {t['macd_hist']}\n\n"
    f"*Bollinger Bands:* {bb_icon}\n"
    f"Upper: ${t['bb_upper']} | Mid: ${t['bb_middle']} | Lower: ${t['bb_lower']}\n"
    f"_{t['bb_position']}_\n"
    f"{'⚡ SQUEEZE — big move coming!' if t['bb_squeeze'] else ''}\n\n"
    f"*Fibonacci (50-bar):*\n"
    f"High: ${t['fib'].get('high','—')} | Low: ${t['fib'].get('low','—')}\n"
    f"Nearest level: {t['nearest_fib']}\n\n"
    f"*Pattern:* 🕯 {t['pattern']}\n\n"
    f"*OVERALL: {t['signal']}* (tech score: {t['tech_score']})\n"
    f"{'⚠️ '+t['warning'] if t['warning'] else ''}"
)
```

def format_sma_block(t: dict) -> str:
“”“Now returns full technical analysis block”””
return format_tech_block(t)

# ── LIVE PRICE (OANDA + gold-api cross-check) ──────────────────────────────────

async def get_live_price() -> str:
oanda_price = None
goldapi_price = None

```
if OANDA_TOKEN:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api-fxtrade.oanda.com/v3/instruments/XAU_USD/candles"
                "?count=1&granularity=S5&price=M",
                headers={"Authorization": f"Bearer {OANDA_TOKEN}"}
            )
            data = resp.json()
            oanda_price = round(float(data["candles"][0]["mid"]["c"]), 3)
    except Exception as e:
        logger.warning(f"OANDA price failed: {e}")

try:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://www.goldapi.io/api/XAU/USD",
            headers={"x-access-token": "goldapi-free"}
        )
        goldapi_price = round(float(resp.json().get("price", 0)), 2)
except Exception as e:
    logger.warning(f"GoldAPI failed: {e}")

if oanda_price and goldapi_price:
    diff_pct = (abs(oanda_price - goldapi_price) / oanda_price) * 100
    tag = "✅ verified" if diff_pct < 0.1 else "⚠️ mismatch"
    return f"{oanda_price} [OANDA] {tag} (gold-api: ${goldapi_price})"
if oanda_price:
    return f"{oanda_price} [OANDA only]"
if goldapi_price:
    return f"{goldapi_price} [gold-api only]"
return ""
```

def parse_price(s: str) -> float:
try:
return float(str(s).split()[0].replace(”,”,””))
except Exception:
return 0.0

# ── LIVE DXY ───────────────────────────────────────────────────────────────────

async def get_dxy_price() -> str:
try:
async with httpx.AsyncClient(timeout=10) as client:
resp = await client.get(
“https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?interval=1m&range=1d”,
headers={“User-Agent”: “Mozilla/5.0”}
)
price = resp.json()[“chart”][“result”][0][“meta”][“regularMarketPrice”]
return str(round(float(price), 2))
except Exception as e:
logger.warning(f”DXY failed: {e}”)
return “”

# ── LIVE OIL ───────────────────────────────────────────────────────────────────

async def get_oil_price() -> str:
try:
async with httpx.AsyncClient(timeout=10) as client:
resp = await client.get(
“https://query1.finance.yahoo.com/v8/finance/chart/CL=F?interval=1m&range=1d”,
headers={“User-Agent”: “Mozilla/5.0”}
)
price = resp.json()[“chart”][“result”][0][“meta”][“regularMarketPrice”]
return str(round(float(price), 2))
except Exception as e:
logger.warning(f”Oil failed: {e}”)
return “”

# ── FETCH ALL LIVE DATA ────────────────────────────────────────────────────────

async def get_all_live_data() -> dict:
gold, dxy, oil, sma = await asyncio.gather(
get_live_price(), get_dxy_price(), get_oil_price(), get_technical_analysis(),
return_exceptions=True
)
return {
“gold”: gold if isinstance(gold, str) else “”,
“dxy”:  dxy  if isinstance(dxy,  str) else “”,
“oil”:  oil  if isinstance(oil,  str) else “”,
“sma”:  sma  if isinstance(sma,  dict) else {“available”: False},
}

# ── RISK CALCULATOR ────────────────────────────────────────────────────────────

def calculate_risk(entry: float = 0, sl: float = 0) -> dict:
bal = runtime_balance[“value”]
max_loss  = round(bal * MAX_RISK_PCT / 100, 2)
daily_max = round(bal * MAX_DAILY_LOSS / 100, 2)
rec_sl    = round(max_loss / PIP_VALUE)

```
if entry and sl:
    sl_pips = round(abs(entry - sl) * 100)
    sl_cost = round(sl_pips * PIP_VALUE, 2)
    risk_pct = round((sl_cost / bal) * 100, 2)
    ok = risk_pct <= MAX_RISK_PCT
else:
    sl_pips = rec_sl
    sl_cost = max_loss
    risk_pct = MAX_RISK_PCT
    ok = True

return {
    "balance": bal, "max_loss": max_loss, "daily_max": daily_max,
    "rec_sl": rec_sl, "sl_pips": sl_pips, "sl_cost": sl_cost,
    "risk_pct": risk_pct, "ok": ok
}
```

def format_risk_block(entry: str, sl: str) -> str:
try:
e, s = parse_price(entry), parse_price(sl)
if e <= 0 or s <= 0:
return “”
r = calculate_risk(e, s)
status = “✅ OK” if r[“ok”] else “❌ TOO HIGH”
return (
f”\n💰 *Risk:* {r[‘sl_pips’]} pips = ${r[‘sl_cost’]} “
f”| {r[‘risk_pct’]}% {status}\n”
f”Max: ${r[‘max_loss’]} | Daily: ${r[‘daily_max’]}”
)
except Exception:
return “”

# ── ECONOMIC CALENDAR ──────────────────────────────────────────────────────────

async def get_market_news() -> dict:
today = datetime.now(SGT)
weekday = today.weekday()
scheduled = {
0: [“No major events Monday”],
1: [“ISM Services PMI 10PM SGT”, “RBA Rate Decision (varies)”],
2: [“ADP Employment 8:15PM SGT”, “EIA Oil Inventory 10:30PM SGT”],
3: [“US Jobless Claims 8:30PM SGT”],
4: [“NFP Non-Farm Payrolls 8:30PM SGT BIGGEST EVENT!”],
}
prompt = f””“Financial news assistant. Today: {today.strftime(’%A %B %d %Y %H:%M SGT’)}
Search for: major economic events today, Fed news, Iran-US update, upcoming events 24hrs.
Return ONLY valid JSON:
{{“breaking_news”:[“item1”,“item2”],“fed_update”:“one line”,“iran_update”:“one line”,“upcoming_events”:[“event 1”,“event 2”],“gold_impact”:“bullish or bearish or neutral”,“impact_reason”:“one sentence”,“risk_level”:“HIGH or MEDIUM or LOW”,“safe_to_trade”:true}}”””
try:
data = await gemini_analysis(prompt)
data[“upcoming_events”] = list(set(data.get(“upcoming_events”,[]) + scheduled.get(weekday,[])))[:5]
return data
except Exception as e:
logger.warning(f”News failed: {e}”)
return {“breaking_news”:[“Check investing.com”],“fed_update”:”—”,“iran_update”:”—”,
“upcoming_events”:scheduled.get(weekday,[]),“gold_impact”:“neutral”,
“impact_reason”:“No data”,“risk_level”:“MEDIUM”,“safe_to_trade”:True}

# ── JSON EXTRACTOR ─────────────────────────────────────────────────────────────

def extract_json(text: str) -> dict:
text = text.replace(”`json","").replace("`”,””).strip()
s, e = text.find(”{”), text.rfind(”}”) + 1
if s != -1 and e > s:
text = text[s:e]
return json.loads(text)

# ── GEMINI AI ──────────────────────────────────────────────────────────────────

async def gemini_analysis(prompt: str, retries: int = 2) -> dict:
url = f”https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_KEY}”
for attempt in range(retries):
try:
async with httpx.AsyncClient(timeout=90) as client:
resp = await client.post(url, json={
“contents”: [{“parts”: [{“text”: prompt}]}],
“generationConfig”: {“temperature”: 0.1}
})
data = resp.json()
if data.get(“error”,{}).get(“code”) in (503, 429):
if attempt < retries-1:
await asyncio.sleep(5)
continue
raise ValueError(f”Gemini error: {data[‘error’]}”)
text = data[“candidates”][0][“content”][“parts”][0][“text”]
return extract_json(text)
except ValueError:
raise
except Exception as e:
if attempt < retries-1:
await asyncio.sleep(3)
continue
raise
raise ValueError(“Gemini failed after retries”)

# ── CLAUDE AI ──────────────────────────────────────────────────────────────────

async def claude_analysis(prompt: str) -> dict:
async with httpx.AsyncClient(timeout=90) as client:
resp = await client.post(
“https://api.anthropic.com/v1/messages”,
headers={“Content-Type”:“application/json”,“x-api-key”:ANTHROPIC_KEY,“anthropic-version”:“2023-06-01”},
json={“model”:“claude-haiku-4-5-20251001”,“max_tokens”:1500,
“tools”:[{“type”:“web_search_20250305”,“name”:“web_search”}],
“messages”:[{“role”:“user”,“content”:prompt}]}
)
data = resp.json()
text = “”.join(b[“text”] for b in data.get(“content”,[]) if b.get(“type”)==“text”)
if not text:
raise ValueError(“Claude returned empty response”)
return extract_json(text)

# ── ANALYSIS PROMPT ────────────────────────────────────────────────────────────

def build_analysis_prompt(live: dict) -> str:
today   = sgt_full()
gold_h  = f”LIVE XAU/USD (OANDA): ${live[‘gold’]}” if live[“gold”] else “Search for XAU/USD (~$4,500-$5,000 May 2026)”
dxy_h   = f”LIVE DXY: {live[‘dxy’]}” if live[“dxy”] else “Search for DXY level”
oil_h   = f”LIVE WTI OIL: ${live[‘oil’]}” if live[“oil”] else “Search for WTI oil”
sma     = live.get(“sma”, {})
sma_ctx = “”
if sma.get(“available”):
sma_ctx = (
f”\nLIVE SMA DATA (from OANDA H1 candles):\n”
f”SMA20={sma[‘sma20’]} SMA50={sma[‘sma50’]} SMA200={sma[‘sma200’]}\n”
f”Price {‘ABOVE’ if sma[‘above_sma20’] else ‘BELOW’} SMA20 | “
f”Price {‘ABOVE’ if sma[‘above_sma50’] else ‘BELOW’} SMA50 | “
f”Price {‘ABOVE’ if sma.get(‘above_sma200’) else ‘BELOW’} SMA200\n”
f”SMA20 trend: {sma[‘sma20_trend’]} | SMA50 trend: {sma[‘sma50_trend’]}\n”
f”Crossover: {‘ABOVE SMA20’ if sma[‘crossed_above_sma20’] else ‘FALSE BREAKOUT’ if sma[‘false_breakout’] else ‘No recent crossover’}\n”
f”SMA Signal: {sma[‘signal’]}”
)

```
weekday = datetime.now(SGT).weekday()
event_warn = {
    2: "WARNING: ADP Employment 8:15PM SGT today!",
    3: "WARNING: Jobless Claims 8:30PM SGT today!",
    4: "CRITICAL: NFP 8:30PM SGT TODAY - DO NOT TRADE BEFORE!"
}.get(weekday, "")

return f"""You are Aden Yang professional gold trading AI. {today}
```

{event_warn}

LIVE DATA (use exactly, do NOT search for price):
{gold_h}
{dxy_h}
{oil_h}
{sma_ctx}

Search web ONLY for: Iran-US news, Fed news, events next 2 hours.

ANALYSIS STEPS:

1. Multi-timeframe: Weekly/Daily/4H trends
1. Technical: RSI, MACD, support/resistance, Fibonacci
1. SMA analysis: use live SMA data above
1. News risk: major event next 2hrs = WAIT
1. Signal: BUY if 2+ TF bullish, SELL if 2+ bearish, WAIT if mixed

SCORING (generous, minimum 25):
Multi-TF (0-20): 10 if 1 TF clear, 20 if 2+ agree
DXY (0-20): below 100=15-20, above 103=0-5
RSI (0-15): 30-50=10, below 35=15, neutral=5
SR Level (0-15): within $30=10, at level=15
News (0-15): Iran war ongoing=min 8, catalyst=15
Pattern (0-10): any action=5, clear pattern=10
SMA (0-5): above SMA20+50=5, crossover=3, false breakout=-5

Return ONLY valid JSON:
{{“price”:“4700”,“signal”:“BUY”,“entry”:“4695”,“sl”:“4680”,“tp1”:“4715”,“tp2”:“4735”,“rr”:“1:2”,“session”:“London”,“score_total”:75,“score_multitf”:15,“score_dxy”:18,“score_rsi”:10,“score_sr_level”:10,“score_news”:12,“score_pattern”:5,“score_external”:2,“score_sma”:3,“weekly_trend”:“bullish”,“daily_trend”:“bullish”,“h4_trend”:“neutral”,“rsi_value”:“42”,“rsi_signal”:“oversold”,“macd”:“bullish”,“pattern_found”:“hammer”,“dxy”:“98.15”,“dxy_trend”:“falling”,“oil”:“99”,“iran_update”:“Peace talks ongoing”,“key_support”:“4680”,“key_resistance”:“4750”,“fib_level”:“4695 (38.2%)”,“sma_signal”:“bullish”,“sma_note”:“Price above SMA20 and SMA50”,“reason”:“DXY below 100 supports gold. RSI oversold at support. Price above key SMAs.”,“risk_warning”:””,“news_filter”:false,“trade_now”:true}}”””

# ── CROSS-CHECK PROMPT ─────────────────────────────────────────────────────────

def build_crosscheck_prompt(signal_text: str, live: dict) -> str:
today  = sgt_full()
gold_h = f”LIVE XAU/USD: ${live[‘gold’]}” if live[“gold”] else “Search (~$4,500-$5,000)”
dxy_h  = f”LIVE DXY: {live[‘dxy’]}” if live[“dxy”] else “Search DXY”
oil_h  = f”LIVE OIL: ${live[‘oil’]}” if live[“oil”] else “Search oil”
sma    = live.get(“sma”, {})
sma_ctx = f”\nSMA: 20={sma.get(‘sma20’,0)} 50={sma.get(‘sma50’,0)} Signal={sma.get(‘signal’,‘N/A’)}” if sma.get(“available”) else “”

```
return f"""Professional gold AI. {today}
```

{gold_h} | {dxy_h} | {oil_h}{sma_ctx}

FORWARDED SIGNAL: {signal_text}

1. Extract direction/entry/SL/TP/channel
1. Search latest news only
1. Multi-TF analysis + SMA check
1. CONFIRMED>=70 MIXED=50-69 REJECTED<50

Return ONLY valid JSON:
{{“source_direction”:“BUY”,“source_entry”:“4700”,“source_sl”:“4680”,“source_tp”:“4730”,“source_name”:“United Signals”,“current_price”:“4705”,“ai_direction”:“BUY”,“ai_agrees”:true,“confidence”:72,“score_total”:72,“score_multitf”:15,“score_dxy”:18,“score_rsi”:10,“score_sr_level”:10,“score_news”:12,“score_pattern”:5,“score_sma”:2,“weekly_trend”:“bullish”,“daily_trend”:“bullish”,“h4_trend”:“bullish”,“dxy”:“98.15”,“dxy_trend”:“falling”,“iran_update”:“Peace talks”,“rsi_value”:“42”,“rsi_signal”:“oversold”,“pattern_found”:“none”,“sma_signal”:“bullish”,“verdict”:“CONFIRMED”,“recommended_entry”:“4700”,“recommended_sl”:“4683”,“recommended_tp1”:“4725”,“recommended_tp2”:“4750”,“recommended_rr”:“1:2”,“reason”:“All confirms align.”,“risk_warning”:””}}”””

# ── FORMAT SIGNAL ──────────────────────────────────────────────────────────────

def format_signal(a: dict, source=“AI”, sma: dict = None) -> str:
e  = {“BUY”:“🟢”,“SELL”:“🔴”,“WAIT”:“🟡”}.get(a.get(“signal”,“WAIT”),“⚪”)
d  = “📉” if a.get(“dxy_trend”)==“falling” else “📈” if a.get(“dxy_trend”)==“rising” else “➡️”
ti = lambda t: “🟢” if t==“bullish” else “🔴” if t==“bearish” else “🟡”
si = {“Asian”:“🌏”,“London”:“🇬🇧”,“New York”:“🇺🇸”,“Overlap”:“⚡”}.get(a.get(“session”,””),“🕐”)
sc = a.get(“score_total”, 0)
sb = “█”*(sc//10) + “░”*(10-sc//10)
ts = get_session_label(
news_filter=a.get(“news_filter”, False),
risk_level=””
)
sma_block = (”\n” + format_sma_block(sma)) if sma and sma.get(“available”) else “”
risk_block = format_risk_block(a.get(“entry”,“0”), a.get(“sl”,“0”))

```
if a.get("signal") == "WAIT":
    return (
        f"⚖️ *ADEN GOLD AI v4.0 — {source}*\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🟡 *WAIT* | 💰 ${a.get('price','—')}\n{sb} {sc}/100\n"
        f"{si} {a.get('session','—')}\n"
        f"{sma_block}\n\n"
        f"📊 *TF:* W:{ti(a.get('weekly_trend','neutral'))} {a.get('weekly_trend','—').upper()} | "
        f"D:{ti(a.get('daily_trend','neutral'))} {a.get('daily_trend','—').upper()} | "
        f"4H:{ti(a.get('h4_trend','neutral'))} {a.get('h4_trend','—').upper()}\n\n"
        f"📈 *Score {sc}/100:*\n"
        f"MTF:{a.get('score_multitf',0)} DXY:{a.get('score_dxy',0)} RSI:{a.get('score_rsi',0)} "
        f"S/R:{a.get('score_sr_level',0)} News:{a.get('score_news',0)} Pat:{a.get('score_pattern',0)} SMA:{a.get('score_sma',0)}\n\n"
        f"{d} DXY:{a.get('dxy','—')} ({a.get('dxy_trend','—')}) | 🛢${a.get('oil','—')}\n"
        f"📍 S:${a.get('key_support','—')} R:${a.get('key_resistance','—')}\n"
        f"🌍 _{a.get('iran_update','—')}_\n💡 _{a.get('reason','—')}_\n"
        f"{'⚠️ '+a.get('risk_warning') if a.get('risk_warning') else ''}\n⏰ {ts}"
    )

return (
    f"⚖️ *ADEN GOLD AI v4.0 — {source}*\n━━━━━━━━━━━━━━━━━━━━━━\n"
    f"{e} *{a.get('signal','—')}* | 💰 ${a.get('price','—')}\n{sb} {sc}/100\n"
    f"{si} {a.get('session','—')}\n"
    f"{sma_block}\n\n"
    f"🎯 *SAR:*\n"
    f"┌ 📍 Entry: `${a.get('entry','—')}`\n"
    f"│ 🛑 SL:    `${a.get('sl','—')}`\n"
    f"│ 🎯 TP1:   `${a.get('tp1','—')}`\n"
    f"│ 🏆 TP2:   `${a.get('tp2','—')}`\n"
    f"└ ⚖️  R:R:   `{a.get('rr','—')}`\n"
    f"{risk_block}\n\n"
    f"📊 *TF:* W:{ti(a.get('weekly_trend','neutral'))} {a.get('weekly_trend','—').upper()} | "
    f"D:{ti(a.get('daily_trend','neutral'))} {a.get('daily_trend','—').upper()} | "
    f"4H:{ti(a.get('h4_trend','neutral'))} {a.get('h4_trend','—').upper()}\n\n"
    f"📈 *Score {sc}/100:*\n"
    f"MTF:{a.get('score_multitf',0)} DXY:{a.get('score_dxy',0)} RSI:{a.get('score_rsi',0)} "
    f"S/R:{a.get('score_sr_level',0)} News:{a.get('score_news',0)} Pat:{a.get('score_pattern',0)} SMA:{a.get('score_sma',0)}\n"
    f"🕯 {a.get('pattern_found','none')} | 📐 {a.get('fib_level','none')}\n\n"
    f"{d} DXY:{a.get('dxy','—')} ({a.get('dxy_trend','—')}) | 🛢${a.get('oil','—')}\n"
    f"📍 S:${a.get('key_support','—')} R:${a.get('key_resistance','—')}\n"
    f"🌍 _{a.get('iran_update','—')}_\n💡 _{a.get('reason','—')}_\n"
    f"{'⚠️ '+a.get('risk_warning') if a.get('risk_warning') else ''}\n"
    f"⏰ {ts}\n━━━━━━━━━━━━━━━━━━━━━━\n"
    f"✅ SL before entry | Max 2u | 0.7-1% target"
)
```

# ── FORMAT CROSS-CHECK ─────────────────────────────────────────────────────────

def format_crosscheck(a: dict) -> str:
ve = {“CONFIRMED”:“✅”,“MIXED”:“⚠️”,“REJECTED”:“❌”}.get(a.get(“verdict”,“MIXED”),“❓”)
ae = “🟢” if a.get(“ai_direction”)==“BUY” else “🔴” if a.get(“ai_direction”)==“SELL” else “🟡”
se = “🟢” if a.get(“source_direction”)==“BUY” else “🔴” if a.get(“source_direction”)==“SELL” else “🟡”
ti = lambda t: “🟢” if t==“bullish” else “🔴” if t==“bearish” else “🟡”
d  = “📉” if a.get(“dxy_trend”)==“falling” else “📈” if a.get(“dxy_trend”)==“rising” else “➡️”
sc = a.get(“score_total”,0)
sb = “█”*(sc//10) + “░”*(10-sc//10)
ts = get_session_label()
risk_block = format_risk_block(a.get(“recommended_entry”,“0”), a.get(“recommended_sl”,“0”))

```
return (
    f"⚖️ *SIGNAL CROSS-CHECK v4.0*\n━━━━━━━━━━━━━━━━━━━━━━\n"
    f"{ve} *{a.get('verdict','—')}* | {sb} {a.get('confidence',0)}%\n\n"
    f"📨 *Source ({a.get('source_name','Unknown')}):*\n"
    f"{se} {a.get('source_direction','—')} | Entry:${a.get('source_entry','—')} SL:${a.get('source_sl','—')} TP:${a.get('source_tp','—')}\n\n"
    f"🤖 *AI Check:* {ae} {a.get('ai_direction','—')} | Agrees:{'✅' if a.get('ai_agrees') else '❌'} | Now:${a.get('current_price','—')}\n\n"
    f"📊 *TF:* W:{ti(a.get('weekly_trend','neutral'))} | D:{ti(a.get('daily_trend','neutral'))} | 4H:{ti(a.get('h4_trend','neutral'))}\n"
    f"📈 *Score {sc}/100:* MTF:{a.get('score_multitf',0)} DXY:{a.get('score_dxy',0)} RSI:{a.get('score_rsi',0)} "
    f"S/R:{a.get('score_sr_level',0)} News:{a.get('score_news',0)} SMA:{a.get('score_sma',0)}\n"
    f"SMA: {a.get('sma_signal','—')}\n\n"
    f"{d} DXY:{a.get('dxy','—')} | 🌍 _{a.get('iran_update','—')}_\n\n"
    f"🎯 *Recommended SAR:*\n"
    f"┌ 📍 Entry: `${a.get('recommended_entry','—')}`\n"
    f"│ 🛑 SL:    `${a.get('recommended_sl','—')}` ✅\n"
    f"│ 🎯 TP1:   `${a.get('recommended_tp1','—')}`\n"
    f"│ 🏆 TP2:   `${a.get('recommended_tp2','—')}`\n"
    f"└ ⚖️  R:R:   `{a.get('recommended_rr','—')}`\n"
    f"{risk_block}\n\n"
    f"💡 _{a.get('reason','—')}_\n"
    f"{'⚠️ '+a.get('risk_warning') if a.get('risk_warning') else ''}\n"
    f"⏰ {ts}\n━━━━━━━━━━━━━━━━━━━━━━\n"
    f"✅ SL before entry | Max 2u | 0.7-1% target"
)
```

def format_news(n: dict) -> str:
risk_level = n.get(“risk_level”, “MEDIUM”)
ts = get_session_label(
news_filter=not n.get(“safe_to_trade”, True),
risk_level=risk_level
)
impact = {“bullish”:“🟢 BULLISH”,“bearish”:“🔴 BEARISH”,“neutral”:“🟡 NEUTRAL”}.get(n.get(“gold_impact”,“neutral”),“🟡”)
risk   = {“HIGH”:“🔴 HIGH”,“MEDIUM”:“🟡 MEDIUM”,“LOW”:“🟢 LOW”}.get(risk_level,“🟡”)
safe   = “✅ OK to trade” if n.get(“safe_to_trade”) else “❌ WAIT — news risk!”
breaking = “\n”.join(f”• {b}” for b in n.get(“breaking_news”,[])[:4])
upcoming = “\n”.join(f”• {e}” for e in n.get(“upcoming_events”,[])[:5])
return (
f”📰 *GOLD MARKET NEWS*\n━━━━━━━━━━━━━━━━━━━━━━\n”
f”{impact} | Risk: {risk} | {safe}\n\n”
f”🚨 *Breaking:*\n{breaking}\n\n”
f”🏦 Fed: *{n.get(‘fed_update’,’—’)}*\n”
f”🌍 Iran: *{n.get(‘iran_update’,’—’)}*\n\n”
f”📅 *Upcoming (SGT):*\n{upcoming}\n\n”
f”💡 *{n.get(‘impact_reason’,’—’)}*\n”
f”⏰ {ts}”
)

# ── SIGNAL DETECTOR ────────────────────────────────────────────────────────────

def is_trading_signal(text: str) -> bool:
keywords = [“buy”,“sell”,“entry”,“sl:”,“tp:”,“stop loss”,“take profit”,
“xau”,“gold”,“signal”,“long”,“short”,“target”,“pips”,
“limit”,“breakout”,“support”,“resistance”,“bullish”,“bearish”]
return sum(1 for k in keywords if k in text.lower()) >= 1

# ── COMMANDS ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
bal = runtime_balance[“value”]
await update.message.reply_text(
f”⚖️ *ADEN GOLD AI BOT v4.1*\n━━━━━━━━━━━━━━━━━━━━━━\n”
f”📊 SMA + RSI + MACD + Bollinger + Fib\n”
f”🕯 Candlestick pattern detection\n”
f”💰 Live: OANDA + gold-api + Yahoo Finance\n”
f”⏰ Singapore Time (SGT) ✅\n”
f”💼 Balance: ${bal:,.2f}\n\n”
f”*Commands:*\n”
f”/monday — 🌅 Monday morning brief\n”
f”/signal — Full Claude analysis + SMA\n”
f”/quick — Fast Gemini (free) + SMA\n”
f”/sma — SMA analysis only\n”
f”/news — Latest news + events\n”
f”/risk — Risk calculator\n”
f”/setbalance — Update balance\n”
f”/crossref — Forward signal guide\n”
f”/rules — Trading rules\n”
f”/status — Bot status\n\n”
f”*Signal Channels (forward to bot):*\n”
f”📊 United Signals | SureShotFX\n”
f”📊 FXPremiere | Uncle Lim Journey”,
parse_mode=“Markdown”
)

async def cmd_sma(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
msg = await update.message.reply_text(
“⏳ *Calculating RSI, MACD, Bollinger, SMA, Fibonacci…*”,
parse_mode=“Markdown”
)
t = await get_technical_analysis()
if not t.get(“available”):
await msg.edit_text(
“❌ Technical data unavailable.\nCheck OANDA_TOKEN in Render.”,
parse_mode=“Markdown”
)
return
await msg.edit_text(
f”📊 *FULL TECHNICAL ANALYSIS — XAU/USD H1*\n”
f”━━━━━━━━━━━━━━━━━━━━━━\n”
f”{format_tech_block(t)}\n”
f”⏰ {sgt_now()}”,
parse_mode=“Markdown”
)

async def cmd_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
msg = await update.message.reply_text(“⏳ *Fetching live data + SMA + Claude analysis…*”, parse_mode=“Markdown”)
try:
live = await get_all_live_data()
prompt = build_analysis_prompt(live)
source = “CLAUDE”
try:
a = await claude_analysis(prompt)
except Exception as e1:
logger.warning(f”Claude failed: {e1}”)
a = await gemini_analysis(prompt)
source = “GEMINI”
if live[“gold”]:
raw = parse_price(live[“gold”])
if raw > 0 and abs(parse_price(a.get(“price”,“0”)) - raw) > 200:
a[“price”] = str(raw)
await msg.edit_text(format_signal(a, source, live.get(“sma”)), parse_mode=“Markdown”)
except Exception as e:
await msg.edit_text(f”❌ Failed: {str(e)[:150]}”, parse_mode=“Markdown”)

async def cmd_quick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
msg = await update.message.reply_text(“⏳ *Fetching live data + SMA + Gemini analysis…*”, parse_mode=“Markdown”)
try:
live = await get_all_live_data()
a = await gemini_analysis(build_analysis_prompt(live))
if live[“gold”]:
raw = parse_price(live[“gold”])
if raw > 0 and abs(parse_price(a.get(“price”,“0”)) - raw) > 200:
a[“price”] = str(raw)
await msg.edit_text(format_signal(a, “GEMINI”, live.get(“sma”)), parse_mode=“Markdown”)
except Exception as e:
await msg.edit_text(f”❌ Gemini failed: {str(e)[:150]}\nTry /signal instead.”, parse_mode=“Markdown”)

async def cmd_news(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
msg = await update.message.reply_text(“⏳ *Fetching latest news…*”, parse_mode=“Markdown”)
try:
await msg.edit_text(format_news(await get_market_news()), parse_mode=“Markdown”)
except Exception as e:
await msg.edit_text(f”❌ News failed: {str(e)[:100]}”, parse_mode=“Markdown”)

async def cmd_risk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
r = calculate_risk()
ts = sgt_now()
hour = datetime.now(SGT).hour
if 15 <= hour < 20:
session, rec = “London 🇬🇧”, “25-35 pips”
elif 20 <= hour < 24:
session, rec = “New York 🇺🇸”, “20-30 pips”
else:
session, rec = “Asian 🌏”, “15-25 pips”
await update.message.reply_text(
f”💰 *RISK CALCULATOR*\n━━━━━━━━━━━━━━\n”
f”💼 Balance: *${r[‘balance’]:,.2f}*\n”
f”📊 Pip: ${PIP_VALUE} | Max: {MAX_RISK_PCT}%\n\n”
f”Max/trade: *${r[‘max_loss’]}*\n”
f”Daily limit: *${r[‘daily_max’]}*\n”
f”Rec SL: *{r[‘rec_sl’]} pips*\n\n”
f”Session: {session} | Ideal SL: {rec}\n”
f”Buffer: {SL_BUFFER_PIPS} pips beyond S/R\n\n”
f”15p=${round(15*PIP_VALUE,2)} | 25p=${round(25*PIP_VALUE,2)} | “
f”50p=${round(50*PIP_VALUE,2)} | 100p=${round(100*PIP_VALUE,2)}\n\n”
f”*/setbalance [amount] to update*\n⏰ {ts}”,
parse_mode=“Markdown”
)

async def cmd_setbalance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
try:
args = ctx.args
if not args:
await update.message.reply_text(
f”💼 Current: ${runtime_balance[‘value’]:,.2f}\nUse: `/setbalance 2000`”,
parse_mode=“Markdown”
)
return
new_bal = float(args[0].replace(”,”,””).replace(”$”,””))
old = runtime_balance[“value”]
runtime_balance[“value”] = new_bal
r = calculate_risk()
await update.message.reply_text(
f”✅ *Balance Updated!*\n${old:,.2f} → *${new_bal:,.2f}*\n\n”
f”Max/trade: ${r[‘max_loss’]} | Daily: ${r[‘daily_max’]}\n”
f”TP1: ${round(new_bal*0.003,2)} | TP2: ${round(new_bal*0.005,2)}”,
parse_mode=“Markdown”
)
except (IndexError, ValueError):
await update.message.reply_text(“❌ Use: `/setbalance 2000`”, parse_mode=“Markdown”)

async def cmd_crossref(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
await update.message.reply_text(
“📨 *Cross-Reference:*\n\n”
“1. Open signal channel\n2. Long press message\n”
“3. Tap Forward\n4. Select @AdenGoldAI_bot ✅\n\n”
“Or paste signal text here!\n\n”
“✅ CONFIRMED = Trade | ⚠️ MIXED = Careful | ❌ REJECTED = Skip”,
parse_mode=“Markdown”
)

async def cmd_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
bal = runtime_balance[“value”]
r = calculate_risk()
await update.message.reply_text(
f”📋 *ADEN’S RULES v4.0*\n━━━━━━━━━━━━━━━━━━━━━━\n”
f”★ SL BEFORE entry always!\n”
f”★ Structure SL + {SL_BUFFER_PIPS} pip buffer\n”
f”★ AI bot + own chart = both confirm\n”
f”★ Check SMA crossover — no false breakout!\n”
f”★ Max loss/trade: ${r[‘max_loss’]} ({MAX_RISK_PCT}%)\n”
f”★ TP1 at 0.3% = ${round(bal*0.003,2)}\n”
f”★ TP2 at 0.5% = ${round(bal*0.005,2)}\n”
f”★ Daily target 0.7-1% = ${round(bal*0.007,2)}-${round(bal*0.01,2)}\n”
f”★ 2 losses = STOP today!\n”
f”★ Target hit = LOG OFF!\n”
f”★ Gold only — no USD/JPY!\n”
f”★ Score >= 70 to trade!\n”
f”★ London+NY sessions only!\n\n”
f”*SAR:* SET → ADJUST → RUN\n”
f”*Small profits compound to millions!* 💪”,
parse_mode=“Markdown”
)

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
await update.message.reply_text(
f”🤖 *BOT STATUS v4.0*\n━━━━━━━━━━━━━━\n”
f”✅ Online | ⏰ {sgt_now()}\n”
f”💼 Balance: ${runtime_balance[‘value’]:,.2f}\n”
f”📈 SMA: OANDA H1 candles\n”
f”💰 Live: OANDA + gold-api + Yahoo\n”
f”🤖 Claude Haiku + Gemini 2.5 Flash\n\n”
f”*Channels:* United Signals | SureShotFX\n”
f”FXPremiere | Uncle Lim Journey”,
parse_mode=“Markdown”
)

async def cmd_monday(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
await _send_monday_brief(ctx.bot if hasattr(ctx, ‘bot’) else None, update=update)

async def *send_monday_brief(bot=None, update=None):
bal = runtime_balance[“value”]
r   = calculate_risk()
ts  = sgt_now()
milestones = [(“W8 $4,695”,4695),(“W15 $10K”,10000),(“W33 $30K”,30000),(“W60 $100K”,100000),(”$1M”,1000000)]
tracker = “\n”.join(f”{‘✅’ if bal>=m else ‘⏳’} {l}” for l,m in milestones)
text = (
f”🌅 *MONDAY MORNING BRIEF*\n━━━━━━━━━━━━━━━━━━━━━━\n”
f”⏰ {ts}\n\n💪 {get_quote()}\n\n”
f”💼 *ACCOUNT:* ${bal:,.2f}\n”
f”🎯 Daily: ${round(bal*0.007,2)} (0.7%) → ${round(bal*0.01,2)} (1%)\n”
f”📍 TP1: ${round(bal*0.003,2)} | TP2: ${round(bal*0.005,2)}\n”
f”🛑 Max risk: ${r[‘max_loss’]} per trade\n\n”
f”📊 *$1M TRACKER:*\n{tracker}\n\n”
f”📋 *WEEKLY CHECK-IN:*\n”
f”1. Last week balance?\n2. Win/loss count?\n”
f”3. Best + worst trade?\n4. Lessons learned?\n\n”
f”💰 *DEPOSIT $500 TODAY!* ✅\n\n”
f”✅ Structure SL + 5 pip buffer\n”
f”✅ AI + own chart + SMA confirm\n”
f”✅ Take 0.3% then 0.5% — no greed!\n”
f”✅ 2 losses = STOP | Target = LOG OFF!\n\n”
f”🚨 *RED FLAGS:*\n”
f”❌ Loss > ${r[‘daily_max’]} in a day\n”
f”❌ Trading outside London/NY\n”
f”❌ No SL set | Chasing losses\n\n”
f”🌟 *Small consistent wins*\n_compound into millions. $1M by 2028* 💪\n\n”
f”Type /news | /sma | /quick to start!”
)
if bot:
await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=“Markdown”)
elif update:
await update.message.reply_text(text, parse_mode=“Markdown”)

# ── SCHEDULED JOBS ─────────────────────────────────────────────────────────────

async def job_morning_quote(ctx: ContextTypes.DEFAULT_TYPE):
if datetime.now(SGT).weekday() >= 5: return
await ctx.bot.send_message(chat_id=CHAT_ID,
text=f”☀️ *GOOD MORNING ADEN!*\n⏰ {datetime.now(SGT).strftime(’%A %d %b’)}\n\n”
f”💪 {get_quote()}\n\n🎯 Hit 0.7% today. Structure SL. Take 0.3-0.5% TP.\n_One day at a time to $1M_ 🚀”,
parse_mode=“Markdown”)

async def job_monday_brief(ctx: ContextTypes.DEFAULT_TYPE):
if datetime.now(SGT).weekday() != 0: return
await _send_monday_brief(ctx.bot)

async def job_pre_london(ctx: ContextTypes.DEFAULT_TYPE):
if datetime.now(SGT).weekday() >= 5: return
bal = runtime_balance[“value”]
await ctx.bot.send_message(chat_id=CHAT_ID,
text=f”⚡ *PRE-LONDON CHECKLIST*\n🇬🇧 Opens in 15 mins!\n\n”
f”☐ /news — any high impact events?\n☐ /sma — SMA crossover check?\n”
f”☐ /quick — AI signal ready?\n☐ Own chart confirms direction?\n”
f”☐ SL level identified on chart?\n☐ TP1: ${round(bal*0.003,2)} | TP2: ${round(bal*0.005,2)}\n\n”
f”⚠️ Score < 70 = WAIT | News in 2hrs = WAIT\n_Best: 3PM-8PM SGT_ 💪”,
parse_mode=“Markdown”)

async def job_ny_open(ctx: ContextTypes.DEFAULT_TYPE):
if datetime.now(SGT).weekday() >= 5: return
bal = runtime_balance[“value”]
await ctx.bot.send_message(chat_id=CHAT_ID,
text=f”🗽 *NY SESSION OPEN*\n⏰ 8PM SGT — Overlap with London!\n\n”
f”💡 Most volatile 8PM-11PM SGT\n🎯 Daily target: ${round(bal*0.007,2)}\n\n”
f”Hit target already? → LOG OFF 🚫\nNot yet? → /quick or /sma first!\n\n”
f”⚠️ Check /news for US events!”,
parse_mode=“Markdown”)

async def job_eod_check(ctx: ContextTypes.DEFAULT_TYPE):
if datetime.now(SGT).weekday() >= 5: return
await ctx.bot.send_message(chat_id=CHAT_ID,
text=f”🌙 *END OF DAY CHECK-IN*\n⏰ {sgt_now()}\n\n”
f”📊 Reply with:\n1. Balance today\n2. Trades: W___ L___\n”
f”3. P&L: +/-$__*\n4. Hit target? Y/N\n\n”
f”💪 {get_quote()}\n\n”
f”✅ Close all positions!\n✅ /setbalance [new amount]\n_Rest well. Tomorrow is a new day* 🌟”,
parse_mode=“Markdown”)

# ── HANDLE MESSAGES ────────────────────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
if not update.message: return
text = update.message.text or update.message.caption or “”
logger.info(f”MSG: {text[:60]}”)
if not text:
await update.message.reply_text(“Empty message.”)
return

```
is_forwarded = any([
    getattr(update.message, "forward_date", None) is not None,
    getattr(update.message, "forward_from", None) is not None,
    getattr(update.message, "forward_from_chat", None) is not None,
    getattr(update.message, "forward_origin", None) is not None,
])
is_signal = is_trading_signal(text)
logger.info(f"forwarded:{is_forwarded} signal:{is_signal}")

if is_forwarded or is_signal:
    msg = await update.message.reply_text(
        "⏳ *Cross-referencing + SMA check...*", parse_mode="Markdown"
    )
    try:
        live = await get_all_live_data()
        prompt = build_crosscheck_prompt(text, live)
        try:
            a = await claude_analysis(prompt)
        except Exception as ce:
            logger.warning(f"Claude failed: {ce}")
            a = await gemini_analysis(prompt)
        if live["gold"]:
            raw = parse_price(live["gold"])
            if raw > 0 and abs(parse_price(a.get("current_price","0")) - raw) > 200:
                a["current_price"] = str(raw)
        await msg.edit_text(format_crosscheck(a), parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ Failed: {str(e)[:150]}\nTry /quick", parse_mode="Markdown")
else:
    await update.message.reply_text(
        "💬 No signal detected.\n/quick | /signal | /sma | /news\nForward a signal to cross-check!"
    )
```

# ── ERROR HANDLER ──────────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
logger.error(f”Error: {context.error}”)
try:
if update and hasattr(update, “message”) and update.message:
await update.message.reply_text(f”⚠️ Error. Try /quick.\n`{str(context.error)[:100]}`”, parse_mode=“Markdown”)
except Exception:
pass

# ── MAIN ───────────────────────────────────────────────────────────────────────

def main():
import datetime as dt
app = Application.builder().token(TELEGRAM_TOKEN).build()

```
# Commands
app.add_handler(CommandHandler("start",      cmd_start))
app.add_handler(CommandHandler("monday",     cmd_monday))
app.add_handler(CommandHandler("signal",     cmd_signal))
app.add_handler(CommandHandler("quick",      cmd_quick))
app.add_handler(CommandHandler("sma",        cmd_sma))
app.add_handler(CommandHandler("news",       cmd_news))
app.add_handler(CommandHandler("risk",       cmd_risk))
app.add_handler(CommandHandler("setbalance", cmd_setbalance))
app.add_handler(CommandHandler("crossref",   cmd_crossref))
app.add_handler(CommandHandler("rules",      cmd_rules))
app.add_handler(CommandHandler("status",     cmd_status))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.add_error_handler(error_handler)

# Scheduled jobs (UTC = SGT - 8hrs)
jq = app.job_queue
jq.run_daily(job_morning_quote, time=dt.time(23, 0, 0))  # 7AM SGT
jq.run_daily(job_monday_brief,  time=dt.time(0,  0, 0))  # 8AM SGT Mon
jq.run_daily(job_pre_london,    time=dt.time(6, 45, 0))  # 2:45PM SGT
jq.run_daily(job_ny_open,       time=dt.time(12, 0, 0))  # 8PM SGT
jq.run_daily(job_eod_check,     time=dt.time(15, 0, 0))  # 11PM SGT

logger.info("⚖️ Aden Gold AI Bot v4.0 started!")
logger.info(f"⏰ {sgt_now()} | Balance: ${runtime_balance['value']}")
app.run_polling(drop_pending_updates=True)
```

if **name** == “**main**”:
main()
