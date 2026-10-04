import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

from eleicoes import fake, geo, parse, store
from eleicoes.views import candidato as cv

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]
WHEN = datetime(2026, 10, 4, 17, 30)


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def write(conn, name, pct=50, uf=None, cdabr=None, seed=1):
    doc = load(name)
    if cdabr:
        doc["cdabr"] = cdabr
    store.write_snapshot(conn, parse.parse_u(fake.fill_votes(doc, pct, seed=seed, when=WHEN), uf=uf))


def build_db(path: Path, n_mun: int = 25) -> None:
    conn = store.connect(path)
    for name in ["br-c0001-e006257-u.json", "rs-c0001-e006257-u.json", "sp-c0001-e006257-u.json",
                 "zz-c0001-e006257-u.json", "rs-c0003-e006259-u.json", "rs-c0005-e006259-u.json",
                 "rs-c0006-e006259-u.json", "rs-c0007-e006259-u.json"]:
        write(conn, name)
    muns = geo.municipios_uf("rs")
    codes = ["88013"] + [m for m in muns["mun"] if m != "88013"][: n_mun - 1]
    for m in codes:
        write(conn, "rs88013-c0001-e006257-u.json", 60, uf="rs", cdabr=m)
        write(conn, "rs88013-c0006-e006259-u.json", 60, uf="rs", cdabr=m)
    conn.close()


def cands_frame():
    # 2 áreas x 3 candidatos, no formato de store.latest_candidates
    rows = [
        ("a", "1", "X", 600, 60.0, 1), ("a", "2", "Y", 300, 30.0, 2), ("a", "3", "Z", 100, 10.0, 3),
        ("b", "2", "Y", 50, 50.0, 1), ("b", "1", "X", 40, 40.0, 2), ("b", "3", "Z", 10, 10.0, 3),
    ]
    df = pd.DataFrame(rows, columns=["mun", "sqcand", "nome_urna", "votos", "pct_validos", "rank"])
    df["numero"] = df["sqcand"]
    df["partido"] = "P"
    df["pct_secoes"] = 50.0
    return df


