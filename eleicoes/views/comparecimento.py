"""Tela: comparecimento — abstenção, brancos e nulos sobre a parte JÁ APURADA.

Taxas (sempre sobre o que já foi contado, nunca sobre o eleitorado total):
- abstenção %       = abstencao / eleitorado_apurado
- comparecimento %  = comparecimento / eleitorado_apurado
- brancos %, nulos % = brancos / votos_total, nulos / votos_total  (no Senado com 2 vagas cada eleitor dá 2 votos)
Áreas sem nada apurado ficam NaN (cinza no mapa), nunca 0.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .. import config, geo, store
from . import maps
from .common import ViewContext, fmt_int, fmt_pct, no_data, uf_name

log = logging.getLogger(__name__)

COUNT_COLS = ["secoes_total", "secoes_totalizadas", "eleitorado", "eleitorado_apurado", "comparecimento",
              "abstencao", "votos_total", "validos", "brancos", "nulos"]

# rótulo -> (coluna da taxa, texto do denominador)
METRICS: dict[str, tuple[str, str]] = {
    "Abstenção": ("abst_pct", "do eleitorado apurado"),
    "Brancos": ("brancos_pct", "dos votos apurados"),
    "Nulos": ("nulos_pct", "dos votos apurados"),
    "Brancos+Nulos": ("bn_pct", "dos votos apurados"),
}
METRIC_KEY = "comp_metric"
CARGOS_COMPARE = (1, 3, 5, 6, 7)

# Paleta (skill dataviz): rampa sequencial azul (claro -> escuro) e slots categóricos 1 e 2.
_BLUE_RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
_THEME = {
    "light": {"series1": "#2a78d6", "series2": "#eb6834", "nodata": "#d9d8d4", "line": "#ffffff"},
    "dark": {"series1": "#3987e5", "series2": "#d95926", "nodata": "#4a4a46", "line": "#1a1a19"},
}


# ----------------------------------------------------------------------------- funções puras

def _ratio(num, den) -> pd.Series | float:
    """100 * num / den, NaN onde den <= 0 (nada apurado)."""
    num = pd.to_numeric(num, errors="coerce")
    den = pd.to_numeric(den, errors="coerce")
    if np.ndim(num) == 0 and np.ndim(den) == 0:
        return float(100.0 * num / den) if den and den > 0 else float("nan")
    num = pd.Series(num, dtype="float64")
    den = pd.Series(den, dtype="float64").reindex(num.index) if isinstance(den, pd.Series) else den
    out = 100.0 * num / den
    return out.where(den > 0)


def add_rates(df: pd.DataFrame) -> pd.DataFrame:
    """Devolve cópia com abst_pct, comp_pct, brancos_pct, nulos_pct, bn_pct (0-100; NaN sem apuração)."""
    out = df.copy()
    if out.empty:
        for c in ("abst_pct", "comp_pct", "brancos_pct", "nulos_pct", "bn_pct"):
            out[c] = pd.Series(dtype="float64")
        return out
    for c in COUNT_COLS:
        if c not in out:
            out[c] = 0
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0)
    out["abst_pct"] = _ratio(out["abstencao"], out["eleitorado_apurado"])
    out["comp_pct"] = _ratio(out["comparecimento"], out["eleitorado_apurado"])
    out["brancos_pct"] = _ratio(out["brancos"], out["votos_total"])
    out["nulos_pct"] = _ratio(out["nulos"], out["votos_total"])
    out["bn_pct"] = _ratio(out["brancos"] + out["nulos"], out["votos_total"])
    return out


def aggregate(df: pd.DataFrame) -> dict:
    """Soma as contagens das linhas (ex.: UFs -> Brasil) e calcula as taxas. Dict vazio se df vazio."""
    if df is None or df.empty:
        return {}
    sums = {c: int(pd.to_numeric(df[c], errors="coerce").fillna(0).sum()) for c in COUNT_COLS if c in df}
    for c in COUNT_COLS:
        sums.setdefault(c, 0)
    sums["pct_secoes"] = _ratio(sums["secoes_totalizadas"], sums["secoes_total"])
    if np.isnan(sums["pct_secoes"]):
        sums["pct_secoes"] = 0.0
    row = add_rates(pd.DataFrame([sums])).iloc[0].to_dict()
    return row


def area_row(conn, eleicao: str, cargo: int, uf: str) -> dict:
    """Totais (com taxas) da área: 'br' usa a linha nacional se existir (só Presidente); senão soma as UFs."""
    if uf == config.BRASIL:
        br = store.latest_totals(conn, eleicao, cargo, "br")
        if not br.empty:
            return aggregate(br.head(1))
        ufs = store.latest_totals(conn, eleicao, cargo, "uf")
        return aggregate(ufs)
    return aggregate(store.latest_totals(conn, eleicao, cargo, "uf", uf=uf))


def rates_by_cargo(conn, turno: int, uf: str) -> pd.DataFrame:
    """Brancos % e nulos % por cargo na área. Colunas: cargo, cargo_nome, brancos_pct, nulos_pct, pct_secoes.

    Pula cargos que não existem no turno e cargos sem nada apurado.
    """
    rows = []
    for cargo in CARGOS_COMPARE:
        ele = config.ELECTIONS.get((turno, cargo))
        if ele is None:
            continue
        r = area_row(conn, ele, cargo, uf)
        if not r or not (r.get("votos_total", 0) > 0):
            continue
        rows.append({"cargo": cargo, "cargo_nome": config.UI_CARGOS.get(cargo, str(cargo)),
                     "brancos_pct": r["brancos_pct"], "nulos_pct": r["nulos_pct"], "pct_secoes": r["pct_secoes"]})
    return pd.DataFrame(rows, columns=["cargo", "cargo_nome", "brancos_pct", "nulos_pct", "pct_secoes"])


def top_bottom(df: pd.DataFrame, col: str, n: int = 10) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(maiores, menores) por `col`, ignorando NaN."""
    d = df[df[col].notna()]
    return (d.sort_values(col, ascending=False).head(n).reset_index(drop=True),
            d.sort_values(col, ascending=True).head(n).reset_index(drop=True))


