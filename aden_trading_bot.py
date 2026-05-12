import asyncio
import logging
import json
import httpx
import os
import pytz
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ── CONFIG ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_KEY",  "YOUR_CLAUDE_API_KEY_HERE")
GEMINI_KEY     = os.environ.get("GEMINI_KEY",      "YOUR_GEMINI_API_KEY_HERE")
OANDA_TOKEN    = os.environ.get("OANDA_TOKEN",     "")
OANDA_ACCOUNT  = os.environ.get("OANDA_ACCOUNT",   "")
SGT            = pytz.timezone("Asia/Singapore")

# ── RISK MANAGEMENT CONFIG ─────────────────────────────────────────────────────
# Update ACCOUNT_BALANCE in Render environment variables when balance changes!
ACCOUNT_BALANCE  = float(os.environ.get("ACCOUNT_BALANCE", "2000"))
PIP_VALUE        = float(os.environ.get("PIP_VALUE",        "0.04"))
MAX_RISK_PCT     = float(os.environ.get("MAX_RISK_PCT",     "1.0"))
MAX_DAILY_LOSS   = float(os.environ.get("MAX_DAILY_LOSS",   "2.0"))
SL_BUFFER_PIPS   = int(os.environ.get("SL_BUFFER_PIPS",    "5"))

# Runtime balance (updated via /setbalance command)
runtime_balance = {"value": ACCOUNT_BALANCE}

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── SGT TIMESTAMP ──────────────────────────────────────────────────────────────
def sgt_now() -> str:
    return datetime.now(SGT).strftime("%d %b %H:%M SGT")

def sgt_full() -> str:
    return datetime.now(SGT).strftime("%B %d, %Y %H:%M SGT")

# ── LIVE GOLD PRICE (OANDA + gold-api cross-check) ────────────────────────────
async def get_live_price() -> str:
    oanda_price = None
    goldapi_price = None

    if OANDA_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api-fxtrade.oanda.com/v3/instruments/XAU_USD/candles?count=1&granularity=S5&price=M",
                    headers={"Authorization": f"Bearer {OANDA_TOKEN}"}
                )
                data = resp.json()
                oanda_price = round(float(data["candles"][0]["mid"]["c"]), 3)
                logger.info(f"OANDA: ${oanda_price}")
        except Exception as e:
            logger.warning(f"OANDA failed: {e}")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://www.goldapi.io/api/XAU/USD",
                headers={"x-access-token": "goldapi-free"}
            )
            data = resp.json()
            goldapi_price = round(float(data.get("price", 0)), 2)
            logger.info(f"GoldAPI: ${goldapi_price}")
    except Exception as e:
        logger.warning(f"GoldAPI failed: {e}")

    if oanda_price and goldapi_price:
        diff_pct = (abs(oanda_price - goldapi_price) / oanda_price) * 100
        if diff_pct < 0.1:
            return f"{oanda_price} [OANDA Live] ✅ verified (vs gold-api: {diff_pct:.3f}%)"
        else:
            return f"{oanda_price} [OANDA Live] ⚠️ (gold-api shows ${goldapi_price})"
    if oanda_price:
        return f"{oanda_price} [OANDA only]"
    if goldapi_price:
        return f"{goldapi_price} [gold-api only]"
    return ""

def parse_price(price_str: str) -> float:
    try:
        return float(str(price_str).split()[0].replace(",",""))
    except Exception:
        return 0.0

# ── LIVE DXY PRICE ─────────────────────────────────────────────────────────────
async def get_dxy_price() -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?interval=1m&range=1d",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            data = resp.json()
            price = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
            return str(round(float(price), 2))
    except Exception as e:
        logger.warning(f"DXY failed: {e}")
        return ""

# ── LIVE OIL PRICE ─────────────────────────────────────────────────────────────
async def get_oil_price() -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/CL=F?interval=1m&range=1d",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            data = resp.json()
            price = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
            return str(round(float(price), 2))
    except Exception as e:
        logger.warning(f"Oil failed: {e}")
        return ""

# ── FETCH ALL LIVE DATA ────────────────────────────────────────────────────────
async def get_all_live_data() -> dict:
    gold, dxy, oil = await asyncio.gather(
        get_live_price(), get_dxy_price(), get_oil_price(),
        return_exceptions=True
    )
    return {
        "gold": gold if isinstance(gold, str) else "",
        "dxy":  dxy  if isinstance(dxy,  str) else "",
        "oil":  oil  if isinstance(oil,  str) else "",
    }