class PureTests(unittest.TestCase):
    def test_photo_url(self):
        self.assertEqual(cv.photo_url("6257", 1, "rs", "280002542548"),
                         "https://resultados.tse.jus.br/oficial/ele2026/6257/fotos/br/280002542548.jpeg")
        self.assertTrue(cv.photo_url("6259", 3, "RS", "1").endswith("/6259/fotos/rs/1.jpeg"))

    def test_options_and_resolve(self):
        df = cands_frame()
        opts, labels = cv.candidate_options(df[df["mun"] == "a"])
        self.assertEqual(opts, ["1", "2", "3"])
        self.assertEqual(labels["1"], "X (1 · P)")
        self.assertEqual(cv.resolve_choice("2", opts), "2")
        self.assertEqual(cv.resolve_choice("99", opts), "1")
        self.assertIsNone(cv.resolve_choice("2", []))
        self.assertEqual(cv.candidate_options(df.head(0)), ([], {}))

    def test_search_options_keeps_current(self):
        res = pd.DataFrame({"sqcand": ["5", "6"]})
        self.assertEqual(cv.search_options(res, "9", ["5", "6", "9"]), ["9", "5", "6"])
        self.assertEqual(cv.search_options(res, "9", ["5", "6"]), ["5", "6"])   # de outra área: some
        self.assertEqual(cv.search_options(res.head(0), None, []), [])

    def test_strength_table(self):
        names = pd.DataFrame({"mun": ["a", "b"], "nome": ["Capital", "Vila"], "capital": [True, False]})
        validos = pd.DataFrame({"mun": ["a", "b"], "validos": [1000, 0]})  # 0 -> cai na soma
        t = cv.strength_table(cands_frame(), "1", "mun", validos, names)
        self.assertEqual(list(t["loc"]), ["a", "b"])
        self.assertEqual(list(t["validos"]), [1000, 100])
        self.assertEqual(list(t["rank"]), [1, 2])
        self.assertEqual(list(t["n_cands"]), [3, 3])
        self.assertEqual(list(t["nome"]), ["Capital", "Vila"])
        self.assertEqual(list(t["capital"]), [True, False])
        self.assertTrue(cv.strength_table(cands_frame(), "nope", "mun").empty)
        self.assertTrue(cv.strength_table(cands_frame().head(0), "1", "mun").empty)
        h = cv.hover_text(t)
        self.assertIn("60,00% dos válidos", h.iloc[0])
        self.assertIn("600 votos · 1º de 3", h.iloc[0])

    def test_strength_table_uf_names(self):
        df = cands_frame().rename(columns={"mun": "uf"}).replace({"a": "rs", "b": "sp"})
        t = cv.strength_table(df, "2", "uf")
        self.assertEqual(set(t["nome"]), {"Rio Grande do Sul", "São Paulo"})

    def test_concentration_and_compare(self):
        tab = pd.DataFrame({"loc": list("abcdefg"), "nome": list("abcdefg"),
                            "capital": [True] + [False] * 6, "votos": [500, 200, 100, 80, 60, 40, 20],
                            "validos": [1000, 1000, 1000, 1000, 1000, 1000, 1000],
                            "pct_validos": [50, 20, 10, 8, 6, 4, 2], "rank": [1] * 7, "n_cands": [3] * 7,
                            "pct_secoes": [50.0] * 7})
        c = cv.concentration(tab)
        self.assertEqual(c["total"], 1000)
        self.assertAlmostEqual(c["capital_pct"], 50.0)
        self.assertAlmostEqual(c["top5_pct"], 94.0)
        self.assertEqual(c["n_half"], 1)
        self.assertEqual(cv.concentration(tab.assign(votos=0)), {})
        rows = cv.compare_rows(tab, [("RS", 15.0), ("Brasil", None)])
        self.assertEqual(list(rows["rotulo"]), ["Capital", "Interior", "RS"])
        self.assertAlmostEqual(rows["pct"].iloc[0], 50.0)
        self.assertAlmostEqual(rows["pct"].iloc[1], 500 / 6000 * 100)
        self.assertEqual(list(cv.compare_rows(None, [("Brasil", 3.0)])["rotulo"]), ["Brasil"])

    def test_top_areas_min_validos(self):
        tab = pd.DataFrame({"loc": ["a", "b", "c"], "votos": [10, 900, 0], "pct_validos": [90.0, 45.0, 0.0],
                            "validos": [11, 2000, 10]})
        self.assertEqual(list(cv.top_areas(tab, "votos")["loc"]), ["b", "a"])
        self.assertEqual(list(cv.top_areas(tab, "pct_validos", min_validos=500)["loc"]), ["b"])

    def test_party_line(self):
        self.assertEqual(cv.party_line({"partido": "PL", "federacao": "", "agremiacao": "PL"}), "PL")
        s = cv.party_line({"partido": "PT", "federacao": "PT/PC do B/PV", "agremiacao": "PSB / PT"})
        self.assertEqual(s, "PT · Federação PT/PC do B/PV · Coligação PSB / PT")

    def test_mun_strength_from_db(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.db"
            build_db(p, n_mun=6)
            conn = store.connect(p)
            br = store.latest_candidates(conn, "6257", 1, "br")
            sq = str(br.iloc[0]["sqcand"])
            tab = cv._mun_strength_live(conn, "6257", 1, "rs", sq)
            conn.close()
            self.assertEqual(len(tab), 6)
            self.assertTrue(tab.loc[tab["loc"] == "88013", "capital"].iloc[0])
            self.assertTrue((tab["validos"] >= tab["votos"]).all())
            fig = cv.strength_map(tab, geo.municipios_geojson("rs"), geo.municipios_uf("rs")["mun"].tolist())
            self.assertEqual(len(fig.data), 2)   # fundo cinza + valores


def _picker_app():
    import streamlit as st
    from eleicoes.views import candidato as cv
    opts = st.session_state.get("opts", ["a", "b", "c"])
    st.session_state["chosen"] = cv._picker("Candidato", opts, {o: o.upper() for o in opts}, "wk")


class PickerTests(unittest.TestCase):
    def test_choice_persists_and_resets(self):
        from streamlit.testing.v1 import AppTest
        at = AppTest.from_function(_picker_app)
        at.run()
        self.assertEqual(at.session_state["chosen"], "a")
        at.selectbox(key="wk").select("c").run()
        self.assertEqual(at.session_state[cv.K_CAND], "c")
        at.run()
        self.assertEqual(at.session_state["chosen"], "c")
        at.session_state["opts"] = ["x", "c", "y"]   # outra área, candidato ainda existe
        at.run()
        self.assertEqual(at.session_state["chosen"], "c")
        at.session_state["opts"] = ["x", "y"]        # não existe mais -> mais votado
        at.run()
        self.assertEqual(at.session_state["chosen"], "x")
        at.session_state[cv.K_CAND] = "y"            # escolhido por outra tela
        at.run()
        self.assertEqual(at.selectbox(key="wk").value, "y")
        self.assertFalse(at.exception)


class AppSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "full.db"
        build_db(cls.db, n_mun=25)
        cls.empty = Path(cls.tmp.name) / "empty.db"
        store.connect(cls.empty).close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_app(self, db, uf, cargo, busca=None, cand=None):
        from streamlit.testing.v1 import AppTest
        os.environ["ELEICOES_DB"] = str(db)
        import importlib
        from eleicoes import config
        importlib.reload(config)
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
        at.session_state["view"] = "Candidato"
        at.session_state["uf"] = uf
        at.session_state["cargo"] = cargo
        at.session_state["turno"] = 1
        if busca is not None:
            at.session_state[cv.K_BUSCA] = busca
        if cand is not None:
            at.session_state[cv.K_CAND] = cand
        at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        return at

    def texts(self, at):
        return " ".join([m.value for m in at.markdown] + [i.value for i in at.info] + [c.value for c in at.caption])

    def test_br_presidente(self):
        at = self.run_app(self.db, "br", 1)
        self.assertNotIn("Não foi possível", self.texts(at))
        self.assertIn("por UF", self.texts(at))
        self.assertTrue(at.session_state[cv.K_CAND])

    def test_rs_presidente_municipios(self):
        at = self.run_app(self.db, "rs", 1)
        t = self.texts(at)
        self.assertNotIn("Não foi possível", t)
        self.assertIn("por município", t)
        self.assertIn("25 de", t)

    def test_rs_governador_sem_municipios(self):
        at = self.run_app(self.db, "rs", 3, cand="280002542548")  # sqcand de outro cargo -> reseta
        t = self.texts(at)
        self.assertNotIn("Não foi possível", t)
        self.assertIn("coletor varre os municípios", t)
        self.assertNotEqual(at.session_state[cv.K_CAND], "280002542548")

    def test_rs_depfed_busca(self):
        conn = store.connect(self.db)
        dep = store.latest_candidates(conn, "6259", 6, "uf", uf="rs").iloc[3]
        conn.close()
        at = self.run_app(self.db, "rs", 6, busca=str(dep["numero"]))
        self.assertNotIn("Não foi possível", self.texts(at))
        self.assertEqual(at.session_state[cv.K_CAND], str(dep["sqcand"]))
        self.assertEqual(at.text_input(key=cv.K_BUSCA).value, str(dep["numero"]))
        # (um segundo at.run() aqui esbarra num limite do AppTest com o segmented_control 'view' do app.py)

    def test_br_senador_pede_uf(self):
        at = self.run_app(self.db, "br", 5)
        self.assertNotIn("Não foi possível", self.texts(at))
        self.assertEqual(at.selectbox(key=cv.K_UF_PICK).value, "rs")

    def test_br_depfed_sem_busca(self):
        at = self.run_app(self.db, "br", 6)
        self.assertNotIn("Não foi possível", self.texts(at))

    def test_exterior(self):
        at = self.run_app(self.db, "zz", 1)
        self.assertNotIn("Não foi possível", self.texts(at))

    def test_empty_db(self):
        at = self.run_app(self.empty, "br", 1)
        self.assertIn("17h", self.texts(at))
        at = self.run_app(self.empty, "rs", 6, busca="silva")
        self.assertIn("17h", self.texts(at))


if __name__ == "__main__":
    unittest.main()
