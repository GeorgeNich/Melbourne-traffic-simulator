"""Timed bus lanes (Phase 2c, qa-reviewer): independent of sim/ code, from lane geometry in the net.

A lane with a `busLaneHours` param is a City of Melbourne timed bus lane reopened to cars in the
baseline: it must allow passenger cars, name a known row, and sit on the stated kerb.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from collections import Counter

import pytest

from tests.contracts.roads_net_helpers import NET_PATH

pytestmark = pytest.mark.skipif(not NET_PATH.exists(), reason="sim/net/roads.net.xml not built yet")

KEYS = {"queen_east", "lonsdale_north", "lonsdale_south", "city_rd_inbound", "spring_both"}


def _pts(shape: str) -> list[tuple[float, float]]:
    return [tuple(map(float, p.split(",")[:2])) for p in shape.split()]


def _timed():
    """(edge name, key, lane, shape, other lane shapes) per timed lane."""
    out = []
    for _, e in ET.iterparse(NET_PATH):
        if e.tag != "edge":
            continue
        lanes = e.findall("lane")
        for ln in lanes:
            for p in ln.findall("param"):
                if p.get("key") == "busLaneHours":
                    others = [_pts(x.get("shape")) for x in lanes if x is not ln]
                    out.append((e.get("name"), p.get("value"), ln, _pts(ln.get("shape")), others))
        e.clear()
    return out


def _allows_car(lane) -> bool:
    if lane.get("allow"):
        return "passenger" in lane.get("allow").split()
    return "passenger" not in lane.get("disallow", "").split()


def test_every_timed_lane_allows_cars_and_names_a_known_row():
    timed = _timed()
    assert len(timed) == 36
    assert {k for _, k, *_ in timed} <= KEYS
    assert not [ln.get("id") for _, _, ln, *_ in timed if not _allows_car(ln)]


def test_bus_only_lanes_are_untimed_and_26():
    only = Counter()
    for _, e in ET.iterparse(NET_PATH):
        if e.tag != "edge" or e.get("function") == "internal":
            continue
        for ln in e.findall("lane"):
            timed = any(p.get("key") == "busLaneHours" for p in ln.findall("param"))
            bus_only = "bus" in ln.get("allow", "").split() and "passenger" not in ln.get(
                "allow", ""
            )
            assert not (timed and bus_only), ln.get("id")
            if bus_only:
                only[e.get("name")] += 1
        e.clear()
    assert sum(only.values()) == 26, only


def test_timed_lanes_sit_on_the_stated_kerb_and_direction():
    """Compass side of the lane relative to its edge's other lanes, and travel direction, from
    shapes (UTM: +x east, +y north)."""
    want = {  # key -> (side, travel direction predicate)
        "queen_east": ("east", None),
        "lonsdale_north": ("north", lambda dx, dy: dx > 0),  # eastbound
        "lonsdale_south": ("south", lambda dx, dy: dx < 0),  # westbound
    }
    seen = Counter()
    for name, key, _, shp, others in _timed():
        if key not in want or not others:
            continue
        side, travel = want[key]
        mx = sum(p[0] for o in others for p in o[:1]) / len(others)
        my = sum(p[1] for o in others for p in o[:1]) / len(others)
        ox, oy = shp[0][0] - mx, shp[0][1] - my
        # edges run along their street; use the lane's own direction for the lateral offset
        dx, dy = shp[-1][0] - shp[0][0], shp[-1][1] - shp[0][1]
        # offset component perpendicular to travel, mapped to compass by its dominant axis
        n = math.hypot(dx, dy) or 1.0
        lat = (ox * -dy + oy * dx) / n  # left-positive; kerb lane is on the LEFT in Melbourne
        assert lat > 0, f"{key} {name}: timed lane not on the kerb (left) side"
        lx, ly = -dy / n, dx / n  # unit left-of-travel vector
        comp = (
            ("east" if lx > 0 else "west")
            if abs(lx) > abs(ly)
            else ("north" if ly > 0 else "south")
        )
        assert comp == side, f"{key} {name}: kerb is on the {comp}, expected {side}"
        if travel:
            assert travel(dx, dy), f"{key} {name}: wrong travel direction ({dx:.0f},{dy:.0f})"
        seen[key] += 1
    assert (
        all(seen[k] >= 2 for k in ("lonsdale_north", "lonsdale_south")) and seen["queen_east"] >= 1
    )
