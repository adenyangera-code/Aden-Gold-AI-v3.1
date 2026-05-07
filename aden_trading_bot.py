"""
Aden Gold AI v4.2 — DUAL ENGINE + LIVE FEED + HEARTBEAT

What's new in v4.2 (vs v4.1):
- AI now returns "next_levels": forward-looking buy/sell zones, invalidation, what to watch
- Price feed cross-check: OANDA + gold-api.com fetched in parallel, divergence flagged in alerts
- 2-hourly heartbeat: status update sent regardless of alert threshold
    • 15:00–10:59 SGT (London + NY): always fires
    • 11:00–14:59 SGT (mid-Asian quiet): only if confidence ≥50%
- New /heartbeat manual command to trigger a test pulse anytime

Architecture:
- Gemini 2.5 Flash analyses every 5 min
- Claude Sonnet analyses every 15 min
- Heartbeat (Gemini snapshot) every 2 hours, time-window aware
- Both AIs agree → +15 sync bonus, "DUAL CONFIRMED" alert
- Runs on Render Background Worker (no sleep)

Required env vars:
    TELEGRAM_TOKEN, ANTHROPIC_KEY, GEMINI_KEY
Recommended (for live broker price):
    OANDA_TOKEN, OANDA_ACCOUNT_ID, OANDA_ENV (live|practice)
Optional:
    ALERT_CHAT_ID, ALERT_THRESHOLD, GEMINI_INTERVAL, CLAUDE_INTERVAL,
    SYNC_BONUS, HEARTBEAT_INTERVAL, HEARTBEAT_ASIAN_MIN_CONF,
    GEMINI_MODEL, CLAUDE_MODEL
"""

import asyncio
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

OANDA_TOKEN = os.environ.get("OANDA_TOKEN")
OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID")
OANDA_ENV = os.environ.get("OANDA_ENV", "live").lower()
OANDA_BASE = (
    "https://api-fxtrade.oanda.com" if OANDA_ENV == "live"
    else "https://api-fxpractice.oanda.com"
)

ALERT_CHAT_ID = os.environ.get("ALERT_CHAT_ID", "@Aden_Yang")
ALERT_THRESHOLD = int(os.environ.get("ALERT_THRESHOLD", "70"))

GEMINI_INTERVAL = int(os.environ.get("GEMINI_INTERVAL", "300"))    # 5 min
CLAUDE_INTERVAL = int(os.environ.get("CLAUDE_INTERVAL", "900"))    # 15 min
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "7200"))  # 2 hours
HEARTBEAT_ASIAN_MIN_CONF = int(os.environ.get("HEARTBEAT_ASIAN_MIN_CONF", "50"))
HEARTBEAT_MAX_DATA_AGE_MIN = 15

SYNC_BONUS = int(os.environ.get("SYNC_BONUS", "15"))
SYNC_WINDOW_MINUTES = 20
SYNC_PARTNER_MIN_CONF = 50

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")

PRICE_DIVERGENCE_PCT = 0.3   # cross-check threshold

SGT = timezone(timedelta(hours=8))
COOLDOWN_MINUTES = 15
PRICE_DELTA_TRIGGER_PCT = 0.3

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

latest_analyses: dict = {"claude": None, "gemini": None}
last_alert: dict = {}

