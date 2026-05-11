import asyncio
import logging
import json
import httpx
import os
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ── CONFIG ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_KEY",  "YOUR_CLAUDE_API_KEY_HERE")
GEMINI_KEY     = os.environ.get("GEMINI_KEY",      "YOUR_GEMINI_API_KEY_HERE")
OANDA_TOKEN    = os.environ.get("OANDA_TOKEN",     "")
OANDA_ACCOUNT  = os.environ.get("OANDA_ACCOUNT",   "")

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── OANDA LIVE PRICE WITH CROSS-CHECK ─────────────────────────────────────────
async def get_live_price() -> str:
    """Get live XAU/USD price from OANDA + cross-check with gold-api"""
    oanda_price = None
    goldapi_price = None

    # Fetch OANDA price
    if OANDA_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api-fxtrade.oanda.com/v3/instruments/XAU_USD/candles?count=1&granularity=S5&price=M",
                    headers={"Authorization": f"Bearer {OANDA_TOKEN}"}
                )
                data = resp.json()
                oanda_price = round(float(data["candles"][0]["mid"]["c"]), 3)
                logger.info(f"OANDA price: ${oanda_price}")
        except Exception as e:
            logger.warning(f"OANDA failed: {e}")

    # Fetch gold-api price
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://www.goldapi.io/api/XAU/USD",
                headers={"x-access-token": "goldapi-free"}
            )
            data = resp.json()
            goldapi_price = round(float(data.get("price", 0)), 2)
            logger.info(f"GoldAPI price: ${goldapi_price}")
    except Exception as e:
        logger.warning(f"GoldAPI failed: {e}")

    # Cross-check both prices
    if oanda_price and goldapi_price:
        diff = abs(oanda_price - goldapi_price)
        diff_pct = (diff / oanda_price) * 100
        if diff_pct < 0.1:  # Within 0.1% = verified ✅
            logger.info(f"Price verified ✅ OANDA:${oanda_price} GoldAPI:${goldapi_price} diff:{diff_pct:.3f}%")
            return f"{oanda_price} ✅ verified (vs gold-api: {diff_pct:.3f}%)"
        else:  # Prices differ — use OANDA but flag it
            logger.warning(f"Price mismatch ⚠️ OANDA:${oanda_price} GoldAPI:${goldapi_price} diff:{diff_pct:.3f}%")
            return f"{oanda_price} ⚠️ (gold-api shows ${goldapi_price})"

    # Single source fallback
    if oanda_price:
        return f"{oanda_price} (OANDA only)"
    if goldapi_price:
        return f"{goldapi_price} (gold-api only)"
    return ""

# ── EXTRACT JSON SAFELY ────────────────────────────────────────────────────────
def extract_json(text: str) -> dict:
    text = text.replace("```json", "").replace("```", "").strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start != -1 and end > start:
        text = text[start:end]
    return json.loads(text)

# ── GEMINI FREE AI ─────────────────────────────────────────────────────────────
async def gemini_analysis(prompt: str) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_KEY}"
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(url, json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1}
        })
        data = resp.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise ValueError(f"Gemini error: {data.get('error', str(e))}")
        return extract_json(text)

# ── CLAUDE PREMIUM AI ──────────────────────────────────────────────────────────
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
        text = "".join(b["text"] for b in data.get("content", []) if b.get("type") == "text")
        if not text:
            raise ValueError("Claude returned empty response")
        return extract_json(text)

