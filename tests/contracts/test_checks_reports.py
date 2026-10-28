"""Contract test for the Phase 2 road-network verification reports in data/processed/checks/.

Written by qa-reviewer, independently of pipeline.checks (it imports neither module, only the
extract bbox from pipeline/config.py, the single source of truth for bboxes). The reports are
committed, so a missing report FAILS here rather than skipping.

What it checks:
- grade_separation.json / vicmap_crosscheck.json and their *_flags.geojson exist and parse.
- Class counts in each summary sum to the totals and to the record lists.
- Every flag point and every record point lies inside SIM_EXTRACT_BBOX (no tolerance); Stage B:
  a Vicmap `osm_structures` record of a motorway tunnel way (clipped at TUNNEL_EXTRACT_BBOX, the
  Burnley / Domain tunnels) may lie inside TUNNEL_EXTRACT_BBOX instead.
- Every OSM way id named in a report exists in roads_3d.geojson.
- The z values in grade_separation.json are those of the CURRENT roads_3d.geojson at that point
  (recomputed here by interpolation), so a report can't silently describe an older roads_3d.
- Each report records the sha256 of the contract files it checked (`inputs_sha256`, keyed by
  repo-relative path) and they match the current files. If a writer changes roads_3d or the
  extract, the reports must be regenerated.
- Both reports carry their licence/attribution text.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import pytest

from pipeline.config import SIM_EXTRACT_BBOX, TUNNEL_EXTRACT_BBOX, BBox

ROOT = Path(__file__).resolve().parents[2]
CHECKS = ROOT / "data" / "processed" / "checks"
GS_JSON = CHECKS / "grade_separation.json"
GS_FLAGS = CHECKS / "grade_separation_flags.geojson"
VC_JSON = CHECKS / "vicmap_crosscheck.json"
VC_FLAGS = CHECKS / "vicmap_crosscheck_flags.geojson"
ROADS_3D = ROOT / "data" / "processed" / "roads_3d.geojson"
EXTRACT = ROOT / "data" / "processed" / "osm_sim_area.osm.gz"

GS_CLASSES = {
    "separated_ok",
    "separated_low",
    "order_mismatch",
    "same_layer_no_junction",
    "pending_tunnel_depth",
    "pending_trench_depth",  # orchestrator-approved 2026-10-26: deck over a road in a cutting
}
BRIDGE_END_CLASSES = {"connected", "extract_edge", "dangling_bridge_end"}
Z_TOL_M = 0.05


def _load(path: Path) -> dict:
    assert path.exists(), f"{path.relative_to(ROOT)} missing (reports are committed)"
    return json.loads(path.read_text())


@pytest.fixture(scope="module")
def gs() -> dict:
    return _load(GS_JSON)


@pytest.fixture(scope="module")
def vc() -> dict:
    return _load(VC_JSON)


@pytest.fixture(scope="module")
def roads() -> dict[int, list[list[float]]]:
    fc = json.loads(ROADS_3D.read_text())
    return {f["properties"]["way_id"]: f["geometry"]["coordinates"] for f in fc["features"]}


def _inside(lon: float, lat: float, b: BBox = SIM_EXTRACT_BBOX) -> bool:
    return b.west <= lon <= b.east and b.south <= lat <= b.north


@pytest.fixture(scope="module")
def extended_clip_ways() -> set[int]:
    """Ways clipped at TUNNEL_EXTRACT_BBOX (Stage B): `tunnel=yes` motorway / motorway_link."""
    with gzip.open(EXTRACT) as fh:
        root = ET.parse(fh).getroot()
    out = set()
    for el in root.iter("way"):
        tags = {c.get("k"): c.get("v") for c in el if c.tag == "tag"}
        if tags.get("tunnel") == "yes" and tags.get("highway") in ("motorway", "motorway_link"):
            out.add(int(el.get("id")))
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


# --- parse + shape ------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [GS_FLAGS, VC_FLAGS], ids=lambda p: p.name)
def test_flags_are_point_feature_collections_inside_extract(path: Path) -> None:
    fc = _load(path)
    assert fc["type"] == "FeatureCollection"
    assert fc["features"], "a flags file with no flags should still be checked by hand"
    for f in fc["features"]:
        assert f["type"] == "Feature"
        assert f["geometry"]["type"] == "Point"
        lon, lat = f["geometry"]["coordinates"]
        assert _inside(lon, lat), (path.name, lon, lat, f["properties"].get("class"))
        assert "class" in f["properties"]


def test_grade_separation_counts_add_up(gs: dict) -> None:
    s = gs["summary"]
    recs = gs["crossings"]
    assert set(s["crossings_by_class"]) == GS_CLASSES
    assert sum(s["crossings_by_class"].values()) == s["crossings_total"] == len(recs)
    assert Counter(r["class"] for r in recs) == +Counter(s["crossings_by_class"])
    assert sum(s["crossings_by_kind"].values()) == len(recs)
    assert sum(s["order_mismatch_detail"].values()) == s["crossings_by_class"]["order_mismatch"]
    pend = s["pending_tunnel_depth_stage_a_class"]
    assert sum(pend.values()) == s["crossings_by_class"]["pending_tunnel_depth"]
    trench = s.get("pending_trench_depth_stage_a_class", {})
    assert sum(trench.values()) == s["crossings_by_class"]["pending_trench_depth"]
    ex = s["crossings_by_class_excluding_service_service"]
    assert sum(ex.values()) == len(recs) - s["service_service_pairs"]
    ends = gs["bridge_ends"]
    assert set(s["bridge_ends"]) <= BRIDGE_END_CLASSES
    assert sum(s["bridge_ends"].values()) == len(ends) == 2 * s["bridge_ways"]
    assert Counter(e["class"] for e in ends) == Counter(s["bridge_ends"])
    assert s["mid_structure_junctions"] == len(gs["mid_structure_junctions"])


def test_nothing_pending_in_stage_b(gs: dict) -> None:
    """Stage B gave tunnels depth and lowered the CityLink cutting: no crossing is pending."""
    by = gs["summary"]["crossings_by_class"]
    assert by["pending_tunnel_depth"] == 0 and by["pending_trench_depth"] == 0, by
    assert not any(r["class"].startswith("pending_") for r in gs["crossings"])


def test_grade_separation_flags_match_records(gs: dict) -> None:
    fc = _load(GS_FLAGS)
    expected = Counter(r["class"] for r in gs["crossings"] if r["class"] != "separated_ok")
    expected += Counter(
        e["class"] for e in gs["bridge_ends"] if e["class"] == "dangling_bridge_end"
    )
    expected += Counter(m["class"] for m in gs["mid_structure_junctions"])
    assert Counter(f["properties"]["class"] for f in fc["features"]) == expected


def test_grade_separation_records_inside_extract_and_ways_exist(gs: dict, roads: dict) -> None:
    for r in gs["crossings"] + gs["bridge_ends"] + gs["mid_structure_junctions"]:
        assert _inside(r["lon"], r["lat"]), r
        assert r["way_a"] in roads, r["way_a"]
        if "way_b" in r:
            assert r["way_b"] in roads, r["way_b"]


def test_crossing_classes_are_consistent_with_their_own_numbers(gs: dict) -> None:
    thr = gs["summary"]["clearance_threshold_m"]
    for r in gs["crossings"]:
        if r["class"] == "same_layer_no_junction":
            assert r["effective_layer_a"] == r["effective_layer_b"]
            continue
        assert r["effective_layer_a"] != r["effective_layer_b"]
        upper_is_a = r["effective_layer_a"] > r["effective_layer_b"]
        dz = (r["z_a_m"] - r["z_b_m"]) if upper_is_a else (r["z_b_m"] - r["z_a_m"])
        assert r["dz_upper_minus_lower_m"] == pytest.approx(dz, abs=0.011)
        now = r["stage_a_class"]
        if now == "separated_ok":
            assert dz >= thr - 0.011
        elif now == "separated_low":
            assert dz < thr + 0.011
        else:
            assert now == "order_mismatch"
            assert dz < thr


# --- the report describes the CURRENT roads_3d --------------------------------------------------


def _z_at(line: list[list[float]], lon: float, lat: float) -> float:
    """z of a [lon, lat, z] polyline at its point nearest (lon, lat), local metres."""
    kx = 111_320.0 * math.cos(math.radians(lat))
    ky = 111_320.0
    px, py = lon * kx, lat * ky
    best = (math.inf, 0.0)
    for (x0, y0, z0), (x1, y1, z1) in zip(line, line[1:], strict=False):
        ax, ay, bx, by = x0 * kx, y0 * ky, x1 * kx, y1 * ky
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
        d = math.hypot(ax + t * dx - px, ay + t * dy - py)
        if d < best[0]:
            best = (d, z0 + t * (z1 - z0))
    return best[1]


def test_grade_separation_z_matches_current_roads_3d(gs: dict, roads: dict) -> None:
    bad = []
    for r in gs["crossings"]:
        for side in ("a", "b"):
            z = _z_at(roads[r[f"way_{side}"]], r["lon"], r["lat"])
            if abs(z - r[f"z_{side}_m"]) > Z_TOL_M:
                bad.append((r[f"way_{side}"], r[f"z_{side}_m"], round(z, 2)))
    assert not bad, f"{len(bad)} crossing z values differ from roads_3d, e.g. {bad[:5]}"


@pytest.mark.parametrize("report", [GS_JSON, VC_JSON], ids=lambda p: p.name)
def test_report_records_sha_of_the_contract_files_it_checked(report: Path) -> None:
    """QA finding (Phase 2 A verification): the reports did not record their inputs' hashes."""
    rep = _load(report)
    shas = rep.get("inputs_sha256")
    assert isinstance(shas, dict), (
        f"{report.name} has no top-level `inputs_sha256` "
        "{repo-relative path: sha256 hex}; can't tell which roads_3d it describes"
    )
    for path in (ROADS_3D, EXTRACT):
        key = str(path.relative_to(ROOT))
        assert key in shas, f"{report.name}: no sha recorded for {key}"
        assert shas[key] == _sha256(path), f"{report.name} is stale: {key} changed since"


