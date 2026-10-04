"""Tela: Visão unificada — todos os cargos de um estado (ou município) numa folha só.

A linguagem visual vem da própria eleição: o líder de cada disputa aparece como a tela de confirmação da urna
(Número em caixinhas, Nome, Partido, foto à direita), e o status usa as cores das teclas CONFIRMA (decidido) e
CORRIGE (rumo ao 2º turno). Tudo é HTML/CSS próprio (st.html), escopado na classe `.uni`.
"""
from __future__ import annotations

import html
import logging
import math
from dataclasses import dataclass

import pandas as pd
import streamlit as st

from .. import analysis, config, store
from .common import ViewContext, fmt_int, fmt_pct, no_data, party_color, uf_name
from .congresso import compute_proportional
from .ranking import ensure_focus, mun_name

log = logging.getLogger(__name__)

OWN_HEADER = True  # app.py não desenha o cabeçalho padrão sobre esta tela

FONTS = ("https://fonts.googleapis.com/css2?family=Azeret+Mono:wght@500;600"
         "&family=Big+Shoulders+Display:wght@700;800&family=Public+Sans:wght@400;600;700&display=swap")
VICE = {1: "Vice-Presidente", 3: "Vice-Governador", 5: "1º Suplente"}
TITULO = {1: "Presidente", 3: "Governador", 5: "Senador", 6: "Deputado Federal", 7: "Deputado Estadual"}
DECIDIDO = (analysis.ST_DECIDIDO, analysis.ST_PRATICA)
MAX_OUTROS = 4


# ----------------------------------------------------------------------------- dados

@dataclass
class Office:
    cargo: int
    titulo: str
    eleicao: str
    vagas: int
    total: pd.Series | None
    cands: pd.DataFrame          # ordenado por votos (rank 1 primeiro)
    status: str = ""
    tone: str = "espera"         # confirma | corrige | neutro | espera
    nota: str = ""

    @property
    def tem_votos(self) -> bool:
        return self.total is not None and not self.cands.empty and int(self.cands["votos"].sum()) > 0


def offices_for(turno: int, uf: str) -> tuple[list[int], list[int]]:
    """(majoritários, proporcionais) disputados na área."""
    if uf == config.EXTERIOR:
        return [1], []
    if turno == 2:
        return [c for c in (1, 3) if (2, c) in config.ELECTIONS], []
    return [1, 3, 5], [6, 7]


def office_title(cargo: int, uf: str) -> str:
    return "Deputado Distrital" if cargo == 7 and uf == "df" else TITULO[cargo]


def office_status(cargo: int, turno: int, vagas: int, cands: pd.DataFrame, total: pd.Series | None,
                  decision: dict, onde: str = "no estado") -> tuple[str, str]:
    """(texto, tom) do selo de status de uma disputa majoritária."""
    if total is None or cands.empty or int(cands["votos"].sum()) <= 0:
        return "Aguardando os primeiros votos", "espera"
    eleitos = cands[cands["eleito"]]
    if not eleitos.empty:
        nomes = " e ".join(eleitos["nome_urna"].head(max(vagas, 1)))
        return (f"Eleito: {nomes}" if len(eleitos) == 1 else f"Eleitos: {nomes}"), "confirma"
    seg = cands[cands["situacao"].fillna("").str.contains("2º turno")]
    if len(seg) >= 2:
        return f"2º turno: {seg.iloc[0]['nome_urna']} × {seg.iloc[1]['nome_urna']}", "corrige"
    if bool(total.get("finalizada")):
        return "Apuração encerrada", "neutro"
    status, maioria = decision.get("status"), decision.get("maioria")
    if cargo == 1:  # presidente visto num estado/município: só quem vence aqui
        return (f"Vencedor {onde} definido (na prática)", "confirma") if status in DECIDIDO \
            else (f"Em aberto {onde}", "neutro")
    if cargo == 3 and turno == 1:
        if maioria == "Vence no 1º turno (na prática)":
            return "Vence no 1º turno (na prática)", "confirma"
        if maioria == "2º turno (na prática)":
            return "Caminho do 2º turno", "corrige"
        return "Em aberto", "neutro"
    if status in DECIDIDO:
        return ("As 2 vagas estão definidas (na prática)" if cargo == 5 else "Liderança definida (na prática)"), \
            "confirma"
    return "Em aberto", "neutro"


