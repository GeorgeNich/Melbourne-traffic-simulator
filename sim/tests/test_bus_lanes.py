"""sim/bus_lanes.py: placement rules, the netconvert copy, and the committed network."""

from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import sumolib

from sim import bus_lanes
from sim.build_net import SHARED_OPTIONS
from sim.config import NET_PATH


def _plan(**tags) -> bus_lanes.BusLaneWay | None:
    return bus_lanes.plan_way(1, {"highway": "secondary", **tags}, "Test St")


def test_lane_values_kerb_is_leftmost():
    assert bus_lanes.lane_values(2, 1, "left") == "designated|yes"
    assert bus_lanes.lane_values(3, 1, "right") == "yes|yes|designated"


def test_counted_bus_lane_on_one_way_goes_to_the_kerb():
    r = _plan(oneway="yes", lanes="3", **{"lanes:bus": "1"})
    assert r.skipped is None
    assert r.new_tags == {"bus:lanes": "designated|yes|yes"}
    r = _plan(oneway="yes", lanes="2", busway="lane")
    assert r.new_tags == {"bus:lanes": "designated|yes"}
    r = _plan(oneway="yes", lanes="2", **{"busway:right": "lane"})
    assert r.new_tags == {"bus:lanes": "yes|designated"}


def test_guesses_are_skipped():
    assert "lane count unknown" in _plan(oneway="yes", **{"lanes:bus": "1"}).skipped
    assert "close the road" in _plan(oneway="yes", lanes="1", **{"lanes:bus": "1"}).skipped
    assert "which direction" in _plan(lanes="4", **{"lanes:bus": "1"}).skipped
    assert _plan(oneway="yes", lanes="2") is None


def test_explicit_bus_lanes_left_to_netconvert():
    r = _plan(oneway="yes", lanes="4", **{"bus:lanes": "yes|yes|yes|designated"})
    assert r.side == "explicit" and r.bus_lanes == 1 and r.new_tags == {}


def test_two_way_with_direction():
    r = _plan(lanes="4", **{"lanes:forward": "2", "lanes:bus:forward": "1"})
    assert r.skipped is None
    assert r.new_tags == {"bus:lanes:forward": "designated|yes"}


OSM = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
<node id="1" lat="-37.8100" lon="144.9500"/><node id="2" lat="-37.8100" lon="144.9520"/>
<node id="3" lat="-37.8100" lon="144.9540"/><node id="4" lat="-37.8080" lon="144.9520"/>
<node id="5" lat="-37.8120" lon="144.9520"/>
<way id="10"><nd ref="1"/><nd ref="2"/><tag k="highway" v="secondary"/><tag k="oneway" v="yes"/>
<tag k="lanes" v="2"/><tag k="lanes:bus" v="1"/></way>
<way id="11"><nd ref="2"/><nd ref="3"/><tag k="highway" v="secondary"/><tag k="oneway" v="yes"/>
<tag k="lanes" v="2"/></way>
<way id="12"><nd ref="2"/><nd ref="4"/><tag k="highway" v="secondary"/>
<tag k="oneway" v="yes"/></way>
<way id="13"><nd ref="2"/><nd ref="5"/><tag k="highway" v="secondary"/>
<tag k="oneway" v="yes"/></way>
</osm>
"""


def test_netconvert_closes_the_kerb_lane_and_cars_still_turn(tmp_path: Path):
    """Eastbound way 10 (2 lanes, one bus lane) meets a left turn (north, way 12), a straight on
    (way 11) and a right turn (south, way 13) in a left-hand network: lane 0 (kerb) is bus-only
    and cars can still make every movement."""
    from sim.checks.netindex import read_osm

    src = tmp_path / "t.osm"
    src.write_text(OSM)
    dst = tmp_path / "bus.osm"
    assert bus_lanes.write_osm(src, dst, bus_lanes.plan(read_osm(src))) == 1
    tags = {t.get("k"): t.get("v") for t in ET.parse(dst).getroot().find("way").iter("tag")}
    assert tags["bus:lanes"] == "designated|yes" and "lanes:bus" not in tags
    out = tmp_path / "t.net.xml"
    subprocess.run(
        [
            sumolib.checkBinary("netconvert"),
            "--osm-files",
            str(dst),
            "--osm.lane-access",
            *SHARED_OPTIONS,
            "-o",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    net = sumolib.net.readNet(str(out))
    e = net.getEdge("10")
    kerb, other = e.getLanes()
    assert kerb.allows("bus") and not kerb.allows("passenger")
    assert other.allows("passenger")
    car_targets = {
        c.getTo().getID()
        for ln in e.getLanes()
        if ln.allows("passenger")
        for c in ln.getOutgoing()
        if c.getToLane().allows("passenger")
    }
    assert car_targets == {"11", "12", "13"}


@pytest.mark.skipif(not NET_PATH.exists(), reason="network not built")
def test_committed_network_bus_lanes():
    net = sumolib.net.readNet(str(NET_PATH))
    lanes = [
        ln
        for e in net.getEdges()
        for ln in e.getLanes()
        if ln.allows("bus") and not ln.allows("passenger")
    ]
    # Full-time ones only (Spring St, Victoria Pde, St Kilda Rd, ...): the timed ones (Lonsdale,
    # Queen, City Rd) are open to cars in the baseline network (sim/bus_lane_hours.py).
    assert len(lanes) >= 20
    for ln in lanes:
        assert ln.getEdge().allows("passenger"), ln.getID()  # never the edge's only lane
