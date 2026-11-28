import os
import json
import html
import logging
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import yfinance as yf
from flask import Flask, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}" if TELEGRAM_TOKEN else ""

CHAT_FILE = "chats.json"
POSITIONS_FILE = "positions.json"
SHARIA_CACHE_FILE = "sharia_cache.json"
WATCHLIST_FILE = "watchlist.json"
WATCH_STATE_FILE = "watch_alert_state.json"
WATCH_SCHEDULE_FILE = "watch_schedule.json"

MIZAN_URL = "https://askmizan.com/api/v1/screen/{}"
MAX_ALERTS = 10
SHARIA_CACHE_HOURS = 24
WATCH_SCAN_HOUR = 16
WATCH_SCAN_MINUTE = 15

scan_lock = threading.Lock()
scheduler_started = False
chat_ids = []
positions = {}
sharia_cache = {}
watchlists = {}
latest_results = {}
watch_alert_state = {}
watch_schedule = {}


# =========================
# JSON storage
# =========================

def load_json(filename, default):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(filename, data):
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as error:
        logger.error("Save error %s: %s", filename, error)


chat_ids = load_json(CHAT_FILE, [])
positions = load_json(POSITIONS_FILE, {})
sharia_cache = load_json(SHARIA_CACHE_FILE, {})
watchlists = load_json(WATCHLIST_FILE, {})
watch_alert_state = load_json(WATCH_STATE_FILE, {})
watch_schedule = load_json(WATCH_SCHEDULE_FILE, {})


# =========================
# Telegram
# =========================

def telegram_request(method, payload):
    if not TELEGRAM_TOKEN:
        logger.error("Missing TELEGRAM_BOT_TOKEN")
        return None
    try:
        response = requests.post(
            f"{TELEGRAM_API}/{method}", json=payload, timeout=25
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            logger.error("Telegram API error: %s", data)
        return data
    except Exception as error:
        logger.error("Telegram request error: %s", error)
        return None


def send_message(chat_id, message, reply_markup=None, parse_mode=None):
    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return telegram_request("sendMessage", payload)


def answer_callback(callback_id, text=""):
    telegram_request("answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text": text,
        "show_alert": False
    })


def register_chat(chat_id):
    chat_id = str(chat_id)
    if chat_id not in chat_ids:
        chat_ids.append(chat_id)
        save_json(CHAT_FILE, chat_ids)


def broadcast(message):
    for chat_id in list(chat_ids):
        send_message(chat_id, message)


# =========================
# Watchlist
# =========================

def get_watchlist(chat_id):
    return watchlists.get(str(chat_id), [])


def add_to_watchlist(chat_id, symbol):
    chat_id = str(chat_id)
    watchlists.setdefault(chat_id, [])
    if symbol not in watchlists[chat_id]:
        watchlists[chat_id].append(symbol)
        save_json(WATCHLIST_FILE, watchlists)
        return True
    return False


# =========================
# AAOIFI / Mizan
# =========================

def check_sharia(symbol):
    now = datetime.now(timezone.utc).timestamp()
    cached = sharia_cache.get(symbol)

    if cached:
        try:
            age = (now - float(cached["timestamp"])) / 3600
            if age < SHARIA_CACHE_HOURS and cached["status"] in ("HALAL", "HARAM"):
                return cached["status"]
        except Exception:
            pass

    try:
        response = requests.get(
            MIZAN_URL.format(symbol),
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20
        )
        response.raise_for_status()
        data = response.json()
        status = None

        screens = data.get("screens", [])
        if isinstance(screens, list):
            for screen in screens:
                if isinstance(screen, dict) and str(
                    screen.get("standard", "")
                ).upper() == "AAOIFI":
                    compliant = screen.get("compliant")
                    if compliant is True:
                        status = "HALAL"
                    elif compliant is False:
                        status = "HARAM"
                    break

        if status is None:
            by_standard = data.get("by_standard", {})
            if isinstance(by_standard, dict):
                result = str(by_standard.get("AAOIFI", "")).lower()
                if result in ("pass", "compliant"):
                    status = "HALAL"
                elif result in ("fail", "non-compliant"):
                    status = "HARAM"

        if status not in ("HALAL", "HARAM"):
            return "UNKNOWN"

        sharia_cache[symbol] = {"status": status, "timestamp": now}
        save_json(SHARIA_CACHE_FILE, sharia_cache)
        return status

    except Exception as error:
        logger.warning("Sharia check failed for %s: %s", symbol, error)
        return "UNKNOWN"


