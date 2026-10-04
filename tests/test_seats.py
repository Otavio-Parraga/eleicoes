import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

from eleicoes import fake, parse, seats, store
from eleicoes.seats import CandIn

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def C(sq, agr, votos, partido=None):
    return CandIn(sq, sq, partido or agr, agr, votos)


def by_sq(alloc):
    return {e.sqcand: e.via for e in alloc.elected}


class QuocienteTests(unittest.TestCase):
    def test_rounding(self):
        self.assertEqual(seats.quociente_eleitoral(1004, 10), 100)
        self.assertEqual(seats.quociente_eleitoral(1005, 10), 100)  # fração = 0,5 -> despreza
        self.assertEqual(seats.quociente_eleitoral(1006, 10), 101)  # fração > 0,5 -> arredonda p/ cima
        self.assertEqual(seats.quociente_eleitoral(0, 10), 0)

    def test_no_votes_no_seats(self):
        a = seats.allocate(10, {"A": 0}, [C("a1", "A", 0)])
        self.assertEqual(a.elected, [])
        self.assertEqual(a.qe, 0)


class AllocateTests(unittest.TestCase):
    def test_exact_qp(self):
        cands = [C(f"{p}{i}", p, 1000 - i) for p in "ABC" for i in range(6)]
        a = seats.allocate(10, {"A": 5000, "B": 3000, "C": 2000}, cands, validos=10000)
        self.assertEqual(a.qe, 1000)
        self.assertEqual(a.seats_agremiacao, {"A": 5, "B": 3, "C": 2})
        self.assertTrue(all(e.via == seats.VIA_QP for e in a.elected))
        self.assertIn("A0", by_sq(a))
        self.assertNotIn("A5", by_sq(a))

    def test_10pct_threshold_unfilled_qp_goes_to_sobras(self):
        # QE = 200; 10% = 20; 20% = 40
        cands = [C("a1", "A", 500), C("a2", "A", 80), C("a3", "A", 10),
                 C("b1", "B", 200), C("b2", "B", 150), C("b3", "B", 50)]
        a = seats.allocate(5, {"A": 600, "B": 400}, cands, validos=1000)
        self.assertEqual(a.qe, 200)
        self.assertEqual(a.qp, {"A": 3, "B": 2})
        got = by_sq(a)
        self.assertEqual(got["a1"], seats.VIA_QP)
        self.assertEqual(got["a2"], seats.VIA_QP)  # 80 >= 10% do QE
        self.assertNotIn("a3", got)                 # 10 < 10% do QE: vaga do QP vai para as sobras
        self.assertEqual(got["b3"], seats.VIA_MEDIA)  # 50 >= 20% do QE
        self.assertEqual(a.seats_agremiacao, {"A": 2, "B": 3})

    def test_80pct_threshold(self):
        # QE = 250; 80% = 200; 20% = 50
        cands = [C("a1", "A", 400), C("a2", "A", 200), C("a3", "A", 50),
                 C("b1", "B", 200), C("c1", "C", 140)]
        a = seats.allocate(4, {"A": 650, "B": 210, "C": 140}, cands, validos=1000)
        self.assertEqual(a.qe, 250)
        got = by_sq(a)
        self.assertEqual(got["a3"], seats.VIA_MEDIA)  # média 650/3 = 216,7 > 210
        self.assertEqual(got["b1"], seats.VIA_MEDIA)  # B tem 84% do QE: disputa
        self.assertNotIn("c1", got)                   # C tem 56% do QE: fora da fase 1
        self.assertEqual(a.seats_agremiacao, {"A": 3, "B": 1})

    def test_20pct_candidate_threshold_then_final_phase(self):
        # QE = 273 (820/3); A tem QP 2; a3 (10) < 20% do QE; B tem 76,9% do QE (< 80%)
        cands = [C("a1", "A", 520), C("a2", "A", 80), C("a3", "A", 10), C("b1", "B", 200)]
        a = seats.allocate(3, {"A": 610, "B": 210}, cands, validos=820)
        self.assertEqual(a.qe, 273)
        got = by_sq(a)
        self.assertEqual(got["a1"], seats.VIA_QP)
        self.assertEqual(got["a2"], seats.VIA_QP)
        # ninguém se qualifica na fase 1 -> fase final com todas (STF 2024): B (210/1) > A (610/3 = 203,3)
        self.assertEqual(got["b1"], seats.VIA_MEDIA_FINAL)
        self.assertNotIn("a3", got)

    def test_final_phase_falls_back_to_largest_average(self):
        cands = [C("a1", "A", 600), C("a2", "A", 50), C("a3", "A", 10), C("b1", "B", 200)]
        a = seats.allocate(3, {"A": 700, "B": 200}, cands, validos=900)
        got = by_sq(a)
        self.assertEqual(got["a3"], seats.VIA_MEDIA_FINAL)  # 700/3 = 233 > 200
        self.assertEqual(a.filled, 3)

    def test_federation_counts_as_one(self):
        fed = "P1 / P2"
        cands = [C("x1", fed, 250, "P1"), C("y1", fed, 120, "P2"), C("y2", fed, 80, "P2"),
                 C("q1", "Q", 400), C("q2", "Q", 50)]
        a = seats.allocate(3, {fed: 450, "Q": 450}, cands, validos=900)
        self.assertEqual(a.qe, 300)
        self.assertEqual(a.seats_agremiacao, {fed: 2, "Q": 1})
        self.assertEqual(a.seats_party, {"P1": 1, "P2": 1, "Q": 1})
        # sem a federação, P1 (250) e P2 (200) não alcançariam o QE
        sep = seats.allocate(3, {"P1": 250, "P2": 200, "Q": 450},
                             [C("x1", "P1", 250), C("y1", "P2", 120), C("y2", "P2", 80),
                              C("q1", "Q", 400), C("q2", "Q", 50)], validos=900)
        self.assertEqual(sep.qp["P1"], 0)
        self.assertEqual(sep.qp["Q"], 1)

    def test_federation_from_frames(self):
        parties = pd.DataFrame([
            {"sigla": "P1", "agremiacao": "P1 / P2", "votos_nominais": 250, "votos_legenda": 0},
            {"sigla": "P2", "agremiacao": "P1 / P2", "votos_nominais": 180, "votos_legenda": 20},
            {"sigla": "Q", "agremiacao": "Q", "votos_nominais": 450, "votos_legenda": 0},
        ])
        cands = pd.DataFrame([
            {"sqcand": "x1", "nome_urna": "X1", "partido": "P1", "agremiacao": "P1 / P2", "votos": 250},
            {"sqcand": "y1", "nome_urna": "Y1", "partido": "P2", "agremiacao": "P1 / P2", "votos": 120},
            {"sqcand": "y2", "nome_urna": "Y2", "partido": "P2", "agremiacao": "P1 / P2", "votos": 60},
            {"sqcand": "q1", "nome_urna": "Q1", "partido": "Q", "agremiacao": "Q", "votos": 400},
            {"sqcand": "q2", "nome_urna": "Q2", "partido": "Q", "agremiacao": "Q", "votos": 50},
        ])
        agr, cl, fallback = seats.build_inputs(parties, cands)
        self.assertEqual(agr, {"P1 / P2": 450, "Q": 450})
        self.assertFalse(fallback)
        a = seats.project(parties, cands, 3, 900)
        self.assertEqual(a.seats_agremiacao, {"P1 / P2": 2, "Q": 1})

    def test_fallback_to_candidate_votes(self):
        parties = pd.DataFrame([{"sigla": "A", "agremiacao": "A", "votos_nominais": 0, "votos_legenda": 0}])
        cands = pd.DataFrame([{"sqcand": "a1", "nome_urna": "A1", "partido": "A", "agremiacao": "A", "votos": 70},
                              {"sqcand": "a2", "nome_urna": "A2", "partido": "A", "agremiacao": "A", "votos": 30}])
        agr, _, fallback = seats.build_inputs(parties, cands)
        self.assertEqual(agr, {"A": 100})
        self.assertTrue(fallback)

    def test_art111_nobody_reaches_qe(self):
        cands = [C("a1", "A", 400), C("b1", "B", 200), C("b2", "B", 100), C("c1", "C", 300)]
        a = seats.allocate(2, {"A": 400, "B": 300, "C": 300}, cands, validos=1000)
        self.assertEqual(set(by_sq(a)), {"a1", "c1"})
        self.assertTrue(all(e.via == seats.VIA_ART111 for e in a.elected))

    def test_not_enough_candidates(self):
        a = seats.allocate(5, {"A": 1000}, [C("a1", "A", 900), C("a2", "A", 100)], validos=1000)
        self.assertEqual(a.filled, 2)


