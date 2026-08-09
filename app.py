import os
import json
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import requests
from flask import Flask, request, redirect


app = Flask(__name__)


# =========================================================
# CONFIGURAÇÕES
# =========================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
MELI_CLIENT_ID = os.environ.get("MELI_CLIENT_ID")
MELI_CLIENT_SECRET = os.environ.get("MELI_CLIENT_SECRET")

TELEGRAM_API = (
    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
)

MELI_API = "https://api.mercadolibre.com"
MELI_TOKEN_URL = f"{MELI_API}/oauth/token"

MELI_REDIRECT_URI = (
    "https://garimpeiro-pessoal.onrender.com/oauth/callback"
)

SITE_ID = "MLB"

# Token obtido pelo OAuth.
# Ainda permanece em memória até adicionarmos persistência.
meli_access_token = None
meli_refresh_token = None


# =========================================================
# UTILITÁRIOS
# =========================================================

def brl(valor):
    if valor is None:
        return "Não informado"

    try:
        numero = Decimal(str(valor))

        texto = f"{numero:,.2f}"
        texto = (
            texto
            .replace(",", "X")
            .replace(".", ",")
            .replace("X", ".")
        )

        return f"R$ {texto}"

    except (InvalidOperation, ValueError, TypeError):
        return str(valor)


def calcular_desconto(preco, original):
    try:
        preco = Decimal(str(preco))
        original = Decimal(str(original))

        if original <= 0:
            return None

        if preco >= original:
            return None

        percentual = (
            (original - preco)
            / original
            * Decimal("100")
        )

        return round(
            float(percentual),
            1
        )

    except Exception:
        return None


def traduzir_condicao(condicao):
    mapa = {
        "new": "Novo",
        "used": "Usado",
        "not_specified": "Não especificado",
    }

    return mapa.get(
        condicao,
        condicao or "Não informada"
    )


def cortar(texto, limite=3900):
    texto = str(texto)

    if len(texto) <= limite:
        return texto

    return texto[:limite] + "\n..."


# =========================================================
# TELEGRAM
# =========================================================

def send_message(chat_id, texto):
    if not TELEGRAM_TOKEN:
        print("TELEGRAM_BOT_TOKEN ausente.")
        return False

    try:
        response = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": cortar(texto),
                "disable_web_page_preview": True,
            },
            timeout=20,
        )

        return response.status_code == 200

    except requests.RequestException as exc:
        print(
            "Erro ao enviar Telegram:",
            exc
        )

        return False


def enviar_texto_grande(
    chat_id,
    texto,
    limite=3800
):
    texto = str(texto)

    for inicio in range(
        0,
        len(texto),
        limite
    ):
        send_message(
            chat_id,
            texto[
                inicio:
                inicio + limite
            ]
        )


# =========================================================
# OAUTH
# =========================================================

@app.route("/")
def home():
    status = (
        "conectado"
        if meli_access_token
        else "aguardando OAuth"
    )

    return (
        "Garimpeiro Pessoal online! 🤖"
        f"<br>Mercado Livre: {status}",
        200,
    )


@app.route("/oauth/login")
def oauth_login():
    if not MELI_CLIENT_ID:
        return (
            "MELI_CLIENT_ID não configurado.",
            500
        )

    parametros = {
        "response_type": "code",
        "client_id": MELI_CLIENT_ID,
        "redirect_uri": MELI_REDIRECT_URI,
    }

    url = (
        "https://auth.mercadolivre.com.br/"
        "authorization?"
        + urlencode(parametros)
    )

    return redirect(url)


@app.route("/oauth/callback")
def oauth_callback():
    global meli_access_token
    global meli_refresh_token

    code = request.args.get("code")

    if not code:
        return (
            "Código OAuth não recebido.",
            400
        )

    try:
        response = requests.post(
            MELI_TOKEN_URL,
            data={
                "grant_type":
                    "authorization_code",

                "client_id":
                    MELI_CLIENT_ID,

                "client_secret":
                    MELI_CLIENT_SECRET,

                "code":
                    code,

                "redirect_uri":
                    MELI_REDIRECT_URI,
            },
            timeout=25,
        )

        try:
            data = response.json()
        except Exception:
            data = {}

    except requests.RequestException as exc:
        return (
            f"Erro OAuth: {exc}",
            500
        )

    if response.status_code != 200:
        return (
            "❌ Não foi possível gerar "
            "o Access Token.<br><br>"
            f"HTTP {response.status_code}<br>"
            f"{data}",
            400,
        )

    meli_access_token = (
        data.get("access_token")
    )

    meli_refresh_token = (
        data.get("refresh_token")
    )

    if not meli_access_token:
        return (
            "Access Token não recebido.",
            400
        )

    return (
        "✅ Mercado Livre conectado!"
        "<br><br>"
        "Volte ao Telegram e envie "
        "<b>/teste</b>.",
        200,
    )