# --- Vicmap cross-check -------------------------------------------------------------------------

VC_SECTIONS = {
    "a_osm_bridges": ("osm_structures", "osm_bridge"),
    "a_osm_tunnels": ("osm_structures", "osm_tunnel"),
    "a_vicmap_bridges": ("vicmap_structures", "vicmap_bridge"),
    "a_vicmap_tunnels": ("vicmap_structures", "vicmap_tunnel"),
}


def test_vicmap_counts_add_up(vc: dict, gs: dict) -> None:
    s = vc["summary"]
    for key, (section, check) in VC_SECTIONS.items():
        got = Counter(r["class"] for r in vc[section] if r["check"] == check)
        assert got == Counter(s[key]), key
    assert Counter(r["class"] for r in vc["crossing_order"]) == Counter(s["b_crossing_order"])
    # one order record per grade-separation crossing
    assert len(vc["crossing_order"]) == gs["summary"]["crossings_total"]
    resolved = [r for r in vc["crossing_order"] if r["roads_3d_agrees_with_vicmap"] is not None]
    agree = Counter(str(r["roads_3d_agrees_with_vicmap"]).lower() for r in resolved)
    assert agree == Counter(s["b_roads_3d_z_agrees_with_vicmap"])
    nodes = Counter(r["class"] for r in vc["vicmap_structure_nodes"])
    assert nodes == Counter(s["c2_vicmap_structure_nodes"])
    # OSM structure counts agree with the grade-separation report
    n_osm_bridges = sum(r["check"] == "osm_bridge" for r in vc["osm_structures"])
    assert n_osm_bridges == gs["summary"]["bridge_ways"]
    n_osm_tunnels = sum(r["check"] == "osm_tunnel" for r in vc["osm_structures"])
    assert n_osm_tunnels == gs["summary"]["tunnel_ways"]


def test_vicmap_records_inside_extract_and_osm_ways_exist(
    vc: dict, roads: dict, extended_clip_ways: set[int]
) -> None:
    for r in vc["osm_structures"]:
        box = TUNNEL_EXTRACT_BBOX if r["way_id"] in extended_clip_ways else SIM_EXTRACT_BBOX
        assert _inside(r["lon"], r["lat"], box), r["way_id"]
        assert r["way_id"] in roads
    for r in vc["crossing_order"]:
        assert _inside(r["lon"], r["lat"])
        assert r["way_a"] in roads and r["way_b"] in roads
    for r in vc["vicmap_structures"]:
        for w in r["osm_ways_matched"]:
            assert w in roads
    for r in vc["vicmap_structure_nodes"]:
        assert _inside(r["lon"], r["lat"]), r["vicmap_node_ufi"]
        for k in ("osm_carried_way", "osm_crossing_way", "osm_junction_node"):
            if k != "osm_junction_node" and r[k] is not None:
                assert r[k] in roads


def test_vicmap_flags_match_records(vc: dict) -> None:
    flagged = {
        "osm_structures": {"no_vicmap_counterpart", "vicmap_not_bridge", "vicmap_not_tunnel"},
        "vicmap_structures": {"no_osm_counterpart", "osm_not_bridge", "osm_not_tunnel"},
        "crossing_order": {"disagree", "vicmap_at_grade_junction", "vicmap_inconsistent"},
        "vicmap_structure_nodes": {"osm_junction_vicmap_grade_separated"},
    }
    expected = sum(1 for k, cls in flagged.items() for r in vc[k] if r["class"] in cls)
    assert len(_load(VC_FLAGS)["features"]) == expected


def test_reports_carry_licence_and_attribution(gs: dict, vc: dict) -> None:
    assert "OpenStreetMap" in gs["licence"] and "ODbL" in gs["licence"]
    assert "OpenStreetMap" in vc["licence"] and "ODbL" in vc["licence"]
    assert "State of Victoria" in vc["licence"]
    assert "Department of Transport and Planning" in vc["licence"]
    assert "CC BY 4.0" in vc["licence"]
