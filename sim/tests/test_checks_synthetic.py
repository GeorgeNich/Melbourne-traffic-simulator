"""Network checks on a tiny hand-made OSM network (synthetic test data, not Melbourne roads).

Layout (lon/lat near 144.95, -37.81; ids are arbitrary):

    E (north link)   B1
    1201 ---------- 1004
     |               |
     C (bridge,      |   P (pedestrian, crosses A at shared node 1006)
     layer 1)        |    |
    1001 -- A1 ---- 1002 --- A2 --- 1006 --- A3 --- 1003
     |               |                |
     |              B2
    1202 ---------- 1005
    F (south link)

C crosses A1 between 1001 and 1002 with no shared node and is a bridge: a grade-separated
crossing that must never become a junction. P is a footway: no car edge. Relation 9001 forbids the
left turn from A1 (eastbound) into B1 (northbound) at 1002 (left-hand traffic: left = north).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sumolib

from sim import build_net
from sim.checks import connectivity, restrictions
from sim.checks.movements import Movements, classify_turn
from sim.checks.netindex import NetIndex, read_osm

NODES = {
    1001: (144.9500, -37.8100),
    1002: (144.9520, -37.8100),
    1006: (144.9530, -37.8100),
    1003: (144.9540, -37.8100),
    1004: (144.9520, -37.8080),
    1005: (144.9520, -37.8120),
    1201: (144.9510, -37.8080),
    1202: (144.9510, -37.8120),
    1301: (144.9530, -37.8085),
    1302: (144.9530, -37.8115),
}
WAYS = {
    2001: ([1001, 1002], {"highway": "tertiary", "name": "A1"}),
    2002: ([1002, 1006], {"highway": "tertiary", "name": "A2"}),
    2003: ([1006, 1003], {"highway": "tertiary", "name": "A3"}),
    2004: ([1002, 1004], {"highway": "tertiary", "name": "B1"}),
    2005: ([1002, 1005], {"highway": "tertiary", "name": "B2"}),
    2006: (
        [1201, 1202],
        {"highway": "tertiary", "name": "C", "bridge": "yes", "layer": "1"},
    ),
    2007: ([1201, 1004], {"highway": "tertiary", "name": "E"}),
    2008: ([1202, 1005], {"highway": "tertiary", "name": "F"}),
    2009: ([1301, 1006, 1302], {"highway": "footway", "name": "P"}),
    2010: ([1001, 1202], {"highway": "tertiary", "name": "G"}),
}
RESTRICTION = (9001, [("way", 2001, "from"), ("node", 1002, "via"), ("way", 2004, "to")])


def write_osm(path: Path) -> None:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6">']
    for nid, (lon, lat) in NODES.items():
        lines.append(f'<node id="{nid}" version="1" lat="{lat}" lon="{lon}"/>')
    for wid, (refs, tags) in WAYS.items():
        lines.append(f'<way id="{wid}" version="1">')
        lines += [f'<nd ref="{r}"/>' for r in refs]
        lines += [f'<tag k="{k}" v="{v}"/>' for k, v in tags.items()]
        lines.append("</way>")
    rid, members = RESTRICTION
    lines.append(f'<relation id="{rid}" version="1">')
    lines += [f'<member type="{t}" ref="{r}" role="{role}"/>' for t, r, role in members]
    lines += ['<tag k="type" v="restriction"/>', '<tag k="restriction" v="no_left_turn"/>']
    lines += ["</relation>", "</osm>"]
    path.write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("synthetic")
    osm_path = tmp / "synthetic.osm.xml"
    write_osm(osm_path)
    net_path = tmp / "synthetic.net.xml"
    build_net.run(
        build_net.osm_pass_command(osm_path, net_path, tmp / "joined.xml", tmp / "nc.log")
    )
    osm = read_osm(osm_path)
    net = sumolib.net.readNet(str(net_path))
    idx = NetIndex(net, [])
    return osm_path, net_path, osm, net, idx, Movements(osm, idx)


def test_footway_has_no_edge(synthetic):
    *_, idx, _ = synthetic
    assert 2009 not in idx.edges_by_way
    assert 2001 in idx.edges_by_way


def test_car_free_check_on_raw_ways(synthetic):
    _, _, osm, net, idx, _ = synthetic
    raw = {
        wid: ([osm.nodes[n] for n in refs], dict(tags, highway=tags["highway"]))
        for wid, (refs, tags) in osm.ways.items()
    }
    report = connectivity.car_free(net, idx, raw)
    assert report["ways_cars_may_not_use"] == 1
    assert report["of_which_have_car_edges"] == []


def test_grade_separated_crossing_not_joined(synthetic):
    _, _, osm, net, idx, mv = synthetic
    report = connectivity.grade_separation(net, idx, osm, mv)
    assert report["grade_separated_crossings"] == 1
    assert report["grade_separated_joined_by_a_junction"] == []
    assert report["untraced_with_level_mismatch"] == []
    # no edge of the bridge C and of A1 meet at a junction
    a_nodes = {n for e in idx.edges_by_way[2001] for n in (e.getFromNode(), e.getToNode())}
    c_nodes = {n for e in idx.edges_by_way[2006] for n in (e.getFromNode(), e.getToNode())}
    assert not ({n.getID() for n in a_nodes} & {n.getID() for n in c_nodes} - {"1001"})


def test_level_reads_implied_layers():
    assert connectivity._level({"bridge": "yes"}) == (1, "bridge")
    assert connectivity._level({"tunnel": "yes", "layer": "-2"}) == (-2, "tunnel")
    assert connectivity._level({}) == (0, "")


def test_restriction_enforced_and_traced(synthetic):
    osm_path, net_path, *_ = synthetic
    rows = restrictions.audit(net_path, osm_path, Path("/nonexistent"))
    assert len(rows) == 1
    assert rows[0].status == "enforced", rows[0].detail


def test_restriction_detects_violation(synthetic, tmp_path):
    """Re-adding the forbidden connection must make the audit report it."""
    osm_path, net_path, _, net, idx, _ = synthetic
    f = next(e for e in idx.edges_by_way[2001] if e.getToNode().getID() == "1002")
    t = next(e for e in idx.edges_by_way[2004] if e.getFromNode().getID() == "1002")
    con = tmp_path / "add.con.xml"
    con.write_text(
        f'<connections><connection from="{f.getID()}" to="{t.getID()}" '
        'fromLane="0" toLane="0"/></connections>'
    )
    bad = tmp_path / "bad.net.xml"
    build_net.run(
        [
            "netconvert",
            "--sumo-net-file",
            str(net_path),
            "--connection-files",
            str(con),
            "--lefthand",
            "-o",
            str(bad),
        ]
    )
    rows = restrictions.audit(bad, osm_path, Path("/nonexistent"))
    assert rows[0].status == "violated"
    assert [f.getID(), t.getID()] in rows[0].offending


def test_reachability_on_synthetic(synthetic):
    _, _, osm, net, idx, _ = synthetic
    r = connectivity.reachability(net, idx, osm)
    assert r["largest_scc_share"] > 0.5
    assert r["trap_loops"] == []


def test_turn_classification():
    assert "straight_on" in classify_turn(5)
    assert "right_turn" in classify_turn(90)
    assert "left_turn" in classify_turn(-90)
    assert "u_turn" in classify_turn(175)


def test_conditional_decisions_cover_the_extract():
    from sim.config import OSM_EXTRACT

    if not OSM_EXTRACT.exists():
        pytest.skip("extract not present")
    osm = read_osm(OSM_EXTRACT)
    conds = {
        t["restriction:conditional"]
        for _, t in osm.relations.values()
        if "restriction:conditional" in t
    }
    assert conds <= set(restrictions.CONDITIONAL_DECISIONS)
