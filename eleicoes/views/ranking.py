"""Tela: ranking da área (Brasil, UF ou município) com KPIs da apuração.

Também expõe funções reutilizadas pela tela de mapa e pelo cabeçalho do app:
`area_totals`, `kpi_row`, `area_ranking`, `candidate_bars`.
"""
from __future__ import annotations

import logging

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .. import config, geo, store
from .common import K_MUN, ViewContext, fmt_int, fmt_pct, no_data, party_color, uf_name

log = logging.getLogger(__name__)

GRID = "rgba(128,128,128,0.18)"
MUTED = "#8A8A85"
REF_LINE = "#8A8A85"
NO_PLOT_BAR = {"displayModeBar": False}


# ----------------------------------------------------------------------------- dados

def area_nivel(cargo: int, uf: str, mun: str | None) -> str:
    if mun:
        return "mu"
    if uf == config.BRASIL:
        return "br"
    return "uf"


def area_totals(conn, eleicao: str, cargo: int, uf: str, mun: str | None = None) -> pd.Series | None:
    """Totais da área (uma linha). Para uf='br' e cargos sem arquivo nacional, soma as UFs."""
    if uf == config.BRASIL and cargo != 1:
        df = store.latest_totals(conn, eleicao, cargo, "uf")
        df = df[df["uf"].isin(config.UF_LIST)]
        if df.empty:
            return None
        num = ["secoes_total", "secoes_totalizadas", "eleitorado", "eleitorado_apurado", "comparecimento",
               "abstencao", "votos_total", "validos", "nominais", "legenda", "brancos", "nulos", "anulados"]
        row = df[num].sum()
        row["pct_secoes"] = 100.0 * row["secoes_totalizadas"] / row["secoes_total"] if row["secoes_total"] else 0.0
        row["tse_ts"] = df["tse_ts"].max()
        row["vagas"] = df["vagas"].sum()
        row["finalizada"] = int(df["finalizada"].all())
        return row
    nivel = area_nivel(cargo, uf, mun)
    df = store.latest_totals(conn, eleicao, cargo, nivel, uf=uf, mun=mun if nivel == "mu" else None)
    if df.empty:
        return None
    return df.iloc[0]


def ensure_focus(conn, uf: str) -> None:
    """Coloca a UF em foco (o coletor passa a varrer os municípios dela). Melhor-esforço: só escreve se preciso."""
    if uf in (config.BRASIL, config.EXTERIOR):
        return
    try:
        if uf not in store.get_focus(conn):
            store.add_focus(conn, uf)
    except Exception:  # noqa: BLE001 - banco ocupado pelo coletor: tenta de novo na próxima atualização
        log.warning("add_focus(%s) falhou", uf, exc_info=True)


def _ratio(a, b) -> float | None:
    try:
        return 100.0 * float(a) / float(b) if float(b) else None
    except (TypeError, ValueError):
        return None


def mun_name(uf: str, mun: str | None) -> str:
    if not mun:
        return ""
    try:
        m = geo.municipios_uf(uf)
        hit = m.loc[m["mun"] == mun, "nome"]
        return str(hit.iloc[0]) if len(hit) else mun
    except Exception:  # noqa: BLE001 - nome é cosmético
        return mun


# ----------------------------------------------------------------------------- componentes

def kpi_row(t: pd.Series) -> None:
    """KPIs da apuração: seções, eleitores apurados, comparecimento, abstenção, brancos, nulos, hora do TSE."""
    cols = st.columns(7)
    ts = t.get("tse_ts")
    hora = ts.strftime("%H:%M:%S") if pd.notna(ts) else "–"
    data = ts.strftime("%d/%m") if pd.notna(ts) else ""
    cols[0].metric("Seções apuradas", fmt_pct(t["pct_secoes"]),
                   help=f"{fmt_int(t['secoes_totalizadas'])} de {fmt_int(t['secoes_total'])} seções")
    cols[1].metric("Eleitores apurados", fmt_int(t["eleitorado_apurado"]),
                   help=f"de {fmt_int(t['eleitorado'])} eleitores aptos")
    cols[2].metric("Comparecimento", fmt_pct(_ratio(t["comparecimento"], t["eleitorado_apurado"])),
                   help=f"{fmt_int(t['comparecimento'])} eleitores votaram (sobre os eleitores apurados)")
    cols[3].metric("Abstenção", fmt_pct(_ratio(t["abstencao"], t["eleitorado_apurado"])),
                   help=f"{fmt_int(t['abstencao'])} eleitores não votaram")
    cols[4].metric("Brancos", fmt_pct(_ratio(t["brancos"], t["votos_total"])),
                   help=f"{fmt_int(t['brancos'])} votos (sobre o total de votos)")
    cols[5].metric("Nulos", fmt_pct(_ratio(t["nulos"], t["votos_total"])),
                   help=f"{fmt_int(t['nulos'])} votos (sobre o total de votos)")
    cols[6].metric("Atualização do TSE", hora, help=f"Horário de Brasília, {data}" if data else None)


