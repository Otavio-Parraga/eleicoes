"""Resultados de 2022 (dados abertos do TSE) para comparar com a apuração de 2026.

O feed ao vivo do TSE não tem mais 2022; usamos os ZIPs do portal de dados abertos (cdn.tse.jus.br):
- votacao_candidato_munzona_2022.zip (~642 MB): votos por candidato × município × zona, dois turnos.
  Só lemos o membro *_BR.csv (Presidente, inclui 'ZZ' = exterior) e os *_UF.csv (Governador; --no-gov pula).
- detalhe_votacao_munzona_2022.zip (~4 MB): aptos/comparecimento/abstenção/brancos/nulos por zona e cargo.

CLI (da raiz do projeto):
    conda run -n python3 python -m eleicoes.hist2022 download   # baixa os ZIPs para data/2022/ (retoma)
    conda run -n python3 python -m eleicoes.hist2022 build      # gera os parquet compactos (~20 s)
    conda run -n python3 python -m eleicoes.hist2022 status

Arquivos gerados em config.HIST_DIR (data/2022/):
- pres_mun.parquet  turno, uf, mun, nome, nr_candidato, nm_urna, partido, votos     (somado nas zonas)
- pres_uf.parquet   turno, uf, nr_candidato, nm_urna, partido, votos                (inclui 'zz')
- pres_br.parquet   turno, nr_candidato, nm_urna, partido, votos
- turnout_mun.parquet turno, cargo, uf, mun, aptos, comparecimento, abstencao, brancos, nulos, validos
- gov_mun.parquet   mesmas colunas de pres_mun, cargo Governador (pulado com --no-gov)

Convenções (iguais às de eleicoes.store): uf minúscula ('rs', 'zz'; 'br' no nível nacional),
mun = código TSE com 5 dígitos ('88013'; '' nos níveis br/uf), nível 'br' | 'uf' | 'mu'.
"""
from __future__ import annotations

import argparse
import sys
import time
import zipfile
from functools import lru_cache
from pathlib import Path

import pandas as pd

from . import config

ODSELE = "https://cdn.tse.jus.br/estatistica/sead/odsele"
ZIPS: dict[str, str] = {
    "votacao": f"{ODSELE}/votacao_candidato_munzona/votacao_candidato_munzona_2022.zip",
    "detalhe": f"{ODSELE}/detalhe_votacao_munzona/detalhe_votacao_munzona_2022.zip",
}
OUTPUTS = ("pres_mun", "pres_uf", "pres_br", "turnout_mun")
OPTIONAL_OUTPUTS = ("gov_mun",)

KEYS = ["uf", "mun"]
CAND_COLS = ["nr_candidato", "nm_urna", "partido"]
TURNOUT_COLS = ["aptos", "comparecimento", "abstencao", "brancos", "nulos", "validos"]

_VOT_USECOLS = ["CD_TIPO_ELEICAO", "NR_TURNO", "SG_UF", "CD_MUNICIPIO", "NM_MUNICIPIO", "CD_CARGO",
                "NR_CANDIDATO", "NM_URNA_CANDIDATO", "SG_PARTIDO", "QT_VOTOS_NOMINAIS_VALIDOS"]
_DET_USECOLS = ["CD_TIPO_ELEICAO", "NR_TURNO", "SG_UF", "CD_MUNICIPIO", "CD_CARGO", "QT_APTOS",
                "QT_COMPARECIMENTO", "QT_ABSTENCOES", "QT_VOTOS_BRANCOS", "QT_TOTAL_VOTOS_NULOS",
                "QT_TOTAL_VOTOS_VALIDOS"]
ORDINARIA = "2"  # CD_TIPO_ELEICAO da eleição geral (exclui suplementares)


def hist_dir() -> Path:
    return Path(config.HIST_DIR)


def zip_path(name: str) -> Path:
    return hist_dir() / Path(ZIPS[name]).name


def out_path(name: str) -> Path:
    return hist_dir() / f"{name}.parquet"


# ----------------------------------------------------------------------------- download