def _decision(cands: pd.DataFrame, totals: pd.DataFrame, vagas: int, majority: bool) -> dict:
    try:
        d = analysis.decision_check(cands, totals, vagas, check_majority=majority)
        return d.iloc[0].to_dict() if not d.empty else {}
    except Exception:  # noqa: BLE001 - status é complemento; a tela segue sem ele
        log.warning("decision_check", exc_info=True)
        return {}


def senate_cut_note(cands: pd.DataFrame, vagas: int) -> str:
    """Diferença entre o último dentro das vagas e o primeiro de fora."""
    if len(cands) <= vagas or int(cands["votos"].sum()) <= 0:
        return ""
    dentro, fora = cands.iloc[vagas - 1], cands.iloc[vagas]
    dv = int(dentro["votos"]) - int(fora["votos"])
    dp = float(dentro["pct_validos"]) - float(fora["pct_validos"])
    return (f"{fora['nome_urna']} está a {fmt_int(dv)} votos ({fmt_pct(dp)[:-1]} p.p.) "
            f"de {dentro['nome_urna']}, que ocupa a {vagas}ª vaga")


def load_office(conn, turno: int, cargo: int, uf: str, mun: str | None) -> Office:
    eleicao = config.ELECTIONS[(turno, cargo)]
    nivel = "mu" if mun else "uf"
    totals = store.latest_totals(conn, eleicao, cargo, nivel, uf=uf, mun=mun)
    cands = store.latest_candidates(conn, eleicao, cargo, nivel, uf=uf, mun=mun)
    cands = cands.sort_values(["votos", "numero"], ascending=[False, True]).reset_index(drop=True)
    total = totals.iloc[0] if not totals.empty else None
    vagas = int(total["vagas"]) if total is not None and pd.notna(total["vagas"]) and total["vagas"] else 1
    of = Office(cargo, office_title(cargo, uf), eleicao, vagas, total, cands)
    if cargo in config.CARGOS_MAJORITARIOS:
        dec = _decision(cands, totals, vagas, majority=(cargo == 3 and turno == 1)) if of.tem_votos else {}
        of.status, of.tone = office_status(cargo, turno, vagas, cands, total, dec,
                                           onde="no município" if mun else "no estado")
        if cargo == 5:
            of.nota = senate_cut_note(cands, vagas)
    return of


def _db_id(conn) -> str:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        return str(row[2]) if row else ""
    except Exception:  # noqa: BLE001
        return ""


@st.cache_data(ttl=60, show_spinner=False, max_entries=16)
def _bancada_cached(_conn, db: str, eleicao: str, cargo: int, uf: str, sig: int) -> dict:
    return compute_proportional(store.latest_totals(_conn, eleicao, cargo, "uf", uf),
                                store.latest_candidates(_conn, eleicao, cargo, "uf", uf),
                                store.latest_parties(_conn, eleicao, cargo, "uf", uf))


def bancada(conn, of: Office, uf: str) -> dict | None:
    """Projeção (ou resultado oficial) das cadeiras da UF — mesma regra da tela Congresso."""
    if not of.tem_votos:
        return None
    try:
        return _bancada_cached(conn, _db_id(conn), of.eleicao, of.cargo, uf, int(of.total["snapshot_id"]))
    except Exception:  # noqa: BLE001
        log.warning("bancada %s %s", of.cargo, uf, exc_info=True)
        return None


def seat_tiles(elected: pd.DataFrame, vagas: int) -> list[tuple[str, str]]:
    """Uma (sigla, cor) por cadeira, agrupadas por partido (maior bancada primeiro); sobra = cadeira vazia."""
    tiles: list[tuple[str, str]] = []
    if elected is not None and not elected.empty:
        counts = elected["partido"].value_counts()
        for sigla, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            tiles += [(sigla, party_color(sigla))] * int(n)
    return tiles[:vagas] + [("", "")] * max(0, vagas - len(tiles))


def tile_columns(vagas: int) -> int:
    rows = max(1, math.ceil(vagas / 16))
    return max(1, math.ceil(vagas / rows))


