"""Tela: Evolução da apuração.

O TSE não publica histórico: o coletor guarda um snapshot por mudança nos níveis br/uf, e esta tela mostra
como a disputa evoluiu durante a noite — % dos válidos dos principais candidatos, viradas (mudanças de
liderança ou de quem ocupa as vagas), vantagem entre 1º e 2º e o ritmo da apuração.

As funções de preparação de dados (resolve_area, counted, ranked, top_candidates, lead_changes,
margin_series, line_styles, pace) são puras e testadas em tests/test_view_evolucao.py.
"""
from __future__ import annotations

import logging

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .. import config, store
from .common import ViewContext, fmt_pct, no_data, party_color, uf_name

log = logging.getLogger(__name__)

DASHES = ["solid", "dash", "dot", "dashdot", "longdash", "longdashdot"]
X_PCT, X_TIME = "% seções apuradas", "Horário"
ERROR_MSG = "Não foi possível montar a evolução da apuração agora. Nova tentativa na próxima atualização."

# Cromo dos gráficos (paleta de referência da skill dataviz). Candidatos usam party_color().
_CHROME = {
    "light": dict(surface="#fcfcfb", ink="#0b0b0b", ink2="#52514e", muted="#898781", base="#c3c2b7",
                  s1="#2a78d6", s2="#eb6834"),
    "dark": dict(surface="#1a1a19", ink="#ffffff", ink2="#c3c2b7", muted="#898781", base="#383835",
                 s1="#3987e5", s2="#d95926"),
}

_CHANGE_COLS = ["snapshot_id", "tse_ts", "pct_secoes", "posicao", "sqcand", "nome_urna", "partido", "pct_validos",
                "sqcand_ultrapassado", "nome_ultrapassado", "partido_ultrapassado", "pct_ultrapassado", "margem_pp"]
_TOP_COLS = ["sqcand", "numero", "nome_urna", "partido", "votos", "pct_validos"]


# ----------------------------------------------------------------------------- funções puras

def resolve_area(cargo: int, uf: str, uf_choice: str | None = None) -> tuple[str, str]:
    """(nivel, uf) da área cujo histórico será mostrado.

    Presidente no Brasil -> ('br', 'br'). Demais cargos não têm arquivo br: no Brasil usa `uf_choice`
    (padrão 'rs'). Municípios não têm histórico, então quem chama passa a UF do município.
    """
    uf = (uf or config.BRASIL).lower()
    if uf == config.BRASIL:
        if cargo == 1:
            return "br", config.BRASIL
        return "uf", (uf_choice or "rs").lower()
    return "uf", uf


def counted(hist: pd.DataFrame) -> pd.DataFrame:
    """Só os snapshots com algum voto (antes das 17h os arquivos vêm zerados)."""
    if hist.empty:
        return hist
    tot = hist.groupby("snapshot_id")["votos"].transform("sum")
    return hist[tot > 0]


def ranked(hist: pd.DataFrame) -> pd.DataFrame:
    """Histórico com votos, ordenado por tse_ts, com a coluna `pos` (1 = mais votado no snapshot)."""
    h = counted(hist)
    if h.empty:
        return h.assign(pos=pd.Series(dtype="int64"))
    h = h.sort_values(["tse_ts", "snapshot_id", "votos", "numero"], ascending=[True, True, False, True])
    return h.assign(pos=h.groupby("snapshot_id", sort=False).cumcount() + 1)


def top_candidates(hist: pd.DataFrame, n: int) -> pd.DataFrame:
    """Os `n` mais votados no snapshot mais recente com votos (mais votado primeiro)."""
    r = ranked(hist)
    if r.empty:
        return pd.DataFrame(columns=_TOP_COLS)
    last = r[r["snapshot_id"] == r["snapshot_id"].iloc[-1]]
    return last[last["pos"] <= int(n)].sort_values("pos")[_TOP_COLS].reset_index(drop=True)


