import os
import json
from urllib.parse import quote

import requests
from flask import Flask, request, redirect


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

MELI_API = "https://api.mercadolibre.com"
MELI_TOKEN_URL = f"{MELI_API}/oauth/token"

# Tokens obtidos pelo OAuth.
# Por enquanto ficam em memória.
meli_access_token = None
meli_refresh_token = None


# =========================================================
# UTILITÁRIOS
# =========================================================

def cortar(texto, limite=3500):
    texto = str(texto)

    if len(texto) <= limite:
        return texto

    return texto[:limite] + "\n\n...[conteúdo cortado]"


def json_seguro(response):
    try:
        return response.json()
    except Exception:
        return {
            "raw_response": response.text[:2000]
        }


def formatar_dinheiro(valor):
    try:
        valor = float(valor)
        return (
            f"R$ {valor:,.2f}"
            .replace(",", "X")
            .replace(".", ",")
            .replace("X", ".")
        )
    except Exception:
        return "Não informado"


# =========================================================
# TELEGRAM
# =========================================================

def send_message(chat_id, text):
    if not TELEGRAM_TOKEN:
        return False

    try:
        response = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": cortar(text, 4000),
                "disable_web_page_preview": True,
            },
            timeout=20,
        )

        return response.status_code == 200

    except Exception as exc:
        print("Erro Telegram:", exc)
        return False


# =========================================================
# MERCADO LIVRE - REQUEST
# =========================================================

def meli_get(endpoint, params=None, usar_token=True):
    headers = {
        "Accept": "application/json",
        "User-Agent": "GarimpeiroPessoal/1.0",
    }

    if usar_token:
        if not meli_access_token:
            return {
                "ok": False,
                "status": None,
                "data": {},
                "error": "TOKEN_NOT_AVAILABLE",
                "message": "Mercado Livre não autorizado.",
            }

        headers["Authorization"] = (
            f"Bearer {meli_access_token}"
        )

    try:
        response = requests.get(
            f"{MELI_API}{endpoint}",
            headers=headers,
            params=params,
            timeout=25,
        )

        data = json_seguro(response)

        return {
            "ok": response.status_code == 200,
            "status": response.status_code,
            "data": data,
            "url": response.url,
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
        "Garimpeiro Pessoal online! 🤖<br><br>"
        "OAuth: /oauth/login<br>"
        "Webhook: /webhook",
        200,
    )


# =========================================================
# OAUTH
# =========================================================

@app.route("/oauth/login")
def oauth_login():
    if not MELI_CLIENT_ID:
        return "MELI_CLIENT_ID não configurado.", 500

    authorization_url = (
        "https://auth.mercadolivre.com.br/authorization"
        "?response_type=code"
        f"&client_id={MELI_CLIENT_ID}"
        f"&redirect_uri={quote(MELI_REDIRECT_URI, safe='')}"
    )

    return redirect(authorization_url)


@app.route("/oauth/callback")
def oauth_callback():
    global meli_access_token
    global meli_refresh_token

    code = request.args.get("code")

    if not code:
        return "Código OAuth não recebido.", 400

    try:
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

        data = json_seguro(response)

    except Exception as exc:
        return (
            f"Erro ao comunicar com Mercado Livre: {exc}",
            500,
        )

    if response.status_code != 200:
        return (
            f"Erro OAuth HTTP {response.status_code}<br>"
            f"{json.dumps(data, ensure_ascii=False)}",
            400,
        )

    meli_access_token = data.get("access_token")
    meli_refresh_token = data.get("refresh_token")

    if not meli_access_token:
        return "Access Token não recebido.", 400

    return (
        "✅ Mercado Livre conectado ao Garimpeiro Pessoal!"
        "<br><br>"
        "🔐 Access Token recebido."
        "<br><br>"
        "Volte ao Telegram e envie <b>/teste</b>.",
        200,
    )


# =========================================================
# REFRESH TOKEN
# =========================================================

def renovar_token():
    global meli_access_token
    global meli_refresh_token

    if not meli_refresh_token:
        return False

    try:
        response = requests.post(
            MELI_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": MELI_CLIENT_ID,
                "client_secret": MELI_CLIENT_SECRET,
                "refresh_token": meli_refresh_token,
            },
            timeout=25,
        )

        data = json_seguro(response)

        if response.status_code != 200:
            return False

        novo_access = data.get("access_token")

        if not novo_access:
            return False

        meli_access_token = novo_access

        novo_refresh = data.get("refresh_token")

        if novo_refresh:
            meli_refresh_token = novo_refresh

        return True

    except Exception:
        return False


