"""Coletor ao vivo: baixa os arquivos '-u.json' do TSE e grava no SQLite.

    python -m eleicoes.collector [--turno 1] [--db PATH] [--focus rs,sp] [--once]
                                 [--tier1 60] [--tier2 180] [--no-notify] [--no-raw]

- Tier 1 (a cada --tier1 s): arquivos br/uf (137 no 1º turno).
- Tier 2 (a cada --tier2 s): municípios das UFs em foco = --focus ∪ store.get_focus() (relido a cada ciclo).
  UF recém-adicionada ao foco (a interface chama store.add_focus) entra na fila já no próximo ciclo do tier 1.
- Só o thread principal escreve no SQLite; as threads do fetcher só fazem rede.
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import signal
import sys
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path

from . import config, notify, parse, store, tse
from .parse import Snapshot

log = logging.getLogger("eleicoes.collector")

MILESTONES = (10, 25, 50, 75, 90, 100)
NOTIFY_TITLE = "Eleições 2026"
TIER2_CHUNK = 400  # alvos de município por lote (entre lotes o tier 1 tem prioridade)


# ----------------------------------------------------------------------------- eventos (função pura)

def _pct(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def area_label(s: Snapshot) -> str:
    nome = config.CARGOS.get(s.cargo, s.cargo_nome or f"Cargo {s.cargo}")
    if s.nivel == "br":
        return nome
    if s.uf == config.EXTERIOR:
        return f"{nome} Exterior"
    return f"{nome} {s.uf.upper()}"


def _leader(s: Snapshot):
    cands = [c for c in s.candidates if c.votos > 0]
    if not cands:
        return None
    return max(cands, key=lambda c: (c.votos, -int(c.numero) if c.numero.isdigit() else 0))


def _is_2t(situacao: str) -> bool:
    s = (situacao or "").lower().replace("°", "º").replace("o turno", "º turno")
    return s.startswith("2º turno") or s == "2º turno"


def detect_events(prev: Snapshot | None, new: Snapshot) -> list[tuple[str, str]]:
    """Compara dois snapshots da MESMA área (br/uf) e devolve [(kind, mensagem)].

    kinds: 'finalizada', 'eleito', 'segundo_turno', 'virada', 'marco'.
    Primeira vez que a área aparece (prev None) → nenhum evento.
    Presidente só gera eventos no nível br (os arquivos por UF repetiriam a mesma notícia 28 vezes).
    """
    if prev is None or new.nivel not in ("br", "uf"):
        return []
    if (prev.eleicao, prev.cargo, prev.nivel, prev.uf) != (new.eleicao, new.cargo, new.nivel, new.uf):
        return []
    if new.cargo == 1 and new.nivel != "br":
        return []
    label = area_label(new)
    out: list[tuple[str, str]] = []

    # marcos de apuração (Presidente, nível br)
    if new.cargo == 1 and new.nivel == "br":
        crossed = [m for m in MILESTONES if prev.totals.pct_secoes < m <= new.totals.pct_secoes]
        if crossed:
            m = crossed[-1]
            top = [c for c in new.candidates if c.votos > 0][:2]
            extra = ", ".join(f"{c.nome_urna} ({c.partido}) {_pct(c.pct_validos)}%" for c in top)
            msg = f"{label}: {m}% das seções apuradas" + (f" — {extra}" if extra else "")
            out.append(("marco", msg))

    # virada: Presidente (br) e Governador (uf)
    if (new.cargo == 1 and new.nivel == "br") or (new.cargo == 3 and new.nivel == "uf"):
        if new.totals.pct_secoes >= 1:
            a, b = _leader(prev), _leader(new)
            if a is not None and b is not None and a.sqcand != b.sqcand:
                out.append(("virada", f"{label}: virada — {b.nome_urna} ({b.partido}) passa "
                                      f"{a.nome_urna} ({a.partido}) com {_pct(new.totals.pct_secoes)}% apurado"))

    # eleito: Presidente, Governador, Senador
    if new.cargo in (1, 3, 5):
        antes = {c.sqcand for c in prev.candidates if c.eleito}
        for c in new.candidates:
            if c.eleito and c.sqcand not in antes:
                out.append(("eleito", f"{label}: {c.nome_urna} ({c.partido}) eleito"))

    # 2º turno: Presidente e Governador
    if new.cargo in (1, 3):
        antes = {c.sqcand for c in prev.candidates if _is_2t(c.situacao)}
        agora = [c for c in new.candidates if _is_2t(c.situacao)]
        if agora and any(c.sqcand not in antes for c in agora):
            nomes = " e ".join(f"{c.nome_urna} ({c.partido})" for c in agora)
            out.append(("segundo_turno", f"{label}: 2º turno entre {nomes}"))

    # apuração finalizada (todos os cargos)
    if new.finalizada and not prev.finalizada:
        out.append(("finalizada", f"{label}: apuração finalizada"))
    return out


# ----------------------------------------------------------------------------- coletor

class Collector:
    def __init__(self, conn, turno: int = 1, cli_focus: list[str] | None = None, *, fetcher: tse.Fetcher | None = None,
                 raw_dir: Path | None = None, notify_on: bool = True):
        self.conn = conn
        self.turno = turno
        self.cli_focus = [u.lower() for u in (cli_focus or []) if u]
        self.fetcher = fetcher or tse.Fetcher()
        self.raw_dir = raw_dir
        self.notify_on = notify_on
        self.prev: dict[tuple, Snapshot] = {}
        self.max_tse_ts: datetime | None = None
        self.total = Counter()
        self.last_tier1: dict = {}
        self.last_tier2: dict = {}
        self.tier1 = tse.tier1_targets(turno)
        self._t2_cache: dict[str, list[tse.Target]] = {}
        self.stop = False

    # --- foco ---------------------------------------------------------------------------
    def focus(self) -> list[str]:
        try:
            db = store.get_focus(self.conn)
        except Exception as e:  # noqa: BLE001
            log.warning("não consegui ler o foco do banco: %s", e)
            db = []
        out = []
        for u in [*self.cli_focus, *db]:
            u = u.lower()
            if u in config.UFS and u not in out:
                out.append(u)
        return out

    def tier2_for(self, uf: str) -> list[tse.Target]:
        if uf not in self._t2_cache:
            try:
                self._t2_cache[uf] = tse.tier2_targets(self.turno, uf)
            except Exception as e:  # noqa: BLE001
                log.warning("não consegui montar a lista de municípios de %s: %s", uf, e)
                return []
        return self._t2_cache[uf]

    # --- processamento ---------------------------------------------------------------------
    def run_batch(self, targets: list[tse.Target], seed: bool = False) -> dict:
        """Busca e processa um lote. Escritas no SQLite só aqui (thread principal)."""
        t0 = time.monotonic()
        counts = Counter()
        warned = 0
        if seed:
            try:
                self.fetcher.seed_from_store(self.conn, (t.url for t in targets))
            except Exception as e:  # noqa: BLE001
                log.warning("falha ao carregar cache HTTP: %s", e)
        for res in self.fetcher.fetch_iter(targets):
            if self.stop:
                break
            counts[res.key] += 1
            if res.status == 404:
                log.debug("404 %s", res.target.url)
            elif res.status == "err":
                if warned < 5:
                    log.warning("erro em %s: %s", res.target.url, res.error)
                    warned += 1
                else:
                    log.debug("erro em %s: %s", res.target.url, res.error)
            elif res.status == 200:
                try:
                    if self.handle(res):
                        counts["new"] += 1
                except Exception as e:  # noqa: BLE001
                    counts["parse_err"] += 1
                    self.fetcher.forget(res.target.url)
                    log.warning("falha ao processar %s: %s: %s", res.target.url, type(e).__name__, e)
            res.doc = res.content = None
        try:
            self.fetcher.persist_cache(self.conn)
        except Exception as e:  # noqa: BLE001
            log.warning("falha ao gravar cache HTTP: %s", e)
        counts["seconds"] = round(time.monotonic() - t0, 1)
        counts["requests"] = sum(counts[k] for k in ("200", "304", "404", "err"))
        for k in ("200", "304", "404", "err", "new", "parse_err"):
            self.total[k] += counts[k]
        return dict(counts)

    def handle(self, res: tse.FetchResult) -> bool:
        """Processa um 200. Retorna True se gravou snapshot novo."""
        t = res.target
        snap = parse.parse_u(res.doc, uf=t.uf)
        sid = store.write_snapshot(self.conn, snap)
        if snap.tse_ts and (self.max_tse_ts is None or snap.tse_ts > self.max_tse_ts):
            self.max_tse_ts = snap.tse_ts
        if snap.nivel not in ("br", "uf"):
            return sid is not None
        if sid is not None and self.raw_dir is not None and res.content:
            self.archive(t, snap, res.content)
        key = snap.area_key
        prev = self.prev.get(key)
        if prev is not None and prev.tse_ts and snap.tse_ts and snap.tse_ts < prev.tse_ts:
            return sid is not None  # resposta antiga de algum nó da CDN: ignora para eventos
        if prev is not None and (prev.tse_ts != snap.tse_ts or prev.tse_ts is None):
            for kind, msg in detect_events(prev, snap):
                self.emit(kind, msg, snap)
        self.prev[key] = snap
        return sid is not None

    def archive(self, t: tse.Target, snap: Snapshot, content: bytes) -> None:
        try:
            ts = (snap.tse_ts or datetime.now()).strftime("%Y%m%dT%H%M%S")
            d = self.raw_dir / t.eleicao
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{t.basename}.{ts}.json.gz"
            if not path.exists():
                tmp = path.with_suffix(".tmp")
                with gzip.open(tmp, "wb", compresslevel=6) as f:
                    f.write(content)
                os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001
            log.warning("falha ao arquivar %s: %s", t.url, e)

    def emit(self, kind: str, msg: str, snap: Snapshot) -> None:
        log.info("EVENTO [%s] %s", kind, msg)
        try:
            store.add_event(self.conn, kind, msg, snap.eleicao, snap.cargo, snap.uf)
        except Exception as e:  # noqa: BLE001
            log.warning("falha ao gravar evento: %s", e)
        if self.notify_on:
            notify.notify(NOTIFY_TITLE, msg)

    def write_meta(self) -> None:
        try:
            store.set_meta(self.conn, "collector_heartbeat", datetime.now().isoformat(timespec="seconds"))
            if self.max_tse_ts:
                store.set_meta(self.conn, "collector_last_tse_ts", self.max_tse_ts.isoformat(timespec="seconds"))
            stats = {"turno": self.turno, "pid": os.getpid(), "focus": self.focus(),
                     "tier1": self.last_tier1, "tier2": self.last_tier2,
                     "total": {k: self.total[k] for k in ("200", "304", "404", "err", "new", "parse_err")},
                     "updated": datetime.now().isoformat(timespec="seconds")}
            store.set_meta(self.conn, "collector_stats", json.dumps(stats, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            log.warning("falha ao gravar meta: %s", e)

    # --- ciclos -----------------------------------------------------------------------------
    def tier1_cycle(self) -> dict:
        c = self.run_batch(self.tier1)
        self.last_tier1 = {**c, "at": datetime.now().isoformat(timespec="seconds")}
        log.info("tier1: %s req em %.1fs — 200=%d (novos %d) 304=%d 404=%d err=%d",
                 c.get("requests", 0), c.get("seconds", 0), c.get("200", 0), c.get("new", 0), c.get("304", 0),
                 c.get("404", 0), c.get("err", 0))
        self.write_meta()
        return c

    def run(self, once: bool = False, tier1_s: float = config.TIER1_INTERVAL_S,
            tier2_s: float = config.TIER2_INTERVAL_S) -> None:
        for uf in self.cli_focus:
            try:
                store.add_focus(self.conn, uf)
            except Exception as e:  # noqa: BLE001
                log.warning("falha ao registrar foco %s: %s", uf, e)

        if once:
            self.tier1_cycle()
            for uf in self.focus():
                self._sweep_uf_all(uf)
            self.write_meta()
            return

        next_t1 = next_t2 = time.monotonic()
        pending: deque[tse.Target] = deque()
        swept: set[str] = set()       # UFs já enfileiradas na varredura atual
        sweep = Counter()
        sweep_t0 = None
        last_beat = 0.0
        while not self.stop:
            now = time.monotonic()
            if now >= next_t1:
                next_t1 = now + tier1_s
                self.tier1_cycle()
                last_beat = time.monotonic()
                focus = self.focus()
                new_ufs = [u for u in focus if u not in swept]
                # UF nova no foco (store.add_focus pela interface): varre já, sem esperar o timer do tier 2.
                # Antes da 1ª varredura (swept vazio) não precisa: ela começa logo em seguida.
                if new_ufs and (pending or swept):
                    if not pending:
                        sweep, sweep_t0 = Counter(), time.monotonic()
                    for uf in new_ufs:
                        log.info("tier2: UF %s entrou no foco; varrendo agora", uf)
                        pending.extend(self.tier2_for(uf))
                        swept.add(uf)
                continue
            if self.stop:
                break
            if not pending and now >= next_t2:
                next_t2 = now + tier2_s
                focus = self.focus()
                swept = set(focus)
                for uf in focus:
                    pending.extend(self.tier2_for(uf))
                sweep, sweep_t0 = Counter(), time.monotonic()
                log.info("tier2: varrendo %s (%d arquivos)", ",".join(focus) or "-", len(pending))
            if pending:
                chunk = [pending.popleft() for _ in range(min(TIER2_CHUNK, len(pending)))]
                c = self.run_batch(chunk, seed=True)
                for k, v in c.items():
                    if k != "seconds":
                        sweep[k] += v
                if not pending:
                    dur = round(time.monotonic() - (sweep_t0 or time.monotonic()), 1)
                    self.last_tier2 = {**{k: v for k, v in sweep.items()}, "seconds": dur,
                                       "ufs": sorted(swept), "at": datetime.now().isoformat(timespec="seconds")}
                    log.info("tier2: %s concluído em %.1fs — 200=%d (novos %d) 304=%d 404=%d err=%d",
                             ",".join(sorted(swept)), dur, sweep["200"], sweep["new"], sweep["304"], sweep["404"],
                             sweep["err"])
                self.write_meta()
                last_beat = time.monotonic()
                continue
            # ocioso: dorme até o próximo ciclo, mantendo o heartbeat
            if time.monotonic() - last_beat >= 15:
                self.write_meta()
                last_beat = time.monotonic()
            time.sleep(max(0.2, min(1.0, min(next_t1, next_t2) - time.monotonic())))

    def _sweep_uf_all(self, uf: str) -> dict:
        targets = self.tier2_for(uf)
        log.info("tier2: varrendo %s (%d arquivos)", uf, len(targets))
        total = Counter()
        t0 = time.monotonic()
        for i in range(0, len(targets), TIER2_CHUNK):
            if self.stop:
                break
            c = self.run_batch(targets[i:i + TIER2_CHUNK], seed=True)
            for k, v in c.items():
                if k != "seconds":
                    total[k] += v
            self.write_meta()
        dur = round(time.monotonic() - t0, 1)
        self.last_tier2 = {**dict(total), "seconds": dur, "ufs": [uf], "at": datetime.now().isoformat(timespec="seconds")}
        log.info("tier2: %s concluído em %.1fs — 200=%d (novos %d) 304=%d 404=%d err=%d", uf, dur, total["200"],
                 total["new"], total["304"], total["404"], total["err"])
        return dict(total)


# ----------------------------------------------------------------------------- CLI

def _same_file(stream, path: Path) -> bool:
    try:
        a = os.fstat(stream.fileno())
        b = path.stat()
        return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
    except Exception:  # noqa: BLE001
        return False


def setup_logging(log_path: Path, verbose: bool = False) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)
        if not _same_file(sys.stdout, log_path):  # run.sh já redireciona o stdout para o log
            fh = logging.FileHandler(log_path, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
    except Exception as e:  # noqa: BLE001
        root.warning("não consegui abrir %s: %s", log_path, e)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m eleicoes.collector", description="Coletor ao vivo do TSE")
    p.add_argument("--turno", type=int, choices=(1, 2), default=1)
    p.add_argument("--db", type=Path, default=Path(config.DB_PATH))
    p.add_argument("--focus", default=",".join(config.DEFAULT_FOCUS), help="UFs para varrer municípios, ex. rs,sp")
    p.add_argument("--once", action="store_true", help="um ciclo de tier 1 + uma varredura do foco, depois sai")
    p.add_argument("--tier1", type=float, default=config.TIER1_INTERVAL_S)
    p.add_argument("--tier2", type=float, default=config.TIER2_INTERVAL_S)
    p.add_argument("--no-notify", action="store_true")
    p.add_argument("--no-raw", action="store_true")
    p.add_argument("--raw-dir", type=Path, default=None,
                   help="padrão: data/raw para o banco padrão; <pasta do --db>/raw caso contrário")
    p.add_argument("--log", type=Path, default=None, help="padrão: <pasta do --db>/collector.log")
    p.add_argument("--concurrency", type=int, default=config.HTTP_CONCURRENCY)
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    db = args.db.expanduser().resolve()
    log_path = args.log or (db.parent / "collector.log")
    setup_logging(log_path, args.verbose)
    raw_dir = None
    if not args.no_raw:
        default_db = Path(config.DB_PATH).expanduser().resolve()
        raw_dir = args.raw_dir or (config.RAW_DIR if db == default_db else db.parent / "raw")
    focus = [u.strip().lower() for u in args.focus.split(",") if u.strip()]
    bad = [u for u in focus if u not in config.UFS]
    if bad:
        log.warning("UFs desconhecidas ignoradas no --focus: %s", ",".join(bad))
        focus = [u for u in focus if u in config.UFS]

    log.info("coletor iniciando: turno=%d db=%s foco=%s tier1=%ss tier2=%ss raw=%s notify=%s pid=%d",
             args.turno, db, ",".join(focus) or "-", args.tier1, args.tier2, raw_dir or "off",
             not args.no_notify, os.getpid())
    for w in tse.check_elections():
        log.warning("ele-c.json: %s", w)

    conn = store.connect(db)
    collector = Collector(conn, args.turno, focus, fetcher=tse.Fetcher(concurrency=min(args.concurrency, 8)),
                          raw_dir=raw_dir, notify_on=not args.no_notify)

    def _term(signum, frame):  # noqa: ARG001
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _term)
    except Exception:  # noqa: BLE001
        pass

    t0 = time.monotonic()
    try:
        collector.run(once=args.once, tier1_s=args.tier1, tier2_s=args.tier2)
    except KeyboardInterrupt:
        collector.stop = True
        log.info("interrompido; encerrando")
    finally:
        collector.stop = True
        try:
            collector.fetcher.persist_cache(conn)
        except Exception:  # noqa: BLE001
            pass
        collector.fetcher.close()
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    log.info("coletor encerrado após %.1fs; total %s", time.monotonic() - t0,
             {k: collector.total[k] for k in ("200", "304", "404", "err", "new", "parse_err")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
