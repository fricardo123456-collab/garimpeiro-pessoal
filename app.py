import os
import json
from urllib.parse import urlencode

import requests
from flask import Flask, request, redirect


app = Flask(__name__)


# =========================================================
# CONFIGURAÇÃO
# =========================================================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

TELEGRAM_API = (
    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
)

MELI_CLIENT_ID = os.environ.get("MELI_CLIENT_ID")
MELI_CLIENT_SECRET = os.environ.get("MELI_CLIENT_SECRET")

MELI_REDIRECT_URI = (
    "https://garimpeiro-pessoal.onrender.com/oauth/callback"
)

MELI_API = "https://api.mercadolibre.com"
MELI_TOKEN_URL = f"{MELI_API}/oauth/token"


# =========================================================
# TOKENS EM MEMÓRIA
# =========================================================

meli_access_token = None
meli_refresh_token = None


# =========================================================
# TELEGRAM
# =========================================================

def send_message(chat_id, text):
    try:
        response = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=20,
        )

        return response.status_code == 200

    except Exception as exc:
        print("Erro Telegram:", exc)
        return False


def enviar_texto_grande(
    chat_id,
    texto,
    limite=3800
):
    """
    Divide mensagens grandes para não ultrapassar
    o limite do Telegram.
    """

    if not texto:
        return

    texto = str(texto)

    for inicio in range(
        0,
        len(texto),
        limite
    ):
        parte = texto[
            inicio:inicio + limite
        ]

        send_message(
            chat_id,
            parte
        )


# =========================================================
# MERCADO LIVRE - REQUEST
# =========================================================

def meli_get(
    endpoint,
    params=None
):
    global meli_access_token

    if not meli_access_token:
        return {
            "ok": False,
            "status": None,
            "data": {},
            "error": "TOKEN_NOT_AVAILABLE",
            "message": (
                "Mercado Livre não autorizado."
            ),
        }

    try:
        response = requests.get(
            f"{MELI_API}{endpoint}",
            headers={
                "Authorization": (
                    f"Bearer {meli_access_token}"
                ),
                "Accept": "application/json",
            },
            params=params,
            timeout=25,
        )

        try:
            data = response.json()

        except Exception:
            data = {
                "raw_response": (
                    response.text[:3000]
                )
            }

        return {
            "ok": (
                200
                <= response.status_code
                < 300
            ),
            "status": response.status_code,
            "data": data,
        }

    except requests.RequestException as exc:
        return {
            "ok": False,
            "status": None,
            "data": {},
            "error": "REQUEST_ERROR",
            "message": str(exc),
        }


# =========================================================
# HOME
# =========================================================

@app.route("/")
def home():
    return (
        "Garimpeiro Pessoal online! 🤖",
        200
    )


