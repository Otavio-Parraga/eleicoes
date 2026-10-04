import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

from eleicoes import config, fake, parse, store
from eleicoes.views import evolucao as ev

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def write_night(conn, fixture: str, steps, seed: int = 1, zero_first: bool = True) -> None:
    """steps: lista de (minuto, pct, bias). Grava um snapshot por passo na mesma área."""
    doc = load(fixture)
    if zero_first:  # arquivo zerado antes das 17h
        store.write_snapshot(conn, parse.parse_u(fake.fill_votes(doc, 0, seed=seed,
                                                                 when=datetime(2026, 10, 4, 16, 50))))
    for minute, pct, bias in steps:
        when = datetime(2026, 10, 4, 17 + minute // 60, minute % 60)
        store.write_snapshot(conn, parse.parse_u(fake.fill_votes(doc, pct, seed=seed, when=when, bias=bias)))


# Com seed=1, sem bias: MISSÃO (14) lidera, PCB (21) em 2º. Viés forte inverte e depois desinverte.
BR_VIRADA = [(0, 5, {"21": 3.0}), (10, 20, {"21": 2.0}), (20, 45, {"21": 1.5}),
             (30, 67, {"14": 1.5}), (40, 90, {"14": 1.6}), (50, 100, {"14": 1.6})]


def hist_df(rows):
    """rows: (snapshot_id, minuto, pct_secoes, sqcand, votos)."""
    df = pd.DataFrame(rows, columns=["snapshot_id", "minute", "pct_secoes", "sqcand", "votos"])
    df["tse_ts"] = pd.Timestamp("2026-10-04 17:00") + pd.to_timedelta(df["minute"], unit="min")
    df["numero"] = df["sqcand"]
    df["nome_urna"] = "N" + df["sqcand"]
    df["partido"] = "P" + df["sqcand"]
    tot = df.groupby("snapshot_id")["votos"].transform("sum")
    df["pct_validos"] = (100 * df["votos"] / tot.where(tot > 0)).fillna(0.0)
    return df.drop(columns="minute")


class PureFunctionTests(unittest.TestCase):
    def test_resolve_area(self):
        self.assertEqual(ev.resolve_area(1, "br"), ("br", "br"))
        self.assertEqual(ev.resolve_area(1, "rs"), ("uf", "rs"))
        self.assertEqual(ev.resolve_area(1, "zz"), ("uf", "zz"))
        self.assertEqual(ev.resolve_area(3, "br"), ("uf", "rs"))
        self.assertEqual(ev.resolve_area(6, "br", "sp"), ("uf", "sp"))
        self.assertEqual(ev.resolve_area(6, "SP"), ("uf", "sp"))

    def test_counted_drops_zero_snapshots(self):
        h = hist_df([(1, 0, 0.0, "a", 0), (1, 0, 0.0, "b", 0), (2, 5, 1.0, "a", 10), (2, 5, 1.0, "b", 5)])
        self.assertEqual(set(ev.counted(h)["snapshot_id"]), {2})
        self.assertTrue(ev.ranked(h.iloc[:0]).empty)
        self.assertTrue(ev.top_candidates(h.iloc[:2], 3).empty)
        self.assertTrue(ev.lead_changes(h.iloc[:2]).empty)

    def test_top_candidates_uses_latest_snapshot(self):
        h = hist_df([(1, 0, 10, "a", 50), (1, 0, 10, "b", 30), (1, 0, 10, "c", 20),
                     (2, 5, 20, "a", 50), (2, 5, 20, "b", 60), (2, 5, 20, "c", 70)])
        self.assertEqual(list(ev.top_candidates(h, 2)["sqcand"]), ["c", "b"])
        self.assertEqual(len(ev.top_candidates(h, 10)), 3)

    def test_lead_changes_simple(self):
        h = hist_df([(1, 0, 10, "a", 60), (1, 0, 10, "b", 40),
                     (2, 5, 30, "a", 55), (2, 5, 30, "b", 45),
                     (3, 9, 67, "a", 45), (3, 9, 67, "b", 55),
                     (4, 12, 90, "a", 40), (4, 12, 90, "b", 60)])
        ch = ev.lead_changes(h)
        self.assertEqual(len(ch), 1)
        row = ch.iloc[0]
        self.assertEqual((row["sqcand"], row["sqcand_ultrapassado"]), ("b", "a"))
        self.assertEqual(row["pct_secoes"], 67)
        self.assertAlmostEqual(row["margem_pp"], 10.0)
        self.assertEqual(row["tse_ts"], pd.Timestamp("2026-10-04 17:09"))

    def test_lead_changes_seats_two(self):
        # a sempre 1º; c passa b na disputa pela 2ª vaga; troca a<->c no topo não é virada de vaga
        h = hist_df([(1, 0, 10, "a", 50), (1, 0, 10, "b", 30), (1, 0, 10, "c", 20),
                     (2, 5, 50, "a", 50), (2, 5, 50, "b", 20), (2, 5, 50, "c", 30),
                     (3, 9, 80, "a", 30), (3, 9, 80, "b", 20), (3, 9, 80, "c", 50)])
        ch = ev.lead_changes(h, seats=2)
        self.assertEqual(len(ch), 1)
        self.assertEqual((ch.iloc[0]["sqcand"], ch.iloc[0]["sqcand_ultrapassado"]), ("c", "b"))
        self.assertEqual(len(ev.lead_changes(h, seats=1)), 1)  # c assume a liderança no 3º boletim

    def test_margin_series_sign(self):
        h = hist_df([(1, 0, 10, "a", 60), (1, 0, 10, "b", 40), (2, 5, 60, "a", 40), (2, 5, 60, "b", 60)])
        m = ev.margin_series(h, "a", "b")
        self.assertEqual(list(m["margem_pp"].round(6)), [20.0, -20.0])
        self.assertTrue(ev.margin_series(h.iloc[:0], "a", "b").empty)

    def test_line_styles_vary_dash_within_party(self):
        top = pd.DataFrame({"sqcand": ["1", "2", "3", "4"], "partido": ["PL", "PT", "PL", "PL"]})
        st = ev.line_styles(top)
        self.assertEqual(st["1"][0], st["3"][0])
        self.assertEqual(len({st["1"][1], st["3"][1], st["4"][1]}), 3)
        self.assertEqual(st["2"][1], "solid")

    def test_pace(self):
        t = pd.DataFrame({"tse_ts": pd.to_datetime(["2026-10-04 17:10", "2026-10-04 17:00"]),
                          "pct_secoes": [10.0, 0.0], "comparecimento": [80, 0], "eleitorado_apurado": [100, 0]})
        p = ev.pace(t)
        self.assertEqual(list(p["pct_secoes"]), [0.0, 10.0])
        self.assertTrue(pd.isna(p["comparecimento_pct"].iloc[0]))
        self.assertAlmostEqual(p["comparecimento_pct"].iloc[1], 80.0)
        self.assertEqual(len(ev.counted_totals(t.sort_values("tse_ts"))), 1)


class StoreIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_virada_detected_from_store(self):
        write_night(self.conn, "br-c0001-e006257-u.json", BR_VIRADA)
        hist = store.history(self.conn, "6257", 1, "br", "br")
        self.assertEqual(hist["snapshot_id"].nunique(), 7)  # inclui o zerado
        ch = ev.lead_changes(hist)
        self.assertEqual(len(ch), 1, ch)
        row = ch.iloc[0]
        self.assertEqual(row["nome_urna"], "RENAN SANTOS")
        self.assertEqual(row["nome_ultrapassado"], "EDMILSON COSTA")
        self.assertAlmostEqual(row["pct_secoes"], 67.0)
        self.assertGreater(row["margem_pp"], 0)
        top = ev.top_candidates(hist, 5)
        self.assertEqual(top.iloc[0]["nome_urna"], "RENAN SANTOS")
        self.assertEqual(len(top), 5)
        m = ev.margin_series(hist, top.iloc[0]["sqcand"], top.iloc[1]["sqcand"])
        self.assertEqual(len(m), 6)
        self.assertLess(m["margem_pp"].iloc[0], 0)
        self.assertGreater(m["margem_pp"].iloc[-1], 0)

    def test_senado_two_seats(self):
        # seed=1: PIMENTA (131) e MANUELA (500) nas vagas; SANDERSON (222) entra na 2ª vaga no fim
        write_night(self.conn, "rs-c0005-e006259-u.json",
                    [(0, 10, None), (10, 50, {"222": 1.2}), (20, 80, {"222": 2.5})])
        hist = store.history(self.conn, "6259", 5, "uf", "rs")
        ch = ev.lead_changes(hist, seats=2)
        self.assertEqual(len(ch), 1, ch)
        self.assertEqual(ch.iloc[0]["nome_urna"], "SANDERSON")

    def test_deputados_large_history(self):
        write_night(self.conn, "rs-c0006-e006259-u.json", [(m, 10 * (m // 10 + 1), None) for m in range(0, 60, 10)])
        hist = store.history(self.conn, "6259", 6, "uf", "rs")
        top = ev.top_candidates(hist, 10)
        self.assertEqual(len(top), 10)
        self.assertTrue(ev.lead_changes(hist).empty)  # mesmo seed e sem viés: ninguém passa ninguém
        totals = store.history_totals(self.conn, "6259", 6, "uf", "rs")
        p = ev.pace(ev.counted_totals(totals))
        self.assertEqual(len(p), 6)
        self.assertTrue(p["comparecimento_pct"].between(70, 90).all())

    def test_sim_db_if_present(self):
        sim = config.SIM_DB_PATH
        if not Path(sim).exists():
            self.skipTest("data/sim.db ainda não existe")
        conn = store.connect(sim, init=False)
        try:
            hist = store.history(conn, "6257", 1, "br", "br")
            if hist.empty:
                self.skipTest("sim.db sem histórico br de Presidente")
            ch = ev.lead_changes(hist)
            self.assertIsInstance(ch, pd.DataFrame)
            self.assertFalse(ev.top_candidates(hist, 5).empty)
        finally:
            conn.close()


class AppSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "evo.db"
        conn = store.connect(cls.db)
        write_night(conn, "br-c0001-e006257-u.json", BR_VIRADA)
        write_night(conn, "rs-c0001-e006257-u.json", BR_VIRADA)
        write_night(conn, "rs-c0003-e006259-u.json", [(0, 10, {"15": 2.0}), (15, 40, None), (30, 70, None)])
        write_night(conn, "rs-c0005-e006259-u.json", [(0, 10, None), (10, 50, {"222": 1.2}), (20, 80, {"222": 2.5})])
        write_night(conn, "rs-c0006-e006259-u.json", [(0, 10, None), (10, 30, None), (20, 60, None)])
        conn.close()
        cls.empty_db = Path(cls.tmp.name) / "empty.db"
        store.connect(cls.empty_db).close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_app(self, db, uf, cargo, turno=1, mun=None, extra=None):
        from streamlit.testing.v1 import AppTest
        old_env, old_path = os.environ.get("ELEICOES_DB"), config.DB_PATH
        os.environ["ELEICOES_DB"] = str(db)
        config.DB_PATH = Path(db)
        try:
            at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
            at.session_state["view"] = "Evolução"
            at.session_state["uf"] = uf
            at.session_state["cargo"] = cargo
            at.session_state["turno"] = turno
            if mun:
                at.session_state["mun"] = mun
            for k, v in (extra or {}).items():
                at.session_state[k] = v
            at.run()
        finally:
            config.DB_PATH = old_path
            if old_env is None:
                os.environ.pop("ELEICOES_DB", None)
            else:
                os.environ["ELEICOES_DB"] = old_env
        self.assertFalse(at.exception, [e.value for e in at.exception])
        infos = [i.value for i in at.info]
        self.assertNotIn(ev.ERROR_MSG, infos)
        return at

    def test_br_presidente(self):
        at = self.run_app(self.db, "br", 1)
        self.assertTrue(any("Evolução" in s.value for s in at.subheader))
        self.assertEqual(len(at.get("plotly_chart")), 3)
        self.assertEqual(at.metric[3].value, "1")  # uma virada

    def test_time_axis_and_turnout(self):
        at = self.run_app(self.db, "br", 1, extra={"evo_x": ev.X_TIME, "evo_comp": True, "evo_n_maj": 3})
        self.assertEqual(at.radio(key="evo_x").value, ev.X_TIME)
        self.assertEqual(len(at.get("plotly_chart")), 3)

    def test_rs_presidente(self):
        at = self.run_app(self.db, "rs", 1)
        self.assertEqual(len(at.get("plotly_chart")), 3)

    def test_br_governador_uses_uf_selector(self):
        at = self.run_app(self.db, "br", 3)
        self.assertEqual(at.selectbox(key="evo_uf").value, "rs")
        self.assertEqual(len(at.get("plotly_chart")), 3)

    def test_rs_senado(self):
        at = self.run_app(self.db, "rs", 5)
        self.assertEqual(len(at.get("plotly_chart")), 3)

    def test_rs_deputado_federal(self):
        at = self.run_app(self.db, "rs", 6)
        self.assertEqual(at.number_input(key="evo_n_prop").value, 10)
        self.assertEqual(len(at.get("plotly_chart")), 3)

    def test_municipio_falls_back_to_uf(self):
        at = self.run_app(self.db, "rs", 1, mun="88013")
        self.assertTrue(any("Municípios não têm histórico" in c.value for c in at.caption))

    def test_empty_db(self):
        at = self.run_app(self.empty_db, "br", 1)
        self.assertTrue(at.info)
        self.assertEqual(len(at.get("plotly_chart")), 0)


if __name__ == "__main__":
    unittest.main()