def bar_scale(cargo: int, top_pct: float) -> float:
    """Fundo de escala das barras: 100% p/ Presidente/Governador (marca de 50%); senadores têm % menores."""
    if cargo in (1, 3):
        return 100.0
    return max(10.0, math.ceil((top_pct or 0) * 1.15 / 10) * 10)


def digits(numero: str) -> list[str]:
    return [c for c in str(numero or "") if c.isdigit()] or ["–"]


def photo_url(cargo: int, uf: str, sqcand: str) -> str:
    eleicao = config.ELECTIONS[(1, cargo)]
    pasta = config.BRASIL if cargo == 1 else uf
    return f"{config.TSE_BASE}/{config.CICLO}/{eleicao}/fotos/{pasta}/{sqcand}.jpeg"


def initials(nome: str) -> str:
    parts = [p for p in str(nome).split() if p[:1].isalpha()]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def pct_of(part, whole) -> float | None:
    try:
        return 100.0 * float(part) / float(whole) if float(whole) else None
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------- HTML

def e(x) -> str:
    return html.escape(str(x if x is not None else ""))


CSS = """
@import url('%(fonts)s');
.uni{--sheet:#ECEFEA;--card:#FBFCFA;--screen:#E2E9E3;--screen-edge:#C9D3CB;--ink:#16201B;--muted:#5B6660;
 --rule:#D3D9D2;--track:#DCE2DC;--confirma:#1D8A4C;--corrige:#CB6418;--neutro:#5B6660;
 font-family:'Public Sans',system-ui,-apple-system,'Segoe UI',sans-serif;color:var(--ink);background:var(--sheet);
 border-radius:22px;padding:30px 30px 34px;line-height:1.35}
.uni.dark{--sheet:#101512;--card:#171D19;--screen:#1F2822;--screen-edge:#2F3B33;--ink:#E6ECE7;--muted:#97A39C;
 --rule:#29322C;--track:#2A332D;--confirma:#45C27C;--corrige:#F0883E;--neutro:#97A39C}
.uni *{box-sizing:border-box}
.uni .mono{font-family:'Azeret Mono',ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums}
.uni .eyebrow{font:600 11px/1.4 'Azeret Mono',ui-monospace,Menlo,monospace;letter-spacing:.16em;
 text-transform:uppercase;color:var(--muted)}
.uni .estado{font:800 clamp(46px,7.2vw,92px)/.86 'Big Shoulders Display',Impact,'Arial Narrow',sans-serif;
 text-transform:uppercase;letter-spacing:.005em;margin:12px 0 4px}
.uni .estado small{display:block;font:700 .34em/1.1 'Big Shoulders Display',Impact,sans-serif;
 letter-spacing:.06em;color:var(--muted);margin-top:.3em}
.uni .apuracao{margin:22px 0 6px}
.uni .apuracao .linha{display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
.uni .apuracao .linha b{font:600 22px/1 'Azeret Mono',ui-monospace,monospace}
.uni .apuracao .linha span{color:var(--muted);font-size:14px}
.uni .rail{position:relative;height:10px;border-radius:6px;background:var(--track);margin-top:10px;overflow:hidden}
.uni .rail i{position:absolute;inset:0 auto 0 0;background:var(--ink);border-radius:6px}
.uni .rail em{position:absolute;top:0;bottom:0;width:1px;background:var(--sheet);opacity:.9}
.uni .bu{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));margin-top:18px;
 border-top:1px solid var(--rule);border-bottom:1px solid var(--rule)}
.uni .bu div{padding:11px 14px 12px 0}
.uni .bu div+div{border-left:1px dashed var(--rule);padding-left:14px}
.uni .bu dt{font:600 10.5px/1.3 'Azeret Mono',ui-monospace,monospace;letter-spacing:.12em;text-transform:uppercase;
 color:var(--muted)}
.uni .bu dd{margin:4px 0 0;font:600 18px/1.1 'Azeret Mono',ui-monospace,monospace}
.uni .secao{display:flex;align-items:baseline;gap:14px;margin:34px 0 14px}
.uni .secao h2{font:800 30px/1 'Big Shoulders Display',Impact,sans-serif;text-transform:uppercase;margin:0;
 letter-spacing:.01em}
.uni .secao p{margin:0;color:var(--muted);font-size:14px}
.uni .secao::after{content:"";flex:1;border-bottom:1px solid var(--rule);transform:translateY(-6px)}
.uni .grade{display:grid;gap:18px;grid-template-columns:repeat(auto-fit,minmax(310px,1fr))}
.uni .grade.dois{grid-template-columns:repeat(auto-fit,minmax(400px,1fr))}
.uni .card{background:var(--card);border:1px solid var(--rule);border-radius:16px;padding:18px 18px 16px;
 display:flex;flex-direction:column;gap:12px;min-width:0}
.uni .card header{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.uni .card h3{font:700 27px/1 'Big Shoulders Display',Impact,sans-serif;text-transform:uppercase;margin:0;
 letter-spacing:.015em}
.uni .card header .mono{font-size:12px;color:var(--muted);white-space:nowrap}
.uni .selo{align-self:flex-start;font-size:12.5px;font-weight:700;border-radius:999px;padding:4px 11px 4px 9px;
 display:inline-flex;align-items:center;gap:7px;border:1.5px solid currentColor}
.uni .selo::before{content:"";width:8px;height:8px;border-radius:2px;background:currentColor}
.uni .selo.confirma{color:var(--confirma)}
.uni .selo.corrige{color:var(--corrige)}
.uni .selo.neutro{color:var(--neutro)}
.uni .selo.espera{color:var(--muted);border-style:dashed}
.uni .tela{background:var(--screen);border:1px solid var(--screen-edge);border-radius:12px;padding:14px 14px 12px;
 display:grid;grid-template-columns:1fr auto;gap:4px 14px;box-shadow:inset 0 1px 0 rgba(255,255,255,.35)}
.uni .tela .campos{grid-column:1;grid-row:1}
.uni .tela .foto{grid-column:2;grid-row:1 / span 2}
.uni .tela .placar{grid-column:1;grid-row:2}
.uni .tela.mini{padding:10px 12px}
.uni .campo{display:flex;gap:8px;align-items:center;min-width:0;font-size:13.5px}
.uni .rot{color:var(--muted);flex:none}
.uni .val{font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.uni .campos{display:flex;flex-direction:column;gap:6px;min-width:0}
.uni .digitos{display:inline-flex;gap:4px}
.uni .digitos b{width:26px;height:33px;border:1.5px solid var(--ink);border-radius:4px;display:inline-flex;
 align-items:center;justify-content:center;font:600 19px/1 'Azeret Mono',ui-monospace,monospace;
 background:var(--card)}
.uni .mini .digitos b{width:21px;height:27px;font-size:15px}
.uni .foto{width:74px;height:96px;border-radius:6px;background:var(--track) center/cover no-repeat;
 border:1px solid var(--screen-edge);display:flex;align-items:flex-end;justify-content:center;overflow:hidden}
.uni .mini .foto{width:46px;height:60px}
.uni .mini .foto span{font-size:16px;padding-bottom:18px}
.uni .foto span{font:700 22px/1 'Big Shoulders Display',Impact,sans-serif;color:var(--muted);padding-bottom:28px}
.uni .placar{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-top:6px}
.uni .placar .pct{font:600 34px/1 'Azeret Mono',ui-monospace,monospace;letter-spacing:-.02em}
.uni .mini .placar .pct{font-size:22px}
.uni .mini .campos{gap:4px}
.uni .placar .votos{color:var(--muted);font-size:12.5px}
.uni .duas{display:flex;flex-direction:column;gap:10px}
.uni .vaga{font:600 10.5px/1 'Azeret Mono',ui-monospace,monospace;letter-spacing:.12em;text-transform:uppercase;
 color:var(--muted);margin-bottom:6px}
.uni ol.outros{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:9px}
.uni .outros li{display:grid;grid-template-columns:38px minmax(0,1fr) 62px;gap:2px 10px;align-items:center}
.uni .outros .num{font-size:12.5px;color:var(--muted)}
.uni .outros .nm{font-size:13.5px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.uni .outros .nm i{font-style:normal;font-weight:400;color:var(--muted);margin-left:6px;font-size:12px}
.uni .outros .pc{font-size:13px;text-align:right}
.uni .trilho{grid-column:2 / 3;position:relative;height:6px;border-radius:4px;background:var(--track)}
.uni .trilho .f{position:absolute;inset:0 auto 0 0;border-radius:4px}
.uni .trilho .meta{position:absolute;top:-4px;bottom:-4px;width:0;border-left:1.5px dotted var(--muted)}
.uni .corte{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px;margin:2px 0}
.uni .corte::before,.uni .corte::after{content:"";flex:1;border-bottom:1.5px dashed var(--rule)}
.uni .nota{font-size:12.5px;color:var(--muted)}
.uni .nota b{color:var(--ink)}
.uni .card footer{margin-top:auto;padding-top:10px;border-top:1px dashed var(--rule);font-size:11.5px;
 color:var(--muted);display:flex;gap:14px;flex-wrap:wrap}
.uni .cadeiras{display:grid;gap:4px}
.uni .cadeiras i{aspect-ratio:1;border-radius:3px;display:block;min-width:0}
.uni .cadeiras i.vazia{background:transparent;border:1.5px dashed var(--rule)}
.uni ul.legenda{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12.5px}
.uni .legenda li{display:inline-flex;align-items:center;gap:6px}
.uni .legenda li i{width:10px;height:10px;border-radius:2px;display:inline-block}
.uni .legenda b{font-family:'Azeret Mono',ui-monospace,monospace}
.uni h4{font:600 10.5px/1 'Azeret Mono',ui-monospace,monospace;letter-spacing:.14em;text-transform:uppercase;
 color:var(--muted);margin:6px 0 0}
.uni ol.bu-lista{list-style:none;margin:0;padding:0;font-size:13px}
.uni .bu-lista li{display:flex;align-items:baseline;gap:8px;padding:4px 0}
.uni .bu-lista .num{width:54px;flex:none;color:var(--muted);font-size:12px}
.uni .bu-lista .nm{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.uni .bu-lista .nm i{font-style:normal;font-weight:400;color:var(--muted);margin-left:6px;font-size:11.5px}
.uni .bu-lista .pontos{flex:1;min-width:16px;border-bottom:1.5px dotted var(--rule);transform:translateY(-3px)}
.uni .bu-lista .v{font-size:12.5px}
.uni .bu-lista .pc{width:58px;text-align:right;font-size:12.5px;color:var(--muted)}
.uni .bu-lista .tag{color:var(--confirma);font-weight:700;width:12px;flex:none;text-align:center}
.uni h4 .tag{color:var(--confirma);letter-spacing:0;text-transform:none;margin-left:10px;font-weight:600}
.uni .pilha{display:flex;height:14px;border-radius:7px;overflow:hidden;background:var(--track)}
.uni .vazio{color:var(--muted);font-size:13.5px;padding:18px 0;text-align:center;border:1.5px dashed var(--rule);
 border-radius:12px}
@media (max-width:640px){.uni{padding:20px 16px 24px;border-radius:16px}.uni .grade.dois{grid-template-columns:1fr}}
"""


