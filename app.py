import os

from flask import Flask, request
import requests

app = Flask(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"


def send_message(chat_id, text):
    requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
        },
        timeout=10,
    )


@app.route("/")
def home():
    return "Garimpeiro Pessoal online! 🤖", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}

    message = update.get("message", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = message.get("text", "")

    if chat_id and text.startswith("/start"):
        send_message(
            chat_id,
            "👋 Bem-vindo ao Garimpeiro Pessoal!\n\n"
            "🔎 Vou ajudar você a monitorar produtos e encontrar "
            "oportunidades de preço.\n\n"
            "🚧 Sistema em configuração."
        )

    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
