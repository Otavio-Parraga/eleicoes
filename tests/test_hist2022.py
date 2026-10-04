"""Testes de eleicoes.hist2022 (sem download: quadros montados à mão) e smoke test da tela 2022 × 2026."""
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import pandas as pd

from eleicoes import config, fake, hist2022, parse, store

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def raw_votacao(rows):
    cols = ["CD_TIPO_ELEICAO", "NR_TURNO", "SG_UF", "CD_MUNICIPIO", "NM_MUNICIPIO", "CD_CARGO", "NR_CANDIDATO",
            "NM_URNA_CANDIDATO", "SG_PARTIDO", "QT_VOTOS_NOMINAIS_VALIDOS"]
    return pd.DataFrame(rows, columns=cols, dtype=str)


class ShapingTests(unittest.TestCase):
    def test_pad_mun(self):
        s = pd.Series(["5231", "88013", "117", "1", None, "", "1007.0"])
        self.assertEqual(hist2022.pad_mun(s).tolist(), ["05231", "88013", "00117", "00001", "", "", "01007"])

    def test_shape_votacao_sums_zones_and_filters(self):
        raw = raw_votacao([
            ["2", "1", "RS", "88013", "PORTO ALEGRE", "1", "13", "LULA", "PT", "100"],
            ["2", "1", "RS", "88013", "PORTO ALEGRE", "1", "13", "LULA", "PT", "50"],    # outra zona
            ["2", "1", "RS", "88013", "PORTO ALEGRE", "1", "22", "JAIR BOLSONARO", "PL", "150"],
            ["2", "1", "PA", "5231", "SALINÓPOLIS", "1", "13", "LULA", "PT", "7"],
            ["2", "1", "RS", "88013", "PORTO ALEGRE", "3", "13", "EDEGAR", "PT", "999"],  # governador
            ["1", "1", "RS", "88013", "PORTO ALEGRE", "1", "13", "LULA", "PT", "999"],    # suplementar
        ])
        df = hist2022.shape_votacao(raw, cargo=1)
        self.assertEqual(len(df), 3)
        pa = df[df["uf"] == "pa"].iloc[0]
        self.assertEqual(pa["mun"], "05231")
        poa = df[(df["mun"] == "88013") & (df["partido"] == "PT")].iloc[0]
        self.assertEqual(int(poa["votos"]), 150)
        self.assertEqual(set(df["uf"]), {"rs", "pa"})
        uf = hist2022.aggregate_uf(df)
        self.assertEqual(int(uf[(uf["uf"] == "rs")]["votos"].sum()), 300)
        br = hist2022.aggregate_br(df)
        self.assertEqual(int(br[br["partido"] == "PT"]["votos"].iloc[0]), 157)

    def test_add_share(self):
        df = pd.DataFrame({"uf": ["rs", "rs", "sp"], "mun": ["", "", ""], "votos": [30, 70, 0]})
        out = hist2022.add_share(df)
        self.assertEqual(out["pct_validos"].round(6).tolist(), [30.0, 70.0, 0.0])


def s22(rows):
    return pd.DataFrame(rows, columns=["uf", "mun", "partido", "nm_urna", "votos", "pct_validos"])


def cands26(rows):
    """rows: (uf, mun, partido, nome_urna, votos, pct_secoes)."""
    return pd.DataFrame(rows, columns=["uf", "mun", "partido", "nome_urna", "votos", "pct_secoes"])


