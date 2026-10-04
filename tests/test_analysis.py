import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

from eleicoes import analysis, config, fake, parse, store

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def totals(rows):
    return pd.DataFrame(rows, columns=["uf", "mun", "eleitorado", "eleitorado_apurado", "validos", "pct_secoes"])


def cands(rows):
    return pd.DataFrame(rows, columns=["uf", "mun", "sqcand", "nome_urna", "partido", "votos"])


# Área a: pouco apurada; área b: metade apurada. Taxa de válidos = 0,8 nas duas.
T2 = totals([("a", "", 1000, 200, 160, 20.0), ("b", "", 2000, 1000, 800, 50.0)])
C2 = cands([("a", "", "X", "Xis", "PT", 120), ("a", "", "Y", "Ípsilon", "PL", 40),
            ("b", "", "X", "Xis", "PT", 200), ("b", "", "Y", "Ípsilon", "PL", 600)])


class RemainingTests(unittest.TestCase):
    def test_known_numbers(self):
        r = analysis.remaining_by_area(T2).set_index("uf")
        self.assertEqual(r.loc["a", "faltam_eleitores"], 800)
        self.assertEqual(r.loc["b", "faltam_eleitores"], 1000)
        self.assertAlmostEqual(r.loc["a", "taxa_validos"], 0.8)
        self.assertEqual(r.loc["a", "validos_restantes_est"], 640)
        self.assertEqual(r.loc["b", "validos_restantes_est"], 800)

    def test_fallback_rate_when_area_not_counted(self):
        t = totals([("a", "", 1000, 500, 400, 50.0), ("b", "", 1000, 0, 0, 0.0)])
        r = analysis.remaining_by_area(t).set_index("uf")
        self.assertAlmostEqual(r.loc["b", "taxa_validos"], 0.8)
        self.assertEqual(r.loc["b", "validos_restantes_est"], 800)

    def test_nothing_counted_uses_default(self):
        r = analysis.remaining_by_area(totals([("a", "", 1000, 0, 0, 0.0)]))
        self.assertAlmostEqual(r.iloc[0]["taxa_validos"], analysis.DEFAULT_TAXA_VALIDOS)

    def test_empty(self):
        self.assertTrue(analysis.remaining_by_area(pd.DataFrame()).empty)
        self.assertTrue(analysis.project_final(pd.DataFrame(), pd.DataFrame()).empty)
        self.assertTrue(analysis.decision_check(pd.DataFrame(), pd.DataFrame()).empty)


class ProjectionTests(unittest.TestCase):
    def test_two_areas(self):
        p = analysis.project_final(C2, T2).set_index("sqcand")
        # a: X 120 + 0,75×640 = 600; Y 40 + 0,25×640 = 200.  b: X 200 + 0,25×800 = 400; Y 600 + 600 = 1200
        self.assertEqual(p.loc["X", "votos_projetados"], 1000)
        self.assertEqual(p.loc["Y", "votos_projetados"], 1400)
        self.assertEqual(p.loc["X", "votos_atuais"], 320)
        self.assertAlmostEqual(p.loc["X", "pct_atual"], 100 / 3)
        self.assertAlmostEqual(p.loc["X", "pct_projetado"], 100 * 1000 / 2400)
        self.assertAlmostEqual(p.loc["X", "delta_pp"], 100 * 1000 / 2400 - 100 / 3)
        out = analysis.project_final(C2, T2)
        self.assertEqual(list(out["sqcand"]), ["Y", "X"])  # ordenado por projeção
        self.assertEqual(list(out.columns), analysis.PROJECTION_COLS)

    def test_area_without_votes_uses_overall_share(self):
        t = totals([("a", "", 1000, 500, 400, 50.0), ("b", "", 1000, 0, 0, 0.0)])
        c = cands([("a", "", "X", "Xis", "PT", 300), ("a", "", "Y", "Ípsilon", "PL", 100)])
        p = analysis.project_final(c, t).set_index("sqcand")
        self.assertEqual(p.loc["X", "votos_projetados"], 300 + 300 + 600)  # a: 0,75×400; b: 0,75×800
        self.assertEqual(p.loc["Y", "votos_projetados"], 100 + 100 + 200)
        self.assertAlmostEqual(p.loc["X", "pct_projetado"], 75.0)

    def test_fully_counted_projection_equals_current(self):
        t = totals([("a", "", 1000, 1000, 800, 100.0)])
        c = cands([("a", "", "X", "Xis", "PT", 500), ("a", "", "Y", "Ípsilon", "PL", 300)])
        p = analysis.project_final(c, t)
        self.assertEqual(list(p["votos_projetados"]), list(p["votos_atuais"]))


