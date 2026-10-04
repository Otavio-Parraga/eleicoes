"""Tela: mapa da apuração.

- Brasil: mapa das 27 UFs (cor do partido do líder, mais forte quanto maior a margem) ou % de seções apuradas.
  Clique numa UF para abri-la (seleção de pontos do plotly_chart).
- UF: mapa dos municípios (nível 'mu', coletado quando a UF está em foco) + ranking do estado.
  Clique num município para ver o ranking dele.
- Exterior ('zz'): só o ranking.
"""
from __future__ import annotations

import logging

import pandas as pd
import streamlit as st

from . import brand
from .. import config, geo, store
from . import maps
from .common import K_MUN, ViewContext, fmt_int, fmt_pct, no_data, party_color, select_uf, uf_name
from .ranking import area_ranking, area_totals, ensure_focus, mun_name

log = logging.getLogger(__name__)

MODES = ["Líder", "% apurado"]
# Escala sequencial (um matiz, claro -> escuro) para % de seções apuradas
K_NONCE = "_mapa_click_nonce"
NO_BAR = {"displayModeBar": False, "scrollZoom": False}


@st.cache_resource(show_spinner=False)
def _states_gj() -> dict:
    return geo.states_geojson()


@st.cache_resource(show_spinner="Carregando malha dos municípios…")
def _mun_gj(uf: str) -> dict:
    return geo.municipios_geojson(uf)


@st.cache_data(show_spinner=False)
def _mun_names(uf: str) -> pd.DataFrame:
    return geo.municipios_uf(uf)[["mun", "nome"]]


def clicked_location(event) -> str | None:
    """Extrai o 'location' do primeiro ponto selecionado no evento de seleção do st.plotly_chart."""
    try:
        sel = event["selection"] if isinstance(event, dict) or hasattr(event, "__getitem__") else None
        pts = sel["points"] if sel is not None else []
    except (KeyError, TypeError, AttributeError):
        try:
            pts = event.selection.points
        except AttributeError:
            return None
    for p in pts or []:
        loc = p.get("location") if hasattr(p, "get") else None
        if loc:
            return str(loc)
    return None


def _nonce() -> int:
    return int(st.session_state.get(K_NONCE, 0))


def _bump_nonce() -> None:
    st.session_state[K_NONCE] = _nonce() + 1