class FixtureTests(unittest.TestCase):
    def _project(self, name, pct=60.0):
        with tempfile.TemporaryDirectory() as tmp:
            conn = store.connect(Path(tmp) / "t.db")
            snap = parse.parse_u(fake.fill_votes(load(name), pct, seed=1, when=datetime(2026, 10, 4, 17, 30)))
            store.write_snapshot(conn, snap)
            uf = snap.uf
            cands = store.latest_candidates(conn, snap.eleicao, snap.cargo, "uf", uf)
            parties = store.latest_parties(conn, snap.eleicao, snap.cargo, "uf", uf)
            tot = store.latest_totals(conn, snap.eleicao, snap.cargo, "uf", uf).iloc[0]
            conn.close()
        return snap, seats.project(parties, cands, int(tot["vagas"]), int(tot["validos"]))

    def test_rs_dep_federal_31_seats(self):
        snap, a = self._project("rs-c0006-e006259-u.json")
        self.assertEqual(a.vagas, 31)
        self.assertEqual(a.filled, 31)
        self.assertEqual(sum(a.seats_agremiacao.values()), 31)
        self.assertEqual(sum(a.seats_party.values()), 31)
        self.assertEqual(len({e.sqcand for e in a.elected}), 31)
        self.assertGreater(a.qe, 0)
        # todo eleito por QP tem >= 10% do QE
        self.assertTrue(all(e.votos * 10 >= a.qe for e in a.elected if e.via == seats.VIA_QP))

    def test_rs_dep_estadual_and_df(self):
        _, a = self._project("rs-c0007-e006259-u.json", 35)
        self.assertEqual(a.filled, 55)
        _, d = self._project("df-c0008-e006259-u.json", 80)
        self.assertEqual(d.filled, 24)


if __name__ == "__main__":
    unittest.main()
