"""Gera votos sintéticos em cima de arquivos '-u.json' reais (que antes das 17h vêm zerados).

Usado nos testes, no simulador (tools/simulate.py) e para desenvolver as telas antes da apuração.
"""
from __future__ import annotations

import copy
import random
import zlib
from datetime import datetime


def _fmt_pct(x: float) -> str:
    return f"{x:.2f}".replace(".", ",")


def _iter_cands(doc: dict):
    for agr in doc["carg"][0].get("agr", []):
        for par in agr.get("par", []):
            for c in par.get("cand", []):
                yield c


def fill_votes(doc: dict, pct_secoes: float, *, seed: int = 0, when: datetime | None = None,
               bias: dict[str, float] | None = None) -> dict:
    """Devolve uma cópia de `doc` com `pct_secoes`% das seções apuradas e votos sintéticos.

    - O peso de cada candidato depende só de (seed, sqcand): é consistente entre áreas e entre chamadas.
    - `bias` multiplica o peso por número de candidato (ex. {'13': 1.3}) para simular viradas.
    - Com pct_secoes >= 100 marca tf='s' e preenche eleito/situação de forma simplificada.
    """
    d = copy.deepcopy(doc)
    pct = max(0.0, min(100.0, float(pct_secoes)))
    frac = pct / 100.0
    area_rng = random.Random(f"{seed}-{d.get('cdabr')}")

    s, e, v = d["s"], d["e"], d["v"]
    ts = int(s.get("ts") or 0)
    st = round(ts * frac)
    s.update(st=str(st), pst=_fmt_pct(pct), snt=str(ts - st), psnt=_fmt_pct(100 - pct))
    te = int(e.get("te") or 0)
    est = round(te * frac)
    comp = round(est * area_rng.uniform(0.74, 0.86))
    e.update(est=str(est), pest=_fmt_pct(pct), c=str(comp), a=str(est - comp),
             pc=_fmt_pct(100 * comp / est if est else 0), pa=_fmt_pct(100 * (est - comp) / est if est else 0))
    brancos = round(comp * area_rng.uniform(0.015, 0.04))
    nulos = round(comp * area_rng.uniform(0.02, 0.05))
    validos = comp - brancos - nulos
    v.update(tv=str(comp), vb=str(brancos), tvn=str(nulos), vn=str(nulos), vv=str(validos), vnom=str(validos),
             pvv=_fmt_pct(100 * validos / comp if comp else 0))

    cands = list(_iter_cands(d))
    weights = []
    for c in cands:
        r = random.Random(zlib.crc32(f"{seed}-{c['sqcand']}".encode()))
        w = r.gammavariate(0.5, 1.0) * area_rng.uniform(0.6, 1.4)
        w *= (bias or {}).get(c.get("n", ""), 1.0)
        weights.append(w)
    total_w = sum(weights) or 1.0
    votes = [int(validos * w / total_w) for w in weights]
    for c, n in zip(cands, votes):
        c["vap"] = str(n)
        c["pvap"] = _fmt_pct(100 * n / validos if validos else 0)
        c["e"], c["st"] = "n", ""

    if pct >= 100:
        d["tf"] = "s"
        cargo = int(d["carg"][0]["cd"])
        nv = int(d["carg"][0].get("nv") or 1)
        ranked = sorted(zip(cands, votes), key=lambda cv: -cv[1])
        if cargo in (1, 3):
            top = ranked[0]
            if validos and top[1] / validos > 0.5:
                top[0].update(e="s", st="Eleito")
            else:
                for c, _ in ranked[:2]:
                    c["st"] = "2º turno"
        else:
            for c, _ in ranked[:nv]:
                c.update(e="s", st="Eleito" if cargo == 5 else "Eleito por QP")
    if when is not None:
        d["dg"], d["hg"] = when.strftime("%d/%m/%Y"), when.strftime("%H:%M:%S")
    return d