# ── RISK CALCULATOR ────────────────────────────────────────────────────────────
def calculate_risk(entry_price: float = 0, sl_price: float = 0) -> dict:
    bal = runtime_balance["value"]
    max_loss = bal * (MAX_RISK_PCT / 100)
    daily_max = bal * (MAX_DAILY_LOSS / 100)
    rec_sl_pips = round(max_loss / PIP_VALUE)

    if sl_price and entry_price:
        sl_pips = round(abs(entry_price - sl_price) * 100)
        sl_cost = round(sl_pips * PIP_VALUE, 2)
        risk_pct = round((sl_cost / bal) * 100, 2)
        acceptable = risk_pct <= MAX_RISK_PCT
    else:
        sl_pips = rec_sl_pips
        sl_cost = round(max_loss, 2)
        risk_pct = MAX_RISK_PCT
        acceptable = True

    return {
        "balance": bal,
        "max_loss": round(max_loss, 2),
        "daily_max": round(daily_max, 2),
        "rec_sl_pips": rec_sl_pips,
        "sl_pips": sl_pips,
        "sl_cost": sl_cost,
        "risk_pct": risk_pct,
        "acceptable": acceptable
    }

def format_risk_block(entry: str, sl: str) -> str:
    try:
        e = parse_price(entry)
        s = parse_price(sl)
        if e <= 0 or s <= 0:
            return ""
        r = calculate_risk(e, s)
        status = "✅ OK" if r["acceptable"] else "❌ TOO HIGH — reduce size!"
        return (
            f"\n💰 *Risk Check:*\n"
            f"SL: {r['sl_pips']} pips = ${r['sl_cost']} | {r['risk_pct']}% {status}\n"
            f"Max allowed: ${r['max_loss']} ({MAX_RISK_PCT}%) | Daily: ${r['daily_max']}"
        )
    except Exception:
        return ""

# ── ECONOMIC CALENDAR ──────────────────────────────────────────────────────────
async def get_market_news() -> dict:
    today = datetime.now(SGT)
    weekday = today.weekday()
    weekly_events = {
        0: ["📊 ISM Manufacturing (if scheduled) 10PM"],
        1: ["📊 ISM Services PMI 10PM SGT", "🏦 RBA Rate Decision (varies)"],
        2: ["📊 ADP Employment 8:15PM SGT", "🛢 EIA Oil Inventory 10:30PM SGT"],
        3: ["📊 US Jobless Claims 8:30PM SGT", "🏦 ECB Rate (monthly)"],
        4: ["💥 NFP Non-Farm Payrolls 8:30PM SGT BIGGEST!", "📊 Unemployment Rate 8:30PM SGT"],
    }
    todays_events = weekly_events.get(weekday, ["No major scheduled events today"])

    news_prompt = f"""You are a financial news assistant. Today: {today.strftime('%A %B %d, %Y %H:%M SGT')}

Search the web for:
1. Major economic events or data releases TODAY affecting gold
2. Latest Federal Reserve news
3. Latest Iran-US conflict update (last 6 hours)
4. Any surprise geopolitical or central bank news
5. Upcoming events next 24 hours

Return ONLY valid JSON:
{{"breaking_news":["news 1","news 2"],"fed_update":"Fed news one line","iran_update":"Iran news one line","upcoming_events":["event 1 with SGT time","event 2"],"gold_impact":"bullish or bearish or neutral","impact_reason":"one sentence","risk_level":"HIGH or MEDIUM or LOW","safe_to_trade":true}}"""

    try:
        data = await gemini_analysis(news_prompt)
        if todays_events:
            existing = data.get("upcoming_events", [])
            data["upcoming_events"] = list(set(existing + todays_events))[:5]
        return data
    except Exception as e:
        logger.warning(f"News failed: {e}")
        return {
            "breaking_news": ["Unable to fetch — check investing.com"],
            "fed_update": "Check reuters.com for Fed news",
            "iran_update": "Check reuters.com for Iran news",
            "upcoming_events": todays_events,
            "gold_impact": "neutral",
            "impact_reason": "No live data available",
            "risk_level": "MEDIUM",
            "safe_to_trade": True
        }

