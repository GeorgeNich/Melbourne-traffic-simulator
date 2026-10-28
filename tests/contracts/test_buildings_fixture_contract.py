"""Contract checks for the Phase 1 buildings test fixture, and the rule that it never leaks.

- tests/fixtures/buildings_fixture.geojson matches the buildings.geojson contract in CLAUDE.md
  exactly (properties id, height_m, levels, height_source), is labelled as test data, sits inside
  SIM_BBOX and contains plain axis-aligned rectangles only.
- `height_source: "test_fixture"` / `_test_fixture` never appear in data/processed/buildings.geojson
  (skipped until data-engineer's file exists).
"""

import json
from pathlib import Path

import pytest

from pipeline.config import BUILDINGS_BBOX, SIM_BBOX

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "buildings_fixture.geojson"
PROCESSED = ROOT / "data" / "processed" / "buildings.geojson"

CONTRACT_PROPS = {"id", "height_m", "levels", "height_source", "min_height_m"}


@pytest.fixture(scope="module")
def fixture_fc() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_is_labelled_as_test_data(fixture_fc: dict):
    assert fixture_fc["type"] == "FeatureCollection"
    assert fixture_fc.get("_test_fixture") is True
    desc = fixture_fc.get("description", "")
    assert "TEST FIXTURE" in desc
    assert "NOT real buildings" in desc
    assert set(fixture_fc) == {"type", "_test_fixture", "description", "features"}


def test_fixture_properties_match_contract_exactly(fixture_fc: dict):
    feats = fixture_fc["features"]
    assert 1 <= len(feats) <= 10
    ids = [f["properties"]["id"] for f in feats]
    assert len(set(ids)) == len(ids)
    for f in feats:
        assert f["type"] == "Feature"
        p = f["properties"]
        assert set(p) == CONTRACT_PROPS
        assert p["height_source"] == "test_fixture"
        assert isinstance(p["height_m"], int | float) and p["height_m"] > 0
        assert p["levels"] is None or (isinstance(p["levels"], int) and p["levels"] >= 0)
        assert isinstance(p["id"], str) and p["id"].startswith("fixture-")


def test_fixture_is_plain_rectangles_inside_sim_bbox(fixture_fc: dict):
    for f in fixture_fc["features"]:
        geom = f["geometry"]
        assert geom["type"] == "Polygon"
        assert len(geom["coordinates"]) == 1, "no holes"
        ring = geom["coordinates"][0]
        assert len(ring) == 5 and ring[0] == ring[-1], "4 corners + closing point"
        for lon, lat in ring:  # WGS84 [lon, lat]; exactly two values
            assert SIM_BBOX.west <= lon <= SIM_BBOX.east
            assert SIM_BBOX.south <= lat <= SIM_BBOX.north
            assert BUILDINGS_BBOX.west <= lon <= BUILDINGS_BBOX.east
        corners = ring[:4]
        assert len({c[0] for c in corners}) == 2 and len({c[1] for c in corners}) == 2
        # Consecutive corners share either lon or lat: axis-aligned edges, not a bow-tie.
        for a, b in zip(ring, ring[1:], strict=False):
            assert (a[0] == b[0]) != (a[1] == b[1])


@pytest.mark.skipif(not PROCESSED.exists(), reason="data/processed/buildings.geojson not merged")
def test_processed_buildings_contain_no_test_fixture_data():
    text = PROCESSED.read_text(encoding="utf-8")
    assert '"_test_fixture"' not in text
    assert '"test_fixture"' not in text
