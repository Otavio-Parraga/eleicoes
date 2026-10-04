import gzip
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import httpx

from eleicoes import collector, fake, notify, parse, store, tse
from eleicoes.collector import detect_events

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def snap(name, pct, seed=1, bias=None, when=None, uf=None, **edits):
    doc = fake.fill_votes(load(name), pct, seed=seed, bias=bias, when=when or datetime(2026, 10, 4, 18, 0, 0))
    return parse.parse_u(doc, uf=uf)


class DetectEventsTests(unittest.TestCase):
    def test_first_sighting(self):
        self.assertEqual(detect_events(None, snap("br-c0001-e006257-u.json", 50)), [])

    def test_no_change(self):
        s = snap("rs-c0003-e006259-u.json", 30)
        self.assertEqual(detect_events(s, s), [])

    def test_municipio_ignored(self):
        a = snap("rs88013-c0001-e006257-u.json", 5, uf="rs")
        b = snap("rs88013-c0001-e006257-u.json", 100, uf="rs")
        self.assertEqual(detect_events(a, b), [])

    def test_milestone(self):
        a = snap("br-c0001-e006257-u.json", 8)
        b = snap("br-c0001-e006257-u.json", 27)
        ev = detect_events(a, b)
        self.assertEqual([k for k, _ in ev], ["marco"])
        self.assertTrue(ev[0][1].startswith("Presidente: 25% das seções apuradas"))

    def test_virada_president(self):
        base = parse.parse_u(fake.fill_votes(load("br-c0001-e006257-u.json"), 5, seed=3))
        nums = [c.numero for c in base.candidates[:2]]
        a = snap("br-c0001-e006257-u.json", 5, seed=3, bias={nums[0]: 3.0})
        b = snap("br-c0001-e006257-u.json", 6, seed=3, bias={nums[1]: 10.0})
        self.assertNotEqual(a.candidates[0].sqcand, b.candidates[0].sqcand)
        ev = detect_events(a, b)
        kinds = [k for k, _ in ev]
        self.assertEqual(kinds, ["virada"])
        msg = ev[0][1]
        self.assertTrue(msg.startswith(f"Presidente: virada — {b.candidates[0].nome_urna}"), msg)
        self.assertIn(f"passa {a.candidates[0].nome_urna}", msg)
        self.assertIn("6,0% apurado", msg)

    def test_virada_needs_1pct(self):
        base = parse.parse_u(fake.fill_votes(load("rs-c0003-e006259-u.json"), 0.5, seed=3))
        nums = [c.numero for c in base.candidates[:2]]
        a = snap("rs-c0003-e006259-u.json", 0.3, seed=3, bias={nums[0]: 3.0})
        b = snap("rs-c0003-e006259-u.json", 0.6, seed=3, bias={nums[1]: 10.0})
        self.assertEqual([k for k, _ in detect_events(a, b)], [])

    def test_governor_eleito_and_finalizada(self):
        # bias forte → um candidato passa de 50% e é eleito no 1º turno
        base = parse.parse_u(fake.fill_votes(load("rs-c0003-e006259-u.json"), 50, seed=2))
        top = base.candidates[0].numero
        a = snap("rs-c0003-e006259-u.json", 90, seed=2, bias={top: 100.0})
        b = snap("rs-c0003-e006259-u.json", 100, seed=2, bias={top: 100.0})
        ev = detect_events(a, b)
        kinds = [k for k, _ in ev]
        self.assertIn("eleito", kinds)
        self.assertIn("finalizada", kinds)
        eleito = [m for k, m in ev if k == "eleito"][0]
        c = b.candidates[0]
        self.assertEqual(eleito, f"Governador RS: {c.nome_urna} ({c.partido}) eleito")
        self.assertIn("Governador RS: apuração finalizada", [m for _, m in ev])

    def test_segundo_turno(self):
        a = snap("sp-c0003-e006259-u.json", 95, seed=5)
        doc = fake.fill_votes(load("sp-c0003-e006259-u.json"), 100, seed=5)
        ranked = sorted(fake._iter_cands(doc), key=lambda c: -int(c["vap"]))
        for c in ranked:
            c["e"], c["st"] = "n", ""
        for c in ranked[:2]:
            c["st"] = "2º turno"
        b = parse.parse_u(doc)
        ev = detect_events(a, b)
        msgs = [m for k, m in ev if k == "segundo_turno"]
        self.assertEqual(len(msgs), 1, ev)
        self.assertTrue(msgs[0].startswith("Governador SP: 2º turno entre "), msgs[0])
        self.assertNotIn("eleito", [k for k, _ in ev])

    def test_president_uf_level_silent(self):
        a = snap("rs-c0001-e006257-u.json", 90)
        b = snap("rs-c0001-e006257-u.json", 100)
        self.assertEqual(detect_events(a, b), [])

    def test_senador_eleitos(self):
        a = snap("rs-c0005-e006259-u.json", 90)
        b = snap("rs-c0005-e006259-u.json", 100)
        ev = detect_events(a, b)
        self.assertEqual(sum(1 for k, _ in ev if k == "eleito"), b.vagas)
        self.assertTrue(all(m.startswith("Senador RS: ") for _, m in ev))