# =========================================================
# TESTE DE AUTENTICAÇÃO
# =========================================================

def testar_autenticacao():
    resultado = meli_get("/users/me")

    if (
        resultado.get("status") == 401
        and renovar_token()
    ):
        resultado = meli_get("/users/me")

    return resultado


# =========================================================
# ERROS
# =========================================================

def extrair_erro(resultado):
    data = resultado.get("data") or {}

    if not isinstance(data, dict):
        return ""

    partes = []

    error = data.get("error")
    message = data.get("message")
    cause = data.get("cause")

    if error:
        partes.append(f"error={error}")

    if message:
        partes.append(f"message={message}")

    if cause:
        try:
            texto = json.dumps(
                cause,
                ensure_ascii=False,
            )

            if len(texto) > 500:
                texto = texto[:500] + "..."

            partes.append(
                f"cause={texto}"
            )
        except Exception:
            pass

    return "\n".join(partes)


def resultado_curto(nome, resultado):
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

    erro = extrair_erro(resultado)

    if erro:
        texto += f"\n{erro}"

    return texto


# =========================================================
# BUSCA DE PRODUTOS DE CATÁLOGO
# =========================================================

def buscar_catalogo(termo, limite=10):
    return meli_get(
        "/products/search",
        params={
            "site_id": "MLB",
            "status": "active",
            "q": termo,
            "limit": limite,
        },
    )


def extrair_produtos_catalogo(resultado):
    if not resultado.get("ok"):
        return []

    data = resultado.get("data", {})

    if not isinstance(data, dict):
        return []

    results = data.get("results", [])

    if not isinstance(results, list):
        return []

    return results


# =========================================================
# DETALHE DO PRODUTO DE CATÁLOGO
# =========================================================

def detalhe_catalogo(product_id):
    return meli_get(
        f"/products/{product_id}"
    )


# =========================================================
# DIAGNÓSTICO DE ROTAS DE OFERTAS
# =========================================================

def diagnosticar_ofertas(product_id):
    testes = []

    rotas = [
        (
            "A. /products/{id}/items",
            f"/products/{product_id}/items",
            None,
        ),
        (
            "B. /products/{id}/items?site_id=MLB",
            f"/products/{product_id}/items",
            {
                "site_id": "MLB",
                "limit": 20,
            },
        ),
        (
            "C. /sites/MLB/search?catalog_product_id",
            "/sites/MLB/search",
            {
                "catalog_product_id": product_id,
                "limit": 20,
            },
        ),
        (
            "D. /sites/MLB/search?catalog_product_id + token",
            "/sites/MLB/search",
            {
                "catalog_product_id": product_id,
                "limit": 50,
                "offset": 0,
            },
        ),
    ]

    for nome, endpoint, params in rotas:
        resultado = meli_get(
            endpoint,
            params=params,
        )

        testes.append(
            (nome, resultado)
        )

    return testes


# =========================================================
# EXTRAÇÃO GENÉRICA DE ANÚNCIOS
# =========================================================

def localizar_listas(data):
    listas = []

    if isinstance(data, list):
        listas.append(data)
        return listas

    if not isinstance(data, dict):
        return listas

    chaves = [
        "results",
        "items",
        "offers",
        "available_items",
        "buy_box_winner",
    ]

    for chave in chaves:
        valor = data.get(chave)

        if isinstance(valor, list):
            listas.append(valor)

        elif isinstance(valor, dict):
            listas.append([valor])

    return listas


def extrair_ofertas(resultado):
    if not resultado.get("ok"):
        return []

    data = resultado.get("data")

    listas = localizar_listas(data)

    ofertas = []

    for lista in listas:
        for item in lista:
            if not isinstance(item, dict):
                continue

            item_id = (
                item.get("id")
                or item.get("item_id")
            )

            titulo = (
                item.get("title")
                or item.get("name")
                or "Produto"
            )

            preco = (
                item.get("price")
                or item.get("sale_price")
            )

            permalink = (
                item.get("permalink")
                or item.get("url")
            )

            seller = item.get("seller")

            ofertas.append({
                "id": item_id,
                "title": titulo,
                "price": preco,
                "permalink": permalink,
                "seller": seller,
                "raw": item,
            })

    return ofertas


# =========================================================
# TESTE DE ITEM INDIVIDUAL
# =========================================================

def detalhe_item(item_id):
    return meli_get(
        f"/items/{item_id}"
    )


