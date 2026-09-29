"""Cliente HTTP resiliente — apenas stdlib (urllib), sem dependências externas.

Cuida do que mais quebra scrapers de licitação na prática:
  * rate limiting educado (não derrubar portais públicos)
  * retry com backoff exponencial + jitter
  * headers de navegador (o /api/search do PNCP devolve corpo vazio sem eles)
  * cache em disco (evita re-baixar a mesma página em re-execuções)
"""
from __future__ import annotations

import gzip
import hashlib
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

CACHE_DIR = Path(__file__).resolve().parent / "data" / ".cache"


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} em {url}: {body[:200]}")
        self.status = status
        self.url = url
        self.body = body


class RateLimited(RuntimeError):
    """O portal respondeu 429 — limite de requisições excedido."""

    def __init__(self, url: str, cooldown: float):
        super().__init__(
            f"limite de requisições excedido (HTTP 429) em {url}; "
            f"aguardando {cooldown:.0f}s"
        )
        self.url = url
        self.cooldown = cooldown


class Client:
    """Cliente HTTP síncrono, com throttle, retry e cache opcional.

    Trata explicitamente HTTP 429. O PNCP aplica limite de requisições e
    responde 429 com uma página HTML ("Limite de Requisições Excedido") — não
    com JSON. Insistir durante a janela de bloqueio só prolonga o bloqueio,
    então ao primeiro 429 o cliente entra em espera longa antes de tentar de novo.
    """

    # espera base ao receber 429 (cresce a cada ocorrência consecutiva)
    COOLDOWN_429 = 60.0
    COOLDOWN_429_MAX = 900.0

    def __init__(
        self,
        *,
        delay: float = 0.7,
        timeout: int = 30,
        retries: int = 4,
        use_cache: bool = True,
        cache_ttl: int = 3600,
        extra_headers: dict[str, str] | None = None,
        on_rate_limit=None,
    ):
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.use_cache = use_cache
        self.cache_ttl = cache_ttl
        self._last_call = 0.0
        self._n_429 = 0
        self.on_rate_limit = on_rate_limit
        self.headers = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        }
        if extra_headers:
            self.headers.update(extra_headers)
        self.stats = {"requests": 0, "cache_hits": 0, "errors": 0, "retries": 0, "rate_limits": 0}

    # ------------------------------------------------------------------ cache
    def _cache_path(self, url: str) -> Path:
        h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        return CACHE_DIR / f"{h}.json"

    def _read_cache(self, url: str):
        if not self.use_cache:
            return None
        p = self._cache_path(url)
        if not p.exists():
            return None
        if time.time() - p.stat().st_mtime > self.cache_ttl:
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write_cache(self, url: str, payload) -> None:
        if not self.use_cache:
            return
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            self._cache_path(url).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    # ------------------------------------------------------------------- core
    def _throttle(self) -> None:
        """Espera o intervalo mínimo entre requisições.

        Se `_gate` estiver definido, o relógio é COMPARTILHADO com outro
        cliente. Sem isso, dois clientes (ex.: consulta + busca) com delay de
        0.6s geram taxa efetiva de 2 requisições por 0.6s — e estouram o
        limite do portal.
        """
        gate = getattr(self, "_gate", None) or self
        elapsed = time.time() - gate._last_call
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        gate._last_call = time.time()

    def get_json(self, url: str, params: dict | None = None, *, use_cache: bool | None = None):
        """GET que devolve JSON decodificado, ou None se 404."""
        if params:
            clean = {k: v for k, v in params.items() if v is not None and v != ""}
            url = f"{url}?{urllib.parse.urlencode(clean)}"

        cache_on = self.use_cache if use_cache is None else use_cache
        if cache_on:
            hit = self._read_cache(url)
            if hit is not None:
                self.stats["cache_hits"] += 1
                return hit

        last_exc: Exception | None = None
        for attempt in range(self.retries):
            self._throttle()
            try:
                req = urllib.request.Request(url, headers=self.headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    text = raw.decode("utf-8", errors="replace")
                    self.stats["requests"] += 1
                    if not text.strip():
                        # PNCP devolve 200 com corpo vazio quando faltam headers
                        raise HttpError(0, url, "corpo vazio")
                    data = json.loads(text)
                    self._n_429 = 0  # sucesso: zera o backoff de 429
                    if cache_on:
                        self._write_cache(url, data)
                    return data
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                if e.code == 404:
                    self.stats["errors"] += 1
                    return None
                if e.code in (400, 422):
                    # erro de parâmetro: retry não resolve
                    self.stats["errors"] += 1
                    raise HttpError(e.code, url, body) from e
                if e.code == 429:
                    # Limite do portal. Esperar é a única saída correta —
                    # insistir durante o bloqueio só o prolonga.
                    self.stats["rate_limits"] += 1
                    self._n_429 += 1
                    espera = min(
                        self.COOLDOWN_429 * (2 ** (self._n_429 - 1)),
                        self.COOLDOWN_429_MAX,
                    )
                    # respeita Retry-After se o servidor informar
                    ra = e.headers.get("Retry-After") if e.headers else None
                    if ra:
                        try:
                            espera = max(espera, float(ra))
                        except ValueError:
                            pass
                    if self.on_rate_limit:
                        self.on_rate_limit(espera)
                    time.sleep(espera)
                    last_exc = RateLimited(url, espera)
                    continue  # não consome o contador de retry normal
                last_exc = HttpError(e.code, url, body)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, HttpError) as e:
                last_exc = e

            self.stats["retries"] += 1
            backoff = min(2 ** attempt, 16) + random.uniform(0, 0.8)
            time.sleep(backoff)

        self.stats["errors"] += 1
        raise RuntimeError(f"falhou após {self.retries} tentativas: {url}") from last_exc
