"""Garimpeiro Pessoal — bot de ofertas Mercado Livre + Telegram.

Objetivos desta versão:
- validar a configuração no anúncio/variação, não apenas no produto de catálogo;
- rejeitar configurações conflitantes ou não confirmadas;
- ordenar as ofertas compatíveis pelo preço atual (ou total com frete, se configurado);
- enriquecer os cinco resultados com link, parcelamento, frete e reputação quando
  esses dados forem realmente retornados pela API;
- entregar uma única mensagem compacta e editável no Telegram;
- manter OAuth, webhook e chamadas HTTP mais resilientes e seguros.

O arquivo usa apenas Flask e requests como dependências externas.
"""

from __future__ import annotations

import html
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlencode, urlparse

import requests
from flask import Flask, redirect, request
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# =============================================================================
# CONFIGURAÇÃO
# =============================================================================


def env_bool(nome: str, padrao: bool) -> bool:
    valor = os.environ.get(nome)
    if valor is None:
        return padrao
    return valor.strip().lower() in {"1", "true", "yes", "sim", "on"}


def env_int(nome: str, padrao: int, minimo: int, maximo: int) -> int:
    try:
        valor = int(os.environ.get(nome, padrao))
    except (TypeError, ValueError):
        valor = padrao
    return max(minimo, min(maximo, valor))


def env_float(nome: str, padrao: float, minimo: float, maximo: float) -> float:
    try:
        valor = float(os.environ.get(nome, padrao))
    except (TypeError, ValueError):
        valor = padrao
    return max(minimo, min(maximo, valor))


def env_csv_int(nome: str) -> set[int]:
    saida: set[int] = set()
    for parte in os.environ.get(nome, "").split(","):
        parte = parte.strip()
        if parte:
            try:
                saida.add(int(parte))
            except ValueError:
                pass
    return saida


TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
TELEGRAM_ALLOWED_CHATS = env_csv_int("TELEGRAM_ALLOWED_CHATS")

MELI_CLIENT_ID = os.environ.get("MELI_CLIENT_ID", "").strip()
MELI_CLIENT_SECRET = os.environ.get("MELI_CLIENT_SECRET", "").strip()
MELI_API = "https://api.mercadolibre.com"
MELI_TOKEN_URL = f"{MELI_API}/oauth/token"
SITE_ID = os.environ.get("MELI_SITE_ID", "MLB").strip().upper() or "MLB"

APP_BASE_URL = os.environ.get(
    "APP_BASE_URL", "https://garimpeiro-pessoal.onrender.com"
).strip().rstrip("/")
MELI_REDIRECT_URI = os.environ.get(
    "MELI_REDIRECT_URI", f"{APP_BASE_URL}/oauth/callback"
).strip()

STRICT_CONFIGURATION = env_bool("STRICT_CONFIGURATION", True)
DEFAULT_CONDITION = os.environ.get("DEFAULT_CONDITION", "new").strip().lower()
if DEFAULT_CONDITION not in {"new", "used", "refurbished", "any"}:
    DEFAULT_CONDITION = "new"

MAX_CATALOG_RESULTS = env_int("MAX_CATALOG_RESULTS", 40, 5, 100)
MAX_CATALOG_PRODUCTS = env_int("MAX_CATALOG_PRODUCTS", 10, 1, 30)
MAX_OFFERS_PER_PRODUCT = env_int("MAX_OFFERS_PER_PRODUCT", 50, 1, 200)
MAX_OFFERS_TO_ENRICH = env_int("MAX_OFFERS_TO_ENRICH", 100, 5, 300)
MAX_RESULTS = env_int("MAX_RESULTS", 5, 1, 10)
MELI_MULTI_GET_SIZE = env_int("MELI_MULTI_GET_SIZE", 20, 1, 20)

CONNECT_TIMEOUT = env_float("CONNECT_TIMEOUT", 6.0, 1.0, 30.0)
READ_TIMEOUT = env_float("READ_TIMEOUT", 25.0, 5.0, 90.0)
HTTP_RETRIES = env_int("HTTP_RETRIES", 3, 0, 6)
SEARCH_WORKERS = env_int("SEARCH_WORKERS", 4, 1, 12)

BUYER_ZIP_CODE = re.sub(r"\D", "", os.environ.get("BUYER_ZIP_CODE", ""))
RANK_BY_TOTAL_WITH_SHIPPING = env_bool("RANK_BY_TOTAL_WITH_SHIPPING", False)
SHIPPING_ENRICH_LIMIT = env_int("SHIPPING_ENRICH_LIMIT", 10, 1, 20)

TOKEN_FILE = Path(
    os.environ.get("MELI_TOKEN_FILE", "/tmp/garimpeiro_meli_tokens.json")
)
ITEM_CACHE_TTL = env_int("ITEM_CACHE_TTL", 180, 0, 3600)
SELLER_CACHE_TTL = env_int("SELLER_CACHE_TTL", 1800, 0, 86400)

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}" if TELEGRAM_TOKEN else ""


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("garimpeiro")

app = Flask(__name__)


# =============================================================================
# HTTP, CACHE E UTILITÁRIOS GERAIS
# =============================================================================


def criar_sessao_http() -> requests.Session:
    sessao = requests.Session()
    retry_kwargs = {
        "total": HTTP_RETRIES,
        "connect": HTTP_RETRIES,
        "read": HTTP_RETRIES,
        "status": HTTP_RETRIES,
        "backoff_factor": 0.45,
        "status_forcelist": (429, 500, 502, 503, 504),
        "respect_retry_after_header": True,
        "raise_on_status": False,
    }
    try:
        retry = Retry(allowed_methods=frozenset({"GET"}), **retry_kwargs)
    except TypeError:  # urllib3 antigo
        retry = Retry(method_whitelist=frozenset({"GET"}), **retry_kwargs)
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    sessao.mount("https://", adapter)
    sessao.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "GarimpeiroPessoal/2.0",
        }
    )
    return sessao


HTTP = criar_sessao_http()
TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)


class TTLCache:
    def __init__(self, ttl: int, max_items: int = 1000):
        self.ttl = ttl
        self.max_items = max_items
        self._dados: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, chave: str) -> Any:
        if self.ttl <= 0:
            return None
        agora = time.monotonic()
        with self._lock:
            item = self._dados.get(chave)
            if not item:
                return None
            expira, valor = item
            if expira <= agora:
                self._dados.pop(chave, None)
                return None
            return valor

    def set(self, chave: str, valor: Any) -> None:
        if self.ttl <= 0:
            return
        agora = time.monotonic()
        with self._lock:
            if len(self._dados) >= self.max_items:
                expiradas = [k for k, (exp, _) in self._dados.items() if exp <= agora]
                for k in expiradas:
                    self._dados.pop(k, None)
                if len(self._dados) >= self.max_items:
                    mais_antiga = next(iter(self._dados), None)
                    if mais_antiga:
                        self._dados.pop(mais_antiga, None)
            self._dados[chave] = (agora + self.ttl, valor)


ITEM_CACHE = TTLCache(ITEM_CACHE_TTL, max_items=2000)
SELLER_CACHE = TTLCache(SELLER_CACHE_TTL, max_items=1000)


def normalizar_texto(texto: Any) -> str:
    base = unicodedata.normalize("NFKD", str(texto or ""))
    base = "".join(ch for ch in base if not unicodedata.combining(ch)).lower()
    base = re.sub(r"\bmacmini\b", "mac mini", base)
    base = re.sub(r"\bmacbookair\b", "macbook air", base)
    base = re.sub(r"\bmacbookpro\b", "macbook pro", base)
    base = re.sub(r"\bminipc\b", "mini pc", base)
    base = re.sub(r"(?<=\d)(gb|tb)\b", r" \1", base)
    base = re.sub(r"(?<=\d)(v)\b", r" \1", base)
    base = re.sub(r"[^a-z0-9.,\"]+", " ", base)
    return " ".join(base.split())


def decimal_seguro(valor: Any) -> Optional[Decimal]:
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, dict):
        for chave in ("amount", "value", "price"):
            if chave in valor:
                convertido = decimal_seguro(valor.get(chave))
                if convertido is not None:
                    return convertido
        return None
    try:
        if isinstance(valor, str):
            texto = valor.strip().replace("R$", "").replace(" ", "")
            if not texto:
                return None
            if "," in texto and "." in texto:
                texto = texto.replace(".", "").replace(",", ".")
            elif "," in texto:
                texto = texto.replace(",", ".")
            valor = texto
        numero = Decimal(str(valor))
        if not numero.is_finite():
            return None
        return numero
    except (InvalidOperation, ValueError, TypeError):
        return None


def brl(valor: Any) -> str:
    numero = decimal_seguro(valor)
    if numero is None:
        return "Não informado"
    numero = numero.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    texto = f"{numero:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {texto}"


def porcentagem_desconto(atual: Any, anterior: Any) -> Optional[Decimal]:
    preco = decimal_seguro(atual)
    original = decimal_seguro(anterior)
    if preco is None or original is None or original <= 0 or preco >= original:
        return None
    return ((original - preco) / original * 100).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )


def lista_unica(valores: Iterable[Any]) -> list[Any]:
    vistos: set[Any] = set()
    saida: list[Any] = []
    for valor in valores:
        if valor is not None and valor not in vistos:
            vistos.add(valor)
            saida.append(valor)
    return saida


def limitar_texto(texto: Any, limite: int) -> str:
    texto = " ".join(str(texto or "").split())
    if len(texto) <= limite:
        return texto
    return texto[: max(1, limite - 1)].rstrip() + "…"


def obter_dict(objeto: Any, *chaves: str) -> dict[str, Any]:
    atual = objeto
    for chave in chaves:
        if not isinstance(atual, dict):
            return {}
        atual = atual.get(chave)
    return atual if isinstance(atual, dict) else {}


