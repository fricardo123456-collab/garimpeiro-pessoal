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
# OAUTH MERCADO LIVRE
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
        "🤖 Agora você pode usar /buscar e /detalhe.",
        200,
    )


# =========================
# API AUXILIAR
# =========================

def meli_get(url, params=None):
    if not meli_access_token:
        return {
            "ok": False,
            "status": None,
            "data": {},
            "erro": "Mercado Livre não autorizado.",
        }

    headers = {
        "Authorization": f"Bearer {meli_access_token}",
        "Accept": "application/json",
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
            "data": {},
            "erro": str(exc),
        }

    try:
        data = response.json()
    except Exception:
        data = {
            "raw_response": response.text[:1500]
        }

    return {
        "ok": response.status_code == 200,
        "status": response.status_code,
        "data": data,
    }


# =========================
# TESTE TOKEN
# =========================

def testar_usuario_meli():
    return meli_get(
        "https://api.mercadolibre.com/users/me"
    )


# =========================
# BUSCA DE PRODUTOS
# =========================

def buscar_produto_meli(termo):
    return meli_get(
        "https://api.mercadolibre.com/products/search",
        params={
            "status": "active",
            "site_id": "MLB",
            "q": termo,
            "limit": 5,
        },
    )


# =========================
# DETALHE DO PRODUTO
# =========================

def detalhe_produto_meli(product_id):
    return meli_get(
        f"https://api.mercadolibre.com/products/{product_id}"
    )


def formatar_erro(status, data):
    message = ""

    if isinstance(data, dict):
        message = data.get("message", "")
        error = data.get("error", "")

        if error:
            message += f"\nerror: {error}"

    return (
        "❌ ERRO NA API DO MERCADO LIVRE\n\n"
        f"HTTP: {status}\n"
        f"{message}"
    )


def formatar_detalhe(product_id, data):
    nome = (
        data.get("name")
        or data.get("title")
        or "Produto"
    )

    linhas = [
        "📦 DETALHES DO PRODUTO",
        "",
        f"Produto: {nome}",
        f"ID: {product_id}",
    ]

    status = data.get("status")
    if status:
        linhas.append(f"Status: {status}")

    catalog_product_id = data.get("catalog_product_id")
    if catalog_product_id:
        linhas.append(
            f"Catalog ID: {catalog_product_id}"
        )

    buy_box = data.get("buy_box_winner")

    if isinstance(buy_box, dict):
        linhas.append("")
        linhas.append("🏆 OFERTA PRINCIPAL ENCONTRADA")

        price = buy_box.get("price")
        currency = buy_box.get("currency_id")
        item_id = buy_box.get("item_id")
        condition = buy_box.get("condition")

        if price is not None:
            linhas.append(
                f"Preço: {currency or 'R$'} {price}"
            )

        if condition:
            linhas.append(
                f"Condição: {condition}"
            )

        if item_id:
            linhas.append(
                f"Item ID: {item_id}"
            )
            linhas.append(
                f"Link: https://produto.mercadolivre.com.br/{item_id}"
            )

        seller = buy_box.get("seller")

        if isinstance(seller, dict):
            seller_id = seller.get("id")

            if seller_id:
                linhas.append(
                    f"Seller ID: {seller_id}"
                )

    else:
        linhas.append("")
        linhas.append(
            "ℹ️ Este retorno não trouxe buy_box_winner."
        )

    return "\n".join(linhas)


# =========================
# TELEGRAM
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
            "Comandos:\n"
            "/status\n"
            "/teste\n"
            "/buscar produto\n"
            "/detalhe PRODUCT_ID\n\n"
            "Exemplo:\n"
            "/buscar Mac Mini M4 16 512\n"
            "/detalhe MLB74895216"
        )

    elif text.startswith("/status"):
        status_ml = (
            "✅ Mercado Livre conectado"
            if meli_access_token
            else "🟡 Mercado Livre aguardando autorização"
        )

        send_message(
            chat_id,
            "🤖 STATUS DO GARIMPEIRO\n\n"
            "✅ Telegram conectado\n"
            "✅ Render online\n"
            f"{status_ml}"
        )

    elif text.startswith("/teste"):
        resultado = testar_usuario_meli()

        if resultado.get("ok"):
            data = resultado.get("data", {})

            send_message(
                chat_id,
                "✅ TESTE DE AUTENTICAÇÃO PASSOU!\n\n"
                f"HTTP: {resultado.get('status')}\n"
                f"User ID: {data.get('id')}\n"
                f"Nickname: {data.get('nickname')}\n"
                f"Site: {data.get('site_id')}"
            )
        else:
            send_message(
                chat_id,
                formatar_erro(
                    resultado.get("status"),
                    resultado.get("data", {}),
                )
            )

    elif text.startswith("/buscar"):
        termo = text.replace(
            "/buscar",
            "",
            1
        ).strip()

        if not termo:
            send_message(
                chat_id,
                "Use:\n/buscar Mac Mini M4 16 512"
            )
            return "OK", 200

        send_message(
            chat_id,
            f"🔎 Buscando:\n{termo}"
        )

        resultado = buscar_produto_meli(termo)

        if not resultado.get("ok"):
            send_message(
                chat_id,
                formatar_erro(
                    resultado.get("status"),
                    resultado.get("data", {}),
                )
            )
            return "OK", 200

        results = resultado.get(
            "data",
            {}
        ).get(
            "results",
            []
        )

        if not results:
            send_message(
                chat_id,
                "Nenhum produto encontrado."
            )
            return "OK", 200

        linhas = [
            "✅ PRODUTOS ENCONTRADOS",
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

            product_id = item.get(
                "id",
                "—"
            )

            linhas.append(
                f"{i}. {nome}\n"
                f"ID: {product_id}\n"
                f"/detalhe {product_id}\n"
            )

        send_message(
            chat_id,
            "\n".join(linhas)
        )

    elif text.startswith("/detalhe"):
        product_id = text.replace(
            "/detalhe",
            "",
            1
        ).strip()

        if not product_id:
            send_message(
                chat_id,
                "Use:\n/detalhe MLB74895216"
            )
            return "OK", 200

        send_message(
            chat_id,
            f"📦 Consultando detalhes de:\n{product_id}"
        )

        resultado = detalhe_produto_meli(
            product_id
        )

        if not resultado.get("ok"):
            send_message(
                chat_id,
                formatar_erro(
                    resultado.get("status"),
                    resultado.get("data", {}),
                )
            )

        else:
            send_message(
                chat_id,
                formatar_detalhe(
                    product_id,
                    resultado.get("data", {}),
                )
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