# ── ANALYSIS PROMPT ────────────────────────────────────────────────────────────
def build_analysis_prompt(context="", live_price=""):
    today = datetime.now().strftime("%B %d, %Y %H:%M SGT")
    price_hint = f"LIVE XAU/USD PRICE FROM OANDA: ${live_price}" if live_price else "Search web for current XAU/USD price (should be in $4,500-$5,000 range in 2026)"
    return f"""You are Aden Yang's professional gold trading AI.

TODAY: {today}
{price_hint}
IMPORTANT: Gold is trading ~$4,500-$5,000 in 2026. Do NOT use 2024 prices ($2,000-$2,500).

{context}

Search the web for current XAU/USD data. Analyse using institutional methods:

1. MULTI-TIMEFRAME: Weekly + Daily + 4H trends (bullish/bearish/neutral)
2. KEY DATA: Gold price, DXY level+direction, Oil price, Iran news
3. INDICATORS: RSI value+signal, MACD direction, Support/Resistance levels
4. PATTERNS: Candlestick patterns, Higher/Lower highs, Fibonacci levels
5. SCORING out of 100:
   - Multi-TF aligned: 0-20
   - DXY confirmed: 0-20
   - RSI signal: 0-15
   - Support/Resistance: 0-15
   - News catalyst: 0-15
   - Pattern found: 0-10
   - External signal: 0-5

RULES: Only BUY if 2+ TF bullish. Only SELL if 2+ TF bearish. WAIT if mixed or score below 60.

Return ONLY valid JSON, no markdown, no explanation:
{{"price":"4700","signal":"BUY","entry":"4695","sl":"4680","tp1":"4720","tp2":"4745","rr":"1:2","session":"London","score_total":75,"score_multitf":15,"score_dxy":20,"score_rsi":10,"score_sr_level":15,"score_news":10,"score_pattern":5,"score_external":0,"weekly_trend":"bullish","daily_trend":"bullish","h4_trend":"neutral","rsi_value":"35","rsi_signal":"oversold","macd":"bullish","pattern_found":"hammer","dxy":"98.50","dxy_trend":"falling","oil":"104","iran_update":"Peace talks ongoing","key_support":"4680","key_resistance":"4750","fib_level":"4700 (38.2%)","reason":"DXY falling supports gold. RSI oversold at support.","risk_warning":"","trade_now":true}}"""

# ── CROSS-CHECK PROMPT ─────────────────────────────────────────────────────────
def build_crosscheck_prompt(signal_text: str):
    return f"""You are Aden Yang's professional gold trading AI.

A signal was forwarded from a Telegram channel:
{signal_text}

Tasks:
1. Extract: direction, entry, SL, TP, channel name
2. Search current gold price and market conditions
3. Run multi-timeframe analysis
4. Cross-reference and give verdict

CONFIRMED = score>=70 AND AI agrees
MIXED = score 50-69 OR partial agreement
REJECTED = score<50 OR AI disagrees

Return ONLY valid JSON, no markdown:
{{"source_direction":"BUY","source_entry":"4700","source_sl":"4680","source_tp":"4730","source_name":"United Signals","current_price":"4705","ai_direction":"BUY","ai_agrees":true,"confidence":78,"score_total":78,"score_multitf":15,"score_dxy":20,"score_rsi":10,"score_sr_level":15,"score_news":10,"score_pattern":8,"weekly_trend":"bullish","daily_trend":"bullish","h4_trend":"bullish","dxy":"98.20","dxy_trend":"falling","iran_update":"Peace talks ongoing","rsi_value":"38","rsi_signal":"oversold","pattern_found":"none","verdict":"CONFIRMED","recommended_entry":"4700","recommended_sl":"4683","recommended_tp1":"4725","recommended_tp2":"4750","recommended_rr":"1:2","reason":"DXY falling and all timeframes bullish confirm buy signal.","risk_warning":""}}"""