def format_news(n: dict) -> str:
    ts = sgt_now()
    impact = {"bullish":"🟢 BULLISH","bearish":"🔴 BEARISH","neutral":"🟡 NEUTRAL"}.get(n.get("gold_impact","neutral"),"🟡 NEUTRAL")
    risk = {"HIGH":"🔴 HIGH","MEDIUM":"🟡 MEDIUM","LOW":"🟢 LOW"}.get(n.get("risk_level","MEDIUM"),"🟡 MEDIUM")
    safe = "✅ OK to trade" if n.get("safe_to_trade") else "❌ WAIT — news risk!"
    breaking = "\n".join(f"• {b}" for b in n.get("breaking_news",[])[:4])
    upcoming = "\n".join(f"• {e}" for e in n.get("upcoming_events",[])[:5])
    return (
        f"📰 *GOLD MARKET NEWS*\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{impact} | Risk: {risk}\n"
        f"Trade now: {safe}\n\n"
        f"🚨 *Breaking:*\n{breaking}\n\n"
        f"🏦 Fed: _{n.get('fed_update','—')}_\n"
        f"🌍 Iran: _{n.get('iran_update','—')}_\n\n"
        f"📅 *Upcoming (SGT):*\n{upcoming}\n\n"
        f"💡 _{n.get('impact_reason','—')}_\n"
        f"⏰ {ts}\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Always check news BEFORE trading!"
    )

# ── EXTRACT JSON ───────────────────────────────────────────────────────────────
def extract_json(text: str) -> dict:
    text = text.replace("```json","").replace("```","").strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start != -1 and end > start:
        text = text[start:end]
    return json.loads(text)

# ── GEMINI AI ──────────────────────────────────────────────────────────────────
async def gemini_analysis(prompt: str, retries: int = 2) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_KEY}"
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                resp = await client.post(url, json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.1}
                })
                data = resp.json()
                if data.get("error",{}).get("code") in (503, 429):
                    logger.warning(f"Gemini {data['error']['code']} attempt {attempt+1}")
                    if attempt < retries-1:
                        await asyncio.sleep(5)
                        continue
                    raise ValueError(f"Gemini error: {data.get('error')}")
                try:
                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                except (KeyError, IndexError) as e:
                    raise ValueError(f"Gemini parse error: {data.get('error', str(e))}")
                return extract_json(text)
        except ValueError:
            raise
        except Exception as e:
            if attempt < retries-1:
                await asyncio.sleep(3)
                continue
            raise
    raise ValueError("Gemini failed after retries")

# ── CLAUDE AI ──────────────────────────────────────────────────────────────────
async def claude_analysis(prompt: str) -> dict:
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01"
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1500,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}]
            }
        )
        data = resp.json()
        text = "".join(b["text"] for b in data.get("content",[]) if b.get("type")=="text")
        if not text:
            raise ValueError("Claude returned empty response")
        return extract_json(text)

# ── ANALYSIS PROMPT ────────────────────────────────────────────────────────────
def build_analysis_prompt(live: dict) -> str:
    today  = sgt_full()
    gold_h = f"LIVE XAU/USD (OANDA): ${live['gold']}" if live["gold"] else "Search web for XAU/USD price (~$4,500-$5,000 in May 2026)"
    dxy_h  = f"LIVE DXY: {live['dxy']}" if live["dxy"] else "Search web for DXY index level"
    oil_h  = f"LIVE WTI OIL: ${live['oil']}" if live["oil"] else "Search web for WTI oil price"
    weekday = datetime.now(SGT).weekday()
    event_warn = {
        2: "WARNING: ADP Employment 8:15PM SGT today — high volatility expected!",
        3: "WARNING: Jobless Claims 8:30PM SGT today — volatility expected!",
        4: "CRITICAL WARNING: NFP Non-Farm Payrolls 8:30PM SGT TODAY — DO NOT TRADE BEFORE RELEASE!"
    }.get(weekday, "")

    return f"""You are Aden Yang's professional gold trading AI. Singapore time: {today}
{event_warn}

LIVE MARKET DATA (use these exact values, do NOT search for price):
{gold_h}
{dxy_h}
{oil_h}
IMPORTANT: Gold is $4,500-$5,000 range in 2026. Ignore any 2024 price data.

TASK: Search web ONLY for news and market sentiment. Use live data above for prices.

STEP 1 - SEARCH WEB for latest news only:
Search for Iran-US conflict update, Fed news, any market-moving events next 2 hours.

STEP 2 - DETERMINE TRENDS from live price context:
Weekly trend, Daily trend, 4H trend (bullish/bearish/neutral)

STEP 3 - TECHNICAL ANALYSIS:
RSI estimate, MACD direction, key support/resistance levels, Fibonacci levels

STEP 4 - NEWS RISK CHECK:
If major news in next 2 hours: set news_filter=true, signal=WAIT
NFP/FOMC/CPI = always WAIT

STEP 5 - GENERATE SIGNAL:
BUY if 2+ timeframes bullish + no news risk
SELL if 2+ timeframes bearish + no news risk
WAIT if mixed OR news risk

STEP 6 - SCORE out of 100 (be GENEROUS with scoring):
Multi-TF (0-20): 10 if 1 TF clear, 20 if 2+ agree
DXY (0-20): below 100 = 15-20pts, above 103 = 0-5pts
RSI (0-15): 30-50 range = 10, below 35 = 15, neutral = 5
SR Level (0-15): within $30 of level = 10, AT level = 15
News (0-15): Iran war ongoing = min 8pts always, fresh catalyst = 15
Pattern (0-10): any price action = 5, clear pattern = 10
External (0-5): 3 baseline, 5 if confirming signals
MINIMUM SCORE MUST BE 25. Never give 0 on news when Iran war is ongoing.

Return ONLY valid JSON no markdown:
{{"price":"4721","signal":"BUY","entry":"4715","sl":"4700","tp1":"4740","tp2":"4760","rr":"1:2","session":"London","score_total":72,"score_multitf":15,"score_dxy":18,"score_rsi":10,"score_sr_level":10,"score_news":12,"score_pattern":5,"score_external":2,"weekly_trend":"bullish","daily_trend":"bullish","h4_trend":"neutral","rsi_value":"42","rsi_signal":"oversold","macd":"bullish","pattern_found":"higher lows","dxy":"98.15","dxy_trend":"falling","oil":"99.17","iran_update":"Iran peace talks progressing","key_support":"4700","key_resistance":"4760","fib_level":"4715 (38.2%)","reason":"DXY below 100 supports gold. Iran tensions provide safe haven demand.","risk_warning":"","news_filter":false,"trade_now":true}}"""

