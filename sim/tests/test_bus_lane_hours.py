"""sim/bus_lane_hours.py: City of Melbourne bus-lane hours, matching, and the baseline network."""

from __future__ import annotations

from datetime import datetime

import pytest
import sumolib

from sim import bus_lane_hours as bh
from sim.config import NET_PATH

TUESDAY = datetime(2026, 10, 27)
SATURDAY = datetime(2026, 10, 31)
TIMES = [
    None,
    TUESDAY.replace(hour=8),
    TUESDAY.replace(hour=12),
    TUESDAY.replace(hour=17),
    TUESDAY.replace(hour=23, minute=30),
    SATURDAY.replace(hour=8),
]


def test_table_is_cited():
    assert bh.SOURCE_URL == "https://www.melbourne.vic.gov.au/bus-lanes"
    assert "not open-licensed" in bh.SOURCE_NOTE
    assert {e.key for e in bh.TABLE if e.hours is None} == {"spring_both"}


def test_lonsdale_south_hours():
    south = bh.BY_KEY["lonsdale_south"]
    assert bh.active(south, TUESDAY.replace(hour=8))
    assert not bh.active(south, TUESDAY.replace(hour=12))
    assert not bh.active(south, TUESDAY.replace(hour=9, minute=30))
    assert not bh.active(south, SATURDAY.replace(hour=8))
    assert not bh.active(south, None)  # the baseline has no clock time
    assert bh.active(bh.BY_KEY["spring_both"], None)


def test_compass():
    assert bh.compass(339) == "north" and bh.compass(69) == "east"
    assert bh.compass(159) == "south" and bh.compass(250) == "west"


@pytest.fixture(scope="module")
def net():
    if not NET_PATH.exists():
        pytest.skip("network not built")
    return sumolib.net.readNet(str(NET_PATH))


def test_baseline_network_has_the_timed_lanes_open(net):
    rows = bh.timed_lane_rows(net)
    assert set(rows) == {e.key for e in bh.TABLE if e.hours}
    assert len(rows["lonsdale_south"]) >= 10 and len(rows["lonsdale_north"]) >= 10
    for ids in rows.values():
        for lid in ids:
            lane = net.getLane(lid)
            assert lane.allows("passenger"), lid
    closed = {(e, i) for e, idx in bh.bus_only_lanes(net).items() for i in idx}
    timed = {
        (lid.rsplit("_", 1)[0], int(lid.rsplit("_", 1)[1])) for v in rows.values() for lid in v
    }
    assert not closed & timed


def test_lonsdale_south_open_at_noon_closed_at_eight(net):
    lid = bh.timed_lane_rows(net)["lonsdale_south"][0]
    edge, idx = lid.rsplit("_", 1)
    assert int(idx) not in bh.bus_only_lanes(net, TUESDAY.replace(hour=12)).get(edge, [])
    assert int(idx) in bh.bus_only_lanes(net, TUESDAY.replace(hour=8)).get(edge, [])
    # the evening peak closes the north side, not the south side
    assert int(idx) not in bh.bus_only_lanes(net, TUESDAY.replace(hour=17)).get(edge, [])


def test_always_on_lanes_are_never_open(net):
    always = {
        (e.getID(), ln.getIndex())
        for e in net.getEdges()
        for ln in e.getLanes()
        if bh.is_bus_only(ln)
    }
    assert len(always) >= 20
    roads = {net.getEdge(e).getName() for e, _ in always}
    assert {"Spring Street", "Victoria Parade", "St Kilda Road"} <= roads
    assert not roads & {"Lonsdale Street", "Queen Street", "City Road"}
    for when in TIMES:
        closed = {(e, i) for e, idx in bh.bus_only_lanes(net, when).items() for i in idx}
        assert always <= closed, when
    assert {(e, i) for e, idx in bh.bus_only_lanes(net).items() for i in idx} == always
