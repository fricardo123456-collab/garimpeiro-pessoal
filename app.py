"""Garimpeiro Pessoal — bot multiloja de ofertas + Telegram.

Objetivos desta versão:
- validar a configuração no anúncio/variação, não apenas no produto de catálogo;
- rejeitar configurações conflitantes ou não confirmadas;
- ordenar as ofertas compatíveis pelo preço atual (ou total com frete, se configurado);
- enriquecer os cinco resultados com link, parcelamento, frete e reputação quando
  esses dados forem realmente retornados pela API;
- entregar uma única mensagem compacta e editável no Telegram;
- aceitar buscas genéricas, aliases de comando e texto livre em conversa privada;
- manter monitores, histórico real de preços e alertas antifalso-positivo;
- consultar Mercado Livre, Amazon e Shopee pelas APIs oficiais disponíveis;
- aceitar OLX/Buscapé e outras lojas por um conector estruturado opcional;
- separar preço confirmado de preço potencial com cupom;
- avisar sobre cupons relevantes sem fingir que foram aceitos no carrinho;
- manter OAuth, webhook e chamadas HTTP mais resilientes e seguros.

Flask e requests são obrigatórios. psycopg é opcional e usado quando DATABASE_URL
aponta para PostgreSQL, recomendado para persistência no Render.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import statistics
import sys
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
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


def bool_seguro(valor: Any, padrao: bool = False) -> bool:
    """Converte booleanos de APIs sem tratar a string ``"false"`` como True."""

    if isinstance(valor, bool):
        return valor
    if valor is None:
        return padrao
    if isinstance(valor, (int, float, Decimal)):
        return valor != 0
    texto = str(valor).strip().lower()
    if texto in {"1", "true", "yes", "sim", "on", "enabled", "active"}:
        return True
    if texto in {"0", "false", "no", "nao", "não", "off", "disabled", "inactive", ""}:
        return False
    return padrao


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


def env_csv_str(nome: str, padrao: str = "") -> list[str]:
    return lista_unica_texto(
        parte.strip().lower()
        for parte in os.environ.get(nome, padrao).split(",")
        if parte.strip()
    )


def lista_unica_texto(valores: Iterable[str]) -> list[str]:
    vistos: set[str] = set()
    saida: list[str] = []
    for valor in valores:
        if valor and valor not in vistos:
            vistos.add(valor)
            saida.append(valor)
    return saida


TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
TELEGRAM_ALLOWED_CHATS = env_csv_int("TELEGRAM_ALLOWED_CHATS")

MELI_CLIENT_ID = os.environ.get("MELI_CLIENT_ID", "").strip()
MELI_CLIENT_SECRET = os.environ.get("MELI_CLIENT_SECRET", "").strip()
MELI_API = "https://api.mercadolibre.com"
MELI_TOKEN_URL = f"{MELI_API}/oauth/token"
SITE_ID = os.environ.get("MELI_SITE_ID", "MLB").strip().upper() or "MLB"
APP_VERSION = "3.0.0-multiloja"

APP_BASE_URL = os.environ.get(
    "APP_BASE_URL", "https://garimpeiro-pessoal.onrender.com"
).strip().rstrip("/")
MELI_REDIRECT_URI = os.environ.get(
    "MELI_REDIRECT_URI", f"{APP_BASE_URL}/oauth/callback"
).strip()

# Fontes são ativadas automaticamente quando suas credenciais existem. Esta
# lista permite desativar uma integração sem remover seus segredos do Render.
ENABLED_SOURCES = set(
    env_csv_str(
        "ENABLED_SOURCES",
        "mercadolivre,amazon,shopee,olx,buscape,universal",
    )
)

MELI_SALE_PRICE_ENABLED = env_bool("MELI_SALE_PRICE_ENABLED", True)
MELI_SALE_PRICE_LIMIT = env_int("MELI_SALE_PRICE_LIMIT", 20, 1, 100)

AMAZON_CREATORS_CLIENT_ID = os.environ.get(
    "AMAZON_CREATORS_CLIENT_ID", ""
).strip()
AMAZON_CREATORS_CLIENT_SECRET = os.environ.get(
    "AMAZON_CREATORS_CLIENT_SECRET", ""
).strip()
AMAZON_CREATORS_CREDENTIAL_VERSION = os.environ.get(
    "AMAZON_CREATORS_CREDENTIAL_VERSION", "3.1"
).strip() or "3.1"
AMAZON_PARTNER_TAG = os.environ.get("AMAZON_PARTNER_TAG", "").strip()
AMAZON_MARKETPLACE = os.environ.get(
    "AMAZON_MARKETPLACE", "www.amazon.com.br"
).strip() or "www.amazon.com.br"
AMAZON_CREATORS_API_URL = os.environ.get(
    "AMAZON_CREATORS_API_URL", "https://creatorsapi.amazon"
).strip().rstrip("/")
AMAZON_CREATORS_TOKEN_URL = os.environ.get(
    "AMAZON_CREATORS_TOKEN_URL", ""
).strip()
AMAZON_MAX_RESULTS = env_int("AMAZON_MAX_RESULTS", 10, 1, 10)
AMAZON_ALLOW_MARKETPLACE_SELLERS = env_bool(
    "AMAZON_ALLOW_MARKETPLACE_SELLERS", False
)

SHOPEE_AFFILIATE_APP_ID = os.environ.get(
    "SHOPEE_AFFILIATE_APP_ID", ""
).strip()
SHOPEE_AFFILIATE_APP_SECRET = os.environ.get(
    "SHOPEE_AFFILIATE_APP_SECRET", ""
).strip()
SHOPEE_AFFILIATE_API_URL = os.environ.get(
    "SHOPEE_AFFILIATE_API_URL",
    "https://open-api.affiliate.shopee.com.br/graphql",
).strip()
SHOPEE_MAX_RESULTS = env_int("SHOPEE_MAX_RESULTS", 20, 1, 50)
SHOPEE_MIN_RATING = env_float("SHOPEE_MIN_RATING", 4.7, 0.0, 5.0)
SHOPEE_MIN_SOLD = env_int("SHOPEE_MIN_SOLD", 50, 0, 100000000)

# A API pública oficial da OLX não pesquisa o marketplace. GECKO_API_KEY é um
# conector terceirizado opcional; sem ele o bot não raspa páginas nem burla o site.
GECKO_API_KEY = os.environ.get("GECKO_API_KEY", "").strip()
GECKO_API_URL = os.environ.get(
    "GECKO_API_URL", "https://api.geckoapi.com.br/v1/extract"
).strip()
GECKO_OLX_STATE = os.environ.get("GECKO_OLX_STATE", "").strip().upper()
GECKO_MAX_RESULTS = env_int("GECKO_MAX_RESULTS", 30, 1, 100)
OLX_ALLOW_AUTOMATIC_ALERTS = env_bool("OLX_ALLOW_AUTOMATIC_ALERTS", False)

# Conector universal: permite adicionar Buscapé e novas lojas sem editar este
# arquivo. O contrato JSON aceito está documentado em /integrations/schema.
UNIVERSAL_SEARCH_URL = os.environ.get("UNIVERSAL_SEARCH_URL", "").strip()
UNIVERSAL_SEARCH_API_KEY = os.environ.get(
    "UNIVERSAL_SEARCH_API_KEY", ""
).strip()
UNIVERSAL_TRUSTED_SOURCES = set(env_csv_str("UNIVERSAL_TRUSTED_SOURCES"))
UNIVERSAL_MAX_RESULTS = env_int("UNIVERSAL_MAX_RESULTS", 40, 1, 200)

# Cupons podem vir embutidos no conector universal, de JSON estático ou de feeds
# HTTPS que sigam o mesmo contrato. Nunca são subtraídos do preço confirmado.
COUPON_ALERTS_ENABLED = env_bool("COUPON_ALERTS_ENABLED", True)
COUPON_MIN_PERCENT = env_float("COUPON_MIN_PERCENT", 30.0, 1.0, 100.0)
COUPON_MIN_REAIS = env_float("COUPON_MIN_REAIS", 100.0, 1.0, 100000.0)
COUPON_FEED_URLS = [
    url.strip() for url in os.environ.get("COUPON_FEED_URLS", "").split(",")
    if url.strip()
]
COUPONS_JSON = os.environ.get("COUPONS_JSON", "").strip()
COUPON_CACHE_TTL = env_int("COUPON_CACHE_TTL", 300, 30, 86400)

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

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
MONITOR_DB_PATH = Path(
    os.environ.get("MONITOR_DB_PATH", "/tmp/garimpeiro_monitor.db")
)
CRON_SECRET = os.environ.get("CRON_SECRET", "").strip()
MONITOR_HISTORY_DAYS = env_int("MONITOR_HISTORY_DAYS", 30, 1, 365)
MONITOR_MIN_HISTORY_CHECKS = env_int("MONITOR_MIN_HISTORY_CHECKS", 6, 2, 100)
MONITOR_CANDIDATES_LIMIT = env_int("MONITOR_CANDIDATES_LIMIT", 15, 5, 50)
ALERT_CONFIRMATIONS = env_int("ALERT_CONFIRMATIONS", 2, 1, 5)
ALERT_DROP_PERCENT = env_float("ALERT_DROP_PERCENT", 8.0, 1.0, 50.0)
ALERT_MIN_DROP_REAIS = env_float("ALERT_MIN_DROP_REAIS", 200.0, 0.0, 10000.0)
ALERT_RENOTIFY_DROP_PERCENT = env_float(
    "ALERT_RENOTIFY_DROP_PERCENT", 3.0, 0.5, 25.0
)
ALERT_COOLDOWN_HOURS = env_int("ALERT_COOLDOWN_HOURS", 24, 1, 720)
MONITOR_MAX_PER_CHAT = env_int("MONITOR_MAX_PER_CHAT", 20, 1, 100)

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
SALE_PRICE_CACHE = TTLCache(ITEM_CACHE_TTL, max_items=2000)
COUPON_CACHE = TTLCache(COUPON_CACHE_TTL, max_items=100)


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
# PERSISTÊNCIA — PostgreSQL quando DATABASE_URL existir; SQLite como fallback
# =============================================================================


class PersistentStore:
    MONITOR_COLUMNS = (
        "id", "chat_id", "thread_id", "query", "target_price_cents", "active", "created_at",
        "updated_at", "last_checked_at", "pending_item_id", "pending_price_cents",
        "pending_count", "last_alert_item_id", "last_alert_price_cents",
        "last_alert_at",
    )

    def __init__(self) -> None:
        self.is_postgres = bool(DATABASE_URL)
        self.available = False
        self.durable = False
        self._lock = threading.RLock()
        self._psycopg = None
        try:
            if self.is_postgres:
                import psycopg  # type: ignore

                self._psycopg = psycopg
                self.durable = True
            else:
                MONITOR_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                caminho = str(MONITOR_DB_PATH.resolve())
                self.durable = not caminho.startswith("/tmp/")
            self._init_schema()
            self.available = True
        except Exception as exc:
            logger.error("Persistência indisponível: %s", exc)

    def _connect(self):
        if self.is_postgres:
            if self._psycopg is None:
                raise RuntimeError("Instale psycopg[binary] para usar DATABASE_URL")
            return self._psycopg.connect(DATABASE_URL)
        connection = sqlite3.connect(str(MONITOR_DB_PATH), timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _sql(self, comando: str) -> str:
        return comando.replace("?", "%s") if self.is_postgres else comando

    def _init_schema(self) -> None:
        comandos = [
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at BIGINT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS monitors (
                id TEXT PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                thread_id BIGINT,
                query TEXT NOT NULL,
                target_price_cents BIGINT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at BIGINT NOT NULL,
                updated_at BIGINT NOT NULL,
                last_checked_at BIGINT,
                pending_item_id TEXT,
                pending_price_cents BIGINT,
                pending_count INTEGER NOT NULL DEFAULT 0,
                last_alert_item_id TEXT,
                last_alert_price_cents BIGINT,
                last_alert_at BIGINT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS price_history (
                id TEXT PRIMARY KEY,
                monitor_id TEXT NOT NULL,
                checked_at BIGINT NOT NULL,
                item_id TEXT NOT NULL,
                seller_id TEXT,
                price_cents BIGINT NOT NULL,
                reputation_level TEXT,
                eligible INTEGER NOT NULL DEFAULT 0,
                title TEXT,
                link TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_history_monitor_time ON price_history (monitor_id, checked_at)",
            "CREATE INDEX IF NOT EXISTS idx_monitors_chat_active ON monitors (chat_id, active)",
        ]
        with self._lock:
            connection = self._connect()
            try:
                cursor = connection.cursor()
                for comando in comandos:
                    cursor.execute(comando)
                connection.commit()
            finally:
                connection.close()

    def execute(self, comando: str, params: tuple[Any, ...] = ()) -> bool:
        if not self.available:
            return False
        with self._lock:
            connection = self._connect()
            try:
                connection.cursor().execute(self._sql(comando), params)
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                logger.exception("Falha ao gravar persistência")
                return False
            finally:
                connection.close()

    def fetchone(self, comando: str, params: tuple[Any, ...] = ()) -> Optional[tuple[Any, ...]]:
        if not self.available:
            return None
        with self._lock:
            connection = self._connect()
            try:
                cursor = connection.cursor()
                cursor.execute(self._sql(comando), params)
                row = cursor.fetchone()
                return tuple(row) if row is not None else None
            except Exception:
                logger.exception("Falha ao ler persistência")
                return None
            finally:
                connection.close()

    def fetchall(self, comando: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        if not self.available:
            return []
        with self._lock:
            connection = self._connect()
            try:
                cursor = connection.cursor()
                cursor.execute(self._sql(comando), params)
                return [tuple(row) for row in cursor.fetchall()]
            except Exception:
                logger.exception("Falha ao ler persistência")
                return []
            finally:
                connection.close()

    def get_setting(self, key: str) -> Optional[str]:
        row = self.fetchone("SELECT value FROM app_settings WHERE key = ?", (key,))
        return str(row[0]) if row and row[0] is not None else None

    def set_settings(self, valores: dict[str, Any]) -> bool:
        if not self.available:
            return False
        agora = int(time.time())
        with self._lock:
            connection = self._connect()
            try:
                cursor = connection.cursor()
                for key, value in valores.items():
                    if value is None:
                        continue
                    cursor.execute(
                        self._sql(
                            """
                            INSERT INTO app_settings (key, value, updated_at)
                            VALUES (?, ?, ?)
                            ON CONFLICT (key) DO UPDATE
                            SET value = excluded.value, updated_at = excluded.updated_at
                            """
                        ),
                        (key, str(value), agora),
                    )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                logger.exception("Falha ao persistir configurações")
                return False
            finally:
                connection.close()

    def _monitor_dict(self, row: Optional[tuple[Any, ...]]) -> Optional[dict[str, Any]]:
        if not row:
            return None
        return dict(zip(self.MONITOR_COLUMNS, row))

    def create_monitor(
        self,
        chat_id: int,
        thread_id: Optional[int],
        query: str,
        target_price_cents: Optional[int],
    ) -> tuple[Optional[dict[str, Any]], str]:
        if not self.available:
            return None, "storage_unavailable"
        for existente in self.list_monitors(chat_id=chat_id, active_only=True):
            if (
                normalizar_texto(existente.get("query")) == normalizar_texto(query)
                and existente.get("target_price_cents") == target_price_cents
                and existente.get("thread_id") == thread_id
            ):
                return existente, "duplicate"
        count = self.fetchone(
            "SELECT COUNT(*) FROM monitors WHERE chat_id = ? AND active = 1",
            (chat_id,),
        )
        if count and int(count[0]) >= MONITOR_MAX_PER_CHAT:
            return None, "limit"
        monitor_id = uuid.uuid4().hex[:10]
        agora = int(time.time())
        ok = self.execute(
            """
            INSERT INTO monitors (
                id, chat_id, thread_id, query, target_price_cents, active, created_at,
                updated_at, pending_count
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, 0)
            """,
            (
                monitor_id,
                chat_id,
                thread_id,
                query,
                target_price_cents,
                agora,
                agora,
            ),
        )
        return (self.get_monitor(monitor_id, chat_id), "") if ok else (None, "write_error")

    def get_monitor(self, monitor_id: str, chat_id: Optional[int] = None) -> Optional[dict[str, Any]]:
        campos = ", ".join(self.MONITOR_COLUMNS)
        if chat_id is None:
            row = self.fetchone(f"SELECT {campos} FROM monitors WHERE id = ?", (monitor_id,))
        else:
            row = self.fetchone(
                f"SELECT {campos} FROM monitors WHERE id = ? AND chat_id = ?",
                (monitor_id, chat_id),
            )
        return self._monitor_dict(row)

    def list_monitors(
        self, chat_id: Optional[int] = None, active_only: bool = True
    ) -> list[dict[str, Any]]:
        campos = ", ".join(self.MONITOR_COLUMNS)
        filtros: list[str] = []
        params: list[Any] = []
        if chat_id is not None:
            filtros.append("chat_id = ?")
            params.append(chat_id)
        if active_only:
            filtros.append("active = 1")
        where = f" WHERE {' AND '.join(filtros)}" if filtros else ""
        rows = self.fetchall(
            f"SELECT {campos} FROM monitors{where} ORDER BY created_at ASC",
            tuple(params),
        )
        return [dict(zip(self.MONITOR_COLUMNS, row)) for row in rows]

    def deactivate_monitor(self, monitor_id: str, chat_id: int) -> bool:
        monitor = self.get_monitor(monitor_id, chat_id)
        if not monitor:
            return False
        return self.execute(
            "UPDATE monitors SET active = 0, updated_at = ? WHERE id = ? AND chat_id = ?",
            (int(time.time()), monitor_id, chat_id),
        )

    def record_history(self, monitor_id: str, checked_at: int, offer: Any, eligible: bool) -> None:
        price_cents = int((offer.price * 100).quantize(Decimal("1")))
        reputation = primeiro_texto(
            obter_dict(offer.seller, "seller_reputation").get("level_id")
        )
        self.execute(
            """
            INSERT INTO price_history (
                id, monitor_id, checked_at, item_id, seller_id, price_cents,
                reputation_level, eligible, title, link
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                monitor_id,
                checked_at,
                getattr(offer, "signal_id", offer.item_id),
                offer.seller_id,
                price_cents,
                reputation,
                1 if eligible else 0,
                offer.title,
                offer.link,
            ),
        )

    def history_baseline(self, monitor_id: str, before: int) -> tuple[Optional[int], int]:
        desde = before - MONITOR_HISTORY_DAYS * 86400
        rows = self.fetchall(
            """
            SELECT checked_at, price_cents FROM price_history
            WHERE monitor_id = ? AND eligible = 1 AND checked_at >= ? AND checked_at < ?
            ORDER BY checked_at ASC
            """,
            (monitor_id, desde, before),
        )
        por_check: dict[int, int] = {}
        for checked_at, cents in rows:
            instante, preco = int(checked_at), int(cents)
            por_check[instante] = min(preco, por_check.get(instante, preco))
        if not por_check:
            return None, 0
        median = int(statistics.median(por_check.values()))
        return median, len(por_check)

    def update_pending(
        self,
        monitor_id: str,
        item_id: Optional[str],
        price_cents: Optional[int],
        count: int,
        checked_at: int,
    ) -> None:
        self.execute(
            """
            UPDATE monitors SET pending_item_id = ?, pending_price_cents = ?,
                pending_count = ?, last_checked_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (item_id, price_cents, count, checked_at, checked_at, monitor_id),
        )

    def mark_alert(self, monitor_id: str, item_id: str, price_cents: int, sent_at: int) -> None:
        self.execute(
            """
            UPDATE monitors SET last_alert_item_id = ?, last_alert_price_cents = ?,
                last_alert_at = ?, updated_at = ? WHERE id = ?
            """,
            (item_id, price_cents, sent_at, sent_at, monitor_id),
        )

    def cleanup_history(self) -> None:
        limite = int(time.time()) - max(45, MONITOR_HISTORY_DAYS + 7) * 86400
        self.execute("DELETE FROM price_history WHERE checked_at < ?", (limite,))


STORE = PersistentStore()


# =============================================================================
# TOKENS OAUTH — memória + banco persistente + arquivo local de fallback
# =============================================================================


class TokenStore:
    def __init__(self, arquivo: Path):
        self.arquivo = arquivo
        self._lock = threading.RLock()
        self._access = os.environ.get("MELI_ACCESS_TOKEN", "").strip() or None
        self._refresh = os.environ.get("MELI_REFRESH_TOKEN", "").strip() or None
        self._expires_at: Optional[float] = None
        self._carregar_persistencia()
        self._carregar_arquivo()

    def _carregar_persistencia(self) -> None:
        if not STORE.available:
            return
        access = STORE.get_setting("meli_access_token")
        refresh = STORE.get_setting("meli_refresh_token")
        expires = STORE.get_setting("meli_expires_at")
        if access:
            self._access = access
        if refresh:
            self._refresh = refresh
        if expires:
            try:
                self._expires_at = float(expires)
            except ValueError:
                pass

    def _carregar_arquivo(self) -> None:
        try:
            if not self.arquivo.exists():
                return
            data = json.loads(self.arquivo.read_text(encoding="utf-8"))
            if not self._access and data.get("access_token"):
                self._access = str(data["access_token"])
            if not self._refresh and data.get("refresh_token"):
                self._refresh = str(data["refresh_token"])
            if self._expires_at is None and data.get("expires_at"):
                self._expires_at = float(data["expires_at"])
        except Exception as exc:
            logger.warning("Não foi possível carregar o arquivo local de tokens: %s", exc)

    def _persistir(self) -> None:
        STORE.set_settings(
            {
                "meli_access_token": self._access,
                "meli_refresh_token": self._refresh,
                "meli_expires_at": self._expires_at,
            }
        )
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
            # O web service e o cron podem estar em processos distintos. Antes de
            # usar um refresh token possivelmente antigo, reaproveita o token mais
            # recente que o outro processo já gravou no banco compartilhado.
            if STORE.available:
                access_persistido = STORE.get_setting("meli_access_token")
                refresh_persistido = STORE.get_setting("meli_refresh_token")
                if (
                    token_que_falhou
                    and access_persistido
                    and access_persistido != token_que_falhou
                ):
                    self._access = access_persistido
                    if refresh_persistido:
                        self._refresh = refresh_persistido
                    return True
                if refresh_persistido:
                    self._refresh = refresh_persistido
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


def pesquisar_anuncios_diretos(termo: str) -> ApiResult:
    """Busca anúncios comuns quando o produto não existe no catálogo central.

    Alguns itens (principalmente acessórios, usados e produtos de nicho) não
    aparecem em ``/products/search``. A busca pública do site funciona como rota
    alternativa; se a aplicação do Mercado Livre não tiver permissão para esse
    endpoint, o erro é tratado sem derrubar a rota de catálogo.
    """
    return meli_get(
        f"/sites/{SITE_ID}/search",
        params={
            "q": termo,
            "limit": min(50, MAX_OFFERS_TO_ENRICH),
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


def obter_sale_price(item_id: str) -> dict[str, Any]:
    """Obtém o preço vencedor atual pela Price API do Mercado Livre.

    Esse endpoint é a fonte autoritativa para preço/promowinning. Cupons de
    checkout não são presumidos aqui, porque podem depender de conta, forma de
    pagamento, limite de uso e saldo da campanha.
    """
    if not MELI_SALE_PRICE_ENABLED or not item_id:
        return {}
    cache = SALE_PRICE_CACHE.get(item_id)
    if isinstance(cache, dict):
        return cache
    resultado = meli_get(
        f"/items/{item_id}/sale_price",
        params={"context": "channel_marketplace"},
    )
    if resultado.ok and isinstance(resultado.data, dict):
        SALE_PRICE_CACHE.set(item_id, resultado.data)
        return resultado.data
    return {}


def aplicar_sale_price(oferta: Any, sale_price: dict[str, Any]) -> None:
    if not isinstance(sale_price, dict) or not sale_price:
        return
    atual = decimal_seguro(
        sale_price.get("amount")
        or sale_price.get("price")
        or obter_dict(sale_price, "sale_price").get("amount")
    )
    regular = decimal_seguro(
        sale_price.get("regular_amount")
        or sale_price.get("original_price")
        or obter_dict(sale_price, "sale_price").get("regular_amount")
    )
    if atual is not None and atual > 0:
        oferta.price = atual
    if regular is not None and regular > oferta.price:
        oferta.original_price = regular

    metadados = sale_price.get("metadata")
    if not isinstance(metadados, dict):
        metadados = {}
    ids: list[str] = []
    for chave in ("promotion_id", "campaign_id", "deal_id"):
        valor = primeiro_texto(sale_price.get(chave), metadados.get(chave))
        if valor:
            ids.append(valor)
    oferta.deal_ids = lista_unica([*oferta.deal_ids, *ids])
    rotulo = primeiro_texto(
        sale_price.get("promotion_type"),
        sale_price.get("price_type"),
        metadados.get("promotion_type"),
    )
    if rotulo:
        oferta.promotion_label = limitar_texto(rotulo.replace("_", " "), 42)


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

OPTIONAL_QUERY_TERMS = {
    "conexao", "conectividade", "bluetooth", "wireless", "fio", "compativel",
    "compatibilidade", "versao", "modelo",
}

PRODUCT_TYPE_TERMS = {
    "teclado", "mouse", "monitor", "notebook", "laptop", "computador", "desktop",
    "headset", "fone", "smartphone", "celular", "tablet", "televisao", "tv",
    "camera", "impressora", "roteador", "controle", "cadeira", "aspirador",
    "cafeteira", "geladeira", "fogao", "microondas", "ar condicionado", "console",
    "videogame", "ssd", "memoria", "carregador", "caixa de som", "soundbar",
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
    model_terms: list[str] = field(default_factory=list)
    product_terms: list[str] = field(default_factory=list)
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


def parece_codigo_modelo(token: str) -> bool:
    return bool(
        len(token) >= 3
        and re.search(r"[a-z]", token)
        and re.search(r"\d", token)
    )


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
        if (
            not token_limpo
            or token_limpo in STOPWORDS
            or token_limpo in MARKETING_WORDS
            or token_limpo in OPTIONAL_QUERY_TERMS
        ):
            continue
        if token_limpo == chip:
            continue
        if token_limpo in {"pro", "max", "ultra"} and chip_tier == token_limpo:
            continue
        if token_limpo.isdigit() and int(token_limpo) in ignorar_numeros:
            continue
        if re.fullmatch(r"(110|127|220)v?", token_limpo):
            continue
        # Números isolados também podem ser a geração do produto (PlayStation 5,
        # JBL Flip 6 etc.). Capacidades, tela e voltagem já foram identificadas
        # acima e continuam tratadas pelos validadores específicos.
        if len(token_limpo) >= 2 or (profile == "generic" and token_limpo.isdigit()):
            identity_terms.append(token_limpo)

    identity_terms = lista_unica(identity_terms)
    model_terms = [
        token
        for token in identity_terms
        if parece_codigo_modelo(token) or (profile == "generic" and token.isdigit())
    ]
    product_terms = [
        termo for termo in PRODUCT_TYPE_TERMS if contem_termo(norm, termo)
    ]

    return SearchSpec(
        raw=raw,
        normalized=norm,
        profile=profile,
        family_key=family_key,
        family_label=family_label,
        identity_terms=identity_terms,
        model_terms=model_terms,
        product_terms=sorted(product_terms),
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
        if family_tokens:
            encontrados = len(identity_to_check) - len(faltantes_identidade)
            proporcao = encontrados / len(identity_to_check)
            if proporcao < 0.65:
                conflitos.append("produto diferente")
            elif proporcao < 1:
                confidence -= 10
        else:
            faltantes_modelo = [
                termo for termo in spec.model_terms if termo in faltantes_identidade
            ]
            faltantes_produto = [
                termo
                for termo in spec.product_terms
                if not contem_termo(evidencia.normalized_text, termo)
            ]
            if faltantes_modelo:
                conflitos.append(
                    "modelo diferente (faltou " + ", ".join(faltantes_modelo) + ")"
                )
            if faltantes_produto:
                conflitos.append(
                    "tipo de produto diferente (faltou "
                    + ", ".join(faltantes_produto)
                    + ")"
                )

            def peso_identidade(termo: str) -> int:
                if termo in spec.model_terms:
                    return 5
                if termo in spec.product_terms or len(termo) >= 7:
                    return 2
                return 1

            peso_total = sum(peso_identidade(t) for t in identity_to_check)
            peso_encontrado = sum(
                peso_identidade(t)
                for t in identity_to_check
                if t not in faltantes_identidade
            )
            similaridade = peso_encontrado / max(1, peso_total)
            if similaridade < 0.45:
                conflitos.append("produto pouco aderente à busca")
            elif similaridade < 0.75:
                confidence -= 15
            elif similaridade < 1:
                confidence -= 5

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
class CouponInfo:
    """Cupom conhecido, mas ainda sujeito à validação no carrinho."""

    coupon_id: str = ""
    code: str = ""
    label: str = ""
    discount_type: str = "unknown"  # percent, fixed ou unknown
    value: Optional[Decimal] = None
    min_purchase: Optional[Decimal] = None
    max_discount: Optional[Decimal] = None
    starts_at: Optional[int] = None
    expires_at: Optional[int] = None
    terms_verified: bool = False
    buyer_specific: bool = False
    url: str = ""
    source: str = ""

    def active(self, now: Optional[int] = None) -> bool:
        instante = int(time.time()) if now is None else int(now)
        if self.starts_at is not None and instante < self.starts_at:
            return False
        if self.expires_at is not None and instante > self.expires_at:
            return False
        return True

    def discount_for(self, price: Decimal) -> Optional[Decimal]:
        if not self.active() or price <= 0:
            return None
        if self.min_purchase is not None and price < self.min_purchase:
            return None
        valor = self.value
        if valor is None or valor <= 0:
            return None
        tipo = normalizar_texto(self.discount_type)
        if tipo in {"percent", "percentage", "porcentagem", "percentual"}:
            desconto = price * valor / Decimal("100")
        elif tipo in {"fixed", "amount", "valor", "reais"}:
            desconto = valor
        else:
            return None
        if self.max_discount is not None and self.max_discount > 0:
            desconto = min(desconto, self.max_discount)
        return min(price, desconto.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    def estimated_price(self, price: Decimal) -> Optional[Decimal]:
        desconto = self.discount_for(price)
        if desconto is None:
            return None
        return max(Decimal("0"), price - desconto).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    def is_good(self, price: Decimal) -> bool:
        desconto = self.discount_for(price)
        if desconto is None:
            return False
        percentual = desconto / price * Decimal("100") if price > 0 else Decimal("0")
        return bool(
            desconto >= Decimal(str(COUPON_MIN_REAIS))
            or percentual >= Decimal(str(COUPON_MIN_PERCENT))
        )

    def fingerprint(self) -> str:
        base = "|".join(
            [
                self.source,
                self.coupon_id,
                self.code,
                self.discount_type,
                str(self.value or ""),
                str(self.expires_at or ""),
            ]
        )
        return hashlib.sha256(base.encode("utf-8")).hexdigest()[:12]


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
    source: str = "mercadolivre"
    source_label: str = "Mercado Livre"
    seller_name: str = ""
    seller_trusted: Optional[bool] = None
    rating: Optional[Decimal] = None
    rating_count: int = 0
    sold_count: int = 0
    coupon: Optional[CouponInfo] = None
    promotion_label: str = ""
    price_confirmed: bool = True
    trust_reason: str = ""
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def total_price(self) -> Decimal:
        if self.shipping_cost is not None:
            return self.price + self.shipping_cost
        return self.price

    @property
    def estimated_coupon_price(self) -> Optional[Decimal]:
        if self.coupon is None:
            return None
        return self.coupon.estimated_price(self.price)

    @property
    def signal_id(self) -> str:
        return f"{self.source}:{self.item_id}"


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
    sources_queried: int = 0
    sources_succeeded: int = 0


@dataclass
class SearchResult:
    ok: bool
    status: Optional[int]
    spec: SearchSpec
    offers: list[Offer]
    stats: SearchStats
    error: str = ""
    sources_used: list[str] = field(default_factory=list)
    provider_errors: dict[str, str] = field(default_factory=dict)
    comparison_links: dict[str, str] = field(default_factory=dict)


SOURCE_ALIASES = {
    "ml": "mercadolivre",
    "meli": "mercadolivre",
    "mercado livre": "mercadolivre",
    "mercadolivre": "mercadolivre",
    "amazon": "amazon",
    "shopee": "shopee",
    "shoppe": "shopee",
    "olx": "olx",
    "buscape": "buscape",
    "buscapé": "buscape",
    "universal": "universal",
}
SOURCE_LABELS = {
    "mercadolivre": "Mercado Livre",
    "amazon": "Amazon",
    "shopee": "Shopee",
    "olx": "OLX",
    "buscape": "Buscapé",
    "universal": "Outras lojas",
}


def normalizar_fonte(valor: Any) -> str:
    norm = normalizar_texto(valor).replace(" ", "")
    if norm in {"ml", "meli", "mercadolivre"}:
        return "mercadolivre"
    if norm in {"shopee", "shoppe"}:
        return "shopee"
    if norm in {"buscape"}:
        return "buscape"
    return norm


def timestamp_seguro(valor: Any) -> Optional[int]:
    if valor in (None, ""):
        return None
    try:
        numero = int(valor)
        if numero > 10_000_000_000:
            numero //= 1000
        return numero if numero > 0 else None
    except (TypeError, ValueError):
        pass
    try:
        texto = str(valor).strip().replace("Z", "+00:00")
        data = datetime.fromisoformat(texto)
        if data.tzinfo is None:
            data = data.replace(tzinfo=timezone.utc)
        return int(data.timestamp())
    except (TypeError, ValueError):
        return None


def coupon_from_raw(raw: Any, source: str = "") -> Optional[CouponInfo]:
    if not isinstance(raw, dict):
        return None
    status = normalizar_texto(raw.get("status"))
    if status in {"paused", "finished", "expired", "cancelled", "canceled", "inactive"}:
        return None
    remaining = decimal_seguro(raw.get("remaining_budget"))
    if remaining is not None and remaining <= 0:
        return None
    tipo = primeiro_texto(
        raw.get("discount_type"), raw.get("type"), raw.get("kind")
    ).lower()
    percentual = decimal_seguro(
        raw.get("percent")
        or raw.get("percentage")
        or raw.get("percent_off")
        or raw.get("discount_percentage")
    )
    fixo = decimal_seguro(
        raw.get("amount")
        or raw.get("value")
        or raw.get("amount_off")
        or raw.get("fixed_amount")
    )
    if percentual is not None:
        tipo, valor = "percent", percentual
    elif tipo in {"percent", "percentage", "porcentagem", "percentual"}:
        valor = fixo
        tipo = "percent"
    else:
        valor = fixo
        if valor is not None:
            tipo = "fixed"
    cupom = CouponInfo(
        coupon_id=primeiro_texto(raw.get("id"), raw.get("coupon_id")),
        code=primeiro_texto(raw.get("code"), raw.get("coupon_code")),
        label=primeiro_texto(raw.get("label"), raw.get("title"), raw.get("name")),
        discount_type=tipo or "unknown",
        value=valor,
        min_purchase=decimal_seguro(
            raw.get("min_purchase") or raw.get("minimum_purchase") or raw.get("min_value")
            or raw.get("min_purchase_amount")
        ),
        max_discount=decimal_seguro(
            raw.get("max_discount") or raw.get("maximum_discount") or raw.get("max_refund")
        ),
        starts_at=timestamp_seguro(
            raw.get("starts_at") or raw.get("start_at") or raw.get("start_date")
        ),
        expires_at=timestamp_seguro(
            raw.get("expires_at")
            or raw.get("end_at")
            or raw.get("expiration")
            or raw.get("finish_date")
            or raw.get("end_date")
        ),
        terms_verified=bool_seguro(
            raw.get("terms_verified")
            if raw.get("terms_verified") is not None
            else raw.get("verified")
        ),
        buyer_specific=bool_seguro(
            raw.get("buyer_specific")
            if raw.get("buyer_specific") is not None
            else raw.get("personalized")
        ),
        url=primeiro_texto(raw.get("url"), raw.get("terms_url")),
        source=normalizar_fonte(raw.get("source") or source),
    )
    if cupom.value is None or cupom.value <= 0 or not cupom.active():
        return None
    return cupom


def extrair_cupons_payload(payload: Any, source: str = "") -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for chave in ("coupons", "results", "items", "offers"):
        itens = payload.get(chave)
        if isinstance(itens, list):
            return [item for item in itens if isinstance(item, dict)]
    return [payload] if coupon_from_raw(payload, source) else []


def carregar_cupons_feed() -> list[dict[str, Any]]:
    cache = COUPON_CACHE.get("all")
    if isinstance(cache, list):
        return cache
    saida: list[dict[str, Any]] = []
    if COUPONS_JSON:
        try:
            saida.extend(extrair_cupons_payload(json.loads(COUPONS_JSON)))
        except (TypeError, ValueError):
            logger.warning("COUPONS_JSON inválido; cupons estáticos ignorados")
    for url in COUPON_FEED_URLS:
        try:
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.hostname:
                logger.warning("Feed de cupons não HTTPS ignorado: %s", url)
                continue
            resposta = HTTP.get(url, timeout=TIMEOUT)
            if 200 <= resposta.status_code < 300:
                saida.extend(extrair_cupons_payload(resposta.json()))
            else:
                logger.info("Feed de cupons retornou HTTP %s", resposta.status_code)
        except (requests.RequestException, ValueError):
            logger.warning("Falha ao consultar feed de cupons", exc_info=True)
    COUPON_CACHE.set("all", saida)
    return saida


def cupom_combina(raw: dict[str, Any], oferta: Offer, termo: str) -> bool:
    fontes = raw.get("sources") or raw.get("source") or []
    if isinstance(fontes, str):
        fontes = [fontes]
    fontes_norm = {normalizar_fonte(v) for v in fontes if v}
    if fontes_norm and oferta.source not in fontes_norm and "all" not in fontes_norm:
        return False
    ids = raw.get("item_ids") or raw.get("product_ids") or []
    if isinstance(ids, str):
        ids = [ids]
    if ids and oferta.item_id not in {str(v) for v in ids} and oferta.product_id not in {
        str(v) for v in ids
    }:
        return False
    vendedores = raw.get("seller_ids") or raw.get("sellers") or []
    if isinstance(vendedores, str):
        vendedores = [vendedores]
    vendedor_norm = normalizar_texto(oferta.seller_name or oferta.seller_id)
    if vendedores and not any(
        normalizar_texto(v) in vendedor_norm for v in vendedores if v
    ):
        return False
    palavras = raw.get("keywords") or raw.get("terms") or []
    if isinstance(palavras, str):
        palavras = [p.strip() for p in palavras.split(",") if p.strip()]
    alvo = normalizar_texto(f"{oferta.title} {termo}")
    if palavras and not all(normalizar_texto(p) in alvo for p in palavras if p):
        return False
    return True


def anexar_melhor_cupom(oferta: Offer, termo: str, feeds: list[dict[str, Any]]) -> None:
    candidatos: list[CouponInfo] = []
    if oferta.coupon is not None:
        candidatos.append(oferta.coupon)
    for raw in feeds:
        if cupom_combina(raw, oferta, termo):
            cupom = coupon_from_raw(raw, oferta.source)
            if cupom:
                candidatos.append(cupom)
    validos = [
        (cupom.discount_for(oferta.price), cupom)
        for cupom in candidatos
        if cupom.discount_for(oferta.price) is not None
    ]
    if validos:
        oferta.coupon = max(validos, key=lambda par: par[0] or Decimal("0"))[1]


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
    ufs = {
        "acre": "AC", "alagoas": "AL", "amapa": "AP", "amazonas": "AM",
        "bahia": "BA", "ceara": "CE", "distrito federal": "DF",
        "espirito santo": "ES", "goias": "GO", "maranhao": "MA",
        "mato grosso": "MT", "mato grosso do sul": "MS", "minas gerais": "MG",
        "para": "PA", "paraiba": "PB", "parana": "PR", "pernambuco": "PE",
        "piaui": "PI", "rio de janeiro": "RJ", "rio grande do norte": "RN",
        "rio grande do sul": "RS", "rondonia": "RO", "roraima": "RR",
        "santa catarina": "SC", "sao paulo": "SP", "sergipe": "SE",
        "tocantins": "TO",
    }
    cidade_final = ""
    estado_final = ""
    for fonte in (detail, raw):
        endereco = fonte.get("seller_address")
        if not isinstance(endereco, dict):
            continue
        city = endereco.get("city")
        state = endereco.get("state")
        cidade = primeiro_texto(city.get("name") if isinstance(city, dict) else city)
        estado_nome = primeiro_texto(
            state.get("name") if isinstance(state, dict) else state
        )
        estado_id = primeiro_texto(state.get("id") if isinstance(state, dict) else "")
        estado = ufs.get(normalizar_texto(estado_nome), estado_nome)
        if not estado and estado_id.startswith("BR-"):
            estado = estado_id[3:]
        if estado.startswith("TUx") or not re.fullmatch(r"[A-Za-zÀ-ÿ -]{2,40}|[A-Z]{2}", estado):
            estado = ""
        if cidade and not cidade.startswith("TUx") and not cidade_final:
            cidade_final = cidade
        if estado and not estado_final:
            estado_final = estado
        if cidade_final and estado_final:
            break
    return cidade_final, estado_final


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
    confiavel = oferta.seller_trusted is True or ordem_level <= 1
    return (
        frete_desconhecido,
        valor,
        not confiavel,
        ordem_level,
        -oferta.confidence,
        oferta.source,
        oferta.item_id,
    )


def buscar_mercado_livre(
    termo: str, limite_resultados: int = MAX_RESULTS
) -> SearchResult:
    limite_resultados = max(1, min(int(limite_resultados), MAX_OFFERS_TO_ENRICH))
    spec = parse_search_spec(termo)
    stats = SearchStats()
    resultado_catalogo = pesquisar_catalogo(spec.raw)
    resultado_direto: Optional[ApiResult] = None
    if resultado_catalogo.ok:
        produtos = extrair_lista_resposta(resultado_catalogo.data)
    else:
        # Nem toda busca possui um produto de catálogo. Nessa situação, tenta os
        # anúncios comuns antes de declarar falha da API.
        resultado_direto = pesquisar_anuncios_diretos(spec.raw)
        if not resultado_direto.ok:
            return SearchResult(
                False,
                resultado_catalogo.status or resultado_direto.status,
                spec,
                [],
                stats,
                resultado_catalogo.error or resultado_direto.error,
            )
        produtos = []

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

    # Se o catálogo não trouxe anúncios, procura diretamente no marketplace.
    # O restante do pipeline é o mesmo: detalhes, compatibilidade estrita,
    # deduplicação, vendedor, risco e ordenação por preço real.
    if not por_item:
        if resultado_direto is None:
            resultado_direto = pesquisar_anuncios_diretos(spec.raw)
        if resultado_direto.ok:
            anuncios = extrair_lista_resposta(resultado_direto.data)
            stats.products_received += len(anuncios)
            for raw in anuncios:
                stats.raw_offers += 1
                item_id = primeiro_texto(raw.get("item_id"), raw.get("id"))
                if not item_id:
                    continue
                pseudo_produto = {
                    "id": primeiro_texto(
                        raw.get("catalog_product_id"), raw.get("product_id")
                    ),
                    "name": primeiro_texto(raw.get("title"), raw.get("name")),
                }
                atual = por_item.get(item_id)
                if atual is None:
                    por_item[item_id] = (raw, pseudo_produto)
                    continue
                preco_novo = decimal_seguro(raw.get("price")) or Decimal("999999999")
                preco_antigo = decimal_seguro(atual[0].get("price")) or Decimal(
                    "999999999"
                )
                if preco_novo < preco_antigo:
                    por_item[item_id] = (raw, pseudo_produto)

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

    # Confirma o preço vencedor atual na API de preços. Isso inclui promoções
    # públicas do anúncio, mas não inventa cupom de checkout.
    ofertas.sort(key=lambda o: (o.price, -o.confidence, o.item_id))
    candidatas_preco = ofertas[:MELI_SALE_PRICE_LIMIT]
    if candidatas_preco:
        with ThreadPoolExecutor(max_workers=min(4, len(candidatas_preco))) as pool:
            precos = list(pool.map(lambda o: obter_sale_price(o.item_id), candidatas_preco))
        for oferta, sale_price in zip(candidatas_preco, precos):
            aplicar_sale_price(oferta, sale_price)

    # Consulta de frete apenas para uma faixa curta dos menores preços. Sem CEP, o
    # ranking permanece estritamente pelo preço atual anunciado.
    ofertas.sort(key=lambda o: (o.price, -o.confidence, o.item_id))
    if BUYER_ZIP_CODE:
        for oferta in ofertas[:SHIPPING_ENRICH_LIMIT]:
            enriquecer_frete(oferta)

    # Reputação é enriquecida antes do desempate, mas preço continua sendo a chave 1.
    sellers: dict[str, dict[str, Any]] = {}
    for oferta in ofertas[: max(limite_resultados, SHIPPING_ENRICH_LIMIT)]:
        if oferta.seller_id and oferta.seller_id not in sellers:
            sellers[oferta.seller_id] = obter_vendedor(oferta.seller_id)
        oferta.seller = sellers.get(oferta.seller_id, {})
        oferta.seller_name = primeiro_texto(oferta.seller.get("nickname"))
        oferta.seller_trusted = reputation_level(oferta) in SAFE_REPUTATION_LEVELS

    ofertas.sort(key=chave_ranking)
    return SearchResult(
        True,
        200,
        spec,
        ofertas[:limite_resultados],
        stats,
        sources_used=["mercadolivre"],
    )


# =============================================================================
# CONECTORES DE OUTRAS LOJAS E AGREGAÇÃO MULTILOJA
# =============================================================================


AMAZON_TOKEN_LOCK = threading.Lock()
AMAZON_TOKEN_STATE: dict[str, Any] = {"access_token": "", "expires_at": 0}


def valor_caminho(objeto: Any, *caminho: Any) -> Any:
    atual = objeto
    for chave in caminho:
        if isinstance(chave, int):
            if not isinstance(atual, list) or chave >= len(atual):
                return None
            atual = atual[chave]
        else:
            if not isinstance(atual, dict):
                return None
            atual = atual.get(chave)
    return atual


def link_https(valor: Any) -> str:
    texto = primeiro_texto(valor)
    try:
        parsed = urlparse(texto)
        if parsed.scheme == "https" and parsed.hostname:
            return texto
    except ValueError:
        pass
    return ""


def normalizar_condicao_externa(valor: Any, padrao: str = "new") -> str:
    norm = normalizar_texto(valor)
    if any(t in norm for t in ("used", "usado", "seminovo", "pre owned")):
        return "used"
    if any(t in norm for t in ("refurbished", "recondicionado", "renewed")):
        return "refurbished"
    if any(t in norm for t in ("new", "novo", "lacrado")):
        return "new"
    return padrao


def atributos_textuais(texto: str) -> list[dict[str, str]]:
    """Transforma descrições externas em atributos reconhecíveis pelo filtro."""
    norm = normalizar_texto(texto)
    atributos: list[dict[str, str]] = []
    ram = valor_proximo_a_marcador(
        norm, r"ram|memoria(?:\s+ram)?|memoria\s+unificada|unified\s+memory"
    )
    storage = valor_proximo_a_marcador(
        norm, r"ssd|nvme|armazenamento|storage|memoria\s+interna|rom|hd"
    )
    if ram is not None:
        atributos.append(
            {"id": "RAM_MEMORY", "name": "Memória RAM", "value_name": f"{ram} GB"}
        )
    if storage is not None:
        atributos.append(
            {
                "id": "STORAGE_CAPACITY",
                "name": "Capacidade de armazenamento",
                "value_name": formatar_capacidade(storage),
            }
        )
    return atributos


def criar_oferta_externa(
    *,
    spec: SearchSpec,
    source: str,
    item_id: Any,
    product_id: Any,
    title: Any,
    price: Any,
    original_price: Any = None,
    link: Any = "",
    seller_id: Any = "",
    seller_name: Any = "",
    condition: Any = "new",
    warranty: Any = "",
    free_shipping: Optional[bool] = None,
    shipping_cost: Any = None,
    city: Any = "",
    state: Any = "",
    rating: Any = None,
    rating_count: Any = 0,
    sold_count: Any = 0,
    seller_trusted: Optional[bool] = None,
    trust_reason: str = "",
    evidence_text: str = "",
    promotion_label: str = "",
    coupon_raw: Any = None,
    raw_metadata: Optional[dict[str, Any]] = None,
) -> tuple[Optional[Offer], MatchResult]:
    titulo = primeiro_texto(title, "Produto")
    condicao = normalizar_condicao_externa(condition)
    evidencias = " ".join(filter(None, [titulo, str(evidence_text or "")]))
    detail = {
        "condition": condicao,
        "attributes": atributos_textuais(evidencias),
    }
    raw = {"condition": condicao}
    match = avaliar_compatibilidade(spec, evidencias, raw, detail)
    if not match.compatible:
        return None, match
    preco = decimal_seguro(price)
    if preco is None or preco <= 0:
        return None, MatchResult(False, 0, "no_price")
    original = decimal_seguro(original_price)
    if original is not None and original <= preco:
        original = None
    custo_frete = decimal_seguro(shipping_cost)
    if free_shipping is True:
        custo_frete = Decimal("0")
    try:
        qtd_avaliacoes = int(rating_count or 0)
    except (TypeError, ValueError):
        qtd_avaliacoes = 0
    try:
        qtd_vendidos = int(sold_count or 0)
    except (TypeError, ValueError):
        qtd_vendidos = 0
    fonte = normalizar_fonte(source) or "universal"
    cupom = coupon_from_raw(coupon_raw, fonte)
    return (
        Offer(
            item_id=primeiro_texto(item_id, product_id),
            product_id=primeiro_texto(product_id),
            title=titulo,
            config_label=spec.configuration_label(titulo),
            price=preco,
            original_price=original,
            link=link_https(link),
            seller_id=primeiro_texto(seller_id),
            condition=condicao,
            warranty=limitar_texto(warranty, 42),
            free_shipping=free_shipping,
            shipping_cost=custo_frete,
            delivery_text="",
            city=limitar_texto(city, 40),
            state=limitar_texto(state, 24),
            installments=None,
            deal_ids=[],
            confidence=match.confidence,
            source=fonte,
            source_label=SOURCE_LABELS.get(fonte, fonte.title()),
            seller_name=limitar_texto(seller_name, 40),
            seller_trusted=seller_trusted,
            rating=decimal_seguro(rating),
            rating_count=qtd_avaliacoes,
            sold_count=qtd_vendidos,
            coupon=cupom,
            promotion_label=limitar_texto(promotion_label, 42),
            trust_reason=trust_reason,
            raw_metadata=raw_metadata or {},
        ),
        match,
    )


def contabilizar_match(stats: SearchStats, match: MatchResult) -> None:
    if match.code == "accessory":
        stats.rejected_accessory += 1
    elif match.code == "conflict":
        stats.rejected_conflict += 1
    elif match.code == "unconfirmed":
        stats.rejected_unconfirmed += 1
    elif match.code == "no_price":
        stats.rejected_no_price += 1


def amazon_token_url() -> str:
    if AMAZON_CREATORS_TOKEN_URL:
        return AMAZON_CREATORS_TOKEN_URL
    if AMAZON_CREATORS_CREDENTIAL_VERSION.startswith("2"):
        return "https://creatorsapi.auth.us-east-1.amazoncognito.com/oauth2/token"
    return "https://api.amazon.com/auth/o2/token"


def obter_token_amazon() -> str:
    agora = int(time.time())
    if AMAZON_TOKEN_STATE.get("access_token") and int(
        AMAZON_TOKEN_STATE.get("expires_at") or 0
    ) > agora + 60:
        return str(AMAZON_TOKEN_STATE["access_token"])
    with AMAZON_TOKEN_LOCK:
        if AMAZON_TOKEN_STATE.get("access_token") and int(
            AMAZON_TOKEN_STATE.get("expires_at") or 0
        ) > int(time.time()) + 60:
            return str(AMAZON_TOKEN_STATE["access_token"])
        payload = {
            "grant_type": "client_credentials",
            "client_id": AMAZON_CREATORS_CLIENT_ID,
            "client_secret": AMAZON_CREATORS_CLIENT_SECRET,
            "scope": (
                "creatorsapi/default"
                if AMAZON_CREATORS_CREDENTIAL_VERSION.startswith("2")
                else "creatorsapi::default"
            ),
        }
        try:
            kwargs: dict[str, Any] = {
                "headers": {"Accept": "application/json"},
                "timeout": TIMEOUT,
            }
            if AMAZON_CREATORS_CREDENTIAL_VERSION.startswith("2"):
                kwargs["data"] = payload
                kwargs["headers"]["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                kwargs["json"] = payload
                kwargs["headers"]["Content-Type"] = "application/json"
            resposta = requests.post(amazon_token_url(), **kwargs)
            data = resposta.json()
        except (requests.RequestException, ValueError):
            logger.warning("Falha ao obter token Amazon Creators API", exc_info=True)
            return ""
        token = primeiro_texto(data.get("access_token")) if isinstance(data, dict) else ""
        if not (200 <= resposta.status_code < 300 and token):
            logger.info("Amazon recusou OAuth (HTTP %s)", resposta.status_code)
            return ""
        try:
            expires = int(data.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires = 3600
        AMAZON_TOKEN_STATE.update(
            {"access_token": token, "expires_at": int(time.time()) + max(120, expires)}
        )
        return token


def lista_amazon(payload: Any) -> list[dict[str, Any]]:
    caminhos = [
        ("searchResult", "items"),
        ("SearchResult", "Items"),
        ("items",),
        ("results",),
    ]
    for caminho in caminhos:
        atual: Any = payload
        for chave in caminho:
            atual = atual.get(chave) if isinstance(atual, dict) else None
        if isinstance(atual, list):
            return [item for item in atual if isinstance(item, dict)]
    return []


def buscar_amazon(termo: str, limite_resultados: int) -> SearchResult:
    spec = parse_search_spec(termo)
    stats = SearchStats(sources_queried=1)
    token = obter_token_amazon()
    if not token:
        return SearchResult(False, 401, spec, [], stats, "amazon_auth")
    recursos = [
        "itemInfo.title",
        "itemInfo.features",
        "itemInfo.productInfo",
        "itemInfo.technicalInfo",
        "offersV2.listings.price",
        "offersV2.listings.merchantInfo",
        "offersV2.listings.condition",
        "offersV2.listings.dealDetails",
        "offersV2.listings.savingBasis",
        "offersV2.listings.availability",
    ]
    payload = {
        "keywords": spec.raw,
        "marketplace": AMAZON_MARKETPLACE,
        "partnerTag": AMAZON_PARTNER_TAG,
        "itemCount": min(AMAZON_MAX_RESULTS, max(1, limite_resultados)),
        "resources": recursos,
    }
    if spec.condition == "new":
        payload["condition"] = "New"
    elif spec.condition == "used":
        payload["condition"] = "Used"
    authorization = f"Bearer {token}"
    if AMAZON_CREATORS_CREDENTIAL_VERSION.startswith("2"):
        authorization += f", Version {AMAZON_CREATORS_CREDENTIAL_VERSION}"
    try:
        resposta = requests.post(
            f"{AMAZON_CREATORS_API_URL}/catalog/v1/searchItems",
            json=payload,
            headers={
                "Authorization": authorization,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "x-marketplace": AMAZON_MARKETPLACE,
            },
            timeout=TIMEOUT,
        )
        data = resposta.json()
    except (requests.RequestException, ValueError):
        return SearchResult(False, None, spec, [], stats, "amazon_network")
    if not 200 <= resposta.status_code < 300:
        return SearchResult(False, resposta.status_code, spec, [], stats, "amazon_api")

    itens = lista_amazon(data)
    stats.raw_offers = len(itens)
    stats.unique_items = len(itens)
    ofertas: list[Offer] = []
    for item in itens:
        asin = primeiro_texto(item.get("asin"), item.get("ASIN"), item.get("id"))
        titulo = primeiro_texto(
            valor_caminho(item, "itemInfo", "title", "displayValue"),
            valor_caminho(item, "ItemInfo", "Title", "DisplayValue"),
            item.get("title"),
        )
        features = valor_caminho(item, "itemInfo", "features", "displayValues")
        if not isinstance(features, list):
            features = valor_caminho(item, "ItemInfo", "Features", "DisplayValues")
        evidence = " ".join(str(v) for v in features) if isinstance(features, list) else ""
        offers_v2 = item.get("offersV2") or item.get("OffersV2") or {}
        listings = offers_v2.get("listings") or offers_v2.get("Listings") or []
        if not isinstance(listings, list):
            listings = []
        if not listings:
            listings = [item]
        for indice, listing in enumerate(listings):
            if not isinstance(listing, dict):
                continue
            price = (
                valor_caminho(listing, "price", "amount")
                or valor_caminho(listing, "Price", "Amount")
                or valor_caminho(listing, "listingPrice", "amount")
                or listing.get("price")
            )
            original = (
                valor_caminho(listing, "savingBasis", "amount")
                or valor_caminho(listing, "SavingBasis", "Amount")
                or valor_caminho(listing, "regularPrice", "amount")
            )
            merchant = listing.get("merchantInfo") or listing.get("MerchantInfo") or {}
            seller_name = primeiro_texto(
                merchant.get("name") if isinstance(merchant, dict) else "",
                merchant.get("Name") if isinstance(merchant, dict) else "",
                listing.get("merchantName"),
            )
            vendido_amazon = "amazon" in normalizar_texto(seller_name)
            trusted = vendido_amazon or AMAZON_ALLOW_MARKETPLACE_SELLERS
            deal = listing.get("dealDetails") or listing.get("DealDetails") or {}
            promotion = primeiro_texto(
                deal.get("badge") if isinstance(deal, dict) else "",
                deal.get("accessType") if isinstance(deal, dict) else "",
                listing.get("promotionLabel"),
            )
            condition = primeiro_texto(
                valor_caminho(listing, "condition", "displayValue"),
                valor_caminho(listing, "condition", "value"),
                valor_caminho(listing, "Condition", "DisplayValue"),
                valor_caminho(listing, "Condition", "Value"),
                "new",
            )
            oferta, match = criar_oferta_externa(
                spec=spec,
                source="amazon",
                item_id=f"{asin}:{indice}" if len(listings) > 1 else asin,
                product_id=asin,
                title=titulo,
                price=price,
                original_price=original,
                link=primeiro_texto(
                    item.get("detailPageURL"), item.get("DetailPageURL"), item.get("url")
                ),
                seller_id=primeiro_texto(
                    merchant.get("id") if isinstance(merchant, dict) else "",
                    seller_name,
                ),
                seller_name=seller_name,
                condition=condition,
                free_shipping=None,
                seller_trusted=trusted,
                trust_reason=(
                    "vendido pela Amazon"
                    if vendido_amazon
                    else "marketplace Amazon permitido"
                    if trusted
                    else "vendedor marketplace não verificado"
                ),
                evidence_text=evidence,
                promotion_label=promotion,
                raw_metadata=listing,
            )
            if oferta:
                ofertas.append(oferta)
            else:
                contabilizar_match(stats, match)
    stats.compatible = len(ofertas)
    stats.sources_succeeded = 1
    ofertas.sort(key=chave_ranking)
    return SearchResult(
        True, 200, spec, ofertas[:limite_resultados], stats, sources_used=["amazon"]
    )


def buscar_shopee(termo: str, limite_resultados: int) -> SearchResult:
    spec = parse_search_spec(termo)
    stats = SearchStats(sources_queried=1)
    query = """query ProductOffers($keyword: String!, $sortType: Int!, $page: Int!, $limit: Int!) {
  productOfferV2(keyword: $keyword, sortType: $sortType, page: $page, limit: $limit) {
    nodes { productId productName productLink offerLink imageUrl price priceMin priceMax shopId shopName ratingStar soldCount commissionRate periodStartTime periodEndTime }
  }
}"""
    payload = {
        "query": query,
        "variables": {
            "keyword": spec.raw,
            "sortType": 2,
            "page": 1,
            "limit": min(SHOPEE_MAX_RESULTS, max(1, limite_resultados * 2)),
        },
    }
    corpo = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    timestamp = str(int(time.time()))
    assinatura = hashlib.sha256(
        f"{SHOPEE_AFFILIATE_APP_ID}{timestamp}{corpo}{SHOPEE_AFFILIATE_APP_SECRET}".encode(
            "utf-8"
        )
    ).hexdigest()
    try:
        resposta = requests.post(
            SHOPEE_AFFILIATE_API_URL,
            data=corpo.encode("utf-8"),
            headers={
                "Authorization": (
                    f"SHA256 Credential={SHOPEE_AFFILIATE_APP_ID}, "
                    f"Timestamp={timestamp}, Signature={assinatura}"
                ),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=TIMEOUT,
        )
        data = resposta.json()
    except (requests.RequestException, ValueError):
        return SearchResult(False, None, spec, [], stats, "shopee_network")
    if not 200 <= resposta.status_code < 300 or not isinstance(data, dict):
        return SearchResult(False, resposta.status_code, spec, [], stats, "shopee_api")
    if data.get("errors"):
        return SearchResult(False, resposta.status_code, spec, [], stats, "shopee_graphql")
    bloco = valor_caminho(data, "data", "productOfferV2") or {}
    nodes = bloco.get("nodes") or bloco.get("items") or bloco.get("products") or []
    if not isinstance(nodes, list):
        nodes = []
    stats.raw_offers = len(nodes)
    stats.unique_items = len(nodes)
    ofertas: list[Offer] = []
    for raw in nodes:
        if not isinstance(raw, dict):
            continue
        centavos = decimal_seguro(raw.get("priceMin") or raw.get("price") or raw.get("priceMax"))
        preco = centavos / 100 if centavos is not None else None
        rating = decimal_seguro(raw.get("ratingStar"))
        try:
            sold = int(raw.get("soldCount") or 0)
        except (TypeError, ValueError):
            sold = 0
        trusted = bool(
            rating is not None
            and rating >= Decimal(str(SHOPEE_MIN_RATING))
            and sold >= SHOPEE_MIN_SOLD
        )
        oferta, match = criar_oferta_externa(
            spec=spec,
            source="shopee",
            item_id=raw.get("productId"),
            product_id=raw.get("productId"),
            title=raw.get("productName"),
            price=preco,
            link=primeiro_texto(raw.get("offerLink"), raw.get("productLink")),
            seller_id=raw.get("shopId"),
            seller_name=raw.get("shopName"),
            condition="new",
            rating=rating,
            sold_count=sold,
            seller_trusted=trusted,
            trust_reason=(
                f"nota {rating} e {sold} vendas"
                if trusted
                else "nota ou volume de vendas abaixo do mínimo"
            ),
            raw_metadata=raw,
        )
        if oferta:
            ofertas.append(oferta)
        else:
            contabilizar_match(stats, match)
    stats.compatible = len(ofertas)
    stats.sources_succeeded = 1
    ofertas.sort(key=chave_ranking)
    return SearchResult(
        True, 200, spec, ofertas[:limite_resultados], stats, sources_used=["shopee"]
    )


def buscar_olx(termo: str, limite_resultados: int) -> SearchResult:
    spec = parse_search_spec(termo)
    stats = SearchStats(sources_queried=1)
    payload: dict[str, Any] = {
        "target": "olx.com.br",
        "type": "plp",
        "keyword": spec.raw,
        "sort": "price",
        "page": 1,
    }
    if GECKO_OLX_STATE:
        payload["state"] = GECKO_OLX_STATE
    try:
        resposta = requests.post(
            GECKO_API_URL,
            json=payload,
            headers={"Authorization": f"Bearer {GECKO_API_KEY}", "Accept": "application/json"},
            timeout=TIMEOUT,
        )
        data = resposta.json()
    except (requests.RequestException, ValueError):
        return SearchResult(False, None, spec, [], stats, "olx_connector_network")
    if not 200 <= resposta.status_code < 300 or not isinstance(data, dict):
        return SearchResult(False, resposta.status_code, spec, [], stats, "olx_connector_api")
    items = valor_caminho(data, "data", "items") or data.get("items") or []
    if not isinstance(items, list):
        items = []
    items = [item for item in items[:GECKO_MAX_RESULTS] if isinstance(item, dict)]
    stats.raw_offers = len(items)
    stats.unique_items = len(items)
    ofertas: list[Offer] = []
    for raw in items:
        location = raw.get("location") if isinstance(raw.get("location"), dict) else {}
        propriedades = raw.get("properties")
        if isinstance(propriedades, dict):
            evidence = " ".join(f"{k} {v}" for k, v in propriedades.items())
        elif isinstance(propriedades, list):
            evidence = " ".join(str(v) for v in propriedades)
        else:
            evidence = ""
        professional = bool_seguro(raw.get("professionalAd"))
        pay_delivery = bool(
            bool_seguro(raw.get("olxPayEnabled"))
            and bool_seguro(raw.get("olxDeliveryEnabled"))
        )
        trusted = bool(OLX_ALLOW_AUTOMATIC_ALERTS and professional and pay_delivery)
        oferta, match = criar_oferta_externa(
            spec=spec,
            source="olx",
            item_id=raw.get("id"),
            product_id=raw.get("id"),
            title=raw.get("title"),
            price=raw.get("price"),
            original_price=raw.get("oldPrice"),
            link=raw.get("url"),
            seller_id=raw.get("sellerId"),
            seller_name=raw.get("sellerName"),
            condition=raw.get("condition"),
            city=location.get("city"),
            state=location.get("state"),
            seller_trusted=trusted,
            trust_reason=(
                "anúncio profissional com pagamento e entrega OLX"
                if trusted
                else "identidade/reputação não confirmada para alerta"
            ),
            evidence_text=evidence,
            raw_metadata=raw,
        )
        if oferta:
            ofertas.append(oferta)
        else:
            contabilizar_match(stats, match)
    stats.compatible = len(ofertas)
    stats.sources_succeeded = 1
    ofertas.sort(key=chave_ranking)
    return SearchResult(True, 200, spec, ofertas[:limite_resultados], stats, sources_used=["olx"])


def buscar_gecko_loja(
    termo: str, limite_resultados: int, source: str
) -> SearchResult:
    """Fallback estruturado para Amazon/Shopee, sem scraping dentro do bot."""
    spec = parse_search_spec(termo)
    stats = SearchStats(sources_queried=1)
    targets = {"amazon": "amazon.com.br", "shopee": "shopee.com.br"}
    target = targets.get(source)
    if not target:
        return SearchResult(False, None, spec, [], stats, "unsupported_connector")
    try:
        resposta = requests.post(
            GECKO_API_URL,
            json={
                "target": target,
                "type": "plp",
                "keyword": spec.raw,
                "sort": "price",
                "page": 1,
            },
            headers={"Authorization": f"Bearer {GECKO_API_KEY}", "Accept": "application/json"},
            timeout=TIMEOUT,
        )
        data = resposta.json()
    except (requests.RequestException, ValueError):
        return SearchResult(False, None, spec, [], stats, f"{source}_connector_network")
    if not 200 <= resposta.status_code < 300 or not isinstance(data, dict):
        return SearchResult(
            False, resposta.status_code, spec, [], stats, f"{source}_connector_api"
        )
    items = valor_caminho(data, "data", "items") or data.get("items") or []
    if not isinstance(items, list):
        items = []
    items = [item for item in items[:GECKO_MAX_RESULTS] if isinstance(item, dict)]
    stats.raw_offers = len(items)
    stats.unique_items = len(items)
    ofertas: list[Offer] = []
    for raw in items:
        seller = raw.get("seller") if isinstance(raw.get("seller"), dict) else {}
        rating = decimal_seguro(
            raw.get("rating") or raw.get("ratingStar") or seller.get("rating")
        )
        sold_raw = raw.get("soldCount") or raw.get("sold") or seller.get("soldCount") or 0
        try:
            sold = int(sold_raw)
        except (TypeError, ValueError):
            sold = 0
        seller_name = primeiro_texto(
            raw.get("sellerName"), raw.get("shopName"), seller.get("name")
        )
        if source == "amazon":
            trusted = bool(
                bool_seguro(raw.get("isAmazon"))
                or "amazon" in normalizar_texto(seller_name)
            )
            if AMAZON_ALLOW_MARKETPLACE_SELLERS:
                trusted = True
        else:
            trusted = bool(
                rating is not None
                and rating >= Decimal(str(SHOPEE_MIN_RATING))
                and sold >= SHOPEE_MIN_SOLD
            )
        props = raw.get("properties") or raw.get("features") or raw.get("description") or ""
        if isinstance(props, dict):
            evidence = " ".join(f"{k} {v}" for k, v in props.items())
        elif isinstance(props, list):
            evidence = " ".join(str(v) for v in props)
        else:
            evidence = str(props)
        oferta, match = criar_oferta_externa(
            spec=spec,
            source=source,
            item_id=primeiro_texto(raw.get("id"), raw.get("asin"), raw.get("productId")),
            product_id=primeiro_texto(raw.get("asin"), raw.get("productId"), raw.get("id")),
            title=primeiro_texto(raw.get("title"), raw.get("name"), raw.get("productName")),
            price=primeiro_texto(
                raw.get("price"), raw.get("priceMin"), valor_caminho(raw, "buyBox", "price")
            ),
            original_price=primeiro_texto(
                raw.get("oldPrice"), raw.get("originalPrice"), raw.get("listPrice")
            ),
            link=primeiro_texto(
                raw.get("url"), raw.get("productLink"), raw.get("offerLink")
            ),
            seller_id=primeiro_texto(raw.get("sellerId"), raw.get("shopId"), seller.get("id")),
            seller_name=seller_name,
            condition=raw.get("condition") or "new",
            warranty=raw.get("warranty"),
            free_shipping=raw.get("freeShipping"),
            rating=rating,
            sold_count=sold,
            seller_trusted=trusted,
            trust_reason=(
                "vendedor confirmado pelo critério da fonte"
                if trusted
                else "vendedor não confirmado pelo conector"
            ),
            evidence_text=evidence,
            promotion_label=primeiro_texto(raw.get("deal"), raw.get("promotion")),
            coupon_raw=raw.get("coupon"),
            raw_metadata=raw,
        )
        if oferta:
            ofertas.append(oferta)
        else:
            contabilizar_match(stats, match)
    stats.compatible = len(ofertas)
    stats.sources_succeeded = 1
    ofertas.sort(key=chave_ranking)
    return SearchResult(
        True, 200, spec, ofertas[:limite_resultados], stats, sources_used=[source]
    )


def buscar_amazon_integrado(termo: str, limite_resultados: int) -> SearchResult:
    oficial = bool(
        AMAZON_CREATORS_CLIENT_ID
        and AMAZON_CREATORS_CLIENT_SECRET
        and AMAZON_PARTNER_TAG
    )
    if oficial:
        resultado = buscar_amazon(termo, limite_resultados)
        if resultado.ok or not GECKO_API_KEY:
            return resultado
        logger.info("Amazon oficial indisponível; usando conector de fallback")
    return buscar_gecko_loja(termo, limite_resultados, "amazon")


def buscar_shopee_integrado(termo: str, limite_resultados: int) -> SearchResult:
    oficial = bool(SHOPEE_AFFILIATE_APP_ID and SHOPEE_AFFILIATE_APP_SECRET)
    if oficial:
        resultado = buscar_shopee(termo, limite_resultados)
        if resultado.ok or not GECKO_API_KEY:
            return resultado
        logger.info("Shopee oficial indisponível; usando conector de fallback")
    return buscar_gecko_loja(termo, limite_resultados, "shopee")


def lista_universal(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [v for v in payload if isinstance(v, dict)]
    if not isinstance(payload, dict):
        return []
    for chave in ("results", "items", "offers", "products"):
        valor = payload.get(chave)
        if isinstance(valor, list):
            return [v for v in valor if isinstance(v, dict)]
    return []


def buscar_universal(
    termo: str, limite_resultados: int, fontes: Optional[set[str]] = None
) -> SearchResult:
    spec = parse_search_spec(termo)
    stats = SearchStats(sources_queried=1)
    headers = {"Accept": "application/json"}
    if UNIVERSAL_SEARCH_API_KEY:
        headers["Authorization"] = f"Bearer {UNIVERSAL_SEARCH_API_KEY}"
    params: dict[str, Any] = {
        "q": spec.raw,
        "limit": min(UNIVERSAL_MAX_RESULTS, max(1, limite_resultados * 4)),
    }
    if fontes:
        params["sources"] = ",".join(sorted(fontes))
    try:
        resposta = HTTP.get(
            UNIVERSAL_SEARCH_URL, params=params, headers=headers, timeout=TIMEOUT
        )
        data = resposta.json()
    except (requests.RequestException, ValueError):
        return SearchResult(False, None, spec, [], stats, "universal_network")
    if not 200 <= resposta.status_code < 300:
        return SearchResult(False, resposta.status_code, spec, [], stats, "universal_api")
    items = lista_universal(data)
    stats.raw_offers = len(items)
    stats.unique_items = len(items)
    ofertas: list[Offer] = []
    sources_used: list[str] = []
    for raw in items:
        fonte = normalizar_fonte(raw.get("source") or raw.get("store") or "universal")
        if fontes and fonte not in fontes and "universal" not in fontes:
            continue
        vendedor = raw.get("seller") if isinstance(raw.get("seller"), dict) else {}
        frete = raw.get("shipping") if isinstance(raw.get("shipping"), dict) else {}
        local = raw.get("location") if isinstance(raw.get("location"), dict) else {}
        trusted_flag = raw.get("trusted")
        trusted = (
            bool_seguro(trusted_flag)
            if trusted_flag is not None
            else fonte in UNIVERSAL_TRUSTED_SOURCES
        )
        descricao = primeiro_texto(raw.get("description"), raw.get("attributes_text"))
        oferta, match = criar_oferta_externa(
            spec=spec,
            source=fonte,
            item_id=primeiro_texto(raw.get("id"), raw.get("item_id")),
            product_id=primeiro_texto(raw.get("product_id"), raw.get("sku")),
            title=primeiro_texto(raw.get("title"), raw.get("name")),
            price=raw.get("price"),
            original_price=raw.get("original_price"),
            link=primeiro_texto(raw.get("url"), raw.get("link")),
            seller_id=primeiro_texto(vendedor.get("id"), raw.get("seller_id")),
            seller_name=primeiro_texto(vendedor.get("name"), raw.get("seller_name")),
            condition=raw.get("condition") or "new",
            warranty=raw.get("warranty"),
            free_shipping=(
                bool_seguro(frete.get("free"))
                if frete.get("free") is not None
                else (
                    bool_seguro(raw.get("free_shipping"))
                    if raw.get("free_shipping") is not None
                    else None
                )
            ),
            shipping_cost=frete.get("cost") or raw.get("shipping_cost"),
            city=primeiro_texto(local.get("city"), raw.get("city")),
            state=primeiro_texto(local.get("state"), raw.get("state")),
            rating=primeiro_texto(vendedor.get("rating"), raw.get("rating")),
            rating_count=primeiro_texto(vendedor.get("rating_count"), raw.get("rating_count"), 0),
            sold_count=primeiro_texto(vendedor.get("sold_count"), raw.get("sold_count"), 0),
            seller_trusted=trusted,
            trust_reason=primeiro_texto(raw.get("trust_reason"), "conector não marcou como confiável"),
            evidence_text=descricao,
            promotion_label=primeiro_texto(raw.get("promotion"), raw.get("promotion_label")),
            coupon_raw=raw.get("coupon"),
            raw_metadata=raw,
        )
        if oferta:
            ofertas.append(oferta)
            sources_used.append(fonte)
        else:
            contabilizar_match(stats, match)
    stats.compatible = len(ofertas)
    stats.sources_succeeded = 1
    ofertas.sort(key=chave_ranking)
    return SearchResult(
        True,
        200,
        spec,
        ofertas[:limite_resultados],
        stats,
        sources_used=lista_unica(sources_used),
    )


def fontes_configuradas() -> dict[str, bool]:
    return {
        "mercadolivre": bool("mercadolivre" in ENABLED_SOURCES and TOKENS.conectado()),
        "amazon": bool(
            "amazon" in ENABLED_SOURCES
            and (
                (
                    AMAZON_CREATORS_CLIENT_ID
                    and AMAZON_CREATORS_CLIENT_SECRET
                    and AMAZON_PARTNER_TAG
                )
                or GECKO_API_KEY
            )
        ),
        "shopee": bool(
            "shopee" in ENABLED_SOURCES
            and (
                (SHOPEE_AFFILIATE_APP_ID and SHOPEE_AFFILIATE_APP_SECRET)
                or GECKO_API_KEY
            )
        ),
        "olx": bool("olx" in ENABLED_SOURCES and GECKO_API_KEY),
        "buscape": bool("buscape" in ENABLED_SOURCES and UNIVERSAL_SEARCH_URL),
        "universal": bool("universal" in ENABLED_SOURCES and UNIVERSAL_SEARCH_URL),
    }


def separar_fonte_busca(termo: str) -> tuple[str, Optional[set[str]]]:
    texto = " ".join(str(termo or "").split())
    match = re.match(r"^([^:]{1,24})\s*:\s*(.+)$", texto)
    if not match:
        return texto, None
    prefixo = normalizar_fonte(match.group(1))
    if prefixo in {"todos", "all"}:
        return match.group(2).strip(), None
    if prefixo in set(SOURCE_LABELS):
        return match.group(2).strip(), {prefixo}
    return texto, None


def links_comparacao(termo: str) -> dict[str, str]:
    q = urlencode({"q": termo})
    keyword = urlencode({"keyword": termo})
    return {
        "Amazon": f"https://www.amazon.com.br/s?{urlencode({'k': termo})}",
        "Shopee": f"https://shopee.com.br/search?{keyword}",
        "OLX": f"https://www.olx.com.br/brasil?{q}",
        "Buscapé": f"https://www.buscape.com.br/search?{q}",
    }


def somar_stats(destino: SearchStats, origem: SearchStats) -> None:
    for campo in (
        "products_received", "products_selected", "raw_offers", "unique_items",
        "rejected_accessory", "rejected_conflict", "rejected_unconfirmed",
        "rejected_no_price", "compatible", "sources_queried", "sources_succeeded",
    ):
        setattr(destino, campo, getattr(destino, campo) + getattr(origem, campo))


def buscar_ofertas_completas(
    termo: str, limite_resultados: int = MAX_RESULTS
) -> SearchResult:
    limite_resultados = max(1, min(int(limite_resultados), MAX_OFFERS_TO_ENRICH))
    consulta, selecionadas = separar_fonte_busca(termo)
    spec = parse_search_spec(consulta)
    configuradas = fontes_configuradas()
    if selecionadas is None:
        fontes = {fonte for fonte, pronta in configuradas.items() if pronta}
    else:
        fontes = {fonte for fonte in selecionadas if configuradas.get(fonte, False)}
    if not fontes:
        solicitadas = selecionadas or set(configuradas)
        faltantes = ", ".join(SOURCE_LABELS.get(f, f) for f in sorted(solicitadas))
        return SearchResult(
            False,
            None,
            spec,
            [],
            SearchStats(),
            "no_sources_configured" + (f":{faltantes}" if faltantes else ""),
            comparison_links=links_comparacao(consulta),
        )

    tarefas: dict[str, Any] = {}
    universal_fontes = {f for f in fontes if f in {"buscape", "universal"}}
    if "mercadolivre" in fontes:
        tarefas["mercadolivre"] = lambda: buscar_mercado_livre(
            consulta, limite_resultados
        )
    if "amazon" in fontes:
        tarefas["amazon"] = lambda: buscar_amazon_integrado(
            consulta, limite_resultados
        )
    if "shopee" in fontes:
        tarefas["shopee"] = lambda: buscar_shopee_integrado(
            consulta, limite_resultados
        )
    if "olx" in fontes:
        tarefas["olx"] = lambda: buscar_olx(consulta, limite_resultados)
    if universal_fontes:
        tarefas["universal"] = lambda: buscar_universal(
            consulta,
            limite_resultados,
            None if "universal" in universal_fontes else universal_fontes,
        )

    resultados: dict[str, SearchResult] = {}
    with ThreadPoolExecutor(max_workers=min(5, len(tarefas))) as pool:
        futuros = {fonte: pool.submit(funcao) for fonte, funcao in tarefas.items()}
        for fonte, futuro in futuros.items():
            try:
                resultados[fonte] = futuro.result()
            except Exception:
                logger.exception("Falha inesperada no conector %s", fonte)
                resultados[fonte] = SearchResult(
                    False, None, spec, [], SearchStats(sources_queried=1), "unexpected"
                )

    stats = SearchStats()
    ofertas: list[Offer] = []
    errors: dict[str, str] = {}
    sources_used: list[str] = []
    algum_sucesso = False
    for fonte, resultado in resultados.items():
        somar_stats(stats, resultado.stats)
        if resultado.ok:
            algum_sucesso = True
            ofertas.extend(resultado.offers)
            sources_used.extend(resultado.sources_used or [fonte])
        else:
            errors[fonte] = resultado.error or f"http_{resultado.status}"

    feeds = carregar_cupons_feed() if (COUPONS_JSON or COUPON_FEED_URLS) else []
    for oferta in ofertas:
        anexar_melhor_cupom(oferta, consulta, feeds)
    unicas: dict[tuple[str, str], Offer] = {}
    for oferta in ofertas:
        chave = (oferta.source, oferta.item_id or oferta.link)
        atual = unicas.get(chave)
        if atual is None or oferta.price < atual.price:
            unicas[chave] = oferta
    ofertas = list(unicas.values())
    ofertas.sort(key=chave_ranking)
    stats.compatible = len(ofertas)
    return SearchResult(
        algum_sucesso,
        200 if algum_sucesso else None,
        spec,
        ofertas[:limite_resultados],
        stats,
        "" if algum_sucesso else "all_sources_failed",
        sources_used=lista_unica(sources_used),
        provider_errors=errors,
        comparison_links=links_comparacao(consulta),
    )


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

SAFE_REPUTATION_LEVELS = {"5_green", "4_light_green"}


def reputation_level(oferta: Offer) -> str:
    return primeiro_texto(
        obter_dict(oferta.seller, "seller_reputation").get("level_id")
    )


def garantia_insegura(garantia: str) -> bool:
    norm = normalizar_texto(garantia)
    return bool(norm and ("sem garantia" in norm or "no warranty" in norm))


def oferta_elegivel_alerta(oferta: Offer) -> bool:
    base = bool(
        oferta.item_id
        and oferta.link
        and oferta.condition == "new"
        and not garantia_insegura(oferta.warranty)
        and oferta.price_confirmed
    )
    if not base:
        return False
    if oferta.source == "mercadolivre":
        return reputation_level(oferta) in SAFE_REPUTATION_LEVELS
    return oferta.seller_trusted is True


def formatar_vendedor(oferta: Offer) -> str:
    seller = oferta.seller if isinstance(oferta.seller, dict) else {}
    seller_id = oferta.seller_id
    nickname = limitar_texto(oferta.seller_name or seller.get("nickname"), 25)
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
    if oferta.source != "mercadolivre":
        if oferta.rating is not None:
            partes.append(f"⭐ {str(oferta.rating).replace('.', ',')}")
        if oferta.sold_count:
            partes.append(f"{oferta.sold_count} vendas")
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
        icone = "⚠️" if garantia_insegura(oferta.warranty) else "🛡"
        partes.append(f"{icone} {oferta.warranty}")
    else:
        partes.append("⚠️ garantia não informada")
    return " • ".join(partes)


def montar_card(oferta: Offer, posicao: int) -> str:
    marcador = MEDALHAS.get(posicao, f"#{posicao}")
    linhas = [
        f"<b>{marcador} {html.escape(brl(oferta.price))}</b> "
        f"• {html.escape(oferta.source_label)}"
    ]
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

    if oferta.promotion_label:
        linhas.append(
            "🏷 " + html.escape(f"Promoção pública: {oferta.promotion_label}")
        )

    if oferta.coupon:
        desconto = oferta.coupon.discount_for(oferta.price)
        estimado = oferta.estimated_coupon_price
        descricao: list[str] = []
        if oferta.coupon.discount_type == "percent" and oferta.coupon.value is not None:
            descricao.append(f"{format(oferta.coupon.value, 'f')}% OFF")
        elif desconto is not None:
            descricao.append(f"{brl(desconto)} OFF")
        if oferta.coupon.max_discount is not None:
            descricao.append(f"até {brl(oferta.coupon.max_discount)}")
        if oferta.coupon.min_purchase is not None:
            descricao.append(f"mín. {brl(oferta.coupon.min_purchase)}")
        if oferta.coupon.code:
            descricao.append(f"código {oferta.coupon.code}")
        if not descricao and oferta.coupon.label:
            descricao.append(oferta.coupon.label)
        linhas.append("🎟 " + html.escape(" • ".join(descricao)))
        if estimado is not None:
            linhas.append(
                f"💡 Estimado com cupom: <b>{html.escape(brl(estimado))}</b>"
            )
        if not oferta.coupon.terms_verified:
            linhas.append("⚠️ <i>Regras do cupom ainda não verificadas</i>")
        elif oferta.coupon.buyer_specific:
            linhas.append("⚠️ <i>Cupom segmentado para contas elegíveis</i>")
        else:
            linhas.append("⚠️ <i>Cupom sujeito à validação no carrinho</i>")

    linhas.append(html.escape(formatar_frete_garantia(oferta)))

    local = ", ".join(filter(None, [oferta.city, oferta.state]))
    vendedor = formatar_vendedor(oferta)
    linha_vendedor = f"👤 {vendedor}"
    if local:
        linha_vendedor = f"📍 {local} • {linha_vendedor}"
    linhas.append(html.escape(linha_vendedor))

    level = reputation_level(oferta)
    if oferta.source == "mercadolivre" and level in {"1_red", "2_orange"}:
        linhas.append("⚠️ <b>Alto risco — não elegível para alerta automático</b>")
    elif oferta.source == "mercadolivre" and level == "3_yellow":
        linhas.append("⚠️ Reputação intermediária — alerta automático bloqueado")
    elif oferta.source == "mercadolivre" and not level:
        linhas.append("⚠️ Reputação não confirmada — alerta automático bloqueado")
    elif oferta.source != "mercadolivre" and oferta.seller_trusted is not True:
        motivo = oferta.trust_reason or "vendedor não confirmado"
        linhas.append(
            "⚠️ " + html.escape(f"{motivo} — alerta automático bloqueado")
        )
    elif garantia_insegura(oferta.warranty):
        linhas.append("⚠️ Sem garantia declarada — alerta automático bloqueado")
    elif oferta.condition != "new":
        linhas.append("⚠️ Item não novo — alerta automático bloqueado")

    if oferta.deal_ids and not oferta.promotion_label:
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
    fontes = ", ".join(
        SOURCE_LABELS.get(fonte, fonte.title()) for fonte in resultado.sources_used
    )
    cabecalho = f"✅ <b>{quantidade} {rotulo}</b> • {criterio}"
    if fontes:
        cabecalho += f"\n🏪 {html.escape(fontes)}"
    blocos = [cabecalho]
    if resultado.provider_errors:
        indisponiveis = ", ".join(
            SOURCE_LABELS.get(fonte, fonte.title())
            for fonte in resultado.provider_errors
        )
        blocos.append(
            "⚠️ Fonte(s) temporariamente indisponível(is): "
            + html.escape(indisponiveis)
        )
    for indice, oferta in enumerate(ofertas, start=1):
        blocos.append(montar_card(oferta, indice))
    blocos.append(
        "<i>Preço principal = valor confirmado pela fonte. Preço com cupom = estimativa; "
        "conta, estoque, pagamento, CEP, limite e carrinho podem alterar a aplicação.</i>"
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
    possui_configuracao = any(
        valor is not None
        for valor in (spec.chip, spec.ram_gb, spec.storage_gb, spec.voltage, spec.screen_inches)
    )
    linhas = [
        "❌ <b>Nenhuma oferta totalmente compatível</b>",
        "",
        f"Busca validada: <b>{html.escape(configuracao)}</b>",
    ]
    if STRICT_CONFIGURATION and possui_configuracao:
        linhas.extend(
            [
                "",
                "Anúncios sem confirmação explícita da configuração foram excluídos.",
            ]
        )
    elif not possui_configuracao:
        linhas.extend(
            [
                "",
                "Resultados com modelo ou tipo de produto diferente foram excluídos.",
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
    if possui_configuracao:
        linhas.extend(
            [
                "",
                "Tente escrever capacidades com unidade, por exemplo:",
                "<code>/buscar Mac Mini M4 16 GB RAM 512 GB SSD</code>",
            ]
        )
    else:
        linhas.extend(
            [
                "",
                "Tente manter marca e código exato do modelo, por exemplo:",
                "<code>/buscar Teclado Logitech Pebble Keys 2 K380s</code>",
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
            "Configuração → lojas → preço → cupom → vendedor → link",
            message_thread_id=message_thread_id,
        )
        resultado = buscar_ofertas_completas(termo)
        if not resultado.ok:
            if resultado.error.startswith("no_sources_configured"):
                texto = (
                    "⚠️ <b>A fonte solicitada ainda não está configurada.</b>\n\n"
                    "Use /status para ver as integrações prontas."
                )
            elif resultado.status == 401 or resultado.error == "not_authenticated":
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
# MONITORAMENTO AUTOMÁTICO E ALERTAS DE PREÇO
# =============================================================================


MONITOR_RUN_LOCK = threading.Lock()


def decimal_para_centavos(valor: Decimal) -> int:
    return int((valor * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def centavos_para_decimal(valor: Any) -> Optional[Decimal]:
    try:
        return (Decimal(int(valor)) / 100).quantize(Decimal("0.01"))
    except (TypeError, ValueError, InvalidOperation):
        return None


def parse_preco_monitor(texto: str) -> Optional[int]:
    valor = re.sub(r"(?i)r\$", "", str(texto or "")).strip().replace(" ", "")
    if not valor:
        return None
    if "," in valor:
        valor = valor.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", valor):
        valor = valor.replace(".", "")
    numero = decimal_seguro(valor)
    if numero is None or numero <= 0:
        return None
    return decimal_para_centavos(numero)


def parse_monitor_request(argumento: str) -> tuple[str, Optional[int], str]:
    partes = [parte.strip() for parte in argumento.rsplit("|", 1)]
    query = partes[0] if partes else ""
    target: Optional[int] = None
    if len(partes) == 2:
        if not partes[1]:
            return "", None, "Preço-alvo vazio."
        target = parse_preco_monitor(partes[1])
        if target is None:
            return "", None, "Preço-alvo inválido."
    return query, target, ""


def monitor_target_text(monitor: dict[str, Any]) -> str:
    target = centavos_para_decimal(monitor.get("target_price_cents"))
    return brl(target) if target is not None else "detecção pelo histórico"


def pode_realertar(monitor: dict[str, Any], item_id: str, price_cents: int, agora: int) -> bool:
    last_price = monitor.get("last_alert_price_cents")
    last_at = monitor.get("last_alert_at")
    last_item = primeiro_texto(monitor.get("last_alert_item_id"))
    if last_price is None or last_at is None:
        return True
    try:
        last_price_int = int(last_price)
        last_at_int = int(last_at)
    except (TypeError, ValueError):
        return True
    queda_minima = Decimal("1") - Decimal(str(ALERT_RENOTIFY_DROP_PERCENT)) / 100
    if Decimal(price_cents) <= Decimal(last_price_int) * queda_minima:
        return True
    cooldown_ok = agora - last_at_int >= ALERT_COOLDOWN_HOURS * 3600
    return bool(item_id != last_item and cooldown_ok and price_cents < last_price_int)


def montar_alerta_promocao(
    monitor: dict[str, Any],
    oferta: Offer,
    baseline_cents: Optional[int],
    history_checks: int,
    motivos: list[str],
    confirmations: int,
) -> str:
    linhas = [
        "🚨 <b>PROMOÇÃO CONFIRMADA</b>",
        "",
        f"<b>{html.escape(brl(oferta.price))}</b>",
        html.escape(limitar_texto(oferta.config_label, 100)),
        f"🏪 {html.escape(oferta.source_label)}",
    ]
    target = centavos_para_decimal(monitor.get("target_price_cents"))
    if target is not None:
        linhas.append(f"🎯 Preço-alvo: {html.escape(brl(target))}")
    if baseline_cents is not None:
        baseline = centavos_para_decimal(baseline_cents)
        if baseline and baseline > 0:
            queda = ((baseline - oferta.price) / baseline * 100).quantize(Decimal("0.1"))
            linhas.append(
                "📉 "
                + html.escape(
                    f"{str(queda).replace('.', ',')}% abaixo da mediana de "
                    f"{history_checks} verificações"
                )
            )
    linhas.append(f"✅ Confirmada em {confirmations} verificações consecutivas")
    linhas.append("🟢 Vendedor elegível para alerta automático")
    linhas.append(html.escape(formatar_frete_garantia(oferta)))
    local = ", ".join(filter(None, [oferta.city, oferta.state]))
    seller = formatar_vendedor(oferta)
    linhas.append(html.escape(f"📍 {local} • 👤 {seller}" if local else f"👤 {seller}"))
    if motivos:
        linhas.append("🔎 " + html.escape(" e ".join(motivos)))
    if oferta.link:
        linhas.append(f'🔗 <a href="{html.escape(oferta.link, quote=True)}">Abrir anúncio</a>')
    linhas.append(
        "\n<i>O alerta usa histórico próprio e reputação; não usa o preço anterior do anúncio como prova.</i>"
    )
    return "\n".join(linhas)


def montar_alerta_cupom(
    monitor: dict[str, Any], oferta: Offer, confirmations: int
) -> str:
    cupom = oferta.coupon
    estimado = oferta.estimated_coupon_price
    if cupom is None or estimado is None:
        return ""
    desconto = cupom.discount_for(oferta.price) or Decimal("0")
    linhas = [
        "🎟 <b>CUPOM FORTE DETECTADO</b>",
        "",
        html.escape(limitar_texto(oferta.config_label, 100)),
        f"🏪 {html.escape(oferta.source_label)}",
        f"Preço confirmado: <b>{html.escape(brl(oferta.price))}</b>",
        f"Estimativa com cupom: <b>{html.escape(brl(estimado))}</b>",
        f"Economia estimada: {html.escape(brl(desconto))}",
    ]
    detalhes: list[str] = []
    if cupom.discount_type == "percent" and cupom.value is not None:
        detalhes.append(f"{format(cupom.value, 'f')}% OFF")
    if cupom.max_discount is not None:
        detalhes.append(f"limite {brl(cupom.max_discount)}")
    if cupom.min_purchase is not None:
        detalhes.append(f"compra mínima {brl(cupom.min_purchase)}")
    if cupom.code:
        detalhes.append(f"código {cupom.code}")
    elif cupom.label:
        detalhes.append(cupom.label)
    if detalhes:
        linhas.append("🏷 " + html.escape(" • ".join(detalhes)))
    target = centavos_para_decimal(monitor.get("target_price_cents"))
    if target is not None:
        atingiu = "atinge" if estimado <= target else "ainda não atinge"
        linhas.append(
            f"🎯 {html.escape(atingiu)} o alvo de {html.escape(brl(target))}"
        )
    linhas.append(f"✅ Encontrado em {confirmations} verificações consecutivas")
    linhas.append("🟢 Vendedor elegível para alerta automático")
    if oferta.link:
        linhas.append(
            f'🔗 <a href="{html.escape(oferta.link, quote=True)}">Abrir anúncio</a>'
        )
    if cupom.url:
        linhas.append(
            f'📄 <a href="{html.escape(cupom.url, quote=True)}">Ver regras do cupom</a>'
        )
    linhas.append(
        "\n⚠️ <b>Valide no carrinho.</b> O cupom pode depender da conta, estoque, "
        "pagamento, CEP, limite de usos e produtos participantes."
    )
    return "\n".join(linhas)


def processar_monitor(
    monitor: dict[str, Any], resultado: Optional[SearchResult] = None
) -> str:
    monitor_id = str(monitor["id"])
    checked_at = int(time.time())
    if resultado is None:
        resultado = buscar_ofertas_completas(
            str(monitor["query"]), MONITOR_CANDIDATES_LIMIT
        )
    if not resultado.ok:
        STORE.update_pending(monitor_id, None, None, 0, checked_at)
        return "api_error"

    baseline_cents, history_checks = STORE.history_baseline(monitor_id, checked_at)
    elegiveis: list[Offer] = []
    for oferta in resultado.offers:
        elegivel = oferta_elegivel_alerta(oferta)
        STORE.record_history(monitor_id, checked_at, oferta, elegivel)
        if elegivel:
            elegiveis.append(oferta)

    if not elegiveis:
        STORE.update_pending(monitor_id, None, None, 0, checked_at)
        return "no_safe_offer"

    elegiveis.sort(key=lambda oferta: oferta.price)
    melhor_confirmado = elegiveis[0]
    confirmed_cents = decimal_para_centavos(melhor_confirmado.price)
    target_cents = monitor.get("target_price_cents")
    target_hit = target_cents is not None and confirmed_cents <= int(target_cents)

    history_hit = False
    if baseline_cents and history_checks >= MONITOR_MIN_HISTORY_CHECKS:
        percentual = Decimal("1") - Decimal(str(ALERT_DROP_PERCENT)) / 100
        limite = int(Decimal(baseline_cents) * percentual)
        queda_absoluta = baseline_cents - confirmed_cents
        history_hit = bool(
            confirmed_cents <= limite
            and queda_absoluta >= int(ALERT_MIN_DROP_REAIS * 100)
        )

    tipo_sinal = ""
    melhor = melhor_confirmado
    signal_price = melhor.price
    signal_id = f"confirmed:{melhor.signal_id}"
    if target_hit or history_hit:
        tipo_sinal = "confirmed"
    elif COUPON_ALERTS_ENABLED:
        cupons = [
            oferta
            for oferta in elegiveis
            if oferta.coupon is not None
            and oferta.estimated_coupon_price is not None
            and oferta.coupon.is_good(oferta.price)
            and oferta.coupon.terms_verified
            and not oferta.coupon.buyer_specific
        ]
        cupons.sort(
            key=lambda oferta: oferta.estimated_coupon_price
            or Decimal("999999999")
        )
        if cupons:
            melhor = cupons[0]
            signal_price = melhor.estimated_coupon_price or melhor.price
            signal_id = (
                f"coupon:{melhor.signal_id}:"
                f"{melhor.coupon.fingerprint() if melhor.coupon else 'unknown'}"
            )
            tipo_sinal = "coupon"

    if not tipo_sinal:
        STORE.update_pending(monitor_id, None, None, 0, checked_at)
        return "observed"

    current_cents = decimal_para_centavos(signal_price)

    pending_item = primeiro_texto(monitor.get("pending_item_id"))
    pending_price = monitor.get("pending_price_cents")
    try:
        pending_count = int(monitor.get("pending_count") or 0)
        pending_price_int = int(pending_price) if pending_price is not None else None
    except (TypeError, ValueError):
        pending_count, pending_price_int = 0, None
    mesma_oferta = pending_item == signal_id
    preco_confirmado = bool(
        pending_price_int is not None
        and current_cents <= int(Decimal(pending_price_int) * Decimal("1.01"))
    )
    confirmations = pending_count + 1 if mesma_oferta and preco_confirmado else 1
    STORE.update_pending(
        monitor_id, signal_id, current_cents, confirmations, checked_at
    )
    if confirmations < ALERT_CONFIRMATIONS:
        return "pending_confirmation"

    monitor_atual = STORE.get_monitor(monitor_id) or monitor
    if not pode_realertar(monitor_atual, signal_id, current_cents, checked_at):
        return "already_alerted"

    motivos = []
    if target_hit:
        motivos.append("abaixo do preço-alvo")
    if history_hit:
        motivos.append("queda real no histórico")
    if tipo_sinal == "confirmed":
        texto = montar_alerta_promocao(
            monitor_atual,
            melhor,
            baseline_cents,
            history_checks,
            motivos,
            confirmations,
        )
    else:
        texto = montar_alerta_cupom(monitor_atual, melhor, confirmations)
    message_id = send_message(
        int(monitor["chat_id"]),
        texto,
        message_thread_id=(
            int(monitor["thread_id"]) if monitor.get("thread_id") is not None else None
        ),
    )
    if message_id is not None:
        STORE.mark_alert(monitor_id, signal_id, current_cents, checked_at)
        return "alert_sent"
    return "telegram_error"


def executar_monitores(
    *, chat_id: Optional[int] = None, monitor_ids: Optional[set[str]] = None
) -> dict[str, int]:
    resumo: dict[str, int] = {"checked": 0, "alerts": 0, "errors": 0}
    if not STORE.available:
        resumo["errors"] = 1
        return resumo
    if not MONITOR_RUN_LOCK.acquire(blocking=False):
        resumo["errors"] = 1
        return resumo
    try:
        monitores = STORE.list_monitors(chat_id=chat_id, active_only=True)
        if monitor_ids is not None:
            monitores = [m for m in monitores if str(m["id"]) in monitor_ids]
        resultados_cache: dict[str, SearchResult] = {}
        for monitor in monitores:
            try:
                chave_busca = normalizar_texto(monitor.get("query"))
                resultado_busca = resultados_cache.get(chave_busca)
                if resultado_busca is None:
                    resultado_busca = buscar_ofertas_completas(
                        str(monitor["query"]), MONITOR_CANDIDATES_LIMIT
                    )
                    resultados_cache[chave_busca] = resultado_busca
                status = processar_monitor(monitor, resultado_busca)
                resumo["checked"] += 1
                if status == "alert_sent":
                    resumo["alerts"] += 1
                elif status in {"api_error", "telegram_error"}:
                    resumo["errors"] += 1
            except Exception:
                logger.exception("Falha no monitor %s", monitor.get("id"))
                resumo["errors"] += 1
        STORE.cleanup_history()
        return resumo
    finally:
        MONITOR_RUN_LOCK.release()


def executar_verificacao_manual(
    chat_id: int,
    thread_id: Optional[int],
    monitor_ids: Optional[set[str]] = None,
) -> None:
    status_id = send_message(
        chat_id,
        "🔄 <b>VERIFICANDO MONITORES…</b>",
        message_thread_id=thread_id,
    )
    resumo = executar_monitores(chat_id=chat_id, monitor_ids=monitor_ids)
    texto = (
        "✅ <b>VERIFICAÇÃO CONCLUÍDA</b>\n\n"
        f"Monitores verificados: {resumo['checked']}\n"
        f"Novos alertas: {resumo['alerts']}\n"
        f"Erros: {resumo['errors']}"
    )
    editar_ou_enviar(chat_id, status_id, texto, thread_id)


def comando_monitorar(
    chat_id: int, thread_id: Optional[int], argumento: str
) -> None:
    query, target_cents, erro = parse_monitor_request(argumento)
    if erro:
        send_message(chat_id, f"❌ {html.escape(erro)}", message_thread_id=thread_id)
        return
    if not query:
        send_message(
            chat_id,
            "Use:\n<code>/monitorar produto | preço-alvo</code>\n\n"
            "Exemplo:\n<code>/monitorar Mac Mini M4 16 512 | 6300</code>",
            message_thread_id=thread_id,
        )
        return
    if len(query) > 180:
        send_message(chat_id, "❌ A busca é longa demais.", message_thread_id=thread_id)
        return
    monitor, code = STORE.create_monitor(chat_id, thread_id, query, target_cents)
    if not monitor:
        mensagem = {
            "limit": "Você atingiu o limite de monitores ativos.",
            "storage_unavailable": "A persistência está indisponível.",
        }.get(code, "Não foi possível criar o monitor.")
        send_message(chat_id, f"❌ {mensagem}", message_thread_id=thread_id)
        return

    if code == "duplicate":
        send_message(
            chat_id,
            "ℹ️ Este monitor já existe.\n\n"
            f"ID: <code>{html.escape(str(monitor['id']))}</code>\n"
            f"Busca: {html.escape(str(monitor['query']))}\n"
            f"Alvo: {html.escape(monitor_target_text(monitor))}",
            message_thread_id=thread_id,
        )
        return

    linhas = [
        "✅ <b>MONITOR CRIADO</b>",
        "",
        f"ID: <code>{html.escape(str(monitor['id']))}</code>",
        f"Busca: {html.escape(str(monitor['query']))}",
        f"Alvo: {html.escape(monitor_target_text(monitor))}",
        "",
        f"O alerta exige vendedor confiável e {ALERT_CONFIRMATIONS} verificações consecutivas.",
        f"Cupons fortes (≥ {COUPON_MIN_PERCENT:g}% ou ≥ {brl(COUPON_MIN_REAIS)}) "
        "também geram aviso quando as regras estão verificadas; o valor continua "
        "identificado como estimativa até o carrinho.",
    ]
    if target_cents is None:
        linhas.append(
            f"O bot aprenderá o preço normal após pelo menos {MONITOR_MIN_HISTORY_CHECKS} verificações."
        )
    if not STORE.durable:
        linhas.append(
            "⚠️ O banco atual é local e pode ser perdido num reinício. Configure DATABASE_URL para persistência definitiva."
        )
    send_message(chat_id, "\n".join(linhas), message_thread_id=thread_id)
    EXECUTOR.submit(
        executar_verificacao_manual, chat_id, thread_id, {str(monitor["id"])}
    )


def comando_monitores(chat_id: int, thread_id: Optional[int]) -> None:
    monitores = STORE.list_monitors(chat_id=chat_id, active_only=True)
    if not monitores:
        send_message(
            chat_id,
            "Você não possui monitores ativos.\n\n"
            "Crie um com:\n<code>/monitorar produto | preço-alvo</code>",
            message_thread_id=thread_id,
        )
        return
    blocos = ["📡 <b>MONITORES ATIVOS</b>"]
    for monitor in monitores:
        blocos.append(
            f"<b>{html.escape(str(monitor['id']))}</b> • {html.escape(monitor_target_text(monitor))}\n"
            f"{html.escape(limitar_texto(monitor['query'], 100))}"
        )
    blocos.append("Use <code>/parar ID</code> para desativar um monitor.")
    send_message(chat_id, "\n\n".join(blocos), message_thread_id=thread_id)


def comando_parar(chat_id: int, thread_id: Optional[int], argumento: str) -> None:
    monitor_id = argumento.strip().lower()
    if not monitor_id:
        send_message(chat_id, "Use: <code>/parar ID</code>", message_thread_id=thread_id)
        return
    if STORE.deactivate_monitor(monitor_id, chat_id):
        send_message(
            chat_id,
            f"⏹ Monitor <code>{html.escape(monitor_id)}</code> desativado.",
            message_thread_id=thread_id,
        )
    else:
        send_message(chat_id, "❌ Monitor não encontrado.", message_thread_id=thread_id)


def comando_historico(chat_id: int, thread_id: Optional[int], argumento: str) -> None:
    monitor_id = argumento.strip().lower()
    monitor = STORE.get_monitor(monitor_id, chat_id) if monitor_id else None
    if not monitor:
        send_message(
            chat_id,
            "Use: <code>/historico ID</code>",
            message_thread_id=thread_id,
        )
        return
    baseline_cents, checks = STORE.history_baseline(monitor_id, int(time.time()) + 1)
    baseline = centavos_para_decimal(baseline_cents)
    ultima = monitor.get("last_checked_at")
    ultima_texto = (
        time.strftime("%d/%m/%Y %H:%M", time.localtime(int(ultima)))
        if ultima
        else "ainda não verificado"
    )
    send_message(
        chat_id,
        "📊 <b>HISTÓRICO DO MONITOR</b>\n\n"
        f"Busca: {html.escape(str(monitor['query']))}\n"
        f"Mediana: {html.escape(brl(baseline)) if baseline is not None else 'aprendendo'}\n"
        f"Verificações válidas: {checks}\n"
        f"Última verificação: {html.escape(ultima_texto)}",
        message_thread_id=thread_id,
    )


# =============================================================================
# OAUTH E ROTAS WEB
# =============================================================================


OAUTH_STATE_TTL = 600
OAUTH_CLOCK_SKEW = 60


def base64_url_encode(valor: bytes) -> str:
    return base64.urlsafe_b64encode(valor).rstrip(b"=").decode("ascii")


def base64_url_decode(valor: str) -> bytes:
    padding = "=" * (-len(valor) % 4)
    return base64.urlsafe_b64decode((valor + padding).encode("ascii"))


def criar_oauth_state() -> str:
    """Cria state assinado, válido mesmo se o callback cair em outro worker."""
    segredo = MELI_CLIENT_SECRET.encode("utf-8")
    emitido_em = int(time.time())
    nonce = secrets.token_urlsafe(18)
    payload = f"{emitido_em}.{nonce}".encode("utf-8")
    assinatura = hmac.new(segredo, payload, hashlib.sha256).digest()
    return f"{base64_url_encode(payload)}.{base64_url_encode(assinatura)}"


def consumir_oauth_state(state: str) -> bool:
    """Valida assinatura e idade sem depender da memória do processo."""
    if not state or not MELI_CLIENT_SECRET:
        return False
    try:
        payload_b64, assinatura_b64 = state.split(".", 1)
        payload = base64_url_decode(payload_b64)
        assinatura = base64_url_decode(assinatura_b64)
        esperada = hmac.new(
            MELI_CLIENT_SECRET.encode("utf-8"), payload, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(assinatura, esperada):
            return False
        emitido_texto, nonce = payload.decode("utf-8").split(".", 1)
        emitido_em = int(emitido_texto)
        idade = int(time.time()) - emitido_em
        return bool(nonce and -OAUTH_CLOCK_SKEW <= idade <= OAUTH_STATE_TTL)
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error):
        return False


def pagina_oauth(
    titulo: str,
    mensagem: str,
    *,
    sucesso: bool,
    mostrar_botao: bool = False,
) -> str:
    cor = "#16a34a" if sucesso else "#dc2626"
    icone = "✓" if sucesso else "!"
    botao = ""
    if mostrar_botao:
        botao = (
            f'<a class="button" href="{html.escape(APP_BASE_URL)}/oauth/login">'
            "Conectar novamente</a>"
        )
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{html.escape(titulo)}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
      padding: 24px; background: #f6f7f9; color: #111827;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    .card {{ width: min(100%, 430px); padding: 34px 28px; border-radius: 24px;
      background: white; box-shadow: 0 18px 55px rgba(17,24,39,.10); text-align: center; }}
    .icon {{ width: 58px; height: 58px; margin: 0 auto 18px; display: grid;
      place-items: center; border-radius: 50%; color: white; background: {cor};
      font-size: 32px; font-weight: 800; }}
    h1 {{ margin: 0 0 12px; font-size: 24px; }}
    p {{ margin: 0; color: #4b5563; font-size: 16px; line-height: 1.55; }}
    .button {{ display: block; margin-top: 24px; padding: 15px 18px;
      border-radius: 14px; background: #111827; color: white;
      font-weight: 700; text-decoration: none; }}
  </style>
</head>
<body><main class="card"><div class="icon">{icone}</div>
<h1>{html.escape(titulo)}</h1><p>{html.escape(mensagem)}</p>{botao}</main></body>
</html>"""


@app.get("/")
def home():
    status = "conectado" if TOKENS.conectado() else "aguardando OAuth"
    prontas = [
        SOURCE_LABELS.get(fonte, fonte.title())
        for fonte, pronta in fontes_configuradas().items()
        if pronta and fonte != "universal"
    ]
    return (
        "Garimpeiro Pessoal online! 🤖"
        f"<br>Versão: {APP_VERSION}"
        f"<br>Mercado Livre: {html.escape(status)}"
        f"<br>Fontes prontas: {html.escape(', '.join(prontas) or 'nenhuma')}"
        "<br><a href='/health'>Health check</a>",
        200,
    )


@app.get("/health")
def health():
    fontes = fontes_configuradas()
    configurado = bool(TELEGRAM_TOKEN and any(fontes.values()))
    backend = (
        "postgresql"
        if STORE.is_postgres and STORE.available
        else "sqlite"
        if STORE.available
        else "unavailable"
    )
    return {
        "ok": True,
        "version": APP_VERSION,
        "configured": configurado,
        "mercado_livre_connected": TOKENS.conectado(),
        "strict_configuration": STRICT_CONFIGURATION,
        "active_searches": len(ACTIVE_CHATS),
        "monitor_storage": backend,
        "monitor_storage_durable": STORE.durable,
        "active_monitors": len(STORE.list_monitors(active_only=True)),
        "sources": fontes,
        "coupon_alerts": COUPON_ALERTS_ENABLED,
        "coupon_min_percent": COUPON_MIN_PERCENT,
        "coupon_min_reais": COUPON_MIN_REAIS,
    }, 200


@app.get("/integrations/schema")
def integrations_schema():
    """Contrato estável para Buscapé, comparadores e novas lojas."""
    return {
        "request": {
            "method": "GET",
            "query": {"q": "produto", "sources": "buscape,loja", "limit": 20},
            "authorization": "Bearer UNIVERSAL_SEARCH_API_KEY (opcional)",
        },
        "response": {
            "results": [
                {
                    "source": "buscape",
                    "id": "oferta-123",
                    "product_id": "produto-123",
                    "title": "Produto e configuração completos",
                    "price": 5999.90,
                    "original_price": 6999.90,
                    "url": "https://loja.example/produto",
                    "condition": "new",
                    "seller": {
                        "id": "loja-1",
                        "name": "Loja",
                        "rating": 4.9,
                        "rating_count": 500,
                        "sold_count": 2000,
                    },
                    "shipping": {"free": True, "cost": 0},
                    "trusted": True,
                    "trust_reason": "lojista verificado pelo conector",
                    "coupon": {
                        "id": "cupom-1",
                        "code": "PROMO30",
                        "discount_type": "percent",
                        "value": 30,
                        "min_purchase": 100,
                        "max_discount": 500,
                        "expires_at": "2026-12-31T23:59:59-03:00",
                        "terms_verified": True,
                    },
                }
            ]
        },
        "important": (
            "trusted=true autoriza alertas automáticos; cupons continuam sujeitos "
            "à validação no carrinho"
        ),
    }, 200


@app.route("/cron/check", methods=["GET", "POST"])
def cron_check():
    if not CRON_SECRET:
        return {"ok": False, "error": "cron_disabled"}, 503
    recebido = request.headers.get("X-Cron-Secret", "")
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        recebido = authorization[7:].strip()
    # Compatibilidade com cron-job.org já configurado como GET. Cabeçalho é
    # preferível, mas o parâmetro mantém os jobs existentes funcionando.
    if not recebido:
        recebido = primeiro_texto(
            request.args.get("secret"),
            request.args.get("token"),
            request.args.get("cron_secret"),
        )
    if not hmac.compare_digest(recebido, CRON_SECRET):
        return {"ok": False, "error": "forbidden"}, 403
    resumo = executar_monitores()
    return {"ok": resumo["errors"] == 0, **resumo}, 200


@app.get("/oauth/login")
def oauth_login():
    if not MELI_CLIENT_ID or not MELI_CLIENT_SECRET:
        return pagina_oauth(
            "Configuração incompleta",
            "MELI_CLIENT_ID ou MELI_CLIENT_SECRET não está configurado no servidor.",
            sucesso=False,
        ), 500
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
        erro = primeiro_texto(
            request.args.get("error_description"), request.args.get("error")
        )
        return pagina_oauth(
            "Autorização não concluída",
            f"O Mercado Livre cancelou ou recusou a autorização: {erro}",
            sucesso=False,
            mostrar_botao=True,
        ), 400

    code = request.args.get("code", "").strip()
    state = request.args.get("state", "").strip()
    if not code:
        return pagina_oauth(
            "Código não recebido",
            "Inicie uma nova conexão para autorizar o Mercado Livre.",
            sucesso=False,
            mostrar_botao=True,
        ), 400
    if not consumir_oauth_state(state):
        return pagina_oauth(
            "Conexão expirada",
            "Este link de autorização não é mais válido. Inicie uma nova conexão.",
            sucesso=False,
            mostrar_botao=True,
        ), 400

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
        return pagina_oauth(
            "Falha de conexão",
            "Não foi possível concluir a comunicação com o Mercado Livre. Tente novamente.",
            sucesso=False,
            mostrar_botao=True,
        ), 502

    if response.status_code != 200:
        mensagem = primeiro_texto(data.get("message"), data.get("error"), "OAuth recusado")
        return pagina_oauth(
            "Autorização recusada",
            f"O Mercado Livre retornou HTTP {response.status_code}: {mensagem}",
            sucesso=False,
            mostrar_botao=True,
        ), 400
    if not TOKENS.salvar_resposta_oauth(data):
        return pagina_oauth(
            "Token não recebido",
            "A resposta não trouxe o token necessário. Tente autorizar novamente.",
            sucesso=False,
            mostrar_botao=True,
        ), 400
    return pagina_oauth(
        "Mercado Livre conectado",
        "A autorização foi concluída. Volte ao Telegram e envie sua busca.",
        sucesso=True,
    ), 200


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
        "Busque em todas as lojas configuradas e receba as cinco ofertas compatíveis, com preço confirmado primeiro.\n\n"
        "Exemplos:\n"
        "<code>/buscar Mac Mini M4 16 GB RAM 512 GB SSD</code>\n"
        "<code>/busca Teclado Logitech Pebble Keys 2 K380s</code>\n"
        "<code>/buscar amazon: Kindle Paperwhite</code>\n\n"
        "Monitor automático:\n"
        "<code>/monitorar Mac Mini M4 16 512 | 6300</code>\n\n"
        "Cupons fortes são avisados separadamente e sempre exigem validação no carrinho.\n\n"
        "Comandos: /buscar, /monitorar, /monitores, /verificaragora, /parar, /historico, /status e /teste"
    )


