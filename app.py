import os
import json
from decimal import Decimal, InvalidOperation

from flask import Flask, request, redirect
import requests

app = Flask(__name__)

# =========================================================
# CONFIGURAÇÃO
# =========================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

MELI_CLIENT_ID = os.environ.get("MELI_CLIENT_ID")
MELI_CLIENT_SECRET = os.environ.get("MELI_CLIENT_SECRET")

MELI_REDIRECT_URI = (
    "https://garimpeiro-pessoal.onrender.com/oauth/callback"
)

MELI_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
MELI_API = "https://api.mercadolibre.com"

meli_access_token = None
meli_refresh_token = None


# =========================================================
# UTILIDADES
# =========================================================

def brl(valor):
    try:
        valor = Decimal(str(valor))
        texto = f"{valor:,.2f}"
        texto = texto.replace(",", "X").replace(".", ",").replace("X", ".")
        return f"R$ {texto}"
    except (InvalidOperation, TypeError, ValueError):
        return "—"


def percentual_desconto(preco, original):
    try:
        preco = Decimal(str(preco))
        original = Decimal(str(original))

        if original <= 0 or preco >= original:
            return None

        desconto = ((original - preco) / original) * 100
        return round(float(desconto), 1)

    except Exception:
        return None


def send_message(chat_id, text):
    requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )


def meli_get(endpoint, params=None):
    if not meli_access_token:
        return {
            "ok": False,
            "status": None,
            "data": {},
            "erro": "Mercado Livre não autorizado."
        }

    try:
        response = requests.get(
            f"{MELI_API}{endpoint}",
            headers={
                "Authorization": f"Bearer {meli_access_token}",
                "Accept": "application/json",
            },
            params=params,
            timeout=25,
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:1500]}

        return {
            "ok": response.status_code == 200,
            "status": response.status_code,
            "data": data,
        }

    except requests.RequestException as exc:
        return {
            "ok": False,
            "status": None,
            "data": {},
            "erro": str(exc),
        }


# =========================================================
# OAUTH
# =========================================================

@app.route("/")
def home():
    return "Garimpeiro Pessoal online! 🤖", 200


@app.route("/oauth/login")
def oauth_login():
    url = (
        "https://auth.mercadolivre.com.br/authorization"
        "?response_type=code"
        f"&client_id={MELI_CLIENT_ID}"
        f"&redirect_uri={MELI_REDIRECT_URI}"
    )

    return redirect(url)


@app.route("/oauth/callback")
def oauth_callback():
    global meli_access_token
    global meli_refresh_token

    code = request.args.get("code")

    if not code:
        return "Código não recebido.", 400

    response = requests.post(
        MELI_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": MELI_CLIENT_ID,
            "client_secret": MELI_CLIENT_SECRET,
            "code": code,
            "redirect_uri": MELI_REDIRECT_URI,
        },
        timeout=25,
    )

    try:
        data = response.json()
    except Exception:
        return "Resposta OAuth inválida.", 500

    if response.status_code != 200:
        return f"Erro OAuth HTTP {response.status_code}", 400

    meli_access_token = data.get("access_token")
    meli_refresh_token = data.get("refresh_token")

    return (
        "✅ Mercado Livre conectado.<br><br>"
        "Agora volte ao Telegram.",
        200,
    )


# =========================================================
# CATÁLOGO
# =========================================================

def buscar_catalogo(termo, limit=10):
    return meli_get(
        "/products/search",
        params={
            "status": "active",
            "site_id": "MLB",
            "q": termo,
            "limit": limit,
        },
    )


def detalhe_catalogo(product_id):
    return meli_get(
        f"/products/{product_id}"
    )


# =========================================================
# ITENS / ANÚNCIOS
# =========================================================

def obter_item(item_id):
    return meli_get(
        f"/items/{item_id}"
    )


def obter_itens_multiget(item_ids):
    if not item_ids:
        return {
            "ok": True,
            "status": 200,
            "data": [],
        }

    ids = ",".join(item_ids[:20])

    return meli_get(
        "/items",
        params={
            "ids": ids,
            "attributes": (
                "id,title,price,base_price,original_price,"
                "currency_id,condition,available_quantity,"
                "seller_id,permalink,shipping,installments,"
                "catalog_product_id,official_store_id"
            ),
        },
    )