class SwingTests(unittest.TestCase):
    def setUp(self):
        self.old = s22([
            ("rs", "", "PT", "LULA", 42, 42.0), ("rs", "", "PL", "JAIR BOLSONARO", 49, 49.0),
            ("rs", "", "MDB", "SIMONE TEBET", 9, 9.0),
            ("sp", "", "PT", "LULA", 40, 40.0), ("sp", "", "PL", "JAIR BOLSONARO", 60, 60.0),
            ("ba", "", "PT", "LULA", 70, 70.0), ("ba", "", "PL", "JAIR BOLSONARO", 30, 30.0),
        ])

    def test_share_2026_and_swing(self):
        c = cands26([
            ("rs", "", "PT", "LULA", 450, 10.0), ("rs", "", "PL", "FLAVIO", 500, 10.0),
            ("rs", "", "NOVO", "ZEMA", 50, 10.0),
            ("sp", "", "PT", "LULA", 0, 0.0), ("sp", "", "PL", "FLAVIO", 0, 0.0),  # nada apurado
            ("ba", "", "PT", "LULA", 600, 3.0), ("ba", "", "PL", "FLAVIO", 400, 3.0),
        ])
        s = hist2022.share_2026(c)
        self.assertEqual(set(s["uf"]), {"rs", "ba"})  # sp sem votos fica de fora
        self.assertAlmostEqual(s[(s["uf"] == "rs") & (s["partido"] == "PT")]["pct_validos"].iloc[0], 45.0)
        sw = hist2022.compute_swing(s, self.old, "PT").set_index("uf")
        self.assertEqual(sorted(sw.index), ["ba", "rs"])
        self.assertAlmostEqual(sw.loc["rs", "swing_pp"], 3.0)
        self.assertAlmostEqual(sw.loc["ba", "swing_pp"], -10.0)
        self.assertEqual(sw.loc["rs", "nm_urna_2022"], "LULA")
        self.assertAlmostEqual(sw.loc["ba", "pct_secoes"], 3.0)
        # partido escolhido sem diferenciar maiúsculas
        self.assertEqual(len(hist2022.compute_swing(s, self.old, "pl")), 2)

    def test_party_missing_in_one_year(self):
        c = cands26([("rs", "", "PT", "LULA", 45, 10.0), ("rs", "", "NOVO", "ZEMA", 55, 10.0)])
        s = hist2022.share_2026(c)
        sw = hist2022.compute_swing(s, self.old, "NOVO")  # NOVO não existia na tabela de 2022
        self.assertAlmostEqual(sw["swing_pp"].iloc[0], 55.0)
        self.assertEqual(sw["nm_urna_2022"].iloc[0], "—")
        self.assertTrue(hist2022.compute_swing(s, self.old, "NOVO", require_both=True).empty)
        sw_pl = hist2022.compute_swing(s, self.old, "PL")
        self.assertAlmostEqual(sw_pl["swing_pp"].iloc[0], -49.0)

    def test_missing_areas_and_empty_inputs(self):
        c = cands26([("ap", "", "PT", "LULA", 10, 1.0)])  # área sem par em 2022
        s = hist2022.share_2026(c)
        self.assertTrue(hist2022.compute_swing(s, self.old, "PT").empty)
        self.assertTrue(hist2022.compute_swing(s.head(0), self.old, "PT").empty)
        self.assertTrue(hist2022.share_2026(c.head(0)).empty)
        self.assertIn("swing_pp", hist2022.compute_swing(s, self.old.head(0), "PT").columns)

    def test_municipal_keys_join_with_padded_codes(self):
        old = s22([("pa", "05231", "PT", "LULA", 60, 60.0), ("pa", "05231", "PL", "JAIR", 40, 40.0)])
        c = cands26([("pa", "05231", "PT", "LULA", 50, 20.0), ("pa", "05231", "PL", "FLAVIO", 50, 20.0)])
        sw = hist2022.compute_swing(hist2022.share_2026(c), old, "PT")
        self.assertEqual(sw["mun"].tolist(), ["05231"])
        self.assertAlmostEqual(sw["swing_pp"].iloc[0], -10.0)

    def test_turnout_change(self):
        t26 = pd.DataFrame({"uf": ["rs", "sp"], "mun": ["", ""], "eleitorado_apurado": [1000, 0],
                            "abstencao": [250, 0], "pct_secoes": [12.0, 0.0]})
        t22 = pd.DataFrame({"uf": ["rs", "sp"], "mun": ["", ""], "pct_abstencao": [20.0, 21.0]})
        a = hist2022.turnout_2026(t26)
        self.assertEqual(a["uf"].tolist(), ["rs"])  # sp sem eleitorado apurado fica de fora
        ch = hist2022.compute_turnout_change(a, t22)
        self.assertAlmostEqual(ch["abst_2026"].iloc[0], 25.0)
        self.assertAlmostEqual(ch["delta_pp"].iloc[0], 5.0)
        self.assertTrue(hist2022.compute_turnout_change(a.head(0), t22).empty)