def css() -> str:
    return "<style>" + CSS.replace("%(fonts)s", FONTS) + "</style>"


def _foto(cargo: int, uf: str, r) -> str:
    url = photo_url(cargo, uf, r["sqcand"])
    return f'<div class="foto" style="background-image:url(\'{e(url)}\')"><span>{e(initials(r["nome_urna"]))}</span></div>'


def urna_screen(cargo: int, uf: str, r, mini: bool = False) -> str:
    """Painel do líder no formato da tela de confirmação da urna."""
    dig = "".join(f"<b>{e(d)}</b>" for d in digits(r["numero"]))
    vice = str(r.get("vice") or "")
    campos = [f'<div class="campo"><span class="rot">Número:</span><span class="digitos">{dig}</span></div>',
              f'<div class="campo"><span class="rot">Nome:</span><span class="val">{e(r["nome_urna"])}</span></div>',
              f'<div class="campo"><span class="rot">Partido:</span><span class="val">{e(r["partido"])}</span></div>']
    if vice and not mini:
        campos.append(f'<div class="campo"><span class="rot">{e(VICE.get(cargo, "Vice"))}:</span>'
                      f'<span class="val">{e(vice)}</span></div>')
    placar = (f'<div class="placar"><span class="pct mono">{e(fmt_pct(r["pct_validos"]))}</span>'
              f'<span class="votos mono">{e(fmt_int(r["votos"]))} votos</span></div>')
    return (f'<div class="tela{" mini" if mini else ""}"><div class="campos">{"".join(campos)}</div>'
            f'{_foto(cargo, uf, r)}{placar}</div>')