# =========================================================
# REFRESH TOKEN
# =========================================================

def renovar_access_token():
    global meli_access_token
    global meli_refresh_token

    if not meli_refresh_token:
        return False

    try:
        response = requests.post(
            MELI_TOKEN_URL,
            data={
                "grant_type":
                    "refresh_token",

                "client_id":
                    MELI_CLIENT_ID,

                "client_secret":
                    MELI_CLIENT_SECRET,

                "refresh_token":
                    meli_refresh_token,
            },
            timeout=25,
        )

        data = response.json()

    except Exception:
        return False

    if response.status_code != 200:
        return False

    novo_access_token = (
        data.get("access_token")
    )

    if not novo_access_token:
        return False

    meli_access_token = (
        novo_access_token
    )

    novo_refresh = (
        data.get("refresh_token")
    )

    if novo_refresh:
        meli_refresh_token = (
            novo_refresh
        )

    return True


# =========================================================
# REQUEST MERCADO LIVRE
# =========================================================

def meli_get(
    endpoint,
    params=None,
    tentar_refresh=True
):
    if not meli_access_token:
        return {
            "ok": False,
            "status": None,
            "data": {},
            "message":
                "Mercado Livre não autorizado.",
        }

    try:
        response = requests.get(
            f"{MELI_API}{endpoint}",
            headers={
                "Authorization":
                    f"Bearer {meli_access_token}",

                "Accept":
                    "application/json",
            },
            params=params,
            timeout=25,
        )

        try:
            data = response.json()
        except Exception:
            data = {
                "raw":
                    response.text[:2000]
            }

    except requests.RequestException as exc:
        return {
            "ok": False,
            "status": None,
            "data": {},
            "message": str(exc),
        }

    # Token expirado
    if (
        response.status_code == 401
        and tentar_refresh
        and renovar_access_token()
    ):
        return meli_get(
            endpoint,
            params=params,
            tentar_refresh=False
        )

    return {
        "ok":
            200 <= response.status_code < 300,

        "status":
            response.status_code,

        "data":
            data,
    }


# =========================================================
# ERROS
# =========================================================

def formatar_erro(resultado):
    status = resultado.get("status")
    data = resultado.get("data") or {}

    linhas = [
        "❌ MERCADO LIVRE",
        "",
        f"HTTP: {status}",
    ]

    if isinstance(data, dict):
        if data.get("error"):
            linhas.append(
                f"error: {data.get('error')}"
            )

        if data.get("message"):
            linhas.append(
                f"message: {data.get('message')}"
            )

        if data.get("cause"):
            try:
                linhas.append(
                    "cause: "
                    + json.dumps(
                        data.get("cause"),
                        ensure_ascii=False
                    )[:800]
                )
            except Exception:
                pass

    if resultado.get("message"):
        linhas.append(
            resultado.get("message")
        )

    return "\n".join(linhas)


# =========================================================
# CATÁLOGO
# =========================================================

def pesquisar_produtos(
    termo,
    limite=20
):
    return meli_get(
        "/products/search",
        params={
            "site_id": SITE_ID,
            "status": "active",
            "q": termo,
            "limit": limite,
        },
    )


def obter_produto(
    product_id
):
    return meli_get(
        f"/products/{product_id}"
    )


# =========================================================
# OFERTAS COMERCIAIS
# =========================================================

def obter_ofertas_produto(
    product_id
):
    """
    Esta é a rota que já comprovamos
    na sua aplicação com HTTP 200.
    """

    return meli_get(
        f"/products/{product_id}/items"
    )


def extrair_lista_ofertas(
    resultado
):
    if not resultado.get("ok"):
        return []

    data = resultado.get("data")

    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        return []

    for chave in [
        "results",
        "items",
        "offers",
    ]:
        valor = data.get(chave)

        if isinstance(valor, list):
            return valor

    return []


