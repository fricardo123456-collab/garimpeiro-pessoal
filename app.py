import os
import re
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

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

MELI_API = "https://api.mercadolibre.com"
MELI_TOKEN_URL = f"{MELI_API}/oauth/token"

MELI_REDIRECT_URI = (
    "https://garimpeiro-pessoal.onrender.com/oauth/callback"
)

SITE_ID = "MLB"

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


def normalizar_texto(texto):
    texto = str(texto or "").lower()

    trocas = {
        "á": "a",
        "à": "a",
        "ã": "a",
        "â": "a",
        "é": "e",
        "ê": "e",
        "í": "i",
        "ó": "o",
        "ô": "o",
        "õ": "o",
        "ú": "u",
        "ç": "c",
    }

    for origem, destino in trocas.items():
        texto = texto.replace(origem, destino)

    texto = re.sub(
        r"[^a-z0-9]+",
        " ",
        texto
    )

    return " ".join(
        texto.split()
    )


def tokens_busca(termo):
    texto = normalizar_texto(
        termo
    )

    ignorar = {
        "apple",
        "de",
        "do",
        "da",
        "para",
        "com",
        "gb",
        "ssd",
        "ram",
    }

    tokens = []

    for palavra in texto.split():
        if palavra in ignorar:
            continue

        if len(palavra) < 2:
            continue

        tokens.append(
            palavra
        )

    return tokens


def score_produto(
    nome,
    termo
):
    nome_norm = normalizar_texto(
        nome
    )

    tokens = tokens_busca(
        termo
    )

    score = 0

    for token in tokens:
        if token in nome_norm:
            score += 10

    # penalizações para resultados claramente ruins
    palavras_ruins = [
        "suporte",
        "hub",
        "case",
        "adaptador",
        "parede",
        "capa",
        "dock",
        "teclado",
        "mouse",
    ]

    for ruim in palavras_ruins:
        if ruim in nome_norm:
            score -= 100

    # se busca contém m4, penaliza M2/M1/M3
    termo_norm = normalizar_texto(
        termo
    )

    if "m4" in termo_norm:
        for antigo in [
            "m1",
            "m2",
            "m3",
        ]:
            if antigo in nome_norm:
                score -= 80

    # se busca pede 512
    if "512" in termo_norm:
        if "512" in nome_norm:
            score += 25

        if "256" in nome_norm:
            score -= 50

        if "1 tb" in nome_norm:
            score -= 30

    # se busca pede 16
    if "16" in termo_norm:
        if "16" in nome_norm:
            score += 20

        if "24" in nome_norm:
            score -= 25

        if "32" in nome_norm:
            score -= 25

    return score


def cortar(
    texto,
    limite=3900
):
    texto = str(texto)

    if len(texto) <= limite:
        return texto

    return texto[:limite] + "\n..."


# =========================================================
# TELEGRAM
# =========================================================

