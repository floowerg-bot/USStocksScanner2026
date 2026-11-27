import os
import json
import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import yfinance as yf
from flask import Flask, request

# =========================
# إعدادات عامة
# =========================

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

CHAT_FILE = "chats.json"
POSITIONS_FILE = "positions.json"

MAX_ALERTS = 10

scan_lock = threading.Lock()

# =========================
# تخزين البيانات
# =========================

def load_json(filename, default):
    try:
        with open(filename, "r", encoding="utf-8") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return default
    except Exception as error:
        logger.error("File loading error %s: %s", filename, error)
        return default


def save_json(filename, data):
    try:
        with open(filename, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=4)
    except Exception as error:
        logger.error("File saving error %s: %s", filename, error)


chat_ids = load_json(CHAT_FILE, [])
positions = load_json(POSITIONS_FILE, {})

# =========================
# Telegram
# =========================

def send_message(chat_id, message):
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is missing")
        return

    url = f"{TELEGRAM_API}/sendMessage"

    try:
        response = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": message
            },
            timeout=20
        )

        if not response.ok:
            logger.error("Telegram error: %s", response.text)

    except Exception as error:
        logger.error("Telegram send error: %s", error)


def broadcast(message):
    for chat_id in list(chat_ids):
        send_message(chat_id, message)


def register_chat(chat_id):
    chat_id = str(chat_id)

    if chat_id not in chat_ids:
        chat_ids.append(chat_id)
        save_json(CHAT_FILE, chat_ids)


# =========================
# قائمة Nasdaq-100
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

        symbols = []

        for row in rows:
            symbol = row.get("symbol")

            if symbol:
                symbols.append(
                    symbol.strip().replace(".", "-")
                )

        symbols = sorted(set(symbols))

        if len(symbols) >= 90:
            logger.info(
                "Successfully loaded %s Nasdaq-100 symbols",
                len(symbols)
            )
            return symbols

        logger.warning(
            "Nasdaq API returned only %s symbols",
            len(symbols)
        )

    except Exception as error:
        logger.error("Nasdaq list error: %s", error)

    # قائمة احتياطية في حال تعطل مصدر Nasdaq

    fallback_symbols = [
        "AAPL", "MSFT", "NVDA", "AMZN", "META",
        "GOOGL", "GOOG", "AVGO", "TSLA", "COST",
        "NFLX", "AMD", "PEP", "ADBE", "CSCO",
        "TMUS", "LIN", "QCOM", "INTU", "TXN",
        "AMGN", "ISRG", "BKNG", "HON", "AMAT",
        "VRTX", "PANW", "ADP", "GILD", "SBUX"
    ]

    logger.warning("Using fallback Nasdaq symbols list")

    return fallback_symbols


# =========================
# المؤشرات الفنية
# =========================

def calculate_rsi(series, period=14):

    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    average_gain = gain.ewm(
        alpha=1 / period,
        min_periods=period,
        adjust=False
    ).mean()

    average_loss = loss.ewm(
        alpha=1 / period,
        min_periods=period,
        adjust=False
    ).mean()

    rs = average_gain / average_loss.replace(0, float("nan"))

    rsi = 100 - (100 / (1 + rs))

    return rsi.fillna(100)


def normalize_yfinance_data(df):

    if df is None or df.empty:
        return None

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    required = ["Close", "High", "Volume"]

    for column in required:
        if column not in df.columns:
            return None

    return df.dropna(subset=["Close", "High"])


def get_completed_data(df):

    """
    أثناء ساعات التداول الأمريكية، نتجنب استخدام شمعة اليوم
    غير المكتملة إذا كانت موجودة.
    """

    try:
        now_ny = datetime.now(ZoneInfo("America/New_York"))

        if now_ny.weekday() < 5:
            if (
                now_ny.hour > 9
                or (now_ny.hour == 9 and now_ny.minute >= 30)
            ) and now_ny.hour < 16:

                last_date = pd.Timestamp(df.index[-1]).date()

                if last_date == now_ny.date() and len(df) > 60:
                    return df.iloc[:-1]

    except Exception as error:
        logger.warning("Completed candle check: %s", error)

    return df