def _fill_db(path: Path) -> None:
    when = datetime(2026, 10, 4, 18, 0, 0)
    conn = store.connect(path)
    for name, uf, pct in [("br-c0001-e006257-u.json", None, 40), ("rs-c0001-e006257-u.json", None, 35),
                          ("sp-c0001-e006257-u.json", None, 20), ("zz-c0001-e006257-u.json", None, 50),
                          ("rs88013-c0001-e006257-u.json", "rs", 60), ("rs-c0003-e006259-u.json", None, 30),
                          ("sp-c0003-e006259-u.json", None, 25)]:
        store.write_snapshot(conn, parse.parse_u(fake.fill_votes(load(name), pct, seed=1, when=when), uf=uf))
    conn.close()


def _make_hist(dirpath: Path) -> None:
    """Parquet mínimos de 2022 (RS, SP, Porto Alegre) no formato do build."""
    pres = pd.DataFrame([
        (1, "rs", "88013", "PORTO ALEGRE", "13", "LULA", "PT", 500), (1, "rs", "88013", "PORTO ALEGRE", "22", "JAIR", "PL", 400),
        (1, "rs", "84735", "ÁUREA", "13", "LULA", "PT", 100), (1, "rs", "84735", "ÁUREA", "22", "JAIR", "PL", 150),
        (1, "sp", "71072", "SÃO PAULO", "13", "LULA", "PT", 900), (1, "sp", "71072", "SÃO PAULO", "22", "JAIR", "PL", 800),
        (2, "rs", "88013", "PORTO ALEGRE", "13", "LULA", "PT", 550), (2, "rs", "88013", "PORTO ALEGRE", "22", "JAIR", "PL", 450),
    ], columns=["turno", "uf", "mun", "nome", "nr_candidato", "nm_urna", "partido", "votos"])
    pres["turno"] = pres["turno"].astype("int8")
    pres.to_parquet(dirpath / "pres_mun.parquet", index=False)
    hist2022.aggregate_uf(pres).to_parquet(dirpath / "pres_uf.parquet", index=False)
    hist2022.aggregate_br(pres).to_parquet(dirpath / "pres_br.parquet", index=False)
    gov = pres.assign(nm_urna=pres["nm_urna"] + " GOV")
    gov.to_parquet(dirpath / "gov_mun.parquet", index=False)
    rows = []
    for t in (1, 2):
        for c in (1, 3):
            for uf, mun in [("rs", "88013"), ("rs", "84735"), ("sp", "71072")]:
                rows.append((t, c, uf, mun, 1000, 800, 200, 20, 30, 750))
    tm = pd.DataFrame(rows, columns=["turno", "cargo", "uf", "mun", *hist2022.TURNOUT_COLS])
    tm[["turno", "cargo"]] = tm[["turno", "cargo"]].astype("int8")
    tm.to_parquet(dirpath / "turnout_mun.parquet", index=False)


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _make_hist(self.tmp)
        self.p = mock.patch.object(config, "HIST_DIR", self.tmp)
        self.p.start()
        hist2022._load.cache_clear()

    def tearDown(self):
        self.p.stop()
        hist2022._load.cache_clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_status_and_loaders(self):
        st = hist2022.status()
        self.assertTrue(st["ready"])
        self.assertEqual(st["parquet"]["pres_mun"], 8)
        br = hist2022.pres_share("br", 1)
        self.assertEqual(set(br["uf"]), {"br"})
        self.assertAlmostEqual(br["pct_validos"].sum(), 100.0)
        mu = hist2022.pres_share("mun", 1, uf="RS")
        self.assertEqual(set(mu["mun"]), {"88013", "84735"})
        ps = hist2022.party_share("uf", 2)
        self.assertEqual(ps["uf"].tolist(), ["rs", "rs"])
        self.assertAlmostEqual(ps[ps["partido"] == "PT"]["pct_validos"].iloc[0], 55.0)
        tu = hist2022.turnout("uf", 1, cargo=1)
        self.assertEqual(tu.set_index("uf").loc["rs", "aptos"], 2000)
        self.assertAlmostEqual(tu["pct_abstencao"].iloc[0], 20.0)
        self.assertEqual(len(hist2022.party_share("uf", 1, cargo=3)), 4)
        with self.assertRaises(ValueError):
            hist2022.pres_share("xx")

    def test_status_when_missing(self):
        empty = Path(tempfile.mkdtemp())
        try:
            with mock.patch.object(config, "HIST_DIR", empty):
                st = hist2022.status()
                self.assertFalse(st["ready"])
                self.assertIsNone(st["zips"]["votacao"])
                with self.assertRaises(FileNotFoundError):
                    hist2022.load("pres_mun")
        finally:
            shutil.rmtree(empty, ignore_errors=True)