# =========================================================
# NORMALIZAÇÃO
# =========================================================

def normalizar_oferta(
    item
):
    if not isinstance(item, dict):
        return None

    shipping = (
        item.get("shipping")
        if isinstance(
            item.get("shipping"),
            dict
        )
        else {}
    )

    seller_address = (
        item.get("seller_address")
        if isinstance(
            item.get("seller_address"),
            dict
        )
        else {}
    )

    cidade = (
        seller_address
        .get("city", {})
        .get("name")
        if isinstance(
            seller_address.get("city"),
            dict
        )
        else None
    )

    estado = (
        seller_address
        .get("state", {})
        .get("name")
        if isinstance(
            seller_address.get("state"),
            dict
        )
        else None
    )

    preco = item.get("price")
    original = item.get(
        "original_price"
    )

    desconto = calcular_desconto(
        preco,
        original
    )

    deal_ids = item.get(
        "deal_ids"
    ) or []

    return {
        "item_id":
            item.get("item_id")
            or item.get("id"),

        "seller_id":
            item.get("seller_id"),

        "price":
            preco,

        "original_price":
            original,

        "discount":
            desconto,

        "currency":
            item.get("currency_id"),

        "condition":
            item.get("condition"),

        "listing_type":
            item.get("listing_type_id"),

        "warranty":
            item.get("warranty"),

        "official_store_id":
            item.get("official_store_id"),

        "mercadopago":
            item.get(
                "accepts_mercadopago"
            ),

        "free_shipping":
            shipping.get(
                "free_shipping"
            ),

        "shipping_cost":
            shipping.get("cost"),

        "logistic_type":
            shipping.get(
                "logistic_type"
            ),

        "city":
            cidade,

        "state":
            estado,

        "deal_ids":
            deal_ids,

        "tags":
            item.get("tags") or [],

        "user_product_id":
            item.get(
                "user_product_id"
            ),

        "min_purchase_unit":
            item.get(
                "min_purchase_unit"
            ) or 1,

        "raw":
            item,
    }


# =========================================================
# RANKING
# =========================================================

def preco_numerico(
    oferta
):
    try:
        return Decimal(
            str(oferta.get("price"))
        )
    except Exception:
        return Decimal(
            "999999999999"
        )


def ordenar_ofertas(
    ofertas
):
    return sorted(
        ofertas,
        key=preco_numerico
    )


# =========================================================
# FORMATAÇÃO DE OFERTA
# =========================================================

def formatar_oferta(
    oferta,
    posicao
):
    linhas = []

    if posicao == 1:
        linhas.append(
            "🏆 MELHOR PREÇO ENCONTRADO"
        )
    else:
        linhas.append(
            f"🔹 OFERTA #{posicao}"
        )

    linhas.append("")

    linhas.append(
        f"💰 Preço atual: "
        f"{brl(oferta.get('price'))}"
    )

    original = oferta.get(
        "original_price"
    )

    if original is not None:
        linhas.append(
            f"Preço anterior: {brl(original)}"
        )

    desconto = oferta.get(
        "discount"
    )

    if desconto is not None:
        linhas.append(
            f"🔥 Desconto: {desconto}%"
        )

    # IMPORTANTE:
    # Não inventar parcelas.
    # Este endpoint não retornou
    # installments no seu JSON.
    linhas.append(
        "💳 Parcelamento: "
        "não informado por esta rota da API"
    )

    if oferta.get("free_shipping"):
        linhas.append(
            "🚚 Frete grátis"
        )
    else:
        custo = oferta.get(
            "shipping_cost"
        )

        if custo is not None:
            linhas.append(
                f"🚚 Frete: {brl(custo)}"
            )

    linhas.append(
        "📦 Condição: "
        + traduzir_condicao(
            oferta.get("condition")
        )
    )

    if oferta.get("warranty"):
        linhas.append(
            "🛡 Garantia: "
            + str(
                oferta.get("warranty")
            )
        )

    if oferta.get(
        "official_store_id"
    ):
        linhas.append(
            "🏪 Loja oficial"
        )

    if oferta.get("mercadopago"):
        linhas.append(
            "💙 Mercado Pago aceito"
        )

    local = []

    if oferta.get("city"):
        local.append(
            oferta.get("city")
        )

    if oferta.get("state"):
        local.append(
            oferta.get("state")
        )

    if local:
        linhas.append(
            "📍 Vendedor: "
            + " - ".join(local)
        )

    if oferta.get("seller_id"):
        linhas.append(
            "👤 Seller ID: "
            f"{oferta.get('seller_id')}"
        )

    if oferta.get("listing_type"):
        linhas.append(
            "📋 Tipo do anúncio: "
            f"{oferta.get('listing_type')}"
        )

    deal_ids = oferta.get(
        "deal_ids"
    )

    if deal_ids:
        linhas.append(
            "🔥 Promoção/deal detectado: "
            + ", ".join(
                map(str, deal_ids)
            )
        )

    linhas.append(
        "🎟 Cupom: "
        "não informado por esta rota"
    )

    linhas.append(
        f"🆔 Item: "
        f"{oferta.get('item_id')}"
    )

    return "\n".join(linhas)


