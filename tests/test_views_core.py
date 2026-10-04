"""Testes das telas principais (Mapa, Ranking) e do app.py via streamlit AppTest.

Reutilizável: `build_db(path)` monta um banco de teste a partir das fixtures com votos sintéticos
(níveis br/uf para vários cargos + alguns municípios do RS) e `run_app(db, **state)` roda o app.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from eleicoes import config, fake, geo, parse, store

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]

UF_FILES = [
    ("br-c0001-e006257-u.json", None), ("rs-c0001-e006257-u.json", None), ("sp-c0001-e006257-u.json", None),
    ("zz-c0001-e006257-u.json", None), ("rs-c0003-e006259-u.json", None), ("sp-c0003-e006259-u.json", None),
    ("rs-c0005-e006259-u.json", None), ("rs-c0006-e006259-u.json", None), ("rs-c0007-e006259-u.json", None),
    ("df-c0008-e006259-u.json", None),
]
MUN_FILES = ["rs88013-c0001-e006257-u.json", "rs88013-c0006-e006259-u.json"]
FAIL_MARKERS = ("Não foi possível", "falhou")


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def build_db(path: str | Path, *, n_mun: int = 25, final: bool = False) -> None:
    """Banco de teste: 2 snapshots por arquivo (17:10 e 17:40) + `n_mun` municípios do RS + 2 eventos."""
    conn = store.connect(path)
    try:
        for i, (name, uf) in enumerate(UF_FILES):
            doc = load(name)
            for m, pct in ((10, 20.0), (40, 100.0 if final else 55.0)):
                store.write_snapshot(conn, parse.parse_u(
                    fake.fill_votes(doc, pct, seed=1, when=datetime(2026, 10, 4, 17, m)), uf=uf))
        muns = geo.municipios_uf("rs")["mun"].tolist()[:n_mun]
        for name in MUN_FILES:
            base = load(name)
            for j, mun in enumerate(muns):
                d = copy.deepcopy(base)
                d["cdabr"] = mun
                store.write_snapshot(conn, parse.parse_u(
                    fake.fill_votes(d, 30.0 + j, seed=1 + j, when=datetime(2026, 10, 4, 17, 30)), uf="rs"))
        store.add_event(conn, "lead_change", "Evento antigo", "6257", 1, "br")
        store.add_event(conn, "lead_change", "Evento antigo 2", "6257", 1, "rs")
    finally:
        conn.close()


def run_app(db: str | Path, timeout: float = 120, **state):
    """Roda app.py com o banco `db` e o session_state dado (view, uf, cargo, turno, mun...)."""
    from streamlit.testing.v1 import AppTest

    os.environ["ELEICOES_DB"] = str(db)
    config.DB_PATH = Path(db)  # config já pode ter sido importado: força o caminho
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=timeout)
    for k, v in state.items():
        at.session_state[k] = v
    at.run()
    return at


def rerun(at):
    """Roda de novo o mesmo AppTest. Contorna bug do AppTest 1.54: segmented_control de seleção única guarda
    str e o AppTest itera os caracteres ao reenviar o estado."""
    for bg in at.get("button_group"):
        v = bg.value
        if isinstance(v, str):
            bg.set_value([v])
    at.run()
    return at


def failures(at) -> list[str]:
    """Exceções do Streamlit + avisos de falha que as telas mostram no lugar de tracebacks."""
    out = [str(e.value) for e in at.exception]
    # algumas telas avisam a falha com no_data() (st.info) ou st.error em vez de st.warning
    for w in [*at.warning, *at.info, *at.error]:
        if any(m in str(w.value) for m in FAIL_MARKERS):
            out.append(str(w.value))
    return out


class _DBCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "t.db"
        build_db(cls.db)
        cls.empty = Path(cls.tmp.name) / "empty.db"
        store.connect(cls.empty).close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class HelperTests(_DBCase):
    def test_clicked_location(self):
        from eleicoes.views.mapa import clicked_location
        ev = {"selection": {"points": [{"curve_number": 0, "point_index": 3, "location": "sp"}],
                            "point_indices": [3], "box": [], "lasso": []}}
        self.assertEqual(clicked_location(ev), "sp")
        self.assertIsNone(clicked_location({"selection": {"points": [], "point_indices": [], "box": [],
                                                          "lasso": []}}))
        self.assertIsNone(clicked_location(None))

    def test_area_totals_br_sums_ufs(self):
        from eleicoes.views.ranking import area_totals
        conn = store.connect(self.db)
        try:
            t = area_totals(conn, "6259", 3, "br")
            self.assertIsNotNone(t)
            self.assertAlmostEqual(t["pct_secoes"], 55.0, delta=0.5)  # rs + sp, ambos 55%
            t = area_totals(conn, "6257", 1, "br")
            self.assertAlmostEqual(t["pct_secoes"], 55.0, delta=0.01)
            mun = geo.municipios_uf("rs")["mun"].iloc[0]
            self.assertIsNotNone(area_totals(conn, "6257", 1, "rs", mun))
            ec = store.connect(self.empty)
            self.assertIsNone(area_totals(ec, "6257", 1, "br"))
            ec.close()
        finally:
            conn.close()

    def test_candidate_bars(self):
        from eleicoes.views.ranking import candidate_bars, party_bars
        conn = store.connect(self.db)
        try:
            c = store.latest_candidates(conn, "6259", 5, "uf", uf="rs")
            fig = candidate_bars(c, 5, vagas=2)
            self.assertEqual(len(fig.data[0].x), len(c))
            self.assertTrue(any(s.y0 == 1.5 for s in fig.layout.shapes))  # linha de corte das 2 vagas
            fig = candidate_bars(store.latest_candidates(conn, "6257", 1, "br"), 1)
            self.assertTrue(any(s.x0 == 50 for s in fig.layout.shapes))
            p = store.latest_parties(conn, "6259", 6, "uf", uf="rs")
            c6 = store.latest_candidates(conn, "6259", 6, "uf", uf="rs")
            fig = party_bars(p, 1000, cands=c6)  # dados simulados não têm votos nominais por partido
            self.assertIsNotNone(fig)
            self.assertLessEqual(len(fig.data[0].x), 16)  # top 15 + "Outros"
        finally:
            conn.close()


class AppSmokeTests(_DBCase):
    CASES = [
        ("Mapa", "br", 1), ("Mapa", "rs", 1), ("Mapa", "rs", 3), ("Mapa", "br", 5), ("Mapa", "rs", 6),
        ("Mapa", "zz", 1), ("Mapa", "sp", 3), ("Mapa", "df", 7),
        ("Ranking", "br", 1), ("Ranking", "rs", 1), ("Ranking", "rs", 3), ("Ranking", "br", 5),
        ("Ranking", "rs", 6), ("Ranking", "zz", 1), ("Ranking", "rs", 7), ("Ranking", "df", 7),
    ]

    def test_views(self):
        for view, uf, cargo in self.CASES:
            with self.subTest(view=view, uf=uf, cargo=cargo):
                at = run_app(self.db, view=view, uf=uf, cargo=cargo)
                self.assertEqual(failures(at), [])
                self.assertTrue(any("Atualizado pelo TSE" in str(m.value) for m in at.markdown),
                                "cabeçalho sem horário do TSE")

    def test_municipio_selected(self):
        mun = geo.municipios_uf("rs")["mun"].iloc[0]
        for view in ("Mapa", "Ranking"):
            with self.subTest(view=view):
                at = run_app(self.db, view=view, uf="rs", cargo=1, mun=mun)
                self.assertEqual(failures(at), [])

    def test_empty_db(self):
        for view, uf, cargo in [("Mapa", "br", 1), ("Mapa", "rs", 3), ("Ranking", "br", 1), ("Ranking", "rs", 6),
                                ("Ranking", "br", 5), ("Mapa", "zz", 1)]:
            with self.subTest(view=view, uf=uf, cargo=cargo):
                at = run_app(self.empty, view=view, uf=uf, cargo=cargo)
                self.assertEqual(failures(at), [])

    def test_event_toasts(self):
        at = run_app(self.db, uf="br", cargo=1)  # tela padrão (o AppTest não reroda segmented_control setado)
        self.assertEqual(failures(at), [])
        self.assertEqual(len(at.toast), 0)  # primeira carga: não mostra eventos antigos
        conn = store.connect(self.db)
        try:
            store.add_event(conn, "lead_change", "Virada no RS!", "6259", 3, "rs")
        finally:
            conn.close()
        rerun(at)
        self.assertTrue(any("Virada no RS!" in str(t.value) for t in at.toast))

    def test_congresso_single_uf_with_votes(self):
        # Só o RS tem votos para Dep. Federal: o hemiciclo/barras nacionais são idênticos aos do RS. Sem `key`
        # o Streamlit levantava StreamlitDuplicateElementId e a tela inteira caía (no início da apuração).
        at = run_app(self.db, view="Congresso", uf="rs", cargo=6)
        self.assertEqual(failures(at), [])
        self.assertGreaterEqual(len(at.get("plotly_chart")), 4)

    def test_add_focus_on_state(self):
        run_app(self.db, view="Mapa", uf="sp", cargo=1)
        conn = store.connect(self.db)
        try:
            self.assertIn("sp", store.get_focus(conn))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