def _badge(situacao: str, eleito: bool) -> str:
    s = (situacao or "").strip()
    if eleito and not s:
        s = "Eleito"
    return s


def candidate_bars(cands: pd.DataFrame, cargo: int, vagas: int = 1, *, top: int | None = None) -> go.Figure:
    """Barras horizontais de % válidos por candidato (cor do partido), líder no topo."""
    df = cands.sort_values(["votos", "numero"], ascending=[False, True]).reset_index(drop=True)
    if top:
        df = df.head(top)
    labels, texts, hovers = [], [], []
    for i, r in df.iterrows():
        badge = _badge(r["situacao"], bool(r["eleito"]))
        lab = f"<b>{r['nome_urna']}</b>  <span style='color:{MUTED}'>{r['partido']}" + \
            ("</span>" if top else f" · {r['numero']}</span>")
        if badge:
            lab += f"  <b>[{badge}]</b>"
        labels.append(lab)
        texts.append(fmt_pct(r["pct_validos"]) if top else f"{fmt_pct(r['pct_validos'])}  ·  {fmt_int(r['votos'])}")
        h = (f"<b>{r['nome_urna']}</b> ({r['partido']} {r['numero']})<br>"
             f"{fmt_int(r['votos'])} votos · {fmt_pct(r['pct_validos'])} dos válidos")
        if r.get("vice"):
            h += f"<br>{'Vice' if cargo in (1, 3) else '1º suplente'}: {r['vice']}"
        if badge:
            h += f"<br>Situação: {badge}"
        hovers.append(h)
    n = len(df)
    xmax = max(55.0 if cargo in (1, 3) else 10.0, float(df["pct_validos"].max() or 0) * (1.25 if top else 1.5))
    fig = go.Figure(go.Bar(
        x=df["pct_validos"], y=list(range(n)), orientation="h",
        marker=dict(color=[party_color(p) for p in df["partido"]], cornerradius=4, line=dict(width=0)),
        text=texts, textposition="outside", cliponaxis=False,
        hovertext=hovers, hovertemplate="%{hovertext}<extra></extra>",
    ))
    fig.update_yaxes(tickvals=list(range(n)), ticktext=labels, autorange="reversed", showgrid=False,
                     ticks="", automargin=True)
    fig.update_xaxes(range=[0, xmax], ticksuffix="%", showgrid=True, gridcolor=GRID, zeroline=False,
                     title=None)
    if cargo in (1, 3):
        fig.add_vline(x=50, line_dash="dot", line_color=REF_LINE, line_width=1.5,
                      annotation_text="50% dos válidos", annotation_position="top",
                      annotation_font_color=REF_LINE, annotation_font_size=11)
    if cargo == 5 and 0 < vagas < n:
        fig.add_hline(y=vagas - 0.5, line_dash="dash", line_color=REF_LINE, line_width=1.5,
                      annotation_text=f"Linha de corte: {vagas} vaga{'s' if vagas > 1 else ''}",
                      annotation_position="bottom right", annotation_font_color=REF_LINE,
                      annotation_font_size=11)
    fig.update_layout(height=max(160, 34 * n + 70), bargap=0.4, margin=dict(l=0, r=10, t=28, b=10),
                      showlegend=False, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      hoverlabel=dict(align="left"))
    return fig


