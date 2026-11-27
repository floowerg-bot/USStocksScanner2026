import os
import json
import time
import threading
import logging

import requests
import pandas as pd
import yfinance as yf

from flask import Flask, request, jsonify

# ==========================================
# CONFIGURATION
# ==========================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

CHATS_FILE = "chats.json"
POSITIONS_FILE = "positions.json"

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SCAN_LOCK = threading.Lock()

# Strategy settings
EMA_FAST = 20
EMA_SLOW = 50
RSI_PERIOD = 14
ATR_PERIOD = 14

RESISTANCE_PERIOD = 20
VOLUME_PERIOD = 20

ATR_STOP_MULTIPLIER = 1.5
ATR_TARGET_MULTIPLIER = 3.0

MAX_ALERTS = 10


# ==========================================
# JSON STORAGE
# ==========================================

def load_json(filename):
    try:
        with open(filename, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json(filename, data):
    with open(filename, "w") as f:
        json.dump(data, f, indent=4)


# ==========================================
# TELEGRAM
# ==========================================

def send_message(chat_id, message):
    if not TELEGRAM_BOT_TOKEN:
        logger.error("Telegram token is missing")
        return

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    try:
        response = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML"
            },
            timeout=20
        )

        if not response.ok:
            logger.error("Telegram error: %s", response.text)

    except Exception as e:
        logger.error("Telegram send error: %s", e)


def broadcast(message):
    chats = load_json(CHATS_FILE)

    for chat_id in chats:
        send_message(chat_id, message)


# ==========================================
# NASDAQ 100 SYMBOLS
# ==========================================

def get_nasdaq_symbols():
    url = "https://en.wikipedia.org/wiki/Nasdaq-100"

    try:
        tables = pd.read_html(url)

        for table in tables:
            if "Ticker" in table.columns:
                symbols = table["Ticker"].astype(str).tolist()

                symbols = [
                    s.replace(".", "-")
                    for s in symbols
                    if s and s != "nan"
                ]

                if symbols:
                    logger.info(
                        "Loaded %s Nasdaq symbols",
                        len(symbols)
                    )
                    return symbols

    except Exception as e:
        logger.error("Nasdaq list error: %s", e)

    logger.warning("Using fallback Nasdaq list")

    return [
        "AAPL", "MSFT", "NVDA", "AMZN", "META",
        "GOOGL", "GOOG", "AVGO", "TSLA", "COST",
        "NFLX", "AMD", "ADBE", "PEP", "CSCO",
        "TMUS", "QCOM", "INTU", "AMGN", "TXN",
        "ISRG", "BKNG", "AMAT", "HON", "VRTX",
        "PANW", "ADP", "SBUX", "GILD", "ADI"
    ]


# ==========================================
# TECHNICAL INDICATORS
# ==========================================

def calculate_indicators(df):

    df = df.copy()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    required = ["Open", "High", "Low", "Close", "Volume"]

    if not all(col in df.columns for col in required):
        return None

    for col in required:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=required)

    if len(df) < 60:
        return None

    # EMA
    df["EMA20"] = df["Close"].ewm(
        span=EMA_FAST,
        adjust=False
    ).mean()

    df["EMA50"] = df["Close"].ewm(
        span=EMA_SLOW,
        adjust=False
    ).mean()

    # RSI
    delta = df["Close"].diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / RSI_PERIOD,
        adjust=False,
        min_periods=RSI_PERIOD
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / RSI_PERIOD,
        adjust=False,
        min_periods=RSI_PERIOD
    ).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))

    df["RSI"] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = df["Close"].ewm(
        span=12,
        adjust=False
    ).mean()

    ema26 = df["Close"].ewm(
        span=26,
        adjust=False
    ).mean()

    df["MACD"] = ema12 - ema26

    df["SIGNAL"] = df["MACD"].ewm(
        span=9,
        adjust=False
    ).mean()

    # ATR
    previous_close = df["Close"].shift(1)

    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - previous_close).abs(),
            (df["Low"] - previous_close).abs()
        ],
        axis=1
    ).max(axis=1)

    df["ATR"] = tr.ewm(
        alpha=1 / ATR_PERIOD,
        adjust=False,
        min_periods=ATR_PERIOD
    ).mean()

    # Resistance: highest high of previous 20 sessions
    # Excludes the current session
    df["RESISTANCE"] = (
        df["High"]
        .shift(1)
        .rolling(RESISTANCE_PERIOD)
        .max()
    )

    # Average volume: previous 20 sessions
    df["AVG_VOLUME"] = (
        df["Volume"]
        .shift(1)
        .rolling(VOLUME_PERIOD)
        .mean()
    )

    return df


