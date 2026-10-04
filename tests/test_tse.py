import json
import unittest
from pathlib import Path

import httpx

from eleicoes import config, tse

FIX = Path(__file__).parent / "fixtures"
BASE = f"{config.TSE_BASE}/{config.CICLO}"


class UrlTests(unittest.TestCase):
    def test_url_for(self):
        self.assertEqual(tse.url_for("6257", 1, "br"), f"{BASE}/6257/dados/br/br-c0001-e006257-u.json")
        self.assertEqual(tse.url_for("6257", 1, "rs", "88013"),
                         f"{BASE}/6257/dados/rs/rs88013-c0001-e006257-u.json")
        self.assertEqual(tse.url_for(6259, 6, "RS"), f"{BASE}/6259/dados/rs/rs-c0006-e006259-u.json")

    def test_target(self):
        t = tse.Target("6259", 3, "RS")
        self.assertEqual(t.url, f"{BASE}/6259/dados/rs/rs-c0003-e006259-u.json")
        self.assertEqual((t.uf, t.nivel, t.basename), ("rs", "uf", "rs-c0003-e006259-u"))
        self.assertEqual(tse.Target("6257", 1, "br").nivel, "br")
        self.assertEqual(tse.Target("6257", 1, "rs", "88013").nivel, "mu")


class TargetTests(unittest.TestCase):
    def test_tier1_turno1(self):
        ts = tse.tier1_targets(1)
        self.assertEqual(len(ts), 137)
        self.assertEqual(len({t.url for t in ts}), 137)
        pres = [t for t in ts if t.cargo == 1]
        self.assertEqual(len(pres), 29)
        self.assertIn("zz", {t.uf for t in pres})
        self.assertEqual([t.uf for t in ts if t.cargo == 8], ["df"])
        self.assertEqual(len([t for t in ts if t.cargo == 7]), 26)
        self.assertTrue(all(t.eleicao == "6259" for t in ts if t.cargo != 1))

    def test_tier1_turno2(self):
        ts = tse.tier1_targets(2)
        self.assertEqual(len(ts), 29 + 27)
        self.assertEqual({t.eleicao for t in ts}, {"6258", "6260"})

    def test_tier2(self):
        ts = tse.tier2_targets(1, "rs")
        self.assertEqual(len(ts), 497 * 5)
        self.assertEqual({t.cargo for t in ts}, {1, 3, 5, 6, 7})
        self.assertTrue(all(t.nivel == "mu" for t in ts))
        self.assertIn(f"{BASE}/6259/dados/rs/rs88013-c0006-e006259-u.json", {t.url for t in ts})
        self.assertEqual(tse.tier2_targets(1, "zz"), [])
        self.assertEqual({t.cargo for t in tse.tier2_targets(1, "df")}, {1, 3, 5, 6, 8})
        self.assertEqual(len(tse.tier2_targets(2, "rs")), 497 * 2)


class FetcherTests(unittest.TestCase):
    def make(self, handler, **kw):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        f = tse.Fetcher(client=client, concurrency=2, retries=kw.pop("retries", 0), backoff=0.0)
        self.addCleanup(f.close)
        return f

    def test_200_then_304(self):
        body = (FIX / "rs-c0003-e006259-u.json").read_bytes()
        seen = []

        def handler(req: httpx.Request):
            seen.append(dict(req.headers))
            if req.headers.get("if-none-match") == '"abc"':
                return httpx.Response(304, headers={"ETag": '"abc"'})
            return httpx.Response(200, content=body, headers={
                "ETag": '"abc"', "Last-Modified": "Sun, 04 Oct 2026 17:00:00 GMT"})

        f = self.make(handler)
        t = tse.Target("6259", 3, "rs")
        r1 = f.fetch_one(t)
        self.assertEqual(r1.status, 200)
        self.assertEqual(r1.etag, '"abc"')
        self.assertEqual(r1.doc["ele"], "6259")
        self.assertNotIn("if-none-match", seen[0])
        r2 = f.fetch_one(t)
        self.assertEqual(r2.status, 304)
        self.assertIsNone(r2.doc)
        self.assertEqual(seen[1].get("if-none-match"), '"abc"')
        self.assertEqual(seen[1].get("if-modified-since"), "Sun, 04 Oct 2026 17:00:00 GMT")

    def test_persist_and_seed(self):
        import tempfile
        from eleicoes import store

        def handler(req):
            return httpx.Response(200, json={"ok": 1}, headers={"ETag": '"e1"', "Last-Modified": "x"})

        f = self.make(handler)
        t = tse.Target("6259", 3, "rs")
        with tempfile.TemporaryDirectory() as d:
            conn = store.connect(Path(d) / "t.db")
            f.fetch_all([t])
            self.assertEqual(f.persist_cache(conn), 1)
            self.assertEqual(f.persist_cache(conn), 0)
            self.assertEqual(store.get_http_cache(conn, t.url), ('"e1"', "x"))
            g = self.make(handler)
            self.assertEqual(g.seed_from_store(conn, [t.url]), 1)
            self.assertEqual(g.cache_get(t.url), ('"e1"', "x"))
            conn.close()

    def test_404(self):
        f = self.make(lambda req: httpx.Response(404))
        r = f.fetch_one(tse.Target("6260", 3, "rs"))
        self.assertEqual(r.status, 404)
        self.assertIsNone(f.cache_get(r.target.url))

    def test_timeout_is_err(self):
        def handler(req):
            raise httpx.ReadTimeout("slow", request=req)

        f = self.make(handler, retries=1)
        r = f.fetch_one(tse.Target("6257", 1, "br"))
        self.assertEqual(r.status, "err")
        self.assertIn("timeout", r.error)

    def test_500_retry_then_ok(self):
        calls = []

        def handler(req):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={"a": 1})

        f = self.make(handler, retries=2)
        r = f.fetch_one(tse.Target("6257", 1, "br"))
        self.assertEqual(r.status, 200)
        self.assertEqual(len(calls), 2)

    def test_bad_json_is_err_and_not_cached(self):
        f = self.make(lambda req: httpx.Response(200, content=b"{trunc", headers={"ETag": '"x"'}))
        r = f.fetch_one(tse.Target("6257", 1, "br"))
        self.assertEqual(r.status, "err")
        self.assertIsNone(f.cache_get(r.target.url))

    def test_fetch_iter_many(self):
        f = self.make(lambda req: httpx.Response(404 if "zz" in str(req.url) else 200, json={"u": str(req.url)}))
        res = list(f.fetch_iter(tse.tier1_targets(1)))
        self.assertEqual(len(res), 137)
        self.assertEqual(sum(1 for r in res if r.status == 404), 1)


class EleConfigTests(unittest.TestCase):
    def test_fixture_matches_config(self):
        doc = json.loads((FIX / "ele-c.json").read_text())
        self.assertEqual(tse.compare_elections(doc), [])

    def test_mismatch_warns(self):
        doc = json.loads((FIX / "ele-c.json").read_text())
        for p in doc["pl"]:
            for e in p["e"]:
                if e["cd"] == "6259":
                    e["cd"] = "9999"
        w = tse.compare_elections(doc)
        self.assertTrue(any("6259" in x for x in w))


if __name__ == "__main__":
    unittest.main()
