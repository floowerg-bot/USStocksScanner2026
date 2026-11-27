import os
import json
import logging
import threading
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
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

CHAT_FILE = "chats.json"
POSITIONS_FILE = "positions.json"
SHARIA_CACHE_FILE = "sharia_cache.json"

MIZAN_URL = "https://askmizan.com/api/v1/screen/{}"

MAX_ALERTS = 10
SHARIA_CACHE_HOURS = 24

scan_lock = threading.Lock()

chat_ids = []
positions = {}
sharia_cache = {}


# =========================
# التخزين
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
        logger.error("Save error: %s", error)


chat_ids = load_json(CHAT_FILE, [])
positions = load_json(POSITIONS_FILE, {})
sharia_cache = load_json(SHARIA_CACHE_FILE, {})


# =========================
# Telegram
# =========================

def send_message(chat_id, message):
    if not TELEGRAM_TOKEN:
        logger.error("Missing TELEGRAM_BOT_TOKEN")
        return

    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message
            },
            timeout=20
        )
    except Exception as error:
        logger.error("Telegram error: %s", error)


def broadcast(message):
    for chat_id in list(chat_ids):
        send_message(chat_id, message)


def register_chat(chat_id):
    chat_id = str(chat_id)

    if chat_id not in chat_ids:
        chat_ids.append(chat_id)
        save_json(CHAT_FILE, chat_ids)


# =========================
# فحص شرعية السهم - Mizan
# =========================

def check_sharia(symbol):
    """
    يعتمد على نتيجة AAOIFI فقط.
    إذا لم تتوفر نتيجة واضحة يرجع UNKNOWN.
    """

    now = datetime.now(timezone.utc).timestamp()

    cached = sharia_cache.get(symbol)

    if cached:
        try:
            age_hours = (
                now - float(cached["timestamp"])
            ) / 3600

            if (
                age_hours < SHARIA_CACHE_HOURS
                and cached["status"] in ("HALAL", "HARAM")
            ):
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

        # الطريقة الأولى: البحث داخل screens

        screens = data.get("screens", [])

        for screen in screens:
            standard = str(
                screen.get("standard", "")
            ).upper()

            if standard == "AAOIFI":
                compliant = screen.get("compliant")

                if compliant is True:
                    status = "HALAL"
                elif compliant is False:
                    status = "HARAM"

                break

        # الطريقة الثانية: by_standard

        if status is None:
            by_standard = data.get("by_standard", {})
            result = str(
                by_standard.get("AAOIFI", "")
            ).lower()

            if result in ("pass", "compliant"):
                status = "HALAL"

            elif result in ("fail", "non-compliant"):
                status = "HARAM"

        # لا نعتمد على التخمين

        if status not in ("HALAL", "HARAM"):
            return "UNKNOWN"

        sharia_cache[symbol] = {
            "status": status,
            "timestamp": now
        }

        save_json(SHARIA_CACHE_FILE, sharia_cache)

        return status

    except Exception as error:
        logger.warning(
            "Sharia check failed for %s: %s",
            symbol,
            error
        )
        return "UNKNOWN"


def sharia_label(status):
    if status == "HALAL":
        return "🟢 شرعي"

    if status == "HARAM":
        return "🔴 غير شرعي"

    return "🟡 غير محسوم"


# =========================
# Nasdaq-100
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
        response = requests.get(
            url,
            headers=headers,
            timeout=20
        )

        response.raise_for_status()
        payload = response.json()

        data = payload.get("data", {})
        rows = data.get("rows", [])

        if not rows and isinstance(data.get("data"), dict):
            rows = data["data"].get("rows", [])

        symbols = [
            row["symbol"].strip().replace(".", "-")
            for row in rows
            if row.get("symbol")
        ]

        symbols = sorted(set(symbols))

        if len(symbols) >= 90:
            return symbols

    except Exception as error:
        logger.error("Nasdaq API error: %s", error)

    return [
        "AAPL", "MSFT", "NVDA", "AMZN", "META",
        "GOOGL", "GOOG", "AVGO", "TSLA", "COST",
        "NFLX", "AMD", "PEP", "ADBE", "CSCO",
        "TMUS", "LIN", "QCOM", "INTU", "TXN",
        "AMGN", "ISRG", "BKNG", "HON", "AMAT",
        "VRTX", "PANW", "ADP", "GILD", "SBUX"
    ]


# =========================
# المؤشرات الفنية
# =========================

def calculate_rsi(series, period=14):

    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / period,
        min_periods=period,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        min_periods=period,
        adjust=False
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
                now.hour > 9
                or (now.hour == 9 and now.minute >= 30)
            )

            if market_open and now.hour < 16:
                last_date = pd.Timestamp(df.index[-1]).date()

                if last_date == now.date() and len(df) > 60:
                    return df.iloc[:-1]

    except Exception as error:
        logger.warning("Candle check: %s", error)

    return df


# =========================
# تحليل السهم V4
# =========================

