"""Identidade visual: o app é a urna eletrônica.

- Barra lateral = teclado da urna (grafite, sempre escuro); área principal = tela de cristal líquido.
- Verde da tecla CONFIRMA = seleção / decidido; laranja da tecla CORRIGE = alerta / rumo ao 2º turno.
- Big Shoulders Display (títulos, como sinalização), Public Sans (texto), Azeret Mono (números, como o boletim).
As cores de tema (fundo, texto, primária) estão em .streamlit/config.toml; aqui ficam os mesmos tokens para o
HTML próprio e o CSS que o tema do Streamlit não alcança.
"""
from __future__ import annotations

import html
from datetime import datetime

import pandas as pd
import streamlit as st

from .common import fmt_pct

TOKENS = {
    "light": dict(tela="#EEF1EC", painel="#E1E7E1", cartao="#FAFBF9", tinta="#16201B", apagado="#5B6660",
                  linha="#CDD5CD", trilho="#DCE2DC", confirma="#1D8A4C", corrige="#CB6418", vazio="#C9CED6"),
    "dark": dict(tela="#0F1411", painel="#1A211C", cartao="#161C18", tinta="#E4EAE5", apagado="#97A39C",
                 linha="#2A332D", trilho="#29322C", confirma="#3DAE6B", corrige="#E8873F", vazio="#2E3631"),
}
TECLADO = dict(fundo="#1B201D", tecla="#232A26", borda="#0A0D0B", texto="#E6ECE7", apagado="#98A39D",
               confirma="#3DAE6B", corrige="#E8873F",
               tecla_confirma="#17773F")  # tecla acesa: verde mais fundo p/ texto branco (contraste ≈ 5,6:1)


# Rampa sequencial "tinta": da cor da tela até o grafite (claro) ou o inverso (escuro). Uma só para o app todo.
_RAMPA = {
    "light": ["#E3E9E3", "#C6D1C8", "#A8B8AC", "#8A9F90", "#6E8676", "#556D5D", "#3E5546", "#2A3E31", "#16201B"],
    "dark": ["#1F2722", "#2F3B33", "#425247", "#586B5E", "#708677", "#8AA091", "#A6B9AB", "#C3D2C7", "#E4EAE5"],
}


def sequential() -> list[list]:
    """Escala contínua da marca para plotly (mapas de %, heatmaps)."""
    r = _RAMPA[mode()]
    return [[i / (len(r) - 1), c] for i, c in enumerate(r)]


def mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:  # noqa: BLE001 - fora do runtime do Streamlit (testes, scripts)
        return "light"


def tokens() -> dict[str, str]:
    return TOKENS[mode()]