def obter_lista(objeto: Any, *chaves: str) -> list[Any]:
    atual = objeto
    for chave in chaves:
        if not isinstance(atual, dict):
            return []
        atual = atual.get(chave)
    return atual if isinstance(atual, list) else []


def primeiro_texto(*valores: Any) -> str:
    for valor in valores:
        if valor is not None and str(valor).strip():
            return str(valor).strip()
    return ""


# =============================================================================
# TOKENS OAUTH — memória + arquivo opcional para sobreviver a reinícios locais
# =============================================================================


class TokenStore:
    def __init__(self, arquivo: Path):
        self.arquivo = arquivo
        self._lock = threading.RLock()
        self._access = os.environ.get("MELI_ACCESS_TOKEN", "").strip() or None
        self._refresh = os.environ.get("MELI_REFRESH_TOKEN", "").strip() or None
        self._expires_at: Optional[float] = None
        self._carregar_arquivo()

    def _carregar_arquivo(self) -> None:
        try:
            if not self.arquivo.exists():
                return
            data = json.loads(self.arquivo.read_text(encoding="utf-8"))
            if not self._access and data.get("access_token"):
                self._access = str(data["access_token"])
            if not self._refresh and data.get("refresh_token"):
                self._refresh = str(data["refresh_token"])
            if data.get("expires_at"):
                self._expires_at = float(data["expires_at"])
        except Exception as exc:
            logger.warning("Não foi possível carregar o arquivo local de tokens: %s", exc)

    def _persistir(self) -> None:
        try:
            self.arquivo.parent.mkdir(parents=True, exist_ok=True)
            temporario = self.arquivo.with_suffix(self.arquivo.suffix + ".tmp")
            conteudo = {
                "access_token": self._access,
                "refresh_token": self._refresh,
                "expires_at": self._expires_at,
                "updated_at": int(time.time()),
            }
            temporario.write_text(json.dumps(conteudo), encoding="utf-8")
            try:
                os.chmod(temporario, 0o600)
            except OSError:
                pass
            temporario.replace(self.arquivo)
        except Exception as exc:
            logger.warning("Tokens atualizados apenas em memória: %s", exc)

    def access_token(self) -> Optional[str]:
        with self._lock:
            return self._access

    def refresh_token(self) -> Optional[str]:
        with self._lock:
            return self._refresh

    def conectado(self) -> bool:
        return bool(self.access_token())

    def salvar_resposta_oauth(self, data: dict[str, Any]) -> bool:
        access = data.get("access_token")
        if not access:
            return False
        with self._lock:
            self._access = str(access)
            if data.get("refresh_token"):
                self._refresh = str(data["refresh_token"])
            expires_in = decimal_seguro(data.get("expires_in"))
            self._expires_at = (
                time.time() + float(expires_in) - 60 if expires_in else None
            )
            self._persistir()
        return True

    def renovar(self, token_que_falhou: Optional[str] = None) -> bool:
        with self._lock:
            if token_que_falhou and self._access and self._access != token_que_falhou:
                return True
            if not self._refresh or not MELI_CLIENT_ID or not MELI_CLIENT_SECRET:
                return False
            try:
                response = requests.post(
                    MELI_TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "client_id": MELI_CLIENT_ID,
                        "client_secret": MELI_CLIENT_SECRET,
                        "refresh_token": self._refresh,
                    },
                    headers={"Accept": "application/json"},
                    timeout=TIMEOUT,
                )
                try:
                    data = response.json()
                except ValueError:
                    data = {}
            except requests.RequestException as exc:
                logger.warning("Falha de rede ao renovar token Mercado Livre: %s", exc)
                return False

            if response.status_code != 200 or not data.get("access_token"):
                logger.warning("Mercado Livre recusou renovação OAuth (HTTP %s)", response.status_code)
                return False
            return self.salvar_resposta_oauth(data)


TOKENS = TokenStore(TOKEN_FILE)


# =============================================================================
# CLIENTE MERCADO LIVRE
# =============================================================================


@dataclass
class ApiResult:
    ok: bool
    status: Optional[int]
    data: Any = field(default_factory=dict)
    error: str = ""


def meli_get(
    endpoint: str,
    params: Optional[dict[str, Any]] = None,
    tentar_refresh: bool = True,
) -> ApiResult:
    token = TOKENS.access_token()
    if not token:
        return ApiResult(False, None, {}, "not_authenticated")

    try:
        response = HTTP.get(
            f"{MELI_API}{endpoint}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=TIMEOUT,
        )
        try:
            data = response.json()
        except ValueError:
            data = {}
    except requests.RequestException as exc:
        logger.warning("Falha Mercado Livre GET %s: %s", endpoint, exc)
        return ApiResult(False, None, {}, "network_error")

    if response.status_code == 401 and tentar_refresh and TOKENS.renovar(token):
        return meli_get(endpoint, params=params, tentar_refresh=False)

    ok = 200 <= response.status_code < 300
    erro = ""
    if not ok:
        if isinstance(data, dict):
            erro = primeiro_texto(data.get("error"), data.get("message"))
        logger.info("Mercado Livre GET %s retornou HTTP %s", endpoint, response.status_code)
    return ApiResult(ok, response.status_code, data, erro)


