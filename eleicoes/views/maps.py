"""Figuras de mapa (plotly choropleth com GeoJSON puro, sem geopandas) compartilhadas pelas telas.

- leader_map: cor do partido do líder, mais forte quanto maior a margem.
- value_map: escala contínua para um valor numérico (ex. % de um candidato, abstenção, swing).
Os DataFrames precisam de uma coluna de localização: 'uf' (mapa de estados) ou 'mun' (mapa de municípios),
casando com feature.id de geo.states_geojson() / geo.municipios_geojson(uf).
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from .. import config
from . import brand
from .common import party_color


NO_DATA_COLOR = "#C9CED6"  # sem votos apurados (tema claro); no escuro usa brand.tokens()["vazio"]


def _no_data() -> str:
    return brand.tokens()["vazio"]


def _border() -> str:
    """Divisas na cor da tela: as áreas parecem recortadas do fundo."""
    return brand.tokens()["tela"]


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def blend(hex_color: str, strength: float, base: str = "#FFFFFF") -> str:
    """Mistura a cor com o fundo: strength 0 -> base, 1 -> cor cheia."""
    s = max(0.0, min(1.0, strength))
    c, b = _hex_to_rgb(hex_color), _hex_to_rgb(base)
    return "#%02X%02X%02X" % tuple(round(bb + (cc - bb) * s) for cc, bb in zip(c, b))


def _layout(fig: go.Figure, height: int) -> go.Figure:
    fig.update_geos(fitbounds="locations", visible=False, projection_type="mercator")
    fig.update_layout(height=height, margin=dict(l=0, r=0, t=0, b=0), dragmode=False,
                      paper_bgcolor="rgba(0,0,0,0)", geo_bgcolor="rgba(0,0,0,0)")
    return fig


def leader_map(df: pd.DataFrame, geojson: dict, *, loc: str = "uf", party_col: str = "partido",
               margin_col: str = "margin_pp", hover_col: str | None = None, height: int = 560,
               full_margin_pp: float = 30.0) -> go.Figure:
    """Mapa por líder. Áreas sem voto (party nulo) ficam cinza claro.

    Intensidade: margem 0 pp -> 35% da cor; margem >= full_margin_pp -> cor cheia.
    `hover_col`: coluna com texto pronto para o tooltip (HTML simples permitido).
    """
    df = df.reset_index(drop=True)
    colors = []
    margins = pd.to_numeric(df[margin_col], errors="coerce").fillna(0)
    for p, m in zip(df[party_col], margins):
        if p is None or (isinstance(p, float) and pd.isna(p)):
            colors.append(_no_data())
        else:
            colors.append(blend(party_color(p), 0.35 + 0.65 * min(1.0, float(m) / full_margin_pp)))
    n = max(len(colors), 1)
    scale = []
    for i, c in enumerate(colors):  # escala discreta: cada área recebe a própria cor
        lo, hi = i / n, (i + 1) / n
        scale += [[lo, c], [hi, c]]
    fig = go.Figure(go.Choropleth(
        geojson=geojson, locations=df[loc], z=[(i + 0.5) / n for i in range(len(df))], zmin=0, zmax=1,
        colorscale=scale or [[0, _no_data()], [1, _no_data()]], showscale=False,
        marker_line_color=_border(), marker_line_width=0.7,
        text=df[hover_col] if hover_col else None, hovertemplate="%{text}<extra></extra>" if hover_col else None,
    ))
    return _layout(fig, height)


def value_map(df: pd.DataFrame, geojson: dict, value_col: str, *, loc: str = "uf", hover_col: str | None = None,
              colorscale: str | list = "Blues", zmin: float | None = None, zmax: float | None = None,
              zmid: float | None = None, colorbar_title: str = "", height: int = 560,
              nan_color: str | None = "auto", line_color: str | None = None) -> go.Figure:
    """Mapa contínuo. Use zmid (ex. 0) com escala divergente ('RdBu') para swing.

    Áreas com valor NaN (ex. nada apurado) são desenhadas em `nan_color` (None = omitidas).
    """
    nan_color = _no_data() if nan_color == "auto" else nan_color
    line_color = line_color or _border()
    fig = go.Figure()
    isnan = df[value_col].isna()
    if nan_color and isnan.any():
        nd = df[isnan]
        fig.add_trace(go.Choropleth(
            geojson=geojson, locations=nd[loc], z=[0] * len(nd), colorscale=[[0, nan_color], [1, nan_color]],
            showscale=False, marker_line_color=line_color, marker_line_width=0.6,
            text=nd[hover_col] if hover_col else None,
            hovertemplate="%{text}<extra></extra>" if hover_col else "%{location}: sem dados<extra></extra>",
        ))
    vd = df[~isnan]
    fig.add_trace(go.Choropleth(
        geojson=geojson, locations=vd[loc], z=vd[value_col], zmin=zmin, zmax=zmax, zmid=zmid,
        colorscale=colorscale, marker_line_color=line_color, marker_line_width=0.6,
        colorbar=dict(title=colorbar_title, thickness=12, len=0.6),
        text=vd[hover_col] if hover_col else None, hovertemplate="%{text}<extra></extra>" if hover_col else None,
    ))
    return _layout(fig, height)


def neutral_color() -> str:
    return config.NEUTRAL_COLOR