# ── CROSS-CHECK PROMPT ─────────────────────────────────────────────────────────
def build_crosscheck_prompt(signal_text: str, live: dict) -> str:
    today  = sgt_full()
    gold_h = f"LIVE XAU/USD (OANDA): ${live['gold']}" if live["gold"] else "Search for XAU/USD (~$4,500-$5,000 in 2026)"
    dxy_h  = f"LIVE DXY: {live['dxy']}" if live["dxy"] else "Search for current DXY"
    oil_h  = f"LIVE OIL: ${live['oil']}" if live["oil"] else "Search for current oil"

    return f"""You are Aden Yang's professional gold trading AI. Singapore time: {today}

LIVE DATA:
{gold_h}
{dxy_h}
{oil_h}

FORWARDED SIGNAL:
{signal_text}

TASKS:
1. Extract: direction, entry, SL, TP, channel name from signal
2. Search web for latest gold news only
3. Run multi-timeframe analysis using live data
4. Cross-reference and give verdict

SCORING (generous, same as main analysis):
Multi-TF (0-20): 10 if 1 TF clear, 20 if 2+ agree
DXY (0-20): below 100 = 15-20, above 103 = 0-5
RSI (0-15): 30-50 = 10, below 35 = 15, neutral = 5
SR (0-15): within $30 = 10, AT level = 15
News (0-15): Iran war = min 8 always, catalyst = 15
Pattern (0-10): any action = 5, clear = 10
MINIMUM SCORE: 25

VERDICT RULES:
CONFIRMED = score >= 70 AND AI agrees with direction
MIXED = score 50-69 OR partially agrees
REJECTED = score < 50 OR AI disagrees strongly

Return ONLY valid JSON no markdown:
{{"source_direction":"BUY","source_entry":"4700","source_sl":"4680","source_tp":"4730","source_name":"United Signals","current_price":"4705","ai_direction":"BUY","ai_agrees":true,"confidence":72,"score_total":72,"score_multitf":15,"score_dxy":18,"score_rsi":10,"score_sr_level":10,"score_news":12,"score_pattern":5,"score_external":2,"weekly_trend":"bullish","daily_trend":"bullish","h4_trend":"bullish","dxy":"98.15","dxy_trend":"falling","iran_update":"Peace talks progressing","rsi_value":"42","rsi_signal":"oversold","pattern_found":"higher lows","verdict":"CONFIRMED","recommended_entry":"4700","recommended_sl":"4683","recommended_tp1":"4725","recommended_tp2":"4750","recommended_rr":"1:2","reason":"DXY below 100 and bullish momentum confirm buy.","risk_warning":""}}"""