def extrair_lista_resposta(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for chave in ("results", "items", "offers"):
        valor = data.get(chave)
        if isinstance(valor, list):
            return [item for item in valor if isinstance(item, dict)]
    return []


def pesquisar_catalogo(termo: str) -> ApiResult:
    return meli_get(
        "/products/search",
        params={
            "site_id": SITE_ID,
            "status": "active",
            "q": termo,
            "limit": MAX_CATALOG_RESULTS,
        },
    )


def obter_ofertas_produto(product_id: str) -> list[dict[str, Any]]:
    todas: list[dict[str, Any]] = []
    offset = 0
    tamanho_pagina = min(50, MAX_OFFERS_PER_PRODUCT)
    ids_vistos: set[str] = set()

    while len(todas) < MAX_OFFERS_PER_PRODUCT:
        resultado = meli_get(
            f"/products/{product_id}/items",
            params={"limit": tamanho_pagina, "offset": offset},
        )
        if not resultado.ok:
            break
        pagina = extrair_lista_resposta(resultado.data)
        if not pagina:
            break

        novos = 0
        for item in pagina:
            item_id = primeiro_texto(item.get("item_id"), item.get("id"))
            chave = item_id or f"sem-id-{len(todas)}"
            if chave in ids_vistos:
                continue
            ids_vistos.add(chave)
            todas.append(item)
            novos += 1
            if len(todas) >= MAX_OFFERS_PER_PRODUCT:
                break

        paging = resultado.data.get("paging", {}) if isinstance(resultado.data, dict) else {}
        total = int(paging.get("total") or 0) if isinstance(paging, dict) else 0
        limite_real = int(paging.get("limit") or len(pagina) or tamanho_pagina)
        offset += max(1, limite_real)

        if novos == 0 or len(pagina) < max(1, limite_real) or (total and offset >= total):
            break

    return todas


def chunks(valores: list[str], tamanho: int) -> Iterable[list[str]]:
    for inicio in range(0, len(valores), tamanho):
        yield valores[inicio : inicio + tamanho]


def obter_detalhes_itens(item_ids: list[str]) -> dict[str, dict[str, Any]]:
    saida: dict[str, dict[str, Any]] = {}
    faltantes: list[str] = []

    for item_id in item_ids:
        cache = ITEM_CACHE.get(item_id)
        if isinstance(cache, dict):
            saida[item_id] = cache
        else:
            faltantes.append(item_id)

    for lote in chunks(faltantes, MELI_MULTI_GET_SIZE):
        resultado = meli_get("/items", params={"ids": ",".join(lote)})
        recebidos: set[str] = set()
        if resultado.ok and isinstance(resultado.data, list):
            for envelope in resultado.data:
                if not isinstance(envelope, dict):
                    continue
                body = envelope.get("body") if isinstance(envelope.get("body"), dict) else envelope
                codigo = envelope.get("code", 200)
                item_id = primeiro_texto(body.get("id"), body.get("item_id"))
                try:
                    codigo_http = int(codigo or 0)
                except (TypeError, ValueError):
                    codigo_http = 0
                if item_id and codigo_http == 200:
                    saida[item_id] = body
                    recebidos.add(item_id)
                    ITEM_CACHE.set(item_id, body)

        # Fallback individual: útil se a conta/API não aceitar multiget.
        for item_id in lote:
            if item_id in recebidos:
                continue
            individual = meli_get(f"/items/{item_id}")
            if individual.ok and isinstance(individual.data, dict):
                saida[item_id] = individual.data
                ITEM_CACHE.set(item_id, individual.data)

    return saida


def obter_vendedor(seller_id: Any) -> dict[str, Any]:
    chave = str(seller_id or "").strip()
    if not chave:
        return {}
    cache = SELLER_CACHE.get(chave)
    if isinstance(cache, dict):
        return cache
    resultado = meli_get(f"/users/{chave}")
    if resultado.ok and isinstance(resultado.data, dict):
        SELLER_CACHE.set(chave, resultado.data)
        return resultado.data
    return {}


# =============================================================================
# INTERPRETAÇÃO DA BUSCA
# =============================================================================


STOPWORDS = {
    "a", "o", "as", "os", "de", "da", "do", "das", "dos", "e", "em", "no",
    "na", "nos", "nas", "para", "por", "com", "sem", "um", "uma", "the",
    "of", "for", "with", "gb", "giga", "gigas", "tb", "tera", "ram", "ssd",
    "hd", "nvme", "memoria", "armazenamento", "storage", "novo", "nova",
    "usado", "usada", "recondicionado", "refurbished", "original", "cor",
}

MARKETING_WORDS = {
    "oferta", "promocao", "barato", "melhor", "preco", "frete", "gratis",
    "lacrado", "lacre", "pronta", "entrega", "envio", "imediato", "imperdivel",
}

ACCESSORY_TERMS = {
    "suporte", "base para", "stand para", "hub usb", "dock", "docking station",
    "case", "capa", "sleeve", "bolsa", "mochila", "pelicula", "skin", "adesivo",
    "carregador", "fonte para", "cabo", "adaptador", "teclado", "mouse", "bracket",
    "parede", "peca de reposicao", "apenas caixa", "caixa vazia", "manual", "controle",
    "bateria para", "cooler para", "ventoinha", "protetor", "organizador",
}

COMPUTER_HINTS = {
    "mac mini", "macbook", "imac", "notebook", "laptop", "mini pc", "desktop",
    "computador", "workstation", "chromebook", "all in one", "nuc",
}

PHONE_HINTS = {
    "iphone", "smartphone", "celular", "galaxy", "redmi", "xiaomi", "motorola",
    "moto g", "pixel", "ipad", "tablet",
}

KNOWN_FAMILIES: list[tuple[str, str]] = [
    ("mac mini", "Mac Mini"),
    ("macbook air", "MacBook Air"),
    ("macbook pro", "MacBook Pro"),
    ("imac", "iMac"),
    ("iphone", "iPhone"),
    ("ipad pro", "iPad Pro"),
    ("ipad air", "iPad Air"),
    ("ipad", "iPad"),
    ("galaxy", "Galaxy"),
]

COMMON_CAPACITIES = {4, 6, 8, 12, 16, 18, 24, 32, 36, 48, 64, 96, 128, 256, 512, 1024, 2048, 4096, 8192}


@dataclass
class SearchSpec:
    raw: str
    normalized: str
    profile: str
    family_key: str = ""
    family_label: str = ""
    identity_terms: list[str] = field(default_factory=list)
    chip: Optional[str] = None
    chip_tier: Optional[str] = None
    ram_gb: Optional[int] = None
    storage_gb: Optional[int] = None
    voltage: Optional[str] = None
    screen_inches: Optional[Decimal] = None
    condition: str = "new"

    def configuration_label(self, fallback_title: str = "") -> str:
        partes: list[str] = []
        if self.family_label:
            partes.append(self.family_label)
        elif fallback_title:
            partes.append(limitar_texto(fallback_title, 62))
        else:
            partes.append(limitar_texto(self.raw, 62))
        if self.chip:
            chip = self.chip.upper()
            if self.chip_tier:
                chip += f" {self.chip_tier.title()}"
            partes.append(chip)
        if self.ram_gb is not None:
            partes.append(f"{self.ram_gb} GB RAM")
        if self.storage_gb is not None:
            partes.append(formatar_capacidade(self.storage_gb))
        return " • ".join(lista_unica(partes))


def formatar_capacidade(gb: int) -> str:
    if gb >= 1024 and gb % 1024 == 0:
        return f"{gb // 1024} TB"
    return f"{gb} GB"


def capacidade_para_gb(numero: str, unidade: str) -> Optional[int]:
    valor = decimal_seguro(numero)
    if valor is None or valor <= 0:
        return None
    if unidade.lower().startswith("t"):
        valor *= 1024
    try:
        return int(valor.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (ValueError, InvalidOperation):
        return None


def extrair_capacidades(texto: str) -> list[int]:
    norm = normalizar_texto(texto)
    saida: list[int] = []
    padrao = r"(?<![a-z0-9])(\d+(?:[.,]\d+)?)\s*(tb|tera(?:bytes?)?|gb|giga(?:bytes?)?|gigas?)\b"
    for match in re.finditer(padrao, norm):
        unidade = "tb" if match.group(2).startswith("t") else "gb"
        valor = capacidade_para_gb(match.group(1), unidade)
        if valor:
            saida.append(valor)
    return saida


def valor_proximo_a_marcador(texto: str, marcadores: str) -> Optional[int]:
    padroes = [
        rf"(?<!\d)(\d+(?:[.,]\d+)?)\s*(tb|gb)?\s*(?:de\s+)?(?:{marcadores})\b",
        rf"\b(?:{marcadores})\s*(?:de\s+)?(\d+(?:[.,]\d+)?)\s*(tb|gb)?\b",
    ]
    for padrao in padroes:
        match = re.search(padrao, texto)
        if not match:
            continue
        unidade = (match.group(2) or "gb").lower()
        valor = capacidade_para_gb(match.group(1), unidade)
        if valor:
            return valor
    return None


def numeros_soltos_busca(texto: str) -> list[tuple[int, int]]:
    saida: list[tuple[int, int]] = []
    for match in re.finditer(r"(?<![a-z0-9])(\d{1,4})(?![a-z0-9])", texto):
        valor = int(match.group(1))
        depois = texto[match.end() : match.end() + 12]
        antes = texto[max(0, match.start() - 12) : match.start()]
        if re.match(r"\s*(?:gb|tb)\b", depois):
            continue
        if re.match(r'\s*(?:v(?:olts?)?|polegadas?|pol\b|")', depois):
            continue
        if re.search(r"(iphone|galaxy|moto|redmi|pixel)\s*$", antes) and valor < 100:
            continue
        if valor in COMMON_CAPACITIES:
            saida.append((match.start(), valor))
    return saida


def parse_search_spec(termo: str) -> SearchSpec:
    raw = " ".join(str(termo or "").split()).strip()
    norm = normalizar_texto(raw)
    profile = "generic"
    if any(dica in norm for dica in COMPUTER_HINTS):
        profile = "computer"
    elif any(dica in norm for dica in PHONE_HINTS):
        profile = "phone"

    family_key = ""
    family_label = ""
    for chave, rotulo in KNOWN_FAMILIES:
        if chave in norm:
            family_key, family_label = chave, rotulo
            break

    chip_match = re.search(r"\bm\s*([1-9])\b", norm)
    chip = f"m{chip_match.group(1)}" if chip_match else None
    chip_tier = None
    if chip:
        tier_match = re.search(rf"\b{re.escape(chip)}\s+(pro|max|ultra)\b", norm)
        if tier_match:
            chip_tier = tier_match.group(1)

    ram = valor_proximo_a_marcador(
        norm, r"ram|memoria(?:\s+ram)?|memoria\s+unificada|unified\s+memory"
    )
    storage = valor_proximo_a_marcador(
        norm, r"ssd|nvme|armazenamento|storage|memoria\s+interna|internal\s+memory|rom|hd"
    )

    # TB sem marcador quase sempre é armazenamento.
    if storage is None:
        tb = re.search(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(tb|tera(?:bytes?)?)\b", norm)
        if tb:
            storage = capacidade_para_gb(tb.group(1), "tb")

    numeros = numeros_soltos_busca(norm)
    valores = lista_unica([valor for _, valor in numeros] + extrair_capacidades(norm))
    if storage is None:
        grandes = [valor for valor in valores if valor >= 128]
        if profile == "phone":
            grandes = [valor for valor in grandes if valor >= 64]
        if grandes:
            storage = grandes[-1]

    if ram is None and profile == "computer":
        pequenos = [valor for valor in valores if valor <= 128 and valor != storage]
        if pequenos:
            ram = pequenos[0]
    elif ram is None and profile == "generic" and len(valores) >= 2:
        pequenos = [valor for valor in valores if valor <= 128 and valor != storage]
        if pequenos:
            ram = pequenos[0]

    voltage = None
    if re.search(r"\bbivolt\b", norm):
        voltage = "bivolt"
    else:
        voltagem = re.search(r"\b(110|127|220)\s*v(?:olts?)?\b", norm)
        if voltagem:
            voltage = voltagem.group(1)

    screen_inches = None
    tela = re.search(r"(?<!\d)(\d{1,3}(?:[.,]\d+)?)\s*(?:\"|pol(?:egadas?)?)\b", norm)
    if tela:
        screen_inches = decimal_seguro(tela.group(1))

    if re.search(r"\b(usado|usada|seminovo|seminova)\b", norm):
        condition = "used"
    elif re.search(r"\b(recondicionado|recondicionada|refurbished)\b", norm):
        condition = "refurbished"
    elif re.search(r"\b(novo|nova|lacrado|lacrada)\b", norm):
        condition = "new"
    else:
        condition = DEFAULT_CONDITION

    ignorar_numeros = {valor for valor in (ram, storage) if valor is not None}
    identity_terms: list[str] = []
    for token in norm.split():
        token_limpo = token.strip(".,\"")
        if not token_limpo or token_limpo in STOPWORDS or token_limpo in MARKETING_WORDS:
            continue
        if token_limpo == chip:
            continue
        if token_limpo in {"pro", "max", "ultra"} and chip_tier == token_limpo:
            continue
        if token_limpo.isdigit() and int(token_limpo) in ignorar_numeros:
            continue
        if re.fullmatch(r"(110|127|220)v?", token_limpo):
            continue
        if len(token_limpo) >= 2:
            identity_terms.append(token_limpo)

    return SearchSpec(
        raw=raw,
        normalized=norm,
        profile=profile,
        family_key=family_key,
        family_label=family_label,
        identity_terms=lista_unica(identity_terms),
        chip=chip,
        chip_tier=chip_tier,
        ram_gb=ram,
        storage_gb=storage,
        voltage=voltage,
        screen_inches=screen_inches,
        condition=condition,
    )


# =============================================================================
# VALIDAÇÃO DO ANÚNCIO E DAS VARIAÇÕES
# =============================================================================


@dataclass
class AttributeRecord:
    source: str
    attr_id: str
    name: str
    value: str


@dataclass
class Evidence:
    normalized_text: str
    chips: set[str] = field(default_factory=set)
    chip_tiers: set[str] = field(default_factory=set)
    ram_values: set[int] = field(default_factory=set)
    storage_values: set[int] = field(default_factory=set)
    variation_ram_values: set[int] = field(default_factory=set)
    variation_storage_values: set[int] = field(default_factory=set)
    voltages: set[str] = field(default_factory=set)
    screen_sizes: set[Decimal] = field(default_factory=set)


@dataclass
class MatchResult:
    compatible: bool
    confidence: int
    code: str
    missing: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


RAM_ATTR_HINTS = {
    "ram", "ram_memory", "memory_ram", "memoria_ram", "memoria unificada",
    "unified memory", "capacidade da memoria ram", "installed_ram",
}
STORAGE_ATTR_HINTS = {
    "internal_memory", "internal memory", "memoria interna", "storage_capacity",
    "storage", "ssd", "ssd_capacity", "hard_drive_capacity", "capacidade do ssd",
    "capacidade de armazenamento", "capacidade do disco rigido", "rom",
}


def value_text(attribute: dict[str, Any]) -> str:
    direto = primeiro_texto(attribute.get("value_name"), attribute.get("value"))
    if direto:
        return direto
    estrutura = attribute.get("value_struct")
    if isinstance(estrutura, dict):
        numero = estrutura.get("number")
        unidade = estrutura.get("unit")
        if numero is not None:
            return f"{numero} {unidade or ''}".strip()
    valores = attribute.get("values")
    if isinstance(valores, list):
        textos = []
        for valor in valores:
            if isinstance(valor, dict):
                textos.append(primeiro_texto(valor.get("name"), valor.get("value_name")))
        return " / ".join(filter(None, textos))
    return ""


def adicionar_atributos(
    saida: list[AttributeRecord], valores: Any, source: str
) -> None:
    if not isinstance(valores, list):
        return
    for atributo in valores:
        if not isinstance(atributo, dict):
            continue
        saida.append(
            AttributeRecord(
                source=source,
                attr_id=normalizar_texto(atributo.get("id")),
                name=normalizar_texto(atributo.get("name")),
                value=value_text(atributo),
            )
        )


def coletar_atributos(raw: dict[str, Any], detail: dict[str, Any]) -> list[AttributeRecord]:
    registros: list[AttributeRecord] = []
    adicionar_atributos(registros, raw.get("variation_attributes"), "variation")
    adicionar_atributos(registros, raw.get("attributes"), "item")
    adicionar_atributos(registros, detail.get("attributes"), "item")

    variation_id = primeiro_texto(
        raw.get("variation_id"),
        obter_dict(raw, "variation").get("id"),
        detail.get("variation_id"),
    )
    variations = detail.get("variations")
    if isinstance(variations, list):
        candidatas = [v for v in variations if isinstance(v, dict)]
        if variation_id:
            candidatas = [v for v in candidatas if str(v.get("id")) == variation_id]
        elif len(candidatas) != 1:
            candidatas = []
        for variacao in candidatas:
            adicionar_atributos(registros, variacao.get("attribute_combinations"), "variation")
            adicionar_atributos(registros, variacao.get("attributes"), "variation")
    return registros


def atributo_tipo(registro: AttributeRecord) -> str:
    identificador = f"{registro.attr_id} {registro.name}".strip()
    if any(dica in identificador for dica in RAM_ATTR_HINTS) or re.search(
        r"\b(ram|ddr[345]?|memoria unificada|unified memory)\b", identificador
    ):
        return "ram"
    if any(dica in identificador for dica in STORAGE_ATTR_HINTS) or re.search(
        r"\b(ssd|nvme|armazenamento|storage|memoria interna|hard drive|rom)\b",
        identificador,
    ):
        return "storage"
    return ""


def marker_positions(texto: str, padrao: str) -> list[int]:
    return [(m.start() + m.end()) // 2 for m in re.finditer(padrao, texto)]


def classificar_capacidade_titulo(
    texto: str, start: int, end: int, valor: int, profile: str
) -> str:
    ram_marker = r"(?:ram|ddr[345]?|memoria(?: ram)?|memoria unificada|unified memory)"
    storage_marker = r"(?:ssd|nvme|armazenamento|storage|memoria interna|rom|disco|hard drive|hd)"
    antes = texto[max(0, start - 24) : start]
    depois = texto[end : min(len(texto), end + 28)]
    if re.match(rf"\s*(?:de\s+)?{ram_marker}\b", depois):
        return "ram"
    if re.match(rf"\s*(?:de\s+)?{storage_marker}\b", depois):
        return "storage"
    if re.search(rf"\b{ram_marker}\s*$", antes):
        return "ram"
    if re.search(rf"\b{storage_marker}\s*$", antes):
        return "storage"
    if valor >= 256:
        return "storage"
    if profile == "computer" and valor <= 96:
        return "ram"
    if profile == "phone" and valor >= 64:
        return "storage"

    center = (start + end) // 2
    inicio = max(0, center - 30)
    fim = min(len(texto), center + 30)
    janela = texto[inicio:fim]
    centro_local = center - inicio
    ram_pos = marker_positions(
        janela, r"\b(ram|ddr[345]?|memoria(?: ram)?|unificada|unified memory)\b"
    )
    storage_pos = marker_positions(
        janela, r"\b(ssd|nvme|armazenamento|storage|memoria interna|rom|disco|hard drive|hd)\b"
    )
    ram_dist = min((abs(p - centro_local) for p in ram_pos), default=999)
    storage_dist = min((abs(p - centro_local) for p in storage_pos), default=999)
    if ram_dist < storage_dist:
        return "ram"
    if storage_dist < ram_dist:
        return "storage"
    if profile == "computer" and valor <= 128:
        return "ram"
    return ""


def construir_evidencia(
    title: str,
    raw: dict[str, Any],
    detail: dict[str, Any],
    spec: SearchSpec,
) -> Evidence:
    registros = coletar_atributos(raw, detail)
    texto_attr = " ".join(f"{r.name} {r.value}" for r in registros)
    texto = normalizar_texto(f"{title} {texto_attr}")
    evidencia = Evidence(normalized_text=texto)

    for chip_match in re.finditer(r"\bm\s*([1-9])\b", texto):
        evidencia.chips.add(f"m{chip_match.group(1)}")
    for tier in ("pro", "max", "ultra"):
        if re.search(rf"\bm\s*[1-9]\s+{tier}\b", texto):
            evidencia.chip_tiers.add(tier)

    # Capacidades confirmadas por atributos; atributos da variação têm precedência.
    for registro in registros:
        tipo = atributo_tipo(registro)
        valores = extrair_capacidades(registro.value)
        if not valores:
            numeros = re.findall(r"(?<!\d)(\d{1,4})(?!\d)", normalizar_texto(registro.value))
            valores = [int(n) for n in numeros if int(n) in COMMON_CAPACITIES]
        if tipo == "ram":
            evidencia.ram_values.update(valores)
            if registro.source == "variation":
                evidencia.variation_ram_values.update(valores)
        elif tipo == "storage":
            evidencia.storage_values.update(valores)
            if registro.source == "variation":
                evidencia.variation_storage_values.update(valores)

    titulo_norm = normalizar_texto(title)
    capacidade_pattern = r"(?<![a-z0-9])(\d+(?:[.,]\d+)?)\s*(tb|tera(?:bytes?)?|gb|giga(?:bytes?)?|gigas?)\b"
    for match in re.finditer(capacidade_pattern, titulo_norm):
        unidade = "tb" if match.group(2).startswith("t") else "gb"
        valor = capacidade_para_gb(match.group(1), unidade)
        if not valor:
            continue
        if unidade == "tb":
            tipo = "storage"
        else:
            tipo = classificar_capacidade_titulo(
                titulo_norm, match.start(), match.end(), valor, spec.profile
            )
        if tipo == "ram":
            evidencia.ram_values.add(valor)
        elif tipo == "storage":
            evidencia.storage_values.add(valor)

    # Títulos compactos como "M4 16/512", sem unidades.
    bare_values = [valor for _, valor in numeros_soltos_busca(titulo_norm)]
    if spec.profile == "computer":
        if not evidencia.storage_values:
            evidencia.storage_values.update(v for v in bare_values if v >= 256)
        if not evidencia.ram_values:
            evidencia.ram_values.update(v for v in bare_values if v <= 128)
    elif spec.profile == "phone" and not evidencia.storage_values:
        evidencia.storage_values.update(v for v in bare_values if v >= 64)

    for match in re.finditer(r"\b(110|127|220)\s*v(?:olts?)?\b", texto):
        evidencia.voltages.add(match.group(1))
    if "bivolt" in texto:
        evidencia.voltages.add("bivolt")
    for match in re.finditer(r"(?<!\d)(\d{1,3}(?:[.,]\d+)?)\s*(?:\"|pol(?:egadas?)?)\b", texto):
        valor = decimal_seguro(match.group(1))
        if valor:
            evidencia.screen_sizes.add(valor)
    return evidencia


def contem_termo(texto: str, termo: str) -> bool:
    if not termo:
        return True
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(termo)}(?![a-z0-9])", texto))


def acessorio_detectado(texto_normalizado: str, consulta_normalizada: str) -> Optional[str]:
    for termo in ACCESSORY_TERMS:
        if contem_termo(texto_normalizado, termo) and not contem_termo(
            consulta_normalizada, termo
        ):
            return termo
    return None


def avaliar_compatibilidade(
    spec: SearchSpec,
    title: str,
    raw: dict[str, Any],
    detail: dict[str, Any],
) -> MatchResult:
    evidencia = construir_evidencia(title, raw, detail, spec)
    conflitos: list[str] = []
    ausentes: list[str] = []
    confidence = 100

    acessorio = acessorio_detectado(evidencia.normalized_text, spec.normalized)
    if acessorio:
        return MatchResult(False, 0, "accessory", conflicts=[acessorio])

    family_tokens = set(spec.family_key.split()) if spec.family_key else set()
    if family_tokens:
        for token in family_tokens:
            if not contem_termo(evidencia.normalized_text, token):
                ausentes.append(f"família:{token}")

    redundant_terms = set(family_tokens)
    if spec.family_key.startswith(("mac", "iphone", "ipad", "imac")):
        redundant_terms.add("apple")
    identity_to_check = [t for t in spec.identity_terms if t not in redundant_terms]
    if identity_to_check:
        faltantes_identidade = [
            t for t in identity_to_check if not contem_termo(evidencia.normalized_text, t)
        ]
        encontrados = len(identity_to_check) - len(faltantes_identidade)
        proporcao = encontrados / len(identity_to_check)
        if proporcao < 0.65:
            conflitos.append("produto diferente")
        elif proporcao < 1:
            confidence -= 10
        if family_tokens and faltantes_identidade:
            ausentes.extend(f"modelo:{t}" for t in faltantes_identidade)

    if spec.chip:
        if evidencia.chips and spec.chip not in evidencia.chips:
            conflitos.append(f"chip diferente ({', '.join(sorted(evidencia.chips))})")
        elif spec.chip not in evidencia.chips:
            ausentes.append(spec.chip.upper())
        if spec.chip_tier:
            if evidencia.chip_tiers and spec.chip_tier not in evidencia.chip_tiers:
                conflitos.append("linha do chip diferente")
            elif spec.chip_tier not in evidencia.chip_tiers:
                ausentes.append(spec.chip_tier.title())

    def validar_capacidade(
        desejado: Optional[int],
        valores: set[int],
        valores_variacao: set[int],
        rotulo: str,
    ) -> None:
        nonlocal confidence
        if desejado is None:
            return
        efetivos = valores_variacao or valores
        if not efetivos:
            ausentes.append(f"{rotulo} {formatar_capacidade(desejado)}")
            confidence -= 25
            return
        if desejado not in efetivos:
            encontrados = "/".join(formatar_capacidade(v) for v in sorted(efetivos))
            conflitos.append(f"{rotulo} diferente ({encontrados})")
            return
        # Se há várias opções no título/item e nenhuma variação individual confirma,
        # o link pode abrir numa configuração diferente da solicitada.
        if len(efetivos) > 1 and not valores_variacao:
            ausentes.append(f"variação de {rotulo} não individualizada")
            confidence -= 30

    validar_capacidade(
        spec.ram_gb,
        evidencia.ram_values,
        evidencia.variation_ram_values,
        "RAM",
    )
    validar_capacidade(
        spec.storage_gb,
        evidencia.storage_values,
        evidencia.variation_storage_values,
        "armazenamento",
    )

    if spec.voltage:
        if evidencia.voltages and spec.voltage not in evidencia.voltages and "bivolt" not in evidencia.voltages:
            conflitos.append("voltagem diferente")
        elif not evidencia.voltages:
            ausentes.append("voltagem")

    if spec.screen_inches is not None:
        if evidencia.screen_sizes and spec.screen_inches not in evidencia.screen_sizes:
            conflitos.append("tamanho de tela diferente")
        elif not evidencia.screen_sizes:
            ausentes.append("tamanho de tela")

    condicao = primeiro_texto(detail.get("condition"), raw.get("condition")).lower()
    if spec.condition != "any":
        if condicao and condicao != spec.condition:
            conflitos.append(f"condição {condicao}")
        elif not condicao:
            ausentes.append("condição")

    if conflitos:
        return MatchResult(False, 0, "conflict", ausentes, conflitos)
    if ausentes and STRICT_CONFIGURATION:
        return MatchResult(False, max(0, confidence), "unconfirmed", ausentes, [])
    if ausentes:
        confidence -= 10 * len(ausentes)
    return MatchResult(True, max(1, confidence), "compatible", ausentes, [])


def score_catalog_product(nome: str, spec: SearchSpec) -> int:
    norm = normalizar_texto(nome)
    if not norm or acessorio_detectado(norm, spec.normalized):
        return -1000
    score = 0
    for token in spec.identity_terms:
        if contem_termo(norm, token):
            score += 20
    if spec.family_key and all(contem_termo(norm, t) for t in spec.family_key.split()):
        score += 80
    chips = {f"m{m.group(1)}" for m in re.finditer(r"\bm\s*([1-9])\b", norm)}
    if spec.chip:
        if chips and spec.chip not in chips:
            return -800
        if spec.chip in chips:
            score += 50
    capacidades = set(extrair_capacidades(norm))
    if spec.ram_gb in capacidades:
        score += 15
    if spec.storage_gb in capacidades:
        score += 25
    return score


# =============================================================================
# NORMALIZAÇÃO DE OFERTAS
# =============================================================================


@dataclass
class InstallmentInfo:
    quantity: int
    amount: Decimal
    interest_free: Optional[bool] = None


@dataclass
class Offer:
    item_id: str
    product_id: str
    title: str
    config_label: str
    price: Decimal
    original_price: Optional[Decimal]
    link: str
    seller_id: str
    condition: str
    warranty: str
    free_shipping: Optional[bool]
    shipping_cost: Optional[Decimal]
    delivery_text: str
    city: str
    state: str
    installments: Optional[InstallmentInfo]
    deal_ids: list[str]
    confidence: int
    seller: dict[str, Any] = field(default_factory=dict)

    @property
    def total_price(self) -> Decimal:
        if self.shipping_cost is not None:
            return self.price + self.shipping_cost
        return self.price


@dataclass
class SearchStats:
    products_received: int = 0
    products_selected: int = 0
    raw_offers: int = 0
    unique_items: int = 0
    rejected_accessory: int = 0
    rejected_conflict: int = 0
    rejected_unconfirmed: int = 0
    rejected_no_price: int = 0
    compatible: int = 0


@dataclass
class SearchResult:
    ok: bool
    status: Optional[int]
    spec: SearchSpec
    offers: list[Offer]
    stats: SearchStats
    error: str = ""


def extrair_preco(raw: dict[str, Any], detail: dict[str, Any]) -> Optional[Decimal]:
    candidatos = [
        obter_dict(detail, "sale_price").get("amount"),
        detail.get("price"),
        obter_dict(raw, "sale_price").get("amount"),
        raw.get("price"),
    ]
    for candidato in candidatos:
        valor = decimal_seguro(candidato)
        if valor is not None and valor > 0:
            return valor
    return None


def extrair_preco_original(
    raw: dict[str, Any], detail: dict[str, Any], atual: Decimal
) -> Optional[Decimal]:
    candidatos = [
        detail.get("original_price"),
        obter_dict(detail, "sale_price").get("regular_amount"),
        raw.get("original_price"),
        obter_dict(raw, "sale_price").get("regular_amount"),
    ]
    for candidato in candidatos:
        valor = decimal_seguro(candidato)
        if valor is not None and valor > atual:
            return valor
    return None


def extrair_installments(*fontes: dict[str, Any]) -> Optional[InstallmentInfo]:
    candidatos: list[Any] = []
    for fonte in fontes:
        if not isinstance(fonte, dict):
            continue
        candidatos.extend(
            [
                fonte.get("installments"),
                obter_dict(fonte, "sale_price").get("installments"),
                obter_dict(fonte, "payment").get("installments"),
            ]
        )
    normalizados: list[InstallmentInfo] = []
    for candidato in candidatos:
        opcoes = candidato if isinstance(candidato, list) else [candidato]
        for opcao in opcoes:
            if not isinstance(opcao, dict):
                continue
            try:
                quantity = int(opcao.get("quantity") or opcao.get("installments") or 0)
            except (TypeError, ValueError):
                quantity = 0
            amount = decimal_seguro(opcao.get("amount") or opcao.get("installment_amount"))
            if quantity <= 1 or amount is None or amount <= 0:
                continue
            interest_free: Optional[bool] = None
            if opcao.get("rate") is not None:
                rate = decimal_seguro(opcao.get("rate"))
                interest_free = rate == 0 if rate is not None else None
            elif opcao.get("interest_free") is not None:
                interest_free = bool(opcao.get("interest_free"))
            normalizados.append(InstallmentInfo(quantity, amount, interest_free))
    return max(normalizados, key=lambda x: x.quantity, default=None)


def extrair_garantia(raw: dict[str, Any], detail: dict[str, Any]) -> str:
    direta = primeiro_texto(detail.get("warranty"), raw.get("warranty"))
    if direta:
        texto = re.sub(
            r"^garantia\s+(?:(?:de|do)\s+)?(?:fabrica|vendedor)\s*:?[ ]*",
            "",
            normalizar_texto(direta),
        )
        return limitar_texto(texto or direta, 42)

    termos = []
    for fonte in (detail, raw):
        for termo in obter_lista(fonte, "sale_terms"):
            if not isinstance(termo, dict):
                continue
            ident = normalizar_texto(termo.get("id"))
            nome = normalizar_texto(termo.get("name"))
            if "warranty" in ident or "garantia" in nome:
                valor = value_text(termo)
                if valor:
                    termos.append(valor)
    return limitar_texto(" • ".join(lista_unica(termos)), 42)


def extrair_local(raw: dict[str, Any], detail: dict[str, Any]) -> tuple[str, str]:
    for fonte in (detail, raw):
        endereco = fonte.get("seller_address")
        if not isinstance(endereco, dict):
            continue
        city = endereco.get("city")
        state = endereco.get("state")
        cidade = primeiro_texto(city.get("name") if isinstance(city, dict) else city)
        estado = primeiro_texto(
            state.get("id") if isinstance(state, dict) else "",
            state.get("name") if isinstance(state, dict) else state,
        )
        if estado.startswith("BR-"):
            estado = estado[3:]
        if cidade or estado:
            return cidade, estado
    return "", ""


def link_item(item_id: str, detail: dict[str, Any], raw: dict[str, Any]) -> str:
    for candidato in (detail.get("permalink"), raw.get("permalink"), raw.get("url")):
        if not candidato:
            continue
        try:
            parsed = urlparse(str(candidato))
            host = (parsed.hostname or "").lower()
            if parsed.scheme in {"http", "https"} and (
                host.endswith("mercadolivre.com.br")
                or host.endswith("mercadolibre.com")
                or host.endswith("mercadolivre.com")
            ):
                return str(candidato)
        except ValueError:
            pass
    match = re.fullmatch(r"([A-Z]{3})(\d+)", item_id.upper())
    if match:
        return f"https://produto.mercadolivre.com.br/{match.group(1)}-{match.group(2)}-_JM"
    return ""


def normalizar_oferta(
    raw: dict[str, Any],
    detail: dict[str, Any],
    product: dict[str, Any],
    spec: SearchSpec,
) -> tuple[Optional[Offer], MatchResult]:
    item_id = primeiro_texto(raw.get("item_id"), raw.get("id"), detail.get("id"))
    product_id = primeiro_texto(product.get("id"), raw.get("product_id"))
    product_name = primeiro_texto(product.get("name"), product.get("title"))
    title = primeiro_texto(detail.get("title"), raw.get("title"), product_name, "Produto")

    match = avaliar_compatibilidade(spec, title, raw, detail)
    if not match.compatible:
        return None, match

    price = extrair_preco(raw, detail)
    if price is None:
        return None, MatchResult(False, 0, "no_price")
    original = extrair_preco_original(raw, detail, price)

    shipping_raw = raw.get("shipping") if isinstance(raw.get("shipping"), dict) else {}
    shipping_detail = detail.get("shipping") if isinstance(detail.get("shipping"), dict) else {}
    free_shipping: Optional[bool] = None
    for shipping in (shipping_detail, shipping_raw):
        if shipping.get("free_shipping") is not None:
            free_shipping = bool(shipping.get("free_shipping"))
            break
    shipping_cost = None
    for shipping in (shipping_raw, shipping_detail):
        custo = decimal_seguro(shipping.get("cost"))
        if custo is not None and custo >= 0:
            shipping_cost = custo
            break
    if free_shipping is True:
        shipping_cost = Decimal("0")

    cidade, estado = extrair_local(raw, detail)
    seller_id = primeiro_texto(detail.get("seller_id"), raw.get("seller_id"))
    condition = primeiro_texto(detail.get("condition"), raw.get("condition"))
    deal_ids: list[str] = []
    for fonte in (raw, detail):
        for chave in ("deal_ids", "promotion_ids"):
            valor = fonte.get(chave)
            if isinstance(valor, list):
                deal_ids.extend(str(v) for v in valor if v)

    return (
        Offer(
            item_id=item_id,
            product_id=product_id,
            title=title,
            config_label=spec.configuration_label(title),
            price=price,
            original_price=original,
            link=link_item(item_id, detail, raw),
            seller_id=seller_id,
            condition=condition,
            warranty=extrair_garantia(raw, detail),
            free_shipping=free_shipping,
            shipping_cost=shipping_cost,
            delivery_text="",
            city=cidade,
            state=estado,
            installments=extrair_installments(raw, detail),
            deal_ids=lista_unica(deal_ids),
            confidence=match.confidence,
        ),
        match,
    )


def enriquecer_frete(oferta: Offer) -> None:
    if not BUYER_ZIP_CODE or not oferta.item_id:
        return
    resultado = meli_get(
        f"/items/{oferta.item_id}/shipping_options",
        params={"zip_code": BUYER_ZIP_CODE},
    )
    if not resultado.ok or not isinstance(resultado.data, dict):
        return
    opcoes = resultado.data.get("options")
    if not isinstance(opcoes, list):
        return
    validas: list[tuple[Decimal, dict[str, Any]]] = []
    for opcao in opcoes:
        if not isinstance(opcao, dict):
            continue
        descricao = normalizar_texto(
            " ".join(
                str(opcao.get(chave) or "")
                for chave in ("name", "shipping_method_type", "mode", "type")
            )
        )
        if any(termo in descricao for termo in ("pickup", "local pick up", "retirada")):
            continue
        custo = decimal_seguro(opcao.get("cost"))
        if custo is not None and custo >= 0:
            validas.append((custo, opcao))
    if not validas:
        return
    custo, melhor = min(validas, key=lambda x: x[0])
    oferta.shipping_cost = custo
    oferta.free_shipping = custo == 0
    estimativa = melhor.get("estimated_delivery_time")
    if isinstance(estimativa, dict):
        data = primeiro_texto(estimativa.get("date"), estimativa.get("pay_before"))
        if data:
            oferta.delivery_text = data[:10]


def chave_ranking(oferta: Offer) -> tuple[Any, ...]:
    usar_total = RANK_BY_TOTAL_WITH_SHIPPING and bool(BUYER_ZIP_CODE)
    frete_desconhecido = usar_total and oferta.shipping_cost is None
    valor = oferta.total_price if usar_total else oferta.price
    reputacao = obter_dict(oferta.seller, "seller_reputation")
    level = primeiro_texto(reputacao.get("level_id"))
    ordem_level = {
        "5_green": 0,
        "4_light_green": 1,
        "3_yellow": 2,
        "2_orange": 3,
        "1_red": 4,
    }.get(level, 5)
    return (frete_desconhecido, valor, ordem_level, -oferta.confidence, oferta.item_id)


def buscar_ofertas_completas(termo: str) -> SearchResult:
    spec = parse_search_spec(termo)
    stats = SearchStats()
    resultado_catalogo = pesquisar_catalogo(spec.raw)
    if not resultado_catalogo.ok:
        return SearchResult(
            False,
            resultado_catalogo.status,
            spec,
            [],
            stats,
            resultado_catalogo.error,
        )

    produtos = extrair_lista_resposta(resultado_catalogo.data)
    stats.products_received = len(produtos)
    ranqueados: list[tuple[int, dict[str, Any]]] = []
    for produto in produtos:
        nome = primeiro_texto(produto.get("name"), produto.get("title"))
        score = score_catalog_product(nome, spec)
        if score > -500:
            ranqueados.append((score, produto))
    ranqueados.sort(key=lambda x: x[0], reverse=True)
    produtos_selecionados = [p for _, p in ranqueados[:MAX_CATALOG_PRODUCTS]]
    stats.products_selected = len(produtos_selecionados)

    por_item: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for produto in produtos_selecionados:
        product_id = primeiro_texto(produto.get("id"))
        if not product_id:
            continue
        for raw in obter_ofertas_produto(product_id):
            stats.raw_offers += 1
            item_id = primeiro_texto(raw.get("item_id"), raw.get("id"))
            if not item_id:
                continue
            atual = por_item.get(item_id)
            if atual is None:
                por_item[item_id] = (raw, produto)
            else:
                preco_novo = decimal_seguro(raw.get("price")) or Decimal("999999999")
                preco_antigo = decimal_seguro(atual[0].get("price")) or Decimal("999999999")
                if preco_novo < preco_antigo:
                    por_item[item_id] = (raw, produto)

    # Limita o enriquecimento sem voltar a ordenar por catálogo: a pré-seleção usa o
    # menor preço bruto; a ordenação final só acontece após validar configuração.
    itens_ordenados = sorted(
        por_item.items(),
        key=lambda pair: decimal_seguro(pair[1][0].get("price")) or Decimal("999999999"),
    )[:MAX_OFFERS_TO_ENRICH]
    stats.unique_items = len(itens_ordenados)
    details = obter_detalhes_itens([item_id for item_id, _ in itens_ordenados])

    ofertas: list[Offer] = []
    for item_id, (raw, produto) in itens_ordenados:
        detail = details.get(item_id, {})
        oferta, match = normalizar_oferta(raw, detail, produto, spec)
        if oferta is None:
            if match.code == "accessory":
                stats.rejected_accessory += 1
            elif match.code == "conflict":
                stats.rejected_conflict += 1
            elif match.code == "unconfirmed":
                stats.rejected_unconfirmed += 1
            elif match.code == "no_price":
                stats.rejected_no_price += 1
            continue
        ofertas.append(oferta)

    stats.compatible = len(ofertas)

    # Consulta de frete apenas para uma faixa curta dos menores preços. Sem CEP, o
    # ranking permanece estritamente pelo preço atual anunciado.
    ofertas.sort(key=lambda o: (o.price, -o.confidence, o.item_id))
    if BUYER_ZIP_CODE:
        for oferta in ofertas[:SHIPPING_ENRICH_LIMIT]:
            enriquecer_frete(oferta)

    # Reputação é enriquecida antes do desempate, mas preço continua sendo a chave 1.
    sellers: dict[str, dict[str, Any]] = {}
    for oferta in ofertas[: max(MAX_RESULTS, SHIPPING_ENRICH_LIMIT)]:
        if oferta.seller_id and oferta.seller_id not in sellers:
            sellers[oferta.seller_id] = obter_vendedor(oferta.seller_id)
        oferta.seller = sellers.get(oferta.seller_id, {})

    ofertas.sort(key=chave_ranking)
    return SearchResult(True, 200, spec, ofertas[:MAX_RESULTS], stats)


# =============================================================================
# FORMATAÇÃO COMPACTA PARA TELEGRAM
# =============================================================================


MEDALHAS = {1: "🥇", 2: "🥈", 3: "🥉"}
REPUTACAO_LABELS = {
    "5_green": ("🟢", "reputação verde"),
    "4_light_green": ("🟢", "reputação verde-clara"),
    "3_yellow": ("🟡", "reputação amarela"),
    "2_orange": ("🟠", "reputação laranja"),
    "1_red": ("🔴", "reputação vermelha"),
}
POWER_SELLER_LABELS = {
    "silver": "MercadoLíder",
    "gold": "MercadoLíder Gold",
    "platinum": "MercadoLíder Platinum",
}


def formatar_vendedor(oferta: Offer) -> str:
    seller = oferta.seller if isinstance(oferta.seller, dict) else {}
    seller_id = oferta.seller_id
    nickname = limitar_texto(seller.get("nickname"), 25)
    identificacao = nickname or (f"Seller {seller_id}" if seller_id else "Vendedor")
    reputacao = seller.get("seller_reputation") if isinstance(seller.get("seller_reputation"), dict) else {}
    level = primeiro_texto(reputacao.get("level_id"))
    power = primeiro_texto(reputacao.get("power_seller_status")).lower()
    partes = [identificacao]
    if power in POWER_SELLER_LABELS:
        partes.append(POWER_SELLER_LABELS[power])
    if level in REPUTACAO_LABELS:
        emoji, label = REPUTACAO_LABELS[level]
        partes.append(f"{emoji} {label}")
    elif seller_id and not nickname:
        partes[0] = f"Seller {seller_id}"
    return " • ".join(partes)


def formatar_frete_garantia(oferta: Offer) -> str:
    partes: list[str] = []
    if oferta.condition == "used":
        partes.append("📦 Usado")
    elif oferta.condition == "refurbished":
        partes.append("📦 Recondicionado")
    if oferta.free_shipping is True or oferta.shipping_cost == 0:
        partes.append("🚚 Frete grátis")
    elif oferta.shipping_cost is not None:
        partes.append(f"🚚 Frete {brl(oferta.shipping_cost)}")
    else:
        partes.append("🚚 Frete a calcular")
    if oferta.delivery_text:
        partes.append(f"entrega {oferta.delivery_text}")
    if oferta.warranty:
        partes.append(f"🛡 {oferta.warranty}")
    return " • ".join(partes)


def montar_card(oferta: Offer, posicao: int) -> str:
    marcador = MEDALHAS.get(posicao, f"#{posicao}")
    linhas = [f"<b>{marcador} {html.escape(brl(oferta.price))}</b>"]
    linhas.append(html.escape(limitar_texto(oferta.config_label, 92)))

    if oferta.original_price:
        desconto = porcentagem_desconto(oferta.price, oferta.original_price)
        linha = f"De {brl(oferta.original_price)} informado pelo anúncio"
        if desconto is not None:
            linha += f" • {str(desconto).replace('.', ',')}% OFF"
        linhas.append(html.escape(linha))

    if oferta.installments:
        juros = " sem juros" if oferta.installments.interest_free is True else ""
        linhas.append(
            html.escape(
                f"💳 {oferta.installments.quantity}x de {brl(oferta.installments.amount)}{juros}"
            )
        )

    linhas.append(html.escape(formatar_frete_garantia(oferta)))

    local = ", ".join(filter(None, [oferta.city, oferta.state]))
    vendedor = formatar_vendedor(oferta)
    linha_vendedor = f"👤 {vendedor}"
    if local:
        linha_vendedor = f"📍 {local} • {linha_vendedor}"
    linhas.append(html.escape(linha_vendedor))

    if oferta.deal_ids:
        linhas.append("🏷 Promoção detectada no anúncio")

    if oferta.link:
        linhas.append(f'🔗 <a href="{html.escape(oferta.link, quote=True)}">Abrir anúncio</a>')
    else:
        linhas.append(f"🆔 {html.escape(oferta.item_id)}")
    return "\n".join(linhas)


def resumo_resultados(resultado: SearchResult) -> str:
    ofertas = resultado.offers
    quantidade = len(ofertas)
    criterio = (
        "menor total com frete primeiro"
        if RANK_BY_TOTAL_WITH_SHIPPING and BUYER_ZIP_CODE
        else "menor preço primeiro"
    )
    rotulo = "oferta compatível" if quantidade == 1 else "ofertas compatíveis"
    cabecalho = f"✅ <b>{quantidade} {rotulo}</b> • {criterio}"
    blocos = [cabecalho]
    for indice, oferta in enumerate(ofertas, start=1):
        blocos.append(montar_card(oferta, indice))
    blocos.append(
        "<i>Preços anteriores são os valores informados pelos anúncios. "
        "Cupom, parcela e frete podem variar por conta e CEP; o bot só exibe o que a API confirmou.</i>"
    )
    texto = "\n\n".join(blocos)
    # Mantém margem abaixo do limite de 4096 caracteres do Telegram.
    if len(texto) > 3950:
        texto = "\n\n".join(blocos[:-1])
    return texto[:4000]


def mensagem_sem_resultados(resultado: SearchResult) -> str:
    spec = resultado.spec
    configuracao = spec.configuration_label(spec.raw)
    stats = resultado.stats
    linhas = [
        "❌ <b>Nenhuma oferta totalmente compatível</b>",
        "",
        f"Busca validada: <b>{html.escape(configuracao)}</b>",
    ]
    if STRICT_CONFIGURATION:
        linhas.extend(
            [
                "",
                "Anúncios sem confirmação explícita da configuração foram excluídos.",
            ]
        )
    if stats.unique_items:
        linhas.extend(
            [
                "",
                f"Analisados: {stats.unique_items} anúncio(s)",
                f"Configuração diferente: {stats.rejected_conflict}",
                f"Configuração não confirmada: {stats.rejected_unconfirmed}",
            ]
        )
    linhas.extend(
        [
            "",
            "Tente escrever capacidades com unidade, por exemplo:",
            "<code>/buscar Mac Mini M4 16 GB RAM 512 GB SSD</code>",
        ]
    )
    return "\n".join(linhas)


# =============================================================================
# TELEGRAM
# =============================================================================


def telegram_call(metodo: str, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    if not TELEGRAM_API:
        logger.error("TELEGRAM_BOT_TOKEN não configurado")
        return None
    try:
        response = requests.post(
            f"{TELEGRAM_API}/{metodo}",
            json=payload,
            timeout=TIMEOUT,
        )
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code != 200 or not data.get("ok"):
            descricao = data.get("description") if isinstance(data, dict) else ""
            logger.warning("Telegram %s falhou HTTP %s: %s", metodo, response.status_code, descricao)
            return None
        result = data.get("result")
        return result if isinstance(result, dict) else {}
    except requests.RequestException as exc:
        logger.warning("Falha de rede no Telegram %s: %s", metodo, exc)
        return None


def send_message(
    chat_id: int,
    texto: str,
    *,
    html_mode: bool = True,
    message_thread_id: Optional[int] = None,
    reply_markup: Optional[dict[str, Any]] = None,
) -> Optional[int]:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": texto[:4096],
        "link_preview_options": {"is_disabled": True},
    }
    if html_mode:
        payload["parse_mode"] = "HTML"
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    if reply_markup:
        payload["reply_markup"] = reply_markup
    result = telegram_call("sendMessage", payload)
    if not result:
        return None
    try:
        return int(result.get("message_id"))
    except (TypeError, ValueError):
        return None


def edit_message(chat_id: int, message_id: Optional[int], texto: str) -> bool:
    if not message_id:
        return False
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": texto[:4096],
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    return telegram_call("editMessageText", payload) is not None


def editar_ou_enviar(
    chat_id: int,
    message_id: Optional[int],
    texto: str,
    message_thread_id: Optional[int],
) -> None:
    if not edit_message(chat_id, message_id, texto):
        send_message(chat_id, texto, message_thread_id=message_thread_id)


def chat_permitido(chat_id: int) -> bool:
    return not TELEGRAM_ALLOWED_CHATS or chat_id in TELEGRAM_ALLOWED_CHATS


def link_oauth_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "🔐 Conectar Mercado Livre", "url": f"{APP_BASE_URL}/oauth/login"}]
        ]
    }


