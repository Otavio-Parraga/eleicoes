"""Testes do simulador (sem rede: só tests/fixtures/)."""
import re
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from eleicoes import store
from tools import simulate as sm

FIX = Path(__file__).parent / "fixtures"
NAME_RE = re.compile(r"([a-z]{2})(\d{5})?-c(\d{4})-e(\d{6})-u\.json")


def fixture_base() -> dict:
    base = {}
    for p in FIX.glob("*-u.json"):
        m = NAME_RE.fullmatch(p.name)
        if m:
            base[(str(int(m.group(4))), int(m.group(3)), m.group(1), m.group(2) or "")] = p
    return base


class SimulateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(Path(self.tmp.name) / "sim.db")
        self.sim = sm.Simulation(fixture_base(), seed=1)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def run_steps(self, minutes):
        for m in minutes:
            sm.simulate_step(self.conn, self.sim, sm.SIM_START + timedelta(minutes=m))

    def test_curve_shape(self):
        self.assertEqual(sm.curve_pct(0, 2, 0.5, 5), 0.0)
        self.assertEqual(sm.curve_pct(5, 2, 0.5, 5), 100.0)
        vals = [sm.curve_pct(t / 10, 2, 0.5, 5) for t in range(51)]
        self.assertEqual(vals, sorted(vals))
        # Sul apura antes do Nordeste
        self.assertLess(sm.uf_curve("sc", 1)[0], sm.uf_curve("ba", 1)[0])

    def test_steps_build_history(self):
        self.run_steps([0, 40, 80, 120])
        h = store.history_totals(self.conn, "6257", 1)
        self.assertGreaterEqual(len(h), 2)
        pct = list(h["pct_secoes"])
        self.assertEqual(pct, sorted(pct))
        self.assertGreater(pct[-1], pct[0])
        uf = store.latest_totals(self.conn, "6257", 1, "uf")
        self.assertTrue({"rs", "sp", "zz"} <= set(uf["uf"]))
        mu = store.latest_totals(self.conn, "6257", 1, "mu", uf="rs")
        self.assertIn("88013", set(mu["mun"]))
        self.assertIsNotNone(store.get_meta(self.conn, "collector_heartbeat"))
        self.assertEqual(store.get_meta(self.conn, "collector_last_tse_ts"), "2026-10-04T19:00:00")

    def test_full_count_finalizes_and_virada(self):
        self.run_steps(range(0, 361, 12))
        self.assertTrue(self.sim.done)
        br = store.latest_totals(self.conn, "6257", 1, "br")
        self.assertEqual(br["pct_secoes"].iloc[0], 100.0)
        self.assertTrue(bool(br["finalizada"].iloc[0]))
        self.assertTrue(store.latest_totals(self.conn, "6259", 3, "uf")["finalizada"].all())
        self.assertTrue(store.latest_totals(self.conn, "6259", 6, "mu", uf="rs")["finalizada"].all())
        # Liderança nacional muda durante a noite (virada) e há eventos para a interface.
        ev = store.events_since(self.conn)
        self.assertGreaterEqual((ev["kind"] == "lider").sum(), 2)
        self.assertTrue({"marco", "lider"} <= set(ev["kind"]))
        self.assertTrue(set(ev["kind"]) & {"eleito", "segundo_turno"})
        top = store.leaders(self.conn, "6257", 1, "br").iloc[0]
        self.assertTrue(30 < top["pct_validos"] < 60)
        # Proporcionais têm votos de legenda e por partido.
        rs6 = store.latest_totals(self.conn, "6259", 6, "uf", uf="rs").iloc[0]
        self.assertGreater(rs6["legenda"], 0)
        self.assertEqual(rs6["nominais"] + rs6["legenda"], rs6["validos"])
        parties = store.latest_parties(self.conn, "6259", 6, "uf", uf="rs")
        self.assertEqual(parties["votos_nominais"].sum(), rs6["nominais"])

    def test_instant_mode_writes_all_areas(self):
        info = sm.simulate_step(self.conn, self.sim, sm.SIM_START + timedelta(hours=2), pct=40, force=True)
        self.assertEqual(info["br_pct"], 40.0)
        self.assertEqual(info["written"], len(self.sim.state_docs) - 1 + len(self.sim.mun_paths))
        sp = store.latest_totals(self.conn, "6259", 3, "uf", uf="sp")
        self.assertAlmostEqual(sp["pct_secoes"].iloc[0], 40.0)

    def test_president_roles_and_bias(self):
        doc = sm.load_doc(FIX / "br-c0001-e006257-u.json")
        roles = sm.president_roles(doc)
        self.assertEqual(roles[:2], ("13", "22"))
        targets = sm.president_targets(doc, roles, "ba", "", 1.0, 1)
        self.assertAlmostEqual(sum(targets.values()), 1.0, places=6)
        self.assertGreater(targets["13"], targets["22"])
        targets_sul = sm.president_targets(doc, roles, "sc", "", 1.0, 1)
        self.assertGreater(targets_sul["22"], targets_sul["13"])


if __name__ == "__main__":
    unittest.main()