# =========================
# تحليل السهم ونظام النقاط V4
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

        df = normalize_yfinance_data(df)

        if df is None or len(df) < 60:
            return None

        df = get_completed_data(df)

        if df is None or len(df) < 60:
            return None

        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        volume = df["Volume"].astype(float)

        ema20 = close.ewm(span=20, adjust=False).mean()
        ema50 = close.ewm(span=50, adjust=False).mean()

        rsi_series = calculate_rsi(close)

        macd_line = (
            close.ewm(span=12, adjust=False).mean()
            - close.ewm(span=26, adjust=False).mean()
        )

        signal_line = macd_line.ewm(
            span=9,
            adjust=False
        ).mean()

        price = float(close.iloc[-1])
        current_ema20 = float(ema20.iloc[-1])
        current_ema50 = float(ema50.iloc[-1])
        current_rsi = float(rsi_series.iloc[-1])

        current_macd = float(macd_line.iloc[-1])
        current_signal = float(signal_line.iloc[-1])

        current_volume = float(volume.iloc[-1])

        average_volume = float(
            volume.iloc[-21:-1].mean()
        )

        if average_volume <= 0:
            volume_ratio = 0
        else:
            volume_ratio = current_volume / average_volume

        # المقاومة: أعلى سعر خلال آخر 20 جلسة
        # باستثناء الجلسة الحالية

        resistance = float(
            high.iloc[-21:-1].max()
        )

        score = 0
        details = {}

        # 1) الاتجاه العام - 25 نقطة

        if price > current_ema20 and current_ema20 > current_ema50:
            score += 25
            details["trend"] = 25

        elif price > current_ema20 and price > current_ema50:
            score += 15
            details["trend"] = 15

        else:
            details["trend"] = 0

        # 2) MACD - 20 نقطة

        if current_macd > current_signal:
            score += 20
            details["macd"] = 20
        else:
            details["macd"] = 0

        # 3) RSI - 15 نقطة

        if 50 <= current_rsi <= 68:
            score += 15
            details["rsi"] = 15

        elif 45 <= current_rsi < 50 or 68 < current_rsi <= 72:
            score += 8
            details["rsi"] = 8

        else:
            details["rsi"] = 0

        # 4) حجم التداول - 20 نقطة

        if volume_ratio >= 1.0:
            score += 20
            details["volume"] = 20

        elif volume_ratio >= 0.80:
            score += 10
            details["volume"] = 10

        else:
            details["volume"] = 0

        # 5) الاختراق - 20 نقطة

        if price > resistance:
            score += 20
            details["breakout"] = 20

        elif price >= resistance * 0.98:
            score += 10
            details["breakout"] = 10

        else:
            details["breakout"] = 0

        # تصنيف التوصية

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
            "ema20": current_ema20,
            "ema50": current_ema50,
            "macd": current_macd,
            "macd_signal": current_signal,
            "volume": current_volume,
            "average_volume": average_volume,
            "volume_ratio": volume_ratio,
            "resistance": resistance,
            "details": details
        }

    except Exception as error:
        logger.error(
            "Analysis error for %s: %s",
            symbol,
            error
        )
        return None


# =========================
# إدارة الصفقات المفتوحة
# =========================

def check_open_positions(analysis_results):

    global positions

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
            reason = "MACD bearish crossover"

        elif result["rsi"] < 40:
            reason = "RSI below 40"

        if reason:

            profit_percent = (
                (price - entry) / entry
            ) * 100

            message = (
                f"🔴 SELL ALERT: {symbol}\n\n"
                f"سعر الدخول: ${entry:.2f}\n"
                f"سعر الخروج: ${price:.2f}\n"
                f"النتيجة: {profit_percent:+.2f}%\n"
                f"السبب: {reason}"
            )

            sell_messages.append(message)

            del positions[symbol]

    save_json(POSITIONS_FILE, positions)

    return sell_messages


# =========================
# تنفيذ فحص السوق
# =========================

