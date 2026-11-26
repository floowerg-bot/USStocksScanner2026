import os
import requests
from flask import Flask, request

app = Flask(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
API = f"https://api.telegram.org/bot{TOKEN}"

@app.get("/")
def home():
    return "US Stocks Scanner is running!"

@app.post("/webhook")
def webhook():
    update = request.get_json(silent=True) or {}
    message = update.get("message", {})
    chat = message.get("chat", {})
    text = message.get("text", "")

    if chat and text.startswith("/start"):
        requests.post(
            f"{API}/sendMessage",
            json={
                "chat_id": chat["id"],
                "text": "Welcome to US Stocks Scanner! 📈\nBot is running successfully."
            },
            timeout=15
        )

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
