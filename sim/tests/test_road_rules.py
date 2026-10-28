"""Road-rules audit: TraCI micro-scenarios and the committed report."""

from __future__ import annotations

import json

import pytest

from sim.checks import road_rules
from sim.config import CHECKS_DIR


def test_cross_intersection_gives_way_to_the_right():
    r = road_rules.give_way_to_right_test()
    assert r["junction_type_built"] == "left_before_right"
    assert r["gives_way_to_the_right"], r


def test_keep_left_and_overtake_on_the_right():
    r = road_rules.keep_left_test()
    assert r["lone_car_keeps_left"], r
    assert r["fast_car_overtook_on_the_right"], r
    assert r["fast_car_back_in_left_lane"], r


def test_report_verdicts():
    path = CHECKS_DIR / "road_rules.json"
    if not path.exists():
        pytest.skip("road_rules.json not generated")
    report = json.loads(path.read_text())
    assert "right_before_left" not in report["junction_types"]
    assert not any(r["verdict"] == "fails" for r in report["rules"])
    red = next(r for r in report["rules"] if r["rule"].startswith("No turning"))["evidence"]
    assert red["left_turns_green_while_same_lane_through_red"] == []
    assert red["turns_green_in_every_phase_while_their_approach_has_red"] == []
    t = next(r for r in report["rules"] if r["rule"].startswith("T-intersections"))
    assert t["evidence"]["continuing_road_yields"] == []
    assert t["evidence"]["t_intersections_judged"] >= 100
    x = next(r for r in report["rules"] if r["rule"].startswith("Give way to the right"))
    assert x["evidence"]["cross_intersections"]["not_give_way_to_the_right"] == []
    assert {c["item"].split(" ")[0] for c in report["cannot_be_modelled"]} >= {
        "Hook",
        "Trams",
        "Real",
        "Clearways",
    }
    bus = next(r for r in report["rules"] if r["rule"].startswith("Bus lanes"))["evidence"]
    assert bus["bus_only_lanes"] > 0
    assert bus["edges_without_a_car_lane"] == []
    assert bus["movements_only_buses_can_make"] == []
    school = next(r for r in report["rules"] if "school zones" in r["rule"])["evidence"]
    assert school["applied_in_baseline"] is False
    assert school["ways"], "the extract carries maxspeed:conditional ways"
    speed = next(r for r in report["rules"] if r["rule"].startswith("Speed limits"))["evidence"]
    assert speed["edges_not_matching_osm_or_default"] == []
    assert speed["cbd_area_limit_40"]["edges"] > 0
    eb = next(k for k in report["known_issues"] if k["item"].startswith("Elizabeth St"))
    assert eb["status"] == "fixed", eb