def sharia_label(status):
    return {
        "HALAL": "🟢 شرعي",
        "HARAM": "🔴 غير شرعي",
        "UNKNOWN": "🟡 غير محسوم"
    }.get(status, "🟡 غير محسوم")


# =========================
# Index constituents
# =========================

def get_nasdaq_symbols():
    url = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/"
    }
    try:
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", {})
        rows = data.get("rows", [])
        if not rows and isinstance(data.get("data"), dict):
            rows = data["data"].get("rows", [])
        symbols = sorted({
            row["symbol"].strip().replace(".", "-")
            for row in rows if row.get("symbol")
        })
        if len(symbols) >= 90:
            return symbols
        logger.warning("Nasdaq API returned only %s symbols; using fallback", len(symbols))
    except Exception as error:
        logger.error("Nasdaq API error: %s", error)

    # Fallback only. The live Nasdaq API is preferred.
    return [
        "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG",
        "AVGO", "TSLA", "COST", "NFLX", "AMD", "PEP", "ADBE",
        "CSCO", "TMUS", "LIN", "QCOM", "INTU", "TXN", "AMGN",
        "ISRG", "BKNG", "HON", "AMAT", "VRTX", "PANW", "ADP",
        "GILD", "SBUX"
    ]


def get_dow_symbols():
    # Fetch the current Dow Jones Industrial Average constituents.
    url = "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        tables = pd.read_html(url, storage_options=headers)
        for table in tables:
            cols = [str(c).strip().lower() for c in table.columns]
            symbol_col = None
            for col in table.columns:
                if str(col).strip().lower() in ("symbol", "ticker"):
                    symbol_col = col
                    break
            if symbol_col is not None:
                symbols = []
                for value in table[symbol_col].dropna().astype(str):
                    symbol = value.strip().replace(".", "-")
                    if symbol and symbol.lower() not in ("symbol", "ticker"):
                        symbols.append(symbol)
                symbols = sorted(set(symbols))
                if 25 <= len(symbols) <= 35:
                    return symbols
    except Exception as error:
        logger.error("Dow Jones constituents fetch failed: %s", error)

    # Fallback list in case Wikipedia is temporarily unavailable.
    return [
        "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO",
        "CVX", "DIS", "GS", "HD", "HON", "IBM", "JNJ", "JPM",
        "KO", "MCD", "MMM", "MRK", "MSFT", "NKE", "NVDA", "PG",
        "SHW", "TRV", "UNH", "V", "VZ", "WMT"
    ]


def get_market_universe():
    """Return {ticker: [index names]}; overlapping constituents appear once."""
    universe = {}
    for symbol in get_nasdaq_symbols():
        universe.setdefault(symbol, []).append("NASDAQ-100")
    for symbol in get_dow_symbols():
        universe.setdefault(symbol, []).append("DOW JONES")
    return universe


def market_label(item):
    markets = item.get("markets", [])
    return " + ".join(markets) if markets else "WATCHLIST"


# =========================
# Technical indicators
# =========================

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(
        alpha=1 / period, min_periods=period, adjust=False
    ).mean()
    avg_loss = loss.ewm(
        alpha=1 / period, min_periods=period, adjust=False
    ).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return (100 - (100 / (1 + rs))).fillna(100)


def normalize_data(df):
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    required = ["Close", "High", "Volume"]
    if not all(col in df.columns for col in required):
        return None
    return df.dropna(subset=["Close", "High"])


def get_completed_data(df):
    try:
        now = datetime.now(ZoneInfo("America/New_York"))
        if now.weekday() < 5:
            market_open = (
                now.hour > 9 or (now.hour == 9 and now.minute >= 30)
            )
            if market_open and now.hour < 16:
                last_date = pd.Timestamp(df.index[-1]).date()
                if last_date == now.date() and len(df) > 60:
                    return df.iloc[:-1]
    except Exception as error:
        logger.warning("Candle check: %s", error)
    return df


# =========================
# RSI entry classification - V6
# =========================