class NotifyTests(unittest.TestCase):
    def test_escape(self):
        s = notify.build_script('Tí"tulo', 'a\\b "c"\nd')
        self.assertEqual(s, 'display notification "a\\\\b \\"c\\" d" with title "Tí\\"tulo"')


class CollectorTests(unittest.TestCase):
    """Coletor de ponta a ponta com transporte HTTP falso (sem rede)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.pct = 0.0
        self.when = datetime(2026, 10, 4, 17, 30, 0)

    def handler(self, req: httpx.Request):
        name = str(req.url).rsplit("/", 1)[-1]
        uf = name.split("-")[0]
        if name == "br-c0001-e006257-u.json":
            src = "br-c0001-e006257-u.json"
        elif name.endswith("-c0003-e006259-u.json") and uf == "rs":
            src = "rs-c0003-e006259-u.json"
        else:
            return httpx.Response(404)
        doc = fake.fill_votes(load(src), self.pct, seed=1, when=self.when)
        etag = f'"{self.pct}"'
        if req.headers.get("if-none-match") == etag:
            return httpx.Response(304, headers={"ETag": etag})
        return httpx.Response(200, content=json.dumps(doc).encode(), headers={"ETag": etag})

    def make(self):
        conn = store.connect(self.dir / "c.db")
        self.addCleanup(conn.close)
        f = tse.Fetcher(client=httpx.Client(transport=httpx.MockTransport(self.handler)), concurrency=4,
                        retries=0, backoff=0)
        self.addCleanup(f.close)
        c = collector.Collector(conn, 1, [], fetcher=f, raw_dir=self.dir / "raw", notify_on=False)
        return conn, c

    def test_cycles(self):
        conn, c = self.make()
        s1 = c.tier1_cycle()
        self.assertEqual((s1["200"], s1["404"], s1["new"]), (2, 135, 2))
        self.assertEqual(len(store.latest_totals(conn, "6257", 1, "br")), 1)
        raws = list((self.dir / "raw" / "6257").glob("*.json.gz"))
        self.assertEqual([p.name for p in raws], ["br-c0001-e006257-u.20261004T173000.json.gz"])
        self.assertEqual(json.loads(gzip.decompress(raws[0].read_bytes()))["ele"], "6257")
        s2 = c.tier1_cycle()
        self.assertEqual(s2["304"], 2)
        self.pct, self.when = 30.0, datetime(2026, 10, 4, 18, 0, 0)
        s3 = c.tier1_cycle()
        self.assertEqual(s3["new"], 2)
        ev = store.events_since(conn)
        self.assertIn("marco", ev["kind"].tolist())
        self.assertIsNotNone(store.get_meta(conn, "collector_heartbeat"))
        self.assertEqual(store.get_meta(conn, "collector_last_tse_ts"), "2026-10-04T18:00:00")
        stats = json.loads(store.get_meta(conn, "collector_stats"))
        self.assertEqual(stats["tier1"]["new"], 2)
        self.assertEqual(store.get_http_cache(conn, tse.url_for("6257", 1, "br"))[0], '"30.0"')

    def test_loop_focus_added_midrun(self):
        import threading
        import time

        conn, c = self.make()
        c.cli_focus = ["df"]
        th = threading.Thread(target=c.run, kwargs=dict(tier1_s=0.3, tier2_s=1000), daemon=True)
        th.start()
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not c.last_tier2:
                time.sleep(0.05)
            self.assertEqual(c.last_tier2["ufs"], ["df"])
            self.assertEqual(c.last_tier2["404"], 5)  # Brasília × 5 cargos
            other = store.connect(self.dir / "c.db")
            store.add_focus(other, "ac")
            other.close()
            n_ac = len(tse.tier2_targets(1, "ac"))
            while time.time() < deadline and "ac" not in c.last_tier2.get("ufs", []):
                time.sleep(0.05)
            self.assertIn("ac", c.last_tier2["ufs"])
            self.assertEqual(c.last_tier2["404"], n_ac)
        finally:
            c.stop = True
            th.join(5)
        self.assertFalse(th.is_alive())

    def test_bad_doc_does_not_crash(self):
        conn, c = self.make()
        orig = self.handler

        def broken(req):
            if "br-c0001" in str(req.url):
                return httpx.Response(200, json={"tpabr": "xx"}, headers={"ETag": '"b"'})
            return orig(req)

        c.fetcher.client = httpx.Client(transport=httpx.MockTransport(broken))
        with self.assertLogs("eleicoes.collector", level="WARNING"):
            s = c.tier1_cycle()
        self.assertEqual(s["parse_err"], 1)
        self.assertIsNone(c.fetcher.cache_get(tse.url_for("6257", 1, "br")))


if __name__ == "__main__":
    unittest.main()
