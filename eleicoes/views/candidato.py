"""Tela: Candidato — onde cada candidato é forte (não só quem lidera).

Escolha do candidato (selectbox p/ cargos majoritários, busca p/ deputados), cartão com o resultado na área,
mapa da força do candidato (% dos válidos por UF ou por município), melhores municípios e concentração dos votos.
A lógica de montagem dos dados fica em funções puras (testadas em tests/test_view_candidato.py).
"""
from __future__ import annotations

import logging

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from . import brand
from .. import config, geo, store
from . import maps
from .common import K_CAND, ViewContext, fmt_int, fmt_pct, no_data, party_color, uf_name

log = logging.getLogger(__name__)

K_UF_PICK = "cand_uf_pick"   # UF escolhida dentro da tela quando a sidebar está em 'Brasil' (cargos estaduais)
K_BUSCA = "cand_busca"       # texto da busca de deputados
DEFAULT_UF = "rs"
MIN_VALIDOS_RANK_PCT = 500   # ranking por % só considera áreas com pelo menos isso de votos válidos apurados
TOP_N = 10

# Cores: rampa sequencial e "sem dados" vêm de brand.py (identidade do app).

STRENGTH_COLS = ["loc", "nome", "capital", "votos", "pct_validos", "rank", "n_cands", "validos", "pct_secoes"]


# ============================================================================ funções puras

def photo_url(eleicao: str, cargo: int, uf: str, sqcand: str) -> str:
    """URL da foto no TSE: <base>/<ciclo>/<eleição>/fotos/<uf>/<sqcand>.jpeg (Presidente usa a pasta 'br')."""
    folder = config.BRASIL if cargo == 1 else uf.lower()
    return f"{config.TSE_BASE}/{config.CICLO}/{eleicao}/fotos/{folder}/{sqcand}.jpeg"


def candidate_label(row: dict) -> str:
    return f"{row.get('nome_urna') or row.get('nome') or '?'} ({row.get('numero', '')} · {row.get('partido', '')})"


def candidate_options(cands: pd.DataFrame) -> tuple[list[str], dict[str, str]]:
    """Candidatos de UMA área -> (sqcands ordenados por votos desc / número, rótulos)."""
    if cands is None or cands.empty:
        return [], {}
    df = cands.drop_duplicates("sqcand").sort_values(["votos", "numero"], ascending=[False, True])
    opts = [str(s) for s in df["sqcand"]]
    return opts, {s: candidate_label(r) for s, r in zip(opts, df.to_dict("records"))}


def resolve_choice(current: str | None, options: list[str]) -> str | None:
    """Mantém a escolha se ela ainda existe na lista; senão cai no primeiro (mais votado)."""
    if current is not None and current in options:
        return current
    return options[0] if options else None


def search_options(results: pd.DataFrame, current: str | None, all_sqcands) -> list[str]:
    """Opções da busca de deputados: resultados + o candidato já escolhido (se for desta área) no topo."""
    opts = [] if results is None or results.empty else [str(s) for s in results["sqcand"].drop_duplicates()]
    if current and current in set(all_sqcands) and current not in opts:
        opts = [current] + opts
    return opts


def strength_table(cands: pd.DataFrame, sqcand: str, loc: str, validos: pd.DataFrame | None = None,
                   names: pd.DataFrame | None = None) -> pd.DataFrame:
    """Uma linha por área (loc='uf' ou 'mun') com o desempenho do candidato.

    cands: saída de store.latest_candidates para várias áreas (todos os candidatos, para ter rank e total).
    validos: opcional, colunas [loc, 'validos'] (store.latest_totals); senão usa a soma dos votos nominais.
    names: opcional, colunas [loc, 'nome', 'capital'] (geo.municipios_uf); p/ 'uf' o nome vem de uf_name.
    Colunas: STRENGTH_COLS (loc = código da área).
    """
    if cands is None or cands.empty or sqcand is None:
        return pd.DataFrame(columns=STRENGTH_COLS)
    g = cands.groupby(loc)
    area = pd.DataFrame({"validos": g["votos"].sum(), "n_cands": g["sqcand"].count()})
    mine = cands[cands["sqcand"].astype(str) == str(sqcand)].drop_duplicates(loc).set_index(loc)
    if mine.empty:
        return pd.DataFrame(columns=STRENGTH_COLS)
    out = mine[["votos", "pct_validos", "rank", "pct_secoes"]].join(area)
    if validos is not None and not validos.empty:
        v = validos.drop_duplicates(loc).set_index(loc)["validos"]
        v = v[v > 0]
        out.loc[out.index.isin(v.index), "validos"] = v.reindex(out.index).dropna()
    out = out.reset_index().rename(columns={loc: "loc"})
    if names is not None and not names.empty:
        nm = names.drop_duplicates(loc).set_index(loc)
        out["nome"] = out["loc"].map(nm["nome"]).fillna(out["loc"])
        out["capital"] = out["loc"].map(nm["capital"]).fillna(False).astype(bool) if "capital" in nm else False
    else:
        out["nome"] = out["loc"].map(uf_name) if loc == "uf" else out["loc"]
        out["capital"] = False
    for c in ("votos", "rank", "n_cands", "validos"):
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0).astype(int)
    out["pct_validos"] = pd.to_numeric(out["pct_validos"], errors="coerce").fillna(0.0)
    out["pct_secoes"] = pd.to_numeric(out["pct_secoes"], errors="coerce").fillna(0.0)
    return out[STRENGTH_COLS].sort_values("votos", ascending=False).reset_index(drop=True)