def comando_status(chat_id: int, thread_id: Optional[int]) -> None:
    mercado = "✅ conectado" if TOKENS.conectado() else "⚠️ não autorizado"
    modo = "rígido" if STRICT_CONFIGURATION else "flexível"
    backend = "PostgreSQL" if STORE.is_postgres else "SQLite"
    persistencia = (
        f"✅ {backend} persistente"
        if STORE.available and STORE.durable
        else f"⚠️ {backend} local"
        if STORE.available
        else "❌ indisponível"
    )
    monitores = len(STORE.list_monitors(chat_id=chat_id, active_only=True))
    fontes = fontes_configuradas()
    linhas_fontes = [
        f"{'✅' if pronta else '⚪'} {SOURCE_LABELS.get(fonte, fonte.title())}"
        for fonte, pronta in fontes.items()
        if fonte != "universal"
    ]
    send_message(
        chat_id,
        "🤖 <b>STATUS</b>\n\n"
        f"Versão: {APP_VERSION}\n"
        "✅ Telegram\n"
        "✅ Aplicação online\n"
        f"Mercado Livre: {mercado}\n"
        f"Filtro de configuração: {modo}\n"
        f"Resultados por busca: {MAX_RESULTS}\n"
        f"Persistência: {persistencia}\n"
        f"Monitores ativos: {monitores}\n"
        f"Cupons fortes: ≥ {COUPON_MIN_PERCENT:g}% ou ≥ {brl(COUPON_MIN_REAIS)}\n\n"
        "<b>Fontes</b>\n"
        + "\n".join(linhas_fontes),
        message_thread_id=thread_id,
        reply_markup=(
            None
            if TOKENS.conectado() or any(v for k, v in fontes.items() if k != "mercadolivre")
            else link_oauth_keyboard()
        ),
    )


