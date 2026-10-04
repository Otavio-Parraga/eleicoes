"""Malhas do IBGE (GeoJSON) e tabela de municípios do TSE, baixadas uma vez e guardadas em data/geo/."""
from __future__ import annotations

import json
from functools import lru_cache

import httpx
import pandas as pd

from . import config

IBGE = "https://servicodados.ibge.gov.br/api/v3/malhas"
STATES_URL = f"{IBGE}/paises/BR?formato=application/vnd.geo%2Bjson&qualidade=minima&intrarregiao=UF"
MUN_URL = IBGE + "/estados/{ibge}?formato=application/vnd.geo%2Bjson&qualidade=intermediaria&intrarregiao=municipio"
TSE_MUN_URL = f"{config.TSE_BASE}/{config.CICLO}/6257/config/mun-e006257-cm.json"


def _signed_area(ring: list) -> float:
    return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:])) / 2


def rewind_for_d3(gj: dict) -> dict:
    """Ajusta a ordem dos anéis para o d3-geo (usado por go.Choropleth): externo horário, buracos anti-horários.

    O IBGE segue a RFC 7946 (externo anti-horário); sem isso o d3 pinta o complemento de cada polígono
    e o mapa inteiro vira um bloco de uma cor só.
    """
    for f in gj["features"]:
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"] if g["type"] == "MultiPolygon" else []
        for poly in polys:
            for i, ring in enumerate(poly):
                if (_signed_area(ring) > 0) == (i == 0):  # externo anti-horário ou buraco horário
                    ring.reverse()
    return gj


def _cached_json(name: str, url: str) -> dict:
    path = config.GEO_DIR / name
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        r = httpx.get(url, timeout=120, follow_redirects=True)
        r.raise_for_status()
        path.write_bytes(r.content)
    return json.loads(path.read_text())


@lru_cache(maxsize=1)
def tse_municipios_raw() -> dict:
    return _cached_json("mun-e006257-cm.json", TSE_MUN_URL)


@lru_cache(maxsize=1)
def municipios() -> pd.DataFrame:
    """Todos os municípios (inclui 'zz' = cidades no exterior, sem código IBGE).

    Colunas: uf, mun (código TSE 5 dígitos), ibge (7 dígitos ou ''), nome, capital (bool), zonas (int).
    """
    rows = []
    for abr in tse_municipios_raw()["abr"]:
        for m in abr["mu"]:
            rows.append((abr["cd"], m["cd"], m.get("cdi", ""), m["nm"], m.get("c") == "s", len(m.get("z", []))))
    return pd.DataFrame(rows, columns=["uf", "mun", "ibge", "nome", "capital", "zonas"])


def municipios_uf(uf: str) -> pd.DataFrame:
    df = municipios()
    return df[df["uf"] == uf.lower()].reset_index(drop=True)


@lru_cache(maxsize=1)
def states_geojson() -> dict:
    """GeoJSON das 27 UFs. Cada feature tem id = sigla minúscula ('rs') e properties {uf, nome, ibge}."""
    gj = rewind_for_d3(_cached_json("br_uf.json", STATES_URL))
    for f in gj["features"]:
        uf = config.IBGE_TO_UF[f["properties"]["codarea"]]
        f["id"] = uf
        f["properties"].update(uf=uf, nome=config.UFS[uf][0], ibge=f["properties"]["codarea"])
    return gj


@lru_cache(maxsize=32)
def municipios_geojson(uf: str) -> dict:
    """GeoJSON dos municípios de uma UF. Cada feature tem id = código TSE do município ('88013')
    e properties {mun, ibge, nome, uf}."""
    uf = uf.lower()
    gj = rewind_for_d3(_cached_json(f"mun_{uf}.json", MUN_URL.format(ibge=config.UFS[uf][1])))
    by_ibge = municipios_uf(uf).set_index("ibge")
    for f in gj["features"]:
        ibge = f["properties"]["codarea"]
        if ibge in by_ibge.index:
            row = by_ibge.loc[ibge]
            f["id"] = row["mun"]
            f["properties"].update(mun=row["mun"], ibge=ibge, nome=row["nome"], uf=uf)
        else:
            f["id"] = f"ibge:{ibge}"
            f["properties"].update(mun="", ibge=ibge, nome="", uf=uf)
    return gj


def warm_cache(ufs: list[str] | None = None) -> None:
    """Baixa tudo de antemão (estados + municípios das UFs pedidas, ou de todas)."""
    states_geojson()
    municipios()
    for uf in ufs or config.UF_LIST:
        municipios_geojson(uf)


if __name__ == "__main__":
    warm_cache()
    print("ok:", sorted(p.name for p in config.GEO_DIR.iterdir()))