def lead_changes(hist: pd.DataFrame, seats: int = 1) -> pd.DataFrame:
    """Viradas: snapshots em que mudou quem ocupa as `seats` primeiras posições.

    seats=1 -> mudanças de liderança; seats=2 (Senado 2026) -> alguém entrou nas duas vagas.
    Uma linha por ultrapassagem: quem passou à frente (sqcand, nome_urna, partido, pct_validos), quem foi
    ultrapassado (*_ultrapassado) e a vantagem em pp logo após a virada (margem_pp), além de snapshot_id,
    tse_ts, pct_secoes e posicao (= seats).
    """
    r = ranked(hist)
    rows = []
    prev: list[str] | None = None
    for sid, g in r.groupby("snapshot_id", sort=False):
        cur = list(g.loc[g["pos"] <= seats, "sqcand"])
        if prev is not None and set(cur) != set(prev):
            by_sq = g.set_index("sqcand")
            entered = [s for s in cur if s not in prev]
            left = sorted((s for s in prev if s not in cur and s in by_sq.index),
                          key=lambda s: by_sq.at[s, "pos"])
            for new, old in zip(entered, left):
                a, b = by_sq.loc[new], by_sq.loc[old]
                rows.append({
                    "snapshot_id": sid, "tse_ts": a["tse_ts"], "pct_secoes": a["pct_secoes"], "posicao": seats,
                    "sqcand": new, "nome_urna": a["nome_urna"], "partido": a["partido"],
                    "pct_validos": a["pct_validos"], "sqcand_ultrapassado": old,
                    "nome_ultrapassado": b["nome_urna"], "partido_ultrapassado": b["partido"],
                    "pct_ultrapassado": b["pct_validos"], "margem_pp": a["pct_validos"] - b["pct_validos"],
                })
        prev = cur
    return pd.DataFrame(rows, columns=_CHANGE_COLS)


def margin_series(hist: pd.DataFrame, a: str, b: str) -> pd.DataFrame:
    """Vantagem de `a` sobre `b` (sqcand) em pontos percentuais dos válidos, snapshot a snapshot.

    Colunas: snapshot_id, tse_ts, pct_secoes, pct_a, pct_b, margem_pp (negativa = b à frente).
    """
    h = counted(hist)
    cols = ["snapshot_id", "tse_ts", "pct_secoes", "pct_a", "pct_b", "margem_pp"]
    if h.empty:
        return pd.DataFrame(columns=cols)
    pa = h[h["sqcand"] == a].set_index("snapshot_id")[["tse_ts", "pct_secoes", "pct_validos"]]
    pb = h[h["sqcand"] == b].set_index("snapshot_id")["pct_validos"].rename("pct_b")
    out = pa.rename(columns={"pct_validos": "pct_a"}).join(pb, how="inner").reset_index()
    out["margem_pp"] = out["pct_a"] - out["pct_b"]
    return out.sort_values("tse_ts")[cols].reset_index(drop=True)


def line_styles(top: pd.DataFrame) -> dict[str, tuple[str, str]]:
    """sqcand -> (cor, traço). Candidatos com a mesma cor (mesmo partido) recebem traços diferentes."""
    seen: dict[str, int] = {}
    out: dict[str, tuple[str, str]] = {}
    for sq, partido in zip(top["sqcand"], top["partido"]):
        color = party_color(partido)
        k = seen.get(color, 0)
        seen[color] = k + 1
        out[sq] = (color, DASHES[k % len(DASHES)])
    return out


def pace(totals: pd.DataFrame) -> pd.DataFrame:
    """Ritmo da apuração: tse_ts, pct_secoes e comparecimento_pct (% do eleitorado das seções apuradas)."""
    if totals.empty:
        return pd.DataFrame(columns=["tse_ts", "pct_secoes", "comparecimento_pct"])
    t = totals[["tse_ts", "pct_secoes", "comparecimento", "eleitorado_apurado"]].copy()
    apur = t["eleitorado_apurado"].where(t["eleitorado_apurado"] > 0)
    t["comparecimento_pct"] = 100 * t["comparecimento"] / apur
    return t.sort_values("tse_ts")[["tse_ts", "pct_secoes", "comparecimento_pct"]].reset_index(drop=True)


# ----------------------------------------------------------------------------- leitura (com cache)

def _cargos(cargo: int) -> tuple[int, ...]:
    return (7, 8) if cargo in (7, 8) else (cargo,)


def _stamp(conn, eleicao: str, cargo: int, nivel: str, uf: str) -> tuple:
    cs = _cargos(cargo)
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    r = conn.execute(
        f"SELECT MAX(id), COUNT(*) FROM snapshot WHERE eleicao=? AND cargo IN ({','.join('?' * len(cs))}) "
        "AND nivel=? AND uf=? AND mun=''", [eleicao, *cs, nivel, uf]).fetchone()
    return db, r[0], r[1]