# =========================================================
# NORMALIZAÇÃO DE OFERTA
# =========================================================

def normalizar_item(item):
    if "body" in item:
        item = item.get("body") or {}

    installments = item.get("installments") or {}
    shipping = item.get("shipping") or {}

    preco = item.get("price")
    original = item.get("original_price")

    parcelas = installments.get("quantity")
    valor_parcela = installments.get("amount")

    total_parcelado = None

    try:
        if parcelas and valor_parcela:
            total_parcelado = (
                Decimal(str(parcelas))
                * Decimal(str(valor_parcela))
            )
    except Exception:
        total_parcelado = None

    return {
        "item_id": item.get("id"),
        "titulo": item.get("title") or "Produto",
        "preco": preco,
        "preco_original": original,
        "desconto": percentual_desconto(preco, original),

        "parcelas": parcelas,
        "valor_parcela": valor_parcela,
        "total_parcelado": total_parcelado,

        "sem_juros": installments.get("rate") in (0, 0.0),

        "frete_gratis": shipping.get("free_shipping", False),
        "condicao": item.get("condition"),
        "seller_id": item.get("seller_id"),
        "official_store_id": item.get("official_store_id"),

        "catalog_product_id": item.get("catalog_product_id"),
        "link": item.get("permalink"),
        "estoque": item.get("available_quantity"),
    }


# =========================================================
# RANKING
# =========================================================

def score_oferta(oferta):
    """
    Quanto menor, melhor.
    Prioriza preço à vista.
    """

    preco = oferta.get("preco")

    try:
        preco = Decimal(str(preco))
    except Exception:
        return Decimal("999999999")

    score = preco

    if oferta.get("frete_gratis"):
        score -= Decimal("10")

    if oferta.get("sem_juros"):
        score -= Decimal("5")

    return score


def ordenar_ofertas(ofertas):
    return sorted(
        ofertas,
        key=score_oferta
    )


# =========================================================
# APRESENTAÇÃO
# =========================================================

def mensagem_oferta(oferta, posicao=None):
    titulo = oferta["titulo"]
    preco = brl(oferta["preco"])

    linhas = []

    if posicao == 1:
        linhas.append("🏆 MELHOR OFERTA")
    elif posicao:
        linhas.append(f"#{posicao}")

    linhas.append(titulo)
    linhas.append("")

    linhas.append(f"💰 À vista: {preco}")

    if oferta.get("preco_original"):
        linhas.append(
            f"De: {brl(oferta['preco_original'])}"
        )

    if oferta.get("desconto"):
        linhas.append(
            f"🔥 Desconto: {oferta['desconto']}%"
        )

    parcelas = oferta.get("parcelas")
    valor_parcela = oferta.get("valor_parcela")

    if parcelas and valor_parcela:
        texto = (
            f"💳 {parcelas}x de "
            f"{brl(valor_parcela)}"
        )

        if oferta.get("sem_juros"):
            texto += " sem juros"

        linhas.append(texto)

        if oferta.get("total_parcelado"):
            linhas.append(
                f"Total parcelado: "
                f"{brl(oferta['total_parcelado'])}"
            )

    if oferta.get("frete_gratis"):
        linhas.append("🚚 Frete grátis")

    condicao = oferta.get("condicao")

    if condicao:
        condicao = {
            "new": "Novo",
            "used": "Usado",
        }.get(condicao, condicao)

        linhas.append(
            f"📦 Condição: {condicao}"
        )

    if oferta.get("official_store_id"):
        linhas.append("🏪 Loja oficial")

    if oferta.get("seller_id"):
        linhas.append(
            f"👤 Seller ID: {oferta['seller_id']}"
        )

    if oferta.get("item_id"):
        linhas.append(
            f"🆔 {oferta['item_id']}"
        )

    if oferta.get("link"):
        linhas.append("")
        linhas.append(
            f"🔗 {oferta['link']}"
        )

    return "\n".join(linhas)