# ─── LIVE PRICE (with cross-check) ────────────────────────────────────────────
async def fetch_oanda_price() -> dict | None:
    if not OANDA_TOKEN or not OANDA_ACCOUNT_ID:
        return None
    url = f"{OANDA_BASE}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing"
    headers = {"Authorization": f"Bearer {OANDA_TOKEN}"}
    params = {"instruments": "XAU_USD"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            logger.warning(f"OANDA returned {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
        prices = data.get("prices", [])
        if not prices:
            return None
        p = prices[0]
        bid = float(p["bids"][0]["price"])
        ask = float(p["asks"][0]["price"])
        return {
            "bid": bid,
            "ask": ask,
            "mid": round((bid + ask) / 2, 2),
            "time": p.get("time", ""),
            "source": f"OANDA {OANDA_ENV.title()}",
        }
    except Exception as e:
        logger.warning(f"OANDA fetch failed: {e}")
        return None


async def fetch_goldapi_price() -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://api.gold-api.com/price/XAU")
        if resp.status_code != 200:
            return None
        data = resp.json()
        price = float(data.get("price", 0))
        if price <= 0:
            return None
        return {
            "bid": price,
            "ask": price,
            "mid": round(price, 2),
            "time": data.get("updatedAt", ""),
            "source": "gold-api.com",
        }
    except Exception as e:
        logger.warning(f"gold-api.com fetch failed: {e}")
        return None


async def get_live_price() -> dict:
    """
    Fetch OANDA and gold-api.com IN PARALLEL, cross-check, return primary
    with verification metadata. OANDA is preferred when available.
    """
    oanda_p, goldapi_p = await asyncio.gather(
        fetch_oanda_price(), fetch_goldapi_price(), return_exceptions=False
    )

    if oanda_p and goldapi_p:
        diff_pct = abs(oanda_p["mid"] - goldapi_p["mid"]) / oanda_p["mid"] * 100
        oanda_p["cross_check"] = "verified" if diff_pct < PRICE_DIVERGENCE_PCT else "diverged"
        oanda_p["cross_check_diff_pct"] = round(diff_pct, 3)
        oanda_p["cross_check_other"] = goldapi_p["mid"]
        oanda_p["cross_check_other_source"] = "gold-api.com"
        if oanda_p["cross_check"] == "diverged":
            logger.warning(
                f"PRICE DIVERGENCE: OANDA={oanda_p['mid']} vs gold-api={goldapi_p['mid']} "
                f"({diff_pct:.2f}% apart)"
            )
        return oanda_p

    if oanda_p:
        oanda_p["cross_check"] = "oanda_only"
        return oanda_p

    if goldapi_p:
        goldapi_p["cross_check"] = "goldapi_fallback"
        logger.warning("OANDA unavailable, using gold-api.com fallback")
        return goldapi_p

    logger.error("ALL price sources failed")
    return {
        "bid": 0, "ask": 0, "mid": 0, "time": "",
        "source": "UNKNOWN", "cross_check": "all_failed",
    }


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
    other = "claude" if provider == "gemini" else "gemini"
    other_result = latest_analyses.get(other)
    if not other_result:
        return None
    age_min = (now - other_result["time_dt"]).total_seconds() / 60
    if age_min > SYNC_WINDOW_MINUTES:
        return None
    if other_result["a"].get("direction") != direction:
        return None
    if other_result["raw_conf"] < SYNC_PARTNER_MIN_CONF:
        return None
    return other_result


def compute_total_confidence(provider, scores, direction, now):
    raw = compute_raw_confidence(scores)
    partner = get_sync_partner(provider, direction, now)
    bonus = SYNC_BONUS if partner else 0
    return min(raw + bonus, 100), raw, bool(partner), partner


# ─── PROMPT BUILDER (with next_levels schema) ─────────────────────────────────
def build_prompt(price_data: dict) -> str:
    bid, ask, mid = price_data.get("bid", 0), price_data.get("ask", 0), price_data.get("mid", 0)
    source = price_data.get("source", "unknown")

    if mid > 0:
        anchor = (
            f"\n*** LIVE PRICE ANCHOR (use this as ground truth) ***\n"
            f"Source: {source}\n"
            f"Bid: ${bid:.2f}  Ask: ${ask:.2f}  Mid: ${mid:.2f}\n"
            f"Use this exact mid price ({mid:.2f}) for your 'price' field, "
            f"and calculate entry/SL/TP relative to it.\n"
            f"DO NOT search the web for the current price — it's already given above.\n"
        )
    else:
        anchor = "\n(No live price available — please web-search for current XAU/USD price.)\n"

    return f"""You are Aden Yang's personal trading AI for XAU/USD (gold).
{anchor}
Apply Larry Weldon-style intermarket + technical analysis. For each of the 7
factors below, mark TRUE only if you have concrete evidence; otherwise FALSE.

THE 7 SCORING FACTORS:
1. DXY aligned (+20)         — US Dollar Index direction supports the trade
2. Multi-TF aligned (+20)    — M15, H1, H4 all point the same direction
3. Fibonacci level (+15)     — entry near 38.2 / 50 / 61.8 / 78.6 retracement
4. Candlestick pattern (+15) — pin bar, engulfing, hammer, doji, shooting star
5. Oil aligned (+10)         — WTI rising = bullish gold (inflation hedge)
6. Volume confirms (+10)     — volume rising into the trend direction
7. News catalyst (+10)       — Fed/CPI/NFP/geopolitical event supports direction

Search the web NOW for (DO NOT search for the price — it's anchored above):
- Current DXY level and short-term trend
- Current WTI crude oil price and trend
- Last 2 hours of market-moving news (Fed, CPI, NFP, Iran, Israel, central bank gold)
- RSI / MACD readings on M15, H1, H4
- Today's session: Asian / London / New York / Overlap

Respond with ONLY a valid JSON object — no markdown fences, no preamble.

Schema:
{{
  "asset": "XAUUSD",
  "price": "{mid:.2f}",
  "session": "Asian|London|New York|Overlap",
  "direction": "BUY|SELL|WAIT",
  "scores": {{
    "dxy_aligned": true|false,
    "multi_tf_aligned": true|false,
    "fibonacci_level": true|false,
    "candlestick_pattern": true|false,
    "oil_aligned": true|false,
    "volume_confirms": true|false,
    "news_catalyst": true|false
  }},
  "score_reasons": {{
    "dxy_aligned": "<short reason or 'no data'>",
    "multi_tf_aligned": "<short reason or 'no data'>",
    "fibonacci_level": "<short reason or 'no data'>",
    "candlestick_pattern": "<short reason or 'no data'>",
    "oil_aligned": "<short reason or 'no data'>",
    "volume_confirms": "<short reason or 'no data'>",
    "news_catalyst": "<short reason or 'no data'>"
  }},
  "entry": "<price for current setup, or 'wait' if no setup>",
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
  "next_levels": {{
    "buy_zone": "<price + condition, e.g. 'pullback to $4730 with bullish engulfing on M15'>",
    "sell_zone": "<price + condition, e.g. 'rejection at $4760 with bearish pin bar'>",
    "invalidation": "<level that breaks current bias, e.g. 'close below $4715 invalidates bullish setup'>",
    "watch_for": "<what to monitor in next 1-4 hours, e.g. 'NFP at 8:30 ET Friday, DXY breakout from 98 range'>"
  }},
  "summary": "<2-sentence synthesis>",
  "risk_warning": "<one-line warning if major risk, else empty string>"
}}"""


# ─── ROBUST JSON PARSER ───────────────────────────────────────────────────────
def extract_json(text: str) -> dict:
    cleaned = (text or "").strip()
    cleaned = cleaned.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON found. First 300 chars: {text[:300]}")
    return json.loads(cleaned[start : end + 1])


# ─── ANALYZE: CLAUDE & GEMINI ─────────────────────────────────────────────────
async def analyse_with_claude(price_data: dict) -> dict:
    prompt = build_prompt(price_data)
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
                "max_tokens": 1800,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}],
            },
        )
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Claude API error: {data['error']}")
    text_parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return extract_json("".join(text_parts))


