"""Tela: Votos restantes — onde estão os votos que faltam apurar, projeção ingênua e "o líder ainda pode ser
alcançado?". Cálculos em eleicoes.analysis; aqui só leitura do store + gráficos."""
from __future__ import annotations

import logging
from functools import lru_cache

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .. import analysis, config, geo, store
from .common import ViewContext, fmt_int, fmt_pct, no_data, party_color, uf_name

log = logging.getLogger(__name__)

CAVEAT = ("Estimativa ingênua: supõe que o que falta apurar em cada área vota como o que já foi apurado nela. "
          "Não é pesquisa nem previsão oficial.")
SINGLE = "#2a78d6"          # série única (sem líder: cargos proporcionais)
GRID = "rgba(128,128,128,0.18)"
OUTLINE_FILL = "rgba(140,140,140,0.12)"
OUTLINE_LINE = "rgba(140,140,140,0.55)"
NEUTRAL_MARK = "rgba(128,128,128,0.85)"
TOP_MUN = 20
TOP_CANDS = 8
STATUS_ORDER = {analysis.ST_ABERTO: 0, analysis.ST_SEM_VOTOS: 1, analysis.ST_PRATICA: 2, analysis.ST_DECIDIDO: 3}


def render(ctx: ViewContext) -> None:
    try:
        _render(ctx)
    except Exception:  # noqa: BLE001 — nunca mostrar traceback ao usuário (controle do Streamlit é BaseException)
        log.exception("restantes: falha ao montar a tela")
        st.warning("Não foi possível montar a tela de votos restantes agora; ela tenta de novo na próxima "
                   "atualização.")


# ----------------------------------------------------------------------------- utilidades