# ==========================================
# STOCK ANALYSIS
# ==========================================

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

        if df.empty:
            return None

        df = calculate_indicators(df)

        if df is None:
            return None

        row = df.iloc[-1]

        price = float(row["Close"])
        ema20 = float(row["EMA20"])
        ema50 = float(row["EMA50"])
        rsi = float(row["RSI"])
        macd = float(row["MACD"])
        signal = float(row["SIGNAL"])
        atr = float(row["ATR"])
        resistance = float(row["RESISTANCE"])
        volume = float(row["Volume"])
        avg_volume = float(row["AVG_VOLUME"])

        values = [
            price, ema20, ema50, rsi,
            macd, signal, atr, resistance,
            volume, avg_volume
        ]

        if not all(pd.notna(v) for v in values):
            return None

        if price <= 0 or atr <= 0 or resistance <= 0:
            return None

        trend = price > ema20 > ema50
        momentum = macd > signal
        rsi_good = 50 <= rsi <= 68
        volume_confirmed = volume > avg_volume

        # Confirmed resistance breakout
        breakout = price > resistance * 1.001

        # Strong BUY: all conditions must be met
        strong_buy = (
            trend
            and momentum
            and rsi_good
            and volume_confirmed
            and breakout
        )

        # Medium opportunity: approaching resistance
        near_resistance = (
            resistance * 0.98 <= price <= resistance * 1.001
        )

        medium_buy = (
            trend
            and momentum
            and 48 <= rsi <= 70
            and near_resistance
            and volume >= avg_volume * 0.80
            and not strong_buy
        )

        # Dynamic risk management using ATR
        stop_loss = price - (
            ATR_STOP_MULTIPLIER * atr
        )

        take_profit = price + (
            ATR_TARGET_MULTIPLIER * atr
        )

        return {
            "symbol": symbol,
            "price": price,
            "ema20": ema20,
            "ema50": ema50,
            "rsi": rsi,
            "macd": macd,
            "signal": signal,
            "atr": atr,
            "resistance": resistance,
            "volume": volume,
            "avg_volume": avg_volume,
            "breakout": breakout,
            "strong_buy": strong_buy,
            "medium_buy": medium_buy,
            "stop_loss": stop_loss,
            "take_profit": take_profit
        }

    except Exception as e:
        logger.warning(
            "Analysis failed for %s: %s",
            symbol, e
        )
        return None


# ==========================================
# BUY ALERT
# ==========================================

def create_buy_message(data):

    symbol = data["symbol"]

    return (
        f"🟢 <b>STRONG BUY - {symbol}</b>\n\n"
        f"💰 Entry: ${data['price']:.2f}\n"
        f"🎯 Target: ${data['take_profit']:.2f}\n"
        f"🛑 Stop Loss: ${data['stop_loss']:.2f}\n\n"
        f"📊 RSI: {data['rsi']:.1f}\n"
        f"📈 EMA20: ${data['ema20']:.2f}\n"
        f"📈 EMA50: ${data['ema50']:.2f}\n"
        f"📉 MACD: Bullish\n"
        f"📦 Volume: Confirmed\n\n"
        f"🚀 Resistance Breakout Confirmed\n"
        f"📐 ATR: ${data['atr']:.2f}\n"
        f"⚖️ Risk/Reward: 1:2\n\n"
        f"⏱ Strategy: Swing Trading\n"
        f"📅 Timeframe: Daily"
    )


# ==========================================
# MEDIUM OPPORTUNITY ALERT
# ==========================================

def create_medium_message(data):

    return (
        f"🟡 <b>MEDIUM OPPORTUNITY - "
        f"{data['symbol']}</b>\n\n"
        f"💰 Current Price: ${data['price']:.2f}\n"
        f"🎯 Resistance: ${data['resistance']:.2f}\n"
        f"📊 RSI: {data['rsi']:.1f}\n"
        f"📈 Trend: Bullish\n"
        f"📉 MACD: Bullish\n\n"
        f"⚠️ Waiting for resistance breakout.\n"
        f"👀 Add to watchlist.\n"
        f"❌ No BUY confirmation yet."
    )


# ==========================================
# SELL MANAGEMENT
# ==========================================

def check_open_positions(data, positions):

    symbol = data["symbol"]

    if symbol not in positions:
        return False

    position = positions[symbol]

    if not isinstance(position, dict):
        return False

    entry = float(position.get("entry", 0))
    stop = float(position.get("stop_loss", 0))
    target = float(position.get("take_profit", 0))

    price = data["price"]

    reason = None

    if stop and price <= stop:
        reason = "Stop Loss reached"

    elif target and price >= target:
        reason = "Take Profit reached"

    elif price < data["ema20"]:
        reason = "Price below EMA20"

    elif data["macd"] < data["signal"]:
        reason = "MACD bearish crossover"

    elif data["rsi"] < 40:
        reason = "RSI below 40"

    if reason:

        profit_pct = (
            ((price - entry) / entry) * 100
            if entry > 0 else 0
        )

        message = (
            f"🔴 <b>SELL ALERT - {symbol}</b>\n\n"
            f"💰 Entry: ${entry:.2f}\n"
            f"📉 Current Price: ${price:.2f}\n"
            f"📊 Return: {profit_pct:+.2f}%\n\n"
            f"⚠️ Reason: {reason}"
        )

        broadcast(message)

        del positions[symbol]
        save_json(POSITIONS_FILE, positions)

        return True

    return False


# ==========================================
# MARKET SCANNER
# ==========================================