# ── FORMAT SIGNAL ──────────────────────────────────────────────────────────────
def format_signal(a: dict, source="AI") -> str:
    e  = {"BUY":"🟢","SELL":"🔴","WAIT":"🟡"}.get(a.get("signal","WAIT"),"⚪")
    d  = "📉" if a.get("dxy_trend")=="falling" else "📈" if a.get("dxy_trend")=="rising" else "➡️"
    ti = lambda t: "🟢" if t=="bullish" else "🔴" if t=="bearish" else "🟡"
    si = {"Asian":"🌏","London":"🇬🇧","New York":"🇺🇸","Overlap":"⚡"}.get(a.get("session",""),"🕐")
    sc = a.get("score_total", 0)
    sb = "█"*(sc//10) + "░"*(10-sc//10)
    ts = sgt_now()

    if a.get("signal") == "WAIT":
        return (
            f"⚖️ *ADEN GOLD AI v3.2 — {source}*\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🟡 *WAIT* | 💰 ${a.get('price','—')}\n{sb} {sc}/100\n"
            f"{si} {a.get('session','—')}\n\n"
            f"📊 *TF:* W:{ti(a.get('weekly_trend','neutral'))} {a.get('weekly_trend','—').upper()} | "
            f"D:{ti(a.get('daily_trend','neutral'))} {a.get('daily_trend','—').upper()} | "
            f"4H:{ti(a.get('h4_trend','neutral'))} {a.get('h4_trend','—').upper()}\n\n"
            f"📈 *Score {sc}/100:*\n"
            f"MTF:{a.get('score_multitf',0)}/20 DXY:{a.get('score_dxy',0)}/20 RSI:{a.get('score_rsi',0)}/15\n"
            f"S/R:{a.get('score_sr_level',0)}/15 News:{a.get('score_news',0)}/15 Pat:{a.get('score_pattern',0)}/10\n\n"
            f"{d} DXY:{a.get('dxy','—')} ({a.get('dxy_trend','—')}) | 🛢${a.get('oil','—')}\n"
            f"📍 S:${a.get('key_support','—')} R:${a.get('key_resistance','—')}\n"
            f"🌍 _{a.get('iran_update','—')}_\n"
            f"💡 _{a.get('reason','—')}_\n"
            f"{'⚠️ '+a.get('risk_warning') if a.get('risk_warning') else ''}\n"
            f"⏰ {ts}"
        )

    risk_block = format_risk_block(a.get('entry','0'), a.get('sl','0'))
    return (
        f"⚖️ *ADEN GOLD AI v3.2 — {source}*\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{e} *{a.get('signal','—')}* | 💰 ${a.get('price','—')}\n{sb} {sc}/100\n"
        f"{si} {a.get('session','—')}\n\n"
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
        f"MTF:{a.get('score_multitf',0)}/20 DXY:{a.get('score_dxy',0)}/20 RSI:{a.get('score_rsi',0)}/15\n"
        f"S/R:{a.get('score_sr_level',0)}/15 News:{a.get('score_news',0)}/15 Pat:{a.get('score_pattern',0)}/10\n"
        f"🕯 {a.get('pattern_found','none')} | 📐 {a.get('fib_level','none')}\n\n"
        f"{d} DXY:{a.get('dxy','—')} ({a.get('dxy_trend','—')}) | 🛢${a.get('oil','—')}\n"
        f"📍 S:${a.get('key_support','—')} R:${a.get('key_resistance','—')}\n"
        f"🌍 _{a.get('iran_update','—')}_\n"
        f"💡 _{a.get('reason','—')}_\n"
        f"{'⚠️ '+a.get('risk_warning') if a.get('risk_warning') else ''}\n"
        f"⏰ {ts}\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ SL before entry | Max 2u | 0.7-1% target"
    )

# ── FORMAT CROSS-CHECK ─────────────────────────────────────────────────────────
def format_crosscheck(a: dict) -> str:
    ve = {"CONFIRMED":"✅","MIXED":"⚠️","REJECTED":"❌"}.get(a.get("verdict","MIXED"),"❓")
    ae = "🟢" if a.get("ai_direction")=="BUY" else "🔴" if a.get("ai_direction")=="SELL" else "🟡"
    se = "🟢" if a.get("source_direction")=="BUY" else "🔴" if a.get("source_direction")=="SELL" else "🟡"
    ti = lambda t: "🟢" if t=="bullish" else "🔴" if t=="bearish" else "🟡"
    d  = "📉" if a.get("dxy_trend")=="falling" else "📈" if a.get("dxy_trend")=="rising" else "➡️"
    sc = a.get("score_total",0)
    sb = "█"*(sc//10) + "░"*(10-sc//10)
    ts = sgt_now()
    risk_block = format_risk_block(a.get("recommended_entry","0"), a.get("recommended_sl","0"))

    return (
        f"⚖️ *SIGNAL CROSS-CHECK v3.2*\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{ve} *{a.get('verdict','—')}* | {sb} {a.get('confidence',0)}%\n\n"
        f"📨 *Source ({a.get('source_name','Unknown')}):*\n"
        f"{se} {a.get('source_direction','—')} | "
        f"Entry:${a.get('source_entry','—')} SL:${a.get('source_sl','—')} TP:${a.get('source_tp','—')}\n\n"
        f"🤖 *AI Check:*\n"
        f"{ae} {a.get('ai_direction','—')} | Agrees:{'✅' if a.get('ai_agrees') else '❌'} | "
        f"Now:${a.get('current_price','—')}\n\n"
        f"📊 *TF:* W:{ti(a.get('weekly_trend','neutral'))} {a.get('weekly_trend','—').upper()} | "
        f"D:{ti(a.get('daily_trend','neutral'))} {a.get('daily_trend','—').upper()} | "
        f"4H:{ti(a.get('h4_trend','neutral'))} {a.get('h4_trend','—').upper()}\n\n"
        f"📈 *Score {sc}/100:*\n"
        f"MTF:{a.get('score_multitf',0)}/20 DXY:{a.get('score_dxy',0)}/20 RSI:{a.get('score_rsi',0)}/15\n"
        f"S/R:{a.get('score_sr_level',0)}/15 News:{a.get('score_news',0)}/15 Pat:{a.get('score_pattern',0)}/10\n"
        f"🕯 {a.get('pattern_found','none')}\n\n"
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

# ── DETECT TRADING SIGNAL ──────────────────────────────────────────────────────
def is_trading_signal(text: str) -> bool:
    keywords = [
        "buy","sell","entry","sl:","tp:","stop loss","take profit",
        "xau","gold","signal","long","short","target","pips",
        "limit","breakout","support","resistance","bullish","bearish"
    ]
    return sum(1 for k in keywords if k in text.lower()) >= 1

# ── COMMANDS ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bal = runtime_balance["value"]
    await update.message.reply_text(
        "⚖️ *ADEN GOLD AI BOT v3.2*\n━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 Multi-TF + Pattern + 100pt Scoring\n"
        "💰 Live: OANDA + gold-api + Yahoo Finance\n"
        "⏰ Singapore Time (SGT) ✅\n"
        "🔄 Auto cross-reference signals\n"
        f"💼 Balance: ${bal} | Pip: ${PIP_VALUE}\n\n"
        "*Commands:*\n"
        "/signal — Full Claude analysis\n"
        "/quick — Fast Gemini (free)\n"
        "/news — Latest news + events\n"
        "/risk — Risk calculator\n"
        "/setbalance — Update your balance\n"
        "/crossref — How to forward signals\n"
        "/rules — Trading rules\n"
        "/status — Bot status\n\n"
        "*Forward signals from:*\n"
        "📊 United Signals\n📊 SureShotFX\n"
        "📊 FXPremiere\n📊 Uncle Lim Journey\n\n"
        "_Forward or paste = instant cross-check!_ 💪",
        parse_mode="Markdown"
    )

async def cmd_setbalance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        args = ctx.args
        if not args:
            bal = runtime_balance["value"]
            r = calculate_risk()
            await update.message.reply_text(
                f"💼 *Current Balance: ${bal}*\n\n"
                f"To update type:\n`/setbalance 2000`\n\n"
                f"Max risk/trade: ${r['max_loss']} ({MAX_RISK_PCT}%)\n"
                f"Daily limit: ${r['daily_max']} ({MAX_DAILY_LOSS}%)",
                parse_mode="Markdown"
            )
            return

        new_bal = float(args[0].replace(",","").replace("$",""))
        old_bal = runtime_balance["value"]
        runtime_balance["value"] = new_bal
        r = calculate_risk()

        await update.message.reply_text(
            f"✅ *Balance Updated!*\n━━━━━━━━━━━━━━\n"
            f"Old: ${old_bal}\n"
            f"New: *${new_bal}*\n\n"
            f"📊 *New Risk Limits:*\n"
            f"Max/trade: *${r['max_loss']}* ({MAX_RISK_PCT}%)\n"
            f"Daily max: *${r['daily_max']}* ({MAX_DAILY_LOSS}%)\n"
            f"Rec SL: *{r['rec_sl_pips']} pips*\n\n"
            f"_Type /risk for full breakdown_",
            parse_mode="Markdown"
        )
    except (IndexError, ValueError):
        await update.message.reply_text(
            "❌ Invalid format.\nUse: `/setbalance 2000`",
            parse_mode="Markdown"
        )

async def cmd_risk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    r = calculate_risk()
    ts = sgt_now()
    hour = datetime.now(SGT).hour
    if 15 <= hour < 20:
        session, rec_sl = "London 🇬🇧", "25-35 pips"
    elif 20 <= hour < 24:
        session, rec_sl = "New York 🇺🇸", "20-30 pips"
    else:
        session, rec_sl = "Asian 🌏", "15-25 pips"

    await update.message.reply_text(
        f"💰 *RISK MANAGEMENT CALCULATOR*\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 Balance: *${r['balance']}*\n"
        f"📊 Pip Value: ${PIP_VALUE}/pip\n"
        f"⚠️ Max Risk: {MAX_RISK_PCT}% per trade\n"
        f"🛑 Daily Limit: {MAX_DAILY_LOSS}%\n\n"
        f"🎯 *Current Limits:*\n"
        f"Max loss/trade: *${r['max_loss']}*\n"
        f"Max daily loss: *${r['daily_max']}*\n"
        f"Recommended SL: *{r['rec_sl_pips']} pips*\n\n"
        f"⏰ Session: {session}\n"
        f"📐 Ideal SL now: {rec_sl}\n"
        f"🔵 Buffer: {SL_BUFFER_PIPS} pips beyond S/R\n\n"
        f"*SL Cost Guide:*\n"
        f"15 pips = ${round(15*PIP_VALUE,2)}\n"
        f"25 pips = ${round(25*PIP_VALUE,2)}\n"
        f"50 pips = ${round(50*PIP_VALUE,2)}\n"
        f"75 pips = ${round(75*PIP_VALUE,2)}\n"
        f"100 pips = ${round(100*PIP_VALUE,2)}\n\n"
        f"💡 _Place SL {SL_BUFFER_PIPS} pips BEYOND_\n"
        f"_nearest support/resistance!_\n"
        f"_Update balance: /setbalance 2000_\n"
        f"⏰ {ts}",
        parse_mode="Markdown"
    )

async def cmd_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "⏳ *Fetching live data + Claude analysis...*",
        parse_mode="Markdown"
    )
    try:
        live = await get_all_live_data()
        logger.info(f"Live: gold={live['gold']} dxy={live['dxy']} oil={live['oil']}")
        prompt = build_analysis_prompt(live)
        source = "CLAUDE"
        try:
            a = await claude_analysis(prompt)
        except Exception as e1:
            logger.warning(f"Claude failed: {e1}")
            a = await gemini_analysis(prompt)
            source = "GEMINI"
        if live["gold"]:
            raw = parse_price(live["gold"])
            if raw > 0 and abs(parse_price(a.get("price","0")) - raw) > 200:
                a["price"] = str(raw)
        await msg.edit_text(format_signal(a, source), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Signal error: {e}")
        await msg.edit_text(f"❌ Analysis failed.\n`{str(e)[:150]}`", parse_mode="Markdown")

async def cmd_quick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "⏳ *Fetching live data + Gemini analysis...*",
        parse_mode="Markdown"
    )
    try:
        live = await get_all_live_data()
        logger.info(f"Live: gold={live['gold']} dxy={live['dxy']} oil={live['oil']}")
        prompt = build_analysis_prompt(live)
        a = await gemini_analysis(prompt)
        if live["gold"]:
            raw = parse_price(live["gold"])
            if raw > 0 and abs(parse_price(a.get("price","0")) - raw) > 200:
                a["price"] = str(raw)
        await msg.edit_text(format_signal(a, "GEMINI"), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Quick error: {e}")
        await msg.edit_text(f"❌ Gemini failed: {str(e)[:150]}\nTry /signal instead.", parse_mode="Markdown")

async def cmd_news(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ *Fetching latest news...*", parse_mode="Markdown")
    try:
        news = await get_market_news()
        await msg.edit_text(format_news(news), parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ News failed: {str(e)[:100]}", parse_mode="Markdown")

async def cmd_crossref(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📨 *Cross-Reference Guide:*\n\n"
        "*Forward method:*\n"
        "1. Open signal channel\n"
        "2. Long press signal message\n"
        "3. Tap Forward\n"
        "4. Select @AdenGoldAI_bot ✅\n\n"
        "*Paste method:*\n"
        "Paste signal text directly here!\n\n"
        "*Results:*\n"
        "✅ CONFIRMED = Trade it!\n"
        "⚠️ MIXED = Be careful!\n"
        "❌ REJECTED = Skip it!",
        parse_mode="Markdown"
    )

async def cmd_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bal = runtime_balance["value"]
    r = calculate_risk()
    await update.message.reply_text(
        "📋 *ADEN'S TRADING RULES v3.2*\n━━━━━━━━━━━━━━━━━━━━━━\n"
        "★ SL BEFORE entry — always!\n"
        "★ Max 2u position size\n"
        f"★ Max loss/trade: ${r['max_loss']} ({MAX_RISK_PCT}%)\n"
        f"★ Max daily loss: ${r['daily_max']} ({MAX_DAILY_LOSS}%)\n"
        f"★ Recommended SL: {r['rec_sl_pips']} pips\n"
        "★ TP = 10-15 pips only\n"
        "★ Daily target = 0.7-1%\n"
        "★ 2 losses = STOP today\n"
        "★ Target hit = LOG OFF!\n"
        "★ Gold only — no USD/JPY!\n"
        "★ Score >= 70 to trade!\n"
        "★ No trading before NFP/FOMC!\n"
        "★ London/NY sessions only!\n\n"
        "*SAR:* SET → ADJUST → RUN\n\n"
        f"_Balance: ${bal} | Update: /setbalance_\n"
        "_Small profits beat big losses!_ 💪",
        parse_mode="Markdown"
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ts = sgt_now()
    bal = runtime_balance["value"]
    await update.message.reply_text(
        f"🤖 *BOT STATUS v3.2*\n━━━━━━━━━━━━━━\n"
        f"✅ Online | ⏰ {ts}\n"
        f"💼 Balance: ${bal}\n"
        f"📊 On-demand mode\n"
        f"💰 Live: OANDA + gold-api + Yahoo Finance\n"
        f"🤖 Claude Haiku + Gemini 2.5 Flash\n"
        f"🔄 Cross-check: Active\n\n"
        f"*Signal Channels:*\n"
        f"📊 United Signals\n"
        f"📊 SureShotFX\n"
        f"📊 FXPremiere\n"
        f"📊 Uncle Lim Journey",
        parse_mode="Markdown"
    )

# ── HANDLE FORWARDED / PASTED SIGNALS ─────────────────────────────────────────
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    text = update.message.text or update.message.caption or ""
    logger.info(f"MSG: {text[:60]}")
    if not text:
        await update.message.reply_text("Empty message received.")
        return

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
            "⏳ *Cross-referencing signal...*\n_Fetching live data + Multi-TF analysis_",
            parse_mode="Markdown"
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
            logger.error(f"Crosscheck error: {e}")
            await msg.edit_text(
                f"❌ Cross-check failed.\nTry /quick for analysis.\n`{str(e)[:150]}`",
                parse_mode="Markdown"
            )
    else:
        await update.message.reply_text(
            "💬 No signal detected.\n\n"
            "/signal — Full analysis\n"
            "/quick — Fast analysis\n"
            "/news — Market news\n"
            "/risk — Risk calculator\n"
            "Forward a signal to cross-check!"
        )

# ── ERROR HANDLER ──────────────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception: {context.error}")
    try:
        if update and hasattr(update, "message") and update.message:
            await update.message.reply_text(
                f"⚠️ Error occurred. Try /quick or /signal.\n`{str(context.error)[:100]}`",
                parse_mode="Markdown"
            )
    except Exception:
        pass

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("signal",      cmd_signal))
    app.add_handler(CommandHandler("quick",       cmd_quick))
    app.add_handler(CommandHandler("news",        cmd_news))
    app.add_handler(CommandHandler("risk",        cmd_risk))
    app.add_handler(CommandHandler("setbalance",  cmd_setbalance))
    app.add_handler(CommandHandler("crossref",    cmd_crossref))
    app.add_handler(CommandHandler("rules",       cmd_rules))
    app.add_handler(CommandHandler("status",      cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    logger.info("⚖️ Aden Gold AI Bot v3.2 started!")
    logger.info(f"⏰ SGT: {sgt_now()} | Balance: ${runtime_balance['value']}")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