def color_range(values: pd.Series) -> tuple[float, float]:
    """Faixa da escala: percentis 2-98 dos valores válidos (robusto a municípios minúsculos)."""
    v = pd.to_numeric(values, errors="coerce").dropna()
    if v.empty:
        return 0.0, 1.0
    lo, hi = float(v.quantile(0.02)), float(v.quantile(0.98))
    if hi - lo < 0.5:
        mid = (hi + lo) / 2
        lo, hi = max(0.0, mid - 0.5), mid + 0.5
    return lo, hi


# ----------------------------------------------------------------------------- figuras

def _theme() -> dict:
    try:
        t = st.context.theme.type
    except Exception:
        t = None
    return _THEME["dark" if t == "dark" else "light"]


def _hover(df: pd.DataFrame, name_col: str, col: str, label: str) -> pd.Series:
    out = []
    for name, v, sec in zip(df[name_col], df[col], df.get("pct_secoes", pd.Series([np.nan] * len(df)))):
        if pd.isna(v):
            out.append(f"<b>{name}</b><br>Nada apurado ainda")
        else:
            out.append(f"<b>{name}</b><br>{label}: {fmt_pct(v)}<br>Seções apuradas: {fmt_pct(sec)}")
    return pd.Series(out, index=df.index)


def _map(df: pd.DataFrame, geojson: dict, loc: str, col: str, label: str, height: int) -> go.Figure:
    """value_map com camada cinza por baixo para áreas sem apuração (NaN)."""
    th = _theme()
    valid = df[df[col].notna()].copy()
    have = set(valid[loc].astype(str))
    feats = geojson.get("features", [])
    # cada traço leva só as próprias feições: o GeoJSON vai uma vez só para o navegador
    gj_val = {"type": "FeatureCollection", "features": [f for f in feats if str(f.get("id")) in have]}
    gj_nod = {"type": "FeatureCollection", "features": [f for f in feats if str(f.get("id")) not in have]}
    lo, hi = color_range(valid[col])
    scale = [[i / (len(_BLUE_RAMP) - 1), c] for i, c in enumerate(_BLUE_RAMP)]
    fig = maps.value_map(valid, gj_val, col, loc=loc, hover_col="hover", colorscale=scale, zmin=lo, zmax=hi,
                         colorbar_title="", height=height)
    fig.update_traces(marker_line_color=th["line"], colorbar=dict(ticksuffix="%"))
    if gj_nod["features"]:
        ids = [f.get("id") for f in gj_nod["features"]]
        names = [(f.get("properties") or {}).get("nome") or f.get("id") for f in gj_nod["features"]]
        fig.add_trace(go.Choropleth(
            geojson=gj_nod, locations=ids, z=[0] * len(ids), zmin=0, zmax=1, showscale=False,
            colorscale=[[0, th["nodata"]], [1, th["nodata"]]], marker_line_color=th["line"], marker_line_width=0.6,
            text=[f"<b>{n}</b><br>Nada apurado ainda" for n in names], hovertemplate="%{text}<extra></extra>",
        ))
    return fig