def classify_rsi(value):
    if value < 50:
        return {
            "status": "WEAK",
            "label": "🟡 زخم ضعيف - انتظار تحسن RSI",
            "entry_allowed": False
        }
    if value <= 68:
        return {
            "status": "READY",
            "label": "🟢 منطقة دخول مناسبة",
            "entry_allowed": True
        }
    if value < 70:
        return {
            "status": "CAUTION",
            "label": "🟡 دخول بحذر - زخم مرتفع",
            "entry_allowed": True
        }
    if value <= 75:
        return {
            "status": "WAIT",
            "label": "🟠 انتظار التراجع قبل الدخول",
            "entry_allowed": False
        }
    return {
        "status": "BLOCK",
        "label": "🔴 تشبع شرائي مرتفع - لا دخول جديد",
        "entry_allowed": False
    }


# =========================
# Stock analysis - same V6 rules
# =========================

def analyze_stock(symbol):
    try:
        df = yf.download(
            symbol, period="6mo", interval="1d",
            auto_adjust=True, progress=False, threads=False
        )
        df = normalize_data(df)
        if df is None or len(df) < 60:
            return None

        df = get_completed_data(df)
        if len(df) < 60:
            return None

        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        volume = df["Volume"].astype(float)

        ema20 = close.ewm(span=20, adjust=False).mean()
        ema50 = close.ewm(span=50, adjust=False).mean()
        rsi = calculate_rsi(close)

        macd = (
            close.ewm(span=12, adjust=False).mean()
            - close.ewm(span=26, adjust=False).mean()
        )
        signal_line = macd.ewm(span=9, adjust=False).mean()

        price = float(close.iloc[-1])
        e20 = float(ema20.iloc[-1])
        e50 = float(ema50.iloc[-1])
        current_rsi = float(rsi.iloc[-1])
        current_macd = float(macd.iloc[-1])
        current_signal = float(signal_line.iloc[-1])

        average_volume = float(volume.iloc[-21:-1].mean())
        current_volume = float(volume.iloc[-1])
        volume_ratio = current_volume / average_volume if average_volume > 0 else 0
        resistance = float(high.iloc[-21:-1].max())

        score = 0
        details = {}

        if price > e20 and e20 > e50:
            score += 25
            details["trend"] = 25
        elif price > e20 and price > e50:
            score += 15
            details["trend"] = 15
        else:
            details["trend"] = 0

        if current_macd > current_signal:
            score += 20
            details["macd"] = 20
        else:
            details["macd"] = 0

        if 50 <= current_rsi <= 68:
            score += 15
            details["rsi"] = 15
        elif 45 <= current_rsi < 50 or 68 < current_rsi <= 72:
            score += 8
            details["rsi"] = 8
        else:
            details["rsi"] = 0

        if volume_ratio >= 1:
            score += 20
            details["volume"] = 20
        elif volume_ratio >= 0.80:
            score += 10
            details["volume"] = 10
        else:
            details["volume"] = 0

        if price > resistance:
            score += 20
            details["breakout"] = 20
        elif price >= resistance * 0.98:
            score += 10
            details["breakout"] = 10
        else:
            details["breakout"] = 0

        if score >= 80:
            signal = "STRONG BUY"
        elif score >= 65:
            signal = "MEDIUM"
        else:
            signal = "NO SIGNAL"

        rsi_info = classify_rsi(current_rsi)
        return {
            "symbol": symbol,
            "price": price,
            "score": score,
            "signal": signal,
            "rsi": current_rsi,
            "rsi_status": rsi_info["status"],
            "rsi_label": rsi_info["label"],
            "entry_allowed": rsi_info["entry_allowed"],
            "ema20": e20,
            "ema50": e50,
            "macd": current_macd,
            "macd_signal": current_signal,
            "volume_ratio": volume_ratio,
            "resistance": resistance,
            "details": details
        }
    except Exception as error:
        logger.error("Analysis error %s: %s", symbol, error)
        return None


# =========================
# Telegram keyboards and recommendation cards
# =========================

def recommendation_keyboard(symbol):
    return {
        "inline_keyboard": [[
            {"text": "📊 تفاصيل السهم", "callback_data": f"details:{symbol}"},
            {
                "text": "📈 الرسم البياني",
                "url": f"https://www.tradingview.com/symbols/{symbol}/"
            }
        ], [
            {"text": "⭐ إضافة للمراقبة", "callback_data": f"watch:{symbol}"}
        ]]
    }


def watch_alert_keyboard(symbol):
    return {
        "inline_keyboard": [[
            {"text": "📊 تفاصيل السهم", "callback_data": f"details:{symbol}"},
            {
                "text": "📈 الرسم البياني",
                "url": f"https://www.tradingview.com/symbols/{symbol}/"
            }
        ]]
    }


