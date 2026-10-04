"""Persistência em SQLite (WAL): o coletor escreve, a interface Streamlit só lê.

Regras:
- Um único thread escreve (o coletor). Leitores abrem uma conexão própria por execução.
- Níveis 'br' e 'uf' guardam histórico completo (um snapshot por mudança no TSE).
- Nível 'mu' guarda só o snapshot mais recente de cada município.
- Em todas as consultas, cargo=7 significa "Deputado Estadual ou Distrital" e casa com cargo IN (7, 8).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

from . import config
from .parse import Snapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
    id INTEGER PRIMARY KEY,
    eleicao TEXT NOT NULL, turno INTEGER NOT NULL, cargo INTEGER NOT NULL,
    nivel TEXT NOT NULL, uf TEXT NOT NULL, mun TEXT NOT NULL DEFAULT '',
    tse_ts TEXT NOT NULL, fetched_at TEXT NOT NULL, finalizada INTEGER NOT NULL,
    vagas INTEGER, secoes_total INTEGER, secoes_totalizadas INTEGER, pct_secoes REAL,
    eleitorado INTEGER, eleitorado_apurado INTEGER, comparecimento INTEGER, abstencao INTEGER,
    votos_total INTEGER, validos INTEGER, nominais INTEGER, legenda INTEGER,
    brancos INTEGER, nulos INTEGER, anulados INTEGER,
    UNIQUE (eleicao, cargo, nivel, uf, mun, tse_ts)
);
CREATE INDEX IF NOT EXISTS ix_snapshot_area ON snapshot (eleicao, cargo, nivel, uf, mun, tse_ts);

CREATE TABLE IF NOT EXISTS cand_result (
    snapshot_id INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
    sqcand TEXT NOT NULL, numero TEXT, nome TEXT, nome_urna TEXT,
    partido TEXT, partido_numero TEXT, federacao TEXT, agremiacao TEXT, vice TEXT,
    votos INTEGER, pct_validos REAL, eleito INTEGER, situacao TEXT,
    PRIMARY KEY (snapshot_id, sqcand)
);

CREATE TABLE IF NOT EXISTS party_result (
    snapshot_id INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE,
    numero TEXT NOT NULL, sigla TEXT, federacao TEXT, agremiacao TEXT, agremiacao_tipo TEXT,
    votos_nominais INTEGER, votos_legenda INTEGER, vagas_agremiacao INTEGER,
    PRIMARY KEY (snapshot_id, numero)
);

CREATE TABLE IF NOT EXISTS latest (
    eleicao TEXT NOT NULL, cargo INTEGER NOT NULL, nivel TEXT NOT NULL, uf TEXT NOT NULL, mun TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL, tse_ts TEXT NOT NULL,
    PRIMARY KEY (eleicao, cargo, nivel, uf, mun)
);

CREATE TABLE IF NOT EXISTS http_cache (
    url TEXT PRIMARY KEY, etag TEXT, last_modified TEXT, status INTEGER, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS focus (uf TEXT PRIMARY KEY, since TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL,
    eleicao TEXT, cargo INTEGER, uf TEXT, message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

HISTORY_TOP_N = 50  # candidatos mantidos nos snapshots antigos de cargos proporcionais (nível br/uf)

SNAPSHOT_COLS = [
    "eleicao", "turno", "cargo", "nivel", "uf", "mun", "tse_ts", "fetched_at", "finalizada", "vagas",
    "secoes_total", "secoes_totalizadas", "pct_secoes", "eleitorado", "eleitorado_apurado",
    "comparecimento", "abstencao", "votos_total", "validos", "nominais", "legenda",
    "brancos", "nulos", "anulados",
]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(path: str | Path | None = None, init: bool = True) -> sqlite3.Connection:
    """Abre o banco (WAL). Leitores devem abrir uma conexão por execução do script."""
    p = Path(path) if path else Path(config.DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    if init:
        init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _cargos(cargo: int) -> tuple[int, ...]:
    return (7, 8) if cargo in (7, 8) else (cargo,)


def _in(n: int) -> str:
    return ",".join("?" * n)


# ----------------------------------------------------------------------------- escrita

def write_snapshot(conn: sqlite3.Connection, snap: Snapshot, fetched_at: datetime | None = None) -> int | None:
    """Grava um snapshot. Retorna o id novo, ou None se (área, tse_ts) já existia."""
    fetched = (fetched_at or datetime.now()).isoformat(timespec="seconds")
    tse_ts = snap.tse_ts.isoformat(timespec="seconds") if snap.tse_ts else fetched
    t = snap.totals
    row = (
        snap.eleicao, snap.turno, snap.cargo, snap.nivel, snap.uf, snap.mun, tse_ts, fetched,
        int(snap.finalizada), snap.vagas, t.secoes_total, t.secoes_totalizadas, t.pct_secoes,
        t.eleitorado, t.eleitorado_apurado, t.comparecimento, t.abstencao, t.votos_total, t.validos,
        t.nominais, t.legenda, t.brancos, t.nulos, t.anulados,
    )
    with conn:
        cur = conn.execute(
            f"INSERT OR IGNORE INTO snapshot ({','.join(SNAPSHOT_COLS)}) VALUES ({_in(len(SNAPSHOT_COLS))})", row)
        if cur.rowcount == 0:
            return None
        sid = cur.lastrowid
        conn.executemany(
            "INSERT OR IGNORE INTO cand_result VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(sid, c.sqcand, c.numero, c.nome, c.nome_urna, c.partido, c.partido_numero, c.federacao,
              c.agremiacao, c.vice, c.votos, c.pct_validos, int(c.eleito), c.situacao) for c in snap.candidates])
        conn.executemany(
            "INSERT OR IGNORE INTO party_result VALUES (?,?,?,?,?,?,?,?,?)",
            [(sid, p.numero, p.sigla, p.federacao, p.agremiacao, p.agremiacao_tipo, p.votos_nominais,
              p.votos_legenda, p.vagas_agremiacao) for p in snap.parties])
        key = (snap.eleicao, snap.cargo, snap.nivel, snap.uf, snap.mun)
        prev = conn.execute(
            "SELECT snapshot_id, tse_ts FROM latest WHERE eleicao=? AND cargo=? AND nivel=? AND uf=? AND mun=?",
            key).fetchone()
        if prev is None or tse_ts >= prev[1]:
            conn.execute("INSERT OR REPLACE INTO latest VALUES (?,?,?,?,?,?,?)", (*key, sid, tse_ts))
            if snap.nivel == "mu" and prev is not None:
                conn.execute("DELETE FROM snapshot WHERE id=?", (prev[0],))
            elif snap.cargo in config.CARGOS_PROPORCIONAIS and prev is not None:
                # deputados: ~1000 candidatos por arquivo; histórico guarda só os mais votados
                conn.execute(
                    "DELETE FROM cand_result WHERE snapshot_id=? AND sqcand NOT IN "
                    "(SELECT sqcand FROM cand_result WHERE snapshot_id=? ORDER BY votos DESC LIMIT ?)",
                    (prev[0], prev[0], HISTORY_TOP_N))
        elif snap.nivel == "mu":  # chegou fora de ordem e é mais antigo: descarta
            conn.execute("DELETE FROM snapshot WHERE id=?", (sid,))
            return None
    return sid


def get_http_cache(conn: sqlite3.Connection, url: str) -> tuple[str | None, str | None] | None:
    r = conn.execute("SELECT etag, last_modified FROM http_cache WHERE url=?", (url,)).fetchone()
    return (r[0], r[1]) if r else None


def set_http_cache(conn: sqlite3.Connection, url: str, etag: str | None, last_modified: str | None,
                   status: int) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO http_cache VALUES (?,?,?,?,?)",
                     (url, etag, last_modified, status, _now()))


def set_focus(conn: sqlite3.Connection, ufs: Iterable[str]) -> None:
    ufs = [u.lower() for u in ufs]
    with conn:
        conn.execute(f"DELETE FROM focus WHERE uf NOT IN ({_in(len(ufs))})", ufs) if ufs else conn.execute(
            "DELETE FROM focus")
        conn.executemany("INSERT OR IGNORE INTO focus VALUES (?,?)", [(u, _now()) for u in ufs])


def add_focus(conn: sqlite3.Connection, uf: str) -> None:
    with conn:
        conn.execute("INSERT OR IGNORE INTO focus VALUES (?,?)", (uf.lower(), _now()))


def get_focus(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT uf FROM focus ORDER BY since")]


def add_event(conn: sqlite3.Connection, kind: str, message: str, eleicao: str = "", cargo: int = 0,
              uf: str = "") -> int:
    with conn:
        cur = conn.execute("INSERT INTO event (ts, kind, eleicao, cargo, uf, message) VALUES (?,?,?,?,?,?)",
                           (_now(), kind, eleicao, cargo, uf, message))
    return cur.lastrowid


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


# ----------------------------------------------------------------------------- leitura (DataFrames)

def events_since(conn: sqlite3.Connection, since_id: int = 0, limit: int = 200) -> pd.DataFrame:
    """Colunas: id, ts, kind, eleicao, cargo, uf, message (mais recentes por último)."""
    return pd.read_sql_query(
        "SELECT * FROM (SELECT * FROM event WHERE id>? ORDER BY id DESC LIMIT ?) ORDER BY id",
        conn, params=(since_id, limit))


def _area_filter(nivel: str, uf: str | None, mun: str | None) -> tuple[str, list]:
    sql, params = " AND l.nivel=?", [nivel]
    if uf is not None:
        sql += " AND l.uf=?"
        params.append(uf.lower())
    if mun is not None:
        sql += " AND l.mun=?"
        params.append(mun)
    return sql, params


def latest_totals(conn: sqlite3.Connection, eleicao: str, cargo: int, nivel: str,
                  uf: str | None = None, mun: str | None = None) -> pd.DataFrame:
    """Último snapshot de cada área. Uma linha por área.

    nivel='br' -> 1 linha; nivel='uf' -> uma por UF (inclui 'zz' p/ Presidente); nivel='mu' -> municípios
    (passe uf). Colunas: snapshot_id + SNAPSHOT_COLS. tse_ts é datetime.
    """
    cs = _cargos(cargo)
    filt, params = _area_filter(nivel, uf, mun)
    df = pd.read_sql_query(
        f"SELECT s.id AS snapshot_id, {', '.join('s.' + c for c in SNAPSHOT_COLS)} FROM latest l "
        f"JOIN snapshot s ON s.id = l.snapshot_id WHERE l.eleicao=? AND l.cargo IN ({_in(len(cs))}){filt} "
        f"ORDER BY s.uf, s.mun", conn, params=[eleicao, *cs, *params])
    df["tse_ts"] = pd.to_datetime(df["tse_ts"])
    return df


def latest_candidates(conn: sqlite3.Connection, eleicao: str, cargo: int, nivel: str,
                      uf: str | None = None, mun: str | None = None) -> pd.DataFrame:
    """Candidatos do último snapshot de cada área.

    Colunas: uf, mun, cargo, sqcand, numero, nome, nome_urna, partido, partido_numero, federacao, agremiacao,
    vice, votos, pct_validos, eleito (bool), situacao, rank (1 = mais votado na área), pct_secoes, tse_ts.
    Ordenado por uf, mun, rank.
    """
    cs = _cargos(cargo)
    filt, params = _area_filter(nivel, uf, mun)
    df = pd.read_sql_query(
        "SELECT s.uf, s.mun, s.cargo, c.sqcand, c.numero, c.nome, c.nome_urna, c.partido, c.partido_numero, "
        "c.federacao, c.agremiacao, c.vice, c.votos, c.pct_validos, c.eleito, c.situacao, s.pct_secoes, s.tse_ts "
        f"FROM latest l JOIN snapshot s ON s.id = l.snapshot_id JOIN cand_result c ON c.snapshot_id = s.id "
        f"WHERE l.eleicao=? AND l.cargo IN ({_in(len(cs))}){filt}",
        conn, params=[eleicao, *cs, *params])
    df["eleito"] = df["eleito"].astype(bool)
    df["tse_ts"] = pd.to_datetime(df["tse_ts"])
    df = df.sort_values(["uf", "mun", "votos", "numero"], ascending=[True, True, False, True])
    df["rank"] = df.groupby(["uf", "mun"]).cumcount() + 1
    return df.reset_index(drop=True)


def leaders(conn: sqlite3.Connection, eleicao: str, cargo: int, nivel: str, uf: str | None = None) -> pd.DataFrame:
    """Uma linha por área com o 1º e o 2º colocados.

    Colunas: uf, mun, sqcand, numero, nome_urna, partido, votos, pct_validos, eleito, situacao,
    second_nome_urna, second_partido, second_pct, margin_pp (pct 1º - pct 2º), pct_secoes, tse_ts.
    Áreas sem nenhum voto ainda aparecem com sqcand/nome_urna/partido = None e pct = 0.
    """
    cands = latest_candidates(conn, eleicao, cargo, nivel, uf)
    cols = ["uf", "mun", "sqcand", "numero", "nome_urna", "partido", "votos", "pct_validos", "eleito", "situacao",
            "second_nome_urna", "second_partido", "second_pct", "margin_pp", "pct_secoes", "tse_ts"]
    if cands.empty:
        return pd.DataFrame(columns=cols)
    first = cands[cands["rank"] == 1].set_index(["uf", "mun"])
    second = cands[cands["rank"] == 2].set_index(["uf", "mun"])[["nome_urna", "partido", "pct_validos"]]
    second.columns = ["second_nome_urna", "second_partido", "second_pct"]
    out = first.join(second, how="left").reset_index()
    out["second_pct"] = out["second_pct"].fillna(0.0)
    out["margin_pp"] = out["pct_validos"] - out["second_pct"]
    no_votes = out["votos"] <= 0
    out.loc[no_votes, ["sqcand", "numero", "nome_urna", "partido", "second_nome_urna", "second_partido"]] = None
    return out[cols]


def history(conn: sqlite3.Connection, eleicao: str, cargo: int, nivel: str = "br", uf: str = "br",
            mun: str = "") -> pd.DataFrame:
    """Evolução dos candidatos de uma área (só br/uf têm histórico).

    Formato longo, ordenado por tse_ts: snapshot_id, tse_ts, pct_secoes, sqcand, numero, nome_urna, partido,
    votos, pct_validos.
    """
    cs = _cargos(cargo)
    df = pd.read_sql_query(
        "SELECT s.id AS snapshot_id, s.tse_ts, s.pct_secoes, c.sqcand, c.numero, c.nome_urna, c.partido, "
        "c.votos, c.pct_validos FROM snapshot s JOIN cand_result c ON c.snapshot_id = s.id "
        f"WHERE s.eleicao=? AND s.cargo IN ({_in(len(cs))}) AND s.nivel=? AND s.uf=? AND s.mun=? "
        "ORDER BY s.tse_ts, c.votos DESC", conn, params=[eleicao, *cs, nivel, uf.lower(), mun])
    df["tse_ts"] = pd.to_datetime(df["tse_ts"])
    return df


def history_totals(conn: sqlite3.Connection, eleicao: str, cargo: int, nivel: str = "br", uf: str = "br",
                   mun: str = "") -> pd.DataFrame:
    """Histórico dos totais de uma área: snapshot_id + SNAPSHOT_COLS, ordenado por tse_ts."""
    cs = _cargos(cargo)
    df = pd.read_sql_query(
        f"SELECT id AS snapshot_id, {', '.join(SNAPSHOT_COLS)} FROM snapshot "
        f"WHERE eleicao=? AND cargo IN ({_in(len(cs))}) AND nivel=? AND uf=? AND mun=? ORDER BY tse_ts",
        conn, params=[eleicao, *cs, nivel, uf.lower(), mun])
    df["tse_ts"] = pd.to_datetime(df["tse_ts"])
    return df


def latest_parties(conn: sqlite3.Connection, eleicao: str, cargo: int, nivel: str = "uf",
                   uf: str | None = None) -> pd.DataFrame:
    """Partidos do último snapshot de cada área.

    Colunas: uf, mun, cargo, numero, sigla, federacao, agremiacao, agremiacao_tipo, votos_nominais,
    votos_legenda, vagas_agremiacao, vagas (vagas do cargo na área), pct_secoes, validos.
    """
    cs = _cargos(cargo)
    filt, params = _area_filter(nivel, uf, None)
    return pd.read_sql_query(
        "SELECT s.uf, s.mun, s.cargo, p.numero, p.sigla, p.federacao, p.agremiacao, p.agremiacao_tipo, "
        "p.votos_nominais, p.votos_legenda, p.vagas_agremiacao, s.vagas, s.pct_secoes, s.validos "
        "FROM latest l JOIN snapshot s ON s.id = l.snapshot_id JOIN party_result p ON p.snapshot_id = s.id "
        f"WHERE l.eleicao=? AND l.cargo IN ({_in(len(cs))}){filt} ORDER BY s.uf, p.sigla",
        conn, params=[eleicao, *cs, *params])


def search_candidates(conn: sqlite3.Connection, eleicao: str, cargo: int, query: str,
                      uf: str | None = None, limit: int = 50) -> pd.DataFrame:
    """Busca por nome (urna ou completo, sem diferenciar maiúsculas) ou número, no nível uf (ou br p/ cargo 1).

    Mesmas colunas de latest_candidates. Para Presidente busca no nível 'br'.
    """
    nivel = "br" if cargo == 1 and uf in (None, "br") else "uf"
    df = latest_candidates(conn, eleicao, cargo, nivel, None if uf == "br" else uf)
    q = query.strip().upper()
    if not q:
        return df.head(0)
    mask = (df["numero"] == q) | df["nome_urna"].str.upper().str.contains(q, regex=False) | \
        df["nome"].str.upper().str.contains(q, regex=False)
    return df[mask].sort_values("votos", ascending=False).head(limit).reset_index(drop=True)
