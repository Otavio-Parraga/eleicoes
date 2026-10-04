"""Tela: 2022 × 2026 — swing por partido e variação da abstenção, sobre a parte JÁ APURADA de 2026.

- Swing (pp) = % dos válidos do candidato do partido em 2026 − % do candidato do mesmo partido em 2022
  (mesmo turno). Compara PARTIDOS, não pessoas.
- Abstenção: % sobre o eleitorado das seções apuradas em 2026 − % de 2022 (todas as seções).
- Presidente sempre; Governador quando data/2022/gov_mun.parquet existe. Demais cargos usam Presidente.
- Dados de 2022: eleicoes.hist2022 (parquet gerados offline; esta tela nunca baixa nada).
"""
from __future__ import annotations

import logging
import math

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .. import config, geo, hist2022, store
from . import maps
from .common import ViewContext, fmt_int, fmt_pct, no_data, party_color, uf_name

log = logging.getLogger(__name__)

K_PARTY, K_MODE = "cmp22_partido", "cmp22_modo"
MODES = ["Mapa do swing", "Dispersão 2022 × 2026", "Abstenção", "Tabela"]
DEFAULT_PARTIES = ("PT", "PL")

# Divergente (skill dataviz): dois matizes + cinza neutro no meio, mesmo nº de passos em cada braço.
# Marrom (perdeu) <-> verde-azulado (ganhou): evita vermelho/azul, que aqui seriam lidos como PT/PL.
_DIV = {
    "light": ["#8c510a", "#bf812d", "#dfc27d", "#f0efec", "#80cdc1", "#35978f", "#01665e"],
    "dark": ["#e3a04f", "#a87132", "#6b4e2a", "#383835", "#2a5f59", "#3a9a8f", "#6fd3c4"],
}
_THEME = {
    "light": {"line": "#ffffff", "nodata_line": "#b5b4ae", "muted": "#52514e", "ring": "#fcfcfb"},
    "dark": {"line": "#1a1a19", "nodata_line": "#5a5a55", "muted": "#c3c2b7", "ring": "#1a1a19"},
}
HELP_CMD = ("conda run -n python3 python -m eleicoes.hist2022 download && "
            "conda run -n python3 python -m eleicoes.hist2022 build")


# ----------------------------------------------------------------------------- utilidades

def _mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:
        return "light"


def _scale(reverse: bool = False) -> list:
    cols = _DIV[_mode()]
    cols = cols[::-1] if reverse else cols
    return [[i / (len(cols) - 1), c] for i, c in enumerate(cols)]


def fmt_pp(x, nd: int = 1) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "–"
    if math.isnan(x):
        return "–"
    return f"{x:+.{nd}f} pp".replace(".", ",")  # hífen ASCII: st.metric usa o sinal p/ a seta


def sym_range(values: pd.Series, floor: float = 2.0) -> float:
    """Limite simétrico da escala divergente: ~98º percentil de |v| (outliers não lavam o resto)."""
    v = pd.to_numeric(values, errors="coerce").abs().dropna()
    if v.empty:
        return floor
    return max(floor, float(math.ceil(v.quantile(0.98))))


def _hist_key() -> str:
    """Chave de cache dos dados de 2022: diretório + mtime mais recente (muda quando o build roda de novo)."""
    d = hist2022.hist_dir()
    try:
        m = max(p.stat().st_mtime for p in d.glob("*.parquet"))
    except ValueError:
        m = 0.0
    return f"{d}|{m}"


@st.cache_data(ttl=3600, show_spinner=False, max_entries=64)
def _share22(cargo: int, level: str, turno: int, uf: str | None, src: str) -> pd.DataFrame:
    return hist2022.party_share(level, turno, uf, cargo=cargo)


@st.cache_data(ttl=3600, show_spinner=False, max_entries=64)
def _turnout22(level: str, turno: int, cargo: int, uf: str | None, src: str) -> pd.DataFrame:
    return hist2022.turnout(level, turno, cargo=cargo, uf=uf)