def _bar_ufs(df: pd.DataFrame, col: str, label: str) -> go.Figure:
    th = _theme()
    d = df[df[col].notna()].sort_values(col, ascending=True)
    fig = go.Figure(go.Bar(
        x=d[col], y=d["uf"].str.upper(), orientation="h", marker_color=th["series1"],
        marker_line_width=0, text=[fmt_pct(v, 1) for v in d[col]], textposition="outside",
        customdata=np.stack([d["nome"], [fmt_pct(v) for v in d[col]], [fmt_pct(v) for v in d["pct_secoes"]]],
                            axis=-1) if len(d) else None,
        hovertemplate="<b>%{customdata[0]}</b><br>" + label + ": %{customdata[1]}<br>"
                      "Seções apuradas: %{customdata[2]}<extra></extra>",
    ))
    fig.update_layout(height=max(260, 22 * len(d) + 60), margin=dict(l=0, r=40, t=10, b=10), bargap=0.25,
                      xaxis=dict(title=None, ticksuffix="%", showgrid=True, zeroline=False),
                      yaxis=dict(title=None, tickfont=dict(size=11)), showlegend=False)
    return fig


def _bar_cargos(df: pd.DataFrame) -> go.Figure:
    th = _theme()
    fig = go.Figure()
    for col, name, color in (("brancos_pct", "Brancos", th["series1"]), ("nulos_pct", "Nulos", th["series2"])):
        fig.add_trace(go.Bar(
            x=df["cargo_nome"], y=df[col], name=name, marker_color=color, marker_line_width=0,
            text=[fmt_pct(v, 1) for v in df[col]], textposition="outside",
            customdata=np.stack([[fmt_pct(v) for v in df[col]], [fmt_pct(v) for v in df["pct_secoes"]]], axis=-1),
            hovertemplate="<b>%{x}</b><br>" + name + ": %{customdata[0]} dos votos<br>"
                          "Seções apuradas: %{customdata[1]}<extra></extra>",
        ))
    ymax = float(np.nanmax(df[["brancos_pct", "nulos_pct"]].to_numpy())) if len(df) else 1.0
    fig.update_layout(barmode="group", bargap=0.3, bargroupgap=0.08, height=340,
                      margin=dict(l=0, r=0, t=30, b=10),
                      yaxis=dict(title=None, ticksuffix="%", range=[0, ymax * 1.2 if ymax > 0 else 1]),
                      xaxis=dict(title=None),
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0))
    return fig