def download(names: list[str] | None = None, force: bool = False, verbose: bool = True) -> dict[str, int]:
    """Baixa os ZIPs (streaming, retoma downloads parciais via Range). Retorna {nome: bytes}."""
    import httpx

    hist_dir().mkdir(parents=True, exist_ok=True)
    out = {}
    for name in names or list(ZIPS):
        url, dest = ZIPS[name], zip_path(name)
        part = dest.with_suffix(dest.suffix + ".part")
        with httpx.Client(timeout=httpx.Timeout(60, read=120), follow_redirects=True) as cli:
            total = int(cli.head(url).headers.get("content-length", 0) or 0)
            if dest.exists() and not force and (total == 0 or dest.stat().st_size == total):
                if verbose:
                    print(f"{dest.name}: já baixado ({dest.stat().st_size / 1e6:.1f} MB)")
                out[name] = dest.stat().st_size
                continue
            if dest.exists() and not part.exists():  # arquivo incompleto de uma execução anterior: retoma
                dest.rename(part)
            have = part.stat().st_size if part.exists() and not force else 0
            headers = {"Range": f"bytes={have}-"} if have else {}
            t0, last = time.time(), 0.0
            with cli.stream("GET", url, headers=headers) as r:
                if r.status_code == 416:  # já completo
                    pass
                else:
                    r.raise_for_status()
                    mode = "ab" if have and r.status_code == 206 else "wb"
                    done = have if mode == "ab" else 0
                    with open(part, mode) as f:
                        for chunk in r.iter_bytes(1 << 20):
                            f.write(chunk)
                            done += len(chunk)
                            if verbose and time.time() - last > 2:
                                last = time.time()
                                pct = f" {100 * done / total:5.1f}%" if total else ""
                                print(f"\r{dest.name}: {done / 1e6:8.1f} MB{pct}", end="", file=sys.stderr)
            part.rename(dest)
            if verbose:
                print(f"\r{dest.name}: {dest.stat().st_size / 1e6:.1f} MB em {time.time() - t0:.0f} s"
                      + " " * 10)
            out[name] = dest.stat().st_size
    return out


# ----------------------------------------------------------------------------- shaping (funções puras)

def pad_mun(s: pd.Series) -> pd.Series:
    """Código TSE de município com 5 dígitos ('5231' -> '05231'). Vazio/nulo -> ''."""
    s = s.fillna("").astype(str).str.strip()
    s = s.str.replace(r"\.0$", "", regex=True)
    return s.where(s == "", s.str.zfill(5))


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").fillna(0).astype("int64")


def shape_votacao(raw: pd.DataFrame, cargo: int = 1) -> pd.DataFrame:
    """Linhas cruas (colunas _VOT_USECOLS, dtype str) -> votos por turno × município × candidato.

    Saída: turno, uf, mun, nome, nr_candidato, nm_urna, partido, votos (somado nas zonas).
    """
    df = raw
    if "CD_TIPO_ELEICAO" in df:
        df = df[df["CD_TIPO_ELEICAO"].astype(str) == ORDINARIA]
    df = df[df["CD_CARGO"].astype(str) == str(cargo)]
    out = pd.DataFrame({
        "turno": _num(df["NR_TURNO"]).astype("int8"),
        "uf": df["SG_UF"].str.lower().str.strip(),
        "mun": pad_mun(df["CD_MUNICIPIO"]),
        "nome": df["NM_MUNICIPIO"].str.strip(),
        "nr_candidato": df["NR_CANDIDATO"].astype(str).str.strip(),
        "nm_urna": df["NM_URNA_CANDIDATO"].str.strip(),
        "partido": df["SG_PARTIDO"].str.strip(),
        "votos": _num(df["QT_VOTOS_NOMINAIS_VALIDOS"]),
    })
    g = out.groupby(["turno", "uf", "mun", "nome", *CAND_COLS], as_index=False, sort=True)["votos"].sum()
    return g.reset_index(drop=True)