# =========================================================
# TELEGRAM
# =========================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}

    message = update.get("message", {})
    chat_id = (
        message
        .get("chat", {})
        .get("id")
    )

    text = message.get("text", "").strip()

    if not chat_id:
        return "OK", 200

    # -----------------------------------------------------

    if text.startswith("/start"):
        send_message(
            chat_id,
            "🤖 GARIMPEIRO PESSOAL\n\n"
            "Comandos:\n\n"
            "/status\n"
            "/buscar produto\n"
            "/produto PRODUCT_ID\n"
            "/item ITEM_ID\n\n"
            "Exemplo:\n"
            "/buscar Mac Mini M4 16 512"
        )

    # -----------------------------------------------------

    elif text.startswith("/status"):
        status = (
            "✅ Mercado Livre conectado"
            if meli_access_token
            else "⚠️ Mercado Livre não autorizado"
        )

        send_message(
            chat_id,
            "🤖 STATUS\n\n"
            "✅ Telegram\n"
            "✅ Render\n"
            f"{status}"
        )

    # -----------------------------------------------------

    elif text.startswith("/buscar"):
        termo = (
            text
            .replace("/buscar", "", 1)
            .strip()
        )

        if not termo:
            send_message(
                chat_id,
                "Use:\n"
                "/buscar Mac Mini M4 16 512"
            )

            return "OK", 200

        resultado = buscar_catalogo(
            termo,
            limit=10,
        )

        if not resultado.get("ok"):
            send_message(
                chat_id,
                f"❌ Busca falhou.\n"
                f"HTTP: {resultado.get('status')}"
            )

            return "OK", 200

        results = (
            resultado
            .get("data", {})
            .get("results", [])
        )

        if not results:
            send_message(
                chat_id,
                "Nenhum produto encontrado."
            )

            return "OK", 200

        linhas = [
            f"🔎 RESULTADOS PARA\n{termo}",
            "",
        ]

        for i, produto in enumerate(
            results[:10],
            start=1,
        ):
            nome = (
                produto.get("name")
                or "Produto"
            )

            pid = produto.get("id")

            linhas.append(
                f"{i}. {nome}\n"
                f"ID: {pid}\n"
                f"/produto {pid}\n"
            )

        send_message(
            chat_id,
            "\n".join(linhas)
        )

    # -----------------------------------------------------

    elif text.startswith("/produto"):
        product_id = (
            text
            .replace("/produto", "", 1)
            .strip()
        )

        if not product_id:
            send_message(
                chat_id,
                "Use:\n/produto MLB74895216"
            )

            return "OK", 200

        resultado = detalhe_catalogo(
            product_id
        )

        if not resultado.get("ok"):
            send_message(
                chat_id,
                f"❌ Produto não encontrado.\n"
                f"HTTP: {resultado.get('status')}"
            )

            return "OK", 200

        data = resultado.get("data", {})

        nome = (
            data.get("name")
            or "Produto"
        )

        send_message(
            chat_id,
            "📦 PRODUTO DE CATÁLOGO\n\n"
            f"{nome}\n\n"
            f"ID: {product_id}\n"
            f"Status: {data.get('status')}\n\n"
            "ℹ️ Agora precisamos localizar "
            "os anúncios comerciais ligados "
            "a este produto."
        )

    # -----------------------------------------------------

    elif text.startswith("/item"):
        item_id = (
            text
            .replace("/item", "", 1)
            .strip()
        )

        if not item_id:
            send_message(
                chat_id,
                "Use:\n/item MLB1234567890"
            )

            return "OK", 200

        resultado = obter_item(
            item_id
        )

        if not resultado.get("ok"):
            send_message(
                chat_id,
                f"❌ Item não encontrado.\n"
                f"HTTP: {resultado.get('status')}"
            )

            return "OK", 200

        oferta = normalizar_item(
            resultado.get("data", {})
        )

        send_message(
            chat_id,
            mensagem_oferta(
                oferta,
                posicao=1,
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