# =========================================================
# PREÇO / PARCELAMENTO
# =========================================================

def extrair_dados_item(data):
    if not isinstance(data, dict):
        return {}

    preco = data.get("price")

    original_price = data.get(
        "original_price"
    )

    titulo = data.get("title")

    permalink = data.get("permalink")

    item_id = data.get("id")

    seller_id = data.get("seller_id")

    listing_type = data.get(
        "listing_type_id"
    )

    shipping = data.get(
        "shipping",
        {}
    )

    frete_gratis = None

    if isinstance(shipping, dict):
        frete_gratis = shipping.get(
            "free_shipping"
        )

    installments = data.get(
        "installments"
    )

    return {
        "id": item_id,
        "title": titulo,
        "price": preco,
        "original_price": original_price,
        "permalink": permalink,
        "seller_id": seller_id,
        "listing_type": listing_type,
        "free_shipping": frete_gratis,
        "installments": installments,
    }


# =========================================================
# DIAGNÓSTICO PRINCIPAL
# =========================================================

def executar_diagnostico():
    resultados = []

    users_me = meli_get(
        "/users/me"
    )

    resultados.append(
        (
            "1. /users/me",
            users_me,
        )
    )

    user_id = None

    if users_me.get("ok"):
        user_id = (
            users_me
            .get("data", {})
            .get("id")
        )

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
                seller_items,
            )
        )

    site_search = meli_get(
        "/sites/MLB/search",
        params={
            "q": "Mac Mini M4",
            "limit": 10,
        },
    )

    resultados.append(
        (
            "3. /sites/MLB/search",
            site_search,
        )
    )

    catalog_search = buscar_catalogo(
        "Mac Mini M4 16 512",
        5,
    )

    resultados.append(
        (
            "4. /products/search",
            catalog_search,
        )
    )

    produtos = extrair_produtos_catalogo(
        catalog_search
    )

    if produtos:
        product_id = produtos[0].get("id")

        if product_id:
            detalhe = detalhe_catalogo(
                product_id
            )

            resultados.append(
                (
                    f"5. /products/{product_id}",
                    detalhe,
                )
            )

            testes_ofertas = (
                diagnosticar_ofertas(
                    product_id
                )
            )

            for nome, resultado in testes_ofertas:
                resultados.append(
                    (
                        f"6. {nome}",
                        resultado,
                    )
                )

    return resultados


# =========================================================
# FORMATAR DIAGNÓSTICO
# =========================================================

def formatar_diagnostico(resultados):
    linhas = [
        "🧪 DIAGNÓSTICO MERCADO LIVRE",
        "",
    ]

    for nome, resultado in resultados:
        linhas.append(
            resultado_curto(
                nome,
                resultado,
            )
        )

        data = resultado.get("data")

        if isinstance(data, dict):
            for chave in [
                "results",
                "items",
                "offers",
            ]:
                valor = data.get(chave)

                if isinstance(valor, list):
                    linhas.append(
                        f"{chave}: {len(valor)}"
                    )

            paging = data.get("paging")

            if isinstance(paging, dict):
                total = paging.get("total")

                if total is not None:
                    linhas.append(
                        f"Total API: {total}"
                    )

        linhas.append("")

    return "\n".join(linhas)


# =========================================================
# COMANDO /BUSCAR
# =========================================================

def comando_buscar(chat_id, termo):
    send_message(
        chat_id,
        "🔎 BUSCANDO NO CATÁLOGO\n\n"
        f"{termo}",
    )

    resultado = buscar_catalogo(
        termo,
        10,
    )

    if not resultado.get("ok"):
        send_message(
            chat_id,
            "❌ Erro na busca.\n\n"
            f"HTTP: {resultado.get('status')}\n"
            f"{extrair_erro(resultado)}",
        )
        return

    produtos = extrair_produtos_catalogo(
        resultado
    )

    if not produtos:
        send_message(
            chat_id,
            "❌ Nenhum produto encontrado.",
        )
        return

    linhas = [
        "📦 PRODUTOS DE CATÁLOGO",
        "",
    ]

    for i, produto in enumerate(
        produtos[:10],
        start=1,
    ):
        product_id = produto.get("id")
        nome = (
            produto.get("name")
            or produto.get("title")
            or "Produto"
        )

        linhas.append(
            f"{i}. {nome}"
        )
        linhas.append(
            f"ID: {product_id}"
        )
        linhas.append(
            f"/ofertas {product_id}"
        )
        linhas.append("")

    send_message(
        chat_id,
        "\n".join(linhas),
    )