def shape_detalhe(raw: pd.DataFrame) -> pd.DataFrame:
    """Linhas cruas do detalhe_votacao (colunas _DET_USECOLS) -> totais por turno × cargo × município."""
    df = raw
    if "CD_TIPO_ELEICAO" in df:
        df = df[df["CD_TIPO_ELEICAO"].astype(str) == ORDINARIA]
    out = pd.DataFrame({
        "turno": _num(df["NR_TURNO"]).astype("int8"),
        "cargo": _num(df["CD_CARGO"]).astype("int8"),
        "uf": df["SG_UF"].str.lower().str.strip(),
        "mun": pad_mun(df["CD_MUNICIPIO"]),
        "aptos": _num(df["QT_APTOS"]),
        "comparecimento": _num(df["QT_COMPARECIMENTO"]),
        "abstencao": _num(df["QT_ABSTENCOES"]),
        "brancos": _num(df["QT_VOTOS_BRANCOS"]),
        "nulos": _num(df["QT_TOTAL_VOTOS_NULOS"]),
        "validos": _num(df["QT_TOTAL_VOTOS_VALIDOS"]),
    })
    return out.groupby(["turno", "cargo", "uf", "mun"], as_index=False, sort=True)[TURNOUT_COLS].sum()


def aggregate_uf(mun_df: pd.DataFrame) -> pd.DataFrame:
    return mun_df.groupby(["turno", "uf", *CAND_COLS], as_index=False, sort=True)["votos"].sum()


def aggregate_br(mun_df: pd.DataFrame) -> pd.DataFrame:
    return mun_df.groupby(["turno", *CAND_COLS], as_index=False, sort=True)["votos"].sum()


def _read_member(z: zipfile.ZipFile, member: str, usecols: list[str], cargo: int | None,
                 chunksize: int = 500_000) -> pd.DataFrame:
    parts = []
    with z.open(member) as f:
        for chunk in pd.read_csv(f, sep=";", encoding="latin-1", dtype=str, usecols=usecols,
                                 chunksize=chunksize, low_memory=False):
            if cargo is not None:
                chunk = chunk[chunk["CD_CARGO"] == str(cargo)]
            parts.append(chunk)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=usecols)


# ----------------------------------------------------------------------------- build

def build(gov: bool = True, verbose: bool = True) -> dict[str, int]:
    """Gera os parquet a partir dos ZIPs já baixados. Retorna {arquivo: linhas}."""
    counts: dict[str, int] = {}

    def save(df: pd.DataFrame, name: str) -> None:
        tmp = out_path(name).with_suffix(".tmp")
        df.to_parquet(tmp, index=False)
        tmp.replace(out_path(name))
        counts[name] = len(df)
        if verbose:
            print(f"{out_path(name).name}: {len(df):,} linhas")

    t0 = time.time()
    vz = zip_path("votacao")
    if not vz.exists():
        raise FileNotFoundError(f"{vz} não existe; rode primeiro: python -m eleicoes.hist2022 download")
    with zipfile.ZipFile(vz) as z:
        member = next(n for n in z.namelist() if n.endswith("_BR.csv"))
        raw = _read_member(z, member, _VOT_USECOLS, cargo=1)
        pres = shape_votacao(raw, cargo=1)
        save(pres, "pres_mun")
        save(aggregate_uf(pres), "pres_uf")
        save(aggregate_br(pres), "pres_br")
        if gov:
            parts = []
            for uf in config.UF_LIST:
                m = f"votacao_candidato_munzona_2022_{uf.upper()}.csv"
                if m in z.namelist():
                    if verbose:
                        print(f"  governador: {m}", file=sys.stderr)
                    parts.append(shape_votacao(_read_member(z, m, _VOT_USECOLS, cargo=3), cargo=3))
            save(pd.concat(parts, ignore_index=True), "gov_mun")

    dz = zip_path("detalhe")
    if dz.exists():
        with zipfile.ZipFile(dz) as z:
            member = next(n for n in z.namelist() if n.endswith("_BRASIL.csv"))
            save(shape_detalhe(_read_member(z, member, _DET_USECOLS, cargo=None)), "turnout_mun")
    elif verbose:
        print(f"aviso: {dz.name} ausente; turnout_mun não gerado", file=sys.stderr)
    if verbose:
        print(f"build em {time.time() - t0:.1f} s")
    _load.cache_clear()
    return counts


# ----------------------------------------------------------------------------- status / leitura