def send_recommendation_card(chat_id, item):
    symbol = item["symbol"]
    price = item["price"]
    rsi_status = item["rsi_status"]

    if item["signal"] == "STRONG BUY":
        title = {
            "READY": "🟢 STRONG BUY - دخول مسموح",
            "CAUTION": "🟡 STRONG BUY - دخول بحذر",
            "WAIT": "🟠 STRONG BUY - انتظار التراجع",
            "BLOCK": "🔴 STRONG BUY - لا دخول حالياً"
        }.get(rsi_status, "🟡 STRONG BUY - زخم ضعيف")
    else:
        title = "🟡 MEDIUM - سهم للمراقبة"

    decision = (
        "مسموح فنياً وفق فلتر RSI"
        if item["entry_allowed"] else "انتظار - لا تسجل صفقة جديدة"
    )

    message = (
        f"<b>{html.escape(title)}</b>\n"
        f"<b>{html.escape(symbol)}</b> | {html.escape(market_label(item))}\n"
        "━━━━━━━━━━━━━━\n"
        f"💵 <b>السعر:</b> ${price:.2f}\n"
        f"🎯 <b>الهدف النظري:</b> ${price * 1.10:.2f}\n"
        f"🛑 <b>وقف الخسارة النظري:</b> ${price * 0.95:.2f}\n"
        "━━━━━━━━━━━━━━\n"
        f"📊 <b>قوة الإشارة:</b> {item['score']}/100\n"
        f"📈 <b>RSI:</b> {item['rsi']:.1f}\n"
        f"📦 <b>Volume:</b> {item['volume_ratio']:.2f}x\n"
        f"🕌 <b>AAOIFI:</b> {sharia_label(item['sharia'])}\n"
        "━━━━━━━━━━━━━━\n"
        f"📌 <b>قرار RSI:</b> {html.escape(item['rsi_label'])}\n"
        f"🔎 <b>حالة الدخول:</b> {decision}\n\n"
        "<i>الهدف والوقف مستويات نظرية وليسا ضماناً للنتيجة.</i>"
    )
    send_message(
        chat_id, message,
        reply_markup=recommendation_keyboard(symbol),
        parse_mode="HTML"
    )


# =========================
# Stock details and callbacks
# =========================

def send_stock_details(chat_id, symbol):
    result = latest_results.get(symbol)
    if result is None:
        result = analyze_stock(symbol)
    if result is None:
        send_message(chat_id, f"تعذر جلب بيانات {symbol} حالياً. حاول لاحقاً.")
        return

    sharia = result.get("sharia") or check_sharia(symbol)
    message = (
        f"📊 <b>تفاصيل {html.escape(symbol)}</b>\n"
        f"🏷️ المؤشر: {html.escape(market_label(result))}\n"
        "━━━━━━━━━━━━━━\n"
        f"💵 السعر: ${result['price']:.2f}\n"
        f"📈 RSI: {result['rsi']:.1f}\n"
        f"📊 التقييم: {result['score']}/100\n"
        f"📉 EMA20: ${result['ema20']:.2f}\n"
        f"📉 EMA50: ${result['ema50']:.2f}\n"
        f"📈 MACD: {result['macd']:.4f}\n"
        f"📈 MACD Signal: {result['macd_signal']:.4f}\n"
        f"📦 Volume: {result['volume_ratio']:.2f}x\n"
        f"🚧 المقاومة: ${result['resistance']:.2f}\n"
        f"🕌 الشرعية: {sharia_label(sharia)}\n"
        "━━━━━━━━━━━━━━\n"
        f"📌 {html.escape(result['rsi_label'])}"
    )
    send_message(chat_id, message, parse_mode="HTML")


def handle_callback(callback):
    callback_id = callback.get("id")
    data = callback.get("data", "")
    chat_id = callback.get("message", {}).get("chat", {}).get("id")
    if not callback_id or not chat_id:
        return

    parts = data.split(":", 1)
    if len(parts) != 2:
        answer_callback(callback_id, "طلب غير معروف")
        return

    action, symbol = parts
    symbol = symbol.upper()

    if action == "details":
        answer_callback(callback_id, "جاري جلب التفاصيل...")
        send_stock_details(chat_id, symbol)
    elif action == "watch":
        added = add_to_watchlist(chat_id, symbol)
        answer_callback(
            callback_id,
            f"تمت إضافة {symbol} للمراقبة ⭐" if added
            else f"{symbol} موجود مسبقاً في قائمتك"
        )
    else:
        answer_callback(callback_id, "طلب غير معروف")


