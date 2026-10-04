"""Distribuição de cadeiras no sistema proporcional brasileiro (Câmara, Assembleias, Câmara Legislativa do DF).

Durante a apuração o TSE só marca `eleito` quando a vaga está matematicamente garantida; este módulo faz a
nossa própria projeção com os votos já totalizados. Funções puras (sem banco, sem Streamlit).

Regras implementadas (Código Eleitoral, arts. 106–109 e 111, na redação da Lei 14.211/2021, e decisão do STF nas
ADIs 7228, 7263 e 7325, de 2024, sobre a última fase das sobras):

1. QE (quociente eleitoral, art. 106) = votos válidos do cargo na UF (nominais + legenda) / vagas;
   fração maior que 0,5 arredonda para cima, igual ou menor que 0,5 arredonda para baixo.
2. Votos da agremiação = soma de votos nominais + votos de legenda de todos os seus partidos. Federação
   (Lei 14.208/2021) conta como uma só agremiação; agrupamos pela composição `agremiacao` (agr.com do TSE).
3. QP (quociente partidário, art. 107) = floor(votos da agremiação / QE). As vagas do QP vão para os
   candidatos mais votados da agremiação que tenham pelo menos 10% do QE (art. 108). Vagas do QP sem
   candidato que atinja 10% ficam para as sobras.
4. Sobras (art. 109), uma vaga por vez, pela maior média = votos da agremiação / (cadeiras já obtidas + 1):
   - fase 1: só disputam agremiações com pelo menos 80% do QE que ainda tenham candidato não eleito com
     pelo menos 20% do QE; a vaga vai para esse candidato (o mais votado ainda não eleito);
   - fase 2 (final): quando nenhuma agremiação/candidato satisfaz a fase 1, todas as agremiações disputam
     (STF, 2024), sem exigência de votação mínima; a vaga vai para o mais votado ainda não eleito.
5. Se nenhuma agremiação alcança o QE, elegem-se os candidatos mais votados (art. 111).

Simplificações (documentadas):
- Empate de médias: vence a agremiação com mais votos; depois a ordem alfabética (o TSE aplica a mesma ideia).
  Empate de votos entre candidatos: mantém a ordem de entrada; o TSE desempata pelo mais idoso (art. 110),
  dado que não temos no banco.
- Não trata candidatos sub judice / votos anulados, nem renúncias, cassações ou recontagens.
- Se os votos do partido (tvtn/tvtl do TSE) vierem zerados mas os candidatos tiverem votos (caso dos dados
  sintéticos de `fake.fill_votes`), usa a soma dos votos nominais dos candidatos do partido (sem legenda).
- Se o total de votos válidos não for informado (ou for 0), usa a soma dos votos das agremiações.
- Durante a apuração é só uma projeção: os votos ainda não totalizados podem mudar tudo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

VIA_QP = "QP"
VIA_MEDIA = "média"
VIA_MEDIA_FINAL = "média (fase final)"
VIA_ART111 = "mais votados (art. 111)"


@dataclass(frozen=True)
class CandIn:
    sqcand: str
    nome_urna: str
    partido: str
    agremiacao: str
    votos: int


@dataclass(frozen=True)
class Elected:
    sqcand: str
    nome_urna: str
    partido: str
    agremiacao: str
    votos: int
    via: str  # VIA_QP | VIA_MEDIA | VIA_MEDIA_FINAL | VIA_ART111


@dataclass
class Allocation:
    vagas: int
    validos: int
    qe: int
    agremiacao_votos: dict[str, int] = field(default_factory=dict)
    qp: dict[str, int] = field(default_factory=dict)
    seats_agremiacao: dict[str, int] = field(default_factory=dict)
    seats_party: dict[str, int] = field(default_factory=dict)
    elected: list[Elected] = field(default_factory=list)

    @property
    def filled(self) -> int:
        return len(self.elected)


def quociente_eleitoral(validos: int, vagas: int) -> int:
    """Art. 106: fração > 0,5 arredonda para cima; <= 0,5 para baixo."""
    if vagas <= 0 or validos <= 0:
        return 0
    q, r = divmod(int(validos), int(vagas))
    if 2 * r > vagas:
        q += 1
    return q


def allocate(vagas: int, agremiacao_votos: Mapping[str, int], candidates: Iterable[CandIn],
             validos: int | None = None) -> Allocation:
    """Distribui `vagas` cadeiras entre agremiações e candidatos (ver docstring do módulo).

    - `agremiacao_votos`: votos (nominais + legenda) por agremiação. Agremiações que só aparecem em
      `candidates` recebem a soma dos votos dos seus candidatos.
    - `validos`: votos válidos do cargo na UF; padrão = soma de `agremiacao_votos`.
    """
    cands = list(candidates)
    votos = {a: int(v) for a, v in agremiacao_votos.items()}
    for c in cands:
        if c.agremiacao not in votos:
            votos[c.agremiacao] = 0
    for a in [a for a, v in votos.items() if v <= 0]:
        votos[a] = sum(max(0, c.votos) for c in cands if c.agremiacao == a)
    total = sum(votos.values())
    validos = int(validos) if validos else total
    vagas = int(vagas)
    qe = quociente_eleitoral(validos, vagas)
    out = Allocation(vagas=vagas, validos=validos, qe=qe, agremiacao_votos=dict(votos))
    if vagas <= 0 or validos <= 0 or total <= 0:
        return out
    qe = max(qe, 1)

    # candidatos de cada agremiação, do mais para o menos votado (ordem estável em empates)
    by_agr: dict[str, list[CandIn]] = {a: [] for a in votos}
    for i, c in sorted(enumerate(cands), key=lambda ic: (-ic[1].votos, ic[0])):
        by_agr[c.agremiacao].append(c)
    nxt = {a: 0 for a in votos}          # índice do próximo candidato não eleito
    seats = {a: 0 for a in votos}
    elected: list[Elected] = []

    def elect(a: str, via: str) -> None:
        c = by_agr[a][nxt[a]]
        nxt[a] += 1
        seats[a] += 1
        elected.append(Elected(c.sqcand, c.nome_urna, c.partido, c.agremiacao, c.votos, via))

    qp = {a: v // qe for a, v in votos.items()}
    out.qp = dict(qp)

    if max(qp.values(), default=0) == 0:
        # art. 111: nenhuma agremiação atingiu o QE
        ranked = sorted(((c, i) for i, c in enumerate(cands)), key=lambda ci: (-ci[0].votos, ci[1]))
        for c, _ in ranked[:vagas]:
            seats[c.agremiacao] += 1
            elected.append(Elected(c.sqcand, c.nome_urna, c.partido, c.agremiacao, c.votos, VIA_ART111))
    else:
        # art. 108: vagas do QP para quem tem >= 10% do QE
        for a in sorted(votos, key=lambda a: (-votos[a], a)):
            for _ in range(qp[a]):
                if len(elected) >= vagas or nxt[a] >= len(by_agr[a]):
                    break
                if by_agr[a][nxt[a]].votos * 10 < qe:
                    break
                elect(a, VIA_QP)

        # art. 109: sobras
        final_phase = False
        while len(elected) < vagas:
            if not final_phase:
                eligible = [a for a in votos
                            if 5 * votos[a] >= 4 * qe and nxt[a] < len(by_agr[a])
                            and 5 * by_agr[a][nxt[a]].votos >= qe]
                if not eligible:
                    final_phase = True
                    continue
            else:
                eligible = [a for a in votos if nxt[a] < len(by_agr[a])]
                if not eligible:
                    break  # não há mais candidatos: vagas ficam sem preencher
            best = min(eligible, key=lambda a: (-votos[a] / (seats[a] + 1), -votos[a], a))
            elect(best, VIA_MEDIA_FINAL if final_phase else VIA_MEDIA)

    out.seats_agremiacao = {a: n for a, n in seats.items() if n > 0}
    party: dict[str, int] = {}
    for e in elected:
        party[e.partido] = party.get(e.partido, 0) + 1
    out.seats_party = party
    out.elected = elected
    return out


# ----------------------------------------------------------------------------- entrada a partir do store

def build_inputs(parties, cands) -> tuple[dict[str, int], list[CandIn], bool]:
    """Monta as entradas de `allocate` a partir dos DataFrames de UMA área (store.latest_parties /
    store.latest_candidates).

    Votos do partido = votos_nominais + votos_legenda; se ambos vierem 0 e os candidatos do partido tiverem
    votos, usa a soma dos votos dos candidatos (ver docstring do módulo). Retorna (votos por agremiação,
    candidatos, usou_fallback).
    """
    cand_list: list[CandIn] = []
    cand_sum: dict[tuple[str, str], int] = {}
    if cands is not None and len(cands):
        for r in cands[["sqcand", "nome_urna", "partido", "agremiacao", "votos"]].itertuples(index=False):
            v = int(r.votos or 0)
            agr = r.agremiacao or r.partido or ""
            cand_list.append(CandIn(str(r.sqcand), str(r.nome_urna or ""), str(r.partido or ""), agr, v))
            cand_sum[(r.partido or "", agr)] = cand_sum.get((r.partido or "", agr), 0) + max(0, v)
    agr_votes: dict[str, int] = {}
    fallback = False
    seen: set[tuple[str, str]] = set()
    if parties is not None and len(parties):
        for r in parties[["sigla", "agremiacao", "votos_nominais", "votos_legenda"]].itertuples(index=False):
            agr = r.agremiacao or r.sigla or ""
            key = (r.sigla or "", agr)
            seen.add(key)
            v = int(r.votos_nominais or 0) + int(r.votos_legenda or 0)
            if v <= 0 and cand_sum.get(key, 0) > 0:
                v = cand_sum[key]
                fallback = True
            agr_votes[agr] = agr_votes.get(agr, 0) + v
    for key, v in cand_sum.items():
        if key not in seen:
            agr_votes[key[1]] = agr_votes.get(key[1], 0) + v
            fallback = fallback or v > 0
    return agr_votes, cand_list, fallback


def project(parties, cands, vagas: int, validos: int | None = None) -> Allocation:
    """Projeção para uma área a partir dos DataFrames do store (uma UF)."""
    agr_votes, cand_list, _ = build_inputs(parties, cands)
    total = sum(agr_votes.values())
    v = int(validos) if validos and validos > 0 else total
    return allocate(int(vagas), agr_votes, cand_list, v)