class DecisionTests(unittest.TestCase):
    def test_status_levels(self):
        t = totals([("open", "", 2000, 1000, 800, 50.0),      # margem 400 < 800 restantes
                    ("math", "", 1000, 900, 720, 90.0),       # margem 480 > 100 eleitores
                    ("prat", "", 1000, 600, 280, 60.0),       # margem 200 > 187 válidos (< 400 eleitores)
                    ("none", "", 1000, 0, 0, 0.0)])
        c = cands([("open", "", "X", "Xis", "PT", 200), ("open", "", "Y", "Ípsilon", "PL", 600),
                   ("math", "", "X", "Xis", "PT", 600), ("math", "", "Y", "Ípsilon", "PL", 120),
                   ("prat", "", "X", "Xis", "PT", 240), ("prat", "", "Y", "Ípsilon", "PL", 40),
                   ("none", "", "X", "Xis", "PT", 0), ("none", "", "Y", "Ípsilon", "PL", 0)])
        d = analysis.decision_check(c, t, check_majority=True).set_index("uf")
        self.assertEqual(d.loc["open", "status"], analysis.ST_ABERTO)
        self.assertEqual(d.loc["open", "lider_nome"], "Ípsilon")
        self.assertEqual(d.loc["open", "margem_votos"], 400)
        self.assertTrue(d.loc["open", "alcancavel"])
        self.assertEqual(d.loc["math", "status"], analysis.ST_DECIDIDO)
        self.assertEqual(d.loc["prat", "status"], analysis.ST_PRATICA)
        self.assertEqual(d.loc["none", "status"], analysis.ST_SEM_VOTOS)
        self.assertIsNone(d.loc["none", "lider_nome"])
        self.assertEqual(d.loc["math", "maioria"], "Vence no 1º turno (na prática)")
        self.assertEqual(d.loc["open", "maioria"], analysis.ST_ABERTO)

    def test_two_seats_compares_second_and_third(self):
        c = cands([("rs", "", "A", "A", "PT", 500), ("rs", "", "B", "B", "PL", 300),
                   ("rs", "", "C", "C", "PSD", 100)])
        lead = analysis.area_leaders(c, vagas=2).iloc[0]
        self.assertEqual((lead["lider_nome"], lead["corte_nome"], lead["desafiante_nome"]), ("A", "B", "C"))
        self.assertEqual(lead["margem_votos"], 200)
        self.assertEqual(lead["segundo_nome"], "B")


class ResidualTests(unittest.TestCase):
    def test_residual_area(self):
        parent_t = pd.DataFrame([dict(uf="rs", mun="", eleitorado=3000, eleitorado_apurado=1200, validos=960,
                                      secoes_total=30, secoes_totalizadas=12, pct_secoes=40.0)])
        parent_c = cands([("rs", "", "X", "Xis", "PT", 520), ("rs", "", "Y", "Ípsilon", "PL", 440)])
        sub_t = pd.DataFrame([dict(uf="rs", mun="1", eleitorado=1000, eleitorado_apurado=200, validos=160,
                                   secoes_total=10, secoes_totalizadas=2, pct_secoes=20.0)])
        sub_c = cands([("rs", "1", "X", "Xis", "PT", 120), ("rs", "1", "Y", "Ípsilon", "PL", 40)])
        t, c = analysis.add_residual_area(sub_t, sub_c, parent_t, parent_c)
        self.assertEqual(len(t), 2)
        res = t[t["mun"] == analysis.RESTO].iloc[0]
        self.assertEqual((res["eleitorado"], res["eleitorado_apurado"], res["validos"]), (2000, 1000, 800))
        self.assertAlmostEqual(res["pct_secoes"], 50.0)
        rc = c[c["mun"] == analysis.RESTO].set_index("sqcand")["votos"]
        self.assertEqual((rc["X"], rc["Y"]), (400, 400))
        # Totais somados batem com os do pai
        self.assertEqual(t["eleitorado"].sum(), 3000)
        self.assertEqual(analysis.project_final(c, t)["votos_atuais"].sum(), 960)

    def test_full_coverage_no_residual(self):
        parent_t = pd.DataFrame([dict(uf="rs", mun="", eleitorado=1000, eleitorado_apurado=200, validos=160)])
        sub_t = pd.DataFrame([dict(uf="rs", mun="1", eleitorado=1000, eleitorado_apurado=200, validos=160)])
        t, _ = analysis.add_residual_area(sub_t, cands([]), parent_t, cands([]))
        self.assertEqual(len(t), 1)


class StoreIntegrationTests(unittest.TestCase):
    """Projeção com dados do fixture real (Presidente br/rs/sp/zz + POA) gravados no banco."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "t.db"
        cls.conn = store.connect(cls.db)
        fill_db(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def test_president_by_uf(self):
        tot = store.latest_totals(self.conn, "6257", 1, "uf")
        cs = store.latest_candidates(self.conn, "6257", 1, "uf")
        rem = analysis.remaining_by_area(tot)
        self.assertEqual(set(rem["uf"]), {"rs", "sp", "zz"})
        self.assertEqual(rem["faltam_eleitores"].sum(), (tot["eleitorado"] - tot["eleitorado_apurado"]).sum())
        p = analysis.project_final(cs, tot)
        self.assertAlmostEqual(p["pct_projetado"].sum(), 100.0, places=6)
        self.assertTrue((p["votos_projetados"] >= p["votos_atuais"]).all())
        expected = cs["votos"].sum() + rem["validos_restantes_est"].sum()
        self.assertAlmostEqual(p["votos_projetados"].sum(), expected, delta=len(p) * 3)
        self.assertEqual(p.iloc[0]["votos_projetados"], p["votos_projetados"].max())

    def test_state_with_partial_municipalities(self):
        tot_uf = store.latest_totals(self.conn, "6257", 1, "uf", uf="rs")
        cs_uf = store.latest_candidates(self.conn, "6257", 1, "uf", uf="rs")
        tot_mu = store.latest_totals(self.conn, "6257", 1, "mu", uf="rs")
        cs_mu = store.latest_candidates(self.conn, "6257", 1, "mu", uf="rs")
        self.assertEqual(len(tot_mu), 1)
        t, c = analysis.add_residual_area(tot_mu, cs_mu, tot_uf, cs_uf)
        self.assertEqual(len(t), 2)
        self.assertEqual(t["eleitorado"].sum(), tot_uf["eleitorado"].sum())
        p = analysis.project_final(c, t)
        self.assertAlmostEqual(p["pct_projetado"].sum(), 100.0, places=6)
        chk = analysis.decision_check(cs_uf, tot_uf, 1, check_majority=True)
        self.assertEqual(len(chk), 1)
        self.assertIn(chk.iloc[0]["status"], {analysis.ST_ABERTO, analysis.ST_PRATICA, analysis.ST_DECIDIDO})

    def test_governor_and_senate(self):
        for cargo, vagas in ((3, 1), (5, 2)):
            tot = store.latest_totals(self.conn, "6259", cargo, "uf")
            cs = store.latest_candidates(self.conn, "6259", cargo, "uf")
            self.assertEqual(int(tot["vagas"].max()), vagas)
            chk = analysis.decision_check(cs, tot, vagas)
            self.assertFalse(chk.empty)
            self.assertTrue(chk["status"].notna().all())


def fill_db(conn):
    def w(name, pct, minute, uf=None, seed=1):
        doc = fake.fill_votes(load(name), pct, seed=seed, when=datetime(2026, 10, 4, 17, minute))
        store.write_snapshot(conn, parse.parse_u(doc, uf=uf))

    w("br-c0001-e006257-u.json", 40, 30)
    w("rs-c0001-e006257-u.json", 35, 30)
    w("sp-c0001-e006257-u.json", 55, 30, seed=2)
    w("zz-c0001-e006257-u.json", 10, 30)
    w("rs88013-c0001-e006257-u.json", 60, 30, uf="rs")
    w("rs-c0003-e006259-u.json", 35, 30)
    w("sp-c0003-e006259-u.json", 80, 30)
    w("rs-c0005-e006259-u.json", 35, 30)
    w("rs-c0006-e006259-u.json", 35, 30)
    w("rs88013-c0006-e006259-u.json", 50, 30, uf="rs")


class AppSmokeTests(unittest.TestCase):
    """A tela roda no app inteiro (AppTest) sem exceção nem aviso de erro."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "smoke.db"
        conn = store.connect(cls.db)
        fill_db(conn)
        conn.close()
        cls.empty_db = Path(cls.tmp.name) / "empty.db"
        store.connect(cls.empty_db).close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _run(self, db, uf, cargo, turno=1):
        from streamlit.testing.v1 import AppTest
        old_path, old_env = config.DB_PATH, os.environ.get("ELEICOES_DB")
        config.DB_PATH = db
        os.environ["ELEICOES_DB"] = str(db)
        try:
            at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=60)
            at.session_state["view"] = "Votos restantes"
            at.session_state["uf"] = uf
            at.session_state["cargo"] = cargo
            at.session_state["turno"] = turno
            at.run()
        finally:
            config.DB_PATH = old_path
            if old_env is None:
                os.environ.pop("ELEICOES_DB", None)
            else:
                os.environ["ELEICOES_DB"] = old_env
        self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])
        warnings = [w.value for w in at.warning if "votos restantes" in str(w.value)]
        self.assertEqual(warnings, [])
        return at

    def test_views(self):
        for uf, cargo in (("br", 1), ("rs", 1), ("br", 3), ("br", 5), ("rs", 3), ("rs", 5), ("rs", 6),
                          ("br", 6), ("sp", 1), ("zz", 1)):
            with self.subTest(uf=uf, cargo=cargo):
                at = self._run(self.db, uf, cargo)
                texts = " ".join(str(m.value) for m in at.caption)
                self.assertIn("Estimativa ingênua", texts)

    def test_empty_db(self):
        for uf, cargo in (("br", 1), ("rs", 1), ("br", 3), ("rs", 6)):
            with self.subTest(uf=uf, cargo=cargo):
                self._run(self.empty_db, uf, cargo)


if __name__ == "__main__":
    unittest.main()
