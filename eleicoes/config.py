"""Constantes compartilhadas. Somente o orquestrador edita este arquivo."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"          # arquivo gzip dos JSON do TSE (nível br/uf)
GEO_DIR = DATA_DIR / "geo"          # GeoJSON do IBGE + config de municípios do TSE
HIST_DIR = DATA_DIR / "2022"        # dados abertos do TSE de 2022 (fase 4)
DB_PATH = Path(os.environ.get("ELEICOES_DB", DATA_DIR / "eleicoes.db"))
SIM_DB_PATH = DATA_DIR / "sim.db"   # banco gerado pelo simulador (tools/simulate.py)

TSE_BASE = "https://resultados.tse.jus.br/oficial"
CICLO = "ele2026"
ELE_CONFIG_URL = f"{TSE_BASE}/comum/config/ele-c.json"

# Códigos verificados em ele-c.json em 2026-10-04.
# (turno, cargo) -> código da eleição
ELECTIONS: dict[tuple[int, int], str] = {
    (1, 1): "6257", (1, 3): "6259", (1, 5): "6259", (1, 6): "6259", (1, 7): "6259", (1, 8): "6259",
    (2, 1): "6258", (2, 3): "6260",
}

CARGOS: dict[int, str] = {
    1: "Presidente",
    3: "Governador",
    5: "Senador",
    6: "Deputado Federal",
    7: "Deputado Estadual",
    8: "Deputado Distrital",
}
# Cargos oferecidos na interface. 7 = "Deputado Estadual/Distrital" (DF usa 8).
UI_CARGOS: dict[int, str] = {
    1: "Presidente", 3: "Governador", 5: "Senador", 6: "Deputado Federal", 7: "Deputado Estadual/Distrital",
}
CARGOS_TURNO2 = (1, 3)
CARGOS_MAJORITARIOS = (1, 3, 5)
CARGOS_PROPORCIONAIS = (6, 7, 8)

# sigla minúscula -> (nome, código IBGE da UF, região)
UFS: dict[str, tuple[str, str, str]] = {
    "ac": ("Acre", "12", "Norte"),
    "al": ("Alagoas", "27", "Nordeste"),
    "ap": ("Amapá", "16", "Norte"),
    "am": ("Amazonas", "13", "Norte"),
    "ba": ("Bahia", "29", "Nordeste"),
    "ce": ("Ceará", "23", "Nordeste"),
    "df": ("Distrito Federal", "53", "Centro-Oeste"),
    "es": ("Espírito Santo", "32", "Sudeste"),
    "go": ("Goiás", "52", "Centro-Oeste"),
    "ma": ("Maranhão", "21", "Nordeste"),
    "mt": ("Mato Grosso", "51", "Centro-Oeste"),
    "ms": ("Mato Grosso do Sul", "50", "Centro-Oeste"),
    "mg": ("Minas Gerais", "31", "Sudeste"),
    "pa": ("Pará", "15", "Norte"),
    "pb": ("Paraíba", "25", "Nordeste"),
    "pr": ("Paraná", "41", "Sul"),
    "pe": ("Pernambuco", "26", "Nordeste"),
    "pi": ("Piauí", "22", "Nordeste"),
    "rj": ("Rio de Janeiro", "33", "Sudeste"),
    "rn": ("Rio Grande do Norte", "24", "Nordeste"),
    "rs": ("Rio Grande do Sul", "43", "Sul"),
    "ro": ("Rondônia", "11", "Norte"),
    "rr": ("Roraima", "14", "Norte"),
    "sc": ("Santa Catarina", "42", "Sul"),
    "sp": ("São Paulo", "35", "Sudeste"),
    "se": ("Sergipe", "28", "Nordeste"),
    "to": ("Tocantins", "17", "Norte"),
}
UF_LIST: list[str] = sorted(UFS)
IBGE_TO_UF: dict[str, str] = {v[1]: k for k, v in UFS.items()}
EXTERIOR = "zz"  # "UF" do voto no exterior (só Presidente)
BRASIL = "br"

DEFAULT_FOCUS = ["rs"]

# Coleta
TIER1_INTERVAL_S = 60    # arquivos br/uf (CDN do TSE usa max-age=60)
TIER2_INTERVAL_S = 180   # varredura de municípios dos estados em foco
HTTP_CONCURRENCY = 8
HTTP_TIMEOUT_S = 20

# Cores convencionais por sigla de partido (aproximadas). Fallback: views.common.party_color.
PARTY_COLORS: dict[str, str] = {
    "PT": "#C8102E", "PC do B": "#8B0000", "PCdoB": "#8B0000", "PCDOB": "#8B0000", "PV": "#2E8B57",
    "DEMOCRATA": "#4E5BA6", "PRTB": "#8D6E63",
    "PL": "#0B3D91", "PSD": "#E8A317", "MDB": "#2F9E44", "UNIÃO": "#1C75BC", "PP": "#6CA6DC",
    "REPUBLICANOS": "#0E7C86", "PSDB": "#4A6FA5", "PSB": "#F28C28", "PDT": "#E4572E",
    "PSOL": "#F4C20D", "REDE": "#00A19A", "NOVO": "#FF6600", "PODE": "#7FBA00",
    "SOLIDARIEDADE": "#FF9F1C", "PRD": "#3D3D8F", "CIDADANIA": "#EC008C", "AVANTE": "#00B5E2",
    "AGIR": "#5C2D91", "DC": "#1E90FF", "MOBILIZA": "#2C3E50", "PMB": "#9B59B6",
    "PCO": "#A50021", "PSTU": "#E30613", "PCB": "#B22222", "UP": "#7A0019", "MISSÃO": "#D4A017",
}
NEUTRAL_COLOR = "#9AA0A6"