# ----------------------------------------------------------------------------- tela

def _kpis(row: dict, area: str) -> None:
    cols = st.columns(5)
    cols[0].metric("Abstenção", fmt_pct(row.get("abst_pct")),
                   help=f"{fmt_int(row.get('abstencao'))} de {fmt_int(row.get('eleitorado_apurado'))} eleitores "
                        f"das seções apuradas")
    cols[1].metric("Comparecimento", fmt_pct(row.get("comp_pct")), help=f"{fmt_int(row.get('comparecimento'))} eleitores")
    cols[2].metric("Brancos", fmt_pct(row.get("brancos_pct")),
                   help=f"{fmt_int(row.get('brancos'))} de {fmt_int(row.get('votos_total'))} votos apurados")
    cols[3].metric("Nulos", fmt_pct(row.get("nulos_pct")),
                   help=f"{fmt_int(row.get('nulos'))} de {fmt_int(row.get('votos_total'))} votos apurados")
    cols[4].metric("Seções apuradas", fmt_pct(row.get("pct_secoes")))
    st.caption(f"{area}: taxas calculadas só sobre as seções já apuradas (não sobre o eleitorado total).")


def _national(ctx: ViewContext, label: str, col: str, denom: str) -> None:
    ufs = store.latest_totals(ctx.conn, ctx.eleicao, ctx.cargo, "uf")
    row = area_row(ctx.conn, ctx.eleicao, ctx.cargo, config.BRASIL)
    if not row or not (row.get("eleitorado_apurado", 0) > 0):
        no_data()
        return
    _kpis(row, "Brasil" + (" (inclui exterior)" if ctx.cargo == 1 else ""))
    base = pd.DataFrame({"uf": config.UF_LIST})
    base["nome"] = base["uf"].map(uf_name)
    df = base.merge(add_rates(ufs[ufs["uf"].isin(config.UF_LIST)]), on="uf", how="left")
    df["hover"] = _hover(df, "nome", col, label)
    c1, c2 = st.columns([3, 2])
    with c1:
        st.markdown(f"**{label} por estado** — % {denom}")
        st.plotly_chart(_map(df, geo.states_geojson(), "uf", col, label, 540), width="stretch",
                        key="comp_map_br", config={"displayModeBar": False})
    with c2:
        st.markdown(f"**Ranking dos estados** — {label.lower()} %")
        if df[col].notna().any():
            st.plotly_chart(_bar_ufs(df, col, label), width="stretch", key="comp_bar_br",
                            config={"displayModeBar": False})
        else:
            st.caption("Nenhum estado com seções apuradas ainda.")
    st.caption("Cinza = nenhuma seção apurada ainda.")


