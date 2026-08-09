import os

import requests
from flask import Flask, request

app = Flask(__name__)

# ============================================================
# CONFIGURAÇÕES
# ============================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ML_ACCESS_TOKEN = os.environ.get("ML_ACCESS_TOKEN")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
ML_API = "https://api.mercadolibre.com"

# Produto que já encontramos anteriormente
TEST_PRODUCT_ID = "MLB74895216"


# ============================================================
# TELEGRAM
# ============================================================

def send_message(chat_id, text):
    try:
        response = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )

        return response

    except Exception as e:
        print("Erro ao enviar mensagem Telegram:", e)
        return None


# ============================================================
# MERCADO LIVRE
# ============================================================

def ml_headers():
    return {
        "Authorization": f"Bearer {ML_ACCESS_TOKEN}"
    }


def ml_get(endpoint, params=None):
    try:
        response = requests.get(
            f"{ML_API}{endpoint}",
            headers=ml_headers(),
            params=params,
            timeout=20,
        )

        return response

    except requests.RequestException as e:
        print("Erro de conexão com Mercado Livre:", e)
        return None


# ============================================================
# FORMATAÇÃO
# ============================================================

def money(value):
    try:
        value = float(value)

        formatted = f"{value:,.2f}"
        formatted = formatted.replace(",", "X")
        formatted = formatted.replace(".", ",")
        formatted = formatted.replace("X", ".")

        return f"R$ {formatted}"

    except Exception:
        return str(value)


# ============================================================
# COMANDOS
# ============================================================

def command_start(chat_id):
    send_message(
        chat_id,
        "👋 Bem-vindo ao Garimpeiro Pessoal!\n\n"
        "🔎 Monitoramento de produtos e oportunidades de preço.\n\n"
        "Comandos disponíveis:\n"
        "/status\n"
        "/teste\n"
        "/ofertas"
    )


def command_status(chat_id):
    telegram_status = "✅ configurado" if TELEGRAM_TOKEN else "❌ ausente"
    ml_status = "✅ configurado" if ML_ACCESS_TOKEN else "❌ ausente"

    send_message(
        chat_id,
        "🤖 STATUS DO GARIMPEIRO\n\n"
        f"Telegram: {telegram_status}\n"
        "Render: ✅ online\n"
        f"Mercado Livre token: {ml_status}"
    )


def command_teste(chat_id):
    if not ML_ACCESS_TOKEN:
        send_message(
            chat_id,
            "❌ ML_ACCESS_TOKEN não está configurado."
        )
        return

    send_message(
        chat_id,
        "🧪 Testando autenticação no Mercado Livre..."
    )

    response = ml_get("/users/me")

    if response is None:
        send_message(
            chat_id,
            "❌ Não consegui conectar à API do Mercado Livre."
        )
        return

    try:
        data = response.json()
    except Exception:
        data = {}

    if response.status_code == 200:
        send_message(
            chat_id,
            "✅ AUTENTICAÇÃO OK\n\n"
            f"HTTP: {response.status_code}\n"
            f"User ID: {data.get('id', '-')}\n"
            f"Nickname: {data.get('nickname', '-')}\n"
            f"Site: {data.get('site_id', '-')}"
        )

    else:
        send_message(
            chat_id,
            "❌ ERRO DE AUTENTICAÇÃO\n\n"
            f"HTTP: {response.status_code}\n"
            f"Resposta: {str(data)[:1000]}"
        )