# =========================================================
# COMANDO /OFERTAS
# =========================================================

def comando_ofertas(chat_id, product_id):
    send_message(
        chat_id,
        "🕵️ PROCURANDO OFERTAS\n\n"
        f"Produto: {product_id}",
    )

    testes = diagnosticar_ofertas(
        product_id
    )

    encontrou = False

    for nome, resultado in testes:
        status = resultado.get("status")

        ofertas = extrair_ofertas(
            resultado
        )

        if ofertas:
            encontrou = True

            linhas = [
                "💰 OFERTAS ENCONTRADAS",
                "",
                f"Rota: {nome}",
                f"HTTP: {status}",
                "",
            ]

            for i, oferta in enumerate(
                ofertas[:10],
                start=1,
            ):
                linhas.append(
                    f"{i}. {oferta.get('title')}"
                )

                linhas.append(
                    f"Preço: "
                    f"{formatar_dinheiro(oferta.get('price'))}"
                )

                item_id = oferta.get("id")

                if item_id:
                    linhas.append(
                        f"ID: {item_id}"
                    )
                    linhas.append(
                        f"/item {item_id}"
                    )

                permalink = oferta.get(
                    "permalink"
                )

                if permalink:
                    linhas.append(
                        permalink
                    )

                linhas.append("")

            send_message(
                chat_id,
                "\n".join(linhas),
            )

            break

    if encontrou:
        return

    linhas = [
        "⚠️ NENHUMA ROTA DEVOLVEU "
        "OFERTAS COM PREÇO",
        "",
        "Resultado dos testes:",
        "",
    ]

    for nome, resultado in testes:
        linhas.append(nome)
        linhas.append(
            f"HTTP: {resultado.get('status')}"
        )

        erro = extrair_erro(resultado)

        if erro:
            linhas.append(erro)

        data = resultado.get("data")

        if isinstance(data, dict):
            linhas.append(
                "Chaves retornadas: "
                + ", ".join(
                    list(data.keys())[:15]
                )
            )

        linhas.append("")

    send_message(
        chat_id,
        "\n".join(linhas),
    )


# =========================================================
# COMANDO /ITEM
# =========================================================

def comando_item(chat_id, item_id):
    send_message(
        chat_id,
        "📦 CONSULTANDO ANÚNCIO\n\n"
        f"{item_id}",
    )

    resultado = detalhe_item(
        item_id
    )

    if not resultado.get("ok"):
        send_message(
            chat_id,
            "❌ Não foi possível consultar "
            "o anúncio.\n\n"
            f"HTTP: {resultado.get('status')}\n"
            f"{extrair_erro(resultado)}",
        )
        return

    dados = extrair_dados_item(
        resultado.get("data")
    )

    linhas = [
        "💰 DETALHES DA OFERTA",
        "",
        dados.get("title") or "Produto",
        "",
        f"Preço atual: "
        f"{formatar_dinheiro(dados.get('price'))}",
    ]

    original = dados.get(
        "original_price"
    )

    if original:
        linhas.append(
            f"Preço original: "
            f"{formatar_dinheiro(original)}"
        )

        try:
            atual = float(
                dados.get("price")
            )

            original_float = float(
                original
            )

            if original_float > 0:
                desconto = (
                    (
                        original_float - atual
                    )
                    / original_float
                    * 100
                )

                linhas.append(
                    f"Desconto: {desconto:.1f}%"
                )

        except Exception:
            pass

    if dados.get("free_shipping") is True:
        linhas.append(
            "🚚 Frete grátis"
        )

    linhas.append(
        f"ID: {dados.get('id')}"
    )

    if dados.get("permalink"):
        linhas.append("")
        linhas.append(
            dados.get("permalink")
        )

    send_message(
        chat_id,
        "\n".join(linhas),
    )


# =========================================================
# WEBHOOK
# =========================================================