def status() -> dict:
    """{'dir', 'zips': {nome: bytes|None}, 'parquet': {nome: linhas|None}, 'ready': bool}."""
    import pyarrow.parquet as pq

    zips = {n: (zip_path(n).stat().st_size if zip_path(n).exists() else None) for n in ZIPS}
    pqs = {}
    for n in (*OUTPUTS, *OPTIONAL_OUTPUTS):
        p = out_path(n)
        try:
            pqs[n] = pq.ParquetFile(p).metadata.num_rows if p.exists() else None
        except Exception:  # arquivo corrompido/incompleto
            pqs[n] = None
    return {"dir": str(hist_dir()), "zips": zips, "parquet": pqs,
            "ready": all(pqs[n] for n in ("pres_mun", "pres_uf", "pres_br"))}


def is_ready() -> bool:
    return status()["ready"]


@lru_cache(maxsize=16)
def _load(name: str, mtime: float) -> pd.DataFrame:
    return pd.read_parquet(out_path(name))


def load(name: str) -> pd.DataFrame:
    """Lê um parquet gerado (cache em memória invalidado quando o arquivo muda)."""
    p = out_path(name)
    if not p.exists():
        raise FileNotFoundError(f"{p} não existe; rode: python -m eleicoes.hist2022 download && ... build")
    return _load(name, p.stat().st_mtime)


def _level(level: str) -> str:
    lv = {"mun": "mu", "municipio": "mu"}.get(level, level)
    if lv not in ("br", "uf", "mu"):
        raise ValueError(f"nível inválido: {level!r} (use 'br', 'uf' ou 'mu')")
    return lv


def add_share(df: pd.DataFrame, keys: list[str] = KEYS) -> pd.DataFrame:
    """Acrescenta pct_validos (0-100) = votos / soma dos votos da área."""
    df = df.copy()
    tot = df.groupby(keys)["votos"].transform("sum")
    df["pct_validos"] = (100.0 * df["votos"] / tot.where(tot > 0)).fillna(0.0)
    return df


def _votes(table: str, level: str, turno: int, uf: str | None) -> pd.DataFrame:
    lv = _level(level)
    if lv == "br":
        df = load(table.replace("_mun", "_br")) if table == "pres_mun" else aggregate_br(load(table))
        df = df[df["turno"] == turno].assign(uf=config.BRASIL, mun="")
    elif lv == "uf":
        df = load("pres_uf") if table == "pres_mun" else aggregate_uf(load(table))
        df = df[df["turno"] == turno].assign(mun="")
    else:
        df = load(table)
        df = df[df["turno"] == turno]
    if uf is not None and lv != "br":
        df = df[df["uf"] == uf.lower()]
    return df


def pres_share(level: str = "uf", turno: int = 1, uf: str | None = None) -> pd.DataFrame:
    """Presidente 2022 por candidato e área.

    level 'br' | 'uf' | 'mu' ('mun' também aceito); uf filtra (ex. 'rs'; nos níveis uf/mu).
    Colunas: uf, mun, nome (só 'mu'), partido, nr_candidato, nm_urna, votos, pct_validos.
    """
    df = _votes("pres_mun", level, turno, uf)
    cols = ["uf", "mun", *(["nome"] if "nome" in df else []), "partido", "nr_candidato", "nm_urna", "votos"]
    out = add_share(df[cols].reset_index(drop=True))
    return out.sort_values(["uf", "mun", "votos"], ascending=[True, True, False]).reset_index(drop=True)


def gov_share(level: str = "uf", turno: int = 1, uf: str | None = None) -> pd.DataFrame:
    """Governador 2022 (gov_mun.parquet). Mesmas colunas de pres_share (nível 'br' não faz sentido)."""
    df = _votes("gov_mun", level, turno, uf)
    cols = ["uf", "mun", *(["nome"] if "nome" in df else []), "partido", "nr_candidato", "nm_urna", "votos"]
    return add_share(df[cols].reset_index(drop=True))