def scan_market():

    if not SCAN_LOCK.acquire(blocking=False):
        return "A scan is already running."

    try:

        logger.info("Starting Nasdaq-100 scan")

        symbols = get_nasdaq_symbols()

        positions = load_json(POSITIONS_FILE)

        strong_signals = []
        medium_signals = []

        scanned = 0

        for symbol in symbols:

            data = analyze_stock(symbol)

            if data is None:
                continue

            scanned += 1

            # Manage existing positions first
            if check_open_positions(data, positions):
                continue

            # Do not create duplicate positions
            if symbol in positions:
                continue

            if data["strong_buy"]:
                strong_signals.append(data)

            elif data["medium_buy"]:
                medium_signals.append(data)

            # Small delay between downloads
            time.sleep(0.15)

        # Send strong BUY signals
        for data in strong_signals[:MAX_ALERTS]:

            symbol = data["symbol"]

            positions[symbol] = {
                "entry": data["price"],
                "stop_loss": data["stop_loss"],
                "take_profit": data["take_profit"],
                "date": pd.Timestamp.now().isoformat()
            }

            save_json(POSITIONS_FILE, positions)

            broadcast(create_buy_message(data))

        # Send medium opportunities
        remaining = max(
            0,
            MAX_ALERTS - len(strong_signals[:MAX_ALERTS])
        )

        for data in medium_signals[:remaining]:
            broadcast(create_medium_message(data))

        # Summary
        summary = (
            "📊 <b>Nasdaq-100 Scan Completed</b>\n\n"
            f"🔍 Stocks analyzed: {scanned}\n"
            f"🟢 Strong BUY: {len(strong_signals)}\n"
            f"🟡 Medium: {len(medium_signals)}\n"
            f"🔴 Existing positions: {len(positions)}\n\n"
        )

        if not strong_signals and not medium_signals:
            summary += (
                "No BUY or MEDIUM opportunities "
                "matched the strategy."
            )

        elif strong_signals:
            summary += "Strong BUY opportunities detected."

        else:
            summary += (
                "No confirmed BUY signals. "
                "Medium opportunities are being monitored."
            )

        broadcast(summary)

        logger.info("Scan completed")

        return summary

    except Exception as e:

        logger.exception("Market scan failed")

        broadcast(
            "⚠️ Error occurred during market scan. "
            "Please try again later."
        )

        return str(e)

    finally:
        SCAN_LOCK.release()


# ==========================================
# TELEGRAM COMMANDS
# ==========================================

def process_update(update):

    message = update.get("message", {})

    if not message:
        return

    chat_id = str(message["chat"]["id"])

    text = message.get("text", "").strip()

    chats = load_json(CHATS_FILE)

    if text.startswith("/start"):

        chats[chat_id] = True
        save_json(CHATS_FILE, chats)

        send_message(
            chat_id,
            "👋 Welcome to Nasdaq-100 Swing Scanner V2!\n\n"
            "📊 Market: Nasdaq-100\n"
            "⏱ Strategy: Swing Trading\n"
            "🟢 Strong BUY alerts\n"
            "🟡 Medium opportunities\n"
            "🔴 SELL alerts\n\n"
            "Commands:\n"
            "/scan - Start market scan\n"
            "/status - Bot status"
        )

    elif text.startswith("/scan"):

        if chat_id not in chats:
            chats[chat_id] = True
            save_json(CHATS_FILE, chats)

        send_message(
            chat_id,
            "🔎 Starting Nasdaq-100 scan...\n"
            "Please wait for the results."
        )

        thread = threading.Thread(
            target=scan_market,
            daemon=True
        )

        thread.start()

    elif text.startswith("/status"):

        send_message(
            chat_id,
            "🟢 Bot is running.\n"
            "📊 Nasdaq-100\n"
            "⏱ Swing Trading\n"
            "📅 Daily timeframe\n"
            "Version: V2"
        )


# ==========================================
# FLASK WEBHOOK
# ==========================================

@app.route("/", methods=["GET"])
def home():

    return "Nasdaq-100 Swing Scanner V2 is running."


@app.route("/health", methods=["GET"])
def health():

    return jsonify({
        "status": "ok",
        "version": "V2"
    })


@app.route("/webhook", methods=["POST"])
def webhook():

    try:

        update = request.get_json(silent=True)

        if update:
            process_update(update)

        return jsonify({"ok": True})

    except Exception as e:

        logger.exception("Webhook error")

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


# ==========================================
# TELEGRAM WEBHOOK SETUP
# ==========================================

def setup_webhook():

    if not TELEGRAM_BOT_TOKEN:
        logger.warning("Telegram token missing")
        return

    if not RENDER_EXTERNAL_URL:
        logger.warning("Render URL missing")
        return

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/setWebhook"
    )

    webhook_url = f"{RENDER_EXTERNAL_URL}/webhook"

    try:

        response = requests.post(
            url,
            json={"url": webhook_url},
            timeout=20
        )

        logger.info(
            "Webhook setup response: %s",
            response.text
        )

    except Exception as e:

        logger.error("Webhook setup failed: %s", e)


# ==========================================
# STARTUP
# ==========================================

setup_webhook()


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )
