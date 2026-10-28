"""Shape checks on the committed reports in data/processed/checks/ (missing = FAIL, not skip)."""

import json

from pipeline import deck_pointcloud as dp
from pipeline.checks import grade_separation as gs
from pipeline.checks import vicmap_crosscheck as vc


def _load(path):
    assert path.exists(), f"{path.name} missing; reports are committed (run pipeline.checks.*)"
    return json.loads(path.read_text())


def test_grade_separation_report_is_consistent():
    rep = _load(gs.OUT_JSON)
    s = rep["summary"]
    assert set(s["crossings_by_class"]) == set(gs.CLASSES)
    assert sum(s["crossings_by_class"].values()) == s["crossings_total"] == len(rep["crossings"])
    assert all(r["class"] in gs.CLASSES for r in rep["crossings"])
    assert s["clearance_threshold_m"] == gs.CLEARANCE_M
    n_ends = sum(s["bridge_ends"].values())
    assert n_ends == len(rep["bridge_ends"]) == 2 * s["bridge_ways"]


def test_grade_separation_flags_are_points_for_every_flag():
    rep = _load(gs.OUT_JSON)
    fc = _load(gs.OUT_FLAGS)
    assert fc["type"] == "FeatureCollection"
    expected = (
        sum(1 for r in rep["crossings"] if r["class"] != "separated_ok")
        + sum(1 for e in rep["bridge_ends"] if e["class"] == "dangling_bridge_end")
        + len(rep["mid_structure_junctions"])
    )
    assert len(fc["features"]) == expected
    for f in fc["features"]:
        assert f["geometry"]["type"] == "Point"
        lon, lat = f["geometry"]["coordinates"]
        b = gs.SIM_EXTRACT_BBOX
        assert b.west <= lon <= b.east and b.south <= lat <= b.north, (lon, lat)
        assert f["properties"]["class"] != "separated_ok"
        assert all(not isinstance(v, list | dict) for v in f["properties"].values())


def test_vicmap_flags_match_report():
    rep = _load(vc.OUT_JSON)
    fc = _load(vc.OUT_FLAGS)
    expected = sum(
        1 for key, classes in vc.FLAG_CLASSES.items() for r in rep[key] if r["class"] in classes
    )
    assert len(fc["features"]) == expected
    b = gs.SIM_EXTRACT_BBOX
    for f in fc["features"]:
        assert f["geometry"]["type"] == "Point"
        lon, lat = f["geometry"]["coordinates"]
        assert b.west <= lon <= b.east and b.south <= lat <= b.north, (lon, lat)


def test_grade_separation_has_no_order_mismatch():
    s = _load(gs.OUT_JSON)["summary"]
    assert s["crossings_by_class"]["order_mismatch"] == 0


def test_grade_separation_nothing_pending_after_stage_b():
    """Stage B gives tunnels depth and lowers the CityLink cutting: nothing pending, nothing low."""
    c = _load(gs.OUT_JSON)["summary"]["crossings_by_class"]
    assert c["pending_tunnel_depth"] == c["pending_trench_depth"] == 0
    assert c["separated_low"] == 0 and c["same_layer_no_junction"] == 0


def test_deck_pointcloud_report_is_consistent():
    rep = _load(dp.REPORT_PATH)
    recs = rep["records"]
    assert recs and rep["attribution"] == dp.ATTRIBUTION
    cal = rep["calibration"]
    from pipeline import roads_3d

    assert cal["separation_global_n"] >= cal["min_n"]
    if cal["deck_thickness_global_n"] < cal["min_n"]:  # too few: the constant is used
        assert cal["deck_thickness_global_m"] == roads_3d.DECK_THICKNESS_M
    assert rep["inputs_sha256"] and all(len(v) == 64 for v in rep["inputs_sha256"].values())
    for a in rep["anchors"]:
        assert abs(a["final_z_m"] - a["measured_z_m"]) <= 0.02, a
    for t in rep["pending_trench_depth"]:
        assert t["trench_depth_m"] > 0
    assert dp.MIN_SEP_M <= cal["separation_global_m"] <= dp.MAX_SEP_M
    for k in ("freeway", "other"):
        n, m = cal[f"separation_{k}_n"], cal[f"separation_{k}_m"]
        assert (m is not None) == (n >= cal["min_n"])
    lf = rep["layer_floors"]
    assert set(lf) >= {"full", "reduced", "dropped", "not_floored", "effective_max_m"}
    pr = rep["precedence"]
    assert pr["anchors_moved"] == 0
    assert all(c["bound_m"] < 0 for c in pr["conflicts_below_ground"])
    assert all(b["dz_m"] < b["separation_m"] for b in pr["estimated_crossings_bent"])
    assert len(rep["tiles_read"]) > 0
    for r in recs:
        assert r["kind"] in ("crossing", "open_deck", "deck_profile")
        assert isinstance(r["used"], bool)
        assert (r["reason"] is None) == r["used"]
        if r["kind"] == "crossing" and r["used"]:
            if "trench_depth_m" in r:  # over a cutting: measured against street level
                assert 0 <= r["separation_m"] <= dp.MAX_SEP_M and r["trench_depth_m"] > 0
            else:
                assert dp.MIN_SEP_M <= r["separation_m"] <= dp.MAX_SEP_M
            assert r["deck_points"] >= dp.MIN_POINTS and r["deck_iqr_m"] <= dp.MAX_IQR_M
            assert not r["lower_is_rail"]
    used = sum(1 for r in recs if r["used"])
    s = rep["summary"]
    assert used == sum(v for k, v in s.items() if k.endswith("_used") and "not_used" not in k)
