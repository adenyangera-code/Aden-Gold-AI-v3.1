import asyncio
import logging
import json
import httpx
import os
import re
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ── CONFIG ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_KEY",  "YOUR_CLAUDE_API_KEY_HERE")
GEMINI_KEY     = os.environ.get("GEMINI_KEY",      "YOUR_GEMINI_API_KEY_HERE")
CHAT_ID        = "@Aden_Yang"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── GEMINI FREE ANALYSIS ───────────────────────────────────────────────────────
async def gemini_analysis(prompt: str) -> dict:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_KEY}"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1}
        })
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        clean = text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean)

# ── CLAUDE PREMIUM ANALYSIS ────────────────────────────────────────────────────
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
        text = "".join(
            b["text"] for b in data.get("content", [])
            if b.get("type") == "text"
        )
        clean = text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean)

# ── FULL GOLD ANALYSIS PROMPT ──────────────────────────────────────────────────
def build_analysis_prompt(extra_context=""):
    return f"""You are Aden Yang's professional gold trading AI.

{extra_context}

Search for current XAU/USD data and analyse using these institutional-grade methods:

STEP 1 - MULTI-TIMEFRAME ANALYSIS:
Search gold price on Weekly, Daily and 4H timeframes.
Determine trend direction for each: bullish/bearish/neutral

STEP 2 - KEY DATA:
- Current XAU/USD price
- DXY US Dollar Index level and direction
- WTI Oil price
- Latest Iran-US war news (last 2 hours)
- US Treasury 10yr yield

STEP 3 - TECHNICAL INDICATORS:
- RSI reading and signal (oversold <30 = bullish, overbought >70 = bearish)
- MACD direction (bullish/bearish)
- Price vs key support/resistance levels
- Any candlestick patterns (hammer, shooting star, engulfing, doji)

STEP 4 - PATTERN RECOGNITION:
- Higher highs/lows forming? (bullish trend)
- Lower highs/lows forming? (bearish trend)
- Price at support or resistance?
- Any Fibonacci levels nearby?

Return ONLY valid JSON (no markdown):
{{
  "price": "<current gold price>",
  "signal": "BUY" or "SELL" or "WAIT",
  "entry": "<price>",
  "sl": "<price>",
  "tp1": "<price>",
  "tp2": "<price>",
  "rr": "<ratio>",
  "session": "Asian" or "London" or "New York" or "Overlap",
  "score_total": <0-100>,
  "score_multitf": <0-20>,
  "score_dxy": <0-20>,
  "score_rsi": <0-15>,
  "score_sr_level": <0-15>,
  "score_news": <0-15>,
  "score_pattern": <0-10>,
  "score_external": <0-5>,
  "weekly_trend": "bullish" or "bearish" or "neutral",
  "daily_trend": "bullish" or "bearish" or "neutral",
  "h4_trend": "bullish" or "bearish" or "neutral",
  "rsi_value": "<value>",
  "rsi_signal": "oversold" or "overbought" or "neutral",
  "macd": "bullish" or "bearish" or "neutral",
  "pattern_found": "<pattern name or none>",
  "dxy": "<level>",
  "dxy_trend": "falling" or "rising" or "flat",
  "oil": "<price>",
  "iran_update": "<one line max 80 chars>",
  "key_support": "<price>",
  "key_resistance": "<price>",
  "fib_level": "<nearest fib level or none>",
  "news_filter": true or false,
  "reason": "<2 sentences combining all analysis>",
  "risk_warning": "<one line or empty>",
  "trade_now": true or false
}}"""