# =========================================================
# OAUTH
# =========================================================

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

    authorization_url = (
        "https://auth.mercadolivre.com.br/"
        "authorization?"
        + urlencode(parametros)
    )

    return redirect(
        authorization_url
    )


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

    if (
        not MELI_CLIENT_ID
        or not MELI_CLIENT_SECRET
    ):
        return (
            "Client ID ou Client Secret "
            "não configurados.",
            500
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
            data = {
                "raw": response.text
            }

    except Exception as exc:
        return (
            f"Erro ao comunicar com "
            f"Mercado Livre: {exc}",
            500
        )

    if response.status_code != 200:

        return (
            "Erro OAuth HTTP "
            f"{response.status_code}<br><br>"
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
        "Volte ao Telegram."
        "<br><br>"
        "Envie /teste.",
        200,
    )


# =========================================================
# FORMATADORES
# =========================================================

def extrair_erro(resultado):

    data = (
        resultado.get("data")
        or {}
    )

    partes = []

    if isinstance(data, dict):

        error = data.get("error")
        message = data.get("message")
        cause = data.get("cause")

        if error:
            partes.append(
                f"error={error}"
            )

        if message:
            partes.append(
                f"message={message}"
            )

        if cause:
            try:
                texto_cause = json.dumps(
                    cause,
                    ensure_ascii=False
                )

                if len(texto_cause) > 500:
                    texto_cause = (
                        texto_cause[:500]
                        + "..."
                    )

                partes.append(
                    f"cause={texto_cause}"
                )

            except Exception:
                pass

    if resultado.get("error"):
        partes.append(
            f"error interno="
            f"{resultado.get('error')}"
        )

    if resultado.get("message"):
        partes.append(
            resultado.get("message")
        )

    return "\n".join(partes)


def linha_teste(
    nome,
    resultado
):

    status = resultado.get("status")

    if resultado.get("ok"):
        simbolo = "✅"

    elif status in (401, 403):
        simbolo = "🔒"

    elif status == 404:
        simbolo = "⚠️"

    else:
        simbolo = "❌"

    texto = (
        f"{simbolo} {nome}\n"
        f"HTTP: {status}"
    )

    erro = extrair_erro(
        resultado
    )

    if erro:
        texto += (
            f"\n{erro}"
        )

    return texto


# =========================================================
# TESTE DE AUTENTICAÇÃO
# =========================================================

def comando_teste(chat_id):

    if not meli_access_token:

        send_message(
            chat_id,
            "⚠️ Mercado Livre "
            "não autorizado.\n\n"
            "Abra:\n"
            "https://garimpeiro-pessoal."
            "onrender.com/oauth/login"
        )

        return

    send_message(
        chat_id,
        "🧪 Testando autenticação..."
    )

    resultado = meli_get(
        "/users/me"
    )

    if not resultado.get("ok"):

        send_message(
            chat_id,
            "❌ TESTE FALHOU\n\n"
            f"HTTP: "
            f"{resultado.get('status')}\n\n"
            f"{extrair_erro(resultado)}"
        )

        return

    data = resultado.get(
        "data",
        {}
    )

    user_id = data.get("id")
    nickname = data.get("nickname")
    site_id = data.get("site_id")

    send_message(
        chat_id,
        "✅ AUTENTICAÇÃO OK!\n\n"

        f"HTTP: "
        f"{resultado.get('status')}\n"

        f"User ID: {user_id}\n"
        f"Nickname: {nickname}\n"
        f"Site: {site_id}\n\n"

        "✅ OAuth válido\n"
        "✅ Access Token válido\n"
        "✅ API acessível"
    )


# =========================================================
# BUSCA DE PRODUTOS DE CATÁLOGO
# =========================================================

def buscar_catalogo(
    chat_id,
    termo
):

    send_message(
        chat_id,
        "🔎 BUSCANDO PRODUTOS\n\n"
        f"{termo}"
    )

    resultado = meli_get(
        "/products/search",
        params={
            "site_id": "MLB",
            "status": "active",
            "q": termo,
            "limit": 10,
        },
    )

    if not resultado.get("ok"):

        send_message(
            chat_id,
            "❌ ERRO NA BUSCA\n\n"
            f"HTTP: "
            f"{resultado.get('status')}\n"
            f"{extrair_erro(resultado)}"
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
            "❌ Nenhum produto encontrado."
        )

        return

    linhas = [
        "✅ PRODUTOS ENCONTRADOS",
        "",
    ]

    for indice, produto in enumerate(
        produtos,
        start=1
    ):

        product_id = produto.get(
            "id",
            "?"
        )

        nome = (
            produto.get("name")
            or produto.get("title")
            or "Produto"
        )

        linhas.extend([
            f"{indice}. {nome}",
            f"ID: {product_id}",
            f"/ofertas {product_id}",
            "",
        ])

    enviar_texto_grande(
        chat_id,
        "\n".join(linhas)
    )


# =========================================================
# OFERTAS DE UM PRODUTO
# =========================================================

def buscar_ofertas(
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
        "🕵️ PROCURANDO OFERTAS\n\n"
        f"Produto: {product_id}"
    )

    resultado = meli_get(
        f"/products/{product_id}/items"
    )

    if not resultado.get("ok"):

        send_message(
            chat_id,
            "❌ NÃO FOI POSSÍVEL "
            "CONSULTAR OFERTAS\n\n"
            f"HTTP: "
            f"{resultado.get('status')}\n"
            f"{extrair_erro(resultado)}"
        )

        return

    data = resultado.get(
        "data",
        {}
    )

    ofertas = []

    if isinstance(data, dict):

        if isinstance(
            data.get("results"),
            list
        ):
            ofertas = data.get(
                "results"
            )

        elif isinstance(
            data.get("items"),
            list
        ):
            ofertas = data.get(
                "items"
            )

        elif isinstance(
            data.get("offers"),
            list
        ):
            ofertas = data.get(
                "offers"
            )

    elif isinstance(data, list):
        ofertas = data

    if not ofertas:

        send_message(
            chat_id,
            "⚠️ A rota respondeu HTTP 200, "
            "mas nenhuma oferta foi "
            "identificada automaticamente.\n\n"
            f"Use:\n"
            f"/rawofertas {product_id}"
        )

        return

    linhas = [
        "💰 OFERTAS ENCONTRADAS",
        "",
        "Rota: "
        f"/products/{product_id}/items",
        f"HTTP: {resultado.get('status')}",
        "",
    ]

    for indice, oferta in enumerate(
        ofertas[:20],
        start=1
    ):

        if not isinstance(
            oferta,
            dict
        ):
            continue

        item_id = (
            oferta.get("item_id")
            or oferta.get("id")
            or "?"
        )

        titulo = (
            oferta.get("title")
            or oferta.get("name")
            or "Produto"
        )

        preco = (
            oferta.get("price")
        )

        moeda = (
            oferta.get("currency_id")
            or "BRL"
        )

        linhas.append(
            f"{indice}. {titulo}"
        )

        if preco is not None:

            try:
                preco_formatado = (
                    f"{float(preco):,.2f}"
                    .replace(",", "X")
                    .replace(".", ",")
                    .replace("X", ".")
                )

                linhas.append(
                    "Preço: "
                    f"R$ {preco_formatado}"
                )

            except Exception:
                linhas.append(
                    f"Preço: {preco} {moeda}"
                )

        linhas.append(
            f"ID: {item_id}"
        )

        linhas.append(
            ""
        )

    linhas.extend([
        "🔬 Para inspecionar todos "
        "os campos disponíveis:",
        f"/rawofertas {product_id}",
    ])

    enviar_texto_grande(
        chat_id,
        "\n".join(linhas)
    )


# =========================================================
# RAW OFERTAS
# =========================================================

def raw_ofertas(
    chat_id,
    product_id
):

    product_id = (
        product_id
        .strip()
        .upper()
    )

    if not product_id.startswith("MLB"):

        send_message(
            chat_id,
            "❌ ID inválido.\n\n"
            "Exemplo:\n"
            "/rawofertas MLB74895216"
        )

        return

    send_message(
        chat_id,
        "🧪 INSPECIONANDO OFERTAS\n\n"
        f"Produto: {product_id}\n"
        f"Rota: "
        f"/products/{product_id}/items"
    )

    resultado = meli_get(
        f"/products/{product_id}/items"
    )

    status = resultado.get(
        "status"
    )

    data = resultado.get(
        "data"
    )

    if not resultado.get("ok"):

        erro = extrair_erro(
            resultado
        )

        texto = (
            "❌ ERRO AO CONSULTAR "
            "OFERTAS\n\n"
            f"HTTP: {status}"
        )

        if erro:
            texto += (
                f"\n\n{erro}"
            )

        send_message(
            chat_id,
            texto
        )

        return

    # -----------------------------------------------------
    # CHAVES PRINCIPAIS
    # -----------------------------------------------------

    if isinstance(
        data,
        dict
    ):

        chaves = list(
            data.keys()
        )

        texto_chaves = (
            "✅ ENDPOINT RESPONDEU\n\n"
            f"HTTP: {status}\n\n"
            "Chaves principais:\n"
        )

        if chaves:

            texto_chaves += "\n".join(
                f"• {chave}"
                for chave in chaves
            )

        else:
            texto_chaves += (
                "Nenhuma chave."
            )

        enviar_texto_grande(
            chat_id,
            texto_chaves
        )

    # -----------------------------------------------------
    # LOCALIZAR LISTA DE OFERTAS
    # -----------------------------------------------------

    ofertas = []

    nome_lista = None

    if isinstance(
        data,
        dict
    ):

        candidatos = [
            "results",
            "items",
            "offers",
        ]

        for candidato in candidatos:

            valor = data.get(
                candidato
            )

            if isinstance(
                valor,
                list
            ):

                ofertas = valor
                nome_lista = candidato
                break

    elif isinstance(
        data,
        list
    ):

        ofertas = data
        nome_lista = (
            "resposta_raiz"
        )

    # -----------------------------------------------------
    # ESTRUTURA
    # -----------------------------------------------------

    send_message(
        chat_id,
        "📊 ESTRUTURA ENCONTRADA\n\n"
        f"Lista: {nome_lista}\n"
        f"Quantidade de registros: "
        f"{len(ofertas)}"
    )

    # -----------------------------------------------------
    # PRIMEIRA OFERTA COMPLETA
    # -----------------------------------------------------

    if ofertas:

        primeiro = ofertas[0]

        try:
            json_primeiro = json.dumps(
                primeiro,
                ensure_ascii=False,
                indent=2
            )

        except Exception:
            json_primeiro = str(
                primeiro
            )

        enviar_texto_grande(
            chat_id,
            "🔬 PRIMEIRA OFERTA — "
            "JSON COMPLETO\n\n"
            + json_primeiro
        )

        # -------------------------------------------------
        # CAMPOS DISPONÍVEIS
        # -------------------------------------------------

        if isinstance(
            primeiro,
            dict
        ):

            chaves_primeiro = list(
                primeiro.keys()
            )

            texto_campos = (
                "🧩 CAMPOS DISPONÍVEIS "
                "NA OFERTA\n\n"
            )

            texto_campos += "\n".join(
                f"• {campo}"
                for campo
                in chaves_primeiro
            )

            enviar_texto_grande(
                chat_id,
                texto_campos
            )

            # ---------------------------------------------
            # TIPOS DOS CAMPOS
            # ---------------------------------------------

            tipos = []

            for chave, valor in (
                primeiro.items()
            ):

                tipos.append(
                    f"• {chave}: "
                    f"{type(valor).__name__}"
                )

            enviar_texto_grande(
                chat_id,
                "🧬 TIPOS DOS CAMPOS\n\n"
                + "\n".join(tipos)
            )

    else:

        # -------------------------------------------------
        # SE NÃO LOCALIZAR A LISTA,
        # MOSTRAR JSON INTEIRO
        # -------------------------------------------------

        try:
            json_completo = json.dumps(
                data,
                ensure_ascii=False,
                indent=2
            )

        except Exception:
            json_completo = str(
                data
            )

        enviar_texto_grande(
            chat_id,
            "⚠️ Não consegui identificar "
            "automaticamente a lista "
            "de ofertas.\n\n"
            "JSON COMPLETO DA RESPOSTA:\n\n"
            + json_completo
        )


# =========================================================
# DETALHE DO PRODUTO DE CATÁLOGO
# =========================================================

def produto_catalogo(
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
        "📦 CONSULTANDO PRODUTO\n\n"
        f"{product_id}"
    )

    resultado = meli_get(
        f"/products/{product_id}"
    )

    if not resultado.get("ok"):

        send_message(
            chat_id,
            "❌ ERRO AO CONSULTAR "
            "PRODUTO\n\n"
            f"HTTP: "
            f"{resultado.get('status')}\n"
            f"{extrair_erro(resultado)}"
        )

        return

    data = resultado.get(
        "data",
        {}
    )

    nome = (
        data.get("name")
        or data.get("title")
        or "Produto"
    )

    status = data.get(
        "status",
        "?"
    )

    send_message(
        chat_id,
        "📦 PRODUTO DE CATÁLOGO\n\n"
        f"{nome}\n\n"
        f"ID: {product_id}\n"
        f"Status: {status}\n\n"
        "💰 Para localizar ofertas:\n"
        f"/ofertas {product_id}\n\n"
        "🔬 Para inspecionar o JSON "
        "das ofertas:\n"
        f"/rawofertas {product_id}"
    )


# =========================================================
# DIAGNÓSTICO
# =========================================================

def executar_diagnostico():

    resultados = []

    # -----------------------------------------------------
    # 1 - USER
    # -----------------------------------------------------

    users_me = meli_get(
        "/users/me"
    )

    resultados.append(
        (
            "1. /users/me",
            users_me
        )
    )

    user_id = None

    if users_me.get("ok"):

        user_id = (
            users_me
            .get("data", {})
            .get("id")
        )

    # -----------------------------------------------------
    # 2 - ITENS DO USUÁRIO
    # -----------------------------------------------------

    if user_id:

        seller_items = meli_get(
            f"/users/{user_id}/items/search",
            params={
                "status": "active",
                "limit": 10,
            },
        )

        resultados.append(
            (
                "2. /users/{id}/items/search",
                seller_items
            )
        )

    # -----------------------------------------------------
    # 3 - SITE SEARCH
    # -----------------------------------------------------

    site_search = meli_get(
        "/sites/MLB/search",
        params={
            "q": "Mac Mini M4",
            "limit": 10,
        },
    )

    resultados.append(
        (
            "3. /sites/MLB/search?q="
            "Mac Mini M4",
            site_search
        )
    )

    # -----------------------------------------------------
    # 4 - PRODUCTS SEARCH
    # -----------------------------------------------------

    catalog_search = meli_get(
        "/products/search",
        params={
            "site_id": "MLB",
            "status": "active",
            "q": "Mac Mini M4 16 512",
            "limit": 5,
        },
    )

    resultados.append(
        (
            "4. /products/search",
            catalog_search
        )
    )

    catalog_product_id = None

    if catalog_search.get("ok"):

        results = (
            catalog_search
            .get("data", {})
            .get("results", [])
        )

        if results:

            catalog_product_id = (
                results[0].get("id")
            )

    # -----------------------------------------------------
    # 5 - PRODUCT DETAIL
    # -----------------------------------------------------

    if catalog_product_id:

        product_detail = meli_get(
            f"/products/"
            f"{catalog_product_id}"
        )

        resultados.append(
            (
                "5. /products/"
                f"{catalog_product_id}",
                product_detail
            )
        )

    # -----------------------------------------------------
    # 6 - SITE SEARCH CATALOG
    # -----------------------------------------------------

    if catalog_product_id:

        catalog_site_search = meli_get(
            "/sites/MLB/search",
            params={
                "catalog_product_id":
                    catalog_product_id,

                "limit": 10,
            },
        )

        resultados.append(
            (
                "6. /sites/MLB/search?"
                "catalog_product_id=...",
                catalog_site_search
            )
        )

    # -----------------------------------------------------
    # 7 - PRODUCTS/{ID}/ITEMS
    # -----------------------------------------------------

    if catalog_product_id:

        ofertas = meli_get(
            f"/products/"
            f"{catalog_product_id}/items"
        )

        resultados.append(
            (
                "7. /products/{id}/items",
                ofertas
            )
        )

    return resultados


def resumo_diagnostico(
    resultados
):

    linhas = [
        "🧪 DIAGNÓSTICO MERCADO LIVRE",
        "",
    ]

    for nome, resultado in resultados:

        linhas.append(
            linha_teste(
                nome,
                resultado
            )
        )

        linhas.append("")

    linhas.extend([
        "📌 Interpretação:",
        "HTTP 200 = rota liberada.",
        "HTTP 403 = rota bloqueada "
        "por política/permissão.",
        "HTTP 404 = rota/recurso "
        "não disponível.",
    ])

    return "\n".join(
        linhas
    )


def contar_resultados(
    resultado
):

    data = resultado.get(
        "data"
    )

    if isinstance(
        data,
        list
    ):
        return len(data)

    if not isinstance(
        data,
        dict
    ):
        return None

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
            return len(valor)

    return None


def diagnostico_detalhado(
    resultados
):

    linhas = [
        "📊 RESUMO DOS RETORNOS",
        "",
    ]

    for nome, resultado in resultados:

        status = resultado.get(
            "status"
        )

        linhas.append(
            nome
        )

        linhas.append(
            f"HTTP {status}"
        )

        quantidade = contar_resultados(
            resultado
        )

        if quantidade is not None:

            linhas.append(
                f"Resultados: {quantidade}"
            )

        data = resultado.get(
            "data"
        )

        if (
            resultado.get("ok")
            and isinstance(data, dict)
        ):

            paging = data.get(
                "paging"
            )

            if isinstance(
                paging,
                dict
            ):

                total = paging.get(
                    "total"
                )

                if total is not None:

                    linhas.append(
                        "Total informado "
                        f"pela API: {total}"
                    )

        linhas.append("")

    return "\n".join(
        linhas
    )


# =========================================================
# TELEGRAM WEBHOOK
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

    # =====================================================
    # START
    # =====================================================

    if text.startswith("/start"):

        send_message(
            chat_id,
            "🤖 GARIMPEIRO PESSOAL\n\n"

            "Comandos disponíveis:\n\n"

            "/status\n"
            "/teste\n"
            "/diagnostico\n\n"

            "/buscar Mac Mini M4 16 512\n\n"

            "/produto MLB74895216\n\n"

            "/ofertas MLB74895216\n\n"

            "/rawofertas MLB74895216"
        )

    # =====================================================
    # STATUS
    # =====================================================

    elif text.startswith("/status"):

        if meli_access_token:

            status_ml = (
                "✅ Mercado Livre conectado"
            )

        else:

            status_ml = (
                "⚠️ Mercado Livre "
                "não autorizado"
            )

        send_message(
            chat_id,
            "🤖 STATUS\n\n"
            "✅ Telegram conectado\n"
            "✅ Render online\n"
            f"{status_ml}"
        )

    # =====================================================
    # TESTE
    # =====================================================

    elif text.startswith("/teste"):

        comando_teste(
            chat_id
        )

    # =====================================================
    # DIAGNÓSTICO
    # =====================================================

    elif text.startswith(
        "/diagnostico"
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

        send_message(
            chat_id,
            "🧪 Iniciando diagnóstico "
            "das rotas...\n\n"

            "Isso pode levar "
            "alguns segundos."
        )

        resultados = (
            executar_diagnostico()
        )

        enviar_texto_grande(
            chat_id,
            resumo_diagnostico(
                resultados
            )
        )

        enviar_texto_grande(
            chat_id,
            diagnostico_detalhado(
                resultados
            )
        )

    # =====================================================
    # BUSCAR
    # =====================================================

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
                "Use:\n"
                "/buscar Mac Mini M4 16 512"
            )

            return "OK", 200

        termo = partes[1].strip()

        buscar_catalogo(
            chat_id,
            termo
        )

    # =====================================================
    # PRODUTO
    # =====================================================

    elif text.startswith("/produto"):

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
                "Use:\n"
                "/produto MLB74895216"
            )

            return "OK", 200

        product_id = (
            partes[1].strip()
        )

        produto_catalogo(
            chat_id,
            product_id
        )

    # =====================================================
    # OFERTAS
    # =====================================================

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
                "Use:\n"
                "/ofertas MLB74895216"
            )

            return "OK", 200

        product_id = (
            partes[1].strip()
        )

        buscar_ofertas(
            chat_id,
            product_id
        )

    # =====================================================
    # RAW OFERTAS
    # =====================================================

    elif text.startswith(
        "/rawofertas"
    ):

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
                "Use:\n"
                "/rawofertas MLB74895216"
            )

            return "OK", 200

        product_id = (
            partes[1].strip()
        )

        raw_ofertas(
            chat_id,
            product_id
        )

    # =====================================================
    # COMANDO DESCONHECIDO
    # =====================================================

    else:

        send_message(
            chat_id,
            "🤖 Comando não reconhecido.\n\n"

            "Use /start para "
            "ver os comandos."
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