@st.cache_data(ttl=600, max_entries=48, show_spinner=False)
def _load(_conn, stamp: tuple, eleicao: str, cargo: int, nivel: str, uf: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    return (store.history(_conn, eleicao, cargo, nivel, uf),
            store.history_totals(_conn, eleicao, cargo, nivel, uf))


# ----------------------------------------------------------------------------- gráficos

def _chrome() -> dict:
    try:
        kind = st.context.theme.type
    except Exception:
        kind = None
    return _CHROME["dark" if kind == "dark" else "light"]


def _rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def _x_axis(xcol: str, r: pd.DataFrame) -> dict:
    if xcol == "pct_secoes":
        hi = float(r["pct_secoes"].max()) if not r.empty else 100.0
        return dict(title="% das seções apuradas", ticksuffix="%", hoverformat=".2f",
                    range=[0, min(100.0, max(5.0, hi * 1.08))])
    return dict(title="Horário (Brasília)", tickformat="%H:%M", hoverformat="%H:%M:%S")


def _base_layout(fig: go.Figure, height: int, **kw) -> None:
    fig.update_layout(height=height, separators=",.", hovermode="x unified",
                      margin=dict(l=8, r=kw.pop("r", 16), t=kw.pop("t", 28), b=8),
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, title_text=""), **kw)


def _main_fig(r, top, styles, changes, xcol, majority, ch) -> go.Figure:
    fig = go.Figure()
    for row in top.itertuples():
        g = r[r["sqcand"] == row.sqcand]
        color, dash = styles[row.sqcand]
        fig.add_trace(go.Scatter(
            x=g[xcol], y=g["pct_validos"], mode="lines", name=f"{row.nome_urna} ({row.partido})",
            line=dict(color=color, width=2, dash=dash, shape="linear"),
            hovertemplate="%{y:.2f}%"))
    if majority:
        fig.add_hline(y=50, line=dict(color=ch["muted"], width=1), annotation_text="50% dos válidos",
                      annotation_position="top left", annotation_font=dict(color=ch["ink2"], size=11))
    if not changes.empty:
        fig.add_trace(go.Scatter(
            x=changes[xcol], y=changes["pct_validos"], mode="markers", name="Virada",
            marker=dict(symbol="diamond", size=12, color=ch["ink"], line=dict(width=2, color=ch["surface"])),
            customdata=changes[["nome_urna", "nome_ultrapassado"]],
            hovertemplate="virada: %{customdata[0]} passa %{customdata[1]}<extra></extra>"))
        last = changes.iloc[-1]
        fig.add_shape(type="line", xref="x", yref="paper", x0=last[xcol], x1=last[xcol], y0=0, y1=1,
                      line=dict(color=ch["muted"], width=1))
        fig.add_annotation(x=last[xcol], y=last["pct_validos"], text=f"{last['nome_urna']} passa "
                           f"{last['nome_ultrapassado']}", showarrow=True, arrowhead=0, arrowcolor=ch["muted"],
                           ax=0, ay=-34, font=dict(color=ch["ink"], size=12), bgcolor=_rgba(ch["surface"], 0.85))
    # rótulos diretos no fim das duas primeiras linhas (texto em tinta, não na cor do partido)
    ends = []
    for row in top.head(2).itertuples():
        g = r[r["sqcand"] == row.sqcand]
        if not g.empty:
            ends.append((g[xcol].iloc[-1], float(g["pct_validos"].iloc[-1]), row.nome_urna))
    for i, (x, y, name) in enumerate(ends):
        other = ends[1 - i][1] if len(ends) == 2 else y
        short = name if len(name) <= 16 else name[:15] + "…"
        fig.add_annotation(x=x, y=y, text=f"{short} {fmt_pct(y)}", showarrow=False, xanchor="left", xshift=6,
                           yshift=(9 if y >= other else -9) if len(ends) == 2 else 0,
                           font=dict(color=ch["ink2"], size=12))
    ymax = float(r.loc[r["sqcand"].isin(top["sqcand"]), "pct_validos"].max() or 0)
    yrange = [0, max(55.0, ymax * 1.08)] if majority else [0, max(1.0, ymax * 1.12)]
    _base_layout(fig, 460, r=150, xaxis=_x_axis(xcol, r),
                 yaxis=dict(title="% dos votos válidos", ticksuffix="%", range=yrange, hoverformat=".2f"))
    return fig


