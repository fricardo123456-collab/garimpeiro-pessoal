import os
from urllib.parse import quote

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

meli_access_token = None
meli_refresh_token = None


def send_message(chat_id, text):
    requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
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
# CALLBACK / ACCESS TOKEN
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

    response = requests.post(
        MELI_TOKEN_URL,
        data=payload,
        timeout=20,
    )

    try:
        data = response.json()
    except Exception:
        return "Erro ao interpretar resposta do Mercado Livre.", 500

    if response.status_code != 200:
        return (
            "❌ Não foi possível gerar o Access Token.<br><br>"
            f"Status HTTP: {response.status_code}",
            400,
        )

    meli_access_token = data.get("access_token")
    meli_refresh_token = data.get("refresh_token")

    if not meli_access_token:
        return "❌ Access Token não recebido.", 400

    return (
        "✅ Mercado Livre conectado ao Garimpeiro Pessoal!<br><br>"
        "🔐 Access Token recebido e armazenado pelo servidor.<br>"
        "🤖 Agora você já pode testar /buscar no Telegram.",
        200,
    )


# =========================
# BUSCA MERCADO LIVRE
# =========================

def buscar_produto_meli(termo):
    if not meli_access_token:
        return {
            "ok": False,
            "erro": "Mercado Livre não autorizado."
        }

    url = "https://api.mercadolibre.com/products/search"

    headers = {
        "Authorization": f"Bearer {meli_access_token}"
    }

    params = {
        "status": "active",
        "site_id": "MLB",
        "q": termo,
        "limit": 5,
    }

    response = requests.get(
        url,
        headers=headers,
        params=params,
        timeout=20,
    )

    if response.status_code != 200:
        return {
            "ok": False,
            "status": response.status_code,
        }

    try:
        data = response.json()
    except Exception:
        return {
            "ok": False,
            "erro": "Resposta inválida da API."
        }

    results = data.get("results", [])

    return {
        "ok": True,
        "results": results,
    }


def formatar_resultados_produtos(termo, results):
    if not results:
        return (
            f"🔎 Busca: {termo}\n\n"
            "Nenhum produto encontrado nessa consulta."
        )

    linhas = [
        f"🔎 RESULTADOS PARA:\n{termo}",
        "",
    ]

    for i, item in enumerate(results[:5], start=1):
        nome = item.get("name") or item.get("title") or "Produto sem nome"
        product_id = item.get("id", "—")

        linhas.append(
            f"{i}. {nome}\n"
            f"ID: {product_id}"
        )
        linhas.append("")

    linhas.append(
        "✅ Consulta feita diretamente na API do Mercado Livre."
    )

    return "\n".join(linhas)


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

    if text.startswith("/start"):
        send_message(
            chat_id,
            "👋 Bem-vindo ao Garimpeiro Pessoal!\n\n"
            "🔎 Monitoramento inteligente de preços.\n\n"
            "Comandos:\n"
            "/status\n"
            "/buscar produto\n\n"
            "Exemplo:\n"
            "/buscar Mac Mini M4 16 512"
        )

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

    elif text.startswith("/buscar"):
        termo = text.replace("/buscar", "", 1).strip()

        if not termo:
            send_message(
                chat_id,
                "🔎 Digite o produto depois de /buscar.\n\n"
                "Exemplo:\n"
                "/buscar Mac Mini M4 16 512"
            )

        elif not meli_access_token:
            send_message(
                chat_id,
                "⚠️ Mercado Livre ainda não está autorizado."
            )

        else:
            send_message(
                chat_id,
                f"🔎 Procurando por:\n{termo}\n\n"
                "Aguarde..."
            )

            resultado = buscar_produto_meli(termo)

            if not resultado.get("ok"):
                status = resultado.get("status")

                send_message(
                    chat_id,
                    "❌ A consulta ao Mercado Livre não funcionou.\n\n"
                    f"Status HTTP: {status}\n\n"
                    "Isso é útil: agora sabemos exatamente como "
                    "a API está respondendo e ajustamos o endpoint "
                    "sem expor nenhuma credencial."
                )

            else:
                mensagem = formatar_resultados_produtos(
                    termo,
                    resultado.get("results", [])
                )

                send_message(
                    chat_id,
                    mensagem
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