# =============================================================================
# EXECUÇÃO ASSÍNCRONA DAS BUSCAS
# =============================================================================


EXECUTOR = ThreadPoolExecutor(max_workers=SEARCH_WORKERS, thread_name_prefix="garimpo")
ACTIVE_CHATS: set[int] = set()
ACTIVE_LOCK = threading.Lock()


def executar_busca(
    chat_id: int,
    termo: str,
    message_thread_id: Optional[int] = None,
) -> None:
    status_id: Optional[int] = None
    try:
        spec = parse_search_spec(termo)
        status_id = send_message(
            chat_id,
            "🔎 <b>GARIMPANDO…</b>\n\n"
            f"Validando <b>{html.escape(spec.configuration_label(spec.raw))}</b>\n"
            "Configuração → preço → vendedor → link",
            message_thread_id=message_thread_id,
        )
        resultado = buscar_ofertas_completas(termo)
        if not resultado.ok:
            if resultado.status == 401 or resultado.error == "not_authenticated":
                texto = (
                    "⚠️ <b>Mercado Livre não autorizado</b>\n\n"
                    f'<a href="{html.escape(APP_BASE_URL)}/oauth/login">Conectar novamente</a>'
                )
            elif resultado.status == 429:
                texto = "⏳ A API limitou temporariamente as consultas. Tente novamente em alguns minutos."
            else:
                detalhe = f" (HTTP {resultado.status})" if resultado.status else ""
                texto = f"❌ A busca falhou{detalhe}. Tente novamente em instantes."
            editar_ou_enviar(chat_id, status_id, texto, message_thread_id)
            return
        texto = resumo_resultados(resultado) if resultado.offers else mensagem_sem_resultados(resultado)
        editar_ou_enviar(chat_id, status_id, texto, message_thread_id)
    except Exception:
        logger.exception("Erro inesperado durante /buscar")
        editar_ou_enviar(
            chat_id,
            status_id,
            "❌ Ocorreu um erro inesperado durante a busca. Tente novamente.",
            message_thread_id,
        )
    finally:
        with ACTIVE_LOCK:
            ACTIVE_CHATS.discard(chat_id)