# =========================================================
# COMANDO /BUSCAR
# =========================================================

def comando_buscar(
    chat_id,
    termo
):
    send_message(
        chat_id,
        "🔎 Procurando produtos...\n\n"
        f"{termo}"
    )

    resultado = pesquisar_produtos(
        termo,
        limite=20
    )

    if not resultado.get("ok"):
        send_message(
            chat_id,
            formatar_erro(
                resultado
            )
        )

        return

    data = resultado.get(
        "data",
        {}
    )

    produtos = data.get(
        "results",
        []
    )

    if not produtos:
        send_message(
            chat_id,
            "Nenhum produto encontrado."
        )

        return

    linhas = [
        "📦 PRODUTOS ENCONTRADOS",
        "",
    ]

    for indice, produto in enumerate(
        produtos[:20],
        start=1
    ):
        product_id = produto.get("id")

        nome = (
            produto.get("name")
            or produto.get("title")
            or "Produto"
        )

        linhas.append(
            f"{indice}. {nome}"
        )

        linhas.append(
            f"ID: {product_id}"
        )

        linhas.append(
            f"/ofertas {product_id}"
        )

        linhas.append("")

    enviar_texto_grande(
        chat_id,
        "\n".join(linhas)
    )


# =========================================================
# COMANDO /OFERTAS
# =========================================================

def comando_ofertas(
    chat_id,
    product_id
):
    product_id = (
        product_id
        .strip()
        .upper()
    )

    send_message(
        chat_id,
        "🕵️ Garimpando ofertas reais...\n\n"
        f"Produto: {product_id}"
    )

    resultado = obter_ofertas_produto(
        product_id
    )

    if not resultado.get("ok"):
        send_message(
            chat_id,
            formatar_erro(
                resultado
            )
        )

        return

    itens = extrair_lista_ofertas(
        resultado
    )

    if not itens:
        send_message(
            chat_id,
            "A API respondeu HTTP 200, "
            "mas não retornou ofertas."
        )

        return

    ofertas = []

    for item in itens:
        oferta = normalizar_oferta(
            item
        )

        if (
            oferta
            and oferta.get("price")
            is not None
        ):
            ofertas.append(
                oferta
            )

    ofertas = ordenar_ofertas(
        ofertas
    )

    if not ofertas:
        send_message(
            chat_id,
            "Nenhuma oferta com preço "
            "foi encontrada."
        )

        return

    send_message(
        chat_id,
        "✅ GARIMPO CONCLUÍDO\n\n"
        f"Ofertas encontradas: "
        f"{len(ofertas)}\n\n"
        "Ordenadas do menor "
        "para o maior preço."
    )

    for indice, oferta in enumerate(
        ofertas[:20],
        start=1
    ):
        send_message(
            chat_id,
            formatar_oferta(
                oferta,
                indice
            )
        )


# =========================================================
# RAW
# =========================================================

def comando_raw(
    chat_id,
    product_id
):
    resultado = obter_ofertas_produto(
        product_id
    )

    if not resultado.get("ok"):
        send_message(
            chat_id,
            formatar_erro(resultado)
        )

        return

    itens = extrair_lista_ofertas(
        resultado
    )

    if not itens:
        send_message(
            chat_id,
            "Nenhuma oferta retornada."
        )

        return

    primeiro = itens[0]

    texto = json.dumps(
        primeiro,
        ensure_ascii=False,
        indent=2
    )

    enviar_texto_grande(
        chat_id,
        "🔬 PRIMEIRA OFERTA — JSON\n\n"
        + texto
    )


