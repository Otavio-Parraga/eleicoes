import unittest

from eleicoes import geo


def _signed_area(ring):
    return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:])) / 2


def _polygons(gj):
    for f in gj["features"]:
        g = f["geometry"]
        yield from ([g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"])


class WindingTests(unittest.TestCase):
    """plotly.graph_objects.Choropleth usa d3-geo: anel externo horário, buracos anti-horários.
    Com a ordem RFC 7946 (anti-horária) o d3 pinta o complemento e o mapa vira um bloco de uma cor só."""

    def assert_d3_winding(self, gj):
        for poly in _polygons(gj):
            self.assertLess(_signed_area(poly[0]), 0, "anel externo deve ser horário")
            for hole in poly[1:]:
                self.assertGreater(_signed_area(hole), 0, "buraco deve ser anti-horário")

    def test_states(self):
        self.assert_d3_winding(geo.states_geojson())

    def test_municipios(self):
        self.assert_d3_winding(geo.municipios_geojson("rs"))

    def test_rewind_is_idempotent(self):
        gj = {"features": [{"geometry": {"type": "Polygon", "coordinates": [
            [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}}]}  # anti-horário
        geo.rewind_for_d3(gj)
        geo.rewind_for_d3(gj)
        self.assert_d3_winding(gj)


if __name__ == "__main__":
    unittest.main()