def other_rows(cargo: int, rows: pd.DataFrame, scale: float) -> str:
    items = []
    for _, r in rows.iterrows():
        w = max(0.0, min(100.0, 100.0 * float(r["pct_validos"]) / scale))
        meta = '<span class="meta" style="left:50%"></span>' if cargo in (1, 3) else ""
        items.append(
            f'<li><span class="num mono">{e(r["numero"])}</span>'
            f'<span class="nm">{e(r["nome_urna"])}<i>{e(r["partido"])}</i></span>'
            f'<span class="pc mono">{e(fmt_pct(r["pct_validos"]))}</span>'
            f'<span></span><span class="trilho"><span class="f" style="width:{w:.2f}%;'
            f'background:{party_color(r["partido"])}"></span>{meta}</span></li>')
    return f'<ol class="outros">{"".join(items)}</ol>' if items else ""


def _footer(total: pd.Series | None) -> str:
    if total is None:
        return ""
    b, n = pct_of(total["brancos"], total["votos_total"]), pct_of(total["nulos"], total["votos_total"])
    return (f'<footer class="mono"><span>Brancos {e(fmt_pct(b, 1))}</span><span>Nulos {e(fmt_pct(n, 1))}</span>'
            f'<span>Válidos {e(fmt_int(total["validos"]))}</span></footer>')


