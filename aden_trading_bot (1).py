"""
Aden Gold AI v4.0 — DUAL ENGINE (Gemini + Claude)

Architecture:
- Gemini 2.5 Flash analyses XAU/USD every 5 min (cheap, real-time monitoring)
- Claude Sonnet analyses XAU/USD every 15 min (deeper, premium analysis)
- When BOTH agree on direction within a 20-min window → +15 sync bonus,
  alert tagged "DUAL CONFIRMED"
- 7-factor Weldon scoring (max 100% raw) + sync bonus, capped at 100
- Runs on Render Background Worker — never sleeps

ALL CREDENTIALS MUST BE PROVIDED VIA ENV VARS — never hardcoded.
Required:
    TELEGRAM_TOKEN     -- from @BotFather
    ANTHROPIC_KEY      -- from https://console.anthropic.com
    GEMINI_KEY         -- from https://aistudio.google.com/apikey
Optional:
    ALERT_CHAT_ID      (default: @Aden_Yang)
    ALERT_THRESHOLD    (default: 70)
    GEMINI_INTERVAL    (default: 300 seconds = 5 min)
    CLAUDE_INTERVAL    (default: 900 seconds = 15 min)
    SYNC_BONUS         (default: 15)
    GEMINI_MODEL       (default: gemini-2.5-flash)
    CLAUDE_MODEL       (default: claude-sonnet-4-20250514)
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
GEMINI_KEY = os.environ.get("GEMINI_KEY")

ALERT_CHAT_ID = os.environ.get("ALERT_CHAT_ID", "@Aden_Yang")
ALERT_THRESHOLD = int(os.environ.get("ALERT_THRESHOLD", "70"))

GEMINI_INTERVAL = int(os.environ.get("GEMINI_INTERVAL", "300"))   # 5 min
CLAUDE_INTERVAL = int(os.environ.get("CLAUDE_INTERVAL", "900"))   # 15 min
SYNC_BONUS = int(os.environ.get("SYNC_BONUS", "15"))
SYNC_WINDOW_MINUTES = 20                                           # other AI's data is fresh
SYNC_PARTNER_MIN_CONF = 50                                         # filter weak agreement

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")

SGT = timezone(timedelta(hours=8))
COOLDOWN_MINUTES = 15
PRICE_DELTA_TRIGGER_PCT = 0.3

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Shared state
latest_analyses: dict = {"claude": None, "gemini": None}   # each: {a, time_dt, raw_conf}
last_alert: dict = {}                                       # {signal, price, time_dt, time_str}

# ─── WELDON SCORING ───────────────────────────────────────────────────────────
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


def compute_raw_confidence(scores: dict) -> int:
    return min(sum(w for k, w in SCORE_WEIGHTS.items() if scores.get(k)), 100)


def get_sync_partner(provider: str, direction: str, now: datetime):
    """Return the OTHER AI's recent matching analysis if one exists; else None."""
    other = "claude" if provider == "gemini" else "gemini"
    other_result = latest_analyses.get(other)
    if not other_result:
        return None
    age_min = (now - other_result["time_dt"]).total_seconds() / 60
    if age_min > SYNC_WINDOW_MINUTES:
        return None
    other_dir = other_result["a"].get("direction")
    if other_dir != direction:
        return None
    if other_result["raw_conf"] < SYNC_PARTNER_MIN_CONF:
        return None
    return other_result


def compute_total_confidence(provider: str, scores: dict, direction: str, now: datetime):
    raw = compute_raw_confidence(scores)
    partner = get_sync_partner(provider, direction, now)
    bonus = SYNC_BONUS if partner else 0
    total = min(raw + bonus, 100)
    return total, raw, bool(partner), partner


# ─── PROMPT (shared by both providers) ────────────────────────────────────────
GOLD_PROMPT = """You are Aden Yang's personal trading AI for XAU/USD (gold).

Apply Larry Weldon-style intermarket + technical analysis. For each of the 7
factors below, mark TRUE only if you have concrete evidence; otherwise FALSE.
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

Search the web NOW for:
- Current XAU/USD spot price (real-time)
- Current DXY level and short-term trend
- Current WTI crude oil price and trend
- Last 2 hours of market-moving news (Fed, CPI, NFP, Iran, Israel, Russia, central bank gold buying)
- RSI / MACD readings on M15, H1, H4 if available
- Today's session: Asian / London / New York / Overlap

Respond with ONLY a valid JSON object — no markdown fences, no preamble, no explanation outside the JSON.

Schema:
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
  "summary": "<2-sentence synthesis>",
  "risk_warning": "<one-line warning if major risk, else empty string>"
}"""