# =========================
# Watchlist display and automatic alerts
# =========================

def send_watchlist(chat_id):
    items = get_watchlist(chat_id)
    if not items:
        send_message(
            chat_id,
            "⭐ قائمة المراقبة فارغة حالياً.\n"
            "استخدم زر إضافة للمراقبة أسفل أي توصية."
        )
        return

    message = "⭐ <b>قائمة الأسهم للمراقبة</b>\n\n"
    for symbol in items:
        message += f"• {html.escape(symbol)}\n"
    message += (
        "\n🔔 تتم مراقبة الأسهم تلقائياً بعد إغلاق السوق "
        "الأمريكي في أيام التداول."
    )
    send_message(chat_id, message, parse_mode="HTML")


def send_watch_entry_alert(chat_id, item):
    symbol = item["symbol"]
    price = item["price"]
    message = (
        "🔔 <b>تنبيه دخول من قائمة المراقبة</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"📌 <b>السهم:</b> {html.escape(symbol)}\n"
        f"💵 <b>السعر:</b> ${price:.2f}\n"
        f"📊 <b>التقييم:</b> {item['score']}/100\n"
        f"📈 <b>RSI:</b> {item['rsi']:.1f}\n"
        f"📉 <b>EMA20:</b> ${item['ema20']:.2f}\n"
        f"📉 <b>EMA50:</b> ${item['ema50']:.2f}\n"
        f"📈 <b>MACD:</b> {item['macd']:.4f}\n"
        f"📦 <b>Volume:</b> {item['volume_ratio']:.2f}x\n"
        f"🕌 <b>AAOIFI:</b> {sharia_label(item['sharia'])}\n"
        "━━━━━━━━━━━━━━\n"
        f"🟢 <b>الإشارة الفنية:</b> {item['signal']}\n"
        f"📌 <b>قرار RSI:</b> {html.escape(item['rsi_label'])}\n\n"
        f"🎯 الهدف النظري: ${price * 1.10:.2f}\n"
        f"🛑 وقف الخسارة النظري: ${price * 0.95:.2f}\n\n"
        "<i>هذا تنبيه فني للمتابعة وليس أمراً بالشراء أو ضماناً للربح.</i>"
    )
    send_message(
        chat_id, message,
        reply_markup=watch_alert_keyboard(symbol),
        parse_mode="HTML"
    )


def check_watchlist_alerts():
    if not scan_lock.acquire(blocking=False):
        logger.info("Watchlist check skipped: another scan is running")
        return False

    try:
        all_symbols = {
            symbol.upper()
            for items in watchlists.values()
            for symbol in items
        }
        if not all_symbols:
            logger.info("No watchlist symbols to scan")
            return True

        analysis_results = {}
        for symbol in sorted(all_symbols):
            result = analyze_stock(symbol)
            if result is None:
                continue
            result["markets"] = []
            result["sharia"] = check_sharia(symbol)
            analysis_results[symbol] = result

        now_text = datetime.now(ZoneInfo("America/New_York")).isoformat()

        for chat_id, items in list(watchlists.items()):
            for symbol in items:
                symbol = symbol.upper()
                result = analysis_results.get(symbol)
                if result is None:
                    continue

                state_key = f"{chat_id}:{symbol}"
                previous = watch_alert_state.get(state_key, {})
                was_eligible = previous.get("entry_allowed", False)

                is_eligible = (
                    result["sharia"] == "HALAL"
                    and result["signal"] in ("STRONG BUY", "MEDIUM")
                    and result["entry_allowed"]
                )

                if is_eligible and not was_eligible:
                    send_watch_entry_alert(chat_id, result)

                watch_alert_state[state_key] = {
                    "entry_allowed": bool(is_eligible),
                    "rsi_status": result["rsi_status"],
                    "signal": result["signal"],
                    "sharia": result["sharia"],
                    "last_rsi": result["rsi"],
                    "last_checked": now_text
                }

        save_json(WATCH_STATE_FILE, watch_alert_state)
        logger.info("Automatic watchlist scan completed")
        return True

    except Exception:
        logger.exception("Watchlist scan error")
        return False
    finally:
        scan_lock.release()


# =========================
# Daily scheduler
# =========================