def send_message(
    chat_id,
    texto
):
    try:
        response = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": cortar(texto),
                "disable_web_page_preview": False,
            },
            timeout=20,
        )

        return (
            response.status_code
            == 200
        )

    except Exception as exc:
        print(
            "Erro Telegram:",
            exc
        )

        return False


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
    parametros = {
        "response_type":
            "code",

        "client_id":
            MELI_CLIENT_ID,

        "redirect_uri":
            MELI_REDIRECT_URI,
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

    code = request.args.get(
        "code"
    )

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

        data = response.json()

    except Exception as exc:
        return (
            f"Erro OAuth: {exc}",
            500
        )

    if response.status_code != 200:
        return (
            "Erro OAuth HTTP "
            f"{response.status_code}<br><br>"
            f"{data}",
            400
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
        "<b>/buscar Mac Mini M4 16 512</b>",
        200
    )


# =========================================================
# TOKEN
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

    novo_access = data.get(
        "access_token"
    )

    if not novo_access:
        return False

    meli_access_token = (
        novo_access
    )

    novo_refresh = data.get(
        "refresh_token"
    )

    if novo_refresh:
        meli_refresh_token = (
            novo_refresh
        )

    return True


# =========================================================
# API MERCADO LIVRE
# =========================================================

def meli_get(
    endpoint,
    params=None,
    refresh=True
):
    if not meli_access_token:
        return {
            "ok": False,
            "status": None,
            "data": {},
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
            data = {}

    except requests.RequestException:
        return {
            "ok": False,
            "status": None,
            "data": {},
        }

    if (
        response.status_code == 401
        and refresh
        and renovar_access_token()
    ):
        return meli_get(
            endpoint,
            params=params,
            refresh=False
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
# CATÁLOGO
# =========================================================

def pesquisar_catalogo(
    termo,
    limite=20
):
    return meli_get(
        "/products/search",
        params={
            "site_id":
                SITE_ID,

            "status":
                "active",

            "q":
                termo,

            "limit":
                limite,
        },
    )


# =========================================================
# OFERTAS
# =========================================================

def obter_ofertas(
    product_id
):
    return meli_get(
        f"/products/{product_id}/items"
    )


def extrair_ofertas(
    resultado
):
    if not resultado.get(
        "ok"
    ):
        return []

    data = resultado.get(
        "data"
    )

    if isinstance(
        data,
        list
    ):
        return data

    if not isinstance(
        data,
        dict
    ):
        return []

    for chave in [
        "results",
        "items",
        "offers",
    ]:
        valor = data.get(
            chave
        )

        if isinstance(
            valor,
            list
        ):
            return valor

    return []


# =========================================================
# BUSCA COMPLETA
# =========================================================

def buscar_ofertas_completas(
    termo
):
    resultado = pesquisar_catalogo(
        termo,
        limite=20
    )

    if not resultado.get(
        "ok"
    ):
        return {
            "ok": False,
            "status":
                resultado.get("status"),
            "ofertas": [],
        }

    produtos = (
        resultado
        .get("data", {})
        .get("results", [])
    )

    produtos_rank = []

    for produto in produtos:
        nome = (
            produto.get("name")
            or produto.get("title")
            or ""
        )

        score = score_produto(
            nome,
            termo
        )

        if score <= 0:
            continue

        produtos_rank.append(
            (
                score,
                produto
            )
        )

    produtos_rank.sort(
        key=lambda x: x[0],
        reverse=True
    )

    # Consulta apenas os melhores produtos
    produtos_rank = (
        produtos_rank[:5]
    )

    ofertas_finais = []

    for score, produto in produtos_rank:
        product_id = produto.get(
            "id"
        )

        nome_produto = (
            produto.get("name")
            or produto.get("title")
            or "Produto"
        )

        if not product_id:
            continue

        resultado_ofertas = (
            obter_ofertas(
                product_id
            )
        )

        itens = extrair_ofertas(
            resultado_ofertas
        )

        for item in itens:
            if not isinstance(
                item,
                dict
            ):
                continue

            preco = item.get(
                "price"
            )

            if preco is None:
                continue

            shipping = (
                item.get("shipping")
                if isinstance(
                    item.get("shipping"),
                    dict
                )
                else {}
            )

            seller_address = (
                item.get(
                    "seller_address"
                )
                if isinstance(
                    item.get(
                        "seller_address"
                    ),
                    dict
                )
                else {}
            )

            cidade = None
            estado = None

            if isinstance(
                seller_address.get(
                    "city"
                ),
                dict
            ):
                cidade = (
                    seller_address
                    ["city"]
                    .get("name")
                )

            if isinstance(
                seller_address.get(
                    "state"
                ),
                dict
            ):
                estado = (
                    seller_address
                    ["state"]
                    .get("name")
                )

            ofertas_finais.append(
                {
                    "product_id":
                        product_id,

                    "product_name":
                        nome_produto,

                    "item_id":
                        item.get(
                            "item_id"
                        ),

                    "seller_id":
                        item.get(
                            "seller_id"
                        ),

                    "price":
                        preco,

                    "original_price":
                        item.get(
                            "original_price"
                        ),

                    "condition":
                        item.get(
                            "condition"
                        ),

                    "listing_type":
                        item.get(
                            "listing_type_id"
                        ),

                    "warranty":
                        item.get(
                            "warranty"
                        ),

                    "mercadopago":
                        item.get(
                            "accepts_mercadopago"
                        ),

                    "free_shipping":
                        shipping.get(
                            "free_shipping"
                        ),

                    "shipping_cost":
                        shipping.get(
                            "cost"
                        ),

                    "city":
                        cidade,

                    "state":
                        estado,

                    "deal_ids":
                        item.get(
                            "deal_ids"
                        ) or [],

                    "score":
                        score,
                }
            )

    # remove duplicados por item_id
    unicos = {}

    for oferta in ofertas_finais:
        item_id = oferta.get(
            "item_id"
        )

        if item_id:
            unicos[item_id] = (
                oferta
            )

    ofertas_finais = list(
        unicos.values()
    )

    # ordena primeiro por aderência ao produto,
    # depois por menor preço
    ofertas_finais.sort(
        key=lambda x: (
            -x.get("score", 0),
            Decimal(
                str(
                    x.get(
                        "price",
                        999999999
                    )
                )
            )
        )
    )

    return {
        "ok": True,
        "status": 200,
        "ofertas":
            ofertas_finais,
    }


# =========================================================
# FORMATAÇÃO
# =========================================================

def montar_card(
    oferta,
    posicao
):
    linhas = []

    if posicao == 1:
        linhas.append(
            "🏆 MELHOR OFERTA"
        )
    else:
        linhas.append(
            f"🔹 OFERTA #{posicao}"
        )

    linhas.append("")

    linhas.append(
        "💻 "
        + oferta.get(
            "product_name",
            "Produto"
        )
    )

    linhas.append("")

    linhas.append(
        "💰 "
        + brl(
            oferta.get(
                "price"
            )
        )
    )

    original = oferta.get(
        "original_price"
    )

    if original:
        linhas.append(
            "De: "
            + brl(
                original
            )
        )

        try:
            atual = Decimal(
                str(
                    oferta.get(
                        "price"
                    )
                )
            )

            antigo = Decimal(
                str(original)
            )

            if (
                antigo > 0
                and atual < antigo
            ):
                desconto = (
                    (
                        antigo - atual
                    )
                    / antigo
                    * 100
                )

                linhas.append(
                    "🔥 "
                    f"{desconto:.1f}% OFF"
                )

        except Exception:
            pass

    # A API liberada ainda não retornou installments.
    linhas.append(
        "💳 Parcelamento: "
        "não informado pela API"
    )

    if oferta.get(
        "free_shipping"
    ):
        linhas.append(
            "🚚 Frete grátis"
        )

    condicao = oferta.get(
        "condition"
    )

    if condicao == "new":
        linhas.append(
            "📦 Novo"
        )

    elif condicao == "used":
        linhas.append(
            "📦 Usado"
        )

    garantia = oferta.get(
        "warranty"
    )

    if garantia:
        linhas.append(
            "🛡 "
            + garantia
        )

    local = []

    if oferta.get(
        "city"
    ):
        local.append(
            oferta.get(
                "city"
            )
        )

    if oferta.get(
        "state"
    ):
        local.append(
            oferta.get(
                "state"
            )
        )

    if local:
        linhas.append(
            "📍 "
            + " - ".join(
                local
            )
        )

    if oferta.get(
        "seller_id"
    ):
        linhas.append(
            "👤 Vendedor: "
            + str(
                oferta.get(
                    "seller_id"
                )
            )
        )

    if oferta.get(
        "deal_ids"
    ):
        linhas.append(
            "🔥 Promoção detectada"
        )

    linhas.append(
        "🆔 "
        + str(
            oferta.get(
                "item_id"
            )
        )
    )

    return "\n".join(
        linhas
    )


# =========================================================
# COMANDO BUSCAR
# =========================================================

def comando_buscar(
    chat_id,
    termo
):
    send_message(
        chat_id,
        "🔎 GARIMPANDO...\n\n"
        f"{termo}\n\n"
        "Buscando produtos e "
        "ofertas comerciais..."
    )

    resultado = (
        buscar_ofertas_completas(
            termo
        )
    )

    if not resultado.get(
        "ok"
    ):
        send_message(
            chat_id,
            "❌ A busca falhou.\n\n"
            f"HTTP: "
            f"{resultado.get('status')}"
        )

        return

    ofertas = resultado.get(
        "ofertas",
        []
    )

    if not ofertas:
        send_message(
            chat_id,
            "❌ Não encontrei "
            "ofertas comerciais compatíveis."
        )

        return

    send_message(
        chat_id,
        "✅ GARIMPO CONCLUÍDO\n\n"
        f"{len(ofertas)} oferta(s) "
        "compatível(is) encontrada(s).\n\n"
        "Mostrando as melhores:"
    )

    # Telegram ficaria enorme se mostramos tudo.
    # Limite visual inicial: 10 melhores.
    for indice, oferta in enumerate(
        ofertas[:10],
        start=1
    ):
        send_message(
            chat_id,
            montar_card(
                oferta,
                indice
            )
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

    if not resultado.get(
        "ok"
    ):
        send_message(
            chat_id,
            "❌ Mercado Livre "
            "não autenticado."
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
        f"User ID: "
        f"{data.get('id')}\n"
        f"Nickname: "
        f"{data.get('nickname')}\n"
        f"Site: "
        f"{data.get('site_id')}\n\n"
        "✅ OAuth válido\n"
        "✅ API acessível"
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

    if text.startswith(
        "/start"
    ):
        send_message(
            chat_id,
            "🤖 GARIMPEIRO PESSOAL\n\n"
            "Agora a busca já procura "
            "as ofertas automaticamente.\n\n"
            "Exemplo:\n"
            "/buscar Mac Mini M4 16 512\n\n"
            "Outros comandos:\n"
            "/status\n"
            "/teste"
        )

    elif text.startswith(
        "/status"
    ):
        mercado = (
            "✅ conectado"
            if meli_access_token
            else "⚠️ não autorizado"
        )

        send_message(
            chat_id,
            "🤖 STATUS\n\n"
            "✅ Telegram\n"
            "✅ Render\n"
            f"Mercado Livre: "
            f"{mercado}"
        )

    elif text.startswith(
        "/teste"
    ):
        if not meli_access_token:
            send_message(
                chat_id,
                "⚠️ Autorize:\n"
                "https://garimpeiro-pessoal."
                "onrender.com/oauth/login"
            )

        else:
            comando_teste(
                chat_id
            )

    elif text.startswith(
        "/buscar"
    ):
        if not meli_access_token:
            send_message(
                chat_id,
                "⚠️ Mercado Livre "
                "não autorizado.\n\n"
                "Abra:\n"
                "https://garimpeiro-pessoal."
                "onrender.com/oauth/login"
            )

            return "OK", 200

        partes = text.split(
            maxsplit=1
        )

        if len(partes) < 2:
            send_message(
                chat_id,
                "Use, por exemplo:\n"
                "/buscar Mac Mini M4 16 512"
            )

        else:
            comando_buscar(
                chat_id,
                partes[1]
            )

    else:
        send_message(
            chat_id,
            "🤖 Envie uma busca assim:\n\n"
            "/buscar Mac Mini M4 16 512"
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