def analyze_stock(symbol):

    try:
        df = yf.download(
            symbol,
            period="6mo",
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=False
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

        signal_line = macd.ewm(
            span=9,
            adjust=False
        ).mean()

        price = float(close.iloc[-1])
        e20 = float(ema20.iloc[-1])
        e50 = float(ema50.iloc[-1])
        current_rsi = float(rsi.iloc[-1])

        current_macd = float(macd.iloc[-1])
        current_signal = float(signal_line.iloc[-1])

        average_volume = float(volume.iloc[-21:-1].mean())
        current_volume = float(volume.iloc[-1])

        volume_ratio = (
            current_volume / average_volume
            if average_volume > 0 else 0
        )

        resistance = float(high.iloc[-21:-1].max())

        score = 0
        details = {}

        # الاتجاه

        if price > e20 and e20 > e50:
            score += 25
            details["trend"] = 25

        elif price > e20 and price > e50:
            score += 15
            details["trend"] = 15

        else:
            details["trend"] = 0

        # MACD

        if current_macd > current_signal:
            score += 20
            details["macd"] = 20
        else:
            details["macd"] = 0

        # RSI

        if 50 <= current_rsi <= 68:
            score += 15
            details["rsi"] = 15

        elif 45 <= current_rsi < 50 or 68 < current_rsi <= 72:
            score += 8
            details["rsi"] = 8

        else:
            details["rsi"] = 0

        # Volume

        if volume_ratio >= 1:
            score += 20
            details["volume"] = 20

        elif volume_ratio >= 0.80:
            score += 10
            details["volume"] = 10

        else:
            details["volume"] = 0

        # Breakout

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

        return {
            "symbol": symbol,
            "price": price,
            "score": score,
            "signal": signal,
            "rsi": current_rsi,
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
# إدارة الصفقات
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
# فحص السوق V5
# =========================

def scan_market():

    if not scan_lock.acquire(blocking=False):
        return "⏳ يوجد فحص آخر قيد التنفيذ."

    try:

        symbols = get_nasdaq_symbols()

        results = []
        analysis_results = {}

        failed = 0

        diagnostics = {
            "trend": 0,
            "macd": 0,
            "rsi": 0,
            "volume": 0,
            "breakout": 0
        }

        for symbol in symbols:

            result = analyze_stock(symbol)

            if result is None:
                failed += 1
                continue

            results.append(result)
            analysis_results[symbol] = result

            for key in diagnostics:
                if result["details"].get(key, 0) == 0:
                    diagnostics[key] += 1

        # تنبيهات البيع

        sell_messages = check_open_positions(analysis_results)

        for message in sell_messages:
            broadcast(message)

        strong = [
            x for x in results
            if x["signal"] == "STRONG BUY"
        ]

        medium = [
            x for x in results
            if x["signal"] == "MEDIUM"
        ]

        candidates = strong + medium

        # فحص الشرعية للمرشحين فقط

        for item in candidates:
            item["sharia"] = check_sharia(item["symbol"])

        # السماح بالتوصية للأسهم المصنفة شرعية فقط

        eligible_strong = [
            x for x in strong
            if x["sharia"] == "HALAL"
        ]

        eligible_medium = [
            x for x in medium
            if x["sharia"] == "HALAL"
        ]

        excluded_haram = [
            x for x in candidates
            if x["sharia"] == "HARAM"
        ]

        excluded_unknown = [
            x for x in candidates
            if x["sharia"] == "UNKNOWN"
        ]

        eligible_strong.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        eligible_medium.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        # تسجيل صفقات الشراء الشرعية فقط

        for item in eligible_strong:

            symbol = item["symbol"]

            if symbol not in positions:

                price = item["price"]

                positions[symbol] = {
                    "entry": round(price, 2),
                    "target": round(price * 1.10, 2),
                    "stop": round(price * 0.95, 2),
                    "opened_at": datetime.now(
                        timezone.utc
                    ).isoformat()
                }

        save_json(POSITIONS_FILE, positions)

        report = (
            "📊 Nasdaq-100 Swing Scanner V5\n\n"
            f"🔍 Symbols: {len(symbols)}\n"
            f"✅ Analyzed: {len(results)}\n"
            f"⚠️ Failed: {failed}\n\n"
            "📊 Technical Signals\n"
            f"🟢 Strong BUY: {len(eligible_strong)}\n"
            f"🟡 Medium: {len(eligible_medium)}\n\n"
            "🕌 Sharia Screening - AAOIFI\n"
            f"🟢 Eligible: {len(eligible_strong) + len(eligible_medium)}\n"
            f"🔴 Excluded (Non-compliant): {len(excluded_haram)}\n"
            f"🟡 Unknown: {len(excluded_unknown)}\n\n"
            f"📂 Open positions: {len(positions)}\n\n"
            "📋 Technical Diagnostics\n"
            f"Trend: {diagnostics['trend']}\n"
            f"MACD: {diagnostics['macd']}\n"
            f"RSI: {diagnostics['rsi']}\n"
            f"Volume: {diagnostics['volume']}\n"
            f"Breakout: {diagnostics['breakout']}\n"
        )

        recommendations = []

        for item in eligible_strong[:MAX_ALERTS]:

            price = item["price"]

            recommendations.append(
                f"🟢 STRONG BUY: {item['symbol']}\n"
                f"🕌 الشرعية: {sharia_label(item['sharia'])}\n"
                f"السعر: ${price:.2f}\n"
                f"الهدف: ${price * 1.10:.2f}\n"
                f"وقف الخسارة: ${price * 0.95:.2f}\n"
                f"النقاط: {item['score']}/100\n"
                f"RSI: {item['rsi']:.1f}\n"
                f"Volume: {item['volume_ratio']:.2f}x"
            )

        remaining = max(0, MAX_ALERTS - len(recommendations))

        for item in eligible_medium[:remaining]:

            recommendations.append(
                f"🟡 MEDIUM: {item['symbol']}\n"
                f"🕌 الشرعية: {sharia_label(item['sharia'])}\n"
                f"السعر: ${item['price']:.2f}\n"
                f"النقاط: {item['score']}/100\n"
                f"RSI: {item['rsi']:.1f}\n"
                "مراقبة قبل الدخول."
            )

        if recommendations:
            report += "\n\n" + "\n\n".join(recommendations)

        else:
            report += (
                "\n\nلا توجد فرص شراء شرعية مطابقة للشروط حالياً."
            )

        # عرض الأسهم المستبعدة شرعياً

        if excluded_haram:
            report += "\n\n🔴 أسهم مستبعدة شرعياً:\n"

            for item in excluded_haram[:5]:
                report += (
                    f"\n🔴 {item['symbol']}"
                    f" | {item['score']}/100"
                    f" | غير شرعي وفق فحص AAOIFI"
                )

        if excluded_unknown:
            report += "\n\n🟡 أسهم لم تحسم شرعيتها:\n"

            for item in excluded_unknown[:5]:
                report += f"\n🟡 {item['symbol']} | غير محسوم"

        report += (
            "\n\n⚠️ الفحص الشرعي آلي ومبني على بيانات Mizan "
            "ولا يُعد فتوى شرعية. راجع البيانات والجهة الشرعية "
            "التي تعتمدها قبل الاستثمار."
        )

        return report

    except Exception as error:

        logger.exception("Scan error")

        return f"❌ حدث خطأ أثناء فحص السوق: {str(error)[:300]}"

    finally:
        scan_lock.release()


# =========================
# أوامر البوت
# =========================

def handle_start(chat_id):

    register_chat(chat_id)

    send_message(
        chat_id,
        "👋 أهلاً بك في Nasdaq-100 Swing Scanner V5\n\n"
        "📊 السوق: Nasdaq-100\n"
        "📈 الاستراتيجية: Swing Trading\n"
        "🕌 الفحص الشرعي: AAOIFI عبر Mizan\n"
        "🔔 التنبيهات: BUY & SELL\n\n"
        "الأوامر:\n"
        "/scan - فحص السوق\n"
        "/status - حالة البوت\n"
        "/help - المساعدة"
    )


def handle_status(chat_id):

    message = (
        "📊 Nasdaq Scanner V5\n\n"
        f"👥 Subscribers: {len(chat_ids)}\n"
        f"📂 Open positions: {len(positions)}\n"
        f"🕌 Sharia standard: AAOIFI\n"
        f"📊 Cached Sharia results: {len(sharia_cache)}\n"
    )

    if positions:
        message += "\nOpen positions:\n"

        for symbol, position in positions.items():
            message += (
                f"\n{symbol}"
                f" | Entry: ${float(position['entry']):.2f}"
                f" | Target: ${float(position['target']):.2f}"
                f" | Stop: ${float(position['stop']):.2f}"
            )

    send_message(chat_id, message)


def run_scan_and_send():
    report = scan_market()
    broadcast(report)


def handle_scan(chat_id):

    register_chat(chat_id)

    if scan_lock.locked():
        send_message(chat_id, "⏳ الفحص قيد التنفيذ.")
        return

    send_message(
        chat_id,
        "🔎 بدأ فحص Nasdaq-100 V5.\n"
        "سيتم فحص المؤشرات والشرعية، انتظر النتائج..."
    )

    threading.Thread(
        target=run_scan_and_send,
        daemon=True
    ).start()


def handle_update(update):

    try:
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

        elif command == "/help":
            send_message(
                chat_id,
                "/start - تشغيل البوت\n"
                "/scan - فحص السوق\n"
                "/status - حالة البوت"
            )

    except Exception as error:
        logger.exception("Update error: %s", error)


# =========================
# Flask
# =========================

@app.route("/", methods=["GET"])
def home():
    return "Nasdaq-100 Swing Scanner V5 is running."


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


# =========================
# Webhook setup
# =========================

def setup_webhook():

    if not TELEGRAM_TOKEN or not RENDER_EXTERNAL_URL:
        logger.error("Missing Telegram or Render environment variable")
        return

    try:
        response = requests.post(
            f"{TELEGRAM_API}/setWebhook",
            json={
                "url": f"{RENDER_EXTERNAL_URL}/webhook"
            },
            timeout=20
        )

        logger.info("Webhook response: %s", response.text)

    except Exception as error:
        logger.error("Webhook setup error: %s", error)


setup_webhook()