def watchlist_scheduler():
    logger.info("Automatic watchlist scheduler started")
    new_york = ZoneInfo("America/New_York")

    while True:
        try:
            now = datetime.now(new_york)
            today = now.strftime("%Y-%m-%d")
            due = (
                now.hour > WATCH_SCAN_HOUR
                or (now.hour == WATCH_SCAN_HOUR and now.minute >= WATCH_SCAN_MINUTE)
            )
            if (
                now.weekday() < 5
                and due
                and watch_schedule.get("last_run_date") != today
            ):
                if check_watchlist_alerts():
                    watch_schedule["last_run_date"] = today
                    save_json(WATCH_SCHEDULE_FILE, watch_schedule)
        except Exception:
            logger.exception("Scheduler error")
        time.sleep(60)


def start_watchlist_scheduler():
    global scheduler_started
    if scheduler_started:
        return
    scheduler_started = True
    threading.Thread(
        target=watchlist_scheduler,
        daemon=True,
        name="WatchlistScheduler"
    ).start()
    logger.info("Watchlist scheduler thread launched")


# =========================
# Virtual position management
# =========================

def check_open_positions(analysis_results):
    sell_messages = []
    for symbol in list(positions.keys()):
        result = analysis_results.get(symbol)
        if not result:
            continue

        position = positions[symbol]
        price = result["price"]
        entry = float(position["entry"])
        target = float(position["target"])
        stop = float(position["stop"])
        reason = None

        if price >= target:
            reason = "Target reached"
        elif price <= stop:
            reason = "Stop loss reached"
        elif price < result["ema20"]:
            reason = "Price below EMA20"
        elif result["macd"] < result["macd_signal"]:
            reason = "MACD bearish"
        elif result["rsi"] < 40:
            reason = "RSI below 40"

        if reason:
            profit = ((price - entry) / entry) * 100
            sell_messages.append(
                f"🔴 SELL ALERT: {symbol}\n\n"
                f"المؤشر: {html.escape(market_label(result))}\n"
                f"الشرعية: {sharia_label(check_sharia(symbol))}\n"
                f"الدخول: ${entry:.2f}\n"
                f"الخروج: ${price:.2f}\n"
                f"النتيجة: {profit:+.2f}%\n"
                f"السبب: {reason}"
            )
            del positions[symbol]

    save_json(POSITIONS_FILE, positions)
    return sell_messages


# =========================
# Combined Nasdaq + Dow scan
# =========================