def agendar_busca(chat_id: int, termo: str, message_thread_id: Optional[int]) -> bool:
    with ACTIVE_LOCK:
        if chat_id in ACTIVE_CHATS:
            return False
        ACTIVE_CHATS.add(chat_id)
    try:
        EXECUTOR.submit(executar_busca, chat_id, termo, message_thread_id)
        return True
    except Exception:
        with ACTIVE_LOCK:
            ACTIVE_CHATS.discard(chat_id)
        raise


# =============================================================================
# OAUTH E ROTAS WEB
# =============================================================================


OAUTH_STATES: dict[str, float] = {}
OAUTH_STATE_LOCK = threading.Lock()
OAUTH_STATE_TTL = 600


def criar_oauth_state() -> str:
    agora = time.time()
    state = secrets.token_urlsafe(32)
    with OAUTH_STATE_LOCK:
        expirados = [chave for chave, expira in OAUTH_STATES.items() if expira <= agora]
        for chave in expirados:
            OAUTH_STATES.pop(chave, None)
        OAUTH_STATES[state] = agora + OAUTH_STATE_TTL
    return state


def consumir_oauth_state(state: str) -> bool:
    if not state:
        return False
    agora = time.time()
    with OAUTH_STATE_LOCK:
        expira = OAUTH_STATES.pop(state, None)
    return bool(expira and expira > agora)