# ── FORMAT SIGNAL ──────────────────────────────────────────────────────────────
def format_signal(a: dict, source="AI") -> str:
    e = {"BUY":"🟢","SELL":"🔴","WAIT":"🟡"}.get(a.get("signal","WAIT"),"⚪")
    d = "📉" if a.get("dxy_trend")=="falling" else "📈" if a.get("dxy_trend")=="rising" else "➡️"
    ti = lambda t: "🟢" if t=="bullish" else "🔴" if t=="bearish" else "🟡"
    si = {"Asian":"🌏","London":"🇬🇧","New York":"🇺🇸","Overlap":"⚡"}.get(a.get("session",""),"🕐")
    ts = datetime.now().strftime("%d %b %H:%M SGT")
    sc = a.get("score_total",0)
    sb = "█"*(sc//10) + "░"*(10-sc//10)

    if a.get("signal") == "WAIT":
        return (
            f"⚖️ *ADEN GOLD AI v3 — {source}*\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🟡 *WAIT* | 💰 ${a.get('price','—')}\n{sb} {sc}/100\n"
            f"{si} {a.get('session','—')}\n\n"
            f"📊 *TF:* W:{ti(a.get('weekly_trend','neutral'))} D:{ti(a.get('daily_trend','neutral'))} 4H:{ti(a.get('h4_trend','neutral'))}\n\n"
            f"📈 *Score:* MTF:{a.get('score_multitf',0)} DXY:{a.get('score_dxy',0)} RSI:{a.get('score_rsi',0)} S/R:{a.get('score_sr_level',0)} News:{a.get('score_news',0)} Pat:{a.get('score_pattern',0)}\n\n"
            f"{d} DXY:{a.get('dxy','—')} | 🛢${a.get('oil','—')}\n"
            f"📍 S:${a.get('key_support','—')} R:${a.get('key_resistance','—')}\n"
            f"🌍 _{a.get('iran_update','—')}_\n💡 _{a.get('reason','—')}_\n⏰ {ts}"
        )

    return (
        f"⚖️ *ADEN GOLD AI v3 — {source}*\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{e} *{a.get('signal','—')}* | 💰 ${a.get('price','—')}\n{sb} {sc}/100\n"
        f"{si} {a.get('session','—')}\n\n"
        f"🎯 *SAR:*\n"
        f"┌ 📍 Entry: `${a.get('entry','—')}`\n"
        f"│ 🛑 SL:    `${a.get('sl','—')}`\n"
        f"│ 🎯 TP1:   `${a.get('tp1','—')}`\n"
        f"│ 🏆 TP2:   `${a.get('tp2','—')}`\n"
        f"└ ⚖️ R:R:   `{a.get('rr','—')}`\n\n"
        f"📊 *TF:* W:{ti(a.get('weekly_trend','neutral'))} {a.get('weekly_trend','—').upper()} | D:{ti(a.get('daily_trend','neutral'))} {a.get('daily_trend','—').upper()} | 4H:{ti(a.get('h4_trend','neutral'))} {a.get('h4_trend','—').upper()}\n\n"
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
    d = "📉" if a.get("dxy_trend")=="falling" else "📈" if a.get("dxy_trend")=="rising" else "➡️"
    sc = a.get("score_total",0)
    sb = "█"*(sc//10) + "░"*(10-sc//10)
    ts = datetime.now().strftime("%d %b %H:%M SGT")

    return (
        f"⚖️ *SIGNAL CROSS-CHECK*\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{ve} *{a.get('verdict','—')}* | {sb} {a.get('confidence',0)}%\n\n"
        f"📨 *Source ({a.get('source_name','Unknown')}):*\n"
        f"{se} {a.get('source_direction','—')} | Entry:${a.get('source_entry','—')} SL:${a.get('source_sl','—')} TP:${a.get('source_tp','—')}\n\n"
        f"🤖 *AI Check:*\n"
        f"{ae} {a.get('ai_direction','—')} | Agrees:{'✅' if a.get('ai_agrees') else '❌'} | Now:${a.get('current_price','—')}\n\n"
        f"📊 *TF:* W:{ti(a.get('weekly_trend','neutral'))} {a.get('weekly_trend','—').upper()} | D:{ti(a.get('daily_trend','neutral'))} {a.get('daily_trend','—').upper()} | 4H:{ti(a.get('h4_trend','neutral'))} {a.get('h4_trend','—').upper()}\n\n"
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
        f"└ ⚖️ R:R:   `{a.get('recommended_rr','—')}`\n\n"
        f"💡 _{a.get('reason','—')}_\n"
        f"{'⚠️ '+a.get('risk_warning') if a.get('risk_warning') else ''}\n"
        f"⏰ {ts}\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ SL before entry | Max 2u | 0.7-1% target"
    )

# ── DETECT SIGNAL ──────────────────────────────────────────────────────────────
def is_trading_signal(text: str) -> bool:
    keywords = ["buy","sell","entry","sl:","tp:","stop loss","take profit",
                "xau","gold","signal","long","short","target","pips",
                "limit","breakout","support","resistance","bullish","bearish"]
    text_lower = text.lower()
    return sum(1 for k in keywords if k in text_lower) >= 1

# ── COMMANDS ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚖️ *ADEN GOLD AI BOT v3.0*\n━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 Multi-TF + Pattern + 100pt Scoring\n"
        "🔄 Auto cross-reference any signal\n\n"
        "*Commands:*\n"
        "/signal — Full Claude analysis\n"
        "/quick — Fast Gemini analysis (free)\n"
        "/crossref — How to forward signals\n"
        "/rules — Trading rules\n"
        "/status — Bot status\n\n"
        "*Forward signals from:*\n"
        "📊 United Signals\n📊 SureShotFX\n"
        "📊 FXPremiere\n📊 Uncle Lim Journey\n\n"
        "_Forward = instant cross-check!_ 💪",
        parse_mode="Markdown"
    )

async def cmd_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "⏳ *Analysing with Claude AI...*\n_Fetching OANDA live price + Multi-TF + Scoring_",
        parse_mode="Markdown"
    )
    try:
        live_price = await get_live_price()
        prompt = build_analysis_prompt(live_price=live_price)
        a = await claude_analysis(prompt)
        if live_price and abs(float(a.get("price","0").replace(",","")) - float(live_price)) > 500:
            a["price"] = live_price
        await msg.edit_text(format_signal(a, "CLAUDE"), parse_mode="Markdown")
    except Exception as e1:
        logger.error(f"Claude failed: {e1}")
        try:
            live_price = await get_live_price()
            prompt = build_analysis_prompt(live_price=live_price)
            a = await gemini_analysis(prompt)
            if live_price and abs(float(a.get("price","0").replace(",","")) - float(live_price)) > 500:
                a["price"] = live_price
            await msg.edit_text(format_signal(a, "GEMINI"), parse_mode="Markdown")
        except Exception as e2:
            await msg.edit_text(f"❌ Both APIs failed.\nCheck keys in Render.\n`{str(e2)[:150]}`", parse_mode="Markdown")

async def cmd_quick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ *Gemini quick analysis...*", parse_mode="Markdown")
    try:
        live_price = await get_live_price()
        prompt = build_analysis_prompt(live_price=live_price)
        a = await gemini_analysis(prompt)
        if live_price and abs(float(a.get("price","0").replace(",","")) - float(live_price)) > 500:
            a["price"] = live_price
        await msg.edit_text(format_signal(a, "GEMINI"), parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ Gemini failed: {str(e)[:150]}\nTry /signal instead.", parse_mode="Markdown")

async def cmd_crossref(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📨 *Cross-Reference Guide:*\n\n"
        "*Forward method:*\n"
        "1. Open signal channel\n"
        "2. Long press signal message\n"
        "3. Tap Forward\n"
        "4. Select @AdenGoldAI_bot ✅\n\n"
        "*Paste method:*\n"
        "Just paste signal text here!\n\n"
        "*Results:*\n"
        "✅ CONFIRMED = Trade it!\n"
        "⚠️ MIXED = Be careful!\n"
        "❌ REJECTED = Skip it!",
        parse_mode="Markdown"
    )

async def cmd_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *ADEN'S TRADING RULES*\n━━━━━━━━━━━━━━━━━━━━━━\n"
        "★ SL BEFORE entry — always!\n"
        "★ Max 2u at $1,500-$2,000\n"
        "★ Max SL = 15 pts (~$9)\n"
        "★ TP = 10-15 pts only\n"
        "★ Daily target = 0.7-1%\n"
        "★ 2 losses = STOP today\n"
        "★ Target hit = LOG OFF!\n"
        "★ Gold only — no USD/JPY!\n"
        "★ Score >= 70 to trade!\n\n"
        "*SAR:* SET → ADJUST → RUN\n"
        "_Small profits beat big losses!_ 💪",
        parse_mode="Markdown"
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *BOT STATUS v3.0*\n━━━━━━━━━━━━━━\n"
        "✅ Online | 📊 On-demand mode\n"
        "🤖 Claude Haiku + Gemini Flash\n"
        "🔄 Auto cross-check: Active\n\n"
        "*Channels:*\n"
        "📊 United Signals\n"
        "📊 SureShotFX\n"
        "📊 FXPremiere\n"
        "📊 Uncle Lim Journey",
        parse_mode="Markdown"
    )

# ── HANDLE FORWARDED / PASTED SIGNALS ─────────────────────────────────────────
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = update.message.text or update.message.caption or ""

    # Debug logging — check Render logs to see what's coming in
    logger.info(f"MSG: {text[:60]}")
    logger.info(f"FWD_DATE:{update.message.forward_date} FWD_FROM:{update.message.forward_from} FWD_CHAT:{update.message.forward_from_chat} FWD_ORIGIN:{getattr(update.message,'forward_origin',None)}")

    if not text:
        await update.message.reply_text("⚠️ Empty message received.")
        return

    # Detect ALL Telegram forward types
    is_forwarded = any([
        update.message.forward_date is not None,
        update.message.forward_from is not None,
        update.message.forward_from_chat is not None,
        getattr(update.message, "forward_origin", None) is not None,
    ])

    is_signal = is_trading_signal(text)
    logger.info(f"is_forwarded:{is_forwarded} is_signal:{is_signal}")

    if is_forwarded or is_signal:
        msg = await update.message.reply_text(
            "⏳ *Cross-referencing signal...*\n_Fetching live price + Multi-TF scoring_",
            parse_mode="Markdown"
        )
        try:
            live_price = await get_live_price()
            today = datetime.now().strftime("%B %d, %Y %H:%M SGT")
            price_context = (
                f"\nToday: {today}\nLIVE XAU/USD from OANDA: ${live_price}"
                if live_price else
                f"\nToday: {today}\nGold ~$4,500-$5,000 in 2026. Do NOT use 2024 prices."
            )
            prompt = build_crosscheck_prompt(text + price_context)
            try:
                a = await claude_analysis(prompt)
            except Exception as ce:
                logger.warning(f"Claude failed: {ce} — using Gemini")
                a = await gemini_analysis(prompt)
            # Override wrong price
            try:
                if live_price:
                    raw = float(live_price.split()[0])
                    ai_p = float(a.get("current_price","0").replace(",",""))
                    if abs(ai_p - raw) > 200:
                        a["current_price"] = str(raw)
            except Exception:
                pass
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
            "Forward a signal to cross-check!"
        )

# ── ERROR HANDLER ──────────────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception: {context.error}")
    try:
        if update and hasattr(update, "message") and update.message:
            await update.message.reply_text(
                f"⚠️ Bot error occurred.\nTry /quick or /signal again.\n`{str(context.error)[:100]}`",
                parse_mode="Markdown"
            )
    except Exception:
        pass

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("signal",   cmd_signal))
    app.add_handler(CommandHandler("quick",    cmd_quick))
    app.add_handler(CommandHandler("crossref", cmd_crossref))
    app.add_handler(CommandHandler("rules",    cmd_rules))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    logger.info("⚖️ Aden Gold AI Bot v3.0 started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
