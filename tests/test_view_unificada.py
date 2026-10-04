import tempfile
import unittest
from pathlib import Path

import pandas as pd

from eleicoes import store
from eleicoes.views import unificada as u
from tests.test_views_core import build_db, failures, run_app

VIEW = "Visão unificada"


def cands(rows):
    return pd.DataFrame(rows, columns=["sqcand", "numero", "nome_urna", "partido", "votos", "pct_validos", "eleito",
                                       "situacao", "vice"])


TOTAL = pd.Series({"finalizada": 0, "pct_secoes": 40.0, "brancos": 10, "nulos": 5, "votos_total": 100,
                   "validos": 85, "vagas": 1})


class PureTests(unittest.TestCase):
    def test_offices_for(self):
        self.assertEqual(u.offices_for(1, "rs"), ([1, 3, 5], [6, 7]))
        self.assertEqual(u.offices_for(2, "rs"), ([1, 3], []))
        self.assertEqual(u.offices_for(1, "zz"), ([1], []))
        self.assertEqual(u.office_title(7, "df"), "Deputado Distrital")

    def test_status(self):
        c = cands([("a", "15", "ANA", "MDB", 60, 60.0, False, "", ""), ("b", "13", "BIA", "PT", 40, 40.0, False, "", "")])
        self.assertEqual(u.office_status(3, 1, 1, c.head(0), None, {}), ("Aguardando os primeiros votos", "espera"))
        self.assertEqual(u.office_status(3, 1, 1, c, TOTAL, {"maioria": "Vence no 1º turno (na prática)"})[1],
                         "confirma")
        self.assertEqual(u.office_status(3, 1, 1, c, TOTAL, {"maioria": "2º turno (na prática)"})[1], "corrige")
        self.assertEqual(u.office_status(1, 1, 1, c, TOTAL, {"status": "Em aberto"}, onde="no município")[0],
                         "Em aberto no município")
        el = c.assign(eleito=[True, False])
        self.assertEqual(u.office_status(3, 1, 1, el, TOTAL, {}), ("Eleito: ANA", "confirma"))
        seg = c.assign(situacao=["2º turno", "2º turno"])
        self.assertEqual(u.office_status(3, 1, 1, seg, TOTAL, {}), ("2º turno: ANA × BIA", "corrige"))

    def test_senate_cut_note(self):
        c = cands([("a", "111", "A", "PL", 50, 50.0, False, "", ""), ("b", "222", "B", "PT", 30, 30.0, False, "", ""),
                   ("c", "333", "C", "MDB", 20, 20.0, False, "", "")])
        self.assertIn("C está a 10 votos", u.senate_cut_note(c, 2))
        self.assertEqual(u.senate_cut_note(c.head(2), 2), "")

    def test_seat_tiles(self):
        el = pd.DataFrame({"partido": ["PT", "PL", "PL"], "sqcand": ["1", "2", "3"]})
        tiles = u.seat_tiles(el, 5)
        self.assertEqual([t[0] for t in tiles], ["PL", "PL", "PT", "", ""])
        self.assertEqual(u.tile_columns(31), 16)
        self.assertEqual(u.tile_columns(55), 14)

    def test_small_helpers(self):
        self.assertEqual(u.digits("13"), ["1", "3"])
        self.assertEqual(u.bar_scale(3, 60), 100.0)
        self.assertEqual(u.bar_scale(5, 31.2), 40.0)
        self.assertEqual(u.initials("MARCEL VAN HATTEM"), "MV")
        self.assertIn("/6257/fotos/br/x.jpeg", u.photo_url(1, "rs", "x"))
        self.assertIn("/6259/fotos/rs/x.jpeg", u.photo_url(3, "rs", "x"))

    def test_html_is_escaped(self):
        c = cands([("a", "15", "<script>x</script>", "MDB", 60, 60.0, False, "", "")])
        html = u.bu_list(c, set())
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


class PageTests(unittest.TestCase):
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

    def test_build_page_rs(self):
        conn = store.connect(self.db)
        try:
            page = u.build_page(conn, 1, "rs", None, False)
        finally:
            conn.close()
        for txt in ("Presidente", "Governador", "Senador", "Deputado Federal", "Deputado Estadual", "Número:",
                    "class=\"cadeiras\"", "Quociente eleitoral"):
            self.assertIn(txt, page)
        self.assertEqual(page.count("class=\"cadeiras\""), 2)

    def test_build_page_municipio_has_no_seats(self):
        conn = store.connect(self.db)
        try:
            mun = store.latest_totals(conn, "6257", 1, "mu", uf="rs")["mun"].iloc[0]
            page = u.build_page(conn, 1, "rs", mun, True)
        finally:
            conn.close()
        self.assertNotIn("class=\"cadeiras\"", page)
        self.assertIn("Mais votados no município", page)
        self.assertIn("uni dark", page)

    def test_app_smoke(self):
        cases = [dict(uf="rs"), dict(uf="br"), dict(uf="sp"), dict(uf="df"), dict(uf="zz"), dict(uf="rs", turno=2)]
        for case in cases:
            with self.subTest(**case):
                at = run_app(self.db, view=VIEW, cargo=1, turno=case.get("turno", 1), uf=case["uf"])
                self.assertEqual(failures(at), [])

    def test_app_empty_db(self):
        at = run_app(self.empty, view=VIEW, cargo=1, turno=1, uf="rs")
        self.assertEqual(failures(at), [])


if __name__ == "__main__":
    unittest.main()