def command_ofertas(chat_id):
    if not ML_ACCESS_TOKEN:
        send_message(
            chat_id,
            "❌ ML_ACCESS_TOKEN não está configurado."
        )
        return

    send_message(
        chat_id,
        "🔬 TESTANDO ROTA DE OFERTAS\n\n"
        f"Produto: {TEST_PRODUCT_ID}\n"
        f"GET /products/{TEST_PRODUCT_ID}/items\n\n"
        "Aguarde..."
    )

    response = ml_get(
        f"/products/{TEST_PRODUCT_ID}/items"
    )

    if response is None:
        send_message(
            chat_id,
            "❌ Não consegui conectar à API."
        )
        return

    try:
        data = response.json()
    except Exception:
        data = {}

    # --------------------------------------------------------
    # ERRO
    # --------------------------------------------------------

    if response.status_code != 200:
        error = data.get("error", "-") if isinstance(data, dict) else "-"
        message = data.get("message", "-") if isinstance(data, dict) else "-"

        send_message(
            chat_id,
            "❌ ROTA DE OFERTAS RECUSADA\n\n"
            f"HTTP: {response.status_code}\n"
            f"error: {error}\n"
            f"message: {message}\n\n"
            f"Resposta:\n{str(data)[:1500]}"
        )

        return

    # --------------------------------------------------------
    # IDENTIFICAR LISTA DE RESULTADOS
    # --------------------------------------------------------

    results = []

    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            results = data["results"]

        elif isinstance(data.get("items"), list):
            results = data["items"]

    elif isinstance(data, list):
        results = data

    # --------------------------------------------------------
    # CABEÇALHO
    # --------------------------------------------------------

    total_api = None

    if isinstance(data, dict):
        paging = data.get("paging", {})

        if isinstance(paging, dict):
            total_api = paging.get("total")

    send_message(
        chat_id,
        "✅ ROTA RESPONDEU HTTP 200!\n\n"
        f"Produto: {TEST_PRODUCT_ID}\n"
        f"Ofertas recebidas nesta resposta: {len(results)}\n"
        f"Total informado pela API: {total_api if total_api is not None else '-'}"
    )

    # --------------------------------------------------------
    # NENHUMA OFERTA
    # --------------------------------------------------------

    if not results:
        send_message(
            chat_id,
            "⚠️ A rota respondeu 200, mas não encontrei uma "
            "lista de ofertas no formato esperado.\n\n"
            "Estrutura recebida:\n"
            f"{str(data)[:3000]}"
        )

        return

    # --------------------------------------------------------
    # MOSTRAR PRIMEIRAS OFERTAS
    # --------------------------------------------------------

    output = "💰 OFERTAS RECEBIDAS\n\n"

    for index, item in enumerate(results[:10], start=1):

        if not isinstance(item, dict):
            continue

        item_id = (
            item.get("item_id")
            or item.get("id")
            or "-"
        )

        seller_id = (
            item.get("seller_id")
            or item.get("seller", {}).get("id")
            if isinstance(item.get("seller"), dict)
            else item.get("seller_id")
        )

        price = item.get("price")

        currency = (
            item.get("currency_id")
            or item.get("currency")
            or "BRL"
        )

        listing_type = (
            item.get("listing_type_id")
            or item.get("listing_type")
            or "-"
        )

        free_shipping = None

        shipping = item.get("shipping")

        if isinstance(shipping, dict):
            free_shipping = shipping.get("free_shipping")

        output += f"{index}. Oferta\n"
        output += f"ID: {item_id}\n"

        if price is not None:
            output += f"💰 Preço: {money(price)}\n"
        else:
            output += "💰 Preço: não informado\n"

        output += f"Moeda: {currency}\n"
        output += f"Vendedor: {seller_id or '-'}\n"
        output += f"Tipo: {listing_type}\n"

        if free_shipping is True:
            output += "🚚 Frete grátis: SIM\n"

        elif free_shipping is False:
            output += "🚚 Frete grátis: NÃO\n"

        output += "\n"

    send_message(
        chat_id,
        output[:4000]
    )


# ============================================================
# WEBHOOK
# ============================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}

    message = update.get("message", {})
    chat = message.get("chat", {})

    chat_id = chat.get("id")
    text = message.get("text", "")

    if not chat_id or not text:
        return "OK", 200

    text = text.strip()

    if text.startswith("/start"):
        command_start(chat_id)

    elif text.startswith("/status"):
        command_status(chat_id)

    elif text.startswith("/teste"):
        command_teste(chat_id)

    elif text.startswith("/ofertas"):
        command_ofertas(chat_id)

    return "OK", 200


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():
    return "Garimpeiro Pessoal online! 🤖", 200


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port
    )