@app.get("/")
def home():
    status = "conectado" if TOKENS.conectado() else "aguardando OAuth"
    return (
        "Garimpeiro Pessoal online! 🤖"
        f"<br>Mercado Livre: {html.escape(status)}"
        "<br><a href='/health'>Health check</a>",
        200,
    )


@app.get("/health")
def health():
    configurado = bool(TELEGRAM_TOKEN and MELI_CLIENT_ID and MELI_CLIENT_SECRET)
    return {
        "ok": True,
        "configured": configurado,
        "mercado_livre_connected": TOKENS.conectado(),
        "strict_configuration": STRICT_CONFIGURATION,
        "active_searches": len(ACTIVE_CHATS),
    }, 200


@app.get("/oauth/login")
def oauth_login():
    if not MELI_CLIENT_ID or not MELI_CLIENT_SECRET:
        return "MELI_CLIENT_ID ou MELI_CLIENT_SECRET não configurado.", 500
    state = criar_oauth_state()
    parametros = {
        "response_type": "code",
        "client_id": MELI_CLIENT_ID,
        "redirect_uri": MELI_REDIRECT_URI,
        "state": state,
    }
    url = "https://auth.mercadolivre.com.br/authorization?" + urlencode(parametros)
    return redirect(url)


@app.get("/oauth/callback")
def oauth_callback():
    if request.args.get("error"):
        erro = html.escape(primeiro_texto(request.args.get("error_description"), request.args.get("error")))
        return f"Autorização cancelada ou recusada: {erro}", 400

    code = request.args.get("code", "").strip()
    state = request.args.get("state", "").strip()
    if not code:
        return "Código OAuth não recebido.", 400
    if not consumir_oauth_state(state):
        return "Estado OAuth inválido ou expirado. Abra /oauth/login novamente.", 400

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
            headers={"Accept": "application/json"},
            timeout=TIMEOUT,
        )
        try:
            data = response.json()
        except ValueError:
            data = {}
    except requests.RequestException:
        return "Falha de rede durante OAuth.", 502

    if response.status_code != 200:
        mensagem = html.escape(primeiro_texto(data.get("message"), data.get("error"), "OAuth recusado"))
        return f"Erro OAuth HTTP {response.status_code}: {mensagem}", 400
    if not TOKENS.salvar_resposta_oauth(data):
        return "Access token não recebido.", 400
    return (
        "✅ Mercado Livre conectado!<br><br>"
        "Volte ao Telegram e envie:<br>"
        "<b>/buscar Mac Mini M4 16 GB RAM 512 GB SSD</b>",
        200,
    )


