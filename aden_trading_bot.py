"""
Aden Gold AI v3.1
XAU/USD-only Telegram bot powered by Claude with web search.
Uses a Weldon-style intermarket scoring system to gate alerts.

ALL CREDENTIALS MUST BE PROVIDED VIA ENVIRONMENT VARIABLES.
Required env vars on Render:
    TELEGRAM_TOKEN     -- from @BotFather
    ANTHROPIC_KEY      -- from console.anthropic.com
Optional:
    ALERT_CHAT_ID      -- where auto-alerts are sent (default: @Aden_Yang)
    CHECK_INTERVAL     -- seconds between auto-checks (default: 900 = 15 min)
    ALERT_THRESHOLD    -- min confidence % to fire alert (default: 70)
    CLAUDE_MODEL       -- override Claude model id
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY")
ALERT_CHAT_ID = os.environ.get("ALERT_CHAT_ID", "@Aden_Yang")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "900"))   # 15 min default
ALERT_THRESHOLD = int(os.environ.get("ALERT_THRESHOLD", "70"))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")

SGT = timezone(timedelta(hours=8))
COOLDOWN_MINUTES = 15
PRICE_DELTA_TRIGGER_PCT = 0.3

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Memory of last alert to avoid spam
last_alert: dict = {}

# ─── WELDON SCORING SYSTEM ────────────────────────────────────────────────────
SCORE_WEIGHTS = {
    "dxy_aligned": 20,
    "multi_tf_aligned": 20,
    "fibonacci_level": 15,
    "candlestick_pattern": 15,
    "oil_aligned": 10,
    "volume_confirms": 10,
    "news_catalyst": 10,
}
SCORE_LABELS = {
    "dxy_aligned": "DXY aligned",
    "multi_tf_aligned": "Multi-TF aligned",
    "fibonacci_level": "Fibonacci level",
    "candlestick_pattern": "Candlestick pattern",
    "oil_aligned": "Oil aligned",
    "volume_confirms": "Volume confirms",
    "news_catalyst": "News catalyst",
}


def compute_confidence(scores: dict) -> int:
    return min(sum(w for k, w in SCORE_WEIGHTS.items() if scores.get(k)), 100)


# ─── PROMPT ───────────────────────────────────────────────────────────────────
GOLD_PROMPT = """You are Aden Yang's personal trading AI for XAU/USD (gold).

Apply Larry Weldon-style intermarket + technical analysis. Score each of the 7
factors below as TRUE only if you have concrete evidence; otherwise FALSE.
Be honest — if you cannot confirm a factor with current data, set it false.

THE 7 SCORING FACTORS:
1. DXY aligned (+20)         — US Dollar Index direction supports the trade
                               (falling DXY = bullish gold; rising DXY = bearish)
2. Multi-TF aligned (+20)    — M15, H1, H4 all point the same direction
3. Fibonacci level (+15)     — entry near 38.2 / 50 / 61.8 / 78.6 retracement
4. Candlestick pattern (+15) — pin bar, engulfing, hammer, doji, shooting star at S/R
5. Oil aligned (+10)         — WTI rising = bullish gold (inflation hedge correlation)
6. Volume confirms (+10)     — volume rising into the trend direction
7. News catalyst (+10)       — Fed/CPI/NFP/geopolitical event supports direction

Use web_search NOW to gather:
- Current XAU/USD spot price (real-time)
- Current DXY level and short-term trend
- Current WTI crude oil price and trend
- Last 2 hours of market-moving news (Fed, CPI, NFP, Iran, Israel, Russia, central bank gold buying)
- RSI / MACD readings on M15, H1, H4 if available
- Today's session: Asian / London / New York / Overlap

Respond with ONLY a valid JSON object (no markdown fences, no preamble):
{
  "asset": "XAUUSD",
  "price": "<current price as string>",
  "session": "Asian|London|New York|Overlap",
  "direction": "BUY|SELL|WAIT",
  "scores": {
    "dxy_aligned": true|false,
    "multi_tf_aligned": true|false,
    "fibonacci_level": true|false,
    "candlestick_pattern": true|false,
    "oil_aligned": true|false,
    "volume_confirms": true|false,
    "news_catalyst": true|false
  },
  "score_reasons": {
    "dxy_aligned": "<short reason or 'no data'>",
    "multi_tf_aligned": "<short reason or 'no data'>",
    "fibonacci_level": "<short reason or 'no data'>",
    "candlestick_pattern": "<short reason or 'no data'>",
    "oil_aligned": "<short reason or 'no data'>",
    "volume_confirms": "<short reason or 'no data'>",
    "news_catalyst": "<short reason or 'no data'>"
  },
  "entry": "<price>",
  "stop_loss": "<price>",
  "take_profit_1": "<price>",
  "take_profit_2": "<price>",
  "rr_ratio": "<e.g. 1:2>",
  "key_support": "<price>",
  "key_resistance": "<price>",
  "fib_level": "<e.g. '61.8% at 4520' or 'none'>",
  "candlestick": "<pattern observed or 'none'>",
  "rsi_h1": "<value or 'n/a'>",
  "macd_h1": "bullish|bearish|neutral",
  "summary": "<2-sentence synthesis combining intermarket + technical>",
  "risk_warning": "<one-line warning if major risk, else empty string>"
}

