import os
import json
import threading
import requests
import pandas as pd
import yfinance as yf

from io import StringIO
from flask import Flask, request
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# =========================
# SETTINGS
# =========================

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
API = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else None

CHAT_FILE = "chats.json"
POSITIONS_FILE = "positions.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 USStocksScanner/1.0",
    "Accept": "application/json, text/html, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/"
}


# =========================
# JSON FUNCTIONS
# =========================

def load_json(filename):
    try:
        with open(filename, "r") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_json(filename, data):
    with open(filename, "w") as f:
        json.dump(data, f)


# =========================
# TELEGRAM
# =========================

def send_message(chat_id, message):

    if not API:
        print("ERROR: TELEGRAM_BOT_TOKEN is missing")
        return

    try:
        response = requests.post(
            f"{API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message
            },
            timeout=20
        )

        response.raise_for_status()

    except Exception as e:
        print("Telegram error:", e)


# =========================
# NASDAQ-100 TICKERS
# =========================

def get_nasdaq_tickers():

    # Try Nasdaq API
    try:
        url = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"

        response = requests.get(
            url,
            headers=HEADERS,
            timeout=20
        )

        response.raise_for_status()

        data = response.json()

        rows = data.get(
            "data", {}
        ).get(
            "data", {}
        ).get(
            "rows", []
        )

        tickers = [
            row["symbol"].replace(".", "-")
            for row in rows
            if row.get("symbol")
        ]

        if len(tickers) >= 50:
            print(f"Loaded {len(tickers)} Nasdaq tickers")
            return tickers

    except Exception as e:
        print("Nasdaq API error:", e)

    # Fallback to Wikipedia
    try:
        url = "https://en.wikipedia.org/wiki/Nasdaq-100"

        response = requests.get(
            url,
            headers=HEADERS,
            timeout=20
        )

        response.raise_for_status()

        tables = pd.read_html(
            StringIO(response.text)
        )

        for table in tables:
            for col in table.columns:

                if str(col).lower() in ("ticker", "symbol"):

                    tickers = (
                        table[col]
                        .dropna()
                        .astype(str)
                        .str.replace(".", "-", regex=False)
                        .tolist()
                    )

                    if len(tickers) >= 50:
                        print(
                            f"Loaded {len(tickers)} tickers from Wikipedia"
                        )
                        return tickers

    except Exception as e:
        print("Wikipedia error:", e)

    raise RuntimeError(
        "Could not load Nasdaq-100 constituents"
    )


# =========================
# MARKET SCANNER
# =========================

def scan_market(chat_id):

    try:
        tickers = get_nasdaq_tickers()

        buys = []
        sells = []

        positions = load_json(POSITIONS_FILE)

        for ticker in tickers:

            try:
                hist = yf.Ticker(ticker).history(
                    period="6mo",
                    interval="1d",
                    timeout=20
                )

                if hist.empty or len(hist) < 60:
                    continue

                close = hist["Close"].dropna()
                volume = hist["Volume"]

                if len(close) < 60:
                    continue

                # EMA 20 / 50
                ema20 = close.ewm(
                    span=20,
                    adjust=False
                ).mean()

                ema50 = close.ewm(
                    span=50,
                    adjust=False
                ).mean()

                # RSI 14
                delta = close.diff()

                gain = delta.clip(
                    lower=0
                ).ewm(
                    alpha=1/14,
                    adjust=False
                ).mean()

                loss = (-delta.clip(
                    upper=0
                )).ewm(
                    alpha=1/14,
                    adjust=False
                ).mean()

                rs = gain / loss.replace(
                    0, float("nan")
                )

                rsi = 100 - (100 / (1 + rs))

                # MACD
                macd = (
                    close.ewm(
                        span=12,
                        adjust=False
                    ).mean()
                    -
                    close.ewm(
                        span=26,
                        adjust=False
                    ).mean()
                )

                signal = macd.ewm(
                    span=9,
                    adjust=False
                ).mean()

                # Current values
                price = float(close.iloc[-1])
                current_rsi = float(rsi.iloc[-1])

                current_ema20 = float(ema20.iloc[-1])
                current_ema50 = float(ema50.iloc[-1])

                current_macd = float(macd.iloc[-1])
                current_signal = float(signal.iloc[-1])

                current_volume = float(volume.iloc[-1])

                average_volume = float(
                    volume.rolling(20).mean().iloc[-1]
                )

                values = [
                    price,
                    current_rsi,
                    current_ema20,
                    current_ema50,
                    current_macd,
                    current_signal,
                    current_volume,
                    average_volume
                ]

                if not all(
                    pd.notna(x) for x in values
                ):
                    continue

                # BUY conditions
                buy = (
                    price > current_ema20 > current_ema50
                    and 50 < current_rsi < 68
                    and current_macd > current_signal
                    and current_volume > average_volume
                )

                # SELL conditions
                sell = (
                    price < current_ema20
                    or current_macd < current_signal
                    or current_rsi < 40
                )

                # BUY signal
                if buy and ticker not in positions:

                    stop = price * 0.95
                    target = price * 1.10

                    buys.append(
                        f"🟢 BUY: {ticker}\n"
                        f"السعر: ${price:.2f}\n"
                        f"الهدف: ${target:.2f}\n"
                        f"وقف الخسارة: ${stop:.2f}\n"
                        f"RSI: {current_rsi:.1f}"
                    )

                    positions.append(ticker)

                # SELL signal
                elif sell and ticker in positions:

                    sells.append(
                        f"🔴 SELL: {ticker}\n"
                        f"السعر: ${price:.2f}\n"
                        f"RSI: {current_rsi:.1f}"
                    )

                    positions.remove(ticker)

            except Exception as e:
                print(f"{ticker} error: {e}")
                continue

        save_json(POSITIONS_FILE, positions)

        results = buys + sells

        if results:

            message = (
                "📊 Nasdaq-100 Swing Scanner\n\n"
                + "\n\n".join(results[:10])
            )

        else:

            message = (
                "📊 انتهى الفحص.\n"
                "لم تظهر إشارات شراء أو بيع مطابقة للشروط."
            )

        send_message(chat_id, message)

        print(
            f"Scan completed: "
            f"Buys={len(buys)}, Sells={len(sells)}"
        )

    except Exception as e:

        print("Scanner error:", e)

        send_message(
            chat_id,
            "حدث خطأ أثناء فحص السوق. حاول لاحقاً."
        )