def scan_market():
    global latest_results

    if not scan_lock.acquire(blocking=False):
        return "⏳ يوجد فحص آخر قيد التنفيذ.", []

    try:
        universe = get_market_universe()
        symbols = sorted(universe.keys())
        nasdaq_count = sum("NASDAQ-100" in markets for markets in universe.values())
        dow_count = sum("DOW JONES" in markets for markets in universe.values())

        results = []
        analysis_results = {}
        failed = 0
        diagnostics = {
            "trend": 0, "macd": 0, "rsi": 0, "volume": 0, "breakout": 0
        }

        for symbol in symbols:
            result = analyze_stock(symbol)
            if result is None:
                failed += 1
                continue

            result["markets"] = universe[symbol]
            results.append(result)
            analysis_results[symbol] = result

            for key in diagnostics:
                if result["details"].get(key, 0) == 0:
                    diagnostics[key] += 1

        latest_results = analysis_results.copy()

        for message in check_open_positions(analysis_results):
            broadcast(message)

        strong = [x for x in results if x["signal"] == "STRONG BUY"]
        medium = [x for x in results if x["signal"] == "MEDIUM"]
        candidates = strong + medium

        for item in candidates:
            item["sharia"] = check_sharia(item["symbol"])

        eligible_strong = [x for x in strong if x["sharia"] == "HALAL"]
        eligible_medium = [x for x in medium if x["sharia"] == "HALAL"]
        excluded_haram = [x for x in candidates if x["sharia"] == "HARAM"]
        excluded_unknown = [x for x in candidates if x["sharia"] == "UNKNOWN"]

        eligible_strong.sort(key=lambda x: x["score"], reverse=True)
        eligible_medium.sort(key=lambda x: x["score"], reverse=True)

        entry_ready = [x for x in eligible_strong if x["entry_allowed"]]
        waiting = [
            x for x in eligible_strong + eligible_medium
            if not x["entry_allowed"]
        ]
        caution = [
            x for x in eligible_strong + eligible_medium
            if x["rsi_status"] == "CAUTION"
        ]

        # Virtual positions are keyed by ticker, so overlapping index
        # constituents cannot create duplicate positions.
        for item in eligible_strong:
            symbol = item["symbol"]
            if item["entry_allowed"] and symbol not in positions:
                price = item["price"]
                positions[symbol] = {
                    "entry": round(price, 2),
                    "target": round(price * 1.10, 2),
                    "stop": round(price * 0.95, 2),
                    "markets": item["markets"],
                    "opened_at": datetime.now(timezone.utc).isoformat()
                }
        save_json(POSITIONS_FILE, positions)

        def count_for_market(items, market):
            return sum(1 for x in items if market in x.get("markets", []))

        report = (
            "📊 Nasdaq-100 + Dow Jones Swing Scanner V7\n\n"
            "📚 Index Coverage\n"
            f"🔵 Nasdaq-100 constituents: {nasdaq_count}\n"
            f"🟣 Dow Jones constituents: {dow_count}\n"
            f"🔍 Unique symbols analyzed: {len(results)}\n"
            f"⚠️ Failed downloads: {failed}\n\n"
            "📊 Technical Signals (AAOIFI eligible)\n"
            f"🟢 Strong BUY: {len(eligible_strong)} "
            f"(Nasdaq: {count_for_market(eligible_strong, 'NASDAQ-100')} | "
            f"Dow: {count_for_market(eligible_strong, 'DOW JONES')})\n"
            f"🟡 Medium: {len(eligible_medium)} "
            f"(Nasdaq: {count_for_market(eligible_medium, 'NASDAQ-100')} | "
            f"Dow: {count_for_market(eligible_medium, 'DOW JONES')})\n\n"
            "🎯 RSI Entry Filter\n"
            f"🟢 Entry Ready: {len(entry_ready)}\n"
            f"🟡 Caution: {len(caution)}\n"
            f"🟠 Waiting / No Entry: {len(waiting)}\n\n"
            "🕌 Sharia Screening - AAOIFI\n"
            f"🟢 Eligible: {len(eligible_strong) + len(eligible_medium)}\n"
            f"🔴 Excluded: {len(excluded_haram)}\n"
            f"🟡 Unknown: {len(excluded_unknown)}\n\n"
            f"📂 Open virtual positions: {len(positions)}\n\n"
            "📋 Technical Diagnostics (failed criterion counts)\n"
            f"Trend: {diagnostics['trend']}\n"
            f"MACD: {diagnostics['macd']}\n"
            f"RSI: {diagnostics['rsi']}\n"
            f"Volume: {diagnostics['volume']}\n"
            f"Breakout: {diagnostics['breakout']}\n"
        )

        all_eligible = eligible_strong + eligible_medium
        all_eligible.sort(
            key=lambda x: (x["score"], x["entry_allowed"]),
            reverse=True
        )
        recommendations = all_eligible[:MAX_ALERTS]

        if not recommendations:
            report += "\n\nلا توجد فرص شراء شرعية مطابقة للشروط حالياً."

        if excluded_haram:
            report += "\n\n🔴 أسهم مستبعدة شرعياً:\n"
            for item in excluded_haram[:5]:
                report += (
                    f"\n🔴 {item['symbol']} | {item['score']}/100"
                    f" | {market_label(item)}"
                )

        if excluded_unknown:
            report += "\n\n🟡 أسهم لم تحسم شرعيتها:\n"
            for item in excluded_unknown[:5]:
                report += f"\n🟡 {item['symbol']} | {market_label(item)}"

        report += (
            "\n\nℹ️ الإشارات فنية وليست ضماناً للربح."
            "\nالصفقات المسجلة افتراضية وليست عمليات شراء فعلية."
            "\n⚠️ الفحص الشرعي آلي ومبني على بيانات Mizan ولا يُعد فتوى."
        )
        return report, recommendations

    except Exception as error:
        logger.exception("Scan error")
        return f"❌ حدث خطأ أثناء فحص السوق: {str(error)[:300]}", []
    finally:
        scan_lock.release()


# =========================
# Commands
# =========================

def send_scan_results(report, recommendations):
    for chat_id in list(chat_ids):
        send_message(chat_id, report)
        for item in recommendations:
            send_recommendation_card(chat_id, item)


def run_scan_and_send():
    report, recommendations = scan_market()
    send_scan_results(report, recommendations)