def hover_text(tab: pd.DataFrame) -> pd.Series:
    return pd.Series([
        f"<b>{r['nome']}</b><br>{fmt_pct(r['pct_validos'])} dos válidos<br>{fmt_int(r['votos'])} votos · "
        f"{int(r['rank'])}º de {int(r['n_cands'])}<br>{fmt_pct(r['pct_secoes'], 1)} das seções apuradas"
        for r in tab.to_dict("records")], index=tab.index, dtype=object)


def concentration(tab: pd.DataFrame) -> dict:
    """De onde vêm os votos do candidato: % vindo da capital, das 5 maiores áreas, e quantas áreas fazem metade."""
    if tab is None or tab.empty:
        return {}
    total = int(tab["votos"].sum())
    if total <= 0:
        return {}
    v = tab["votos"].sort_values(ascending=False)
    cum = v.cumsum()
    n_half = int((cum < total / 2).sum()) + 1
    cap = tab.loc[tab["capital"].astype(bool), "votos"].sum() if tab["capital"].astype(bool).any() else None
    return {
        "total": total,
        "top5_pct": 100.0 * v.head(5).sum() / total,
        "capital_pct": None if cap is None else 100.0 * cap / total,
        "n_half": n_half,
        "n_areas": int(len(tab)),
        "n_com_voto": int((tab["votos"] > 0).sum()),
    }


def top_areas(tab: pd.DataFrame, by: str, n: int = TOP_N, min_validos: int = 0) -> pd.DataFrame:
    """Top n áreas por 'votos' ou 'pct_validos' (no % exige um mínimo de válidos p/ evitar cidades minúsculas)."""
    if tab is None or tab.empty:
        return tab
    d = tab[tab["votos"] > 0]
    if by == "pct_validos" and min_validos:
        d = d[d["validos"] >= min_validos]
    return d.sort_values([by, "votos"], ascending=False).head(n).reset_index(drop=True)


def compare_rows(tab: pd.DataFrame | None, extra: list[tuple[str, float | None]] = ()) -> pd.DataFrame:
    """Barras de comparação: % do candidato na capital e no interior (se houver tabela municipal) + extras
    (ex. ('Rio Grande do Sul', 23.4), ('Brasil', 31.0)). Colunas: rotulo, pct. Linhas sem valor saem."""
    rows: list[tuple[str, float | None]] = []
    if tab is not None and not tab.empty and tab["capital"].astype(bool).any():
        cap, inter = tab[tab["capital"].astype(bool)], tab[~tab["capital"].astype(bool)]
        for label, d in (("Capital", cap), ("Interior", inter)):
            val = d["validos"].sum()
            rows.append((label, 100.0 * d["votos"].sum() / val if val > 0 else None))
    rows += list(extra)
    return pd.DataFrame([(lbl, float(p)) for lbl, p in rows if p is not None], columns=["rotulo", "pct"])


def vice_label(cargo: int) -> str | None:
    return {1: "Vice", 3: "Vice", 5: "1º suplente"}.get(cargo)


def party_line(row: dict) -> str:
    """'PT · Federação PT/PC do B/PV · Coligação PSB / PDT / ...' (só o que acrescenta informação)."""
    partido = row.get("partido") or ""
    parts = [partido] if partido else []
    fed = row.get("federacao") or ""
    if fed and fed != partido:
        parts.append(f"Federação {fed}")
    agr = row.get("agremiacao") or ""
    norm = agr.replace(" ", "").upper()
    if agr and norm not in (partido.replace(" ", "").upper(), fed.replace(" ", "").upper()):
        parts.append(f"Coligação {agr}")
    return " · ".join(parts)


