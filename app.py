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

MELI_API = "https://api.mercadolibre.com"
MELI_TOKEN_URL = f"{MELI_API}/oauth/token"

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
    except Exception:
        return "—"


def calcular_desconto(preco, original):
    try:
        preco = Decimal(str(preco))
        original = Decimal(str(original))

        if original <= 0 or preco >= original:
            return None

        return round(
            float(((original - preco) / original) * 100),
            1
        )

    except Exception:
        return None


def send_message(chat_id, text):
    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
    except Exception:
        pass


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
            data = {
                "raw": response.text[:1500]
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
        return "Erro ao comunicar com o Mercado Livre.", 500

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
        "Volte ao Telegram e use /buscar.",
        200,
    )


# =========================================================
# CATÁLOGO
# =========================================================

def buscar_catalogo(termo, limit=20):
    return meli_get(
        "/products/search",
        params={
            "status": "active",
            "site_id": "MLB",
            "q": termo,
            "limit": limit,
        },
    )


# =========================================================
# BUSCA DE ANÚNCIOS
# =========================================================

def buscar_anuncios_por_texto(termo, limit=50):
    """
    Busca anúncios comerciais reais no Mercado Livre Brasil.
    """

    return meli_get(
        "/sites/MLB/search",
        params={
            "q": termo,
            "limit": limit,
        },
    )


def buscar_anuncios_por_catalogo(product_id, limit=50):
    """
    Tenta localizar anúncios ligados diretamente
    ao catalog_product_id.
    """

    return meli_get(
        "/sites/MLB/search",
        params={
            "catalog_product_id": product_id,
            "limit": limit,
        },
    )


# =========================================================
# ITEM INDIVIDUAL
# =========================================================

def obter_item(item_id):
    return meli_get(
        f"/items/{item_id}"
    )


# =========================================================
# NORMALIZAÇÃO
# =========================================================

def normalizar_item(item):
    if "body" in item:
        item = item.get("body") or {}

    shipping = item.get("shipping") or {}
    installments = item.get("installments") or {}

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
        pass

    return {
        "item_id": item.get("id"),
        "titulo": item.get("title") or "Produto",
        "preco": preco,
        "preco_original": original,
        "desconto": calcular_desconto(
            preco,
            original
        ),
        "parcelas": parcelas,
        "valor_parcela": valor_parcela,
        "total_parcelado": total_parcelado,
        "sem_juros": installments.get("rate") in (0, 0.0),
        "frete_gratis": shipping.get("free_shipping", False),
        "condicao": item.get("condition"),
        "seller_id": item.get("seller_id"),
        "catalog_product_id": item.get("catalog_product_id"),
        "official_store_id": item.get("official_store_id"),
        "link": item.get("permalink"),
        "estoque": item.get("available_quantity"),
    }


# =========================================================
# FILTROS
# =========================================================

def palavras_relevantes(termo):
    ignorar = {
        "de", "da", "do", "e", "com",
        "-", "/", "apple"
    }

    return [
        p.lower()
        for p in termo.split()
        if len(p) > 1
        and p.lower() not in ignorar
    ]


def item_relevante(item, termo):
    titulo = (
        item.get("titulo")
        or ""
    ).lower()

    palavras = palavras_relevantes(termo)

    if not palavras:
        return True

    acertos = sum(
        1 for palavra in palavras
        if palavra in titulo
    )

    return acertos >= max(
        2,
        len(palavras) // 2
    )


# =========================================================
# RANKING
# =========================================================

def score_oferta(oferta):
    try:
        preco = Decimal(
            str(oferta.get("preco"))
        )
    except Exception:
        return Decimal("999999999")

    score = preco

    if oferta.get("frete_gratis"):
        score -= Decimal("10")

    if oferta.get("sem_juros"):
        score -= Decimal("5")

    if oferta.get("official_store_id"):
        score -= Decimal("3")

    return score


# =========================================================
# APRESENTAÇÃO
# =========================================================

