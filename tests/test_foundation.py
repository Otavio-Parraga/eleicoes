import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from eleicoes import fake, parse, store

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


class ParseTests(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(parse.br_int("3077"), 3077)
        self.assertEqual(parse.br_float("45,67"), 45.67)
        self.assertEqual(parse.br_float("1.234,5"), 1234.5)
        self.assertEqual(parse.br_float(""), 0.0)

    def test_br_president(self):
        s = parse.parse_u(load("br-c0001-e006257-u.json"))
        self.assertEqual((s.eleicao, s.cargo, s.nivel, s.uf, s.mun), ("6257", 1, "br", "br", ""))
        self.assertEqual(s.vagas, 1)
        self.assertGreater(s.totals.eleitorado, 150_000_000)
        self.assertGreater(len(s.candidates), 3)
        self.assertIsInstance(s.tse_ts, datetime)

    def test_municipio_requires_uf(self):
        with self.assertRaises(ValueError):
            parse.parse_u(load("rs88013-c0001-e006257-u.json"))
        s = parse.parse_u(load("rs88013-c0001-e006257-u.json"), uf="rs")
        self.assertEqual((s.nivel, s.uf, s.mun), ("mu", "rs", "88013"))

    def test_deputados_federation(self):
        s = parse.parse_u(load("rs-c0006-e006259-u.json"))
        self.assertEqual(s.vagas, 31)
        psol = [p for p in s.parties if p.sigla == "PSOL"][0]
        self.assertEqual(psol.federacao, "PSOL/REDE")
        self.assertEqual(psol.agremiacao_tipo, "f")
        self.assertEqual(len(s.candidates), 435)

    def test_fake_votes_parse(self):
        doc = fake.fill_votes(load("rs-c0003-e006259-u.json"), 42.5, seed=1)
        s = parse.parse_u(doc)
        self.assertAlmostEqual(s.totals.pct_secoes, 42.5)
        self.assertGreater(s.totals.validos, 0)
        self.assertAlmostEqual(sum(c.votos for c in s.candidates), s.totals.validos, delta=len(s.candidates))
        self.assertEqual(s.candidates[0].votos, max(c.votos for c in s.candidates))

    def test_fake_final_marks_result(self):
        s = parse.parse_u(fake.fill_votes(load("rs-c0005-e006259-u.json"), 100, seed=3))
        self.assertTrue(s.finalizada)
        self.assertEqual(sum(c.eleito for c in s.candidates), 2)  # Senado 2026: 2 vagas


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _write(self, name, pct, minute, uf=None):
        doc = fake.fill_votes(load(name), pct, seed=7, when=datetime(2026, 10, 4, 17, minute))
        return store.write_snapshot(self.conn, parse.parse_u(doc, uf=uf))

    def test_history_and_dedupe(self):
        self.assertIsNotNone(self._write("br-c0001-e006257-u.json", 10, 5))
        self.assertIsNone(self._write("br-c0001-e006257-u.json", 10, 5))
        self._write("br-c0001-e006257-u.json", 30, 10)
        h = store.history_totals(self.conn, "6257", 1)
        self.assertEqual(list(h["pct_secoes"]), [10.0, 30.0])
        tot = store.latest_totals(self.conn, "6257", 1, "br")
        self.assertEqual(len(tot), 1)
        self.assertEqual(tot.iloc[0]["pct_secoes"], 30.0)

    def test_municipio_keeps_only_latest(self):
        self._write("rs88013-c0001-e006257-u.json", 10, 5, uf="rs")
        self._write("rs88013-c0001-e006257-u.json", 50, 9, uf="rs")
        n = self.conn.execute("SELECT COUNT(*) FROM snapshot WHERE nivel='mu'").fetchone()[0]
        self.assertEqual(n, 1)
        n_c = self.conn.execute("SELECT COUNT(DISTINCT snapshot_id) FROM cand_result").fetchone()[0]
        self.assertEqual(n_c, 1)
        c = store.latest_candidates(self.conn, "6257", 1, "mu", uf="rs")
        self.assertEqual(c.iloc[0]["rank"], 1)
        self.assertEqual(c.iloc[0]["pct_secoes"], 50.0)

    def test_proportional_history_is_pruned_latest_complete(self):
        self._write("rs-c0006-e006259-u.json", 10, 5)
        self._write("rs-c0006-e006259-u.json", 20, 9)
        counts = [r[0] for r in self.conn.execute(
            "SELECT COUNT(*) FROM cand_result GROUP BY snapshot_id ORDER BY snapshot_id")]
        self.assertEqual(counts, [store.HISTORY_TOP_N, 435])
        self.assertEqual(len(store.latest_candidates(self.conn, "6259", 6, "uf", uf="rs")), 435)

    def test_leaders_and_cargo7_includes_df(self):
        self._write("rs-c0007-e006259-u.json", 60, 5)
        self._write("df-c0008-e006259-u.json", 60, 5)
        ld = store.leaders(self.conn, "6259", 7, "uf")
        self.assertEqual(set(ld["uf"]), {"rs", "df"})
        self.assertTrue((ld["margin_pp"] >= 0).all())

    def test_search_focus_events_meta(self):
        self._write("rs-c0003-e006259-u.json", 20, 5)
        anyname = store.latest_candidates(self.conn, "6259", 3, "uf").iloc[0]["nome_urna"]
        self.assertGreaterEqual(len(store.search_candidates(self.conn, "6259", 3, anyname[:4].lower())), 1)
        store.set_focus(self.conn, ["rs", "sp"])
        store.set_focus(self.conn, ["sp"])
        self.assertEqual(store.get_focus(self.conn), ["sp"])
        store.add_event(self.conn, "eleito", "X eleito", "6259", 3, "rs")
        self.assertEqual(len(store.events_since(self.conn, 0)), 1)
        store.set_meta(self.conn, "k", "v")
        self.assertEqual(store.get_meta(self.conn, "k"), "v")


if __name__ == "__main__":
    unittest.main()