# ── SIGNAL CROSS-CHECK PROMPT ──────────────────────────────────────────────────
def build_crosscheck_prompt(forwarded_signal: str):
    return f"""You are Aden Yang's professional gold trading AI.

A trading signal has been forwarded from a Telegram signal channel.
Here is the signal:

{forwarded_signal}

TASK:
1. Extract the signal details (direction, entry, SL, TP, source)
2. Search for current gold price and market conditions
3. Run your own multi-timeframe analysis
4. Cross-reference and give verdict

Return ONLY valid JSON (no markdown):
{{
  "source_signal": {{
    "direction": "BUY" or "SELL" or "WAIT",
    "entry": "<price or unknown>",
    "sl": "<price or unknown>",
    "tp": "<price or unknown>",
    "source_name": "<channel name>"
  }},
  "current_price": "<live gold price>",
  "ai_direction": "BUY" or "SELL" or "WAIT",
  "ai_agrees": true or false,
  "confidence": <0-100>,
  "score_total": <0-100>,
  "score_multitf": <0-20>,
  "score_dxy": <0-20>,
  "score_rsi": <0-15>,
  "score_sr_level": <0-15>,
  "score_news": <0-15>,
  "score_pattern": <0-10>,
  "weekly_trend": "bullish" or "bearish" or "neutral",
  "daily_trend": "bullish" or "bearish" or "neutral",
  "h4_trend": "bullish" or "bearish" or "neutral",
  "dxy": "<level>",
  "dxy_trend": "falling" or "rising" or "flat",
  "iran_update": "<one line>",
  "rsi_value": "<value>",
  "rsi_signal": "oversold" or "overbought" or "neutral",
  "pattern_found": "<pattern or none>",
  "verdict": "CONFIRMED" or "MIXED" or "REJECTED",
  "recommended_entry": "<price>",
  "recommended_sl": "<price>",
  "recommended_tp1": "<price>",
  "recommended_tp2": "<price>",
  "recommended_rr": "<ratio>",
  "reason": "<2 sentences>",
  "risk_warning": "<one line or empty>"
}}"""