If direction is WAIT, still fill entry/SL/TP with the levels you'd watch, but
make clear in summary that no trade is recommended right now."""


# ─── CLAUDE CALL ──────────────────────────────────────────────────────────────
async def get_analysis() -> dict:
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 1500,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": GOLD_PROMPT}],
            },
        )
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Claude API error: {data['error']}")

    text_parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    full = "".join(text_parts).strip().replace("```json", "").replace("```", "").strip()

    start = full.find("{")
    end = full.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON in response: {full[:200]}")
    return json.loads(full[start : end + 1])


# ─── FORMATTING ───────────────────────────────────────────────────────────────
def emoji_for(direction: str) -> str:
    return {"BUY": "🟢", "SELL": "🔴", "WAIT": "🟡"}.get(direction, "⚪")


def confidence_bar(pct: int) -> str:
    filled = pct // 10
    return "█" * filled + "░" * (10 - filled)


def format_alert(a: dict, alert_type: str = "MANUAL") -> str:
    confidence = compute_confidence(a.get("scores", {}))
    e = emoji_for(a.get("direction", "WAIT"))
    now = datetime.now(SGT).strftime("%d %b %H:%M SGT")

    scores = a.get("scores", {})
    reasons = a.get("score_reasons", {})
    score_lines = []
    for key, weight in SCORE_WEIGHTS.items():
        check = "✅" if scores.get(key) else "❌"
        reason = (reasons.get(key) or "")[:60]
        score_lines.append(f"{check} {SCORE_LABELS[key]} (+{weight}) — _{reason}_")
    score_block = "\n".join(score_lines)

    direction = a.get("direction", "WAIT")
    risk = a.get("risk_warning", "") or ""

    if direction == "WAIT":
        return (
            f"⚖️ *ADEN GOLD AI — {alert_type}*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"{e} *XAU/USD: WAIT*\n"
            f"💰 Price: *{a.get('price', '—')}*\n"
            f"🎯 Confidence: {confidence}%\n"
            f"`{confidence_bar(confidence)}`\n\n"
            f"📊 *Score breakdown:*\n{score_block}\n\n"
            f"📍 Support: {a.get('key_support', '—')}\n"
            f"📍 Resistance: {a.get('key_resistance', '—')}\n\n"
            f"💡 _{a.get('summary', '')}_\n\n"
            f"⏰ {now}"
        )

    body = (
        f"⚖️ *ADEN GOLD AI — {alert_type}*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{e} *XAU/USD: {direction}*\n"
        f"💰 Price: *{a.get('price', '—')}*\n"
        f"🎯 Confidence: *{confidence}%*\n"
        f"`{confidence_bar(confidence)}`\n\n"
        f"🎯 *SAR SETUP:*\n"
        f"┌──────────────────\n"
        f"│ Entry: `{a.get('entry', '—')}`\n"
        f"│ SL:    `{a.get('stop_loss', '—')}`\n"
        f"│ TP1:   `{a.get('take_profit_1', '—')}`\n"
        f"│ TP2:   `{a.get('take_profit_2', '—')}`\n"
        f"│ R:R:   `{a.get('rr_ratio', '—')}`\n"
        f"└──────────────────\n\n"
        f"📊 *Score breakdown:*\n{score_block}\n\n"
        f"📍 S: {a.get('key_support', '—')} | R: {a.get('key_resistance', '—')}\n"
        f"📈 Fib: {a.get('fib_level', '—')}\n"
        f"🕯 Candle: {a.get('candlestick', '—')}\n"
        f"RSI(H1): {a.get('rsi_h1', '—')} | MACD: {a.get('macd_h1', '—')}\n\n"
        f"💡 _{a.get('summary', '')}_\n"
    )
    if risk:
        body += f"\n⚠️ {risk}\n"
    body += (
        f"\n⏰ {now}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"✅ SL BEFORE entry\n"
        f"✅ Max 2u | 2 losses = STOP\n"
        f"✅ Daily target 0.7–1%"
    )
    return body


# ─── COMMAND HANDLERS ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚖️ *ADEN GOLD AI v3.1*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🤖 Claude + Weldon scoring\n"
        f"📡 Auto-checks every {CHECK_INTERVAL // 60} min\n"
        f"🔔 Alert if confidence ≥ {ALERT_THRESHOLD}%\n\n"
        "*Commands:*\n"
        "/gold — XAU/USD analysis now\n"
        "/score — explain scoring\n"
        "/rules — Aden's trading rules\n"
        "/status — bot status\n\n"
        "_DXY+20, MultiTF+20, Fib+15, Candle+15, Oil+10, Vol+10, News+10_",
        parse_mode="Markdown",
    )


async def cmd_gold(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Analysing XAU/USD (Claude + Weldon)...")
    try:
        a = await get_analysis()
        await msg.edit_text(format_alert(a, "MANUAL"), parse_mode="Markdown")
    except Exception as e:
        logger.exception("Analysis failed")
        await msg.edit_text(f"❌ Error: `{str(e)[:200]}`", parse_mode="Markdown")


async def cmd_score(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lines = ["📊 *WELDON SCORING SYSTEM*", "━━━━━━━━━━━━━━━━━━━"]
    total = 0
    for k, w in SCORE_WEIGHTS.items():
        lines.append(f"{SCORE_LABELS[k]:<22} +{w}")
        total += w
    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append(f"Max confidence         {total}%")
    lines.append("")
    lines.append(f"*Alert threshold: {ALERT_THRESHOLD}%+*")
    lines.append("_Below threshold = WAIT (no auto-alert)_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *ADEN'S TRADING RULES*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "★ SL BEFORE entry — always!\n"
        "★ Max 2u at $1,500–2,000 balance\n"
        "★ Max SL ~ 15 pts (~$9 loss)\n"
        "★ TP = 10–15 pts (small wins!)\n"
        "★ Daily target 0.7–1%\n"
        "★ 2 losses = STOP for the day\n"
        "★ After target hit = LOG OFF\n"
        "★ Gold only — NO USD/JPY!\n"
        "★ No trading before big news\n"
        "★ Phone away = more profit\n\n"
        "*SAR Method:*\n"
        "SET → Limit + SL + TP\n"
        "ADJUST → TP1 hit: close 50%\n"
        "RUN → Let rest go to TP2 free!\n\n"
        "_Small profits beat big losses!_ 💪",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lines = [
        "🤖 *BOT STATUS*",
        "━━━━━━━━━━━━━━",
        "✅ Online",
        f"⏱️ Auto-check: every {CHECK_INTERVAL // 60} min",
        f"🎯 Alert threshold: {ALERT_THRESHOLD}%",
        "📈 Tracking: XAU/USD",
        "",
        "*Last alert:*",
    ]
    if not last_alert:
        lines.append("_No alerts yet this session_")
    else:
        lines.append(
            f"{last_alert['signal']} @ {last_alert['price']} ({last_alert['time_str']})"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── AUTO ALERT LOOP ──────────────────────────────────────────────────────────
async def auto_check(ctx: ContextTypes.DEFAULT_TYPE):
    global last_alert
    try:
        logger.info("[AUTO] Checking XAU/USD")
        a = await get_analysis()
        confidence = compute_confidence(a.get("scores", {}))
        direction = a.get("direction", "WAIT")
        price = a.get("price", "0")
        now = datetime.now(SGT)

        if direction not in ("BUY", "SELL") or confidence < ALERT_THRESHOLD:
            logger.info(f"[NO ALERT] {direction} {confidence}%")
            return

        # Cooldown: skip same signal within 15 min unless price moved enough
        if last_alert and last_alert.get("signal") == direction:
            age_min = (now - last_alert["time_dt"]).total_seconds() / 60
            try:
                pct_move = abs(float(price) - float(last_alert["price"])) / float(last_alert["price"]) * 100
            except (ValueError, TypeError, ZeroDivisionError):
                pct_move = 0
            if age_min < COOLDOWN_MINUTES and pct_move < PRICE_DELTA_TRIGGER_PCT:
                logger.info(f"[COOLDOWN] same {direction}")
                return

        text = "🚨 *AUTO ALERT — STRONG SIGNAL!*\n\n" + format_alert(a, "AUTO")
        await ctx.bot.send_message(chat_id=ALERT_CHAT_ID, text=text, parse_mode="Markdown")
        last_alert = {
            "signal": direction,
            "price": price,
            "time_dt": now,
            "time_str": now.strftime("%d %b %H:%M SGT"),
        }
        logger.info(f"[ALERT SENT] {direction} {confidence}%")

    except Exception:
        logger.exception("Auto-check failed")


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Missing TELEGRAM_TOKEN env var")
    if not ANTHROPIC_KEY:
        raise RuntimeError("Missing ANTHROPIC_KEY env var")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("gold", cmd_gold))
    app.add_handler(CommandHandler("signal", cmd_gold))   # legacy alias
    app.add_handler(CommandHandler("sar", cmd_gold))      # legacy alias
    app.add_handler(CommandHandler("score", cmd_score))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("status", cmd_status))

    app.job_queue.run_repeating(auto_check, interval=CHECK_INTERVAL, first=30)

    logger.info(
        f"Aden Gold AI v3.1 up — interval={CHECK_INTERVAL}s, threshold={ALERT_THRESHOLD}%"
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
