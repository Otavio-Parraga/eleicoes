"""Acesso HTTP aos arquivos de resultado do TSE: URLs, alvos (tiers) e um fetcher com GET condicional.

Regras de educação com o TSE:
- GET condicional (If-None-Match / If-Modified-Since) com cache em memória;
- no máximo `config.HTTP_CONCURRENCY` requisições simultâneas;
- backoff em 429/5xx (com pausa global compartilhada entre as threads).

O fetcher NÃO grava no SQLite: as threads só fazem rede. Quem persiste o cache HTTP
(`store.set_http_cache`) é o thread principal, via `Fetcher.persist_cache(conn)`.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Iterable, Iterator

import httpx

from . import config

log = logging.getLogger("eleicoes.tse")

USER_AGENT = "eleicoes-local/0.1 (acompanhamento pessoal da apuracao; GET condicional)"


# ----------------------------------------------------------------------------- URLs e alvos

def url_for(eleicao: str | int, cargo: int, uf: str, mun: str = "") -> str:
    uf = uf.lower()
    return (f"{config.TSE_BASE}/{config.CICLO}/{int(eleicao)}/dados/{uf}/"
            f"{uf}{mun}-c{int(cargo):04d}-e{int(eleicao):06d}-u.json")


@dataclass(frozen=True)
class Target:
    eleicao: str
    cargo: int
    uf: str            # 'br', sigla minúscula ou 'zz'
    mun: str = ""      # código TSE de 5 dígitos ('' se não for município)
    url: str = ""

    def __post_init__(self):
        object.__setattr__(self, "uf", self.uf.lower())
        object.__setattr__(self, "eleicao", str(int(self.eleicao)))
        if not self.url:
            object.__setattr__(self, "url", url_for(self.eleicao, self.cargo, self.uf, self.mun))

    @property
    def nivel(self) -> str:
        if self.uf == config.BRASIL:
            return "br"
        return "mu" if self.mun else "uf"

    @property
    def basename(self) -> str:
        """Nome do arquivo sem '.json', ex. 'rs-c0003-e006259-u'."""
        name = self.url.rsplit("/", 1)[-1]
        return name[:-5] if name.endswith(".json") else name


def cargos_uf(turno: int, uf: str) -> list[int]:
    """Cargos com arquivo próprio da UF (exceto Presidente)."""
    if turno == 1:
        return [3, 5, 6, 8 if uf == "df" else 7]
    return [3]


def tier1_targets(turno: int = 1) -> list[Target]:
    """Arquivos br/uf: Presidente (br + 27 UFs + zz) e cargos estaduais das 27 UFs."""
    pres = config.ELECTIONS[(turno, 1)]
    out = [Target(pres, 1, uf) for uf in [config.BRASIL, *config.UF_LIST, config.EXTERIOR]]
    for uf in config.UF_LIST:
        for cargo in cargos_uf(turno, uf):
            out.append(Target(config.ELECTIONS[(turno, cargo)], cargo, uf))
    return out


def tier2_targets(turno: int, uf: str) -> list[Target]:
    """Um alvo por município da UF × cargo do turno. Ignora 'zz' e 'br'."""
    from . import geo  # import tardio: geo puxa pandas e o cache de municípios

    uf = uf.lower()
    if uf in (config.EXTERIOR, config.BRASIL) or uf not in config.UFS:
        return []
    cargos = [1, *cargos_uf(turno, uf)]
    muns = geo.municipios_uf(uf)["mun"].tolist()
    return [Target(config.ELECTIONS[(turno, c)], c, uf, str(m)) for m in muns for c in cargos]


# ----------------------------------------------------------------------------- fetcher

@dataclass
class FetchResult:
    target: Target
    status: int | str                 # 200, 304, 404 ou 'err'
    doc: dict | None = None           # JSON decodificado (só em 200)
    content: bytes | None = field(default=None, repr=False)  # corpo bruto (só em 200)
    etag: str | None = None
    last_modified: str | None = None
    http_status: int | None = None    # código HTTP real (ex. 403 ou 500 quando status == 'err')
    error: str | None = None
    elapsed: float = 0.0

    @property
    def key(self) -> str:
        return str(self.status)


class Fetcher:
    """GET condicional concorrente. `fetch_iter` entrega resultados conforme ficam prontos."""

    def __init__(self, client: httpx.Client | None = None, concurrency: int = config.HTTP_CONCURRENCY,
                 timeout: float = config.HTTP_TIMEOUT_S, retries: int = 2, backoff: float = 2.0):
        self.concurrency = max(1, int(concurrency))
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            limits=httpx.Limits(max_connections=self.concurrency, max_keepalive_connections=self.concurrency),
        )
        self.retries = retries
        self.backoff = backoff
        self.pool = ThreadPoolExecutor(self.concurrency, thread_name_prefix="tse")
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[str | None, str | None]] = {}
        self._dirty: dict[str, tuple[str | None, str | None, int]] = {}
        self._seeded: set[str] = set()
        self._cooldown_until = 0.0

    # --- cache -------------------------------------------------------------------------
    def cache_get(self, url: str) -> tuple[str | None, str | None] | None:
        with self._lock:
            return self._cache.get(url)

    def cache_set(self, url: str, etag: str | None, last_modified: str | None) -> None:
        with self._lock:
            self._cache[url] = (etag, last_modified)

    def forget(self, url: str) -> None:
        """Esquece o validador (ex.: o 200 não pôde ser processado) para baixar de novo no próximo ciclo."""
        with self._lock:
            self._cache.pop(url, None)
            self._dirty.pop(url, None)

    def seed_from_store(self, conn, urls: Iterable[str]) -> int:
        """Carrega validadores persistidos (chamar no thread principal). Retorna quantos foram carregados."""
        from . import store

        n = 0
        for url in urls:
            if url in self._seeded:
                continue
            self._seeded.add(url)
            if self.cache_get(url) is not None:
                continue
            got = store.get_http_cache(conn, url)
            if got and (got[0] or got[1]):
                self.cache_set(url, got[0], got[1])
                n += 1
        return n

    def persist_cache(self, conn) -> int:
        """Grava no SQLite os validadores novos. Só no thread principal."""
        from . import store

        with self._lock:
            dirty, self._dirty = self._dirty, {}
        for url, (etag, lm, status) in dirty.items():
            store.set_http_cache(conn, url, etag, lm, status)
        return len(dirty)

    # --- rede --------------------------------------------------------------------------
    def _wait_cooldown(self) -> None:
        while True:
            with self._lock:
                delay = self._cooldown_until - time.monotonic()
            if delay <= 0:
                return
            time.sleep(min(delay, 5.0))

    def _set_cooldown(self, seconds: float) -> None:
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + seconds)

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        ra = resp.headers.get("Retry-After")
        if not ra:
            return None
        try:
            return float(ra)
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(ra).timestamp() - time.time())
            except Exception:  # noqa: BLE001
                return None

    def fetch_one(self, target: Target) -> FetchResult:
        """Nunca levanta exceção: erros viram status 'err'."""
        t0 = time.monotonic()
        url = target.url
        last_err = "?"
        http_status = None
        for attempt in range(self.retries + 1):
            self._wait_cooldown()
            headers = {}
            cached = self.cache_get(url)
            if cached:
                if cached[0]:
                    headers["If-None-Match"] = cached[0]
                if cached[1]:
                    headers["If-Modified-Since"] = cached[1]
            try:
                resp = self.client.get(url, headers=headers)
            except httpx.TimeoutException as e:
                last_err, http_status = f"timeout: {type(e).__name__}", None
                if attempt < self.retries:
                    time.sleep(self.backoff * (2 ** attempt))
                continue
            except httpx.HTTPError as e:
                last_err, http_status = f"{type(e).__name__}: {e}", None
                if attempt < self.retries:
                    time.sleep(self.backoff * (2 ** attempt))
                continue
            except Exception as e:  # noqa: BLE001 — nunca derrubar o thread
                last_err, http_status = f"{type(e).__name__}: {e}", None
                break

            code = resp.status_code
            http_status = code
            if code == 304:
                return FetchResult(target, 304, etag=cached[0] if cached else None,
                                   last_modified=cached[1] if cached else None, http_status=304,
                                   elapsed=time.monotonic() - t0)
            if code == 404:
                return FetchResult(target, 404, http_status=404, elapsed=time.monotonic() - t0)
            if code == 200:
                content = resp.content
                try:
                    doc = json.loads(content)
                except ValueError as e:
                    # arquivo truncado/em atualização: não guarda validador, tenta de novo no próximo ciclo
                    return FetchResult(target, "err", http_status=200, error=f"JSON inválido: {e}",
                                       elapsed=time.monotonic() - t0)
                etag = resp.headers.get("ETag")
                lm = resp.headers.get("Last-Modified")
                with self._lock:
                    self._cache[url] = (etag, lm)
                    self._dirty[url] = (etag, lm, 200)
                return FetchResult(target, 200, doc=doc, content=content, etag=etag, last_modified=lm,
                                   http_status=200, elapsed=time.monotonic() - t0)
            if code == 429 or code >= 500:
                ra = self._retry_after(resp)
                delay = min(60.0, ra if ra is not None else self.backoff * (2 ** attempt))
                last_err = f"HTTP {code}"
                if delay > 0:
                    self._set_cooldown(delay)
                if attempt < self.retries:
                    continue
                break
            last_err = f"HTTP {code}"  # 403 etc.: sem retry
            break
        return FetchResult(target, "err", http_status=http_status, error=last_err, elapsed=time.monotonic() - t0)

    def fetch_iter(self, targets: Iterable[Target], window: int | None = None) -> Iterator[FetchResult]:
        """Busca os alvos com no máx. `concurrency` requisições simultâneas; entrega na ordem de término.

        Mantém só `window` requisições enfileiradas por vez para não acumular corpos grandes na memória.
        """
        window = window or self.concurrency * 3
        queue = deque(targets)
        inflight = set()
        try:
            while queue or inflight:
                while queue and len(inflight) < window:
                    inflight.add(self.pool.submit(self.fetch_one, queue.popleft()))
                done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in done:
                    yield fut.result()
        finally:
            for fut in inflight:
                fut.cancel()

    def fetch_all(self, targets: Iterable[Target]) -> list[FetchResult]:
        return list(self.fetch_iter(targets))

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass


# ----------------------------------------------------------------------------- ele-c.json

def compare_elections(doc: dict) -> list[str]:
    """Compara os códigos de 2026 em ele-c.json com config.ELECTIONS. Lista vazia = tudo certo."""
    warnings: list[str] = []
    entries = [e for p in doc.get("pl", []) if p.get("c") == config.CICLO for e in p.get("e", [])]
    if not entries:
        return [f"ele-c.json não lista eleições do ciclo {config.CICLO}"]
    codes = set()
    for e in entries:
        codes.add(str(e.get("cd", "")))
        if e.get("cdt2"):
            codes.add(str(e["cdt2"]))
    for (turno, cargo), code in sorted(config.ELECTIONS.items()):
        if code not in codes:
            warnings.append(f"eleição {code} (turno {turno}, cargo {cargo}) não aparece em ele-c.json")
    # Federal (tp 8) e Estadual (tp 1): confere 1º turno e o código do 2º turno
    expect = {"8": ((1, 1), (2, 1), "Federal"), "1": ((1, 3), (2, 3), "Estadual")}
    for e in entries:
        tp = str(e.get("tp", ""))
        if tp in expect and str(e.get("t", "")) == "1":
            k1, k2, nome = expect[tp]
            if str(e.get("cd")) != config.ELECTIONS[k1]:
                warnings.append(f"eleição {nome} 1º turno é {e.get('cd')} em ele-c.json, config usa "
                                f"{config.ELECTIONS[k1]}")
            if e.get("cdt2") and str(e["cdt2"]) != config.ELECTIONS[k2]:
                warnings.append(f"eleição {nome} 2º turno é {e['cdt2']} em ele-c.json, config usa "
                                f"{config.ELECTIONS[k2]}")
    return warnings


def check_elections(client: httpx.Client | None = None) -> list[str]:
    """Baixa ele-c.json e devolve avisos (nunca levanta exceção)."""
    try:
        if client is not None:
            r = client.get(config.ELE_CONFIG_URL)
        else:
            r = httpx.get(config.ELE_CONFIG_URL, timeout=config.HTTP_TIMEOUT_S, follow_redirects=True,
                          headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        return compare_elections(r.json())
    except Exception as e:  # noqa: BLE001
        return [f"não foi possível verificar ele-c.json: {type(e).__name__}: {e}"]