def party_share(level: str = "uf", turno: int = 1, uf: str | None = None, cargo: int = 1) -> pd.DataFrame:
    """% dos válidos por partido e área (Presidente; cargo=3 usa gov_mun).

    Colunas: uf, mun, partido, nm_urna (candidato do partido), votos, pct_validos.
    """
    df = (pres_share if cargo == 1 else gov_share)(level, turno, uf)
    g = df.groupby([*KEYS, "partido"], as_index=False).agg(
        nm_urna=("nm_urna", lambda s: " / ".join(sorted(set(s)))), votos=("votos", "sum"),
        pct_validos=("pct_validos", "sum"))
    return g


def turnout(level: str = "uf", turno: int = 1, cargo: int = 1, uf: str | None = None) -> pd.DataFrame:
    """Comparecimento 2022 por área (somado sobre zonas/municípios).

    Colunas: uf, mun, aptos, comparecimento, abstencao, brancos, nulos, validos, pct_abstencao (0-100).
    Cargo 7 inclui o 8 (Distrital).
    """
    lv = _level(level)
    df = load("turnout_mun")
    cs = (7, 8) if cargo in (7, 8) else (cargo,)
    df = df[(df["turno"] == turno) & (df["cargo"].isin(cs))]
    if uf is not None and lv != "br":
        df = df[df["uf"] == uf.lower()]
    if lv == "br":
        df = df.assign(uf=config.BRASIL, mun="")
    elif lv == "uf":
        df = df.assign(mun="")
    out = df.groupby(KEYS, as_index=False)[TURNOUT_COLS].sum()
    out["pct_abstencao"] = (100.0 * out["abstencao"] / out["aptos"].where(out["aptos"] > 0)).fillna(0.0)
    return out


# ----------------------------------------------------------------------------- comparação com 2026

def share_2026(cands: pd.DataFrame, keys: list[str] = KEYS) -> pd.DataFrame:
    """Candidatos 2026 (formato de store.latest_candidates) -> % dos válidos por partido e área.

    Só áreas com algum voto. Colunas: *keys, partido, nm_urna, votos, pct_validos, pct_secoes.
    """
    cols = [*keys, "partido", "nm_urna", "votos", "pct_validos", "pct_secoes"]
    if cands is None or cands.empty:
        return pd.DataFrame(columns=cols)
    df = cands.rename(columns={"nome_urna": "nm_urna"}).copy()
    df["votos"] = pd.to_numeric(df["votos"], errors="coerce").fillna(0)
    if "pct_secoes" not in df:
        df["pct_secoes"] = float("nan")
    df["partido"] = df["partido"].fillna("").astype(str).str.strip()
    g = df.groupby([*keys, "partido"], as_index=False).agg(
        nm_urna=("nm_urna", lambda s: " / ".join(sorted(set(map(str, s))))), votos=("votos", "sum"),
        pct_secoes=("pct_secoes", "max"))
    tot = g.groupby(keys)["votos"].transform("sum")
    g = g[tot > 0].copy()
    g["pct_validos"] = 100.0 * g["votos"] / tot[tot > 0]
    return g[cols].reset_index(drop=True)


def compute_swing(s26: pd.DataFrame, s22: pd.DataFrame, partido: str, keys: list[str] = KEYS,
                  require_both: bool = False) -> pd.DataFrame:
    """Swing de um partido: % válidos 2026 − % válidos 2022 (pontos percentuais), por área.

    s26: saída de share_2026; s22: saída de party_share. Áreas sem apuração em 2026 ficam de fora; áreas
    onde o partido não teve candidato em um dos anos contam 0% naquele ano (desde que a área exista nos
    dois) — ou ficam de fora com require_both=True (útil p/ Governador, em que cada UF é uma disputa).
    Colunas: *keys, nm_urna_2022, nm_urna_2026, pct_2022, pct_2026, swing_pp, pct_secoes.
    """
    cols = [*keys, "nm_urna_2022", "nm_urna_2026", "pct_2022", "pct_2026", "swing_pp", "pct_secoes"]
    if s26 is None or s26.empty or s22 is None or s22.empty:
        return pd.DataFrame(columns=cols)
    p = partido.strip().upper()
    a26 = s26.groupby(keys, as_index=False)["pct_secoes"].max()            # áreas apuradas em 2026
    a22 = s22[keys].drop_duplicates()                                       # áreas existentes em 2022
    areas = a26.merge(a22, on=keys, how="inner")
    x26 = s26[s26["partido"].str.upper() == p][[*keys, "nm_urna", "pct_validos"]]
    x22 = s22[s22["partido"].str.upper() == p][[*keys, "nm_urna", "pct_validos"]]
    out = (areas.merge(x26.rename(columns={"nm_urna": "nm_urna_2026", "pct_validos": "pct_2026"}), on=keys,
                       how="left")
           .merge(x22.rename(columns={"nm_urna": "nm_urna_2022", "pct_validos": "pct_2022"}), on=keys,
                  how="left"))
    if require_both:
        out = out[out["pct_2026"].notna() & out["pct_2022"].notna()].copy()
    out[["pct_2026", "pct_2022"]] = out[["pct_2026", "pct_2022"]].fillna(0.0)
    out[["nm_urna_2026", "nm_urna_2022"]] = out[["nm_urna_2026", "nm_urna_2022"]].fillna("—")
    out["swing_pp"] = out["pct_2026"] - out["pct_2022"]
    return out[cols].sort_values(keys).reset_index(drop=True)