def _head(of: Office, extra: str = "") -> str:
    pct = fmt_pct(of.total["pct_secoes"]) if of.total is not None else "–"
    return (f'<header><h3>{e(of.titulo)}</h3><span class="mono">{e(extra)}{e(pct)} apurado</span></header>')


def majoritarian_card(of: Office, uf: str, national: str = "") -> str:
    parts = [_head(of, f"{of.vagas} vagas · " if of.vagas > 1 else "")]
    parts.append(f'<span class="selo {of.tone}">{e(of.status)}</span>')
    if not of.tem_votos:
        parts.append('<div class="vazio">Os votos aparecem aqui assim que o TSE publicar a primeira parcial.</div>')
    else:
        c = of.cands
        scale = bar_scale(of.cargo, float(c["pct_validos"].iloc[0]))
        if of.cargo == 5 and of.vagas >= 2:
            telas = "".join(f'<div><div class="vaga">{i + 1}º colocado</div>{urna_screen(5, uf, c.iloc[i], True)}</div>'
                            for i in range(min(2, len(c))))
            parts.append(f'<div class="duas">{telas}</div>')
            if len(c) > 2:
                parts.append('<div class="corte">linha de corte</div>')
                parts.append(other_rows(5, c.iloc[2:2 + MAX_OUTROS - 1], scale))
            if of.nota:
                parts.append(f'<div class="nota">{e(of.nota)}</div>')
        else:
            parts.append(urna_screen(of.cargo, uf, c.iloc[0]))
            parts.append(other_rows(of.cargo, c.iloc[1:1 + MAX_OUTROS], scale))
        if national:
            parts.append(f'<div class="nota">{national}</div>')
    parts.append(_footer(of.total))
    return f'<article class="card">{"".join(parts)}</article>'


def bu_list(rows: pd.DataFrame, eleitos: set[str] | None = None, oficial: bool = False) -> str:
    items = []
    for _, r in rows.iterrows():
        dentro = bool(eleitos) and r["sqcand"] in eleitos
        tag = f'<span class="tag">{"✓" if dentro else ""}</span>' if eleitos is not None else ""
        items.append(f'<li>{tag}<span class="num mono">{e(r["numero"])}</span>'
                     f'<span class="nm">{e(r["nome_urna"])}<i>{e(r["partido"])}</i></span>'
                     f'<span class="pontos"></span><span class="v mono">{e(fmt_int(r["votos"]))}</span>'
                     f'<span class="pc mono">{e(fmt_pct(r["pct_validos"]))}</span></li>')
    return f'<ol class="bu-lista">{"".join(items)}</ol>'


