"""Contract test for data/processed/buildings.geojson (CLAUDE.md "Data contracts").

Written by qa-reviewer, independently of pipeline.buildings.validate_feature_collection: it does
not import the pipeline's validator or constants, only the bboxes and the City of Melbourne boundary
path from pipeline/config.py (the single source of truth for bboxes). Skips when the file is absent.

Contract (CLAUDE.md, stacked-tier change 2026-10-27): pieces over the City of Melbourne boundary
union BUILDINGS_BBOX; properties id, height_m, levels, height_source, min_height_m.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from pathlib import Path

import pytest

from pipeline.config import BUILDINGS_AREA_BBOX, BUILDINGS_BBOX

PATH = Path(__file__).resolve().parents[2] / "data" / "processed" / "buildings.geojson"
RAW = Path(__file__).resolve().parents[2] / "data" / "raw"
ALLOWED_SOURCES = {
    "osm_height",
    "osm_levels",
    "com_open_data",
    "com_pointcloud_2018",
    "type_default",
}
EXPECTED_KEYS = {"id", "height_m", "levels", "height_source", "min_height_m"}
AREA_TOLERANCE_DEG = 5e-5  # ~5 m: coordinates are rounded to 6 decimals and clipped sliver-wise
DECIMALS = 6

pytestmark = pytest.mark.skipif(not PATH.exists(), reason="buildings.geojson not built yet")


@pytest.fixture(scope="module")
def fc() -> dict:
    return json.loads(PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def features(fc: dict) -> list[dict]:
    return fc["features"]


def _rings(geom: dict) -> Iterator[list]:
    if geom["type"] == "Polygon":
        yield from geom["coordinates"]
    else:
        for poly in geom["coordinates"]:
            yield from poly


def _positions(geom: dict) -> Iterator[list]:
    for ring in _rings(geom):
        yield from ring


def _signed_area(ring: list) -> float:
    return 0.5 * sum(
        x1 * y2 - x2 * y1 for (x1, y1, *_), (x2, y2, *_) in zip(ring, ring[1:], strict=False)
    )


def test_feature_collection_rfc7946(fc: dict, features: list[dict]) -> None:
    assert fc["type"] == "FeatureCollection"
    assert "crs" not in fc, "RFC 7946 removed the crs member"
    assert isinstance(features, list) and features, "no features"
    for f in features:
        assert f["type"] == "Feature"
        assert set(f) >= {"type", "geometry", "properties"}


def test_geometry_polygon_or_multipolygon(features: list[dict]) -> None:
    bad = [
        f["properties"].get("id")
        for f in features
        if f["geometry"]["type"] not in ("Polygon", "MultiPolygon")
    ]
    assert not bad, f"{len(bad)} non-polygon geometries, e.g. {bad[:5]}"


def test_rings_closed_and_long_enough(features: list[dict]) -> None:
    bad = []
    for f in features:
        for ring in _rings(f["geometry"]):
            if len(ring) < 4 or ring[0] != ring[-1]:
                bad.append(f["properties"]["id"])
                break
    assert not bad, f"{len(bad)} features with open/short rings, e.g. {bad[:5]}"


def test_ring_orientation_rfc7946(features: list[dict]) -> None:
    """Exterior rings counter-clockwise, holes clockwise (RFC 7946 section 3.1.6)."""
    bad = []
    for f in features:
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        for poly in polys:
            if _signed_area(poly[0]) <= 0 or any(_signed_area(h) >= 0 for h in poly[1:]):
                bad.append(f["properties"]["id"])
                break
    assert not bad, f"{len(bad)} features with wrong winding, e.g. {bad[:5]}"


def test_properties_exact(features: list[dict]) -> None:
    bad = [
        (f["properties"].get("id"), sorted(f["properties"]))
        for f in features
        if set(f["properties"]) != EXPECTED_KEYS
    ]
    assert not bad, f"{len(bad)} features with wrong property keys, e.g. {bad[:3]}"


def test_height_m_numeric_positive(features: list[dict]) -> None:
    bad = []
    for f in features:
        h = f["properties"]["height_m"]
        if isinstance(h, bool) or not isinstance(h, int | float) or not math.isfinite(h) or h <= 0:
            bad.append((f["properties"]["id"], h))
    assert not bad, f"{len(bad)} bad height_m, e.g. {bad[:5]}"


def test_min_height_m_valid_and_below_height(features: list[dict]) -> None:
    bad = []
    for f in features:
        p = f["properties"]
        lo, hi = p["min_height_m"], p["height_m"]
        ok = (
            isinstance(lo, int | float)
            and not isinstance(lo, bool)
            and math.isfinite(lo)
            and 0 <= lo < hi
        )
        if not ok:
            bad.append((p["id"], lo, hi))
    assert not bad, f"{len(bad)} pieces with min_height_m not in [0, height_m): {bad[:5]}"
    assert any(f["properties"]["min_height_m"] > 0 for f in features), "no stacked pieces at all"


def test_levels_int_or_null(features: list[dict]) -> None:
    bad = []
    for f in features:
        lv = f["properties"]["levels"]
        if lv is None:
            continue
        if isinstance(lv, bool) or not isinstance(lv, int) or lv <= 0:
            bad.append((f["properties"]["id"], lv))
    assert not bad, f"{len(bad)} levels values not a positive int or null, e.g. {bad[:5]}"


def test_height_source_allowed(features: list[dict]) -> None:
    sources = {f["properties"]["height_source"] for f in features}
    assert "test_fixture" not in sources, "test_fixture must never appear in data/processed/"
    assert sources <= ALLOWED_SOURCES, f"unexpected sources {sources - ALLOWED_SOURCES}"


def test_ids_unique_nonempty_strings(features: list[dict]) -> None:
    ids = [f["properties"]["id"] for f in features]
    assert all(isinstance(i, str) and i for i in ids)
    dupes = {i for i in ids if ids.count(i) > 1} if len(set(ids)) != len(ids) else set()
    assert not dupes, f"duplicate ids: {sorted(dupes)[:10]}"


def test_coordinates_in_bbox_lon_lat(features: list[dict]) -> None:
    b = BUILDINGS_AREA_BBOX
    out = []
    for f in features:
        for pos in _positions(f["geometry"]):
            assert len(pos) == 2, "positions must be [lon, lat] (no altitude)"
            lon, lat = pos
            if not (b.west <= lon <= b.east and b.south <= lat <= b.north):
                out.append((f["properties"]["id"], pos))
                break
    assert not out, (
        f"{len(out)} features outside BUILDINGS_AREA_BBOX (or lat/lon swapped): {out[:3]}"
    )


def test_coordinates_inside_buildings_area_polygon(features: list[dict]) -> None:
    """Inside City of Melbourne boundary UNION BUILDINGS_BBOX (within a few metres)."""
    boundary = RAW / "com_municipal_boundary.geojson"
    if not boundary.exists():
        pytest.skip("data/raw/com_municipal_boundary.geojson not available")
    from shapely.geometry import box, shape

    com = shape(json.loads(boundary.read_text(encoding="utf-8"))["features"][0]["geometry"])
    area = com.union(box(*BUILDINGS_BBOX)).buffer(AREA_TOLERANCE_DEG)
    out = [f["properties"]["id"] for f in features if not area.contains(shape(f["geometry"]))]
    assert not out, f"{len(out)} pieces outside the City of Melbourne boundary + ring: {out[:5]}"


def test_no_piece_is_osm_underground_only(features: list[dict]) -> None:
    """Pieces from OSM must not be underground-only (every `level` < 0, or location=underground)."""
    pbf = RAW / "bbbike_melbourne.osm.pbf"
    if not pbf.exists():
        pytest.skip("OSM extract not available")
    osmium = pytest.importorskip("osmium")
    import re

    wanted = {
        f["properties"]["id"].split("-v")[0] for f in features if f["properties"]["id"][0] in "wr"
    }
    bad = []
    fp = osmium.FileProcessor(str(pbf)).with_filter(osmium.filter.KeyFilter("level", "location"))
    for o in fp:
        if not (o.is_way() or o.is_relation()):
            continue
        key = ("w" if o.is_way() else "r") + str(o.id)
        if key not in wanted:
            continue
        tags = {t.k: t.v for t in o.tags}
        levels = [
            float(x)
            for x in re.findall(r"-?\d+(?:\.\d+)?", tags.get("level", "").replace("--", " -"))
        ]
        if tags.get("location") == "underground" or (levels and max(levels) < 0):
            bad.append((key, tags))
    assert not bad, f"{len(bad)} underground-only OSM pieces drawn, e.g. {bad[:3]}"


def test_lon_lat_order(features: list[dict]) -> None:
    """Melbourne: lon ~ +145, lat ~ -37.8. A swapped pair would put lon negative."""
    for f in features[:: max(1, len(features) // 500)]:
        lon, lat = next(_positions(f["geometry"]))
        assert 140 < lon < 150 and -40 < lat < -35, (f["properties"]["id"], lon, lat)


def test_six_decimal_rounding(features: list[dict]) -> None:
    bad = []
    for f in features:
        for lon, lat in _positions(f["geometry"]):
            if round(lon, DECIMALS) != lon or round(lat, DECIMALS) != lat:
                bad.append((f["properties"]["id"], lon, lat))
                break
    assert not bad, f"{len(bad)} features with more than {DECIMALS} decimals, e.g. {bad[:3]}"


def test_raw_text_has_at_most_six_decimals() -> None:
    """Check the serialised text too, so float repr noise (e.g. 144.96000000000001) can't hide."""
    import re

    text = PATH.read_text(encoding="utf-8")
    long = re.findall(r"-?\d+\.\d{7,}", text)
    assert not long, f"{len(long)} numbers with >6 decimals in the file, e.g. {long[:5]}"
