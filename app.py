import os

import json

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

MELI_API = "https://api.mercadolibre.com"

MELI_TOKEN_URL = f"{MELI_API}/oauth/token"

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

    except Exception:

        return False

# =========================================================

# MERCADO LIVRE - REQUEST

# =========================================================

def meli_get(endpoint, params=None):

    if not meli_access_token:

        return {

            "ok": False,

            "status": None,

            "data": {},

            "error": "TOKEN_NOT_AVAILABLE",

            "message": "Mercado Livre não autorizado."

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

            data = {

                "raw_response": response.text[:1500]

            }

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

            "error": "REQUEST_ERROR",

            "message": str(exc),

        }

# =========================================================

# HOME

# =========================================================

@app.route("/")

def home():

    return "Garimpeiro Pessoal online! 🤖", 200

# =========================================================

# OAUTH

# =========================================================

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

        data = response.json()

    except Exception:

        return "Erro ao comunicar com Mercado Livre.", 500

    if response.status_code != 200:

        return (

            f"Erro OAuth HTTP {response.status_code}",

            400,

        )

    meli_access_token = data.get("access_token")

    meli_refresh_token = data.get("refresh_token")

    if not meli_access_token:

        return "Access Token não recebido.", 400

    return (

        "✅ Mercado Livre conectado!<br><br>"

        "Volte ao Telegram e envie /diagnostico.",

        200,

    )

# =========================================================

# FORMATADORES

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

            texto_cause = json.dumps(

                cause,

                ensure_ascii=False

            )

            if len(texto_cause) > 300:

                texto_cause = texto_cause[:300] + "..."

            partes.append(

                f"cause={texto_cause}"

            )

        except Exception:

            pass

    return "\n".join(partes)

def linha_teste(nome, resultado):

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

# DIAGNÓSTICO

# =========================================================

def executar_diagnostico():

    resultados = []

    # -----------------------------------------------------

    # 1 - usuário autenticado

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

    # 2 - anúncios do próprio usuário

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

    # 3 - busca geral de site

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

            "3. /sites/MLB/search?q=Mac Mini M4",

            site_search

        )

    )

    # -----------------------------------------------------

    # 4 - busca do catálogo

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

    # 5 - detalhe do produto

    # -----------------------------------------------------

    if catalog_product_id:

        product_detail = meli_get(

            f"/products/{catalog_product_id}"

        )

        resultados.append(

            (

                f"5. /products/{catalog_product_id}",

                product_detail

            )

        )

    # -----------------------------------------------------

    # 6 - tentativa de busca no site por catalog_product_id

    # -----------------------------------------------------

    if catalog_product_id:

        catalog_site_search = meli_get(

            "/sites/MLB/search",

            params={

                "catalog_product_id": catalog_product_id,

                "limit": 10,

            },

        )

        resultados.append(

            (

                "6. /sites/MLB/search?catalog_product_id=...",

                catalog_site_search

            )

        )

    return resultados

def resumo_diagnostico(resultados):

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

    linhas.append(

        "📌 Interpretação:"

    )

    linhas.append(

        "HTTP 200 = rota liberada."

    )

    linhas.append(

        "HTTP 403 = rota bloqueada por política/permissão."

    )

    linhas.append(

        "HTTP 404 = rota/recurso não disponível."

    )

    return "\n".join(linhas)

# =========================================================

# EXTRA DE DIAGNÓSTICO

# =========================================================

def contar_resultados(resultado):

    data = resultado.get("data")

    if not isinstance(data, dict):

        return None

    results = data.get("results")

    if isinstance(results, list):

        return len(results)

    return None

def diagnostico_detalhado(resultados):

    linhas = [

        "📊 RESUMO DOS RETORNOS",

        "",

    ]

    for nome, resultado in resultados:

        status = resultado.get("status")

        linhas.append(

            f"{nome}"

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

        data = resultado.get("data")

        if (

            resultado.get("ok")

            and isinstance(data, dict)

        ):

            if "paging" in data:

                paging = data.get(

                    "paging",

                    {}

                )

                total = paging.get(

                    "total"

                )

                if total is not None:

                    linhas.append(

                        f"Total informado pela API: {total}"

                    )

        linhas.append("")

    return "\n".join(linhas)

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

    # -----------------------------------------------------

    if text.startswith("/start"):

        send_message(

            chat_id,

            "🤖 GARIMPEIRO PESSOAL\n\n"

            "Modo diagnóstico ativo.\n\n"

            "Comandos:\n"

            "/status\n"

            "/diagnostico"

        )

    # -----------------------------------------------------

    elif text.startswith("/status"):

        if meli_access_token:

            status_ml = (

                "✅ Mercado Livre conectado"

            )

        else:

            status_ml = (

                "⚠️ Mercado Livre não autorizado"

            )

        send_message(

            chat_id,

            "🤖 STATUS\n\n"

            "✅ Telegram conectado\n"

            "✅ Render online\n"

            f"{status_ml}"

        )

    # -----------------------------------------------------

    elif text.startswith("/diagnostico"):

        if not meli_access_token:

            send_message(

                chat_id,

                "⚠️ Mercado Livre não autorizado.\n\n"

                "Abra:\n"

                "https://garimpeiro-pessoal.onrender.com/oauth/login"

            )

            return "OK", 200

        send_message(

            chat_id,

            "🧪 Iniciando diagnóstico das rotas...\n\n"

            "Isso pode levar alguns segundos."

        )

        resultados = (

            executar_diagnostico()

        )

        send_message(

            chat_id,

            resumo_diagnostico(

                resultados

            )

        )

        send_message(

            chat_id,

            diagnostico_detalhado(

                resultados

            )

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
