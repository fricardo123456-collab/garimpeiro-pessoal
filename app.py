import os
import json

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

# Temporário: fica na memória do servidor.
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

    try:
        response = requests.post(
            MELI_TOKEN_URL,
            data=payload,
            timeout=20,
        )

        data = response.json()

    except Exception:
        return "Erro ao comunicar com o Mercado Livre.", 500

    if response.status_code != 200:
        return (
            "❌ Não foi possível gerar o Access Token.<br>"
            f"HTTP {response.status_code}",
            400,
        )

    meli_access_token = data.get("access_token")
    meli_refresh_token = data.get("refresh_token")

    if not meli_access_token:
        return "❌ Access Token não recebido.", 400

    return (
        "✅ Mercado Livre conectado ao Garimpeiro Pessoal!<br><br>"
        "🔐 Access Token recebido pelo servidor.<br>"
        "🤖 Agora teste /teste no Telegram.",
        200,
    )


# =========================
# TESTE DO TOKEN
# =========================

def testar_usuario_meli():
    if not meli_access_token:
        return {
            "ok": False,
            "status": None,
            "erro": "Mercado Livre não autorizado.",
        }

    url = "https://api.mercadolibre.com/users/me"

    headers = {
        "Authorization": f"Bearer {meli_access_token}",
        "Accept": "application/json",
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=20,
        )
    except requests.RequestException as exc:
        return {
            "ok": False,
            "status": None,
            "erro": f"Falha de conexão: {str(exc)[:200]}",
        }

    try:
        data = response.json()
    except Exception:
        data = {
            "raw_response": response.text[:1000]
        }

    return {
        "ok": response.status_code == 200,
        "status": response.status_code,
        "data": data,
    }


# =========================
# BUSCA / DIAGNÓSTICO
# =========================

def buscar_produto_meli(termo):
    if not meli_access_token:
        return {
            "ok": False,
            "status": None,
            "erro": "Mercado Livre não autorizado.",
        }

    url = "https://api.mercadolibre.com/products/search"

    headers = {
        "Authorization": f"Bearer {meli_access_token}",
        "Accept": "application/json",
    }

    params = {
        "status": "active",
        "site_id": "MLB",
        "q": termo,
        "limit": 5,
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=20,
        )
    except requests.RequestException as exc:
        return {
            "ok": False,
            "status": None,
            "erro": f"Falha de conexão: {str(exc)[:200]}",
        }

    try:
        data = response.json()
    except Exception:
        data = {
            "raw_response": response.text[:1000]
        }

    return {
        "ok": response.status_code == 200,
        "status": response.status_code,
        "data": data,
    }


def formatar_erro_api(status, data):
    linhas = [
        "❌ MERCADO LIVRE RECUSOU A CONSULTA",
        "",
        f"HTTP: {status}",
    ]

    if isinstance(data, dict):
        error = data.get("error")
        message = data.get("message")
        cause = data.get("cause")

        if error:
            linhas.append(f"error: {error}")

        if message:
            linhas.append(f"message: {message}")

        if cause:
            texto_cause = json.dumps(
                cause,
                ensure_ascii=False
            )

            if len(texto_cause) > 1200:
                texto_cause = texto_cause[:1200] + "..."

            linhas.append(f"cause: {texto_cause}")

        if not error and not message and not cause:
            texto = json.dumps(
                data,
                ensure_ascii=False
            )

            if len(texto) > 1500:
                texto = texto[:1500] + "..."

            linhas.append(f"resposta: {texto}")

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

    # /start
    if text.startswith("/start"):
        send_message(
            chat_id,
            "👋 Bem-vindo ao Garimpeiro Pessoal!\n\n"
            "Comandos:\n"
            "/status\n"
            "/teste\n"
            "/buscar produto\n\n"
            "Exemplo:\n"
            "/buscar Mac Mini M4 16 512"
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

    # /teste
    elif text.startswith("/teste"):
        if not meli_access_token:
            send_message(
                chat_id,
                "⚠️ Mercado Livre ainda não está autorizado."
            )

        else:
            send_message(
                chat_id,
                "🧪 Testando Access Token no endpoint /users/me..."
            )

            resultado = testar_usuario_meli()

            status = resultado.get("status")
            data = resultado.get("data") or {}

            if resultado.get("ok"):
                user_id = data.get("id", "—")
                nickname = data.get("nickname", "—")
                site_id = data.get("site_id", "—")

                send_message(
                    chat_id,
                    "✅ TESTE DE AUTENTICAÇÃO PASSOU!\n\n"
                    f"HTTP: {status}\n"
                    f"User ID: {user_id}\n"
                    f"Nickname: {nickname}\n"
                    f"Site: {site_id}\n\n"
                    "✅ OAuth válido\n"
                    "✅ Access Token válido\n"
                    "✅ API do Mercado Livre acessível"
                )

            else:
                send_message(
                    chat_id,
                    formatar_erro_api(
                        status,
                        data
                    )
                )

    # /buscar
    elif text.startswith("/buscar"):
        termo = text.replace(
            "/buscar",
            "",
            1
        ).strip()

        if not termo:
            send_message(
                chat_id,
                "🔎 Digite um produto.\n\n"
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
                f"🔎 Testando busca para:\n{termo}"
            )

            resultado = buscar_produto_meli(termo)

            status = resultado.get("status")
            data = resultado.get("data") or {}

            if not resultado.get("ok"):
                send_message(
                    chat_id,
                    formatar_erro_api(
                        status,
                        data
                    )
                )

            else:
                results = data.get("results", [])

                if not results:
                    send_message(
                        chat_id,
                        "✅ API respondeu HTTP 200.\n\n"
                        "Nenhum produto foi retornado."
                    )

                else:
                    linhas = [
                        "✅ API respondeu HTTP 200!",
                        "",
                    ]

                    for i, item in enumerate(
                        results[:5],
                        start=1
                    ):
                        nome = (
                            item.get("name")
                            or item.get("title")
                            or "Produto"
                        )

                        product_id = item.get("id", "—")

                        linhas.append(
                            f"{i}. {nome}\n"
                            f"ID: {product_id}\n"
                        )

                    send_message(
                        chat_id,
                        "\n".join(linhas)
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