@st.cache_data(ttl=3600, show_spinner=False)
def _status(src: str) -> dict:
    return hist2022.status()


def _area_names(level: str, uf: str | None) -> dict[str, str]:
    if level == "mu" and uf:
        m = geo.municipios_uf(uf)
        return dict(zip(m["mun"], m["nome"]))
    return {u: uf_name(u) for u in [*config.UF_LIST, config.EXTERIOR, config.BRASIL]}


# ----------------------------------------------------------------------------- telas sem dados

def _missing_2022() -> None:
    st.warning("Os resultados de 2022 ainda não foram processados neste computador.")
    st.markdown("Rode, na raiz do projeto (baixa ~650 MB do portal de dados abertos do TSE e gera arquivos "
                "parquet compactos em `data/2022/`; leva poucos minutos):")
    st.code(HELP_CMD, language="bash")
    try:
        s = hist2022.status()
        rows = [{"arquivo": f"{n}.zip", "situação": "—" if b is None else f"{b / 1e6:.1f} MB"}
                for n, b in s["zips"].items()]
        rows += [{"arquivo": f"{n}.parquet", "situação": "—" if r is None else f"{fmt_int(r)} linhas"}
                 for n, r in s["parquet"].items()]
        st.dataframe(pd.DataFrame(rows), hide_index=True)
        st.caption(f"Diretório: {s['dir']}")
    except Exception:  # noqa: BLE001
        log.exception("hist2022.status falhou")


# ----------------------------------------------------------------------------- figuras

def _hover_swing(df: pd.DataFrame, partido: str) -> pd.Series:
    out = []
    for r in df.itertuples():
        out.append(
            f"<b>{r.nome}</b><br>{partido} 2022 ({r.nm_urna_2022}): {fmt_pct(r.pct_2022)}"
            f"<br>{partido} 2026 ({r.nm_urna_2026}): {fmt_pct(r.pct_2026)}"
            f"<br>Swing: <b>{fmt_pp(r.swing_pp)}</b><br>Seções apuradas 2026: {fmt_pct(r.pct_secoes)}")
    return pd.Series(out, index=df.index, dtype=object)


def _hover_abst(df: pd.DataFrame) -> pd.Series:
    out = []
    for r in df.itertuples():
        out.append(f"<b>{r.nome}</b><br>Abstenção 2022: {fmt_pct(r.abst_2022)}"
                   f"<br>Abstenção 2026 (apurado): {fmt_pct(r.abst_2026)}"
                   f"<br>Variação: <b>{fmt_pp(r.delta_pp)}</b><br>Seções apuradas 2026: {fmt_pct(r.pct_secoes)}")
    return pd.Series(out, index=df.index, dtype=object)


def diverging_map(df: pd.DataFrame, geojson: dict, loc: str, col: str, title: str, *,
                  reverse: bool = False, height: int = 560) -> go.Figure:
    """Mapa divergente centrado em 0. Áreas sem apuração em 2026 ficam só com o contorno (sem cor)."""
    th = _THEME[_mode()]
    lim = sym_range(df[col])
    fig = maps.value_map(df, geojson, col, loc=loc, hover_col="hover", colorscale=_scale(reverse),
                         zmin=-lim, zmax=lim, zmid=0, colorbar_title=title, height=height)
    fig.update_traces(marker_line_color=th["line"], colorbar=dict(ticksuffix=" pp"))
    ids = [f.get("id") for f in geojson.get("features", [])]
    have = set(df[loc])
    rest = [i for i in ids if i not in have]
    names = {f.get("id"): (f.get("properties") or {}).get("nome", "") for f in geojson.get("features", [])}
    if rest:
        base = go.Choropleth(
            geojson=geojson, locations=rest, z=[0] * len(rest), zmin=0, zmax=1, showscale=False,
            colorscale=[[0, "rgba(0,0,0,0)"], [1, "rgba(0,0,0,0)"]], marker_line_color=th["nodata_line"],
            marker_line_width=0.5, text=[f"<b>{names.get(i) or i}</b><br>Nada apurado em 2026" for i in rest],
            hovertemplate="%{text}<extra></extra>")
        fig.add_trace(base)
        fig.data = (fig.data[1], fig.data[0])
    return fig