def party_bars(parties: pd.DataFrame, validos: int, top: int = 15,
               cands: pd.DataFrame | None = None) -> go.Figure | None:
    """Votos por partido (nominais + legenda), cor do partido; resto agrupado em 'Outros'.

    Se o arquivo não trouxer os nominais por partido (ex.: dados simulados), soma os votos dos candidatos.
    """
    if parties.empty and (cands is None or cands.empty):
        return None
    p = parties[["sigla", "federacao", "votos_nominais", "votos_legenda"]].copy()
    if cands is not None and not cands.empty and p["votos_nominais"].fillna(0).sum() <= 0:
        nom = cands.groupby("partido")["votos"].sum()
        p["votos_nominais"] = p["sigla"].map(nom).fillna(0)
        missing = nom[~nom.index.isin(p["sigla"])]
        if len(missing):
            p = pd.concat([p, pd.DataFrame({"sigla": missing.index, "federacao": "", "votos_nominais": missing.values,
                                            "votos_legenda": 0})], ignore_index=True)
    p = p.assign(votos=p["votos_nominais"].fillna(0) + p["votos_legenda"].fillna(0))
    p = p.groupby("sigla", as_index=False).agg(votos=("votos", "sum"), legenda=("votos_legenda", "sum"),
                                               federacao=("federacao", "first"))
    p = p[p["votos"] > 0].sort_values("votos", ascending=False)
    if p.empty:
        return None
    rest = p.iloc[top:]
    p = p.head(top)
    colors = [party_color(s) for s in p["sigla"]]
    if not rest.empty:
        p = pd.concat([p, pd.DataFrame([{"sigla": f"Outros ({len(rest)})", "votos": rest["votos"].sum(),
                                         "legenda": rest["legenda"].sum(), "federacao": ""}])])
        colors.append(config.NEUTRAL_COLOR)
    tot = validos or p["votos"].sum()
    pct = 100.0 * p["votos"] / tot if tot else p["votos"] * 0
    hov = [f"<b>{s}</b>{' (' + f + ')' if f else ''}<br>{fmt_int(v)} votos · {fmt_pct(x)} dos válidos"
           f"<br>dos quais {fmt_int(lg)} de legenda"
           for s, f, v, x, lg in zip(p["sigla"], p["federacao"], p["votos"], pct, p["legenda"])]
    n = len(p)
    fig = go.Figure(go.Bar(
        x=p["votos"], y=list(range(n)), orientation="h",
        marker=dict(color=colors, cornerradius=4, line=dict(width=0)),
        text=[fmt_pct(x, 1) for x in pct], textposition="outside", cliponaxis=False,
        hovertext=hov, hovertemplate="%{hovertext}<extra></extra>",
    ))
    fig.update_yaxes(tickvals=list(range(n)), ticktext=list(p["sigla"]), autorange="reversed", showgrid=False,
                     ticks="", automargin=True)
    fig.update_xaxes(showgrid=True, gridcolor=GRID, zeroline=False, range=[0, float(p["votos"].max()) * 1.25],
                     tickformat="~s")
    fig.update_layout(height=max(160, 26 * n + 50), bargap=0.35, margin=dict(l=0, r=10, t=10, b=10),
                      showlegend=False, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      hoverlabel=dict(align="left"))
    return fig


