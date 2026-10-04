"""Converte arquivos "-u.json" do TSE (mesmo formato nos níveis br, uf e município) em Snapshots.

Funções puras: sem rede, sem banco.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


def br_int(s: str | int | None) -> int:
    """'1.234' -> 1234; '' / None -> 0."""
    if s is None or s == "":
        return 0
    if isinstance(s, int):
        return s
    return int(str(s).replace(".", "").strip() or 0)


def br_float(s: str | float | None) -> float:
    """'1.234,56' -> 1234.56; '' / None -> 0.0."""
    if s is None or s == "":
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    return float(str(s).replace(".", "").replace(",", ".").strip() or 0)


def parse_tse_ts(dg: str, hg: str) -> datetime | None:
    """('02/10/2026', '18:30:57') -> datetime ingênuo no horário de Brasília."""
    if not dg or not hg:
        return None
    return datetime.strptime(f"{dg} {hg}", "%d/%m/%Y %H:%M:%S")


@dataclass(frozen=True)
class Candidate:
    numero: str          # número na urna, ex. '13', '1234'
    sqcand: str          # id único do candidato no TSE
    nome: str
    nome_urna: str
    partido: str         # sigla do partido, ex. 'PT'
    partido_numero: str  # ex. '13'
    federacao: str       # sigla da federação (ex. 'PT/PC do B/PV') ou ''
    agremiacao: str      # composição da agremiação/coligação (agr.com), ex. 'PSOL / REDE' ou 'PL'
    votos: int           # vap
    pct_validos: float   # pvap, 0-100
    eleito: bool         # e == 's'
    situacao: str        # st, ex. 'Eleito', '2º turno', 'Eleito por QP', 'Suplente', '' durante a apuração
    vice: str            # nome de urna do vice/1º suplente ('' se não houver)


@dataclass(frozen=True)
class Party:
    numero: str
    sigla: str
    federacao: str       # sigla da federação ou ''
    agremiacao: str      # agr.com
    agremiacao_tipo: str # 'i' (isolado), 'f' (federação), 'c' (coligação)
    votos_nominais: int  # tvtn
    votos_legenda: int   # tvtl
    vagas_agremiacao: int  # agr.vag (vagas da agremiação inteira; repetido para cada partido dela)


@dataclass(frozen=True)
class Totals:
    secoes_total: int
    secoes_totalizadas: int
    pct_secoes: float          # 0-100
    eleitorado: int
    eleitorado_apurado: int    # eleitores das seções já totalizadas
    comparecimento: int
    abstencao: int
    votos_total: int
    validos: int
    nominais: int
    legenda: int
    brancos: int
    nulos: int                 # tvn (inclui nulos técnicos)
    anulados: int              # van (anulados por decisão judicial)


@dataclass(frozen=True)
class Snapshot:
    eleicao: str               # ex. '6257'
    turno: int
    cargo: int                 # 1,3,5,6,7,8
    cargo_nome: str
    vagas: int                 # nv
    nivel: str                 # 'br' | 'uf' | 'mu'
    uf: str                    # 'br' no nível br; sigla minúscula ('rs', 'zz') nos demais
    mun: str                   # código TSE de 5 dígitos no nível 'mu'; '' nos demais
    tse_ts: datetime | None    # data/hora de geração do arquivo (dg + hg), Brasília
    finalizada: bool           # tf == 's'
    totals: Totals
    candidates: tuple[Candidate, ...] = field(default_factory=tuple)
    parties: tuple[Party, ...] = field(default_factory=tuple)

    @property
    def area_key(self) -> tuple[str, int, str, str, str]:
        return (self.eleicao, self.cargo, self.nivel, self.uf, self.mun)


def _uf_mun(doc: dict) -> tuple[str, str, str]:
    tpabr = doc.get("tpabr", "")
    cdabr = str(doc.get("cdabr", "")).lower()
    if tpabr == "br":
        return "br", "br", ""
    if tpabr == "uf":
        return "uf", cdabr, ""
    if tpabr == "mu":
        return "mu", "", cdabr  # uf é preenchida pelo chamador (não vem no arquivo)
    raise ValueError(f"tpabr desconhecido: {tpabr!r}")


def parse_u(doc: dict, uf: str | None = None) -> Snapshot:
    """Converte o JSON de um arquivo '-u.json' em Snapshot.

    `uf` é obrigatório para arquivos de município (tpabr == 'mu'), pois o arquivo não traz a UF.
    """
    nivel, uf_doc, mun = _uf_mun(doc)
    if nivel == "mu":
        if not uf:
            raise ValueError("arquivo de município exige o parâmetro uf")
        uf_doc = uf.lower()
    cargs = doc.get("carg") or []
    if not cargs:
        raise ValueError("arquivo sem 'carg'")
    cg = cargs[0]

    fed_sigla = {f["n"]: f.get("sg", "") for f in cg.get("fed", [])}
    candidates: list[Candidate] = []
    parties: list[Party] = []
    for agr in cg.get("agr", []):
        agr_com = agr.get("com") or agr.get("nm", "")
        for par in agr.get("par", []):
            fed = fed_sigla.get(par.get("nfed", ""), "")
            parties.append(Party(
                numero=par.get("n", ""),
                sigla=par.get("sg", ""),
                federacao=fed,
                agremiacao=agr_com,
                agremiacao_tipo=agr.get("tp", ""),
                votos_nominais=br_int(par.get("tvtn")),
                votos_legenda=br_int(par.get("tvtl")),
                vagas_agremiacao=br_int(agr.get("vag")),
            ))
            for c in par.get("cand", []):
                vs = c.get("vs") or []
                candidates.append(Candidate(
                    numero=c.get("n", ""),
                    sqcand=c.get("sqcand", ""),
                    nome=c.get("nm", ""),
                    nome_urna=c.get("nmu", "") or c.get("nm", ""),
                    partido=par.get("sg", ""),
                    partido_numero=par.get("n", ""),
                    federacao=fed,
                    agremiacao=agr_com,
                    votos=br_int(c.get("vap")),
                    pct_validos=br_float(c.get("pvap")),
                    eleito=c.get("e") == "s",
                    situacao=c.get("st", "") or "",
                    vice=(vs[0].get("nmu", "") if vs else ""),
                ))
    candidates.sort(key=lambda c: (-c.votos, c.numero))

    s, e, v = doc.get("s", {}), doc.get("e", {}), doc.get("v", {})
    totals = Totals(
        secoes_total=br_int(s.get("ts")),
        secoes_totalizadas=br_int(s.get("st")),
        pct_secoes=br_float(s.get("pst")),
        eleitorado=br_int(e.get("te")),
        eleitorado_apurado=br_int(e.get("est")),
        comparecimento=br_int(e.get("c")),
        abstencao=br_int(e.get("a")),
        votos_total=br_int(v.get("tv")),
        validos=br_int(v.get("vv")),
        nominais=br_int(v.get("vnom")),
        legenda=br_int(v.get("vl")),
        brancos=br_int(v.get("vb")),
        nulos=br_int(v.get("tvn")),
        anulados=br_int(v.get("van")),
    )
    return Snapshot(
        eleicao=str(doc.get("ele", "")),
        turno=br_int(doc.get("t", "1")),
        cargo=br_int(cg.get("cd")),
        cargo_nome=cg.get("nmn", ""),
        vagas=br_int(cg.get("nv")),
        nivel=nivel,
        uf=uf_doc,
        mun=mun,
        tse_ts=parse_tse_ts(doc.get("dg", ""), doc.get("hg", "")),
        finalizada=doc.get("tf") == "s",
        totals=totals,
        candidates=tuple(candidates),
        parties=tuple(parties),
    )