def fmt_compact(n) -> str:
    """1234567 -> '1,2 mi'; 35400 -> '35 mil'; 812 -> '812'."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "–"
    a = abs(n)
    if a >= 1e6:
        return f"{n / 1e6:.1f} mi".replace(".", ",")
    if a >= 1e4:
        return f"{n / 1e3:.0f} mil"
    if a >= 1e3:
        return f"{n / 1e3:.1f} mil".replace(".", ",")
    return fmt_int(round(n))


def _vagas(totals: pd.DataFrame, cargo: int) -> int:
    if "vagas" in totals.columns and not totals.empty:
        v = pd.to_numeric(totals["vagas"], errors="coerce").max()
        if pd.notna(v) and v >= 1:
            return int(v)
    return 2 if cargo == 5 else 1


def _base_layout(fig: go.Figure, height: int, legend: bool = True) -> go.Figure:
    fig.update_layout(
        height=height, margin=dict(l=0, r=10, t=36 if legend else 10, b=0), separators=",.",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0, xanchor="left", title_text=""),
        hoverlabel=dict(align="left"),
    )
    return fig


def _area_label(uf: str, mun: str, names: dict[str, str]) -> str:
    if mun == analysis.RESTO:
        return "Resto do estado (sem dados por município)"
    if mun:
        return names.get(mun, mun)
    return uf_name(uf)


def _leader_txt(r) -> str:
    if not isinstance(r.get("lider_nome"), str):
        return "sem votos apurados"
    return f"{r['lider_nome']} ({r['lider_partido']}) {fmt_pct(r['lider_pct'], 1)}"


def _hover(r, label: str) -> str:
    lines = [f"<b>{label}</b>",
             f"Faltam {fmt_int(r['faltam_eleitores'])} eleitores ({fmt_pct(100 - r['pct_secoes'], 1)} das seções)",
             f"Válidos restantes (est.): {fmt_int(r['validos_restantes_est'])}"]
    if "lider_nome" in r.index:
        lines.append(f"Lidera: {_leader_txt(r)}")
    return "<br>".join(lines)


def _kpis(rem: pd.DataFrame) -> None:
    el, ap = rem["eleitorado"].sum(), rem["eleitorado_apurado"].sum()
    if "secoes_total" in rem.columns and rem["secoes_total"].sum() > 0:
        pct = 100 * rem["secoes_totalizadas"].sum() / rem["secoes_total"].sum()
    else:
        pct = 100 * ap / el if el else 0.0
    c1, c2, c3 = st.columns(3)
    c1.metric("Eleitores ainda não apurados", fmt_compact(rem["faltam_eleitores"].sum()),
              help=f"{fmt_int(rem['faltam_eleitores'].sum())} de {fmt_int(el)} eleitores")
    c2.metric("Seções apuradas", fmt_pct(pct, 2))
    c3.metric("Votos válidos que faltam (estimativa)", fmt_compact(rem["validos_restantes_est"].sum()),
              help="Eleitores que faltam × (válidos ÷ eleitorado já apurado) em cada área.")


# ----------------------------------------------------------------------------- gráficos

@lru_cache(maxsize=1)
def _uf_centroids() -> dict[str, tuple[float, float]]:
    """(lon, lat) do centróide do maior polígono de cada UF, calculado do GeoJSON do IBGE."""
    out = {}
    for f in geo.states_geojson()["features"]:
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        best, best_a = None, -1.0
        for poly in polys:
            ring = poly[0]
            a = cx = cy = 0.0
            for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
                cr = x0 * y1 - x1 * y0
                a += cr
                cx += (x0 + x1) * cr
                cy += (y0 + y1) * cr
            if abs(a) > best_a and a != 0:
                best_a, best = abs(a), (cx / (3 * a), cy / (3 * a))
        if best:
            out[f["id"]] = best
    return out


def _bubble_map(rl: pd.DataFrame, by_leader: bool) -> go.Figure | None:
    cent = _uf_centroids()
    d = rl[rl["uf"].isin(cent.keys()) & (rl["faltam_eleitores"] > 0)].copy()
    if d.empty:
        return None
    d["lon"] = d["uf"].map(lambda u: cent[u][0])
    d["lat"] = d["uf"].map(lambda u: cent[u][1])
    d["hover"] = [_hover(r, uf_name(r["uf"])) for _, r in d.iterrows()]
    d["grupo"] = d["lider_partido"].fillna("Sem votos") if by_leader and "lider_partido" in d else "Todos"
    gj = geo.states_geojson()
    ufs = [f["id"] for f in gj["features"]]
    fig = go.Figure(go.Choropleth(
        geojson=gj, locations=ufs, z=[0] * len(ufs), colorscale=[[0, OUTLINE_FILL], [1, OUTLINE_FILL]],
        showscale=False, marker_line_color=OUTLINE_LINE, marker_line_width=0.7, hoverinfo="skip", showlegend=False))
    sizeref = 2.0 * d["faltam_eleitores"].max() / (48 ** 2)
    order = d.groupby("grupo")["faltam_eleitores"].sum().sort_values(ascending=False).index
    for g in order:
        grp = d[d["grupo"] == g].sort_values("faltam_eleitores", ascending=False)
        color = SINGLE if g == "Todos" else (config.NEUTRAL_COLOR if g == "Sem votos" else party_color(g))
        fig.add_trace(go.Scattergeo(
            lon=grp["lon"], lat=grp["lat"], mode="markers", name=g, showlegend=g != "Todos",
            marker=dict(size=grp["faltam_eleitores"], sizemode="area", sizeref=sizeref, sizemin=4, color=color,
                        opacity=0.85, line=dict(color="rgba(255,255,255,0.9)", width=1.5)),
            text=grp["hover"], hovertemplate="%{text}<extra></extra>"))
    fig.update_geos(fitbounds="locations", visible=False, projection_type="mercator", bgcolor="rgba(0,0,0,0)")
    _base_layout(fig, 520, legend=by_leader)
    fig.update_layout(margin=dict(l=0, r=0, t=36 if by_leader else 0, b=0), dragmode=False)
    return fig


def _bar_remaining(rl: pd.DataFrame, labels: pd.Series, by_leader: bool, n: int | None = None) -> go.Figure:
    d = rl.assign(label=labels.values).sort_values("faltam_eleitores", ascending=False)
    if n:
        d = d.head(n)
    d["hover"] = [_hover(r, r["label"]) for _, r in d.iterrows()]
    d["grupo"] = d["lider_partido"].fillna("Sem votos") if by_leader and "lider_partido" in d else "Todos"
    fig = go.Figure()
    order = d.groupby("grupo", sort=False)["faltam_eleitores"].sum().sort_values(ascending=False).index
    for g in order:
        grp = d[d["grupo"] == g]
        color = SINGLE if g == "Todos" else (config.NEUTRAL_COLOR if g == "Sem votos" else party_color(g))
        fig.add_trace(go.Bar(
            y=grp["label"], x=grp["faltam_eleitores"], orientation="h", name=g, marker_color=color,
            text=[fmt_compact(v) for v in grp["faltam_eleitores"]], textposition="outside", cliponaxis=False,
            hovertext=grp["hover"], hovertemplate="%{hovertext}<extra></extra>", showlegend=g != "Todos"))
    fig.update_yaxes(categoryorder="array", categoryarray=list(d["label"])[::-1], title=None)
    fig.update_xaxes(showgrid=True, gridcolor=GRID, zeroline=False, tickformat=",d", title=None,
                     range=[0, d["faltam_eleitores"].max() * 1.15 if len(d) else 1])
    _base_layout(fig, max(240, (20 if len(d) > 20 else 26) * len(d) + 80), legend=by_leader)
    fig.update_layout(barmode="overlay", bargap=0.3, barcornerradius=4)
    return fig


def _dumbbell(proj: pd.DataFrame, majority_line: bool) -> go.Figure:
    p = proj.head(TOP_CANDS).iloc[::-1].copy()
    p["label"] = p["nome_urna"].astype(str) + " (" + p["partido"].astype(str) + ")"
    colors = [party_color(x) for x in p["partido"]]
    fig = go.Figure()
    for (_, r), col in zip(p.iterrows(), colors):
        fig.add_trace(go.Scatter(x=[r["pct_atual"], r["pct_projetado"]], y=[r["label"], r["label"]], mode="lines",
                                 line=dict(color=col, width=2), hoverinfo="skip", showlegend=False))
    hover_a = [f"<b>{r['label']}</b><br>Atual: {fmt_pct(r['pct_atual'])} ({fmt_int(r['votos_atuais'])} votos)"
               for _, r in p.iterrows()]
    hover_p = [f"<b>{r['label']}</b><br>Projeção: {fmt_pct(r['pct_projetado'])} "
               f"({fmt_int(r['votos_projetados'])} votos)<br>Variação: {r['delta_pp']:+.2f} pp".replace(".", ",")
               for _, r in p.iterrows()]
    fig.add_trace(go.Scatter(x=p["pct_atual"], y=p["label"], mode="markers", name="Atual", showlegend=False,
                             marker=dict(symbol="circle-open", size=11, color=colors, line=dict(width=2)),
                             hovertext=hover_a, hovertemplate="%{hovertext}<extra></extra>"))
    fig.add_trace(go.Scatter(x=p["pct_projetado"], y=p["label"], mode="markers+text", name="Projeção",
                             showlegend=False, text=[fmt_pct(v, 1) for v in p["pct_projetado"]],
                             textposition="middle right",
                             marker=dict(symbol="circle", size=12, color=colors,
                                         line=dict(color="rgba(255,255,255,0.9)", width=2)),
                             hovertext=hover_p, hovertemplate="%{hovertext}<extra></extra>"))
    # Chaves de legenda neutras: a cor é do partido; o formato (vazado/cheio) diz atual × projeção.
    fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers", name="Atual (apurado)",
                             marker=dict(symbol="circle-open", size=11, color=NEUTRAL_MARK, line=dict(width=2))))
    fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers", name="Projeção ingênua",
                             marker=dict(symbol="circle", size=12, color=NEUTRAL_MARK)))
    xmax = float(max(p["pct_atual"].max(), p["pct_projetado"].max(), 1.0))
    if majority_line and xmax > 35:
        fig.add_vline(x=50, line_width=1, line_color="rgba(128,128,128,0.6)",
                      annotation_text="50% dos válidos", annotation_position="top")
        xmax = max(xmax, 52)
    fig.update_xaxes(range=[0, xmax * 1.15], ticksuffix="%", showgrid=True, gridcolor=GRID, zeroline=False,
                     title=None)
    fig.update_yaxes(title=None, showgrid=False)
    return _base_layout(fig, max(220, 44 * len(p) + 80))


# ----------------------------------------------------------------------------- blocos

def _projection_block(cands: pd.DataFrame, totals: pd.DataFrame, ctx: ViewContext, area_txt: str) -> None:
    st.subheader(f"Projeção ingênua — {ctx.cargo_nome}, {area_txt}")
    proj = analysis.project_final(cands, totals)
    if proj.empty or proj["votos_atuais"].sum() <= 0:
        no_data("A projeção aparece quando houver votos apurados.")
        return
    lider = proj.iloc[0]
    msg = f"Pela projeção ingênua, **{lider['nome_urna']} ({lider['partido']})** termina com " \
          f"**{fmt_pct(lider['pct_projetado'])}** dos válidos (agora: {fmt_pct(lider['pct_atual'])})."
    if ctx.turno == 1 and ctx.cargo in (1, 3) and len(proj) > 1:
        if lider["pct_projetado"] > 50:
            msg += " Isso seria vitória no 1º turno."
        else:
            seg = proj.iloc[1]
            msg += f" Haveria 2º turno contra **{seg['nome_urna']} ({seg['partido']})** " \
                   f"({fmt_pct(seg['pct_projetado'])})."
    st.markdown(msg)
    st.plotly_chart(_dumbbell(proj, majority_line=ctx.turno == 1 and ctx.cargo in (1, 3)),
                    key=f"restantes_proj_{ctx.uf}_{ctx.cargo}",
                    config={"displayModeBar": False})
    tab = pd.DataFrame({
        "Candidato": proj["nome_urna"], "Partido": proj["partido"],
        "Votos atuais": proj["votos_atuais"].map(fmt_int), "% atual": proj["pct_atual"].map(fmt_pct),
        "Votos projetados": proj["votos_projetados"].map(fmt_int),
        "% projetado": proj["pct_projetado"].map(fmt_pct),
        "Variação (pp)": proj["delta_pp"].map(lambda v: f"{v:+.2f}".replace(".", ",")),
    })
    with st.expander("Tabela da projeção"):
        st.dataframe(tab, hide_index=True)


def _decision_table(chk: pd.DataFrame, labels: pd.Series, vagas: int, majority: bool) -> pd.DataFrame:
    d = chk.assign(label=labels.values)
    d["_ord"] = d["status"].map(STATUS_ORDER).fillna(9)
    rest = d["validos_restantes_est"].where(d["validos_restantes_est"] > 0, 1)
    d["_ratio"] = d["margem_votos"] / rest
    d = d.sort_values(["_ord", "_ratio"])
    disputa = "Último eleito × 1º de fora" if vagas > 1 else "Líder × 2º"

    def nome(n, p):
        return f"{n} ({p})" if isinstance(n, str) else "–"

    out = pd.DataFrame({
        "Área": d["label"],
        "Seções apuradas": d["pct_secoes"].map(lambda v: fmt_pct(v, 1)),
        disputa: [f"{nome(a, b)} × {nome(c, e)}" for a, b, c, e in
                  zip(d["corte_nome"], d["corte_partido"], d["desafiante_nome"], d["desafiante_partido"])],
        "Margem (votos)": d["margem_votos"].map(fmt_int),
        "Válidos restantes (est.)": d["validos_restantes_est"].map(fmt_int),
        "Situação": d["status"],
    })
    if majority and "maioria" in d:
        out["1º turno?"] = d["maioria"]
    return out


def _status_line(chk_row, vagas: int, majority: bool) -> None:
    s = chk_row["status"]
    if s == analysis.ST_SEM_VOTOS:
        no_data("Ainda não há votos apurados para verificar se a liderança está decidida.")
        return
    quem = f"{chk_row['corte_nome']} ({chk_row['corte_partido']})"
    contra = (f"{chk_row['desafiante_nome']} ({chk_row['desafiante_partido']})"
              if isinstance(chk_row.get("desafiante_nome"), str) else "o próximo")
    alvo = "pela última vaga" if vagas > 1 else "pela liderança"
    txt = (f"**{s}** — {quem} tem {fmt_int(chk_row['margem_votos'])} votos de vantagem sobre {contra} {alvo}; "
           f"faltam ~{fmt_int(chk_row['validos_restantes_est'])} votos válidos "
           f"({fmt_int(chk_row['faltam_eleitores'])} eleitores).")
    if majority and "maioria" in chk_row.index and chk_row["maioria"] != analysis.ST_ABERTO:
        txt += f" Maioria absoluta: {chk_row['maioria'].lower()}."
    (st.success if s in (analysis.ST_DECIDIDO, analysis.ST_PRATICA) else st.info)(txt)


# ----------------------------------------------------------------------------- telas

def _render(ctx: ViewContext) -> None:
    st.subheader(f"Votos restantes — {ctx.cargo_nome}, {ctx.area_nome}")
    if ctx.is_brasil:
        _national(ctx)
    else:
        _state(ctx)


def _national(ctx: ViewContext) -> None:
    conn, ele, cargo = ctx.conn, ctx.eleicao, ctx.cargo
    tot = store.latest_totals(conn, ele, cargo, "uf")
    if tot.empty:
        no_data()
        return
    majoritario = cargo in config.CARGOS_MAJORITARIOS
    vagas = _vagas(tot, cargo)
    cands = store.latest_candidates(conn, ele, cargo, "uf") if majoritario else pd.DataFrame()
    rl = analysis.remaining_with_leaders(cands, tot, vagas) if majoritario else analysis.remaining_by_area(tot)
    _kpis(rl)
    st.caption(CAVEAT)
    if rl["eleitorado_apurado"].sum() <= 0:
        st.info("Nada apurado ainda: todo o eleitorado está em \"falta apurar\". Os gráficos mostram onde ele está.")

    labels = rl["uf"].map(uf_name)
    by_leader = majoritario
    lead_txt = " (cor = partido de quem lidera na UF)" if by_leader else ""
    c1, c2 = st.columns([1.1, 1])
    with c1:
        st.markdown(f"**Onde faltam eleitores**{lead_txt}")
        fig = _bubble_map(rl, by_leader)
        if fig is None:
            no_data("Apuração encerrada em todas as UFs.")
        else:
            st.plotly_chart(fig, key=f"restantes_map_{cargo}",
                            config={"displayModeBar": False})
            if (rl["uf"] == config.EXTERIOR).any():
                st.caption("O exterior não aparece no mapa; está no gráfico ao lado.")
    with c2:
        st.markdown(f"**Eleitores ainda não apurados por UF**{lead_txt}")
        st.plotly_chart(_bar_remaining(rl, labels, by_leader),
                        key=f"restantes_bar_{cargo}", config={"displayModeBar": False})

    if not majoritario:
        st.info("Cargo proporcional: a projeção por candidato não faz sentido (as vagas dependem de quociente "
                "eleitoral, sobras e votos de legenda). Mostramos só quanto falta apurar.")
        return
    if cargo == 1:
        tot_br = store.latest_totals(conn, ele, cargo, "br")
        cands_br = store.latest_candidates(conn, ele, cargo, "br")
        if not tot_br.empty and not cands_br.empty:
            st.subheader("O líder ainda pode ser alcançado?")
            chk = analysis.decision_check(cands_br, tot_br, 1, check_majority=ctx.turno == 1)
            if not chk.empty:
                _status_line(chk.iloc[0], 1, ctx.turno == 1)
        _projection_block(cands, tot, ctx, "Brasil")
        return

    st.subheader("O líder ainda pode ser alcançado?")
    st.caption("Compara a vantagem em votos com o que ainda falta. **Decidido**: a margem supera até o número de "
               "eleitores que faltam. **Decidido na prática**: supera a estimativa de válidos restantes. "
               + ("No Senado (2 vagas) a disputa relevante é entre o 2º e o 3º colocados." if vagas > 1 else ""))
    chk = analysis.decision_check(cands, tot, vagas, check_majority=ctx.turno == 1 and cargo == 3)
    if chk.empty:
        no_data()
        return
    counts = chk["status"].value_counts()
    st.markdown(" · ".join(f"**{counts.get(s, 0)}** {s.lower()}" for s in
                           (analysis.ST_DECIDIDO, analysis.ST_PRATICA, analysis.ST_ABERTO, analysis.ST_SEM_VOTOS)
                           if counts.get(s, 0)))
    st.dataframe(_decision_table(chk, chk["uf"].map(uf_name), vagas, ctx.turno == 1 and cargo == 3),
                 hide_index=True)


def _state(ctx: ViewContext) -> None:
    conn, ele, cargo, uf = ctx.conn, ctx.eleicao, ctx.cargo, ctx.uf
    try:
        store.add_focus(conn, uf)
    except Exception:  # noqa: BLE001 — banco somente leitura/ocupado: segue sem foco
        log.warning("restantes: não foi possível pôr %s em foco", uf)
    majoritario = cargo in config.CARGOS_MAJORITARIOS
    tot_uf = store.latest_totals(conn, ele, cargo, "uf", uf=uf)
    tot_mu = store.latest_totals(conn, ele, cargo, "mu", uf=uf)
    if tot_uf.empty and tot_mu.empty:
        no_data()
        return
    cands_uf = store.latest_candidates(conn, ele, cargo, "uf", uf=uf) if majoritario else pd.DataFrame()
    cands_mu = store.latest_candidates(conn, ele, cargo, "mu", uf=uf) if majoritario else pd.DataFrame()
    vagas = _vagas(tot_uf if not tot_uf.empty else tot_mu, cargo)
    try:
        mdf = geo.municipios_uf(uf)
        names = dict(zip(mdf["mun"], mdf["nome"]))
    except Exception:  # noqa: BLE001
        names = {}

    if tot_mu.empty:
        areas_tot, areas_cands = tot_uf, cands_uf
        mun_note = ("Os dados por município ainda não chegaram (o coletor baixa os municípios do estado em foco a "
                    "cada poucos minutos). Por enquanto, os números são do estado inteiro.")
    else:
        areas_tot, areas_cands = analysis.add_residual_area(tot_mu, cands_mu, tot_uf, cands_uf)
        n_all = len(names) or len(tot_mu)
        cob = tot_mu["eleitorado"].sum() / tot_uf["eleitorado"].sum() if not tot_uf.empty and \
            tot_uf["eleitorado"].sum() else 1.0
        mun_note = None if cob >= 0.995 else (
            f"Municípios com dados: {len(tot_mu)} de {n_all} ({fmt_pct(100 * cob, 1)} do eleitorado). "
            "O restante entra como \"Resto do estado\", calculado a partir do total da UF.")

    state_rem = analysis.remaining_by_area(tot_uf if not tot_uf.empty else tot_mu)
    _kpis(state_rem)
    st.caption(CAVEAT)
    if mun_note:
        st.caption(mun_note)

    rl = (analysis.remaining_with_leaders(areas_cands, areas_tot, vagas) if majoritario
          else analysis.remaining_by_area(areas_tot))
    if rl.empty:
        no_data()
        return
    labels = pd.Series([_area_label(u, m, names) for u, m in zip(rl["uf"], rl["mun"])], index=rl.index)
    if tot_mu.empty:
        st.markdown(f"**Eleitores ainda não apurados — {uf_name(uf)}**")
    else:
        lead_txt = " (cor = partido de quem lidera no município)" if majoritario else ""
        st.markdown(f"**Municípios com mais eleitores ainda não apurados** (top {TOP_MUN}){lead_txt}")
    if rl["faltam_eleitores"].sum() <= 0:
        st.success("Apuração encerrada: não falta apurar nenhum eleitor.")
    else:
        st.plotly_chart(_bar_remaining(rl, labels, majoritario, n=TOP_MUN),
                        key=f"restantes_mun_{uf}_{cargo}", config={"displayModeBar": False})

    if not majoritario:
        st.info("Cargo proporcional: a projeção por candidato não faz sentido (as vagas dependem de quociente "
                "eleitoral, sobras e votos de legenda). Mostramos só quanto falta apurar.")
        return

    if not tot_uf.empty and not cands_uf.empty:
        st.subheader("O líder ainda pode ser alcançado?")
        chk = analysis.decision_check(cands_uf, tot_uf, vagas, check_majority=ctx.turno == 1 and cargo in (1, 3))
        if not chk.empty:
            _status_line(chk.iloc[0], vagas, ctx.turno == 1 and cargo in (1, 3))

    _projection_block(areas_cands, areas_tot, ctx, uf_name(uf))
    if tot_mu.empty:
        st.caption("Sem dados por município, a projeção do estado é igual às porcentagens atuais.")