async def analyse_with_gemini(price_data: dict) -> dict:
    prompt = build_prompt(price_data)
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    )
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
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
        raise ValueError(f"No Gemini candidates: {str(data)[:300]}")
    parts = candidates[0].get("content", {}).get("parts", [])
    return extract_json("".join(p.get("text", "") for p in parts))


async def run_analysis(provider: str) -> tuple[dict, dict]:
    price_data = await get_live_price()
    if provider == "claude":
        a = await analyse_with_claude(price_data)
    else:
        a = await analyse_with_gemini(price_data)
    if price_data["mid"] > 0:
        a["price"] = f"{price_data['mid']:.2f}"
    a["_price_source"] = price_data["source"]
    a["_price_check"] = price_data.get("cross_check", "unknown")
    a["_price_diff_pct"] = price_data.get("cross_check_diff_pct")
    a["_price_other"] = price_data.get("cross_check_other")
    return a, price_data


# ─── FORMATTING ───────────────────────────────────────────────────────────────
def emoji_for(d: str) -> str:
    return {"BUY": "🟢", "SELL": "🔴", "WAIT": "🟡"}.get(d, "⚪")


def confidence_bar(pct: int) -> str:
    return "█" * (pct // 10) + "░" * (10 - pct // 10)


def price_check_badge(check: str, diff_pct, other) -> str:
    if check == "verified":
        return f"✅ verified (vs gold-api: {diff_pct}%)"
    if check == "diverged":
        return f"⚠️ DIVERGED (gold-api says ${other}, {diff_pct}% gap — verify manually!)"
    if check == "oanda_only":
        return "✅ OANDA only (gold-api unavailable)"
    if check == "goldapi_fallback":
        return "⚠️ fallback only (OANDA unavailable)"
    return "❌ no verification"


def format_alert(provider: str, a: dict, raw_conf: int, total_conf: int, synced: bool, partner) -> str:
    e = emoji_for(a.get("direction", "WAIT"))
    now = datetime.now(SGT).strftime("%d %b %H:%M SGT")
    direction = a.get("direction", "WAIT")
    risk = a.get("risk_warning", "") or ""
    price_source = a.get("_price_source", "unknown")
    badge = price_check_badge(
        a.get("_price_check", ""), a.get("_price_diff_pct"), a.get("_price_other")
    )

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

    # Forward-looking next levels block (NEW)
    nl = a.get("next_levels", {}) or {}
    next_block = ""
    if nl:
        next_block = (
            f"\n🎯 *Next Levels to Watch:*\n"
            f"• 🟢 Buy zone: _{nl.get('buy_zone', '—')}_\n"
            f"• 🔴 Sell zone: _{nl.get('sell_zone', '—')}_\n"
            f"• ❌ Invalidation: _{nl.get('invalidation', '—')}_\n"
            f"• 👀 Watch: _{nl.get('watch_for', '—')}_\n"
        )

    header = f"⚖️ *ADEN GOLD AI — {provider.upper()}*"
    price_line = (
        f"💰 Price: *${a.get('price', '—')}* `[{price_source}]`\n"
        f"   {badge}"
    )

    if direction == "WAIT":
        return (
            f"{header}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"{e} *XAU/USD: WAIT*\n"
            f"{price_line}\n"
            f"🎯 Confidence: {total_conf}% (raw {raw_conf}%)\n"
            f"`{confidence_bar(total_conf)}`"
            f"{sync_block}\n"
            f"📊 *Scoring:*\n{score_block}\n\n"
            f"📍 S: {a.get('key_support', '—')} | R: {a.get('key_resistance', '—')}"
            f"{next_block}\n"
            f"💡 _{a.get('summary', '')}_\n\n"
            f"⏰ {now}"
        )

    body = (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{e} *XAU/USD: {direction}*\n"
        f"{price_line}\n"
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
        f"RSI(H1): {a.get('rsi_h1', '—')} | MACD: {a.get('macd_h1', '—')}"
        f"{next_block}\n"
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


async def safe_send(bot, chat_id, text):
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except Exception as md_err:
        logger.warning(f"Markdown failed, sending plain: {md_err}")
        await bot.send_message(chat_id=chat_id, text=text)


# ─── HEARTBEAT (NEW) ──────────────────────────────────────────────────────────
def is_active_heartbeat_window(hour_sgt: int) -> bool:
    """15:00 to 10:59 next day = always-fire. 11:00 to 14:59 = conditional."""
    return hour_sgt >= 15 or hour_sgt < 11


async def send_heartbeat(ctx, manual: bool = False):
    """Sends a status pulse from latest Gemini analysis."""
    now = datetime.now(SGT)
    latest = latest_analyses.get("gemini")

    if not latest:
        msg = "📊 *2H HEARTBEAT*\n\n_No Gemini data yet — bot is warming up._"
        if manual:
            await safe_send(ctx.bot, ALERT_CHAT_ID, msg)
        else:
            logger.info("[HEARTBEAT] No Gemini data, skipping auto-fire")
        return

    age_min = (now - latest["time_dt"]).total_seconds() / 60
    if age_min > HEARTBEAT_MAX_DATA_AGE_MIN and not manual:
        logger.warning(f"[HEARTBEAT] Latest Gemini is {age_min:.0f}m old, skipping")
        return

    a = latest["a"]
    raw_conf = latest["raw_conf"]
    direction = a.get("direction", "WAIT")
    active = is_active_heartbeat_window(now.hour)

    # Asian quiet window: only fire if confidence ≥ threshold
    if not active and not manual and raw_conf < HEARTBEAT_ASIAN_MIN_CONF:
        logger.info(
            f"[HEARTBEAT] Asian window {now.hour}h, conf {raw_conf}% < "
            f"{HEARTBEAT_ASIAN_MIN_CONF}%, skipping"
        )
        return

    total_conf, _, synced, partner = compute_total_confidence(
        "gemini", a.get("scores", {}), direction, latest["time_dt"]
    )
    window_label = "Active session" if active else "Asian session"
    tag = "MANUAL" if manual else "AUTO"
    prefix = f"📊 *2H HEARTBEAT — {window_label} ({tag})*"
    text = prefix + "\n\n" + format_alert("gemini", a, raw_conf, total_conf, synced, partner)
    await safe_send(ctx.bot, ALERT_CHAT_ID, text)
    logger.info(f"[HEARTBEAT SENT] {direction} {total_conf}% in {window_label} ({tag})")


async def heartbeat_job(ctx):
    await send_heartbeat(ctx, manual=False)


# ─── SHARED CHECK + ALERT LOGIC ───────────────────────────────────────────────
async def run_check(ctx: ContextTypes.DEFAULT_TYPE, provider: str):
    global last_alert
    try:
        logger.info(f"[{provider.upper()}] Checking XAU/USD")
        a, price_data = await run_analysis(provider)

        scores = a.get("scores", {})
        direction = a.get("direction", "WAIT")
        price = a.get("price", "0")
        now = datetime.now(SGT)

        raw_conf = compute_raw_confidence(scores)
        latest_analyses[provider] = {"a": a, "time_dt": now, "raw_conf": raw_conf}

        total_conf, _, synced, partner = compute_total_confidence(provider, scores, direction, now)
        logger.info(
            f"[{provider.upper()}] {direction} raw={raw_conf}% total={total_conf}% "
            f"sync={synced} price={price} src={price_data['source']} "
            f"check={price_data.get('cross_check')}"
        )

        if direction not in ("BUY", "SELL") or total_conf < ALERT_THRESHOLD:
            return

        if last_alert and last_alert.get("signal") == direction:
            age_min = (now - last_alert["time_dt"]).total_seconds() / 60
            try:
                pct_move = abs(float(price) - float(last_alert["price"])) / float(last_alert["price"]) * 100
            except (ValueError, TypeError, ZeroDivisionError):
                pct_move = 0
            if age_min < COOLDOWN_MINUTES and pct_move < PRICE_DELTA_TRIGGER_PCT:
                logger.info(f"[COOLDOWN] same {direction} from {provider}, sync={synced}")
                return

        prefix = "🚨🔥 *DUAL-AI ALERT!*" if synced else "🚨 *AUTO ALERT*"
        text = f"{prefix}\n\n" + format_alert(provider, a, raw_conf, total_conf, synced, partner)
        await safe_send(ctx.bot, ALERT_CHAT_ID, text)

        last_alert = {
            "signal": direction, "price": price,
            "time_dt": now, "time_str": now.strftime("%d %b %H:%M SGT"),
        }
        logger.info(f"[ALERT SENT] {provider} {direction} {total_conf}% sync={synced}")

    except Exception:
        logger.exception(f"[{provider.upper()}] Check failed")


async def gemini_check(ctx):
    await run_check(ctx, "gemini")


async def claude_check(ctx):
    await run_check(ctx, "claude")


# ─── COMMANDS ─────────────────────────────────────────────────────────────────
async def cmd_start(update, ctx):
    price_source = "OANDA Live" if (OANDA_TOKEN and OANDA_ACCOUNT_ID) else "gold-api.com fallback"
    await update.message.reply_text(
        "⚖️ *ADEN GOLD AI v4.2 — DUAL ENGINE + HEARTBEAT*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"💹 Price: *{price_source}* (cross-checked)\n"
        f"🤖 Gemini — every {GEMINI_INTERVAL // 60} min\n"
        f"🧠 Claude — every {CLAUDE_INTERVAL // 60} min\n"
        f"📊 Heartbeat — every {HEARTBEAT_INTERVAL // 3600}h "
        f"(Asian: ≥{HEARTBEAT_ASIAN_MIN_CONF}% only)\n"
        f"🔥 Both AIs agree = +{SYNC_BONUS} sync bonus\n"
        f"🔔 Alert if total ≥ {ALERT_THRESHOLD}%\n\n"
        "*Commands:*\n"
        "/gold — Quick analysis (Gemini)\n"
        "/deep — Deep analysis (Claude)\n"
        "/both — Run both side-by-side\n"
        "/price — Just live price check\n"
        "/heartbeat — Test 2h pulse now\n"
        "/score — Explain scoring\n"
        "/rules — Trading rules\n"
        "/status — Bot status\n",
        parse_mode="Markdown",
    )


async def cmd_price(update, ctx):
    msg = await update.message.reply_text("⏳ Fetching live + cross-check...")
    p = await get_live_price()
    if p["mid"] <= 0:
        await msg.edit_text("❌ All price sources failed.")
        return
    spread = p["ask"] - p["bid"] if p["ask"] != p["bid"] else 0
    badge = price_check_badge(
        p.get("cross_check", ""), p.get("cross_check_diff_pct"), p.get("cross_check_other")
    )
    now = datetime.now(SGT).strftime("%d %b %H:%M SGT")
    await msg.edit_text(
        f"💹 *XAU/USD Live Price*\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"Source: `{p['source']}`\n"
        f"Bid: *${p['bid']:.2f}*\n"
        f"Ask: *${p['ask']:.2f}*\n"
        f"Mid: *${p['mid']:.2f}*\n"
        f"Spread: ${spread:.2f}\n"
        f"Verification: {badge}\n\n"
        f"⏰ {now}",
        parse_mode="Markdown",
    )


async def _manual(update, provider, label):
    msg = await update.message.reply_text(f"⏳ {label} analysing XAU/USD with live price...")
    try:
        a, _ = await run_analysis(provider)
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
            await msg.edit_text(text)
    except Exception as e:
        logger.exception(f"{provider} manual failed")
        await msg.edit_text(f"❌ {label} error: `{str(e)[:200]}`", parse_mode="Markdown")


async def cmd_gold(update, ctx):
    await _manual(update, "gemini", "Gemini")


async def cmd_deep(update, ctx):
    await _manual(update, "claude", "Claude")


async def cmd_both(update, ctx):
    await update.message.reply_text("⏳ Running both AIs side-by-side... ~45s")
    try:
        a_g, _ = await run_analysis("gemini")
        now_g = datetime.now(SGT)
        raw_g = compute_raw_confidence(a_g.get("scores", {}))
        latest_analyses["gemini"] = {"a": a_g, "time_dt": now_g, "raw_conf": raw_g}
    except Exception as e:
        await update.message.reply_text(f"❌ Gemini error: `{str(e)[:200]}`", parse_mode="Markdown")
        return
    try:
        a_c, _ = await run_analysis("claude")
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


async def cmd_heartbeat(update, ctx):
    await update.message.reply_text("⏳ Generating manual heartbeat from latest Gemini snapshot...")
    await send_heartbeat(ctx, manual=True)


async def cmd_score(update, ctx):
    lines = ["📊 *DUAL-AI WELDON SCORING*", "━━━━━━━━━━━━━━━━━━━"]
    for k, w in SCORE_WEIGHTS.items():
        lines.append(f"{SCORE_LABELS[k]:<22} +{w}")
    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append("Raw max                100%")
    lines.append(f"Other AI agrees        +{SYNC_BONUS} sync bonus")
    lines.append("")
    lines.append(f"*Alert threshold: {ALERT_THRESHOLD}%+ total*")
    lines.append(f"*Heartbeat: every {HEARTBEAT_INTERVAL // 3600}h*")
    lines.append(f"  • Active 15:00–10:59 SGT: always fires")
    lines.append(f"  • Asian 11:00–14:59 SGT: only ≥{HEARTBEAT_ASIAN_MIN_CONF}%")
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
    price_source = "OANDA Live" if (OANDA_TOKEN and OANDA_ACCOUNT_ID) else "gold-api.com fallback"
    in_active = is_active_heartbeat_window(now.hour)
    window = "Active (15:00–10:59 SGT)" if in_active else "Asian (11:00–14:59 SGT)"
    lines = [
        "🤖 *DUAL-AI BOT STATUS v4.2*",
        "━━━━━━━━━━━━━━",
        "✅ Online — Background Worker (no sleep)",
        f"💹 Price source: {price_source} (cross-checked vs gold-api)",
        f"⏱️ Gemini: every {GEMINI_INTERVAL // 60} min",
        f"⏱️ Claude: every {CLAUDE_INTERVAL // 60} min",
        f"📊 Heartbeat: every {HEARTBEAT_INTERVAL // 3600}h",
        f"🌏 Current window: {window}",
        f"🎯 Alert threshold: {ALERT_THRESHOLD}% (sync +{SYNC_BONUS})",
        "",
        "*Latest analyses:*",
    ]
    for prov in ("gemini", "claude"):
        latest = latest_analyses.get(prov)
        if latest:
            a = latest["a"]
            age = int((now - latest["time_dt"]).total_seconds() / 60)
            lines.append(
                f"• {prov.title()}: {a.get('direction', '—')} @ ${a.get('price', '—')} "
                f"({latest['raw_conf']}%, {age}m ago)"
            )
        else:
            lines.append(f"• {prov.title()}: no data yet")
    lines.append("")
    lines.append("*Last alert:*")
    if last_alert:
        lines.append(f"{last_alert['signal']} @ ${last_alert['price']} ({last_alert['time_str']})")
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

    if OANDA_TOKEN and OANDA_ACCOUNT_ID:
        logger.info(f"OANDA configured: {OANDA_ENV} account {OANDA_ACCOUNT_ID[:7]}...")
    else:
        logger.warning("OANDA not configured — falling back to gold-api.com")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("gold", cmd_gold))
    app.add_handler(CommandHandler("signal", cmd_gold))
    app.add_handler(CommandHandler("sar", cmd_gold))
    app.add_handler(CommandHandler("deep", cmd_deep))
    app.add_handler(CommandHandler("both", cmd_both))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("heartbeat", cmd_heartbeat))
    app.add_handler(CommandHandler("score", cmd_score))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("status", cmd_status))

    app.job_queue.run_repeating(gemini_check, interval=GEMINI_INTERVAL, first=30)
    app.job_queue.run_repeating(claude_check, interval=CLAUDE_INTERVAL, first=90)
    # Heartbeat: first one at 6 min in (after first Gemini result lands), then every 2h
    app.job_queue.run_repeating(heartbeat_job, interval=HEARTBEAT_INTERVAL, first=360)

    logger.info(
        f"Aden Gold AI v4.2 — Gemini {GEMINI_INTERVAL}s, Claude {CLAUDE_INTERVAL}s, "
        f"Heartbeat {HEARTBEAT_INTERVAL}s, threshold {ALERT_THRESHOLD}%, sync +{SYNC_BONUS}"
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