class AppSmokeTests(unittest.TestCase):
    """Roda app.py com a tela '2022 × 2026' em várias seleções, com e sem os parquet de 2022."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.db = cls.tmp / "test.db"
        _fill_db(cls.db)
        cls.hist = cls.tmp / "2022"
        cls.hist.mkdir()
        _make_hist(cls.hist)
        cls.nohist = cls.tmp / "vazio"
        cls.nohist.mkdir()
        cls.env = mock.patch.dict(os.environ, {"ELEICOES_DB": str(cls.db)})
        cls.env.start()
        cls.dbp = mock.patch.object(config, "DB_PATH", cls.db)
        cls.dbp.start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.dbp.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _run(self, uf: str, cargo: int, hist_dir: Path, turno: int = 1, modo: str | None = None):
        from streamlit.testing.v1 import AppTest
        hist2022._load.cache_clear()
        with mock.patch.object(config, "HIST_DIR", hist_dir):
            at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
            at.session_state["view"] = "2022 × 2026"
            at.session_state["uf"] = uf
            at.session_state["cargo"] = cargo
            at.session_state["turno"] = turno
            if modo:
                at.session_state["cmp22_modo"] = modo
            at.run()
        return at

    def _ok(self, at):
        self.assertFalse(at.exception, [e.value for e in at.exception])
        errs = [e.value for e in at.error if "comparação com 2022" in str(e.value)]
        self.assertEqual(errs, [])

    def test_with_hist(self):
        for uf, cargo in [("br", 1), ("rs", 1), ("br", 3), ("rs", 3), ("br", 6), ("zz", 1)]:
            with self.subTest(uf=uf, cargo=cargo):
                at = self._run(uf, cargo, self.hist)
                self._ok(at)
                if uf != "zz":
                    self.assertTrue(any("2022 × 2026" in s.value for s in at.subheader))

    def test_modes(self):
        for modo in ("Dispersão 2022 × 2026", "Abstenção", "Tabela"):
            for uf in ("br", "rs"):
                with self.subTest(modo=modo, uf=uf):
                    self._ok(self._run(uf, 1, self.hist, modo=modo))

    def test_turno2_without_data(self):
        self._ok(self._run("br", 1, self.hist, turno=2))

    def test_without_hist(self):
        for uf, cargo in [("br", 1), ("rs", 1), ("br", 3)]:
            with self.subTest(uf=uf, cargo=cargo):
                at = self._run(uf, cargo, self.nohist)
                self._ok(at)
                self.assertTrue(any("eleicoes.hist2022" in c.value for c in at.code))


if __name__ == "__main__":
    unittest.main()
