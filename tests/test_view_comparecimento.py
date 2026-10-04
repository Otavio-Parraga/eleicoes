"""Testes da tela Comparecimento: funções puras de taxa + smoke test com AppTest."""
import json
import logging
import math
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

from eleicoes import config, fake, geo, parse, store
from eleicoes.views import comparecimento as cv

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def put(conn, doc: dict, pct: float, minute: int = 0, uf: str | None = None) -> None:
    filled = fake.fill_votes(doc, pct, seed=1, when=datetime(2026, 10, 4, 17, minute))
    store.write_snapshot(conn, parse.parse_u(filled, uf=uf))


def build_db(path: Path, n_mun: int = 25) -> None:
    conn = store.connect(path)
    put(conn, load("br-c0001-e006257-u.json"), 40)
    put(conn, load("rs-c0001-e006257-u.json"), 50)
    put(conn, load("sp-c0001-e006257-u.json"), 0)      # SP sem nada apurado -> NaN
    put(conn, load("zz-c0001-e006257-u.json"), 30)
    for cargo in (3, 5, 6, 7):
        put(conn, load(f"rs-c{cargo:04d}-e006259-u.json"), 50)
    put(conn, load("sp-c0003-e006259-u.json"), 20)
    put(conn, load("df-c0008-e006259-u.json"), 10)
    put(conn, load("rs88013-c0006-e006259-u.json"), 60, uf="rs")
    poa = load("rs88013-c0001-e006257-u.json")
    muns = geo.municipios_uf("rs")["mun"].tolist()[:n_mun]
    for i, mun in enumerate(muns):
        d = dict(poa)
        d["cdabr"] = mun
        put(conn, d, 0 if i % 5 == 0 else 50 + i, uf="rs")  # 1 em cada 5 sem apuração
    conn.close()


def row(**kw) -> dict:
    base = {c: 0 for c in cv.COUNT_COLS}
    base.update(kw)
    return base


