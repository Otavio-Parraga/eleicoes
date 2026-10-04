"""Contrato compartilhado entre app.py e as telas (views/*.py).

Toda tela expõe `render(ctx: ViewContext) -> None` e lê dados só via eleicoes.store.
"""
from __future__ import annotations

import sqlite3
import zlib
from dataclasses import dataclass

import streamlit as st

from .. import config

# Chaves de st.session_state usadas por mais de uma tela
K_TURNO, K_CARGO, K_UF, K_MUN, K_CAND, K_SOURCE = "turno", "cargo", "uf", "mun", "cand_sqcand", "source"

_FALLBACK = ["#7E57C2", "#26A69A", "#8D6E63", "#EC407A", "#5C6BC0", "#9CCC65", "#FFA726", "#78909C"]


@dataclass
class ViewContext:
    conn: sqlite3.Connection
    turno: int        # 1 ou 2
    cargo: int        # cargo da interface: 1, 3, 5, 6 ou 7 (7 = Estadual/Distrital; store casa 7 e 8)
    uf: str           # 'br' (visão nacional) ou sigla minúscula ('rs', 'zz')
    mun: str | None = None   # código TSE do município selecionado (opcional)
    refresh_s: int = 30

    @property
    def eleicao(self) -> str:
        return config.ELECTIONS[(self.turno, self.cargo)]

    @property
    def cargo_nome(self) -> str:
        return config.UI_CARGOS.get(self.cargo, config.CARGOS.get(self.cargo, str(self.cargo)))

    @property
    def is_brasil(self) -> bool:
        return self.uf == config.BRASIL

    @property
    def area_nome(self) -> str:
        return uf_name(self.uf)


def available(turno: int, cargo: int) -> bool:
    return (turno, cargo) in config.ELECTIONS


def uf_name(uf: str) -> str:
    if uf == config.BRASIL:
        return "Brasil"
    if uf == config.EXTERIOR:
        return "Exterior"
    return config.UFS.get(uf, (uf.upper(),))[0]


def party_color(sigla: str | None) -> str:
    if not sigla:
        return config.NEUTRAL_COLOR
    if sigla in config.PARTY_COLORS:
        return config.PARTY_COLORS[sigla]
    return _FALLBACK[zlib.crc32(sigla.encode()) % len(_FALLBACK)]


def fmt_int(n) -> str:
    try:
        return f"{int(n):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "–"


def fmt_pct(x, nd: int = 2) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "–"
    return "–" if v != v else f"{v:.{nd}f}%".replace(".", ",")  # v != v: NaN


def select_uf(uf: str) -> None:
    """Muda o estado selecionado (ex.: clique no mapa) e reroda o app inteiro."""
    st.session_state[K_UF] = uf
    st.session_state[K_MUN] = None
    st.rerun(scope="app")


def no_data(msg: str = "Ainda não há dados para esta seleção. O TSE começa a divulgar às 17h (Brasília).") -> None:
    st.info(msg)