# ============================================================================ dados (com cache)

def _db_path(conn) -> str:
    try:
        return conn.execute("PRAGMA database_list").fetchone()[2] or ""
    except Exception:
        return ""


@st.cache_data(ttl=60, show_spinner=False, max_entries=64)
def _mun_strength(db_path: str, eleicao: str, cargo: int, uf: str, sqcand: str) -> pd.DataFrame:
    """Tabela por município do candidato (consulta pesada p/ deputados: todas as linhas de todos os municípios)."""
    conn = store.connect(db_path, init=False)
    try:
        cands = store.latest_candidates(conn, eleicao, cargo, "mu", uf=uf)
        tot = store.latest_totals(conn, eleicao, cargo, "mu", uf=uf)
    finally:
        conn.close()
    cands = cands[cands["pct_secoes"] > 0]
    names = geo.municipios_uf(uf)[["mun", "nome", "capital"]]
    return strength_table(cands, sqcand, "mun", tot[["mun", "validos"]] if not tot.empty else None, names)


def _mun_strength_live(conn, eleicao: str, cargo: int, uf: str, sqcand: str) -> pd.DataFrame:
    path = _db_path(conn)
    if path:
        return _mun_strength(path, eleicao, cargo, uf, sqcand)
    cands = store.latest_candidates(conn, eleicao, cargo, "mu", uf=uf)
    cands = cands[cands["pct_secoes"] > 0]
    return strength_table(cands, sqcand, "mun", None, geo.municipios_uf(uf)[["mun", "nome", "capital"]])


# ============================================================================ figuras

def strength_map(tab: pd.DataFrame, geojson: dict, all_locs: list[str], height: int = 480) -> go.Figure:
    """Mapa sequencial do % do candidato; áreas ainda sem dados ficam cinza (camada de fundo)."""
    d = tab[tab["pct_secoes"] > 0].copy()
    d["hover"] = hover_text(d)
    zmax = max(1.0, float(d["pct_validos"].max())) if not d.empty else 1.0
    fig = maps.value_map(d, geojson, "pct_validos", loc="loc", hover_col="hover", colorscale=brand.sequential(), zmin=0,
                         zmax=zmax, colorbar_title="% válidos", height=height)
    missing = [x for x in all_locs if x not in set(d["loc"])]
    if missing:
        fig.add_trace(go.Choropleth(
            geojson=geojson, locations=missing, z=[0] * len(missing), colorscale=[[0, brand.tokens()["vazio"]], [1, brand.tokens()["vazio"]]],
            showscale=False, marker_line_color=brand.tokens()["tela"], marker_line_width=0.6, hoverinfo="skip"))
        fig.data = (fig.data[1], fig.data[0])
    fig.update_traces(colorbar_ticksuffix="%")
    return fig


def compare_bar(rows: pd.DataFrame) -> go.Figure:
    fig = go.Figure(go.Bar(
        x=rows["pct"], y=rows["rotulo"], orientation="h", marker_color=brand.tokens()["tinta"], width=0.55,
        text=[fmt_pct(p, 1) for p in rows["pct"]], textposition="outside", cliponaxis=False,
        hovertemplate="%{y}: %{text} dos válidos<extra></extra>"))
    fig.update_layout(height=60 + 38 * len(rows), margin=dict(l=0, r=40, t=8, b=8), barcornerradius=4,
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", showlegend=False,
                      xaxis=dict(visible=False, range=[0, max(5.0, float(rows["pct"].max()) * 1.2)]),
                      yaxis=dict(autorange="reversed", showgrid=False))
    return fig


# ============================================================================ UI

def _picker(label: str, options: list[str], labels: dict[str, str], wkey: str) -> str | None:
    cur = resolve_choice(st.session_state.get(K_CAND), options)
    if cur is None:
        return None
    if st.session_state.get(wkey) != cur:
        st.session_state[wkey] = cur

    def _sync() -> None:
        st.session_state[K_CAND] = st.session_state.get(wkey)

    choice = st.selectbox(label, options, format_func=lambda s: labels.get(s, s), key=wkey, on_change=_sync)
    st.session_state[K_CAND] = choice
    return choice


def _area_uf(ctx: ViewContext) -> str:
    if ctx.cargo == 1 or not ctx.is_brasil:
        return ctx.uf
    if st.session_state.get(K_UF_PICK) not in config.UF_LIST:
        st.session_state[K_UF_PICK] = DEFAULT_UF
    return st.selectbox("Estado", config.UF_LIST, format_func=uf_name, key=K_UF_PICK)