# =========================================================
# TESTE
# =========================================================

def comando_teste(
    chat_id
):
    resultado = meli_get(
        "/users/me"
    )

    if not resultado.get("ok"):
        send_message(
            chat_id,
            formatar_erro(resultado)
        )

        return

    data = resultado.get(
        "data",
        {}
    )

    send_message(
        chat_id,
        "✅ AUTENTICAÇÃO OK\n\n"
        f"HTTP: "
        f"{resultado.get('status')}\n"
        f"User ID: {data.get('id')}\n"
        f"Nickname: "
        f"{data.get('nickname')}\n"
        f"Site: {data.get('site_id')}\n\n"
        "✅ OAuth válido\n"
        "✅ Access Token válido\n"
        "✅ API Mercado Livre acessível"
    )


# =========================================================
# WEBHOOK
# =========================================================

@app.route(
    "/webhook",
    methods=["POST"]
)
def webhook():
    update = (
        request.get_json(
            silent=True
        )
        or {}
    )

    message = update.get(
        "message",
        {}
    )

    chat_id = (
        message
        .get("chat", {})
        .get("id")
    )

    text = (
        message
        .get("text", "")
        .strip()
    )

    if not chat_id:
        return "OK", 200

    # -----------------------------------------------------

    if text.startswith("/start"):
        send_message(
            chat_id,
            "🤖 GARIMPEIRO PESSOAL\n\n"
            "Mercado Livre conectado.\n\n"
            "Comandos:\n\n"
            "/status\n"
            "/teste\n"
            "/buscar Mac Mini M4 16 512\n"
            "/ofertas MLB74895216\n"
            "/raw MLB74895216"
        )

    # -----------------------------------------------------

    elif text.startswith("/status"):
        mercado = (
            "✅ conectado"
            if meli_access_token
            else "⚠️ aguardando OAuth"
        )

        send_message(
            chat_id,
            "🤖 STATUS\n\n"
            "✅ Telegram\n"
            "✅ Render\n"
            f"Mercado Livre: {mercado}"
        )

    # -----------------------------------------------------

    elif text.startswith("/teste"):
        if not meli_access_token:
            send_message(
                chat_id,
                "⚠️ Autorize o Mercado Livre:\n\n"
                "https://garimpeiro-pessoal."
                "onrender.com/oauth/login"
            )

        else:
            comando_teste(
                chat_id
            )

    # -----------------------------------------------------

    elif text.startswith("/buscar"):
        if not meli_access_token:
            send_message(
                chat_id,
                "⚠️ Mercado Livre "
                "não autorizado."
            )

            return "OK", 200

        partes = text.split(
            maxsplit=1
        )

        if len(partes) < 2:
            send_message(
                chat_id,
                "Exemplo:\n"
                "/buscar Mac Mini M4 16 512"
            )

        else:
            comando_buscar(
                chat_id,
                partes[1]
            )

    # -----------------------------------------------------

    elif text.startswith("/ofertas"):
        if not meli_access_token:
            send_message(
                chat_id,
                "⚠️ Mercado Livre "
                "não autorizado."
            )

            return "OK", 200

        partes = text.split(
            maxsplit=1
        )

        if len(partes) < 2:
            send_message(
                chat_id,
                "Exemplo:\n"
                "/ofertas MLB74895216"
            )

        else:
            comando_ofertas(
                chat_id,
                partes[1]
            )

    # -----------------------------------------------------

    elif text.startswith("/raw"):
        if not meli_access_token:
            send_message(
                chat_id,
                "⚠️ Mercado Livre "
                "não autorizado."
            )

            return "OK", 200

        partes = text.split(
            maxsplit=1
        )

        if len(partes) < 2:
            send_message(
                chat_id,
                "Exemplo:\n"
                "/raw MLB74895216"
            )

        else:
            comando_raw(
                chat_id,
                partes[1]
            )

    # -----------------------------------------------------

    else:
        send_message(
            chat_id,
            "🤖 Comando não reconhecido.\n\n"
            "Envie /start."
        )

    return "OK", 200


# =========================================================
# START
# =========================================================

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
