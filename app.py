import os

from flask import Flask, request, redirect
import requests

app = Flask(__name__)

# Telegram
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"

# Mercado Livre
MELI_CLIENT_ID = os.environ.get("MELI_CLIENT_ID")
MELI_CLIENT_SECRET = os.environ.get("MELI_CLIENT_SECRET")

MELI_REDIRECT_URI = "https://garimpeiro-pessoal.onrender.com/oauth/callback"


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


@app.route("/oauth/login")
def oauth_login():
    authorization_url = (
        "https://auth.mercadolivre.com.br/authorization"
        f"?response_type=code"
        f"&client_id={MELI_CLIENT_ID}"
        f"&redirect_uri={MELI_REDIRECT_URI}"
    )

    return redirect(authorization_url)


@app.route("/oauth/callback")
def oauth_callback():
    code = request.args.get("code")

    if not code:
        return "Código de autorização não recebido.", 400

    return (
        "✅ Mercado Livre autorizou o Garimpeiro Pessoal.<br><br>"
        "Código recebido com sucesso. Próxima etapa: gerar o access token.",
        200,
    )


@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}

    message = update.get("message", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = message.get("text", "")

    if not chat_id:
        return "OK", 200

    if text.startswith("/start"):
        send_message(
            chat_id,
            "👋 Bem-vindo ao Garimpeiro Pessoal!\n\n"
            "🔎 Vou ajudar você a monitorar produtos e encontrar "
            "oportunidades de preço.\n\n"
            "Comandos disponíveis:\n"
            "/buscar produto - pesquisar um produto\n"
            "/status - verificar o sistema"
        )

    elif text.startswith("/status"):
        send_message(
            chat_id,
            "✅ Garimpeiro Pessoal online.\n"
            "✅ Telegram conectado.\n"
            "✅ Servidor Render conectado.\n"
            "🟡 Mercado Livre aguardando autorização."
        )

    elif text.startswith("/buscar"):
        produto = text.replace("/buscar", "", 1).strip()

        if not produto:
            send_message(
                chat_id,
                "🔎 Digite o produto depois de /buscar.\n\n"
                "Exemplo:\n"
                "/buscar Mac Mini M4 16 512"
            )
        else:
            send_message(
                chat_id,
                f"🔎 Entendi. Você quer procurar:\n\n"
                f"{produto}\n\n"
                "🟡 A pesquisa automática do Mercado Livre "
                "será ativada assim que concluirmos a autorização da API."
            )

    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