class RateTests(unittest.TestCase):
    def test_add_rates_values_and_nan(self):
        df = pd.DataFrame([
            row(eleitorado=1000, eleitorado_apurado=500, comparecimento=400, abstencao=100,
                votos_total=400, brancos=20, nulos=30),
            row(eleitorado=1000, eleitorado_apurado=0),  # nada apurado
        ])
        out = cv.add_rates(df)
        self.assertAlmostEqual(out.loc[0, "abst_pct"], 20.0)
        self.assertAlmostEqual(out.loc[0, "comp_pct"], 80.0)
        self.assertAlmostEqual(out.loc[0, "brancos_pct"], 5.0)
        self.assertAlmostEqual(out.loc[0, "nulos_pct"], 7.5)
        self.assertAlmostEqual(out.loc[0, "bn_pct"], 12.5)
        for c in ("abst_pct", "comp_pct", "brancos_pct", "nulos_pct", "bn_pct"):
            self.assertTrue(math.isnan(out.loc[1, c]), c)

    def test_rates_use_counted_part_not_total_electorate(self):
        out = cv.add_rates(pd.DataFrame([row(eleitorado=10_000, eleitorado_apurado=100, abstencao=25,
                                             comparecimento=75, votos_total=75)]))
        self.assertAlmostEqual(out.loc[0, "abst_pct"], 25.0)

    def test_add_rates_empty(self):
        out = cv.add_rates(pd.DataFrame(columns=cv.COUNT_COLS))
        self.assertIn("abst_pct", out)
        self.assertTrue(out.empty)

    def test_aggregate_sums_counts(self):
        df = pd.DataFrame([
            row(secoes_total=10, secoes_totalizadas=5, eleitorado_apurado=100, abstencao=10, comparecimento=90,
                votos_total=90, brancos=9, nulos=0),
            row(secoes_total=10, secoes_totalizadas=0, eleitorado_apurado=0),
            row(secoes_total=20, secoes_totalizadas=20, eleitorado_apurado=300, abstencao=90, comparecimento=210,
                votos_total=210, brancos=1, nulos=30),
        ])
        r = cv.aggregate(df)
        self.assertEqual(r["eleitorado_apurado"], 400)
        self.assertAlmostEqual(r["abst_pct"], 100 * 100 / 400)  # ponderado, não média das taxas
        self.assertAlmostEqual(r["brancos_pct"], 100 * 10 / 300)
        self.assertAlmostEqual(r["nulos_pct"], 100 * 30 / 300)
        self.assertAlmostEqual(r["pct_secoes"], 100 * 25 / 40)
        self.assertEqual(cv.aggregate(pd.DataFrame()), {})

    def test_aggregate_nothing_counted_is_nan(self):
        r = cv.aggregate(pd.DataFrame([row(secoes_total=10, eleitorado=1000)]))
        self.assertTrue(math.isnan(r["abst_pct"]))
        self.assertTrue(math.isnan(r["brancos_pct"]))
        self.assertEqual(r["pct_secoes"], 0.0)

    def test_top_bottom_ignores_nan(self):
        df = pd.DataFrame({"nome": list("abcde"), "x": [1.0, float("nan"), 3.0, 2.0, float("nan")]})
        top, bottom = cv.top_bottom(df, "x", n=2)
        self.assertEqual(top["nome"].tolist(), ["c", "d"])
        self.assertEqual(bottom["nome"].tolist(), ["a", "d"])

    def test_map_grey_layer_for_nan(self):
        gj = geo.states_geojson()
        df = pd.DataFrame({"uf": config.UF_LIST, "nome": config.UF_LIST})
        df["x"] = float("nan")
        df.loc[df["uf"].isin(["rs", "sp", "ba"]), "x"] = [20.0, 25.0, 30.0]
        df["hover"] = "h"
        fig = cv._map(df, gj, "uf", "x", "X", 400)
        self.assertEqual(len(fig.data), 2)
        val, grey = fig.data
        self.assertEqual(sorted(val.locations), ["ba", "rs", "sp"])
        self.assertEqual(len(val.geojson["features"]), 3)
        self.assertEqual(len(grey.locations), 24)  # NaN nunca vira 0 na escala: vai para a camada cinza
        df["x"] = float("nan")
        fig = cv._map(df, gj, "uf", "x", "X", 400)  # nada apurado em lugar nenhum: não quebra
        self.assertEqual(len(fig.data[-1].locations), 27)

    def test_color_range(self):
        self.assertEqual(cv.color_range(pd.Series([float("nan")])), (0.0, 1.0))
        lo, hi = cv.color_range(pd.Series([20.0, 20.0]))
        self.assertLess(lo, 20.0)
        self.assertGreater(hi, 20.0)


class StoreBackedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "t.db"
        build_db(cls.db)
        cls.conn = store.connect(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def test_area_row_br_presidente_uses_br_row(self):
        r = cv.area_row(self.conn, "6257", 1, "br")
        br = store.latest_totals(self.conn, "6257", 1, "br").iloc[0]
        self.assertEqual(r["eleitorado_apurado"], br["eleitorado_apurado"])
        self.assertAlmostEqual(r["abst_pct"], 100 * br["abstencao"] / br["eleitorado_apurado"])

    def test_area_row_br_senado_sums_ufs(self):
        r = cv.area_row(self.conn, "6259", 5, "br")
        ufs = store.latest_totals(self.conn, "6259", 5, "uf")
        self.assertEqual(r["votos_total"], ufs["votos_total"].sum())
        self.assertAlmostEqual(r["nulos_pct"], 100 * ufs["nulos"].sum() / ufs["votos_total"].sum())

    def test_area_row_sp_nothing_counted(self):
        r = cv.area_row(self.conn, "6257", 1, "sp")
        self.assertTrue(math.isnan(r["abst_pct"]))

    def test_rates_by_cargo(self):
        rs = cv.rates_by_cargo(self.conn, 1, "rs")
        self.assertEqual(rs["cargo"].tolist(), [1, 3, 5, 6, 7])
        self.assertTrue(rs["brancos_pct"].between(0, 100).all())
        t2 = cv.rates_by_cargo(self.conn, 2, "rs")  # 2º turno sem dados ainda
        self.assertTrue(t2.empty)
        sp = cv.rates_by_cargo(self.conn, 1, "sp")  # Presidente SP zerado -> fica de fora
        self.assertEqual(sp["cargo"].tolist(), [3])


class AppSmokeTests(unittest.TestCase):
    """Roda app.py com AppTest em cada cenário pedido e exige zero exceções."""

    @classmethod
    def setUpClass(cls):
        logging.getLogger("streamlit").setLevel(logging.ERROR)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "smoke.db"
        build_db(cls.db)
        cls.empty = Path(cls.tmp.name) / "empty.db"
        store.connect(cls.empty).close()
        cls._old = config.DB_PATH

    @classmethod
    def tearDownClass(cls):
        config.DB_PATH = cls._old
        cls.tmp.cleanup()

    def run_app(self, db: Path, uf: str, cargo: int, metric: str | None = None, turno: int = 1):
        from streamlit.testing.v1 import AppTest
        os.environ["ELEICOES_DB"] = str(db)
        config.DB_PATH = db  # app._db_path lê config.DB_PATH na hora
        cwd = os.getcwd()
        os.chdir(ROOT)
        try:
            at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
            at.session_state["view"] = "Comparecimento"
            at.session_state["uf"] = uf
            at.session_state["cargo"] = cargo
            at.session_state["turno"] = turno
            if metric:
                at.session_state[cv.METRIC_KEY] = metric
            # a tela engole exceções (mostra no_data) mas registra log.exception: isso também é falha aqui
            with self.assertNoLogs(cv.log.name, level="ERROR"):
                at.run()
        finally:
            os.chdir(cwd)
        self.assertFalse(at.exception, [e.value for e in at.exception])
        return at

    def test_scenarios(self):
        for uf, cargo in [("br", 1), ("rs", 1), ("br", 5), ("rs", 6), ("sp", 1), ("zz", 1), ("df", 7)]:
            for metric in (None, "Brancos+Nulos"):
                with self.subTest(uf=uf, cargo=cargo, metric=metric):
                    at = self.run_app(self.db, uf, cargo, metric)
                    self.assertGreaterEqual(len(at.get("plotly_chart")), 1)

    def test_rs_municipal_tables(self):
        at = self.run_app(self.db, "rs", 1, "Abstenção")
        self.assertEqual(len(at.dataframe), 2)
        top = at.dataframe[0].value
        self.assertLessEqual(len(top), 10)
        self.assertGreater(len(top), 0)

    def test_rs_without_municipal_data_falls_back(self):
        # Governador não tem arquivos de município no banco de teste -> nota com o total do estado
        at = self.run_app(self.db, "rs", 3, "Brancos")
        self.assertTrue(any("ainda não chegaram" in i.value for i in at.info))
        self.assertEqual(len(at.dataframe), 0)
        # abstenção é igual em todos os cargos: usa os municípios de Presidente
        at = self.run_app(self.db, "rs", 3, "Abstenção")
        self.assertEqual(len(at.dataframe), 2)
        self.assertTrue(any("Presidente" in c.value for c in at.caption))

    def test_empty_db(self):
        for uf, cargo in [("br", 1), ("rs", 1), ("br", 5), ("rs", 6)]:
            with self.subTest(uf=uf, cargo=cargo):
                at = self.run_app(self.empty, uf, cargo)
                self.assertTrue(at.info)  # no_data()

    def test_turno2(self):
        self.run_app(self.db, "rs", 1, turno=2)

    def test_sim_db_if_present(self):
        if not config.SIM_DB_PATH.exists():
            self.skipTest("data/sim.db ainda não existe")
        import sqlite3
        copy = Path(self.tmp.name) / "sim_copy.db"  # não escrever (add_focus) no banco do simulador
        src, dst = sqlite3.connect(config.SIM_DB_PATH), sqlite3.connect(copy)
        src.backup(dst)
        src.close()
        dst.close()
        for uf, cargo in [("br", 1), ("rs", 1), ("br", 5), ("rs", 6)]:
            with self.subTest(uf=uf, cargo=cargo):
                self.run_app(copy, uf, cargo)


if __name__ == "__main__":
    unittest.main()
