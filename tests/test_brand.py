import tomllib
import unittest
from datetime import datetime
from pathlib import Path

from eleicoes.views import brand

ROOT = Path(__file__).resolve().parents[1]


class BrandTests(unittest.TestCase):
    def test_led_states(self):
        now = datetime(2026, 10, 4, 19, 0, 0)
        self.assertIn("Coletor parado", brand.led_html(None, None, now))
        ok = brand.led_html("2026-10-04T18:59:40", "2026-10-04T18:58:01", now)
        self.assertIn('class="led ok"', ok)
        self.assertIn("há 20 s", ok)
        self.assertIn("TSE 18:58:01", ok)
        self.assertIn('class="led atraso"', brand.led_html("2026-10-04T18:50:00", None, now))

    def test_header_escapes_and_clamps(self):
        h = brand.header_html("Presidente · 1º turno", "<b>RS</b>", pct=140.0, encerrada=True)
        self.assertIn("&lt;b&gt;RS&lt;/b&gt;", h)
        self.assertIn("width:100.00%", h)
        self.assertIn("apuração encerrada", h)
        self.assertNotIn('class="apur"', brand.header_html("x", "y", pct=None))

    def test_sequential_ramp(self):
        r = brand.sequential()
        self.assertEqual(r[0][0], 0.0)
        self.assertEqual(r[-1][0], 1.0)
        self.assertEqual(r[0][1], brand._RAMPA["light"][0])

    def test_theme_config_matches_tokens(self):
        cfg = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text())
        th = cfg["theme"]
        for m in ("light", "dark"):
            self.assertEqual(th[m]["backgroundColor"], brand.TOKENS[m]["tela"])
            self.assertEqual(th[m]["textColor"], brand.TOKENS[m]["tinta"])
            self.assertEqual(th[m]["primaryColor"], brand.TOKENS[m]["confirma"])
        for face in th["fontFaces"]:
            self.assertTrue((ROOT / face["url"].removeprefix("app/")).exists(), face["url"])
        self.assertTrue(cfg["server"]["enableStaticServing"])


if __name__ == "__main__":
    unittest.main()