def scan_market():

    if not scan_lock.acquire(blocking=False):
        return (
            "⏳ يوجد فحص للسوق قيد التنفيذ. "
            "انتظر حتى ينتهي ثم حاول مجدداً."
        )

    try:

        symbols = get_nasdaq_symbols()

        if not symbols:
            return "❌ تعذر الحصول على قائمة Nasdaq-100."

        results = []
        analysis_results = {}

        failed_downloads = 0

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
                failed_downloads += 1
                continue

            results.append(result)
            analysis_results[symbol] = result

            details = result["details"]

            for key in diagnostics:
                if details.get(key, 0) == 0:
                    diagnostics[key] += 1

        # فحص الصفقات المفتوحة أولاً

        sell_messages = check_open_positions(
            analysis_results
        )

        for message in sell_messages:
            broadcast(message)

        strong_buys = [
            item for item in results
            if item["signal"] == "STRONG BUY"
        ]

        medium_signals = [
            item for item in results
            if item["signal"] == "MEDIUM"
        ]

        strong_buys.sort(
            key=lambda item: item["score"],
            reverse=True
        )

        medium_signals.sort(
            key=lambda item: item["score"],
            reverse=True
        )

        # تسجيل صفقات Strong BUY الجديدة فقط

        for item in strong_buys:

            symbol = item["symbol"]

            if symbol not in positions:

                entry = item["price"]

                positions[symbol] = {
                    "entry": round(entry, 2),
                    "target": round(entry * 1.10, 2),
                    "stop": round(entry * 0.95, 2),
                    "opened_at": datetime.utcnow().isoformat()
                }

        save_json(POSITIONS_FILE, positions)

        # تقرير الفحص

        report = (
            "📊 Nasdaq-100 Scan Completed V4\n\n"
            f"🔍 Symbols in list: {len(symbols)}\n"
            f"✅ Stocks analyzed: {len(results)}\n"
            f"⚠️ Failed downloads: {failed_downloads}\n\n"
            f"🟢 Strong BUY: {len(strong_buys)}\n"
            f"🟡 Medium: {len(medium_signals)}\n"
            f"🔴 Open positions: {len(positions)}\n\n"
            "📋 Stocks with zero points in each condition\n"
            f"📈 Trend: {diagnostics['trend']}\n"
            f"📉 MACD: {diagnostics['macd']}\n"
            f"📊 RSI: {diagnostics['rsi']}\n"
            f"📦 Volume: {diagnostics['volume']}\n"
            f"🚀 Breakout: {diagnostics['breakout']}\n\n"
            "ℹ️ Exclusion counts can overlap."
        )

        # تفاصيل التوصيات

        recommendations = []

        for item in strong_buys[:MAX_ALERTS]:

            symbol = item["symbol"]
            price = item["price"]

            target = price * 1.10
            stop = price * 0.95

            recommendations.append(
                "🟢 STRONG BUY: "
                f"{symbol}\n"
                f"السعر: ${price:.2f}\n"
                f"الهدف: ${target:.2f}\n"
                f"وقف الخسارة: ${stop:.2f}\n"
                f"النقاط: {item['score']}/100\n"
                f"RSI: {item['rsi']:.1f}\n"
                f"حجم التداول: {item['volume_ratio']:.2f}x"
            )

        remaining_slots = max(
            0,
            MAX_ALERTS - len(recommendations)
        )

        for item in medium_signals[:remaining_slots]:

            recommendations.append(
                "🟡 MEDIUM WATCHLIST: "
                f"{item['symbol']}\n"
                f"السعر: ${item['price']:.2f}\n"
                f"النقاط: {item['score']}/100\n"
                f"RSI: {item['rsi']:.1f}\n"
                f"حجم التداول: {item['volume_ratio']:.2f}x\n"
                "مراقبة السهم قبل الدخول."
            )

        if recommendations:
            report += "\n\n" + "\n\n".join(recommendations)
        else:
            report += (
                "\n\nNo BUY or MEDIUM opportunities "
                "matched the scoring strategy."
            )

        report += (
            "\n\n⚠️ التنبيهات إشارات فنية وليست ضماناً للربح. "
            "تحقق من السهم قبل اتخاذ قرار التداول."
        )

        return report

    except Exception as error:

        logger.exception("Market scan error")

        return (
            "❌ حدث خطأ أثناء فحص السوق.\n"
            f"التفاصيل: {str(error)[:500]}"
        )

    finally:
        scan_lock.release()


# =========================
# أوامر البوت
# =========================

def handle_start(chat_id):

    register_chat(chat_id)

    message = (
        "👋 أهلاً بك في Nasdaq-100 Swing Scanner V4\n\n"
        "📊 نطاق الفحص: Nasdaq-100\n"
        "📈 نوع التداول: Swing Trading\n"
        "🔔 التنبيهات: BUY & SELL\n"
        "🧮 نظام التقييم: 100 نقطة\n\n"
        "الأوامر المتاحة:\n"
        "/scan - فحص السوق\n"
        "/status - حالة البوت والصفقات\n"
        "/help - المساعدة\n\n"
        "اضغط /scan لبدء الفحص."
    )

    send_message(chat_id, message)


def handle_status(chat_id):

    message = (
        "📊 Nasdaq-100 Scanner Status\n\n"
        f"👥 Subscribers: {len(chat_ids)}\n"
        f"🔴 Open positions: {len(positions)}\n"
        f"⚙️ Strategy: Swing Trading\n"
        f"🧮 Scoring: V4 / 100\n"
        f"🔔 Alerts: BUY & SELL"
    )

    if positions:

        message += "\n\n📂 Open Positions:\n"

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

        send_message(
            chat_id,
            "⏳ يوجد فحص للسوق قيد التنفيذ. انتظر قليلاً."
        )
        return

    send_message(
        chat_id,
        "🔎 بدأ فحص Nasdaq-100 V4، انتظر النتائج..."
    )

    thread = threading.Thread(
        target=run_scan_and_send,
        daemon=True
    )

    thread.start()


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
                "الأوامر المتاحة:\n\n"
                "/start - تشغيل البوت\n"
                "/scan - فحص Nasdaq-100\n"
                "/status - حالة البوت والصفقات"
            )

    except Exception as error:
        logger.exception("Update handling error: %s", error)


# =========================
# Flask Webhook
# =========================

@app.route("/", methods=["GET"])
def home():

    return "Nasdaq-100 Swing Scanner V4 is running."


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
# إعداد Telegram Webhook
# =========================

def setup_webhook():

    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is missing")
        return

    if not RENDER_EXTERNAL_URL:
        logger.error("RENDER_EXTERNAL_URL is missing")
        return

    webhook_url = f"{RENDER_EXTERNAL_URL}/webhook"

    try:

        response = requests.post(
            f"{TELEGRAM_API}/setWebhook",
            json={"url": webhook_url},
            timeout=20
        )

        logger.info(
            "Webhook setup response: %s",
            response.text
        )

    except Exception as error:

        logger.error(
            "Webhook setup failed: %s",
            error
        )


setup_webhook()