def scatter_fig(df: pd.DataFrame, partido: str, label_top: int = 6, height: int = 520) -> go.Figure:
    """x = % 2022, y = % 2026 por área, com a diagonal y = x (acima dela o partido cresceu)."""
    th = _THEME[_mode()]
    hi = max(5.0, float(df[["pct_2022", "pct_2026"]].max().max()) if len(df) else 5.0)
    hi = min(100.0, math.ceil(hi / 5) * 5 + 5)
    many = len(df) > 60
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=[0, hi], y=[0, hi], mode="lines", hoverinfo="skip", showlegend=False,
                             line=dict(color=th["muted"], width=1, dash="dash")))
    text = [""] * len(df)
    if not many and len(df):
        top = df["swing_pp"].abs().nlargest(label_top).index
        text = [str(r.loc_label) if i in top else "" for i, r in zip(df.index, df.itertuples())]
    fig.add_trace(go.Scatter(
        x=df["pct_2022"], y=df["pct_2026"], mode="markers+text" if not many else "markers",
        text=text, textposition="top center", textfont=dict(size=11, color=th["muted"]),
        marker=dict(size=7 if many else 10, color=party_color(partido), opacity=0.85 if many else 1,
                    line=dict(color=th["ring"], width=1.5 if not many else 1)),
        hovertext=df["hover"], hovertemplate="%{hovertext}<extra></extra>", showlegend=False))
    fig.add_annotation(x=0.02 * hi, y=0.97 * hi, xanchor="left", yanchor="top", showarrow=False,
                       text=f"acima da diagonal: {partido} cresceu", font=dict(size=11, color=th["muted"]))
    fig.add_annotation(x=0.98 * hi, y=0.03 * hi, xanchor="right", yanchor="bottom", showarrow=False,
                       text=f"abaixo: {partido} caiu", font=dict(size=11, color=th["muted"]))
    axis = dict(range=[0, hi], ticksuffix="%", zeroline=False, gridcolor="rgba(128,128,128,0.15)")
    fig.update_layout(height=height, margin=dict(l=10, r=10, t=10, b=10), paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)",
                      xaxis=dict(title="2022 (% dos válidos)", **axis),
                      yaxis=dict(title="2026 (% dos válidos, parte apurada)", scaleanchor="x", scaleratio=1,
                                 **axis))
    return fig


# ----------------------------------------------------------------------------- dados

def _level_ctx(ctx: ViewContext) -> tuple[str, str | None, str, dict | None]:
    """(nível p/ mapa, uf p/ filtro, coluna de localização, geojson ou None)."""
    if ctx.is_brasil:
        return "uf", None, "uf", geo.states_geojson()
    if ctx.uf == config.EXTERIOR:
        return "mu", ctx.uf, "mun", None
    return "mu", ctx.uf, "mun", geo.municipios_geojson(ctx.uf)


def _cargo_cmp(ctx: ViewContext, has_gov: bool) -> int:
    return 3 if ctx.cargo == 3 and has_gov and (ctx.turno, 3) in config.ELECTIONS else 1


def _headline(ctx: ViewContext, cargo: int, partido: str, eleicao: str, mtime: str) -> bool:
    """Métricas do partido e da abstenção na área inteira (Brasil ou UF). True se havia dados de 2026."""
    if ctx.is_brasil:
        if cargo != 1:
            return False
        c26 = store.latest_candidates(ctx.conn, eleicao, 1, "br")
        t26 = store.latest_totals(ctx.conn, eleicao, 1, "br")
        s22 = _share22(1, "br", ctx.turno, None, mtime)
        a22 = _turnout22("br", ctx.turno, 1, None, mtime)
    else:
        c26 = store.latest_candidates(ctx.conn, eleicao, cargo, "uf", ctx.uf)
        t26 = store.latest_totals(ctx.conn, eleicao, cargo, "uf", ctx.uf)
        s22 = _share22(cargo, "uf", ctx.turno, ctx.uf, mtime)
        a22 = _turnout22("uf", ctx.turno, cargo, ctx.uf, mtime)
    keys = ["uf", "mun"]
    s26 = hist2022.share_2026(c26, keys)
    sw = hist2022.compute_swing(s26, s22, partido, keys, require_both=(cargo != 1))
    ab = hist2022.compute_turnout_change(hist2022.turnout_2026(t26, keys), a22, keys)
    if sw.empty and ab.empty:
        return False
    cols = st.columns(4)
    if len(sw):
        r = sw.iloc[0]
        cols[0].metric(f"{partido} 2026 · {r['nm_urna_2026']}", fmt_pct(r["pct_2026"]),
                       fmt_pp(r["swing_pp"]) + " vs 2022", delta_color="off",
                       help="% dos válidos na parte apurada de 2026; variação em pontos percentuais.")
        cols[1].metric(f"{partido} 2022 · {r['nm_urna_2022']}", fmt_pct(r["pct_2022"]))
        cols[3].metric("Seções apuradas 2026", fmt_pct(r["pct_secoes"]))
    if len(ab):
        a = ab.iloc[0]
        cols[2].metric("Abstenção 2026 (apurado)", fmt_pct(a["abst_2026"]),
                       fmt_pp(a["delta_pp"]) + f" vs 2022 ({fmt_pct(a['abst_2022'])})", delta_color="off")
    return True


def _render(ctx: ViewContext) -> None:
    mtime = _hist_key()
    stt = _status(mtime)
    if not stt["ready"]:
        _missing_2022()
        return
    has_gov = bool(stt["parquet"].get("gov_mun"))
    cargo = _cargo_cmp(ctx, has_gov)
    cargo_nome = config.CARGOS[cargo]
    eleicao = config.ELECTIONS[(ctx.turno, cargo)]
    level, uf, loc, gj = _level_ctx(ctx)
    if level == "mu" and uf != config.EXTERIOR:
        try:
            store.add_focus(ctx.conn, uf)
        except Exception:  # noqa: BLE001 — banco só-leitura etc.: o mapa fica sem municípios
            log.exception("add_focus falhou")

    st.subheader(f"2022 × 2026 · {cargo_nome}, {ctx.turno}º turno · {ctx.area_nome}")
    if cargo != ctx.cargo:
        st.info(f"A comparação com 2022 está disponível para Presidente"
                f"{' e Governador' if has_gov else ''}; mostrando **Presidente** "
                f"(cargo selecionado: {ctx.cargo_nome}).")
    if cargo == 3 and ctx.is_brasil:
        st.caption("Governador: cada estado tem a sua disputa; o mapa compara o candidato do partido em cada "
                   "UF. UFs onde o partido não teve candidato em 2022 ou em 2026 ficam de fora.")
    st.caption("Compara **partidos**, não pessoas: em 2022 o PT teve Lula e o PL, Bolsonaro; em 2026 os "
               "candidatos podem ser outros. Valores de 2026 são da parte já apurada — no início da contagem "
               "eles ainda se movem bastante.")

    # ---- dados 2026 por área do mapa
    keys = ["uf", "mun"]
    nivel26 = "uf" if level == "uf" else "mu"
    c26 = store.latest_candidates(ctx.conn, eleicao, cargo, nivel26, uf)
    if level == "uf":
        c26 = c26[c26["uf"].isin(config.UF_LIST)]
    s26 = hist2022.share_2026(c26, keys)
    s22 = _share22(cargo, "uf" if level == "uf" else "mu", ctx.turno, uf, mtime)

    parties26 = set(s26["partido"]) if len(s26) else set()
    parties22 = set(s22["partido"])
    options = sorted(parties26 & parties22) if parties26 else sorted(parties22)
    if not options:
        no_data("Nenhum partido com candidato nos dois anos para esta seleção.")
        return
    default = next((p for p in DEFAULT_PARTIES if p in options), options[0])
    if st.session_state.get(K_PARTY) not in options:
        st.session_state[K_PARTY] = default
    c1, c2 = st.columns([1, 3])
    partido = c1.selectbox("Partido", options, key=K_PARTY,
                           help="Partido do candidato comparado (mesma sigla em 2022 e 2026).")
    mode = c2.segmented_control("Visualização", MODES, default=MODES[0], key=K_MODE) or MODES[0]

    has_head = _headline(ctx, cargo, partido, eleicao, mtime)
    if s26.empty:
        if has_head and level == "mu":
            st.info(f"Os resultados por município de {ctx.area_nome} aparecem quando o coletor varrer o estado "
                    "(alguns minutos depois de abri-lo pela primeira vez).")
        else:
            no_data("Ainda não há votos apurados de 2026 para esta seleção — o swing aparece assim que o TSE "
                    "divulgar os primeiros resultados (a partir das 17h de Brasília).")
        _reference_2022(s22, partido, loc, gj, level, uf)
        return

    names = _area_names(level, uf)
    sw = hist2022.compute_swing(s26, s22, partido, keys, require_both=(cargo != 1))
    sw["nome"] = sw[loc].map(names).fillna(sw[loc])
    sw["loc_label"] = sw["uf"].str.upper() if loc == "uf" else sw["nome"]
    sw["hover"] = _hover_swing(sw, partido)

    if mode == MODES[0]:
        if sw.empty:
            no_data(f"{partido} não tem candidato comparável nas áreas apuradas.")
        elif gj is None:
            _table(sw, partido)
        else:
            st.plotly_chart(diverging_map(sw, gj, loc, "swing_pp", f"Swing {partido}<br>2026 − 2022"),
                            width="stretch", key="cmp22_map_swing", config={"displayModeBar": False})
            _swing_caption(sw, partido, level)
    elif mode == MODES[1]:
        if sw.empty:
            no_data(f"{partido} não tem candidato comparável nas áreas apuradas.")
        else:
            st.plotly_chart(scatter_fig(sw, partido), width="stretch", key="cmp22_scatter",
                            config={"displayModeBar": False})
            _swing_caption(sw, partido, level)
    elif mode == MODES[2]:
        _abstention(ctx, cargo, eleicao, level, uf, loc, gj, names, mtime)
    else:
        _table(sw, partido)


def _swing_caption(sw: pd.DataFrame, partido: str, level: str) -> None:
    up, down = int((sw["swing_pp"] > 0).sum()), int((sw["swing_pp"] < 0).sum())
    what = "UFs" if level == "uf" else "municípios"
    st.caption(f"{len(sw)} {what} com apuração em 2026: {partido} cresceu em {up} e caiu em {down}. "
               "Cores: verde-azulado = ganhou, marrom = perdeu, cinza = sem mudança; sem cor = nada apurado.")


def _table(sw: pd.DataFrame, partido: str) -> None:
    t = sw.sort_values("swing_pp", ascending=False)[
        ["nome", "nm_urna_2022", "pct_2022", "nm_urna_2026", "pct_2026", "swing_pp", "pct_secoes"]]
    st.dataframe(t, hide_index=True, width="stretch", key="cmp22_table", column_config={
        "nome": "Área",
        "nm_urna_2022": f"{partido} 2022",
        "pct_2022": st.column_config.NumberColumn("% 2022", format="%.2f"),
        "nm_urna_2026": f"{partido} 2026",
        "pct_2026": st.column_config.NumberColumn("% 2026", format="%.2f"),
        "swing_pp": st.column_config.NumberColumn("Swing (pp)", format="%+.2f"),
        "pct_secoes": st.column_config.NumberColumn("Seções apuradas 2026 (%)", format="%.1f"),
    })