# =========================
# AUTOMATIC DAILY SCAN
# =========================

def scheduled_scan():

    chats = load_json(CHAT_FILE)

    print("Starting automatic Nasdaq scan...")

    if not chats:
        print("No registered Telegram users")
        return

    for chat_id in chats:

        threading.Thread(
            target=scan_market,
            args=(chat_id,),
            daemon=True
        ).start()


# =========================
# SCHEDULER
# =========================

scheduler = BackgroundScheduler(
    timezone="America/New_York"
)

scheduler.add_job(
    scheduled_scan,
    trigger="cron",
    day_of_week="mon-fri",
    hour=16,
    minute=30,
    id="daily_market_scan",
    replace_existing=True,
    max_instances=1,
    coalesce=True,
    misfire_grace_time=3600
)

scheduler.start()

print(
    "Daily Nasdaq scanner scheduled "
    "for 4:30 PM New York time"
)


# =========================
# WEB ROUTES
# =========================

@app.get("/")
def home():
    return "US Stocks Scanner is running!"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/webhook")
def webhook():

    update = request.get_json(silent=True) or {}

    message = update.get("message", {})

    if not message:
        return {"ok": True}

    chat_id = message.get(
        "chat", {}
    ).get("id")

    command = message.get(
        "text", ""
    ).strip()

    if not chat_id:
        return {"ok": True}

    if command.startswith("/start"):

        chats = load_json(CHAT_FILE)

        if chat_id not in chats:
            chats.append(chat_id)
            save_json(CHAT_FILE, chats)

        send_message(
            chat_id,
            "أهلاً بك في US Stocks Scanner 📈\n\n"
            "الأوامر:\n"
            "/scan - فحص أسهم Nasdaq-100\n"
            "/start - القائمة الرئيسية"
        )

    elif command.startswith("/scan"):

        send_message(
            chat_id,
            "🔎 بدأ فحص Nasdaq-100، انتظر النتائج..."
        )

        threading.Thread(
            target=scan_market,
            args=(chat_id,),
            daemon=True
        ).start()

    return {"ok": True}


# =========================
# TELEGRAM WEBHOOK
# =========================

def setup_webhook():

    url = os.environ.get("RENDER_EXTERNAL_URL")

    if TOKEN and url:

        try:
            response = requests.post(
                f"{API}/setWebhook",
                json={
                    "url": f"{url}/webhook"
                },
                timeout=15
            )

            print(
                "Webhook setup:",
                response.text
            )

        except Exception as error:
            print("Webhook error:", error)


setup_webhook()