def solicitar_busca(
    chat_id: int, thread_id: Optional[int], argumento: str
) -> None:
    if not any(fontes_configuradas().values()):
        send_message(
            chat_id,
            "⚠️ <b>Nenhuma fonte de busca está pronta.</b>\n\n"
            "Conecte o Mercado Livre ou configure uma das integrações multiloja.",
            message_thread_id=thread_id,
            reply_markup=link_oauth_keyboard(),
        )
    elif not argumento:
        send_message(
            chat_id,
            "Use, por exemplo:\n"
            "<code>/buscar Teclado Logitech Pebble Keys 2 K380s</code>",
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


def comando_teste(chat_id: int, thread_id: Optional[int]) -> None:
    if not TOKENS.conectado():
        fontes = fontes_configuradas()
        prontas = [
            SOURCE_LABELS.get(fonte, fonte.title())
            for fonte, pronta in fontes.items()
            if pronta and fonte != "universal"
        ]
        if prontas:
            send_message(
                chat_id,
                "✅ <b>INTEGRAÇÕES DISPONÍVEIS</b>\n\n"
                + "\n".join(f"✅ {html.escape(fonte)}" for fonte in prontas)
                + "\n⚠️ Mercado Livre ainda não autorizado.",
                message_thread_id=thread_id,
                reply_markup=link_oauth_keyboard(),
            )
            return
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
    chat_type = primeiro_texto(chat.get("type")).lower()
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
    elif comando in {"/buscar", "/busca", "/pesquisar", "/pesquisa", "/search"}:
        solicitar_busca(chat_id, thread_id, argumento)
    elif comando == "/monitorar":
        if not any(fontes_configuradas().values()):
            send_message(
                chat_id,
                "⚠️ Configure ao menos uma fonte antes de criar um monitor.",
                message_thread_id=thread_id,
                reply_markup=link_oauth_keyboard(),
            )
        else:
            comando_monitorar(chat_id, thread_id, argumento)
    elif comando in {"/monitores", "/alertas"}:
        comando_monitores(chat_id, thread_id)
    elif comando in {"/verificaragora", "/verificar"}:
        EXECUTOR.submit(executar_verificacao_manual, chat_id, thread_id)
    elif comando in {"/parar", "/desativar"}:
        comando_parar(chat_id, thread_id, argumento)
    elif comando == "/historico":
        comando_historico(chat_id, thread_id, argumento)
    elif not comando and argumento and chat_type == "private":
        solicitar_busca(chat_id, thread_id, argumento)
    else:
        send_message(
            chat_id,
            "Comando não reconhecido. Use:\n"
            "<code>/buscar nome do produto</code>\n"
            "ou envie apenas o nome do produto em conversa privada.",
            message_thread_id=thread_id,
        )
    return "OK", 200


# =============================================================================
# START LOCAL
# =============================================================================


if __name__ == "__main__":
    if "--monitor-once" in sys.argv:
        resultado_monitor = executar_monitores()
        print(json.dumps(resultado_monitor, ensure_ascii=False))
        raise SystemExit(0 if resultado_monitor["errors"] == 0 else 1)
    port = env_int("PORT", 10000, 1, 65535)
    app.run(host="0.0.0.0", port=port, threaded=True)