def party_stack(cands: pd.DataFrame, top: int = 6) -> str:
    v = cands.groupby("partido")["votos"].sum().sort_values(ascending=False)
    v = v[v > 0]
    if v.empty:
        return ""
    total = float(v.sum())
    head, rest = v.head(top), float(v.iloc[top:].sum())
    segs = "".join(f'<i style="width:{100 * x / total:.2f}%;background:{party_color(p)}" title="{e(p)}"></i>'
                   for p, x in head.items())
    if rest > 0:
        segs += f'<i style="width:{100 * rest / total:.2f}%;background:var(--muted)" title="Outros"></i>'
    leg = "".join(f'<li><i style="background:{party_color(p)}"></i>{e(p)} <b>{e(fmt_pct(100 * x / total, 1))}</b></li>'
                  for p, x in head.items())
    return f'<div class="pilha">{segs}</div><ul class="legenda">{leg}</ul>'


def proportional_card(of: Office, proj: dict | None, mun_label: str = "") -> str:
    parts = [_head(of, f"{of.vagas} vagas · " if not mun_label else "")]
    if not of.tem_votos:
        parts.append('<span class="selo espera">Aguardando os primeiros votos</span>')
        parts.append('<div class="vazio">As cadeiras aparecem aqui assim que o TSE publicar a primeira parcial.</div>')
        return f'<article class="card">{"".join(parts)}</article>'
    if mun_label:  # cadeiras são estaduais: no município mostra só votos
        parts.append(f'<span class="selo neutro">Votação em {e(mun_label)}</span>')
        parts.append('<h4>Votos por partido</h4>' + party_stack(of.cands))
        parts.append('<h4>Mais votados no município</h4>' + bu_list(of.cands.head(6)))
        parts.append(_footer(of.total))
        return f'<article class="card">{"".join(parts)}</article>'
    elected = proj["elected"] if proj else pd.DataFrame(columns=["partido", "sqcand"])
    oficial = bool(proj and not proj["ufs"].empty and bool(proj["ufs"]["oficial"].iloc[0]))
    tone, label = ("confirma", "Resultado oficial") if oficial else ("neutro", "Projeção da bancada — muda a cada parcial")
    parts.append(f'<span class="selo {tone}">{label}</span>')
    tiles = seat_tiles(elected, of.vagas)
    cells = "".join(f'<i style="background:{c}" title="{e(s)}"></i>' if s else '<i class="vazia"></i>'
                    for s, c in tiles)
    parts.append(f'<div class="cadeiras" style="grid-template-columns:repeat({tile_columns(of.vagas)},1fr)">{cells}</div>')
    if not elected.empty:
        counts = elected["partido"].value_counts()
        leg = "".join(f'<li><i style="background:{party_color(p)}"></i>{e(p)} <b>{int(n)}</b></li>'
                      for p, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
        parts.append(f'<ul class="legenda">{leg}</ul>')
    if proj and not proj["ufs"].empty and int(proj["ufs"]["qe"].iloc[0] or 0) > 0:
        parts.append(f'<div class="nota">Quociente eleitoral: <b class="mono">'
                     f'{e(fmt_int(proj["ufs"]["qe"].iloc[0]))}</b> votos por cadeira</div>')
    eleitos = set(elected["sqcand"]) if not elected.empty else set()
    legenda = "✓ eleito" if oficial else "✓ eleito na projeção"
    parts.append(f'<h4>Mais votados<span class="tag">{legenda}</span></h4>'
                 + bu_list(of.cands.head(6), eleitos, oficial))
    parts.append(_footer(of.total))
    return f'<article class="card">{"".join(parts)}</article>'


def hero(area: str, sub: str, turno: int, total: pd.Series | None) -> str:
    when = ""
    if total is not None and pd.notna(total.get("tse_ts")):
        when = f" · atualizado pelo TSE às {pd.Timestamp(total['tse_ts']).strftime('%H:%M:%S')}"
    eyebrow = f'<div class="eyebrow">Boletim unificado · {turno}º turno{e(when)}</div>'
    title = f'<h1 class="estado">{e(area)}{f"<small>{e(sub)}</small>" if sub else ""}</h1>'
    if total is None:
        return eyebrow + title + '<div class="vazio">Sem dados desta área ainda.</div>'
    pct = float(total["pct_secoes"] or 0)
    ticks = "".join(f'<em style="left:{t}%"></em>' for t in (25, 50, 75))
    rail = (f'<div class="apuracao"><div class="linha"><b>{e(fmt_pct(pct))}</b>'
            f'<span>das seções apuradas · {e(fmt_int(total["secoes_totalizadas"]))} de '
            f'{e(fmt_int(total["secoes_total"]))}</span></div>'
            f'<div class="rail"><i style="width:{min(100.0, pct):.2f}%"></i>{ticks}</div></div>')
    comp = pct_of(total["comparecimento"], total["eleitorado_apurado"])
    abst = pct_of(total["abstencao"], total["eleitorado_apurado"])
    bu = (f'<dl class="bu"><div><dt>Eleitores aptos</dt><dd>{e(fmt_int(total["eleitorado"]))}</dd></div>'
          f'<div><dt>Já apurados</dt><dd>{e(fmt_int(total["eleitorado_apurado"]))}</dd></div>'
          f'<div><dt>Comparecimento</dt><dd>{e(fmt_pct(comp, 1))}</dd></div>'
          f'<div><dt>Abstenção</dt><dd>{e(fmt_pct(abst, 1))}</dd></div></dl>')
    return eyebrow + title + rail + bu


def national_line(conn, eleicao: str) -> str:
    """Resumo nacional para o cartão de Presidente visto num estado."""
    try:
        ld = store.leaders(conn, eleicao, 1, "br")
        if ld.empty or not ld.iloc[0]["nome_urna"]:
            return ""
        r = ld.iloc[0]
        return (f'No Brasil: <b>{e(r["nome_urna"])}</b> {e(fmt_pct(r["pct_validos"]))} · '
                f'{e(r["second_nome_urna"] or "")} {e(fmt_pct(r["second_pct"]))} · '
                f'{e(fmt_pct(r["pct_secoes"]))} apurado')
    except Exception:  # noqa: BLE001
        return ""


def build_page(conn, turno: int, uf: str, mun: str | None, dark: bool) -> str:
    major, prop = offices_for(turno, uf)
    offices = {c: load_office(conn, turno, c, uf, mun) for c in major + prop}
    lead = offices.get(1) or next(iter(offices.values()))
    area = mun_name(uf, mun) if mun else uf_name(uf)
    sub = uf_name(uf) if mun else ""
    out = [f'<div class="uni{" dark" if dark else ""}">', hero(area, sub, turno, lead.total)]
    out.append('<div class="secao"><h2>Majoritários</h2><p>vence quem tiver mais votos</p></div>')
    cards = []
    for c in major:
        nat = national_line(conn, offices[c].eleicao) if c == 1 and uf != config.EXTERIOR else ""
        cards.append(majoritarian_card(offices[c], uf, nat))
    out.append(f'<div class="grade">{"".join(cards)}</div>')
    if prop:
        out.append('<div class="secao"><h2>Proporcionais</h2>'
                   '<p>cadeiras divididas pelo quociente eleitoral</p></div>')
        pcs = [proportional_card(offices[c], None if mun else bancada(conn, offices[c], uf),
                                 mun_label=area if mun else "") for c in prop]
        out.append(f'<div class="grade dois">{"".join(pcs)}</div>')
    out.append("</div>")
    return css() + "".join(out)


# ----------------------------------------------------------------------------- tela

def _theme_dark() -> bool:
    try:
        return st.context.theme.type == "dark"
    except Exception:  # noqa: BLE001
        return False


def render(ctx: ViewContext) -> None:
    try:
        uf = ctx.uf
        if uf == config.BRASIL:
            uf = st.selectbox("Estado", config.UF_LIST, index=config.UF_LIST.index("rs"), format_func=uf_name,
                              key="uni_uf", help="A visão unificada mostra um estado de cada vez.")
        ensure_focus(ctx.conn, uf)
        st.html(build_page(ctx.conn, ctx.turno, uf, ctx.mun, _theme_dark()))
        if ctx.mun:
            st.caption("Mostrando um município — clique em outro estado no mapa ou mude o Estado na barra "
                       "lateral para voltar à visão estadual.")
    except Exception:  # noqa: BLE001 - a tela nunca derruba o app
        log.exception("visão unificada")
        no_data("Não foi possível montar a visão unificada agora. Tentando de novo na próxima atualização.")