def mensagem_oferta(oferta, posicao):
    linhas = []

    if posicao == 1:
        linhas.append("🏆 MELHOR OFERTA")
    else:
        linhas.append(f"🔹 OFERTA #{posicao}")

    linhas.append("")
    linhas.append(oferta["titulo"])
    linhas.append("")

    linhas.append(
        f"💰 Preço: {brl(oferta['preco'])}"
    )

    if oferta.get("preco_original"):
        linhas.append(
            f"Antes: {brl(oferta['preco_original'])}"
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
                "Total parcelado: "
                f"{brl(oferta['total_parcelado'])}"
            )

    if oferta.get("frete_gratis"):
        linhas.append(
            "🚚 Frete grátis"
        )

    condicao = oferta.get("condicao")

    if condicao:
        traduzido = {
            "new": "Novo",
            "used": "Usado",
        }.get(
            condicao,
            condicao
        )

        linhas.append(
            f"📦 Condição: {traduzido}"
        )

    if oferta.get("official_store_id"):
        linhas.append(
            "🏪 Loja oficial"
        )

    if oferta.get("seller_id"):
        linhas.append(
            f"👤 Vendedor: {oferta['seller_id']}"
        )

    if oferta.get("estoque") is not None:
        linhas.append(
            f"📦 Estoque: {oferta['estoque']}"
        )

    if oferta.get("link"):
        linhas.append("")
        linhas.append(
            f"🔗 {oferta['link']}"
        )

    return "\n".join(linhas)


# =========================================================
# MOTOR DE BUSCA COMPLETO
# =========================================================

def garimpar_ofertas(termo):
    # 1. Primeiro tenta busca direta dos anúncios
    resultado = buscar_anuncios_por_texto(
        termo,
        limit=50,
    )

    ofertas = []

    if resultado.get("ok"):
        resultados = (
            resultado
            .get("data", {})
            .get("results", [])
        )

        for item in resultados:
            oferta = normalizar_item(item)

            if (
                oferta.get("preco") is not None
                and item_relevante(
                    oferta,
                    termo
                )
            ):
                ofertas.append(oferta)

    # 2. Se não encontrou, tenta catálogo
    if not ofertas:
        catalogo = buscar_catalogo(
            termo,
            limit=10,
        )

        if catalogo.get("ok"):
            produtos = (
                catalogo
                .get("data", {})
                .get("results", [])
            )

            for produto in produtos[:3]:
                product_id = produto.get("id")

                if not product_id:
                    continue

                anuncios = buscar_anuncios_por_catalogo(
                    product_id,
                    limit=50,
                )

                if not anuncios.get("ok"):
                    continue

                resultados = (
                    anuncios
                    .get("data", {})
                    .get("results", [])
                )

                for item in resultados:
                    oferta = normalizar_item(item)

                    if (
                        oferta.get("preco") is not None
                        and item_relevante(
                            oferta,
                            termo
                        )
                    ):
                        ofertas.append(oferta)

    # remove duplicados
    unicos = {}

    for oferta in ofertas:
        item_id = oferta.get("item_id")

        if item_id:
            unicos[item_id] = oferta

    ofertas = list(
        unicos.values()
    )

    ofertas.sort(
        key=score_oferta
    )

    return ofertas


# =========================================================
# TELEGRAM
# =========================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(
        silent=True
    ) or {}

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
            "Agora busco anúncios reais, "
            "preço e parcelamento.\n\n"
            "Use:\n"
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
            "✅ Telegram conectado\n"
            "✅ Render online\n"
            f"{status}"
        )

    # -----------------------------------------------------

    elif text.startswith("/buscar"):
        termo = (
            text
            .replace(
                "/buscar",
                "",
                1
            )
            .strip()
        )

        if not termo:
            send_message(
                chat_id,
                "Use:\n"
                "/buscar Mac Mini M4 16 512"
            )

            return "OK", 200

        if not meli_access_token:
            send_message(
                chat_id,
                "⚠️ Mercado Livre não autorizado."
            )

            return "OK", 200

        send_message(
            chat_id,
            "🔎 GARIMPANDO OFERTAS...\n\n"
            f"{termo}"
        )

        ofertas = garimpar_ofertas(
            termo
        )

        if not ofertas:
            send_message(
                chat_id,
                "❌ Não encontrei anúncios "
                "com preço para essa busca."
            )

            return "OK", 200

        send_message(
            chat_id,
            f"✅ {len(ofertas)} ofertas "
            "relevantes encontradas.\n\n"
            "Mostrando as melhores:"
        )

        for i, oferta in enumerate(
            ofertas[:10],
            start=1,
        ):
            send_message(
                chat_id,
                mensagem_oferta(
                    oferta,
                    i
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