def _margin_fig(m, xcol, color_a, color_b, ch) -> go.Figure:
    fig = go.Figure()
    pos, neg = m["margem_pp"].clip(lower=0), m["margem_pp"].clip(upper=0)
    fig.add_trace(go.Scatter(x=m[xcol], y=pos, mode="lines", line=dict(width=0), fill="tozeroy",
                             fillcolor=_rgba(color_a, 0.16), hoverinfo="skip", showlegend=False))
    fig.add_trace(go.Scatter(x=m[xcol], y=neg, mode="lines", line=dict(width=0), fill="tozeroy",
                             fillcolor=_rgba(color_b, 0.16), hoverinfo="skip", showlegend=False))
    fig.add_trace(go.Scatter(x=m[xcol], y=m["margem_pp"], mode="lines", name="Vantagem",
                             line=dict(color=ch["ink2"], width=2), hovertemplate="%{y:+.2f} pp<extra></extra>",
                             showlegend=False))
    fig.add_hline(y=0, line=dict(color=ch["base"], width=1))
    lo, hi = float(min(0.0, m["margem_pp"].min())), float(max(0.0, m["margem_pp"].max()))
    pad = max(0.5, (hi - lo) * 0.1)
    _base_layout(fig, 300, t=12, xaxis=_x_axis(xcol, m),
                 yaxis=dict(title="pontos percentuais", ticksuffix=" pp", range=[lo - pad, hi + pad]))
    return fig


def _pace_fig(p, show_comp, ch) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=p["tse_ts"], y=p["pct_secoes"], mode="lines", name="Seções apuradas",
                             line=dict(color=ch["s1"], width=2), hovertemplate="%{y:.2f}%"))
    if show_comp:
        c = p.dropna(subset=["comparecimento_pct"])
        fig.add_trace(go.Scatter(x=c["tse_ts"], y=c["comparecimento_pct"], mode="lines",
                                 name="Comparecimento (seções apuradas)", line=dict(color=ch["s2"], width=2),
                                 hovertemplate="%{y:.2f}%"))
    _base_layout(fig, 300, t=12 if not show_comp else 28, showlegend=show_comp,
                 xaxis=dict(title="Horário (Brasília)", tickformat="%H:%M", hoverformat="%H:%M:%S"),
                 yaxis=dict(title="%", ticksuffix="%", range=[0, 102]))
    return fig


# ----------------------------------------------------------------------------- tela

def render(ctx: ViewContext) -> None:
    try:
        _render(ctx)
    except Exception:  # nunca deixar a exceção chegar ao usuário
        log.exception("evolucao: falha ao renderizar")
        no_data(ERROR_MSG)