# =============================================================================
# WEBHOOK E COMANDOS
# =============================================================================


PROCESSED_UPDATES: dict[int, float] = {}
PROCESSED_LOCK = threading.Lock()
UPDATE_TTL = 900


def update_novo(update_id: Any) -> bool:
    try:
        identificador = int(update_id)
    except (TypeError, ValueError):
        return True
    agora = time.monotonic()
    with PROCESSED_LOCK:
        expirados = [k for k, expira in PROCESSED_UPDATES.items() if expira <= agora]
        for chave in expirados:
            PROCESSED_UPDATES.pop(chave, None)
        if identificador in PROCESSED_UPDATES:
            return False
        PROCESSED_UPDATES[identificador] = agora + UPDATE_TTL
    return True


def separar_comando(texto: str) -> tuple[str, str]:
    texto = texto.strip()
    if not texto.startswith("/"):
        return "", texto
    partes = texto.split(maxsplit=1)
    comando = partes[0].split("@", 1)[0].lower()
    argumento = partes[1].strip() if len(partes) > 1 else ""
    return comando, argumento


def mensagem_inicial() -> str:
    return (
        "🤖 <b>GARIMPEIRO PESSOAL</b>\n\n"
        "Encontre as cinco melhores ofertas com configuração confirmada e menor preço primeiro.\n\n"
        "Exemplo:\n"
        "<code>/buscar Mac Mini M4 16 GB RAM 512 GB SSD</code>\n\n"
        "Comandos: /buscar, /status, /teste e /ajuda"
    )