def _numeric(df: pd.DataFrame) -> pd.DataFrame:
    for c in ("pct_validos", "second_pct", "margin_pp", "pct_secoes", "votos"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _hover(df: pd.DataFrame, name_col: str) -> list[str]:
    out = []
    for r in df.itertuples(index=False):
        h = f"<b>{getattr(r, name_col)}</b>"
        if isinstance(r.nome_urna, str) and r.nome_urna:
            h += f"<br>1º {r.nome_urna} ({r.partido}) · {fmt_pct(r.pct_validos)}"
            if isinstance(r.second_nome_urna, str) and r.second_nome_urna:
                h += f"<br>2º {r.second_nome_urna} ({r.second_partido}) · {fmt_pct(r.second_pct)}"
                h += f"<br>Margem: {fmt_pct(r.margin_pp, 1).replace('%', ' p.p.')}"
            if isinstance(r.situacao, str) and r.situacao:
                h += f"<br>Situação: {r.situacao}"
        elif pd.notna(r.pct_secoes):
            h += "<br>Sem votos apurados ainda"
        else:
            h += "<br>Ainda não coletado"
        h += f"<br>Seções apuradas: {fmt_pct(r.pct_secoes) if pd.notna(r.pct_secoes) else '–'}"
        out.append(h)
    return out


def _legend(lead: pd.DataFrame, unidade: str) -> None:
    """Legenda do mapa por líder: cor do partido + candidato + nº de áreas lideradas (identidade nunca só por cor)."""
    led = lead.dropna(subset=["partido"])
    if led.empty:
        return
    g = (led.groupby(["partido", "nome_urna"]).size().reset_index(name="n")
         .sort_values("n", ascending=False))
    many = len(g) > 8
    if many:  # deputados: agrupa por partido
        g = led.groupby("partido").size().reset_index(name="n").sort_values("n", ascending=False)
        g["nome_urna"] = ""
    chips = []
    for r in g.head(10).itertuples(index=False):
        who = f"{r.nome_urna} ({r.partido})" if r.nome_urna else r.partido
        chips.append(
            f"<span style='white-space:nowrap;margin-right:14px'>"
            f"<span style='display:inline-block;width:11px;height:11px;border-radius:2px;margin-right:5px;"
            f"vertical-align:-1px;background:{party_color(r.partido)}'></span>{who} · {r.n} {unidade}</span>")
    if len(g) > 10:
        chips.append(f"<span style='opacity:.7'>+{len(g) - 10} outros</span>")
    st.markdown("<div style='font-size:0.85rem;line-height:1.9'>" + " ".join(chips) + "</div>",
                unsafe_allow_html=True)


def _figure(df: pd.DataFrame, gj: dict, loc: str, mode: str, height: int):
    if mode == "% apurado":
        return maps.value_map(df, gj, "pct_secoes", loc=loc, hover_col="hover", colorscale=brand.sequential(), zmin=0,
                              zmax=100, colorbar_title="% seções", height=height)
    return maps.leader_map(df, gj, loc=loc, hover_col="hover", height=height)


def _uf_table(lead: pd.DataFrame) -> None:
    t = pd.DataFrame({
        "UF": lead["uf"].str.upper(),
        "Líder": lead["nome_urna"].fillna("–"),
        "Partido": lead["partido"].fillna(""),
        "% válidos": lead["pct_validos"],
        "2º": lead["second_nome_urna"].fillna(""),
        "Margem (p.p.)": lead["margin_pp"],
        "Apurado": lead["pct_secoes"],
        "Situação": lead["situacao"].fillna(""),
    })
    st.dataframe(t, hide_index=True, width="stretch", height=min(990, 35 * (len(t) + 1) + 3), column_config={
        "% válidos": st.column_config.NumberColumn(format="%.2f%%"),
        "Margem (p.p.)": st.column_config.NumberColumn(format="%.1f"),
        "Apurado": st.column_config.ProgressColumn(format="%.1f%%", min_value=0, max_value=100),
    })


def _mode() -> str:
    return st.segmented_control("Colorir por", MODES, default=MODES[0], key="mapa_modo",
                                label_visibility="collapsed") or MODES[0]


def _render_brasil(ctx: ViewContext) -> None:
    lead = store.leaders(ctx.conn, ctx.eleicao, ctx.cargo, "uf")
    base = pd.DataFrame({"uf": config.UF_LIST})
    df = base.merge(lead, on="uf", how="left")
    df["nome_area"] = [f"{uf_name(u)} ({u.upper()})" for u in df["uf"]]
    df = _numeric(df)
    df["hover"] = _hover(df, "nome_area")

    left, right = st.columns([3, 2], gap="large")
    with left:
        mode = _mode()
        if lead.empty:
            st.caption("Sem dados por estado ainda — o mapa fica cinza até o TSE divulgar.")
        fig = _figure(df, _states_gj(), "uf", mode, height=620)
        event = st.plotly_chart(fig, on_select="rerun", selection_mode="points", config=NO_BAR,
                                key=f"mapa_br_{_nonce()}")
        if mode == MODES[0]:
            _legend(lead[lead["uf"].isin(config.UF_LIST)], "UFs")
        st.caption("Clique num estado para abri-lo. Cor = partido do líder; quanto mais forte, maior a margem.")
        loc = clicked_location(event)
        if loc in config.UFS:
            _bump_nonce()
            select_uf(loc)
    with right:
        if ctx.cargo == 1:
            st.markdown("**Brasil — ranking nacional**")
            if not area_ranking(ctx.conn, ctx.eleicao, 1, config.BRASIL, compact=True, key="mapa_rank"):
                no_data()
            zz = lead[lead["uf"] == config.EXTERIOR]
            if not zz.empty and isinstance(zz.iloc[0]["nome_urna"], str):
                r = zz.iloc[0]
                st.caption(f"Exterior: lidera {r['nome_urna']} ({r['partido']}) com {fmt_pct(r['pct_validos'])} "
                           f"· {fmt_pct(r['pct_secoes'])} apurado")
        else:
            st.markdown(f"**{ctx.cargo_nome} — líder em cada estado**")
            if lead.empty:
                no_data()
            else:
                ufs = lead[lead["uf"].isin(config.UF_LIST)].sort_values("uf")
                _uf_table(ufs)


def _render_uf(ctx: ViewContext) -> None:
    uf = ctx.uf
    ensure_focus(ctx.conn, uf)
    lead = store.leaders(ctx.conn, ctx.eleicao, ctx.cargo, "mu", uf=uf)
    names = _mun_names(uf)
    mun = ctx.mun if ctx.mun and ctx.mun in set(names["mun"]) else None

    left, right = st.columns([3, 2], gap="large")
    with left:
        mode = _mode()
        if lead.empty:
            st.info("Carregando municípios — o coletor varre os municípios deste estado a cada ~3 min.")
        else:
            df = _numeric(names.merge(lead.drop(columns=["uf"]), on="mun", how="left"))
            df["hover"] = _hover(df, "nome")
            fig = _figure(df, _mun_gj(uf), "mun", mode, height=640)
            event = st.plotly_chart(fig, on_select="rerun", selection_mode="points", config=NO_BAR,
                                    key=f"mapa_mu_{uf}_{_nonce()}")
            if mode == MODES[0]:
                _legend(lead, "mun.")
            n_ok = int((lead["pct_secoes"] > 0).sum())
            st.caption(f"{fmt_int(len(lead))} de {fmt_int(len(names))} municípios coletados · "
                       f"{fmt_int(n_ok)} com seções apuradas. Clique num município para ver o ranking dele.")
            if ctx.cargo in config.CARGOS_PROPORCIONAIS:
                st.caption("Para deputados, a cor mostra o partido do candidato mais votado em cada município.")
            loc = clicked_location(event)
            if loc and loc in set(names["mun"]) and loc != mun:
                _bump_nonce()
                st.session_state[K_MUN] = loc
                st.rerun(scope="fragment")
    with right:
        if mun:
            c1, c2 = st.columns([3, 2])
            c1.markdown(f"**{mun_name(uf, mun)}**")
            if c2.button("Voltar ao estado", key="mapa_voltar_uf", width="stretch"):
                st.session_state[K_MUN] = None
                _bump_nonce()
                st.rerun(scope="fragment")
            t = area_totals(ctx.conn, ctx.eleicao, ctx.cargo, uf, mun)
            if t is not None:
                st.caption(f"{fmt_pct(t['pct_secoes'])} das seções apuradas")
            if not area_ranking(ctx.conn, ctx.eleicao, ctx.cargo, uf, mun, compact=True, key="mapa_rank_mu"):
                no_data("Este município ainda não foi coletado.")
            st.divider()
        st.markdown(f"**{uf_name(uf)} — ranking estadual**")
        t = area_totals(ctx.conn, ctx.eleicao, ctx.cargo, uf)
        if t is not None:
            st.caption(f"{fmt_pct(t['pct_secoes'])} das seções apuradas")
        if not area_ranking(ctx.conn, ctx.eleicao, ctx.cargo, uf, compact=True, key="mapa_rank_uf"):
            no_data()


def _render_exterior(ctx: ViewContext) -> None:
    st.caption("Votos no exterior (só Presidente) — sem mapa.")
    if not area_ranking(ctx.conn, ctx.eleicao, ctx.cargo, config.EXTERIOR, key="mapa_rank_zz"):
        no_data()


def render(ctx: ViewContext) -> None:
    try:
        if ctx.uf == config.BRASIL:
            _render_brasil(ctx)
        elif ctx.uf == config.EXTERIOR:
            _render_exterior(ctx)
        else:
            _render_uf(ctx)
    except Exception as e:  # noqa: BLE001 - nunca mostrar traceback ao usuário
        log.exception("mapa")
        st.warning(f"Não foi possível montar o mapa agora ({type(e).__name__}). "
                   "Tentando de novo na próxima atualização.")