# ─── ROBUST JSON PARSER (handles both providers' quirks) ──────────────────────
def extract_json(text: str) -> dict:
    """Strips fences, locates outermost JSON object, parses leniently."""
    cleaned = (text or "").strip()
    # Strip markdown fences if Gemini/Claude wraps output
    cleaned = cleaned.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
    # Find outermost { ... } block (handles preamble/postamble)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found. First 300 chars: {text[:300]}")
    candidate = cleaned[start : end + 1]
    return json.loads(candidate)


# ─── ANALYZE: CLAUDE (web_search tool) ────────────────────────────────────────
async def analyse_with_claude() -> dict:
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
    return extract_json("".join(text_parts))


# ─── ANALYZE: GEMINI (Google Search grounding) ────────────────────────────────
async def analyse_with_gemini() -> dict:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    )
    body = {
        "contents": [{"role": "user", "parts": [{"text": GOLD_PROMPT}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.3, "topP": 0.9},
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=body)
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Gemini API error: {data['error']}")
    candidates = data.get("candidates", [])
    if not candidates:
        raise ValueError(f"No candidates in Gemini response: {str(data)[:300]}")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts)
    return extract_json(text)


# ─── FORMATTING ───────────────────────────────────────────────────────────────
def emoji_for(d: str) -> str:
    return {"BUY": "🟢", "SELL": "🔴", "WAIT": "🟡"}.get(d, "⚪")


def confidence_bar(pct: int) -> str:
    return "█" * (pct // 10) + "░" * (10 - pct // 10)


def format_alert(provider: str, a: dict, raw_conf: int, total_conf: int, synced: bool, partner) -> str:
    e = emoji_for(a.get("direction", "WAIT"))
    now = datetime.now(SGT).strftime("%d %b %H:%M SGT")
    direction = a.get("direction", "WAIT")
    risk = a.get("risk_warning", "") or ""

    scores = a.get("scores", {})
    reasons = a.get("score_reasons", {})
    score_lines = []
    for k, w in SCORE_WEIGHTS.items():
        check = "✅" if scores.get(k) else "❌"
        reason = (reasons.get(k) or "")[:55]
        score_lines.append(f"{check} {SCORE_LABELS[k]} (+{w}) — _{reason}_")
    score_block = "\n".join(score_lines)

    sync_block = ""
    if synced and partner:
        other_name = "Claude" if provider == "gemini" else "Gemini"
        partner_dir = partner["a"].get("direction", "—")
        sync_block = (
            f"\n🔥 *DUAL CONFIRMED* (+{SYNC_BONUS}) — "
            f"{other_name} also {partner_dir} @ {partner['raw_conf']}%\n"
        )

    header = f"⚖️ *ADEN GOLD AI — {provider.upper()}*"

    if direction == "WAIT":
        return (
            f"{header}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"{e} *XAU/USD: WAIT*\n"
            f"💰 Price: *{a.get('price', '—')}*\n"
            f"🎯 Confidence: {total_conf}% (raw {raw_conf}%)\n"
            f"`{confidence_bar(total_conf)}`"
            f"{sync_block}\n"
            f"📊 *Scoring:*\n{score_block}\n\n"
            f"📍 S: {a.get('key_support', '—')} | R: {a.get('key_resistance', '—')}\n\n"
            f"💡 _{a.get('summary', '')}_\n\n"
            f"⏰ {now}"
        )

    body = (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{e} *XAU/USD: {direction}*\n"
        f"💰 Price: *{a.get('price', '—')}*\n"
        f"🎯 Confidence: *{total_conf}%* (raw {raw_conf}%)\n"
        f"`{confidence_bar(total_conf)}`"
        f"{sync_block}\n"
        f"🎯 *SAR SETUP:*\n"
        f"┌──────────────────\n"
        f"│ Entry: `{a.get('entry', '—')}`\n"
        f"│ SL:    `{a.get('stop_loss', '—')}`\n"
        f"│ TP1:   `{a.get('take_profit_1', '—')}`\n"
        f"│ TP2:   `{a.get('take_profit_2', '—')}`\n"
        f"│ R:R:   `{a.get('rr_ratio', '—')}`\n"
        f"└──────────────────\n\n"
        f"📊 *Scoring:*\n{score_block}\n\n"
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
        f"✅ Final decision = mine, bot is just a guide"
    )
    return body


# ─── ALERT-SAFE SEND (falls back to plain text if Markdown fails) ─────────────
async def safe_send(bot, chat_id, text):
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except Exception as md_err:
        logger.warning(f"Markdown failed, sending plain: {md_err}")
        await bot.send_message(chat_id=chat_id, text=text)


# ─── SHARED CHECK + ALERT LOGIC ───────────────────────────────────────────────
async def run_check(ctx: ContextTypes.DEFAULT_TYPE, provider: str):
    global last_alert
    try:
        logger.info(f"[{provider.upper()}] Checking XAU/USD")
        a = await (analyse_with_claude() if provider == "claude" else analyse_with_gemini())

        scores = a.get("scores", {})
        direction = a.get("direction", "WAIT")
        price = a.get("price", "0")
        now = datetime.now(SGT)

        raw_conf = compute_raw_confidence(scores)
        # Save own result before checking sync (sync checks the OTHER provider only)
        latest_analyses[provider] = {"a": a, "time_dt": now, "raw_conf": raw_conf}

        total_conf, _, synced, partner = compute_total_confidence(provider, scores, direction, now)

        logger.info(
            f"[{provider.upper()}] {direction} raw={raw_conf}% total={total_conf}% sync={synced}"
        )

        if direction not in ("BUY", "SELL") or total_conf < ALERT_THRESHOLD:
            return

        # Cooldown — applies regardless of provider/sync
        if last_alert and last_alert.get("signal") == direction:
            age_min = (now - last_alert["time_dt"]).total_seconds() / 60
            try:
                pct_move = (
                    abs(float(price) - float(last_alert["price"]))
                    / float(last_alert["price"])
                    * 100
                )
            except (ValueError, TypeError, ZeroDivisionError):
                pct_move = 0
            if age_min < COOLDOWN_MINUTES and pct_move < PRICE_DELTA_TRIGGER_PCT:
                logger.info(f"[COOLDOWN] same {direction} from {provider}, sync={synced}")
                return

        prefix = "🚨🔥 *DUAL-AI ALERT!*" if synced else "🚨 *AUTO ALERT*"
        text = f"{prefix}\n\n" + format_alert(provider, a, raw_conf, total_conf, synced, partner)
        await safe_send(ctx.bot, ALERT_CHAT_ID, text)

        last_alert = {
            "signal": direction,
            "price": price,
            "time_dt": now,
            "time_str": now.strftime("%d %b %H:%M SGT"),
        }
        logger.info(f"[ALERT SENT] {provider} {direction} {total_conf}% sync={synced}")

    except Exception:
        logger.exception(f"[{provider.upper()}] Check failed")


async def gemini_check(ctx):
    await run_check(ctx, "gemini")


async def claude_check(ctx):
    await run_check(ctx, "claude")


# ─── COMMAND HANDLERS ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚖️ *ADEN GOLD AI v4.0 — DUAL ENGINE*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Gemini 2.5 Flash — every {GEMINI_INTERVAL // 60} min\n"
        f"🧠 Claude Sonnet — every {CLAUDE_INTERVAL // 60} min\n"
        f"🔥 Both AIs agree = +{SYNC_BONUS} sync bonus\n"
        f"🔔 Alert if total confidence ≥ {ALERT_THRESHOLD}%\n\n"
        "*Commands:*\n"
        "/gold — Quick analysis (Gemini)\n"
        "/deep — Deep analysis (Claude)\n"
        "/both — Run both side-by-side\n"
        "/score — Explain scoring\n"
        "/rules — Trading rules\n"
        "/status — Bot status\n\n"
        "_DXY+20, MultiTF+20, Fib+15, Candle+15, Oil+10, Vol+10, News+10_",
        parse_mode="Markdown",
    )


async def _manual(update: Update, provider: str, label: str):
    msg = await update.message.reply_text(f"⏳ {label} analysing XAU/USD...")
    try:
        a = await (analyse_with_claude() if provider == "claude" else analyse_with_gemini())
        scores = a.get("scores", {})
        direction = a.get("direction", "WAIT")
        now = datetime.now(SGT)
        raw_conf = compute_raw_confidence(scores)
        latest_analyses[provider] = {"a": a, "time_dt": now, "raw_conf": raw_conf}
        total_conf, _, synced, partner = compute_total_confidence(provider, scores, direction, now)
        text = format_alert(provider, a, raw_conf, total_conf, synced, partner)
        try:
            await msg.edit_text(text, parse_mode="Markdown")
        except Exception:
            await msg.edit_text(text)  # fallback plain
    except Exception as e:
        logger.exception(f"{provider} manual failed")
        await msg.edit_text(f"❌ {label} error: `{str(e)[:200]}`", parse_mode="Markdown")


async def cmd_gold(update, ctx):
    await _manual(update, "gemini", "Gemini")


async def cmd_deep(update, ctx):
    await _manual(update, "claude", "Claude")


async def cmd_both(update, ctx):
    await update.message.reply_text("⏳ Running both AIs side-by-side... ~45s")
    # Gemini first so Claude can see it for sync
    try:
        a_g = await analyse_with_gemini()
        now_g = datetime.now(SGT)
        raw_g = compute_raw_confidence(a_g.get("scores", {}))
        latest_analyses["gemini"] = {"a": a_g, "time_dt": now_g, "raw_conf": raw_g}
    except Exception as e:
        await update.message.reply_text(f"❌ Gemini error: `{str(e)[:200]}`", parse_mode="Markdown")
        return
    try:
        a_c = await analyse_with_claude()
        now_c = datetime.now(SGT)
        raw_c = compute_raw_confidence(a_c.get("scores", {}))
        latest_analyses["claude"] = {"a": a_c, "time_dt": now_c, "raw_conf": raw_c}
    except Exception as e:
        await update.message.reply_text(f"❌ Claude error: `{str(e)[:200]}`", parse_mode="Markdown")
        return

    total_g, _, synced_g, p_g = compute_total_confidence("gemini", a_g.get("scores", {}), a_g.get("direction"), now_g)
    total_c, _, synced_c, p_c = compute_total_confidence("claude", a_c.get("scores", {}), a_c.get("direction"), now_c)

    await safe_send(ctx.bot, update.effective_chat.id, format_alert("gemini", a_g, raw_g, total_g, synced_g, p_g))
    await safe_send(ctx.bot, update.effective_chat.id, format_alert("claude", a_c, raw_c, total_c, synced_c, p_c))


async def cmd_score(update, ctx):
    lines = ["📊 *DUAL-AI WELDON SCORING*", "━━━━━━━━━━━━━━━━━━━"]
    for k, w in SCORE_WEIGHTS.items():
        lines.append(f"{SCORE_LABELS[k]:<22} +{w}")
    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append("Raw max                100%")
    lines.append(f"Other AI agrees        +{SYNC_BONUS} sync bonus")
    lines.append("")
    lines.append(f"*Alert threshold: {ALERT_THRESHOLD}%+ total*")
    lines.append("_Total = raw + sync bonus, capped at 100%_")
    lines.append("")
    lines.append("_Sync requires: same direction, other AI's data ≤20 min old, partner conf ≥50%_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_rules(update, ctx):
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
        "_Bot guides. I decide. Always._ 💪",
        parse_mode="Markdown",
    )


async def cmd_status(update, ctx):
    now = datetime.now(SGT)
    lines = [
        "🤖 *DUAL-AI BOT STATUS*",
        "━━━━━━━━━━━━━━",
        "✅ Online — Background Worker (no sleep)",
        f"⏱️ Gemini: every {GEMINI_INTERVAL // 60} min",
        f"⏱️ Claude: every {CLAUDE_INTERVAL // 60} min",
        f"🎯 Threshold: {ALERT_THRESHOLD}% (sync bonus +{SYNC_BONUS})",
        "📈 Tracking: XAU/USD",
        "",
        "*Latest analyses:*",
    ]
    for prov in ("gemini", "claude"):
        latest = latest_analyses.get(prov)
        if latest:
            a = latest["a"]
            age = int((now - latest["time_dt"]).total_seconds() / 60)
            lines.append(
                f"• {prov.title()}: {a.get('direction', '—')} @ {a.get('price', '—')} "
                f"({latest['raw_conf']}%, {age}m ago)"
            )
        else:
            lines.append(f"• {prov.title()}: no data yet")
    lines.append("")
    lines.append("*Last alert sent:*")
    if last_alert:
        lines.append(f"{last_alert['signal']} @ {last_alert['price']} ({last_alert['time_str']})")
    else:
        lines.append("_None this session_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Missing TELEGRAM_TOKEN env var")
    if not ANTHROPIC_KEY:
        raise RuntimeError("Missing ANTHROPIC_KEY env var")
    if not GEMINI_KEY:
        raise RuntimeError("Missing GEMINI_KEY env var")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("gold", cmd_gold))
    app.add_handler(CommandHandler("signal", cmd_gold))   # legacy alias → quick (Gemini)
    app.add_handler(CommandHandler("sar", cmd_gold))      # legacy alias
    app.add_handler(CommandHandler("deep", cmd_deep))
    app.add_handler(CommandHandler("both", cmd_both))
    app.add_handler(CommandHandler("score", cmd_score))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("status", cmd_status))

    # Schedulers — Gemini first (30s), Claude offset (90s) so first sync window is clean
    app.job_queue.run_repeating(gemini_check, interval=GEMINI_INTERVAL, first=30)
    app.job_queue.run_repeating(claude_check, interval=CLAUDE_INTERVAL, first=90)

    logger.info(
        f"Aden Gold AI v4.0 (DUAL) — Gemini {GEMINI_INTERVAL}s, "
        f"Claude {CLAUDE_INTERVAL}s, threshold {ALERT_THRESHOLD}%, sync_bonus +{SYNC_BONUS}"
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
