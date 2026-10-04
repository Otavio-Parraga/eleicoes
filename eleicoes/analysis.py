"""Análises sobre os DataFrames do store: votos restantes, projeção ingênua, "o líder ainda pode ser alcançado?".

Funções puras (sem Streamlit, sem banco). Uma "área" é o par (uf, mun): no nível 'uf' mun == ''.

Projeção ingênua: supõe que o que falta apurar em cada área vota como o que já foi apurado nela.
Não é pesquisa nem previsão oficial.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

AREA = ["uf", "mun"]
# Usado só quando nada foi apurado em nenhuma área (válidos / eleitorado ≈ 0,75 em 2022).
DEFAULT_TAXA_VALIDOS = 0.75
RESTO = "~resto"  # código de "município" da área residual criada por add_residual_area

REMAINING_COLS = AREA + ["eleitorado", "eleitorado_apurado", "faltam_eleitores", "pct_secoes", "validos",
                         "taxa_validos", "validos_restantes_est"]
PROJECTION_COLS = ["sqcand", "numero", "nome_urna", "partido", "votos_atuais", "pct_atual", "votos_projetados",
                   "pct_projetado", "delta_pp"]
LEADER_COLS = AREA + ["total_votos", "lider_sqcand", "lider_nome", "lider_partido", "lider_votos", "lider_pct",
                      "segundo_nome", "segundo_partido", "segundo_votos", "corte_nome", "corte_partido",
                      "corte_votos", "desafiante_nome", "desafiante_partido", "desafiante_votos", "margem_votos"]

ST_SEM_VOTOS = "Sem votos apurados"
ST_DECIDIDO = "Decidido"
ST_PRATICA = "Decidido na prática"
ST_ABERTO = "Em aberto"


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    """Cópia com as colunas de área garantidas (uf, mun como texto)."""
    d = df.copy()
    if "uf" not in d.columns:
        d["uf"] = ""
    if "mun" not in d.columns:
        d["mun"] = ""
    d["uf"] = d["uf"].fillna("").astype(str)
    d["mun"] = d["mun"].fillna("").astype(str)
    return d


def _num(d: pd.DataFrame, col: str) -> pd.Series:
    if col not in d.columns:
        return pd.Series(0.0, index=d.index)
    return pd.to_numeric(d[col], errors="coerce").fillna(0).astype(float)


def _empty(cols: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=cols)


# ----------------------------------------------------------------------------- votos restantes

def overall_taxa_validos(totals: pd.DataFrame) -> float:
    """Válidos / eleitorado apurado somando todas as áreas (DEFAULT_TAXA_VALIDOS se nada foi apurado)."""
    if totals is None or totals.empty:
        return DEFAULT_TAXA_VALIDOS
    ap, vv = _num(totals, "eleitorado_apurado").sum(), _num(totals, "validos").sum()
    return float(vv / ap) if ap > 0 and vv > 0 else DEFAULT_TAXA_VALIDOS


def remaining_by_area(totals: pd.DataFrame) -> pd.DataFrame:
    """Quanto falta apurar em cada área (linhas de store.latest_totals, nível 'uf' ou 'mu').

    Colunas: uf, mun, eleitorado, eleitorado_apurado, faltam_eleitores, pct_secoes, validos, taxa_validos,
    validos_restantes_est (+ secoes_total, secoes_totalizadas, vagas, quando presentes na entrada).
    taxa_validos = validos / eleitorado_apurado; sem nada apurado na área, usa a taxa geral de todas as áreas.
    """
    if totals is None or totals.empty:
        return _empty(REMAINING_COLS)
    t = _norm(totals).reset_index(drop=True)
    el = _num(t, "eleitorado")
    ap = np.minimum(_num(t, "eleitorado_apurado"), el)
    vv = _num(t, "validos")
    faltam = (el - ap).clip(lower=0)
    geral = overall_taxa_validos(t)
    taxa = pd.Series(np.where(ap > 0, vv / ap.where(ap > 0, 1.0), geral), index=t.index)
    if "pct_secoes" in t.columns:
        pct = _num(t, "pct_secoes")
    else:
        ts = _num(t, "secoes_total")
        pct = pd.Series(np.where(ts > 0, 100 * _num(t, "secoes_totalizadas") / ts.where(ts > 0, 1.0), 0.0),
                        index=t.index)
    out = pd.DataFrame({
        "uf": t["uf"], "mun": t["mun"],
        "eleitorado": el.round().astype("int64"),
        "eleitorado_apurado": ap.round().astype("int64"),
        "faltam_eleitores": faltam.round().astype("int64"),
        "pct_secoes": pct,
        "validos": vv.round().astype("int64"),
        "taxa_validos": taxa.astype(float),
        "validos_restantes_est": (faltam * taxa).round().astype("int64"),
    })
    for extra in ("secoes_total", "secoes_totalizadas", "vagas"):
        if extra in t.columns:
            out[extra] = _num(t, extra).round().astype("int64")
    return out


# ----------------------------------------------------------------------------- projeção ingênua

def project_final(cands: pd.DataFrame, totals: pd.DataFrame) -> pd.DataFrame:
    """Projeção ingênua do resultado final somando áreas (UFs + 'zz' para Presidente, ou municípios de uma UF).

    Em cada área: votos projetados = votos atuais + fatia atual do candidato na área × validos_restantes_est.
    Área sem nenhum voto ainda: usa a fatia geral do candidato (soma de todas as áreas).
    Colunas: sqcand, numero, nome_urna, partido, votos_atuais, pct_atual, votos_projetados, pct_projetado,
    delta_pp (projetado − atual, em pontos percentuais). Ordenado por votos_projetados desc.
    """
    if cands is None or cands.empty:
        return _empty(PROJECTION_COLS)
    c = _norm(cands)
    c["votos"] = _num(c, "votos")
    rem = remaining_by_area(totals) if totals is not None and not totals.empty else _empty(REMAINING_COLS)

    mat = c.pivot_table(index=AREA, columns="sqcand", values="votos", aggfunc="sum", fill_value=0.0)
    rem_idx = pd.MultiIndex.from_frame(rem[AREA]) if not rem.empty else pd.MultiIndex.from_tuples([], names=AREA)
    idx = mat.index.union(rem_idx)
    mat = mat.reindex(idx, fill_value=0.0).astype(float)
    restantes = (rem.set_index(AREA)["validos_restantes_est"].astype(float).reindex(idx).fillna(0.0)
                 if not rem.empty else pd.Series(0.0, index=idx))

    geral = mat.sum(axis=0)
    geral_share = geral / geral.sum() if geral.sum() > 0 else geral * 0.0
    rowsum = mat.sum(axis=1)
    share = mat.div(rowsum.where(rowsum > 0), axis=0)
    sem_votos = rowsum <= 0
    if sem_votos.any():
        share.loc[sem_votos, :] = np.tile(geral_share.to_numpy(), (int(sem_votos.sum()), 1))
    proj = mat + share.mul(restantes, axis=0)

    atuais, projetados = mat.sum(axis=0), proj.sum(axis=0)
    info_cols = [col for col in ("sqcand", "numero", "nome_urna", "partido") if col in c.columns]
    info = c.drop_duplicates("sqcand")[info_cols].set_index("sqcand")
    out = pd.DataFrame({"votos_atuais": atuais, "votos_projetados": projetados})
    out.index.name = "sqcand"
    out = out.join(info, how="left").reset_index()
    for col in ("numero", "nome_urna", "partido"):
        if col not in out.columns:
            out[col] = ""
    sa, sp = out["votos_atuais"].sum(), out["votos_projetados"].sum()
    out["pct_atual"] = 100 * out["votos_atuais"] / sa if sa > 0 else 0.0
    out["pct_projetado"] = 100 * out["votos_projetados"] / sp if sp > 0 else 0.0
    out["delta_pp"] = out["pct_projetado"] - out["pct_atual"]
    out["votos_atuais"] = out["votos_atuais"].round().astype("int64")
    out["votos_projetados"] = out["votos_projetados"].round().astype("int64")
    out = out.sort_values(["votos_projetados", "votos_atuais", "numero"], ascending=[False, False, True])
    return out[PROJECTION_COLS].reset_index(drop=True)


# ----------------------------------------------------------------------------- líderes e "ainda dá?"

def area_leaders(cands: pd.DataFrame, vagas: int = 1) -> pd.DataFrame:
    """Uma linha por área: líder, 2º colocado, último eleito ('corte', posição = vagas) e o 1º de fora
    ('desafiante', posição vagas+1). margem_votos = votos do corte − votos do desafiante.

    Para vagas=1 (Presidente, Governador) corte = líder e desafiante = 2º. Senado 2026: vagas=2.
    Áreas sem votos: campos de candidato None e votos 0.
    """
    if cands is None or cands.empty:
        return _empty(LEADER_COLS)
    vagas = max(1, int(vagas or 1))
    c = _norm(cands)
    c["votos"] = _num(c, "votos")
    for col in ("sqcand", "nome_urna", "partido", "numero"):
        if col not in c.columns:
            c[col] = ""
    c = c.sort_values(AREA + ["votos", "numero"], ascending=[True, True, False, True])
    c["_pos"] = c.groupby(AREA).cumcount() + 1
    base = c.groupby(AREA)["votos"].sum().rename("total_votos").to_frame()

    def at(pos: int, prefix: str, with_id: bool = False) -> pd.DataFrame:
        sel = c[c["_pos"] == pos].set_index(AREA)
        cols = {"nome_urna": f"{prefix}_nome", "partido": f"{prefix}_partido", "votos": f"{prefix}_votos"}
        if with_id:
            cols["sqcand"] = f"{prefix}_sqcand"
        return sel[list(cols)].rename(columns=cols)

    out = (base.join(at(1, "lider", with_id=True)).join(at(2, "segundo")).join(at(vagas, "corte"))
           .join(at(vagas + 1, "desafiante")).reset_index())
    for p in ("lider", "segundo", "corte", "desafiante"):
        out[f"{p}_votos"] = out[f"{p}_votos"].fillna(0.0)
    tot = out["total_votos"]
    out["lider_pct"] = np.where(tot > 0, 100 * out["lider_votos"] / tot.where(tot > 0, 1.0), 0.0)
    out["margem_votos"] = out["corte_votos"] - out["desafiante_votos"]
    sem = tot <= 0
    name_cols = [f"{p}_{k}" for p in ("lider", "segundo", "corte", "desafiante") for k in ("nome", "partido")]
    out[name_cols + ["lider_sqcand"]] = out[name_cols + ["lider_sqcand"]].astype(object)
    out.loc[sem, name_cols + ["lider_sqcand"]] = None
    for col in ("total_votos", "lider_votos", "segundo_votos", "corte_votos", "desafiante_votos", "margem_votos"):
        out[col] = out[col].round().astype("int64")
    return out[LEADER_COLS]


def remaining_with_leaders(cands: pd.DataFrame, totals: pd.DataFrame, vagas: int = 1) -> pd.DataFrame:
    """remaining_by_area + area_leaders: onde estão os votos que faltam e quem lidera ali."""
    rem = remaining_by_area(totals)
    if rem.empty:
        return _empty(REMAINING_COLS + [c for c in LEADER_COLS if c not in AREA])
    lead = area_leaders(cands, vagas)
    out = rem.merge(lead, on=AREA, how="left") if not lead.empty else rem.reindex(
        columns=REMAINING_COLS + [c for c in LEADER_COLS if c not in AREA] + [c for c in rem.columns
                                                                             if c not in REMAINING_COLS])
    for col in ("total_votos", "lider_votos", "segundo_votos", "corte_votos", "desafiante_votos", "margem_votos"):
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0).astype("int64")
    out["lider_pct"] = pd.to_numeric(out["lider_pct"], errors="coerce").fillna(0.0)
    return out


def decision_check(cands: pd.DataFrame, totals: pd.DataFrame, vagas: int = 1,
                   check_majority: bool = False) -> pd.DataFrame:
    """'O líder ainda pode ser alcançado?' por área.

    status:
    - "Decidido": margem > eleitores que faltam (nem com todos os restantes votando no desafiante dá);
    - "Decidido na prática": margem > validos_restantes_est (estimativa de válidos que ainda faltam);
    - "Em aberto": caso contrário; "Sem votos apurados" se a área não tem votos.
    Para vagas > 1 a disputa é entre o último eleito (corte) e o 1º de fora (desafiante). Comparar a margem com
    todos os válidos restantes é conservador (no Senado cada eleitor dá 2 votos, mas só 1 a cada candidato).

    check_majority=True (1º turno de Presidente/Governador) acrescenta 'maioria': o líder passa de 50% dos válidos
    mesmo se todos os válidos restantes forem para outros? ("Vence no 1º turno (na prática)"), não chega a 50%
    nem com todos ("2º turno (na prática)"), ou "Em aberto".
    """
    m = remaining_with_leaders(cands, totals, vagas)
    if m.empty:
        cols = list(m.columns) + ["status", "alcancavel", "folga_votos"] + (["maioria"] if check_majority else [])
        return _empty(cols)
    margem, faltam, rest = m["margem_votos"], m["faltam_eleitores"], m["validos_restantes_est"]
    sem = m["total_votos"] <= 0
    status = np.select([sem, margem > faltam, margem > rest], [ST_SEM_VOTOS, ST_DECIDIDO, ST_PRATICA], ST_ABERTO)
    m["status"] = status
    m["alcancavel"] = m["status"].isin([ST_ABERTO, ST_SEM_VOTOS])
    m["folga_votos"] = margem - rest  # > 0: margem já supera tudo o que (estima-se) falta
    if check_majority:
        v, tot = m["lider_votos"].astype(float), m["total_votos"].astype(float)
        final = tot + rest.astype(float)
        m["maioria"] = np.select(
            [sem, v > final / 2, v + rest <= final / 2],
            [ST_SEM_VOTOS, "Vence no 1º turno (na prática)", "2º turno (na prática)"], ST_ABERTO)
    return m


# ----------------------------------------------------------------------------- área residual

_SUM_COLS = ["eleitorado", "eleitorado_apurado", "validos", "secoes_total", "secoes_totalizadas", "comparecimento",
             "abstencao", "votos_total", "nominais", "legenda", "brancos", "nulos", "anulados"]


def add_residual_area(sub_totals: pd.DataFrame, sub_cands: pd.DataFrame, parent_totals: pd.DataFrame,
                      parent_cands: pd.DataFrame, mun: str = RESTO) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Completa áreas parciais (ex.: só alguns municípios já baixados) com uma área "resto do estado"
    = total do pai − soma dos filhos (valores negativos, por defasagem entre arquivos, viram 0).

    Devolve (totals, cands) com a linha/os candidatos extras (mun = RESTO). Se os filhos já cobrem todo o
    eleitorado do pai, devolve as entradas sem mudança.
    """
    if parent_totals is None or parent_totals.empty:
        return sub_totals, sub_cands
    p = _norm(parent_totals).iloc[0]
    s = _norm(sub_totals) if sub_totals is not None and not sub_totals.empty else _norm(pd.DataFrame(columns=AREA))
    res = {}
    for col in _SUM_COLS:
        pv = float(pd.to_numeric(pd.Series([p.get(col, 0)]), errors="coerce").fillna(0).iloc[0])
        res[col] = max(0.0, pv - _num(s, col).sum())
    if res["eleitorado"] <= 0:
        return sub_totals, sub_cands
    res["eleitorado_apurado"] = min(res["eleitorado_apurado"], res["eleitorado"])
    res["secoes_totalizadas"] = min(res["secoes_totalizadas"], res["secoes_total"])
    res["pct_secoes"] = 100 * res["secoes_totalizadas"] / res["secoes_total"] if res["secoes_total"] else 0.0
    row = {**{k: v for k, v in p.items() if k in s.columns or k in AREA}, **res, "uf": p["uf"], "mun": mun}
    new_totals = pd.concat([s, pd.DataFrame([row])], ignore_index=True)

    new_cands = sub_cands
    if parent_cands is not None and not parent_cands.empty:
        pc = _norm(parent_cands)
        pc = pc[pc["uf"] == p["uf"]] if (pc["uf"] == p["uf"]).any() else pc
        sc = _norm(sub_cands) if sub_cands is not None and not sub_cands.empty else None
        sub_sum = sc.groupby("sqcand")["votos"].sum() if sc is not None else pd.Series(dtype=float)
        rc = pc.drop_duplicates("sqcand").copy()
        rc["votos"] = (_num(rc, "votos") - rc["sqcand"].map(sub_sum).fillna(0)).clip(lower=0).round().astype("int64")
        rc["mun"] = mun
        new_cands = pd.concat([sc, rc], ignore_index=True) if sc is not None else rc.reset_index(drop=True)
    return new_totals, new_cands