def comando_status(chat_id: int, thread_id: Optional[int]) -> None:
    mercado = "✅ conectado" if TOKENS.conectado() else "⚠️ não autorizado"
    modo = "rígido" if STRICT_CONFIGURATION else "flexível"
    send_message(
        chat_id,
        "🤖 <b>STATUS</b>\n\n"
        "✅ Telegram\n"
        "✅ Aplicação online\n"
        f"Mercado Livre: {mercado}\n"
        f"Filtro de configuração: {modo}\n"
        f"Resultados por busca: {MAX_RESULTS}",
        message_thread_id=thread_id,
        reply_markup=None if TOKENS.conectado() else link_oauth_keyboard(),
    )


def comando_teste(chat_id: int, thread_id: Optional[int]) -> None:
    if not TOKENS.conectado():
        send_message(
            chat_id,
            "⚠️ Mercado Livre não autorizado.",
            message_thread_id=thread_id,
            reply_markup=link_oauth_keyboard(),
        )
        return
    resultado = meli_get("/users/me")
    if not resultado.ok or not isinstance(resultado.data, dict):
        send_message(
            chat_id,
            f"❌ Autenticação falhou{f' (HTTP {resultado.status})' if resultado.status else ''}.",
            message_thread_id=thread_id,
        )
        return
    data = resultado.data
    send_message(
        chat_id,
        "✅ <b>AUTENTICAÇÃO OK</b>\n\n"
        f"User ID: {html.escape(str(data.get('id') or '—'))}\n"
        f"Nickname: {html.escape(str(data.get('nickname') or '—'))}\n"
        f"Site: {html.escape(str(data.get('site_id') or SITE_ID))}",
        message_thread_id=thread_id,
    )


@app.post("/webhook")
def webhook():
    if TELEGRAM_WEBHOOK_SECRET:
        recebido = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(recebido, TELEGRAM_WEBHOOK_SECRET):
            return "Forbidden", 403

    update = request.get_json(silent=True) or {}
    if not isinstance(update, dict) or not update_novo(update.get("update_id")):
        return "OK", 200

    message = update.get("message")
    if not isinstance(message, dict):
        return "OK", 200
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    try:
        chat_id = int(chat.get("id"))
    except (TypeError, ValueError):
        return "OK", 200
    if not chat_permitido(chat_id):
        logger.warning("Mensagem ignorada de chat não autorizado: %s", chat_id)
        return "OK", 200

    texto = primeiro_texto(message.get("text"))
    thread_id = message.get("message_thread_id")
    try:
        thread_id = int(thread_id) if thread_id is not None else None
    except (TypeError, ValueError):
        thread_id = None
    comando, argumento = separar_comando(texto)

    if comando in {"/start", "/ajuda", "/help"}:
        send_message(chat_id, mensagem_inicial(), message_thread_id=thread_id)
    elif comando == "/status":
        comando_status(chat_id, thread_id)
    elif comando == "/teste":
        comando_teste(chat_id, thread_id)
    elif comando == "/buscar":
        if not TOKENS.conectado():
            send_message(
                chat_id,
                "⚠️ <b>Mercado Livre não autorizado.</b>",
                message_thread_id=thread_id,
                reply_markup=link_oauth_keyboard(),
            )
        elif not argumento:
            send_message(
                chat_id,
                "Use, por exemplo:\n"
                "<code>/buscar Mac Mini M4 16 GB RAM 512 GB SSD</code>",
                message_thread_id=thread_id,
            )
        elif len(argumento) > 180:
            send_message(chat_id, "❌ A busca é longa demais.", message_thread_id=thread_id)
        elif not agendar_busca(chat_id, argumento, thread_id):
            send_message(
                chat_id,
                "⏳ Já existe um garimpo em andamento neste chat.",
                message_thread_id=thread_id,
            )
    else:
        send_message(
            chat_id,
            "Envie uma busca assim:\n"
            "<code>/buscar Mac Mini M4 16 GB RAM 512 GB SSD</code>",
            message_thread_id=thread_id,
        )
    return "OK", 200


# =============================================================================
# START LOCAL
# =============================================================================


if __name__ == "__main__":
    port = env_int("PORT", 10000, 1, 65535)
    app.run(host="0.0.0.0", port=port, threaded=True)
