"""Simulador da apuração: escreve votos sintéticos em data/sim.db (mesmo schema do coletor).

Usa como base os arquivos '-u.json' reais do TSE (zerados antes das 17h), baixados uma vez para
data/sim_base/, e `fake.fill_votes` para preencher votos. O relógio simulado começa às 17:00 de
04/10/2026 e a apuração inteira (17h -> 23h) é comprimida em `--minutes` minutos reais.

Exemplos (da raiz do projeto; `--no-capture-output` faz o log aparecer em tempo real):
    conda run --no-capture-output -n python3 python tools/simulate.py --reset   # 10 min, RS por município
    conda run -n python3 python tools/simulate.py --reset --minutes 3 --tick 3
    conda run -n python3 python tools/simulate.py --offline --reset --instant 40 --clock 19:10
    conda run -n python3 python tools/simulate.py --prefetch --mun-ufs rs,sp   # só baixa a base

Virada: Presidente tem pesos por região (um candidato forte no Nordeste/Norte, outro no
Sul/Sudeste/Centro-Oeste) e as UFs do Sul/Sudeste apuram mais cedo, então a liderança nacional muda
durante a noite. O nível 'br' do Presidente é a soma exata das 27 UFs + exterior (quando todas existem).
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import random
import sys
import threading
import time
import zlib
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from eleicoes import config, fake, geo, parse, store  # noqa: E402

BASE_DIR = config.DATA_DIR / "sim_base"
FIXTURES = ROOT / "tests" / "fixtures"
SIM_START = datetime(2026, 10, 4, 17, 0, 0)
SIM_HOURS = 6.0              # o relógio simulado vai de 17:00 a 23:00
LAST_DONE_H = SIM_HOURS - 0.3  # toda área chega a 100% até ~22:42
DOWNLOAD_CONCURRENCY = 4
PRES = "6257"
GERAL = "6259"
BR_KEY = (PRES, 1, "br", "")

# Eleitorado aproximado (milhões, 2022) — só para a forma das curvas e para o fallback do nível br.
ELECTORATE_M = {
    "sp": 34.7, "mg": 16.3, "rj": 12.8, "ba": 11.3, "rs": 8.6, "pr": 8.5, "pe": 7.0, "ce": 6.8, "pa": 6.1,
    "sc": 5.5, "ma": 5.1, "go": 4.9, "pb": 3.1, "es": 2.9, "am": 2.6, "pi": 2.6, "rn": 2.6, "mt": 2.5,
    "al": 2.3, "df": 2.2, "ms": 2.0, "se": 1.7, "ro": 1.2, "to": 1.1, "ac": 0.6, "ap": 0.55, "rr": 0.37,
    "zz": 0.7,
}
# Hora (desde 17h) em que metade das seções da UF está apurada, por região.
REGION_T50 = {"Sul": 1.55, "Sudeste": 1.8, "Centro-Oeste": 1.9, "Norte": 2.65, "Nordeste": 2.6}
# Presidente: participação-alvo (A = forte no NE/N, B = forte no S/SE/CO, C = 3º, D = 4º).
REGION_SHARES = {
    "Nordeste": (0.58, 0.30, 0.05, 0.02),
    "Norte": (0.45, 0.43, 0.06, 0.02),
    "Sudeste": (0.39, 0.47, 0.06, 0.05),
    "Sul": (0.35, 0.52, 0.06, 0.04),
    "Centro-Oeste": (0.34, 0.51, 0.09, 0.03),
    "Exterior": (0.46, 0.42, 0.05, 0.04),
}
# Demais cargos: força relativa por número do partido (2 primeiros dígitos do número do candidato),
# para que os grandes partidos ganhem mais vezes. Partidos fora da tabela: 0.5.
PARTY_STRENGTH = {
    "22": 3.0, "13": 3.0, "15": 2.5, "55": 2.5, "44": 2.5, "11": 2.0, "10": 2.0, "40": 1.6, "45": 1.3,
    "12": 1.2, "20": 1.1, "50": 1.1, "30": 1.0, "77": 0.8, "65": 0.7, "43": 0.6, "18": 0.5, "70": 0.6,
    "25": 0.6, "23": 0.5, "16": 0.15, "21": 0.15, "29": 0.15, "80": 0.2, "27": 0.3, "35": 0.3,
    "14": 0.4, "36": 0.3, "33": 0.3,
}
HOME_BOOST = {"55": ("go", 0.15), "30": ("mg", 0.10), "22": ("rj", 0.05)}  # número -> (UF natal, bônus)

# Limiar (pontos percentuais de seções) para gravar um novo snapshot de uma área.
THRESHOLD = {("uf", "maj"): 0.01, ("uf", "prop"): 2.0, ("mu", "maj"): 5.0, ("mu", "prop"): 15.0}
MILESTONES = (25, 50, 75, 100)
MAX_MUN_WRITES_PER_TICK = 800  # espalha o "atraso" (início, UF nova em foco) por vários ticks


def log(*a) -> None:
    print(f"[sim {datetime.now():%H:%M:%S}]", *a, flush=True)


def _p(x: float) -> str:
    return f"{x:.2f}".replace(".", ",")


def _br(x: float) -> str:
    return _p(x)


def _i(s) -> int:
    return parse.br_int(s)


# ============================================================================ arquivos base

def file_name(eleicao: str, cargo: int, uf: str, mun: str = "") -> str:
    return f"{uf}{mun}-c{cargo:04d}-e{int(eleicao):06d}-u.json"


def file_url(eleicao: str, cargo: int, uf: str, mun: str = "") -> str:
    return f"{config.TSE_BASE}/{config.CICLO}/{eleicao}/dados/{uf}/{file_name(eleicao, cargo, uf, mun)}"


def state_keys() -> list[tuple]:
    """Conjunto do 1º turno no nível br/UF (137 arquivos)."""
    keys = [BR_KEY] + [(PRES, 1, uf, "") for uf in config.UF_LIST + [config.EXTERIOR]]
    for uf in config.UF_LIST:
        for cargo in (3, 5, 6, 8 if uf == "df" else 7):
            keys.append((GERAL, cargo, uf, ""))
    return keys


def mun_keys(uf: str) -> list[tuple]:
    uf = uf.lower()
    keys = []
    for mun in geo.municipios_uf(uf)["mun"]:
        for cargo in (1, 3, 5, 6, 8 if uf == "df" else 7):
            keys.append((PRES if cargo == 1 else GERAL, cargo, uf, mun))
    return keys


async def _download_async(items: list[tuple[tuple, str, Path]], concurrency: int) -> dict[tuple, Path]:
    sem = asyncio.Semaphore(concurrency)
    got: dict[tuple, Path] = {}
    done = 0
    headers = {"User-Agent": "eleicoes-local-sim/1.0 (uso pessoal; base zerada baixada uma vez)"}
    async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT_S, follow_redirects=True, headers=headers,
                                 limits=httpx.Limits(max_connections=concurrency)) as client:
        async def one(key, url, path):
            nonlocal done
            for attempt in range(3):
                r = None
                async with sem:
                    try:
                        r = await client.get(url)
                    except httpx.HTTPError:
                        r = None
                if r is not None and r.status_code == 200:
                    try:
                        json.loads(r.content)
                    except ValueError:
                        r = None
                    else:
                        tmp = path.with_suffix(".tmp")
                        tmp.write_bytes(r.content)
                        tmp.replace(path)
                        got[key] = path
                        break
                if r is not None and r.status_code == 404:
                    break
                await asyncio.sleep(1.0 + 2 * attempt)
            done += 1
            if done % 250 == 0 or done == len(items):
                log(f"base: {done}/{len(items)} arquivos")

        await asyncio.gather(*(one(*it) for it in items))
    return got


def ensure_base(keys: list[tuple], offline: bool = False) -> dict[tuple, Path]:
    """Caminho local de cada arquivo base: cache em data/sim_base/ (baixa o que falta) ou fixtures."""
    found: dict[tuple, Path] = {}
    missing = []
    for k in keys:
        name = file_name(*k)
        cached, fixture = BASE_DIR / name, FIXTURES / name
        if not offline and cached.exists():
            found[k] = cached
        elif offline:
            if cached.exists():
                found[k] = cached
            elif fixture.exists():
                found[k] = fixture
        else:
            missing.append(k)
    if missing:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        log(f"baixando {len(missing)} arquivos base do TSE (concorrência {DOWNLOAD_CONCURRENCY})...")
        items = [(k, file_url(*k), BASE_DIR / file_name(*k)) for k in missing]
        got = asyncio.run(_download_async(items, DOWNLOAD_CONCURRENCY))
        found.update(got)
        for k in missing:
            if k not in got and (FIXTURES / file_name(*k)).exists():
                found[k] = FIXTURES / file_name(*k)
        log(f"download: {len(got)}/{len(missing)} ok em {time.monotonic() - t0:.1f}s")
    return found


def load_doc(path: Path) -> dict:
    return json.loads(Path(path).read_bytes())


# ============================================================================ curvas de progresso

def _logistic(t: float, t50: float, w: float) -> float:
    return 1.0 / (1.0 + math.exp(-(t - t50) / w))


def curve_pct(t: float, t50: float, w: float, t_done: float) -> float:
    """% de seções apuradas no instante t (horas desde 17h): logística normalizada, 0 em t=0 e 100 em t_done."""
    if t <= 0:
        return 0.0
    if t >= t_done:
        return 100.0
    l0, l1 = _logistic(0, t50, w), _logistic(t_done, t50, w)
    return max(0.0, min(100.0, 100.0 * (_logistic(t, t50, w) - l0) / (l1 - l0)))


def uf_curve(uf: str, seed: int) -> tuple[float, float, float]:
    rng = random.Random(f"{seed}-curve-{uf}")
    if uf == config.EXTERIOR:
        t50, w = 3.3 + rng.uniform(-0.2, 0.2), 0.75
    else:
        region = config.UFS[uf][2]
        t50 = REGION_T50[region] + 0.22 * math.log10(ELECTORATE_M.get(uf, 2.0) / 5.0) + rng.uniform(-0.25, 0.25)
        w = rng.uniform(0.40, 0.60)
    return t50, w, min(LAST_DONE_H, t50 + 4.6 * w)


def mun_curve(uf: str, mun: str, seed: int, capital: bool = False) -> tuple[float, float, float]:
    t50, w, _ = uf_curve(uf, seed)
    rng = random.Random(f"{seed}-curve-{uf}-{mun}")
    t50 = max(0.35, t50 + rng.gauss(0, 0.45) + (0.35 if capital else 0.0))
    w = w * rng.uniform(0.25, 0.45)
    return t50, w, min(LAST_DONE_H, t50 + 4.6 * w)


# ============================================================================ votos

def _iter_cands(doc: dict):
    for agr in doc["carg"][0].get("agr", []):
        for par in agr.get("par", []):
            yield from par.get("cand", [])


def _gamma_w(seed: int, sqcand: str) -> float:
    """Mesmo peso-base que fake.fill_votes usa para o candidato."""
    return random.Random(zlib.crc32(f"{seed}-{sqcand}".encode())).gammavariate(0.5, 1.0)


def calibrated_bias(doc: dict, targets: dict[str, float], seed: int) -> dict[str, float]:
    """`bias` para fill_votes que faz a área terminar com as participações `targets` (número -> fração).

    Replica o ruído por área de fill_votes (Random(f'{seed}-{cdabr}'): 3 sorteios de totais e um por
    candidato) para cancelá-lo. Se fill_votes mudar, o resultado só fica mais ruidoso.
    """
    rng = random.Random(f"{seed}-{doc.get('cdabr')}")
    for _ in range(3):
        rng.random()
    bias = {}
    for c in _iter_cands(doc):
        g = _gamma_w(seed, c["sqcand"])
        u = rng.uniform(0.6, 1.4)
        bias[c.get("n", "")] = targets.get(c.get("n", ""), 0.0) / max(g * u, 1e-12)
    return bias


def president_roles(doc: dict) -> tuple[str | None, ...]:
    """(A, B, C, D): A forte no NE/N ('13'), B forte no S/SE/CO ('22'), C 3º ('55'), D 4º ('30')."""
    nums = [c.get("n", "") for c in _iter_cands(doc)]
    roles: list[str | None] = [p if p in nums else None for p in ("13", "22", "55", "30")]
    for i, r in enumerate(roles):
        if r is None:
            rest = [n for n in nums if n not in roles]
            roles[i] = rest[0] if rest else None
    return tuple(roles)


def president_targets(doc: dict, roles: tuple, uf: str, mun: str, frac: float, seed: int) -> dict[str, float]:
    region = "Exterior" if uf == config.EXTERIOR else config.UFS[uf][2]
    a, b, c, d = REGION_SHARES[region]
    rng = random.Random(f"{seed}-pres-{uf}")
    a += rng.uniform(-0.04, 0.04)
    b += rng.uniform(-0.04, 0.04)
    if mun:
        delta = random.Random(f"{seed}-pres-{uf}-{mun}").uniform(-0.12, 0.12)
        a, b = a + delta, b - 0.8 * delta
    drift = 0.03 * (frac - 0.5)  # dentro da área, A cresce um pouco ao longo da apuração
    a, b = a + drift, b - drift
    shares = {}
    for role, s in zip(roles, (a, b, c, d)):
        if role:
            shares[role] = max(0.01, s)
    for num, (home, bonus) in HOME_BOOST.items():
        if num in shares and home == uf:
            scale = (1 - shares[num] - bonus) / max(1e-9, 1 - shares[num])
            shares = {k: (v * scale if k != num else v + bonus) for k, v in shares.items()}
    rest = max(0.015, 1.0 - sum(shares.values()))
    others = [cd for cd in _iter_cands(doc) if cd.get("n") not in shares]
    gs = {cd["n"]: _gamma_w(seed, cd["sqcand"]) for cd in others}
    tot = sum(gs.values()) or 1.0
    for n, g in gs.items():
        shares[n] = rest * g / tot
    return shares


def complete_parties(doc: dict, seed: int) -> dict:
    """Preenche votos por partido (tvtn/tvtl/tval) e, nos proporcionais, votos de legenda (vl).

    O comparecimento e os válidos de fill_votes ficam intactos: a legenda sai dos nominais.
    """
    cg = doc["carg"][0]
    cargo = _i(cg.get("cd"))
    v = doc["v"]
    vv = _i(v.get("vv"))
    pars = [p for agr in cg.get("agr", []) for p in agr.get("par", [])]
    if cargo in config.CARGOS_PROPORCIONAIS and vv > 0:
        nom = {id(p): sum(_i(c.get("vap")) for c in p.get("cand", [])) for p in pars}
        leg = {id(p): nom[id(p)] * random.Random(f"{seed}-leg-{p.get('n')}").uniform(0.02, 0.09) for p in pars}
        total_nom, total_leg = sum(nom.values()) or 1, sum(leg.values())
        scale = (vv - total_leg) / total_nom
        for p in pars:
            for c in p.get("cand", []):
                n = int(_i(c.get("vap")) * scale)
                c["vap"], c["pvap"] = str(n), _p(100 * n / vv)
        vl = vv - sum(_i(c.get("vap")) for p in pars for c in p.get("cand", []))
        legs = [int(leg[id(p)]) for p in pars]
        if pars:  # resto do arredondamento vai para o maior partido
            imax = max(range(len(pars)), key=lambda i: legs[i])
            legs[imax] += vl - sum(legs)
        for p, lg in zip(pars, legs):
            p["tvtl"] = str(max(0, lg))
        v.update(vl=str(vl), vnom=str(vv - vl), pvnom=_p(100 * (vv - vl) / max(1, _i(v.get("tv")))))
    for p in pars:
        n = sum(_i(c.get("vap")) for c in p.get("cand", []))
        p["tvtn"] = str(n)
        p["tval"] = str(n + _i(p.get("tvtl")))
    return doc


def _finalize_majoritario(doc: dict) -> None:
    cg = doc["carg"][0]
    cands = sorted(_iter_cands(doc), key=lambda c: -_i(c.get("vap")))
    vv = _i(doc["v"].get("vv"))
    for c in cands:
        c["e"], c["st"] = "n", ""
    if not cands:
        return
    if int(cg["cd"]) in (1, 3):
        if vv and _i(cands[0]["vap"]) / vv > 0.5:
            cands[0].update(e="s", st="Eleito")
        else:
            for c in cands[:2]:
                c["st"] = "2º turno"
    else:
        for c in cands[: _i(cg.get("nv")) or 1]:
            c.update(e="s", st="Eleito")


def aggregate(base: dict, subdocs: list[dict], when: datetime) -> dict:
    """Soma exata dos arquivos de UF (+ exterior) no documento nacional."""
    d = copy.deepcopy(base)

    def tot(sec, key):
        return sum(_i(x[sec].get(key)) for x in subdocs)

    ts, st = tot("s", "ts"), tot("s", "st")
    pct = 100.0 * st / ts if ts else 0.0
    d["s"].update(ts=str(ts), st=str(st), pst=_p(pct), snt=str(ts - st), psnt=_p(100 - pct))
    te, est, comp = tot("e", "te"), tot("e", "est"), tot("e", "c")
    d["e"].update(te=str(te), est=str(est), pest=_p(100 * est / te if te else 0), c=str(comp), a=str(est - comp),
                  pc=_p(100 * comp / est if est else 0), pa=_p(100 * (est - comp) / est if est else 0))
    vals = {k: tot("v", k) for k in ("tv", "vb", "tvn", "vn", "vv", "vnom", "vl", "van")}
    d["v"].update({k: str(n) for k, n in vals.items()})
    d["v"]["pvv"] = _p(100 * vals["vv"] / vals["tv"] if vals["tv"] else 0)
    votes: dict[str, int] = defaultdict(int)
    for x in subdocs:
        for c in _iter_cands(x):
            votes[c.get("n", "")] += _i(c.get("vap"))
    vv = vals["vv"]
    for c in _iter_cands(d):
        n = votes.get(c.get("n", ""), 0)
        c["vap"], c["pvap"], c["e"], c["st"] = str(n), _p(100 * n / vv if vv else 0), "n", ""
    complete_parties(d, 0)
    if subdocs and all(x.get("tf") == "s" for x in subdocs) and pct >= 99.995:
        d["tf"] = "s"
        _finalize_majoritario(d)
    else:
        d["tf"] = "n"
    d["dg"], d["hg"] = when.strftime("%d/%m/%Y"), when.strftime("%H:%M:%S")
    return d


# ============================================================================ simulação

def _kind(cargo: int) -> str:
    return "prop" if cargo in config.CARGOS_PROPORCIONAIS else "maj"


class Simulation:
    """Estado da simulação. `step(conn, sim_time)` grava um "tick" no banco."""

    def __init__(self, base: dict[tuple, Path | dict], seed: int = 1):
        self.seed = seed
        self.state_docs: dict[tuple, dict] = {}
        self.mun_paths: dict[tuple, Path] = {}
        self.last_pct: dict[tuple, float] = {}
        self.capitals: set[str] = set()
        self.mun_ufs: set[str] = set()
        self.br_leader: str | None = None
        self.br_last_pct = 0.0
        self.add_base(base)
        pres = self.state_docs.get(BR_KEY) or next((d for k, d in self.state_docs.items() if k[1] == 1), None)
        self.roles = president_roles(pres) if pres else (None, None, None, None)
        self._pending: list[dict] = []
        self._lock = threading.Lock()

    # -- base
    def add_base(self, base: dict[tuple, Path | dict]) -> None:
        for k, v in base.items():
            if k[3]:
                self.mun_paths[k] = v
                self.mun_ufs.add(k[2])
            else:
                self.state_docs[k] = v if isinstance(v, dict) else load_doc(v)
        if any(k[3] for k in base):
            try:
                df = geo.municipios()
                self.capitals = set(df.loc[df["capital"], "mun"])
            except Exception:  # noqa: BLE001
                pass

    def add_base_async(self, uf: str, offline: bool) -> None:
        """Baixa (em thread) a base municipal de uma UF que entrou em foco; entra no próximo tick."""
        self.mun_ufs.add(uf)

        def work():
            try:
                found = ensure_base(mun_keys(uf), offline=offline)
                with self._lock:
                    self._pending.append(found)
                log(f"UF {uf}: {len(found)} arquivos municipais prontos")
            except Exception as exc:  # noqa: BLE001
                log(f"UF {uf}: falha ao preparar base municipal: {exc}")

        threading.Thread(target=work, daemon=True).start()

    def _absorb_pending(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, []
        for found in pending:
            self.add_base(found)

    # -- progresso
    def area_pct(self, key: tuple, t_h: float) -> float:
        _, _, uf, mun = key
        if mun:
            curve = mun_curve(uf, mun, self.seed, capital=mun in self.capitals)
        else:
            curve = uf_curve(uf, self.seed)
        return round(curve_pct(t_h, *curve), 2)

    @property
    def done(self) -> bool:
        keys = list(self.state_docs) + list(self.mun_paths)
        return bool(keys) and all(self.last_pct.get(k, 0) >= 100 for k in keys)

    def load_state(self, conn) -> None:
        self.br_leader = store.get_meta(conn, "sim_br_leader") or None
        self.br_last_pct = float(store.get_meta(conn, "sim_br_pct") or 0)

    # -- um área
    def _fill(self, key: tuple, doc: dict, pct: float, when: datetime) -> dict:
        eleicao, cargo, uf, mun = key
        bias = None
        if cargo == 1 and any(self.roles):
            targets = president_targets(doc, self.roles, uf, mun, pct / 100.0, self.seed)
            bias = calibrated_bias(doc, targets, self.seed)
        elif cargo != 1:
            bias = {c.get("n", ""): PARTY_STRENGTH.get(c.get("n", "")[:2], 0.5) for c in _iter_cands(doc)}
        filled = fake.fill_votes(doc, pct, seed=self.seed, when=when, bias=bias)
        return complete_parties(filled, self.seed)

    def _write(self, conn, key: tuple, doc: dict) -> parse.Snapshot:
        snap = parse.parse_u(doc, uf=key[2] if key[3] else None)
        store.write_snapshot(conn, snap)
        return snap

    def _due(self, key: tuple, pct: float, force: bool) -> bool:
        if force:
            return True
        last = self.last_pct.get(key)
        if last is None:
            return True
        if last >= 100:
            return False
        if pct >= 100:
            return True
        return abs(pct - last) >= THRESHOLD[("mu" if key[3] else "uf", _kind(key[1]))]

    # -- tick
    def step(self, conn, sim_time: datetime, pct: float | None = None, force: bool = False) -> dict:
        """Grava as áreas cujo progresso mudou. `pct` fixa o mesmo % para todas as áreas (modo --instant)."""
        self._absorb_pending()
        t_h = (sim_time - SIM_START).total_seconds() / 3600.0
        written = 0
        pres_filled: dict[str, dict] = {}
        pres_pct: dict[str, float] = {}
        for key, doc in self.state_docs.items():
            if key == BR_KEY:
                continue
            p = pct if pct is not None else self.area_pct(key, t_h)
            due = self._due(key, p, force)
            if key[1] == 1:
                filled = self._fill(key, doc, p, sim_time)
                pres_filled[key[2]], pres_pct[key[2]] = filled, p
            elif due:
                filled = self._fill(key, doc, p, sim_time)
            else:
                continue
            if due:
                snap = self._write(conn, key, filled)
                written += 1
                if key[1] == 3 and snap.finalizada and self.last_pct.get(key, 0) < 100:
                    self._governor_event(conn, key[2], snap)
                self.last_pct[key] = p
        mun_written = 0
        for key, path in list(self.mun_paths.items()):
            if not force and mun_written >= MAX_MUN_WRITES_PER_TICK:
                break
            p = pct if pct is not None else self.area_pct(key, t_h)
            if not self._due(key, p, force):
                continue
            try:
                doc = load_doc(path)
            except (OSError, ValueError):
                continue
            self._write(conn, key, self._fill(key, doc, p, sim_time))
            self.last_pct[key] = p
            written += 1
            mun_written += 1
        br_snap = self._write_br(conn, sim_time, pres_filled, pres_pct, pct)
        now = datetime.now().isoformat(timespec="seconds")
        store.set_meta(conn, "collector_heartbeat", now)
        store.set_meta(conn, "collector_last_tse_ts", sim_time.isoformat(timespec="seconds"))
        store.set_meta(conn, "sim_clock", sim_time.isoformat(timespec="seconds"))
        return {"written": written, "br_pct": br_snap.totals.pct_secoes if br_snap else None,
                "leader": br_snap.candidates[0].nome_urna if br_snap and br_snap.candidates else None}

    def _write_br(self, conn, when, pres_filled, pres_pct, pct) -> parse.Snapshot | None:
        base = self.state_docs.get(BR_KEY)
        if base is None:
            return None
        expected = set(config.UF_LIST) | {config.EXTERIOR}
        if expected <= set(pres_filled):
            doc = aggregate(base, [pres_filled[u] for u in sorted(expected)], when)
        else:  # base incompleta (ex. --offline): curva e votos estimados pelo eleitorado de cada UF
            weights = {u: ELECTORATE_M[u] for u in expected}
            if pct is not None:
                p = pct
            else:
                t_h = (when - SIM_START).total_seconds() / 3600.0
                p = sum(w * curve_pct(t_h, *uf_curve(u, self.seed)) for u, w in weights.items()) / sum(weights.values())
                p = round(p, 2)
            bias = None
            if any(self.roles):
                t_h = (when - SIM_START).total_seconds() / 3600.0
                mix: dict[str, float] = defaultdict(float)
                wsum = 0.0
                for u, w in weights.items():
                    up = pct if pct is not None else curve_pct(t_h, *uf_curve(u, self.seed))
                    if up <= 0:
                        continue
                    for n, s in president_targets(base, self.roles, u, "", up / 100, self.seed).items():
                        mix[n] += w * up * s
                    wsum += w * up
                if wsum:
                    bias = calibrated_bias(base, {n: s / wsum for n, s in mix.items()}, self.seed)
            doc = complete_parties(fake.fill_votes(base, p, seed=self.seed, when=when, bias=bias), self.seed)
        snap = parse.parse_u(doc)
        if self.last_pct.get(BR_KEY) is not None and self.last_pct[BR_KEY] >= 100 and pct is None:
            return snap
        store.write_snapshot(conn, snap)
        self.last_pct[BR_KEY] = snap.totals.pct_secoes if not snap.finalizada else 100.0
        self._br_events(conn, snap)
        return snap

    # -- eventos
    def _br_events(self, conn, snap: parse.Snapshot) -> None:
        pct = snap.totals.pct_secoes
        top = snap.candidates[0] if snap.candidates and snap.candidates[0].votos > 0 else None
        if top and top.numero != self.br_leader:
            verb = "assume a liderança" if self.br_leader else "lidera a apuração"
            store.add_event(conn, "lider", f"Presidente: {top.nome_urna} ({top.partido}) {verb} com "
                            f"{_br(top.pct_validos)}% dos válidos ({_br(pct)}% das seções apuradas)",
                            PRES, 1, "br")
            self.br_leader = top.numero
            store.set_meta(conn, "sim_br_leader", top.numero)
        for m in MILESTONES:
            if self.br_last_pct < m <= pct + 1e-9 and top:
                store.add_event(conn, "marco", f"Presidente: {m}% das seções apuradas — {top.nome_urna} "
                                f"({top.partido}) lidera com {_br(top.pct_validos)}% dos válidos", PRES, 1, "br")
        if snap.finalizada and self.br_last_pct < 100 and snap.candidates:
            el = [c for c in snap.candidates if c.eleito]
            if el:
                store.add_event(conn, "eleito", f"{el[0].nome_urna} ({el[0].partido}) é eleito(a) Presidente com "
                                f"{_br(el[0].pct_validos)}% dos válidos", PRES, 1, "br")
            else:
                a, b = snap.candidates[0], snap.candidates[1]
                store.add_event(conn, "segundo_turno", f"Presidente: 2º turno entre {a.nome_urna} ({a.partido}, "
                                f"{_br(a.pct_validos)}%) e {b.nome_urna} ({b.partido}, {_br(b.pct_validos)}%)",
                                PRES, 1, "br")
        self.br_last_pct = 100.0 if snap.finalizada else pct
        store.set_meta(conn, "sim_br_pct", str(self.br_last_pct))

    def _governor_event(self, conn, uf: str, snap: parse.Snapshot) -> None:
        nome_uf = config.UFS.get(uf, (uf.upper(),))[0]
        el = [c for c in snap.candidates if c.eleito]
        if el:
            store.add_event(conn, "eleito", f"{el[0].nome_urna} ({el[0].partido}) é eleito(a) governador(a) "
                            f"de {nome_uf} com {_br(el[0].pct_validos)}% dos válidos", GERAL, 3, uf)
        elif len(snap.candidates) >= 2:
            a, b = snap.candidates[0], snap.candidates[1]
            store.add_event(conn, "segundo_turno", f"Governador de {nome_uf}: 2º turno entre {a.nome_urna} "
                            f"({a.partido}) e {b.nome_urna} ({b.partido})", GERAL, 3, uf)


def simulate_step(conn, sim: Simulation, sim_time: datetime, pct: float | None = None,
                  force: bool = False) -> dict:
    """Função de conveniência (testes): um tick da simulação."""
    return sim.step(conn, sim_time, pct=pct, force=force)


def build_simulation(mun_ufs: list[str], seed: int = 1, offline: bool = False) -> Simulation:
    keys = state_keys()
    for uf in mun_ufs:
        keys += mun_keys(uf)
    base = ensure_base(keys, offline=offline)
    n_mun = sum(1 for k in base if k[3])
    log(f"base: {len(base) - n_mun} arquivos br/UF, {n_mun} municipais")
    return Simulation(base, seed=seed)


def reset_db(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()


def clear_results(conn) -> None:
    """Apaga resultados e eventos (mantém foco e meta) — usado por --loop."""
    conn.execute("PRAGMA foreign_keys=OFF")
    with conn:
        for t in ("cand_result", "party_result", "latest", "snapshot", "event"):
            conn.execute(f"DELETE FROM {t}")
        conn.execute("DELETE FROM meta WHERE key IN ('sim_br_leader', 'sim_br_pct')")
    conn.execute("PRAGMA foreign_keys=ON")


# ============================================================================ CLI

def _check_focus(conn, sim: Simulation, offline: bool) -> None:
    for uf in store.get_focus(conn):
        if uf in config.UFS and uf not in sim.mun_ufs:
            log(f"UF {uf} entrou em foco: preparando base municipal")
            sim.add_base_async(uf, offline)


def run_live(conn, sim: Simulation, minutes: float, tick: float, offline: bool, loop: bool) -> None:
    total = max(1.0, minutes * 60.0)
    while True:
        t0 = time.monotonic()
        while True:
            ts = time.monotonic()
            frac = min(1.0, (ts - t0) / total)
            sim_time = SIM_START + timedelta(hours=SIM_HOURS * frac)
            sim_time = sim_time.replace(microsecond=0)
            _check_focus(conn, sim, offline)
            info = sim.step(conn, sim_time)
            dt = time.monotonic() - ts
            log(f"{sim_time:%H:%M:%S} br={info['br_pct']}% líder={info['leader']} "
                f"gravados={info['written']} ({dt:.1f}s)")
            if frac >= 1.0 and sim.done:
                break
            time.sleep(max(0.0, tick - dt))
        store.set_meta(conn, "sim_status", "done")
        log("apuração simulada concluída (100%)")
        if not loop:
            return
        log("--loop: reiniciando")
        clear_results(conn)
        sim.last_pct.clear()
        sim.br_leader, sim.br_last_pct = None, 0.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Simulador da apuração (grava em data/sim.db)")
    ap.add_argument("--db", default=str(config.SIM_DB_PATH))
    ap.add_argument("--minutes", type=float, default=10.0, help="duração real da apuração inteira (min)")
    ap.add_argument("--tick", type=float, default=5.0, help="intervalo entre gravações (s)")
    ap.add_argument("--mun-ufs", default="rs", help="UFs simuladas por município, separadas por vírgula ('' = nenhuma)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--reset", action="store_true", help="apaga o banco antes de começar")
    ap.add_argument("--offline", action="store_true", help="usa só data/sim_base/ e tests/fixtures/ (sem rede)")
    ap.add_argument("--instant", type=float, default=None, metavar="PCT",
                    help="grava um único snapshot de todas as áreas com PCT%% e sai")
    ap.add_argument("--clock", default=None, metavar="HH:MM", help="horário simulado no modo --instant")
    ap.add_argument("--loop", action="store_true", help="ao chegar a 100%%, recomeça")
    ap.add_argument("--prefetch", action="store_true", help="só baixa os arquivos base e sai")
    args = ap.parse_args(argv)

    mun_ufs = [u.strip().lower() for u in args.mun_ufs.split(",") if u.strip()]
    db = Path(args.db)
    if args.reset:
        reset_db(db)
    if args.prefetch:
        t0 = time.monotonic()
        build_simulation(mun_ufs, seed=args.seed, offline=args.offline)
        log(f"prefetch pronto em {time.monotonic() - t0:.1f}s")
        return 0
    conn = store.connect(db)
    for uf in mun_ufs:
        store.add_focus(conn, uf)
    focus = [u for u in store.get_focus(conn) if u in config.UFS]
    sim = build_simulation(sorted(set(mun_ufs) | set(focus)), seed=args.seed, offline=args.offline)
    if args.instant is None and not args.reset:
        clear_results(conn)  # o modo ao vivo sempre recomeça às 17:00: histórico antigo confundiria `latest`
    sim.load_state(conn)
    store.set_meta(conn, "sim_status", "running")
    store.set_meta(conn, "sim_seed", str(args.seed))

    if args.instant is not None:
        if args.clock:
            hh, mm = (int(x) for x in args.clock.split(":"))
            when = SIM_START.replace(hour=hh, minute=mm)
        else:
            when = SIM_START + timedelta(hours=5.0 * min(100.0, args.instant) / 100.0)
        t0 = time.monotonic()
        info = sim.step(conn, when, pct=float(args.instant), force=True)
        store.set_meta(conn, "sim_status", "done" if args.instant >= 100 else "instant")
        log(f"instant {args.instant}% @ {when:%H:%M}: {info['written']} áreas, br={info['br_pct']}% "
            f"líder={info['leader']} ({time.monotonic() - t0:.1f}s)")
        return 0
    try:
        run_live(conn, sim, args.minutes, args.tick, args.offline, args.loop)
    except KeyboardInterrupt:
        log("interrompido")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