def _css() -> str:
    t, k = tokens(), TECLADO
    return f"""<style>
:root{{--tela:{t['tela']};--tinta:{t['tinta']};--apagado:{t['apagado']};--linha:{t['linha']};
 --trilho:{t['trilho']};--confirma:{t['confirma']};--corrige:{t['corrige']}}}
[data-testid="stHeader"]{{background:transparent}}
/* o cabeçalho fixo do Streamlit (3.75rem) fica por cima do conteúdo: o topo precisa passar dele, senão ele
   "come" a parte de cima das teclas e o clique só funciona embaixo delas */
[data-testid="stMainBlockContainer"]{{padding-top:4.5rem;padding-bottom:4rem;max-width:1560px}}

/* números com cara de boletim */
[data-testid="stMetricValue"]{{font-family:'Azeret Mono',ui-monospace,monospace;font-weight:600;
 letter-spacing:-.015em}}
[data-testid="stMetricLabel"] p{{font-family:'Azeret Mono',ui-monospace,monospace;font-size:.66rem;
 font-weight:600;letter-spacing:.12em;text-transform:uppercase;opacity:.75}}
[data-testid="stMetric"]{{border-top:1px solid var(--linha);padding-top:.55rem}}

/* teclas da urna: todo seletor segmentado vira fileira de teclas; a escolhida acende como CONFIRMA */
div[data-testid="stButtonGroup"]:has([data-testid^="stBaseButton-segmented_control"]){{gap:6px;flex-wrap:wrap}}
[data-testid="stBaseButton-segmented_control"],[data-testid="stBaseButton-segmented_controlActive"]{{
 background:{k['tecla']} !important;color:{k['texto']} !important;border:1px solid {k['borda']} !important;
 border-radius:8px !important;margin:0 !important;font-weight:600 !important;min-height:2.35rem;
 padding:0 .95rem !important;box-shadow:inset 0 1px 0 rgba(255,255,255,.08),inset 0 -3px 0 rgba(0,0,0,.55);
 transition:background-color .12s ease,transform .06s ease}}
[data-testid="stBaseButton-segmented_control"]:hover{{background:#2E3632 !important}}
[data-testid="stBaseButton-segmented_control"]:active{{transform:translateY(1px);
 box-shadow:inset 0 1px 0 rgba(255,255,255,.08),inset 0 -1px 0 rgba(0,0,0,.55)}}
[data-testid="stBaseButton-segmented_controlActive"]{{background:{k['tecla_confirma']} !important;
 border-color:#145C33 !important;color:#FFFFFF !important}}
[data-testid^="stBaseButton-segmented_control"]:focus-visible{{outline:2px solid var(--confirma);
 outline-offset:2px}}

/* teclado (barra lateral) */
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p{{font-family:'Azeret Mono',ui-monospace,monospace;
 font-size:.66rem;font-weight:600;letter-spacing:.14em;text-transform:uppercase;color:{k['apagado']}}}
.urna-marca{{padding:.2rem 0 1.1rem;border-bottom:1px solid #2E3632;margin-bottom:.4rem}}
.urna-marca .olho{{font:600 10.5px/1.4 'Azeret Mono',ui-monospace,monospace;letter-spacing:.16em;
 text-transform:uppercase;color:{k['apagado']}}}
.urna-marca .nome{{font:800 44px/.86 'Big Shoulders Display','Arial Narrow',sans-serif;text-transform:uppercase;
 color:{k['texto']};margin-top:.45rem;letter-spacing:.005em}}
.urna-marca .nome b{{display:block;font-weight:800;color:{k['apagado']}}}
.led{{display:grid;grid-template-columns:12px 1fr;gap:2px 10px;align-items:center;padding:.8rem .9rem;
 border-radius:10px;background:{k['tecla']};border:1px solid {k['borda']};
 box-shadow:inset 0 1px 0 rgba(255,255,255,.06)}}
.led i{{width:10px;height:10px;border-radius:50%;background:{k['apagado']};box-shadow:0 0 0 3px rgba(0,0,0,.35)}}
.led.ok i{{background:{k['confirma']};box-shadow:0 0 0 3px rgba(61,174,107,.18),0 0 10px rgba(61,174,107,.55)}}
.led.atraso i{{background:{k['corrige']};box-shadow:0 0 0 3px rgba(232,135,63,.18),0 0 10px rgba(232,135,63,.5)}}
.led span{{font-size:13.5px;font-weight:700;color:{k['texto']}}}
.led small{{grid-column:2;font:500 11px/1.4 'Azeret Mono',ui-monospace,monospace;color:{k['apagado']}}}
@media (prefers-reduced-motion:no-preference){{.led.ok i{{animation:led 2.4s ease-in-out infinite}}}}
@keyframes led{{50%{{opacity:.45}}}}

/* cabeçalho das telas: o topo do boletim */
.cab{{margin:0 0 1.1rem}}
.cab .olho{{font:600 11px/1.4 'Azeret Mono',ui-monospace,monospace;letter-spacing:.16em;text-transform:uppercase;
 color:var(--apagado)}}
.cab .linha{{display:flex;align-items:flex-end;justify-content:space-between;gap:12px 28px;flex-wrap:wrap}}
.cab h1{{font:800 clamp(40px,5.4vw,68px)/.86 'Big Shoulders Display','Arial Narrow',sans-serif !important;
 text-transform:uppercase;letter-spacing:.005em;margin:.35rem 0 0;padding:0;color:var(--tinta)}}
.cab h1 small{{font:700 .42em/1 'Big Shoulders Display',sans-serif;color:var(--apagado);margin-left:.35em;
 letter-spacing:.04em}}
.cab .apur{{min-width:240px;flex:0 1 340px;padding-bottom:4px}}
.cab .apur div{{display:flex;align-items:baseline;gap:8px;font-size:13px;color:var(--apagado)}}
.cab .apur b{{font:600 20px/1 'Azeret Mono',ui-monospace,monospace;color:var(--tinta)}}
.cab .trilho{{height:8px;border-radius:5px;background:var(--trilho);margin-top:8px;overflow:hidden}}
.cab .trilho i{{display:block;height:100%;background:var(--tinta);border-radius:5px}}
.cab .fim{{color:var(--confirma);font-weight:700}}
</style>"""


def inject() -> None:
    """CSS global da identidade (chamar uma vez por execução, fora de fragmentos)."""
    st.html(_css())


def sidebar_mark() -> None:
    st.sidebar.html('<div class="urna-marca"><div class="olho">Apuração ao vivo · dados do TSE</div>'
                    '<div class="nome">Eleições<b>2026</b></div></div>')


def led_html(heartbeat: str | None, last_tse: str | None, now: datetime | None = None) -> str:
    """Luz de status do coletor: verde (ativo), laranja (atrasado), apagada (nunca rodou neste banco)."""
    if not heartbeat:
        return ('<div class="led"><i></i><span>Coletor parado</span>'
                '<small>nenhum ciclo neste banco ainda</small></div>')
    try:
        age = int(((now or datetime.now()) - datetime.fromisoformat(heartbeat)).total_seconds())
    except ValueError:
        age = 10**6
    tse = ""
    if last_tse:
        try:
            tse = f" · TSE {datetime.fromisoformat(last_tse).strftime('%H:%M:%S')}"
        except ValueError:
            pass
    quando = f"último ciclo há {age} s" if age < 3600 else "último ciclo há mais de 1 h"
    if age < 180:
        return f'<div class="led ok"><i></i><span>Coletor ativo</span><small>{quando}{tse}</small></div>'
    return f'<div class="led atraso"><i></i><span>Coletor atrasado</span><small>{quando}{tse}</small></div>'


def header_html(olho: str, titulo: str, sub: str = "", pct: float | None = None, encerrada: bool = False) -> str:
    e = html.escape
    sub_html = f"<small>{e(sub)}</small>" if sub else ""
    apur = ""
    if pct is not None and not pd.isna(pct):
        fim = ' · <span class="fim">apuração encerrada</span>' if encerrada else ""
        apur = (f'<div class="apur"><div><b>{e(fmt_pct(pct))}</b><span>das seções apuradas{fim}</span></div>'
                f'<div class="trilho"><i style="width:{max(0.0, min(100.0, float(pct))):.2f}%"></i></div></div>')
    return (f'<div class="cab"><div class="olho">{e(olho)}</div>'
            f'<div class="linha"><h1>{e(titulo)}{sub_html}</h1>{apur}</div></div>')
