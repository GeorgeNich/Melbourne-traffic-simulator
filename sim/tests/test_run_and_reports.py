"""Smoke-run metric parsing, and the committed check reports in sim/out/checks/."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sim import run
from sim.config import CHECKS_DIR

LOG = """\
Warning: Teleporting vehicle 'rt1938'; waited too long (yield), lane='777282583#2_0', time=1101.00.
Warning: Teleporting vehicle 'rt1032'; waited too long (jam), lane=':1895925155_2_0', time=1168.00.
Warning: Vehicle 'rt1032' ends teleporting on edge '316628748', time=1180.00.
Warning: Teleporting vehicle 'rt7'; waited too long (jam), lane='-316628748_1', time=1177.00.
"""


def test_parse_teleports():
    t = run.parse_teleports(LOG)
    assert [x["edge"] for x in t] == ["777282583#2", ":1895925155_2", "-316628748"]
    assert t[0]["reason"] == "waited too long (yield)"
    assert t[1]["time"] == pytest.approx(1168.0)


def test_lane_to_edge():
    assert run.lane_to_edge("123#4_1") == "123#4"
    assert run.lane_to_edge("-123_0") == "-123"
    assert run.lane_to_edge(":J_3_0") == ":J_3"


def test_parse_statistics(tmp_path: Path):
    p = tmp_path / "stats.xml"
    p.write_text(
        '<statistics><vehicles loaded="600" inserted="600" running="215" waiting="0"/>'
        '<teleports total="2" jam="1" yield="1" wrongLane="0"/>'
        '<vehicleTripStatistics count="385" speed="8.03" duration="370.7" waitingTime="109.3"/>'
        "</statistics>"
    )
    s = run.parse_statistics(p)
    assert s["vehicles"]["inserted"] == 600
    assert s["teleports"]["total"] == 2
    assert s["trips"]["speed"] == pytest.approx(8.03)


def test_parse_edgedata_ranks_by_waiting(tmp_path: Path):
    p = tmp_path / "e.xml"
    p.write_text(
        '<meandata><interval begin="300" end="1200">'
        '<edge id="a" waitingTime="5" speed="3" entered="4"/>'
        '<edge id="b" waitingTime="50" speed="1" entered="9"/>'
        '<edge id="c" speed="10" entered="2"/>'
        "</interval></meandata>"
    )
    rows = run.parse_edgedata(p)
    assert [r["edge"] for r in rows] == ["b", "a"]


def _report(name: str) -> dict:
    path = CHECKS_DIR / name
    if not path.exists():
        pytest.skip(f"{path} not generated")
    return json.loads(path.read_text())


def test_every_restriction_accounted_for():
    r = _report("turn_restrictions.json")
    assert r["relations"] == len(r["rows"])
    assert set(r["by_status"]) <= {"enforced", "not_applied", "not_in_network"}
    for row in r["rows"]:
        if row["status"] == "not_applied":
            assert "conditional" in row["kind"] or "except" in row["detail"]


def test_every_signal_explained():
    r = _report("signals.json")
    assert "unexplained" not in r["by_class"]
    assert r["traffic_lights_without_osm_signal"] == []
    assert sum(r["by_class"].values()) == r["osm_signal_nodes"]
    assert all(p["timing"] == "sumo_default" for p in r["programs"])


def test_connectivity_report_has_no_hard_failures():
    r = _report("connectivity.json")
    g = r["grade_separation"]
    assert g["grade_separated_joined_by_a_junction"] == []
    assert g["untraced_with_level_mismatch"] == []
    assert not [t for t in r["reachability"]["trap_loops"] if t["where"] == "sim_area"]
    cf = r["car_free"]
    if "skipped" not in cf:
        for check in cf["named_checks"][:2]:  # Swanston St, Bourke St Mall
            assert check["car_ways_with_edges"] == []
            assert check["car_edges_along_corridor"] == []
        assert cf["named_checks"][2]["car_free_with_car_edges"] == []
    for trip in r["routing"]:
        assert trip.get("length_km"), trip


def test_ramp_check_applies_restrictions():
    r = _report("connectivity.json")["ramps"]
    assert r["missing_movements"] == []
    assert r["movements_banned_by_osm_restrictions"] > 0
    nodes = [(m["node"], m["from_way"], m["to_way"]) for m in r["missing_movements"]]
    assert len(nodes) == len(set(nodes))


def test_no_private_way_leaks():
    cf = _report("connectivity.json")["car_free"]
    if "skipped" not in cf:
        assert cf["of_which_have_car_edges"] == []


def test_netconvert_warnings_all_classified():
    r = _report("netconvert_warnings.json")
    for key in ("pass1_osm_import", "pass2_final_network"):
        assert "unclassified" not in r[key]
        assert all(v["verdict"] in ("fixed", "justified") for v in r[key].values())
    assert "edge_without_onward_connection" not in r["pass2_final_network"]


def test_vicmap_structures_never_joined():
    r = _report("vicmap_connectivity.json")
    assert r["structures_joined"] == []
    assert r["by_kind_and_status"].get("structure:separated", 0) > 100


def test_baseline_trip_checks_pass_their_gates():
    r = _report("trips_baseline_checks.json")
    a = r["alignment"]
    assert a["share_within_any_carriageway"] >= 0.99  # the gate
    assert a["share_within_own_way"] is not None  # reported, not gated
    assert "share_within_6m" in a and "p99" in a["distance_m"]  # still reported
    assert a["pass"]
    assert r["corner_cutting"]["chords_over_1_5m"] == 0
    assert r["corner_cutting"]["chord_outside_road_surface_m"]["max"] < 1.5
    assert r["heights"]["pass"]
    assert r["car_free"].get("pass", True)