@app.route(
    "/webhook",
    methods=["POST"],
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
    # START
    # -----------------------------------------------------

    if text.startswith("/start"):
        send_message(
            chat_id,
            "🤖 GARIMPEIRO PESSOAL\n\n"
            "Mercado Livre - modo de desenvolvimento\n\n"
            "Comandos:\n\n"
            "/status\n"
            "/teste\n"
            "/diagnostico\n"
            "/buscar Mac Mini M4 16 512\n"
            "/ofertas ID_DO_PRODUTO\n"
            "/item ID_DO_ANUNCIO",
        )

    # -----------------------------------------------------
    # STATUS
    # -----------------------------------------------------

    elif text.startswith("/status"):
        if meli_access_token:
            ml = (
                "✅ Mercado Livre conectado"
            )
        else:
            ml = (
                "⚠️ Mercado Livre não autorizado"
            )

        send_message(
            chat_id,
            "🤖 STATUS DO GARIMPEIRO\n\n"
            "✅ Telegram conectado\n"
            "✅ Render online\n"
            f"{ml}",
        )

    # -----------------------------------------------------
    # TESTE
    # -----------------------------------------------------

    elif text.startswith("/teste"):
        if not meli_access_token:
            send_message(
                chat_id,
                "⚠️ Mercado Livre não autorizado.\n\n"
                "Abra no navegador:\n"
                "https://garimpeiro-pessoal.onrender.com"
                "/oauth/login",
            )

            return "OK", 200

        send_message(
            chat_id,
            "🧪 Testando autenticação...",
        )

        resultado = testar_autenticacao()

        if resultado.get("ok"):
            data = resultado.get(
                "data",
                {}
            )

            send_message(
                chat_id,
                "✅ AUTENTICAÇÃO OK!\n\n"
                f"HTTP: {resultado.get('status')}\n"
                f"User ID: {data.get('id')}\n"
                f"Nickname: {data.get('nickname')}\n"
                f"Site: {data.get('site_id')}\n\n"
                "✅ OAuth válido\n"
                "✅ Access Token válido\n"
                "✅ API acessível",
            )

        else:
            send_message(
                chat_id,
                "❌ TESTE FALHOU\n\n"
                f"HTTP: {resultado.get('status')}\n"
                f"{extrair_erro(resultado)}",
            )

    # -----------------------------------------------------
    # DIAGNÓSTICO
    # -----------------------------------------------------

    elif text.startswith("/diagnostico"):
        if not meli_access_token:
            send_message(
                chat_id,
                "⚠️ Mercado Livre não autorizado.\n\n"
                "Abra:\n"
                "https://garimpeiro-pessoal.onrender.com"
                "/oauth/login",
            )

            return "OK", 200

        send_message(
            chat_id,
            "🧪 TESTANDO ROTAS\n\n"
            "Agora vou testar inclusive possíveis "
            "rotas de ofertas.",
        )

        resultados = executar_diagnostico()

        texto = formatar_diagnostico(
            resultados
        )

        # Telegram tem limite de tamanho.
        for inicio in range(
            0,
            len(texto),
            3800,
        ):
            send_message(
                chat_id,
                texto[
                    inicio:
                    inicio + 3800
                ],
            )

    # -----------------------------------------------------
    # BUSCAR
    # -----------------------------------------------------

    elif text.startswith("/buscar"):
        if not meli_access_token:
            send_message(
                chat_id,
                "⚠️ Mercado Livre não autorizado.",
            )

            return "OK", 200

        termo = (
            text[len("/buscar"):]
            .strip()
        )

        if not termo:
            send_message(
                chat_id,
                "Use:\n"
                "/buscar Mac Mini M4 16 512",
            )

        else:
            comando_buscar(
                chat_id,
                termo,
            )

    # -----------------------------------------------------
    # OFERTAS
    # -----------------------------------------------------

    elif text.startswith("/ofertas"):
        if not meli_access_token:
            send_message(
                chat_id,
                "⚠️ Mercado Livre não autorizado.",
            )

            return "OK", 200

        partes = text.split(
            maxsplit=1
        )

        if len(partes) < 2:
            send_message(
                chat_id,
                "Use:\n"
                "/ofertas MLB74895216",
            )

        else:
            product_id = (
                partes[1]
                .strip()
                .upper()
            )

            comando_ofertas(
                chat_id,
                product_id,
            )

    # -----------------------------------------------------
    # ITEM
    # -----------------------------------------------------

    elif text.startswith("/item"):
        if not meli_access_token:
            send_message(
                chat_id,
                "⚠️ Mercado Livre não autorizado.",
            )

            return "OK", 200

        partes = text.split(
            maxsplit=1
        )

        if len(partes) < 2:
            send_message(
                chat_id,
                "Use:\n"
                "/item MLB123456789",
            )

        else:
            item_id = (
                partes[1]
                .strip()
                .upper()
            )

            comando_item(
                chat_id,
                item_id,
            )

    return "OK", 200


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    port = int(
        os.environ.get(
            "PORT",
            10000,
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )
