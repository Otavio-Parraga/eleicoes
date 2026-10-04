"""Tela: Congresso — composição (projetada ou oficial) da Câmara, do Senado e das Assembleias.

Durante a apuração o TSE só marca eleitos quando a vaga está garantida; para Câmara e Assembleias usamos a
projeção de `eleicoes.seats` com os votos já totalizados (UF a UF), e as marcas do TSE quando a UF está
finalizada.
"""
from __future__ import annotations

import logging
import math
from collections import Counter

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .. import config, seats, store
from .common import ViewContext, fmt_int, fmt_pct, no_data, party_color, uf_name

log = logging.getLogger(__name__)

K_CASA = "congresso_casa"
K_CASA_CARGO = "congresso_casa_cargo"
K_AL_UF = "congresso_assembleia_uf"
CASAS = ["Câmara", "Senado", "Assembleia"]
LABEL_PROJ = "Projeção (apuração em andamento)"
LABEL_OFIC = "Resultado oficial"

# Escala sequencial (uma matiz, claro -> escuro) da skill dataviz
SEQ_SCALE = [[0.0, "#cde2fb"], [0.35, "#86b6ef"], [0.7, "#2a78d6"], [1.0, "#0d366b"]]

# Ordem ideológica aproximada (esquerda -> direita) para dispor os assentos no hemiciclo.
IDEOLOGIA = [
    "PCO", "PSTU", "PCB", "UP", "PSOL", "REDE", "PT", "PCDOB", "PC do B", "PCdoB", "PV", "PSB", "PDT",
    "SOLIDARIEDADE", "CIDADANIA", "AVANTE", "PMB", "AGIR", "DEMOCRATA", "MDB", "PSDB", "PSD", "MOBILIZA", "PODE", "DC",
    "PRTB", "PRD", "REPUBLICANOS", "PP", "UNIÃO", "MISSÃO", "NOVO", "PL",
]
_RANK = {s: i for i, s in enumerate(IDEOLOGIA)}
# Siglas como vêm nos arquivos do TSE que não estão em config.PARTY_COLORS
_COLOR_ALIAS = {"PCDOB": "PCdoB"}


def _color(sigla: str) -> str:
    return party_color(_COLOR_ALIAS.get(sigla, sigla))


def _ideo(sigla: str) -> float:
    return _RANK.get(sigla, _RANK["MDB"] - 0.5)  # desconhecidos ficam no centro


def _default_casa(cargo: int) -> str:
    return {5: "Senado", 7: "Assembleia", 8: "Assembleia"}.get(cargo, "Câmara")


# ----------------------------------------------------------------------------- dados

def _db_id(conn) -> str:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        return str(row[2]) if row else ""
    except Exception:
        return ""


def _signature(conn, eleicao: str, cargo: int, uf: str | None) -> tuple:
    t = store.latest_totals(conn, eleicao, cargo, "uf", uf)
    return tuple((str(u), int(s)) for u, s in zip(t["uf"], t["snapshot_id"]))


def compute_proportional(totals: pd.DataFrame, cands: pd.DataFrame, parties: pd.DataFrame,
                         top_n: int = 300) -> dict:
    """Junta, UF a UF, eleitos oficiais (UF finalizada) ou projetados (seats.project)."""
    ufs_rows, el_rows, agr_rows = [], [], []
    cand_groups = dict(tuple(cands.groupby("uf"))) if not cands.empty else {}
    party_groups = dict(tuple(parties.groupby("uf"))) if not parties.empty else {}
    status: dict[tuple[str, str], str] = {}
    for t in totals.itertuples(index=False):
        uf = t.uf
        c = cand_groups.get(uf, cands.head(0))
        p = party_groups.get(uf, parties.head(0))
        base = dict(uf=uf, vagas=int(t.vagas or 0), pct_secoes=float(t.pct_secoes or 0),
                    finalizada=bool(t.finalizada), tse_ts=t.tse_ts)
        if c.empty or int(c["votos"].sum()) <= 0 or base["vagas"] <= 0:
            ufs_rows.append({**base, "validos": 0, "qe": 0, "oficial": False, "incluida": False, "cadeiras": 0})
            continue
        alloc = seats.project(p, c, base["vagas"], int(t.validos or 0))
        official = base["finalizada"] and bool(c["eleito"].any())
        pct = dict(zip(c["sqcand"], c["pct_validos"]))
        if official:
            el = c[c["eleito"]]
            rows = [dict(uf=uf, sqcand=r.sqcand, nome_urna=r.nome_urna, partido=r.partido,
                         agremiacao=r.agremiacao, votos=int(r.votos), pct_validos=float(r.pct_validos),
                         via=r.situacao or "Eleito", fonte="oficial") for r in el.itertuples(index=False)]
            for r in c.itertuples(index=False):
                status[(uf, r.sqcand)] = r.situacao or ("Eleito" if r.eleito else "Não eleito")
        else:
            rows = [dict(uf=uf, sqcand=e.sqcand, nome_urna=e.nome_urna, partido=e.partido,
                         agremiacao=e.agremiacao, votos=e.votos, pct_validos=float(pct.get(e.sqcand, 0.0)),
                         via=e.via, fonte="projeção") for e in alloc.elected]
            elected_ids = {e.sqcand for e in alloc.elected}
            for sq in c["sqcand"]:
                status[(uf, sq)] = "Eleito (projeção)" if sq in elected_ids else "Não eleito (projeção)"
        el_rows.extend(rows)
        seats_agr = Counter(r["agremiacao"] for r in rows)
        qe = alloc.qe or 1
        for agr, v in alloc.agremiacao_votos.items():
            if v <= 0 and not seats_agr.get(agr):
                continue
            agr_rows.append(dict(uf=uf, agremiacao=agr, votos=int(v), pct=100 * v / max(alloc.validos, 1),
                                 pct_qe=100 * v / qe, qp=int(alloc.qp.get(agr, 0)),
                                 cadeiras=int(seats_agr.get(agr, 0))))
        ufs_rows.append({**base, "validos": alloc.validos, "qe": alloc.qe, "oficial": official,
                         "incluida": True, "cadeiras": len(rows)})

    ufs = pd.DataFrame(ufs_rows, columns=["uf", "vagas", "pct_secoes", "finalizada", "tse_ts", "validos", "qe",
                                          "oficial", "incluida", "cadeiras"])
    elected = pd.DataFrame(el_rows, columns=["uf", "sqcand", "nome_urna", "partido", "agremiacao", "votos",
                                             "pct_validos", "via", "fonte"])
    agr = pd.DataFrame(agr_rows, columns=["uf", "agremiacao", "votos", "pct", "pct_qe", "qp", "cadeiras"])
    top = cands[cands["votos"] > 0].nlargest(top_n, "votos")[
        ["uf", "sqcand", "nome_urna", "partido", "agremiacao", "votos", "pct_validos"]].copy() \
        if not cands.empty else pd.DataFrame(columns=["uf", "sqcand", "nome_urna", "partido", "agremiacao",
                                                      "votos", "pct_validos"])
    top["situacao"] = [status.get((u, s), "") for u, s in zip(top["uf"], top["sqcand"])]
    return {"ufs": ufs, "elected": elected, "agr": agr, "top": top.reset_index(drop=True)}


@st.cache_data(ttl=60, show_spinner=False, max_entries=32)
def _proportional_cached(_conn, db: str, eleicao: str, cargo: int, uf: str | None, sig: tuple) -> dict:
    totals = store.latest_totals(_conn, eleicao, cargo, "uf", uf)
    cands = store.latest_candidates(_conn, eleicao, cargo, "uf", uf)
    parties = store.latest_parties(_conn, eleicao, cargo, "uf", uf)
    return compute_proportional(totals, cands, parties)


def _proportional(conn, eleicao: str, cargo: int, uf: str | None) -> dict:
    sig = _signature(conn, eleicao, cargo, uf)
    return _proportional_cached(conn, _db_id(conn), eleicao, cargo, uf, sig)


def compute_senado(totals: pd.DataFrame, cands: pd.DataFrame) -> dict:
    ufs_rows, seat_rows = [], []
    groups = dict(tuple(cands.groupby("uf"))) if not cands.empty else {}
    for t in totals.itertuples(index=False):
        c = groups.get(t.uf)
        vagas = int(t.vagas or 0) or 2
        base = dict(uf=t.uf, vagas=vagas, pct_secoes=float(t.pct_secoes or 0), finalizada=bool(t.finalizada))
        if c is None or c.empty or int(c["votos"].sum()) <= 0:
            ufs_rows.append({**base, "incluida": False, "oficial": False})
            continue
        c = c.sort_values("rank")
        official = base["finalizada"] and bool(c["eleito"].any())
        win = c[c["eleito"]] if official else c.head(vagas)
        rest = c[~c["sqcand"].isin(win["sqcand"])]
        nxt = rest.iloc[0] if not rest.empty else None
        for i, r in enumerate(win.itertuples(index=False)):
            seat_rows.append(dict(uf=t.uf, pos=i + 1, sqcand=r.sqcand, nome_urna=r.nome_urna, partido=r.partido,
                                  votos=int(r.votos), pct_validos=float(r.pct_validos),
                                  fonte="oficial" if official else "projeção"))
        last_pct = float(win["pct_validos"].iloc[-1]) if not win.empty else 0.0
        ufs_rows.append({**base, "incluida": True, "oficial": official,
                         "prox_nome": None if nxt is None else nxt["nome_urna"],
                         "prox_partido": None if nxt is None else nxt["partido"],
                         "prox_pct": None if nxt is None else float(nxt["pct_validos"]),
                         "margem": None if nxt is None else last_pct - float(nxt["pct_validos"])})
    ufs = pd.DataFrame(ufs_rows)
    for col in ["incluida", "oficial", "prox_nome", "prox_partido", "prox_pct", "margem"]:
        if col not in ufs:
            ufs[col] = None
    seats_df = pd.DataFrame(seat_rows, columns=["uf", "pos", "sqcand", "nome_urna", "partido", "votos",
                                                "pct_validos", "fonte"])
    return {"ufs": ufs, "seats": seats_df}


@st.cache_data(ttl=60, show_spinner=False, max_entries=8)
def _senado_cached(_conn, db: str, eleicao: str, sig: tuple) -> dict:
    return compute_senado(store.latest_totals(_conn, eleicao, 5, "uf"),
                          store.latest_candidates(_conn, eleicao, 5, "uf"))


# ----------------------------------------------------------------------------- gráficos

def hemicycle_positions(n: int) -> np.ndarray:
    """Coordenadas (x, y) de n assentos em arcos concêntricos, ordenadas da esquerda para a direita."""
    if n <= 0:
        return np.zeros((0, 2))
    rows = max(1, min(n, int(round(math.sqrt(n / 4.0)))))
    radii = np.linspace(0.4, 1.0, rows) if rows > 1 else np.array([0.8])
    raw = n * radii / radii.sum()
    counts = np.floor(raw).astype(int)
    for i in np.argsort(-(raw - counts))[: n - int(counts.sum())]:
        counts[i] += 1
    pts = []
    for r, k in zip(radii, counts):
        angles = [math.pi / 2] if k == 1 else np.linspace(math.pi, 0, k)
        pts.extend((a, r) for a in angles)
    pts.sort(key=lambda p: (-round(p[0], 9), p[1]))
    return np.array([(r * math.cos(a), r * math.sin(a)) for a, r in pts])


def _seat_size(n: int, px_per_unit: float = 270.0) -> float:
    if n <= 0:
        return 10
    rows = max(1, min(n, int(round(math.sqrt(n / 4.0)))))
    radii = np.linspace(0.4, 1.0, rows) if rows > 1 else np.array([0.8])
    k_outer = max(2, n * radii[-1] / radii.sum())
    spacing = math.pi * radii[-1] / (k_outer - 1)
    if rows > 1:
        spacing = min(spacing, 0.6 / (rows - 1))
    return float(max(4.0, min(24.0, 0.8 * spacing * px_per_unit)))


def hemicycle_figure(seats_df: pd.DataFrame, hover: list[str], center_label: str) -> go.Figure:
    """`seats_df` precisa da coluna 'partido'; uma linha por assento. Ordena por ideologia e pinta por partido."""
    df = seats_df.copy()
    df["_hover"] = hover
    df["_rank"] = df["partido"].map(_ideo)
    sort_cols = ["_rank", "partido"] + (["votos"] if "votos" in df else [])
    df = df.sort_values(sort_cols, ascending=[True, True] + ([False] if "votos" in df else []))
    pos = hemicycle_positions(len(df))
    df["x"], df["y"] = pos[:, 0], pos[:, 1]
    size = _seat_size(len(df))
    fig = go.Figure()
    for partido, g in df.groupby("partido", sort=False):
        fig.add_trace(go.Scatter(
            x=g["x"], y=g["y"], mode="markers", name=f"{partido} ({len(g)})",
            marker=dict(size=size, color=_color(partido), line=dict(width=0)),
            text=g["_hover"], hovertemplate="%{text}<extra></extra>"))
    fig.add_annotation(x=0, y=0.05, text=center_label, showarrow=False, font=dict(size=16))
    fig.update_layout(
        height=430, margin=dict(l=0, r=0, t=10, b=0), showlegend=True,
        legend=dict(orientation="h", yanchor="top", y=-0.02, xanchor="center", x=0.5, font=dict(size=11),
                    itemclick="toggle"),
        xaxis=dict(visible=False, range=[-1.08, 1.08]),
        yaxis=dict(visible=False, range=[-0.08, 1.08], scaleanchor="x", scaleratio=1),
        hoverlabel=dict(align="left"))
    return fig


def party_bar(counts: pd.Series, title: str) -> go.Figure:
    counts = counts[counts > 0].sort_values(ascending=True)
    fig = go.Figure(go.Bar(
        x=counts.values, y=counts.index, orientation="h",
        marker=dict(color=[_color(p) for p in counts.index], line=dict(width=0)),
        text=[str(v) for v in counts.values], textposition="outside", cliponaxis=False,
        hovertemplate="%{y}: %{x} cadeiras<extra></extra>"))
    fig.update_layout(
        title=dict(text=title, font=dict(size=14)), height=max(220, 24 * len(counts) + 70),
        margin=dict(l=0, r=30, t=40, b=10), bargap=0.25, barcornerradius=4, showlegend=False,
        xaxis=dict(visible=False), yaxis=dict(showgrid=False, ticks=""))
    return fig


def seats_heatmap(elected: pd.DataFrame) -> go.Figure:
    pv = elected.groupby(["partido", "uf"]).size().unstack(fill_value=0)
    pv = pv.loc[pv.sum(axis=1).sort_values(ascending=False).index, sorted(pv.columns)]
    z = pv.values.astype(float)
    text = np.where(z > 0, z.astype(int).astype(str), "")
    z[z == 0] = np.nan
    fig = go.Figure(go.Heatmap(
        z=z, x=[u.upper() for u in pv.columns], y=list(pv.index), colorscale=SEQ_SCALE, zmin=0.5,
        text=text, texttemplate="%{text}", textfont=dict(size=11), xgap=2, ygap=2, hoverongaps=False,
        colorbar=dict(title="Cadeiras", thickness=12),
        hovertemplate="%{y} · %{x}: %{z} cadeiras<extra></extra>"))
    fig.update_layout(height=max(260, 24 * len(pv) + 90), margin=dict(l=0, r=0, t=10, b=10),
                      yaxis=dict(autorange="reversed", showgrid=False), xaxis=dict(side="top", showgrid=False))
    return fig


# ----------------------------------------------------------------------------- blocos de tela

def _banner(n_ufs: int, n_oficial: int, total_ufs: int | None = 27) -> None:
    if n_ufs and n_oficial == n_ufs and (total_ufs is None or n_ufs >= total_ufs):
        st.success(f"**{LABEL_OFIC}** — eleitos conforme marcação do TSE.")
    else:
        extra = f" {n_oficial} de {n_ufs} UFs já com resultado oficial do TSE." if n_oficial else ""
        st.info(f"**{LABEL_PROJ}** — cadeiras calculadas por nós pelas regras do quociente eleitoral com os "
                f"votos já totalizados; podem mudar até o fim da apuração.{extra}")


def _hover_seat(r) -> str:
    return (f"<b>{r.nome_urna}</b><br>{r.partido} · {str(r.uf).upper()}<br>{fmt_int(r.votos)} votos "
            f"({fmt_pct(r.pct_validos)} na UF)")


def _rules_expander() -> None:
    with st.expander("Como a projeção é calculada"):
        st.markdown(seats.__doc__ or "")


def _render_uf_detail(data: dict, uf: str, casa: str, banner: bool = True) -> None:
    ufs = data["ufs"]
    row = ufs[ufs["uf"] == uf]
    if row.empty or not bool(row["incluida"].iloc[0]):
        no_data(f"Ainda não há votos para {casa} em {uf_name(uf)}.")
        return
    u = row.iloc[0]
    el = data["elected"][data["elected"]["uf"] == uf]
    if banner:
        _banner(1, int(bool(u["oficial"])), None)
    else:
        st.caption(f"**{LABEL_OFIC if bool(u['oficial']) else LABEL_PROJ}** em {uf_name(uf)}.")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Vagas", fmt_int(u["vagas"]))
    c2.metric("Seções apuradas", fmt_pct(u["pct_secoes"]))
    c3.metric("Votos válidos", fmt_int(u["validos"]))
    c4.metric("Quociente eleitoral", fmt_int(u["qe"]))
    left, right = st.columns([3, 2])
    with left:
        fig = hemicycle_figure(el, [_hover_seat(r) for r in el.itertuples(index=False)],
                               f"<b>{len(el)}</b><br>cadeiras")
        st.plotly_chart(fig, config={"displayModeBar": False}, key=f"cong_uf_hemi_{uf}")
    with right:
        st.plotly_chart(party_bar(el["partido"].value_counts(), "Cadeiras por partido"),
                        config={"displayModeBar": False}, key=f"cong_uf_bar_{uf}")
    agr = data["agr"][data["agr"]["uf"] == uf].sort_values("votos", ascending=False)
    st.markdown("**Agremiações** (federação conta como uma só)")
    st.dataframe(pd.DataFrame({
        "Agremiação": agr["agremiacao"],
        "Votos": agr["votos"].map(fmt_int),
        "% válidos": agr["pct"].map(fmt_pct),
        "% do QE": agr["pct_qe"].map(lambda x: fmt_pct(x, 1)),
        "QP": agr["qp"],
        "Cadeiras": agr["cadeiras"],
    }), hide_index=True)
    st.markdown("**Eleitos** " + ("(TSE)" if bool(u["oficial"]) else "(projeção)"))
    els = el.sort_values("votos", ascending=False)
    st.dataframe(pd.DataFrame({
        "Candidato": els["nome_urna"], "Partido": els["partido"], "Agremiação": els["agremiacao"],
        "Votos": els["votos"].map(fmt_int), "% válidos": els["pct_validos"].map(fmt_pct), "Via": els["via"],
    }), hide_index=True)


def _render_camara(ctx: ViewContext) -> None:
    eleicao = config.ELECTIONS[(1, 6)]
    data = _proportional(ctx.conn, eleicao, 6, None)
    ufs = data["ufs"]
    inc = ufs[ufs["incluida"].astype(bool)] if not ufs.empty else ufs
    if inc.empty:
        no_data()
        return
    el = data["elected"]
    st.subheader("Câmara dos Deputados")
    _banner(len(inc), int(inc["oficial"].sum()))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("UFs incluídas", f"{len(inc)} de 27")
    c2.metric("Seções apuradas (média das UFs)", fmt_pct(inc["pct_secoes"].mean()))
    c3.metric("Cadeiras nessas UFs", fmt_int(inc["vagas"].sum()))
    c4.metric("UFs com resultado oficial", fmt_int(inc["oficial"].sum()))
    left, right = st.columns([3, 2])
    with left:
        fig = hemicycle_figure(el, [_hover_seat(r) for r in el.itertuples(index=False)],
                               f"<b>{len(el)}</b><br>cadeiras")
        st.plotly_chart(fig, config={"displayModeBar": False}, key="cong_camara_hemi")
    with right:
        st.plotly_chart(party_bar(el["partido"].value_counts(), "Cadeiras por partido"),
                        config={"displayModeBar": False}, key="cong_camara_bar")
    if len(inc) < 27:
        faltam = sorted(set(config.UF_LIST) - set(inc["uf"]))
        st.caption("Sem votos ainda: " + ", ".join(u.upper() for u in faltam))

    if ctx.uf in config.UFS:
        st.subheader(f"Bancada federal — {uf_name(ctx.uf)}")
        _render_uf_detail(data, ctx.uf, "Deputado Federal", banner=False)

    st.subheader("Cadeiras por partido e UF")
    st.plotly_chart(seats_heatmap(el), config={"displayModeBar": False}, key="cong_camara_heatmap")

    st.subheader("Os 20 deputados federais mais votados do país")
    top = data["top"].head(20)
    st.dataframe(pd.DataFrame({
        "#": range(1, len(top) + 1), "Candidato": top["nome_urna"], "Partido": top["partido"],
        "UF": top["uf"].str.upper(), "Votos": top["votos"].map(fmt_int),
        "% válidos na UF": top["pct_validos"].map(fmt_pct), "Situação": top["situacao"],
    }), hide_index=True)
    _rules_expander()


def _render_senado(ctx: ViewContext) -> None:
    eleicao = config.ELECTIONS[(1, 5)]
    sig = _signature(ctx.conn, eleicao, 5, None)
    data = _senado_cached(ctx.conn, _db_id(ctx.conn), eleicao, sig)
    ufs = data["ufs"]
    inc = ufs[ufs["incluida"].astype(bool)] if not ufs.empty else ufs
    if inc.empty:
        no_data()
        return
    s = data["seats"]
    st.subheader("Senado Federal — vagas em disputa em 2026")
    n_of = int(inc["oficial"].astype(bool).sum())
    if n_of == len(inc) and len(inc) >= 27:
        st.success(f"**{LABEL_OFIC}** — eleitos conforme marcação do TSE.")
    else:
        extra = f" {n_of} de {len(inc)} UFs já com resultado oficial." if n_of else ""
        st.info(f"**{LABEL_PROJ}** — nas UFs ainda em apuração mostramos os {2} mais votados no momento.{extra}")
    c1, c2, c3 = st.columns(3)
    c1.metric("UFs incluídas", f"{len(inc)} de 27")
    c2.metric("Seções apuradas (média das UFs)", fmt_pct(inc["pct_secoes"].mean()))
    c3.metric("Cadeiras nessas UFs", fmt_int(inc["vagas"].sum()))
    left, right = st.columns([3, 2])
    with left:
        fig = hemicycle_figure(s, [_hover_seat(r) for r in s.itertuples(index=False)],
                               f"<b>{len(s)}</b><br>cadeiras")
        st.plotly_chart(fig, config={"displayModeBar": False}, key="cong_senado_hemi")
    with right:
        st.plotly_chart(party_bar(s["partido"].value_counts(), "Cadeiras por partido"),
                        config={"displayModeBar": False}, key="cong_senado_bar")

    def who(uf: str, pos: int) -> str:
        r = s[(s["uf"] == uf) & (s["pos"] == pos)]
        if r.empty:
            return ""
        r = r.iloc[0]
        return f"{r['nome_urna']} ({r['partido']}) – {fmt_pct(r['pct_validos'])}"

    rows = []
    for u in inc.sort_values("uf").itertuples(index=False):
        rows.append({
            "UF": u.uf.upper(), "1ª vaga": who(u.uf, 1), "2ª vaga": who(u.uf, 2),
            "Próximo colocado": "" if u.prox_nome is None or pd.isna(u.prox_nome)
            else f"{u.prox_nome} ({u.prox_partido}) – {fmt_pct(u.prox_pct)}",
            "Margem p/ entrar (p.p.)": "" if u.margem is None or pd.isna(u.margem)
            else fmt_pct(u.margem).replace("%", ""),
            "Seções apuradas": fmt_pct(u.pct_secoes),
            "Situação": LABEL_OFIC if u.oficial else "Projeção",
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True)
    st.caption("% sobre os votos válidos para senador na UF (cada eleitor vota em 2 candidatos em 2026).")


def _render_assembleia(ctx: ViewContext) -> None:
    uf = ctx.uf if ctx.uf in config.UFS else None
    if uf is None:
        st.info("A Assembleia Legislativa é estadual: escolha um estado na barra lateral (ou aqui embaixo).")
        uf = st.selectbox("Estado", config.UF_LIST, index=None, format_func=uf_name, key=K_AL_UF,
                          placeholder="Escolha um estado")
        if not uf:
            return
    casa = "Câmara Legislativa do Distrito Federal" if uf == "df" else f"Assembleia Legislativa — {uf_name(uf)}"
    st.subheader(casa)
    data = _proportional(ctx.conn, config.ELECTIONS[(1, 7)], 7, uf)
    _render_uf_detail(data, uf, "Deputado Distrital" if uf == "df" else "Deputado Estadual")
    _rules_expander()


def render(ctx: ViewContext) -> None:
    if ctx.turno != 1:
        st.info("No 2º turno só há eleição para Presidente e Governador. A composição da Câmara, do Senado e das "
                "Assembleias é definida no 1º turno — escolha \"1º turno\" na barra lateral para vê-la.")
        return
    try:
        if st.session_state.get(K_CASA_CARGO) != ctx.cargo or st.session_state.get(K_CASA) not in CASAS:
            st.session_state[K_CASA] = _default_casa(ctx.cargo)
            st.session_state[K_CASA_CARGO] = ctx.cargo
        casa = st.radio("Casa", CASAS, key=K_CASA, horizontal=True, label_visibility="collapsed")
        if casa == "Senado":
            _render_senado(ctx)
        elif casa == "Assembleia":
            _render_assembleia(ctx)
        else:
            _render_camara(ctx)
    except Exception:  # nunca mostrar traceback ao usuário
        log.exception("erro na tela Congresso")
        no_data("Não foi possível montar a tela do Congresso agora; tentaremos de novo na próxima atualização.")