# ── FORMAT FULL SIGNAL ─────────────────────────────────────────────────────────
def format_signal(a: dict, alert_type="SIGNAL") -> str:
    e = {"BUY": "🟢", "SELL": "🔴", "WAIT": "🟡"}.get(a["signal"], "⚪")
    dxy = "📉" if a["dxy_trend"] == "falling" else "📈" if a["dxy_trend"] == "rising" else "➡️"
    tf_icon = lambda t: "🟢" if t == "bullish" else "🔴" if t == "bearish" else "🟡"
    sess = {"Asian":"🌏","London":"🇬🇧","New York":"🇺🇸","Overlap":"⚡"}.get(a.get("session",""), "🕐")
    time_str = datetime.now().strftime("%d %b %H:%M SGT")

    score = a.get("score_total", 0)
    score_bar = "█" * (score // 10) + "░" * (10 - score // 10)

    if a["signal"] == "WAIT":
        return f"""⚖️ *ADEN GOLD AI v3 — {alert_type}*
━━━━━━━━━━━━━━━━━━━━━━
🟡 *Signal: WAIT*
💰 Gold: *${a["price"]}*
{score_bar} {score}/100

{sess} Session: {a.get("session","—")}

📊 *Multi-Timeframe:*
Weekly: {tf_icon(a["weekly_trend"])} {a["weekly_trend"].upper()}
Daily:  {tf_icon(a["daily_trend"])} {a["daily_trend"].upper()}
4H:     {tf_icon(a["h4_trend"])} {a["h4_trend"].upper()}

📈 *Scoring Breakdown:*
Multi-TF:  {a.get("score_multitf",0)}/20
DXY:       {a.get("score_dxy",0)}/20
RSI:       {a.get("score_rsi",0)}/15
S/R Level: {a.get("score_sr_level",0)}/15
News:      {a.get("score_news",0)}/15
Pattern:   {a.get("score_pattern",0)}/10
External:  {a.get("score_external",0)}/5

{dxy} DXY: {a["dxy"]} ({a["dxy_trend"]})
🛢 Oil: ${a["oil"]}
🌍 Iran: _{a["iran_update"]}_
📍 Support: ${a["key_support"]} | Resistance: ${a["key_resistance"]}

💡 _{a["reason"]}_
⏰ {time_str}"""

    return f"""⚖️ *ADEN GOLD AI v3 — {alert_type}*
━━━━━━━━━━━━━━━━━━━━━━
{e} *Signal: {a["signal"]}*
💰 Gold: *${a["price"]}*
{score_bar} {score}/100

{sess} Session: {a.get("session","—")}

🎯 *SAR SETUP:*
┌──────────────────────
│ 📍 Entry:  `${a["entry"]}`
│ 🛑 SL:     `${a["sl"]}` (max ~$9 at 2u)
│ 🎯 TP1:    `${a["tp1"]}`
│ 🏆 TP2:    `${a["tp2"]}`
│ ⚖️ R:R:    `{a["rr"]}`
└──────────────────────

📊 *Multi-Timeframe:*
Weekly: {tf_icon(a["weekly_trend"])} {a["weekly_trend"].upper()}
Daily:  {tf_icon(a["daily_trend"])} {a["daily_trend"].upper()}
4H:     {tf_icon(a["h4_trend"])} {a["h4_trend"].upper()}

📈 *Scoring Breakdown:*
Multi-TF:  {a.get("score_multitf",0)}/20 | DXY: {a.get("score_dxy",0)}/20
RSI({a.get("rsi_value","—")}): {a.get("score_rsi",0)}/15 | S/R: {a.get("score_sr_level",0)}/15
News: {a.get("score_news",0)}/15 | Pattern: {a.get("score_pattern",0)}/10
🕯 Pattern: {a.get("pattern_found","none")}
📐 Fib: {a.get("fib_level","none")}

{dxy} DXY: {a["dxy"]} ({a["dxy_trend"]})
🛢 Oil: ${a["oil"]}
🌍 Iran: _{a["iran_update"]}_
📍 S: ${a["key_support"]} | R: ${a["key_resistance"]}

💡 _{a["reason"]}_
{"⚠️ " + a.get("risk_warning","") if a.get("risk_warning") else ""}

⏰ {time_str}
━━━━━━━━━━━━━━━━━━━━━━
✅ SL BEFORE entry | Max 2u
✅ 2 losses = STOP | 0.7-1% target"""

# ── FORMAT CROSS-CHECK ─────────────────────────────────────────────────────────
def format_crosscheck(a: dict) -> str:
    verdict_emoji = {"CONFIRMED": "✅", "MIXED": "⚠️", "REJECTED": "❌"}
    v_e = verdict_emoji.get(a["verdict"], "❓")
    src = a.get("source_signal", {})
    ai_e = "🟢" if a["ai_direction"] == "BUY" else "🔴" if a["ai_direction"] == "SELL" else "🟡"
    src_e = "🟢" if src.get("direction") == "BUY" else "🔴" if src.get("direction") == "SELL" else "🟡"
    tf_icon = lambda t: "🟢" if t == "bullish" else "🔴" if t == "bearish" else "🟡"
    dxy = "📉" if a["dxy_trend"] == "falling" else "📈" if a["dxy_trend"] == "rising" else "➡️"
    score = a.get("score_total", 0)
    score_bar = "█" * (score // 10) + "░" * (10 - score // 10)
    time_str = datetime.now().strftime("%d %b %H:%M SGT")

    return f"""⚖️ *SIGNAL CROSS-CHECK*
━━━━━━━━━━━━━━━━━━━━━━
{v_e} *Verdict: {a["verdict"]}*
{score_bar} {a["confidence"]}% confidence

📨 *Source Signal ({src.get("source_name","Unknown")}):*
{src_e} Direction: {src.get("direction","—")}
Entry: `${src.get("entry","—")}` | SL: `${src.get("sl","—")}` | TP: `${src.get("tp","—")}`

🤖 *AI Verification:*
{ai_e} AI says: {a["ai_direction"]}
Agrees: {"✅ YES" if a["ai_agrees"] else "❌ NO"}
Gold now: ${a["current_price"]}

📊 *Multi-Timeframe:*
Weekly: {tf_icon(a["weekly_trend"])} {a["weekly_trend"].upper()}
Daily:  {tf_icon(a["daily_trend"])} {a["daily_trend"].upper()}
4H:     {tf_icon(a["h4_trend"])} {a["h4_trend"].upper()}

📈 *Score: {score}/100*
Multi-TF: {a.get("score_multitf",0)}/20 | DXY: {a.get("score_dxy",0)}/20
RSI({a.get("rsi_value","—")}): {a.get("score_rsi",0)}/15 | S/R: {a.get("score_sr_level",0)}/15
News: {a.get("score_news",0)}/15 | Pattern: {a.get("score_pattern",0)}/10
🕯 Pattern: {a.get("pattern_found","none")}

{dxy} DXY: {a["dxy"]} ({a["dxy_trend"]})
🌍 _{a["iran_update"]}_

🎯 *Recommended SAR:*
┌──────────────────────
│ 📍 Entry: `${a["recommended_entry"]}`
│ 🛑 SL:    `${a["recommended_sl"]}` ✅
│ 🎯 TP1:   `${a["recommended_tp1"]}`
│ 🏆 TP2:   `${a["recommended_tp2"]}`
│ ⚖️ R:R:   `{a["recommended_rr"]}`
└──────────────────────

💡 _{a["reason"]}_
{"⚠️ " + a.get("risk_warning","") if a.get("risk_warning") else ""}

⏰ {time_str}
━━━━━━━━━━━━━━━━━━━━━━
✅ SL BEFORE entry | Max 2u | 0.7-1% target"""

# ── DETECT IF MESSAGE IS A TRADING SIGNAL ─────────────────────────────────────
def is_trading_signal(text: str) -> bool:
    keywords = [
        "buy", "sell", "entry", "sl:", "tp:", "stop loss", "take profit",
        "xau", "gold", "usd/jpy", "signal", "long", "short",
        "bid", "ask", "limit", "target"
    ]
    text_lower = text.lower()
    matches = sum(1 for k in keywords if k in text_lower)
    return matches >= 2

# ── COMMANDS ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚖️ *ADEN GOLD AI BOT v3.0*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 Institutional-grade AI Analysis\n"
        "📊 Multi-timeframe + Pattern Recognition\n"
        "🔄 Cross-reference Free Signal Channels\n"
        "📡 No auto checks — on demand only!\n\n"
        "*Commands:*\n"
        "/signal — Full AI analysis (Claude + multi-TF + scoring)\n"
        "/quick — Fast free analysis (Gemini)\n"
        "/crossref — Manual cross-reference\n"
        "/rules — Trading rules\n"
        "/status — Bot status\n\n"
        "*Auto Cross-Check:*\n"
        "_Forward any signal from United Signals,\n"
        "SureShotFX or FXPremiere — bot analyses\n"
        "and replies CONFIRMED/MIXED/REJECTED!_\n\n"
        "*Sources:* Claude AI + Gemini + Web Search",
        parse_mode="Markdown"
    )

async def cmd_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "⏳ Running institutional-grade analysis...\n"
        "_Multi-TF + Scoring + Pattern Recognition_",
        parse_mode="Markdown"
    )
    try:
        prompt = build_analysis_prompt()
        analysis = await claude_analysis(prompt)
        await msg.edit_text(
            format_signal(analysis, "CLAUDE ANALYSIS"),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Signal error: {e}")
        try:
            prompt = build_analysis_prompt()
            analysis = await gemini_analysis(prompt)
            await msg.edit_text(
                format_signal(analysis, "GEMINI ANALYSIS"),
                parse_mode="Markdown"
            )
        except Exception as e2:
            await msg.edit_text(f"❌ Analysis failed: {str(e2)[:100]}")

async def cmd_quick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Quick Gemini analysis...")
    try:
        prompt = build_analysis_prompt()
        analysis = await gemini_analysis(prompt)
        await msg.edit_text(
            format_signal(analysis, "GEMINI QUICK"),
            parse_mode="Markdown"
        )
    except Exception as e:
        await msg.edit_text(f"❌ Error: {str(e)[:100]}")

async def cmd_crossref(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📨 *How to use cross-reference:*\n\n"
        "1. Open United Signals / SureShotFX / FXPremiere / Uncle Lim Journey\n"
        "2. Long press any signal message\n"
        "3. Tap *Forward*\n"
        "4. Select *@AdenGoldAI_bot*\n"
        "5. Bot analyses and replies instantly!\n\n"
        "_Or paste any signal text directly here!_",
        parse_mode="Markdown"
    )

async def cmd_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *ADEN'S TRADING RULES v3*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "★ SL BEFORE entry — always!\n"
        "★ Max 2u at $1,500–$2,000 balance\n"
        "★ Max SL = 15 pts (~$9 loss)\n"
        "★ TP = 10–15 pts (small wins!)\n"
        "★ Daily target = 0.7–1% only\n"
        "★ 2 losses = STOP for the day\n"
        "★ After target = LOG OFF!\n"
        "★ Gold only — NO USD/JPY!\n"
        "★ No trading before big news!\n"
        "★ Score >= 70 before trading!\n\n"
        "*SAR Method:*\n"
        "SET → Limit order + SL + TP\n"
        "ADJUST → TP1 hit: partial close 50%\n"
        "RUN → Let rest go to TP2 free!\n\n"
        "*Cross-Reference Rule:*\n"
        "Only trade CONFIRMED signals!\n"
        "_Small profits beat big losses!_ 💪",
        parse_mode="Markdown"
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🤖 *BOT STATUS v3.0*\n"
        f"━━━━━━━━━━━━━━\n"
        f"✅ Bot: Online\n"
        f"📊 Mode: On-demand only\n"
        f"🔄 Auto checks: Disabled (save cost)\n"
        f"📡 Cross-check: Active\n"
        f"🤖 Primary AI: Claude Haiku\n"
        f"🆓 Backup AI: Gemini Flash\n"
        f"📱 Signal channels:\n"
        f"   • United Signals\n"
        f"   • SureShotFX\n"
        f"   • FXPremiere\n"
        f"   • Uncle Lim Journey\n\n"
        f"_Forward signals for instant analysis!_",
        parse_mode="Markdown"
    )

