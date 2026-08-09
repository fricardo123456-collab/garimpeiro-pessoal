import os

from flask import Flask, request, redirect
import requests

app = Flask(__name__)

# =========================
# TELEGRAM
# =========================

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"

# =========================
# MERCADO LIVRE
# =========================

MELI_CLIENT_ID = os.environ.get("MELI_CLIENT_ID")
MELI_CLIENT_SECRET = os.environ.get("MELI_CLIENT_SECRET")

MELI_REDIRECT_URI = (
    "https://garimpeiro-pessoal.onrender.com/oauth/callback"
)

MELI_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"

# Por enquanto ficam somente na memória.
# Depois colocaremos persistência segura.
meli_access_token = None
meli_refresh_token = None


def send_message(chat_id, text):
    requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
        },
        timeout=15,
    )


@app.route("/")
def home():
    return "Garimpeiro Pessoal online! 🤖", 200


# =========================
# LOGIN MERCADO LIVRE
# =========================

@app.route("/oauth/login")
def oauth_login():

    authorization_url = (
        "https://auth.mercadolivre.com.br/authorization"
        "?response_type=code"
        f"&client_id={MELI_CLIENT_ID}"
        f"&redirect_uri={MELI_REDIRECT_URI}"
    )

    return redirect(authorization_url)


# =========================
# CALLBACK MERCADO LIVRE
# =========================

@app.route("/oauth/callback")
def oauth_callback():

    global meli_access_token
    global meli_refresh_token

    code = request.args.get("code")

    if not code:
        return "Código de autorização não recebido.", 400

    payload = {
        "grant_type": "authorization_code",
        "client_id": MELI_CLIENT_ID,
        "client_secret": MELI_CLIENT_SECRET,
        "code": code,
        "redirect_uri": MELI_REDIRECT_URI,
    }

    try:

        response = requests.post(
            MELI_TOKEN_URL,
            data=payload,
            timeout=20,
        )

        data = response.json()

    except Exception as exc:

        return (
            "❌ Erro ao comunicar com o Mercado Livre.<br><br>"
            f"{str(exc)}",
            500,
        )

    if response.status_code != 200:

        return (
            "❌ Não foi possível gerar o Access Token.<br><br>"
            f"Status: {response.status_code}<br>"
            f"Resposta: {data}",
            400,
        )

    meli_access_token = data.get("access_token")
    meli_refresh_token = data.get("refresh_token")

    if not meli_access_token:

        return "❌ Access Token não recebido.", 400

    return (
        "✅ Mercado Livre conectado ao Garimpeiro Pessoal!<br><br>"
        "🔐 Access Token recebido e armazenado pelo servidor.<br>"
        "🤖 Agora podemos iniciar os testes de consulta à API.",
        200,
    )


# =========================
# TELEGRAM WEBHOOK
# =========================

@app.route("/webhook", methods=["POST"])
def webhook():

    update = request.get_json(silent=True) or {}

    message = update.get("message", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = message.get("text", "")

    if not chat_id:
        return "OK", 200

    # /start
    if text.startswith("/start"):

        send_message(
            chat_id,
            "👋 Bem-vindo ao Garimpeiro Pessoal!\n\n"
            "🔎 Monitoramento inteligente de preços.\n\n"
            "Comandos:\n\n"
            "/status\n"
            "/buscar produto"
        )

    # /status
    elif text.startswith("/status"):

        if meli_access_token:

            status_ml = "✅ Mercado Livre conectado"

        else:

            status_ml = "🟡 Mercado Livre aguardando autorização"

        send_message(
            chat_id,
            "🤖 STATUS DO GARIMPEIRO\n\n"
            "✅ Telegram conectado\n"
            "✅ Render online\n"
            f"{status_ml}"
        )

    # /buscar
    elif text.startswith("/buscar"):

        produto = text.replace(
            "/buscar",
            "",
            1
        ).strip()

        if not produto:

            send_message(
                chat_id,
                "🔎 Digite o produto depois de /buscar.\n\n"
                "Exemplo:\n"
                "/buscar Mac Mini M4 16 512"
            )

        elif not meli_access_token:

            send_message(
                chat_id,
                "⚠️ Mercado Livre ainda não está autorizado.\n\n"
                "Finalize a conexão OAuth primeiro."
            )

        else:

            send_message(
                chat_id,
                f"🔎 Produto recebido:\n\n"
                f"{produto}\n\n"
                "✅ Mercado Livre conectado.\n"
                "Próxima etapa: ativar busca real de anúncios."
            )

    return "OK", 200


if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