def handle_start(chat_id):
    register_chat(chat_id)
    send_message(
        chat_id,
        "👋 أهلاً بك في Nasdaq-100 + Dow Jones Swing Scanner V7\n\n"
        "📊 الأسواق: Nasdaq-100 و Dow Jones 30\n"
        "📈 الاستراتيجية: Swing Trading\n"
        "🕌 الفحص الشرعي: AAOIFI عبر Mizan\n"
        "🎯 شروط RSI والتقييم: نفس V6\n"
        "🔔 التنبيهات: BUY & SELL\n"
        "⭐ مراقبة تلقائية للأسهم المضافة\n\n"
        "الأوامر:\n"
        "/scan - فحص المؤشرين\n"
        "/status - حالة البوت\n"
        "/watchlist - قائمة المراقبة\n"
        "/help - المساعدة"
    )


def handle_status(chat_id):
    message = (
        "📊 Nasdaq + Dow Scanner V7\n\n"
        f"👥 Subscribers: {len(chat_ids)}\n"
        f"📂 Open positions: {len(positions)}\n"
        "🕌 Sharia standard: AAOIFI\n"
        f"📊 Cached Sharia results: {len(sharia_cache)}\n"
        f"⭐ Watchlist: {len(get_watchlist(chat_id))}\n"
        "🔔 Automatic watchlist alerts: ON\n"
        "🕓 Watchlist scan: 4:15 PM New York time, weekdays\n"
        "📈 Markets: Nasdaq-100 + Dow Jones\n"
    )
    if positions:
        message += "\nOpen virtual positions:\n"
        for symbol, position in positions.items():
            message += (
                f"\n{symbol}"
                f" | Entry: ${float(position['entry']):.2f}"
                f" | Target: ${float(position['target']):.2f}"
                f" | Stop: ${float(position['stop']):.2f}"
            )
    send_message(chat_id, message)


def handle_scan(chat_id):
    register_chat(chat_id)
    if scan_lock.locked():
        send_message(chat_id, "⏳ الفحص قيد التنفيذ.")
        return

    send_message(
        chat_id,
        "🔎 بدأ فحص Nasdaq-100 وDow Jones V7.\n"
        "سيتم تطبيق شروط V6 الفنية والفلترة الشرعية AAOIFI.\n"
        "ستصلك النتائج على شكل بطاقات تفاعلية."
    )
    threading.Thread(target=run_scan_and_send, daemon=True).start()


def handle_update(update):
    try:
        callback = update.get("callback_query")
        if callback:
            handle_callback(callback)
            return

        message = update.get("message")
        if not message:
            return

        chat_id = message["chat"]["id"]
        text = message.get("text", "").strip()
        if not text:
            return

        command = text.split()[0].split("@")[0].lower()
        if command == "/start":
            handle_start(chat_id)
        elif command == "/scan":
            handle_scan(chat_id)
        elif command == "/status":
            register_chat(chat_id)
            handle_status(chat_id)
        elif command == "/watchlist":
            register_chat(chat_id)
            send_watchlist(chat_id)
        elif command == "/help":
            send_message(
                chat_id,
                "/start - تشغيل البوت\n"
                "/scan - فحص ناسداك وداو جونز\n"
                "/status - حالة البوت\n"
                "/watchlist - قائمة المراقبة"
            )
    except Exception as error:
        logger.exception("Update error: %s", error)


# =========================
# Flask and webhook
# =========================

@app.route("/", methods=["GET"])
def home():
    return "Nasdaq-100 + Dow Jones Swing Scanner V7 is running."


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        update = request.get_json(silent=True)
        if update:
            handle_update(update)
        return "OK", 200
    except Exception as error:
        logger.exception("Webhook error: %s", error)
        return "ERROR", 500


def setup_webhook():
    if not TELEGRAM_TOKEN or not RENDER_EXTERNAL_URL:
        logger.error("Missing Telegram or Render environment variable")
        return
    try:
        response = requests.post(
            f"{TELEGRAM_API}/setWebhook",
            json={
                "url": f"{RENDER_EXTERNAL_URL}/webhook",
                "allowed_updates": ["message", "callback_query"]
            },
            timeout=20
        )
        logger.info("Webhook response: %s", response.text)
    except Exception as error:
        logger.error("Webhook setup error: %s", error)


setup_webhook()
start_watchlist_scheduler()