def _card(ctx: ViewContext, row: dict, area_uf: str, n_cands: int, foto: str | None) -> None:
    with st.container(border=True):
        cols = st.columns([1, 6]) if foto else [None, st.container()]
        if foto:
            with cols[0]:
                st.image(foto, width=110)
        with cols[1]:
            cor = party_color(row.get("partido"))
            st.markdown(f"### {row.get('nome_urna') or row.get('nome')} · {row.get('numero')}")
            st.markdown(f"<span style='color:{cor};font-size:1.1em'>&#9632;</span> {party_line(row)}",
                        unsafe_allow_html=True)
            vl = vice_label(ctx.cargo)
            if vl and row.get("vice"):
                st.caption(f"{vl}: {row['vice']}")
            if row.get("nome") and row.get("nome") != row.get("nome_urna"):
                st.caption(f"Nome completo: {row['nome']}")
        situ = row.get("situacao") or ("Eleito" if row.get("eleito") else "")
        if not situ:
            situ = "Em apuração" if float(row.get("pct_secoes") or 0) < 100 else "—"
        m = st.columns(4)
        m[0].metric("Votos", fmt_int(row.get("votos")))
        m[1].metric("% dos válidos", fmt_pct(row.get("pct_validos")))
        m[2].metric("Posição", f"{int(row.get('rank') or 0)}º de {n_cands}")
        m[3].metric("Situação", situ)
        st.caption(f"Resultado em {uf_name(area_uf)} · {fmt_pct(row.get('pct_secoes'), 2)} das seções apuradas")


def _top_tables(tab: pd.DataFrame, unidade: str) -> None:
    def fmt(d: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame({unidade: d["nome"], "Votos": [fmt_int(v) for v in d["votos"]],
                             "% válidos": [fmt_pct(p) for p in d["pct_validos"]],
                             "Posição": [f"{int(r)}º" for r in d["rank"]]})
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"**{unidade}s com mais votos**")
        st.dataframe(fmt(top_areas(tab, "votos")), hide_index=True, width="stretch")
    with c2:
        st.markdown(f"**{unidade}s com maior %**")
        by_pct = top_areas(tab, "pct_validos", min_validos=MIN_VALIDOS_RANK_PCT if unidade == "Município" else 0)
        st.dataframe(fmt(by_pct), hide_index=True, width="stretch")
        if unidade == "Município":
            st.caption(f"Só municípios com pelo menos {fmt_int(MIN_VALIDOS_RANK_PCT)} votos válidos apurados.")


def _concentration_tiles(conc: dict, municipal: bool) -> None:
    if not conc:
        return
    st.markdown("**Concentração dos votos**")
    c = st.columns(3)
    plural, top5 = ("municípios", "Vindos dos 5 municípios com mais votos") if municipal else \
        ("UFs", "Vindos das 5 UFs com mais votos")
    if municipal and conc.get("capital_pct") is not None:
        c[0].metric("Vindos da capital", fmt_pct(conc["capital_pct"], 1))
    else:
        c[0].metric(f"{'Municípios' if municipal else 'UFs'} com voto", f"{conc['n_com_voto']} de {conc['n_areas']}")
    c[1].metric(top5, fmt_pct(conc["top5_pct"], 1))
    n = conc["n_half"]
    c[2].metric("Metade dos votos vem de", f"{n} {plural if n > 1 else plural[:-1]}")


def _pick_candidate(ctx: ViewContext, area: pd.DataFrame, area_uf: str) -> str | None:
    wkey = f"cand_pick_{ctx.eleicao}_{ctx.cargo}_{area_uf}"
    if ctx.cargo in config.CARGOS_MAJORITARIOS:
        opts, labels = candidate_options(area)
        return _picker("Candidato", opts, labels, wkey)
    q = st.text_input("Buscar deputado(a) por nome ou número", key=K_BUSCA, placeholder="ex.: 1310 ou SILVA")
    if q.strip():
        res = store.search_candidates(ctx.conn, ctx.eleicao, ctx.cargo, q, uf=area_uf, limit=50)
    else:
        res = area.sort_values(["votos", "numero"], ascending=[False, True]).head(30)
        st.caption("Sem busca: mostrando os 30 mais votados do estado.")
    _, labels = candidate_options(area)
    opts = search_options(res, st.session_state.get(K_CAND), area["sqcand"].astype(str))
    if not opts:
        st.info(f"Nenhum candidato encontrado para “{q.strip()}” em {uf_name(area_uf)}.")
        return None
    return _picker("Candidato", opts, labels, wkey)