def _cand_table(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame({
        "#": df["rank"].astype(int),
        "Candidato": df["nome_urna"],
        "Nº": df["numero"],
        "Partido": df["partido"],
        "Federação": df["federacao"].fillna(""),
        "Votos": df["votos"].astype(int),
        "% válidos": df["pct_validos"].astype(float),
        "Situação": [_badge(s, e) for s, e in zip(df["situacao"], df["eleito"])],
    })
    return out


CAND_COLS = {
    "#": st.column_config.NumberColumn("#", width="small"),
    "Nº": st.column_config.TextColumn("Nº", width="small"),
    "Votos": st.column_config.NumberColumn("Votos", format="localized"),
    "% válidos": st.column_config.NumberColumn("% válidos", format="%.2f%%"),
}


def area_ranking(conn, eleicao: str, cargo: int, uf: str, mun: str | None = None, *, compact: bool = False,
                 key: str = "rank") -> bool:
    """Desenha o ranking de candidatos de uma área. Retorna False se não houver dados."""
    nivel = area_nivel(cargo, uf, mun)
    cands = store.latest_candidates(conn, eleicao, cargo, nivel, uf=uf, mun=mun if nivel == "mu" else None)
    if cands.empty:
        return False
    totals = area_totals(conn, eleicao, cargo, uf, mun)
    vagas = int(totals["vagas"]) if totals is not None and pd.notna(totals.get("vagas")) else 1
    if cargo in config.CARGOS_MAJORITARIOS:
        if cands["votos"].sum() <= 0:
            st.caption("Ainda sem votos apurados nesta área — candidatos:")
            t = _cand_table(cands)
            st.dataframe(t[["Candidato", "Nº", "Partido"]], hide_index=True, width="stretch",
                         column_config=CAND_COLS)
            return True
        st.plotly_chart(candidate_bars(cands, cargo, vagas, top=8 if compact else None), config=NO_PLOT_BAR,
                        key=f"{key}_bars")
        if not compact:
            with st.expander("Tabela"):
                st.dataframe(_cand_table(cands), hide_index=True, width="stretch", column_config=CAND_COLS)
        return True
    # proporcionais
    if compact:
        t = _cand_table(cands.head(15))
        st.caption(f"15 mais votados de {fmt_int(len(cands))} candidatos · {vagas} vagas")
        st.dataframe(t[["#", "Candidato", "Partido", "Votos", "% válidos"]], hide_index=True, width="stretch",
                     column_config=CAND_COLS, height=min(600, 36 * 16))
        return True
    _proportional(conn, eleicao, cargo, uf, mun, cands, totals, vagas, key)
    return True


def _proportional(conn, eleicao, cargo, uf, mun, cands, totals, vagas, key) -> None:
    area_key = f"{cargo}_{uf}_{mun or ''}"
    left, right = st.columns([3, 2], gap="large")
    with left:
        c1, c2, c3 = st.columns([2, 2, 1])
        q = c1.text_input("Buscar candidato", key=f"{key}_busca", placeholder="nome ou número")
        partidos = sorted(p for p in cands["partido"].dropna().unique() if p)
        sel = c2.multiselect("Partidos", partidos, key=f"{key}_partidos_{area_key}", placeholder="todos")
        todos = c3.toggle("Mostrar todos", key=f"{key}_todos")
        df = cands
        if q and q.strip():
            qq = q.strip().upper()
            df = df[(df["numero"] == qq) | df["nome_urna"].str.upper().str.contains(qq, regex=False)
                    | df["nome"].str.upper().str.contains(qq, regex=False)]
        if sel:
            df = df[df["partido"].isin(sel)]
        shown = df if todos else df.head(50)
        st.caption(f"Mostrando {fmt_int(len(shown))} de {fmt_int(len(df))} candidatos "
                   f"({fmt_int(len(cands))} no total) · {vagas} vagas")
        st.dataframe(_cand_table(shown), hide_index=True, width="stretch", column_config=CAND_COLS,
                     height=min(700, 35 * (len(shown) + 1) + 3))
    with right:
        st.markdown("**Votos por partido** (nominais + legenda)")
        parties = store.latest_parties(conn, eleicao, cargo, area_nivel(cargo, uf, mun), uf=uf)
        if mun:
            parties = parties[parties["mun"] == mun]
        validos = int(totals["validos"]) if totals is not None else 0
        fig = party_bars(parties, validos, cands=cands)
        if fig is None:
            st.caption("Sem votos por partido ainda.")
        else:
            st.plotly_chart(fig, config=NO_PLOT_BAR, key=f"{key}_partidos_chart")


# ----------------------------------------------------------------------------- tela

def _mun_picker(conn, ctx: ViewContext, uf: str) -> str | None:
    """Seletor opcional de município (só municípios já coletados). Sincroniza com session_state[K_MUN]."""
    try:
        muns = store.latest_totals(conn, ctx.eleicao, ctx.cargo, "mu", uf=uf)["mun"].tolist()
    except Exception:  # noqa: BLE001
        muns = []
    cur = ctx.mun if ctx.mun else None
    if not muns and not cur:
        return None
    names = dict(zip(geo.municipios_uf(uf)["mun"], geo.municipios_uf(uf)["nome"]))
    opts = [None] + sorted(set(muns) | ({cur} if cur else set()), key=lambda m: names.get(m, m))
    k = f"rank_mun_{uf}"
    if st.session_state.get(k) != cur:
        st.session_state[k] = cur

    def _sync() -> None:
        st.session_state[K_MUN] = st.session_state.get(k)

    st.selectbox("Município", opts, key=k, on_change=_sync,
                 format_func=lambda m: f"{uf_name(uf)} (estado inteiro)" if m is None else names.get(m, m),
                 help="Municípios aparecem aqui quando o coletor já os varreu (estado em foco).")
    return cur


def _render(ctx: ViewContext) -> None:
    conn, cargo = ctx.conn, ctx.cargo
    uf, mun = ctx.uf, ctx.mun
    if uf == config.BRASIL and cargo != 1:
        c1, c2 = st.columns([1, 3])
        uf = c1.selectbox("Estado", config.UF_LIST, index=config.UF_LIST.index("rs"), key="rank_uf_pick",
                          format_func=uf_name)
        c2.info(f"Para {ctx.cargo_nome} a apuração é por estado — escolha a UF ao lado "
                "(ou no menu lateral).")
        mun = None
    elif uf not in (config.BRASIL, config.EXTERIOR):
        ensure_focus(conn, uf)
        c1, _ = st.columns([1, 3])
        with c1:
            mun = _mun_picker(conn, ctx, uf)
    else:
        mun = None

    titulo = uf_name(uf) + (f" — {mun_name(uf, mun)}" if mun else "")
    totals = area_totals(conn, ctx.eleicao, cargo, uf, mun)
    if totals is None:
        no_data()
        return
    if uf != ctx.uf:  # br + cargo estadual: o cabeçalho do app diz "Brasil"; aqui vai a UF escolhida
        st.markdown(f"#### {ctx.cargo_nome} · {titulo}")
    kpi_row(totals)
    if not area_ranking(conn, ctx.eleicao, cargo, uf, mun, key="rank"):
        no_data()


def render(ctx: ViewContext) -> None:
    try:
        _render(ctx)
    except Exception as e:  # noqa: BLE001 - nunca mostrar traceback ao usuário
        log.exception("ranking")
        st.warning(f"Não foi possível montar o ranking agora ({type(e).__name__}). "
                   "Tentando de novo na próxima atualização.")