def _render(ctx: ViewContext) -> None:
    st.subheader("Evolução da apuração")
    proportional = ctx.cargo in config.CARGOS_PROPORCIONAIS
    c_uf, c_n, c_x, c_comp = st.columns([1.3, 1, 1.6, 1.4], vertical_alignment="bottom")
    uf_choice = None
    if ctx.uf == config.BRASIL and ctx.cargo != 1:
        uf_choice = c_uf.selectbox("Estado", config.UF_LIST, index=config.UF_LIST.index("rs"),
                                   format_func=uf_name, key="evo_uf",
                                   help="Só Presidente tem totalização nacional; os demais cargos são por estado.")
    nivel, uf = resolve_area(ctx.cargo, ctx.uf, uf_choice)
    n = int(c_n.number_input("Candidatos no gráfico", min_value=2, max_value=20, value=10 if proportional else 5,
                             step=1, key=f"evo_n_{'prop' if proportional else 'maj'}"))
    xmode = c_x.radio("Eixo horizontal", [X_PCT, X_TIME], horizontal=True, key="evo_x")
    show_comp = c_comp.toggle("Comparecimento no ritmo", value=False, key="evo_comp")
    xcol = "tse_ts" if xmode == X_TIME else "pct_secoes"

    area = "Brasil" if nivel == "br" else uf_name(uf)
    if ctx.mun:
        st.caption(f"Municípios não têm histórico: mostrando a evolução de {area}.")

    stamp = _stamp(ctx.conn, ctx.eleicao, ctx.cargo, nivel, uf)
    if not stamp[2]:
        no_data()
        return
    hist, totals = _load(ctx.conn, stamp, ctx.eleicao, ctx.cargo, nivel, uf)
    r = ranked(hist)
    if r.empty:
        no_data()
        return

    vagas = totals["vagas"].dropna()
    seats = 1 if proportional or vagas.empty else max(1, int(vagas.iloc[-1]))
    top = top_candidates(hist, n)
    pair = top_candidates(hist, seats + 1)
    changes = lead_changes(hist, seats)
    styles = line_styles(top)
    ch = _chrome()
    last_pct = float(r["pct_secoes"].iloc[-1])
    n_snap = r["snapshot_id"].nunique()

    st.caption(f"{ctx.cargo_nome} · {area} · {n_snap} boletins com votos desde "
               f"{r['tse_ts'].iloc[0]:%H:%M} · último às {r['tse_ts'].iloc[-1]:%H:%M:%S}")

    # números-resumo
    m1, m2, m3, m4 = st.columns(4)
    p = pace(counted_totals(totals))
    m1.metric("Seções apuradas", fmt_pct(last_pct), chart_data=p["pct_secoes"].round(2).tolist() or None,
              chart_type="area")
    lead = top.iloc[0]
    m2.metric("Lidera", f"{lead['nome_urna']} ({lead['partido']})", fmt_pct(lead["pct_validos"]),
              delta_color="off", delta_arrow="off")
    if len(pair) > seats:
        a, b = pair.iloc[seats - 1], pair.iloc[seats]
        label = "Vantagem sobre o 2º" if seats == 1 else f"Vantagem na {seats}ª vaga"
        m3.metric(label, f"{fmt_pct(a['pct_validos'] - b['pct_validos'])[:-1]} pp",
                  f"sobre {b['nome_urna']}", delta_color="off", delta_arrow="off")
    m4.metric("Viradas na noite", len(changes))

    title = "% dos votos válidos" + (" — candidatos mais votados" if proportional else "")
    st.markdown(f"**{title}** · top {len(top)}")
    st.plotly_chart(_main_fig(r, top, styles, changes, xcol, ctx.cargo in (1, 3), ch), width="stretch",
                    key="evo_main")

    left, right = st.columns(2)
    with left:
        if len(pair) > seats:
            a, b = pair.iloc[seats - 1], pair.iloc[seats]
            what = "1º e 2º" if seats == 1 else f"{seats}º e {seats + 1}º (disputa pela última vaga)"
            st.markdown(f"**Vantagem entre {what}**")
            st.caption(f"Acima de zero: {a['nome_urna']} à frente; abaixo: {b['nome_urna']} à frente.")
            m = margin_series(hist, a["sqcand"], b["sqcand"])
            st.plotly_chart(_margin_fig(m, xcol, party_color(a["partido"]), party_color(b["partido"]), ch),
                            width="stretch", key="evo_gap")
        else:
            st.caption("Só há um candidato com votos nesta disputa.")
    with right:
        st.markdown("**Ritmo da apuração**")
        st.caption("% das seções totalizadas ao longo da noite" +
                   ("; comparecimento = % do eleitorado das seções já apuradas." if show_comp else "."))
        st.plotly_chart(_pace_fig(p, show_comp, ch), width="stretch", key="evo_pace")

    st.markdown("**Viradas**" if seats == 1 else f"**Mudanças entre os {seats} primeiros (vagas)**")
    if changes.empty:
        st.caption(f"Nenhuma virada até agora: {lead['nome_urna']} lidera desde o primeiro boletim com votos."
                   if seats == 1 else "Nenhuma mudança entre os que ocupam as vagas até agora.")
    else:
        tbl = pd.DataFrame({
            "Horário": changes["tse_ts"].dt.strftime("%H:%M:%S"),
            "Seções apuradas": changes["pct_secoes"].map(fmt_pct),
            "Passou à frente": changes["nome_urna"] + " (" + changes["partido"] + ")",
            "Ultrapassado": changes["nome_ultrapassado"] + " (" + changes["partido_ultrapassado"] + ")",
            "Vantagem após (pp)": changes["margem_pp"].map(lambda v: fmt_pct(v)[:-1]),
        })
        st.dataframe(tbl.iloc[::-1], hide_index=True, width="stretch", key="evo_viradas")
        if len(changes) > 1:
            st.caption("Viradas com poucas seções apuradas costumam refletir só a ordem de chegada das urnas.")


def counted_totals(totals: pd.DataFrame) -> pd.DataFrame:
    """Totais a partir do primeiro boletim com seções apuradas (descarta os zerados antes das 17h)."""
    if totals.empty:
        return totals
    started = totals["pct_secoes"].fillna(0).gt(0).cummax()
    return totals[started]