def _municipal(ctx: ViewContext, label: str, col: str, denom: str) -> None:
    uf = ctx.uf
    try:
        store.add_focus(ctx.conn, uf)
    except Exception:  # banco ocupado: o coletor pega o foco na próxima vez
        log.warning("add_focus falhou", exc_info=True)
    row = area_row(ctx.conn, ctx.eleicao, ctx.cargo, uf)
    if not row or not (row.get("eleitorado_apurado", 0) > 0):
        no_data()
        return
    _kpis(row, uf_name(uf))
    mu = add_rates(store.latest_totals(ctx.conn, ctx.eleicao, ctx.cargo, "mu", uf=uf))
    pres = config.ELECTIONS.get((ctx.turno, 1))
    if (mu.empty or not mu[col].notna().any()) and col == "abst_pct" and ctx.cargo != 1 and pres:
        # abstenção é a mesma em todos os cargos: usa os municípios de Presidente se o cargo ainda não tem
        alt = add_rates(store.latest_totals(ctx.conn, pres, 1, "mu", uf=uf))
        if not alt.empty and alt[col].notna().any():
            mu = alt
            st.caption("Municípios: usando os arquivos de Presidente (mesmos eleitores, mesma abstenção).")
    if mu.empty or not mu[col].notna().any():
        st.info(f"Os dados por município de {uf_name(uf)} ainda não chegaram (o coletor busca os municípios "
                f"dos estados em foco a cada poucos minutos). Por enquanto, o total do estado: "
                f"**{label}: {fmt_pct(row.get(col))}** {denom}.")
        return
    names = geo.municipios_uf(uf)[["mun", "nome"]]
    df = names.merge(mu, on="mun", how="left")
    df["hover"] = _hover(df, "nome", col, label)
    n_cnt = int(df[col].notna().sum())
    c1, c2 = st.columns([3, 2])
    with c1:
        st.markdown(f"**{label} por município** — % {denom}")
        if uf in config.UFS:
            st.plotly_chart(_map(df, geo.municipios_geojson(uf), "mun", col, label, 560), width="stretch",
                            key="comp_map_mu", config={"displayModeBar": False})
        st.caption(f"{n_cnt} de {len(names)} municípios com seções apuradas. Cinza = nada apurado ainda.")
    with c2:
        top, bottom = top_bottom(df, col)
        st.markdown(f"**Maior {label.lower()}**")
        st.dataframe(_table(top, col, label), hide_index=True, width="stretch", key="comp_top")
        st.markdown(f"**Menor {label.lower()}**")
        st.dataframe(_table(bottom, col, label), hide_index=True, width="stretch", key="comp_bottom")


def _table(d: pd.DataFrame, col: str, label: str) -> pd.DataFrame:
    return pd.DataFrame({
        "Município": d["nome"],
        f"{label} %": [fmt_pct(v) for v in d[col]],
        "Seções apuradas": [fmt_pct(v, 0) for v in d["pct_secoes"]],
        "Eleitores apurados": [fmt_int(v) for v in d["eleitorado_apurado"]],
    })


def _por_cargo(ctx: ViewContext) -> None:
    st.markdown(f"#### Brancos e nulos por cargo — {uf_name(ctx.uf)}")
    df = rates_by_cargo(ctx.conn, ctx.turno, ctx.uf)
    if df.empty:
        st.caption("Ainda não há votos apurados para comparar os cargos.")
        return
    st.plotly_chart(_bar_cargos(df), width="stretch", key="comp_cargos", config={"displayModeBar": False})
    st.caption("% sobre o total de votos de cada cargo já apurados. No Senado, com 2 vagas, cada eleitor dá 2 votos. "
               "Brancos e nulos costumam ser bem maiores para Senado e Deputados do que para Presidente.")
    if ctx.uf == config.BRASIL:
        st.caption("Brasil: Presidente inclui o exterior; os demais cargos somam as 27 UFs.")


def render(ctx: ViewContext) -> None:
    try:
        label = st.segmented_control("Métrica", list(METRICS), default="Abstenção", key=METRIC_KEY,
                                     label_visibility="collapsed") or "Abstenção"
        col, denom = METRICS[label]
        if label == "Abstenção":
            st.caption("Os mesmos eleitores votam para todos os cargos, então abstenção e comparecimento são "
                       "praticamente iguais em qualquer cargo: este mapa não depende do cargo escolhido.")
    except Exception:
        log.exception("comparecimento: seletor")
        label, (col, denom) = "Abstenção", METRICS["Abstenção"]
    try:
        if ctx.uf == config.BRASIL:
            _national(ctx, label, col, denom)
        else:
            _municipal(ctx, label, col, denom)
    except Exception:
        log.exception("comparecimento: área")
        no_data("Não foi possível montar o comparecimento desta área agora. Tentando de novo na próxima atualização.")
    try:
        _por_cargo(ctx)
    except Exception:
        log.exception("comparecimento: por cargo")
        st.caption("Comparação por cargo indisponível no momento.")