def _render(ctx: ViewContext) -> None:
    area_uf = _area_uf(ctx)
    is_state = area_uf in config.UFS
    if is_state:
        try:
            store.add_focus(ctx.conn, area_uf)   # o coletor passa a varrer os municípios desta UF
        except Exception:
            log.warning("add_focus falhou", exc_info=True)
    nivel = "br" if area_uf == config.BRASIL else "uf"
    area = store.latest_candidates(ctx.conn, ctx.eleicao, ctx.cargo, nivel, uf=None if nivel == "br" else area_uf)
    if area.empty:
        no_data()
        return
    area["sqcand"] = area["sqcand"].astype(str)

    sq = _pick_candidate(ctx, area, area_uf)
    if sq is None:
        return
    row = area[area["sqcand"] == sq].iloc[0].to_dict()
    foto = photo_url(ctx.eleicao, ctx.cargo, area_uf, sq)
    _card(ctx, row, area_uf, int(area["sqcand"].nunique()), foto)

    if float(row.get("pct_secoes") or 0) <= 0:
        no_data("A apuração ainda não começou nesta área: o mapa da força do candidato aparece quando houver votos.")
        return

    # Comparação nacional (Presidente numa UF)
    extra: list[tuple[str, float | None]] = []
    if ctx.cargo == 1 and nivel == "uf":
        br = store.latest_candidates(ctx.conn, ctx.eleicao, 1, "br")
        br = br[br["sqcand"].astype(str) == sq]
        extra = [(uf_name(area_uf), float(row["pct_validos"]))]
        if not br.empty:
            extra.append(("Brasil", float(br.iloc[0]["pct_validos"])))
    elif nivel == "uf":
        extra = [(uf_name(area_uf), float(row["pct_validos"]))]

    nome = row.get("nome_urna") or row.get("nome")
    if area_uf == config.BRASIL:   # Presidente, visão nacional: força por UF
        ufs = store.latest_candidates(ctx.conn, ctx.eleicao, 1, "uf")
        ufs = ufs[ufs["uf"].isin(config.UF_LIST)]
        tab = strength_table(ufs, sq, "uf")
        if tab.empty:
            no_data("Ainda não há resultados por UF.")
            return
        left, right = st.columns([3, 2])
        with left:
            st.markdown(f"**{nome}: % dos votos válidos por UF**")
            st.plotly_chart(strength_map(tab, geo.states_geojson(), config.UF_LIST), config={"displayModeBar": False})
        with right:
            rows = compare_rows(None, [(uf_name(r["loc"]), r["pct_validos"])
                                       for r in top_areas(tab, "pct_validos", n=5).to_dict("records")]
                                + [("Brasil", float(row["pct_validos"]))])
            st.markdown("**Melhores UFs × Brasil (% válidos)**")
            st.plotly_chart(compare_bar(rows), config={"displayModeBar": False})
            _concentration_tiles(concentration(tab), municipal=False)
        _top_tables(tab, "UF")
        st.caption("Escolha um estado na barra lateral para ver a força do candidato por município.")
        return

    if not is_state:   # exterior: sem mapa
        if extra:
            st.plotly_chart(compare_bar(compare_rows(None, extra)), config={"displayModeBar": False})
        st.caption("Votos no exterior: sem mapa por município.")
        return

    tab = _mun_strength_live(ctx.conn, ctx.eleicao, ctx.cargo, area_uf, sq)
    if tab.empty:
        if extra:
            st.plotly_chart(compare_bar(compare_rows(None, extra)), config={"displayModeBar": False})
        no_data(f"Ainda não há resultados por município em {uf_name(area_uf)}: o coletor varre os municípios "
                f"a cada ~3 min.")
        return
    muns = geo.municipios_uf(area_uf)
    left, right = st.columns([3, 2])
    with left:
        st.markdown(f"**{nome}: % dos votos válidos por município**")
        st.plotly_chart(strength_map(tab, geo.municipios_geojson(area_uf), muns["mun"].tolist()),
                        config={"displayModeBar": False})
        st.caption(f"{len(tab)} de {len(muns)} municípios com dados. Cinza: ainda sem dados.")
    with right:
        rows = compare_rows(tab, extra)
        if not rows.empty:
            st.markdown("**% dos válidos: capital × interior**")
            st.plotly_chart(compare_bar(rows), config={"displayModeBar": False})
        _concentration_tiles(concentration(tab), municipal=True)
    _top_tables(tab, "Município")


def render(ctx: ViewContext) -> None:
    try:
        _render(ctx)
    except Exception:  # nunca mostrar traceback ao usuário
        log.exception("tela candidato falhou")
        no_data("Não foi possível montar a tela do candidato agora; ela tenta de novo na próxima atualização.")