# ── AUTO CROSS-CHECK FORWARDED/PASTED SIGNALS ─────────────────────────────────
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = update.message.text or update.message.caption or ""
    if not text:
        return

    # Check if forwarded or contains trading signal
    is_forwarded = update.message.forward_date is not None
    is_signal = is_trading_signal(text)

    if is_forwarded or is_signal:
        msg = await update.message.reply_text(
            "⏳ *Cross-referencing signal...*\n"
            "_Running multi-TF analysis + scoring_",
            parse_mode="Markdown"
        )
        try:
            prompt = build_crosscheck_prompt(text)
            # Try Claude first for better accuracy
            try:
                analysis = await claude_analysis(prompt)
            except Exception:
                analysis = await gemini_analysis(prompt)

            await msg.edit_text(
                format_crosscheck(analysis),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Cross-check error: {e}")
            await msg.edit_text(
                f"❌ Cross-check failed.\n"
                f"Try /signal for fresh analysis.\n"
                f"`{str(e)[:100]}`",
                parse_mode="Markdown"
            )

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("signal",   cmd_signal))
    app.add_handler(CommandHandler("quick",    cmd_quick))
    
    
    app.add_handler(CommandHandler("crossref", cmd_crossref))
    app.add_handler(CommandHandler("rules",    cmd_rules))
    app.add_handler(CommandHandler("status",   cmd_status))

    # Auto cross-check forwarded messages and pasted signals
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_message
    ))

    logger.info("⚖️ Aden Gold AI Bot v3.0 started!")
    logger.info("📡 Waiting for signals to cross-check...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
