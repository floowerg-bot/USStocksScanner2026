import os
import json
import threading
import requests
import pandas as pd
import yfinance as yf

from io import StringIO
from flask import Flask, request

app = Flask(__name__)

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


def load_json(filename):
    try:
        with open(filename, "r") as f:
            return json.load(f)
    except Exception:
        return []


def save_json(filename, data):
    with open(filename, "w") as f:
        json.dump(data, f)


def send_message(chat_id, message):
    if not TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is missing")
        return

    response = requests.post(
        f"{API}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": message
        },
        timeout=20
    )

    response.raise_for_status()


# Get Nasdaq-100 constituents
def get_nasdaq_tickers():

    # Try Nasdaq API first
    try:
        url = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"

        response = requests.get(
            url,
            headers=HEADERS,
            timeout=20
        )

        response.raise_for_status()

        data = response.json()
        rows = data.get("data", {}).get("data", {}).get("rows", [])

        tickers = [
            row["symbol"].replace(".", "-")
            for row in rows
            if row.get("symbol")
        ]

        if len(tickers) >= 50:
            print(f"Loaded {len(tickers)} Nasdaq tickers from Nasdaq API")
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

        tables = pd.read_html(StringIO(response.text))

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
                        print(f"Loaded {len(tickers)} Nasdaq tickers from Wikipedia")
                        return tickers

    except Exception as e:
        print("Wikipedia error:", e)

    raise RuntimeError("Could not load Nasdaq-100 constituents")


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

                close = hist["Close"]
                volume = hist["Volume"]

                ema20 = close.ewm(span=20, adjust=False).mean()
                ema50 = close.ewm(span=50, adjust=False).mean()

                delta = close.diff()

                gain = delta.clip(lower=0).ewm(
                    alpha=1/14,
                    adjust=False
                ).mean()

                loss = (-delta.clip(upper=0)).ewm(
                    alpha=1/14,
                    adjust=False
                ).mean()

                rs = gain / loss.replace(0, float("nan"))

                rsi = 100 - (100 / (1 + rs))

                macd = (
                    close.ewm(span=12, adjust=False).mean()
                    -
                    close.ewm(span=26, adjust=False).mean()
                )

                signal = macd.ewm(
                    span=9,
                    adjust=False
                ).mean()

                price = float(close.iloc[-1])
                current_rsi = float(rsi.iloc[-1])

                average_volume = volume.rolling(20).mean().iloc[-1]

                if pd.isna(current_rsi) or pd.isna(average_volume):
                    continue

                buy = (
                    price > ema20.iloc[-1] > ema50.iloc[-1]
                    and 50 < current_rsi < 68
                    and macd.iloc[-1] > signal.iloc[-1]
                    and volume.iloc[-1] > average_volume
                )

                sell = (
                    price < ema20.iloc[-1]
                    or macd.iloc[-1] < signal.iloc[-1]
                    or current_rsi < 40
                )

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
            f"Scan completed. "
            f"Buys: {len(buys)}, Sells: {len(sells)}"
        )

    except Exception as e:

        print("Scanner error:", e)

        send_message(
            chat_id,
            "حدث خطأ أثناء فحص السوق. حاول لاحقاً."
        )


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

    chat_id = message.get("chat", {}).get("id")

    command = message.get("text", "").strip()

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


def setup_webhook():

    url = os.environ.get("RENDER_EXTERNAL_URL")

    if TOKEN and url:

        try:
            response = requests.post(
                f"{API}/setWebhook",
                json={"url": f"{url}/webhook"},
                timeout=15
            )

            print("Webhook setup:", response.text)

        except Exception as error:
            print("Webhook error:", error)


setup_webhook()