def turnout_2026(totals: pd.DataFrame, keys: list[str] = KEYS) -> pd.DataFrame:
    """Totais 2026 (store.latest_totals) -> abstenção na parte apurada. Só áreas com eleitorado apurado.

    Colunas: *keys, eleitorado_apurado, abstencao, pct_abstencao, pct_secoes.
    """
    cols = [*keys, "eleitorado_apurado", "abstencao", "pct_abstencao", "pct_secoes"]
    if totals is None or totals.empty:
        return pd.DataFrame(columns=cols)
    df = totals.copy()
    df = df[pd.to_numeric(df["eleitorado_apurado"], errors="coerce").fillna(0) > 0]
    df["pct_abstencao"] = 100.0 * df["abstencao"] / df["eleitorado_apurado"]
    return df[cols].reset_index(drop=True)


def compute_turnout_change(t26: pd.DataFrame, t22: pd.DataFrame, keys: list[str] = KEYS) -> pd.DataFrame:
    """Abstenção 2026 (parte apurada) − 2022, em pp. Só áreas presentes nos dois.

    Colunas: *keys, abst_2022, abst_2026, delta_pp, pct_secoes.
    """
    cols = [*keys, "abst_2022", "abst_2026", "delta_pp", "pct_secoes"]
    if t26 is None or t26.empty or t22 is None or t22.empty:
        return pd.DataFrame(columns=cols)
    out = t26[[*keys, "pct_abstencao", "pct_secoes"]].rename(columns={"pct_abstencao": "abst_2026"}).merge(
        t22[[*keys, "pct_abstencao"]].rename(columns={"pct_abstencao": "abst_2022"}), on=keys, how="inner")
    out["delta_pp"] = out["abst_2026"] - out["abst_2022"]
    return out[cols].sort_values(keys).reset_index(drop=True)


# ----------------------------------------------------------------------------- CLI

def _print_status() -> None:
    st = status()
    print(f"diretório: {st['dir']}")
    for n, b in st["zips"].items():
        print(f"  zip {n:8s}: {'—' if b is None else f'{b / 1e6:.1f} MB'}")
    for n, rows in st["parquet"].items():
        print(f"  {n + '.parquet':20s}: {'—' if rows is None else f'{rows:,} linhas'}")
    print("pronto" if st["ready"] else "incompleto")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eleicoes.hist2022", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download", help="baixa os ZIPs de 2022 (retoma)")
    d.add_argument("--force", action="store_true")
    d.add_argument("--only", choices=list(ZIPS), nargs="*")
    b = sub.add_parser("build", help="gera os parquet em data/2022/")
    b.add_argument("--no-gov", action="store_true", help="pula Governador por município (lê ~4 GB de CSV, ~20 s)")
    sub.add_parser("status", help="mostra o que já existe")
    args = ap.parse_args(argv)
    if args.cmd == "download":
        download(args.only, force=args.force)
    elif args.cmd == "build":
        build(gov=not args.no_gov)
    _print_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