def _abstention(ctx: ViewContext, cargo: int, eleicao: str, level: str, uf: str | None, loc: str,
                gj: dict | None, names: dict, mtime: str) -> None:
    keys = ["uf", "mun"]
    t26 = store.latest_totals(ctx.conn, eleicao, cargo, "uf" if level == "uf" else "mu", uf)
    if level == "uf":
        t26 = t26[t26["uf"].isin(config.UF_LIST)]
    t22 = _turnout22("uf" if level == "uf" else "mu", ctx.turno, cargo, uf, mtime)
    ab = hist2022.compute_turnout_change(hist2022.turnout_2026(t26, keys), t22, keys)
    if ab.empty:
        no_data("Ainda não há eleitorado apurado em 2026 para comparar a abstenção.")
        return
    ab["nome"] = ab[loc].map(names).fillna(ab[loc])
    ab["hover"] = _hover_abst(ab)
    if gj is not None:
        st.plotly_chart(diverging_map(ab, gj, loc, "delta_pp", "Abstenção<br>2026 − 2022", reverse=True),
                        width="stretch", key="cmp22_map_abst", config={"displayModeBar": False})
    up = int((ab["delta_pp"] > 0).sum())
    st.caption(f"Abstenção de 2026 calculada sobre o eleitorado das seções já apuradas; 2022 = resultado final "
               f"do {ctx.turno}º turno. A abstenção subiu em {up} de {len(ab)} áreas. "
               "Cores: marrom = abstenção maior que em 2022, verde-azulado = menor.")
    with st.expander("Tabela"):
        st.dataframe(ab.sort_values("delta_pp", ascending=False)[["nome", "abst_2022", "abst_2026", "delta_pp",
                                                                   "pct_secoes"]],
                     hide_index=True, width="stretch", key="cmp22_abst_table", column_config={
                         "nome": "Área",
                         "abst_2022": st.column_config.NumberColumn("Abstenção 2022 (%)", format="%.2f"),
                         "abst_2026": st.column_config.NumberColumn("Abstenção 2026 (%)", format="%.2f"),
                         "delta_pp": st.column_config.NumberColumn("Variação (pp)", format="%+.2f"),
                         "pct_secoes": st.column_config.NumberColumn("Seções apuradas 2026 (%)", format="%.1f"),
                     })


def _reference_2022(s22: pd.DataFrame, partido: str, loc: str, gj: dict | None, level: str,
                    uf: str | None) -> None:
    """Antes da apuração: mapa de referência com o % do partido em 2022."""
    if gj is None or s22.empty:
        return
    d = s22[s22["partido"] == partido].copy()
    if d.empty:
        return
    names = _area_names(level, uf)
    d["nome"] = d[loc].map(names).fillna(d[loc])
    d["hover"] = [f"<b>{n}</b><br>{partido} 2022 ({c}): {fmt_pct(p)}"
                  for n, c, p in zip(d["nome"], d["nm_urna"], d["pct_validos"])]
    ramp = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
    st.markdown(f"**Referência: {partido} em 2022** (% dos válidos)")
    fig = maps.value_map(d, gj, "pct_validos", loc=loc, hover_col="hover", zmin=0,
                         colorscale=[[i / 6, c] for i, c in enumerate(ramp)], colorbar_title="%", height=520)
    fig.update_traces(marker_line_color=_THEME[_mode()]["line"], colorbar=dict(ticksuffix="%"))
    st.plotly_chart(fig, width="stretch", key="cmp22_map_ref", config={"displayModeBar": False})


def render(ctx: ViewContext) -> None:
    try:
        _render(ctx)
    except Exception:  # noqa: BLE001 — nunca deixar a exceção chegar ao usuário
        log.exception("comparacao2022 falhou")
        st.error("Não foi possível montar a comparação com 2022 agora. Tentando de novo na próxima atualização.")
