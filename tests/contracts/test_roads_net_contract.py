"""Contract test for sim/net/roads.net.xml (CLAUDE.md "Data contracts"), written by qa-reviewer.

The contract: a SUMO network built by netconvert from data/processed/osm_sim_area.osm.gz with
--output.original-names (edges record their OSM way ids) and --lefthand; cars only (Phase 2
decision). Parsed independently of sim/ code (tests/contracts/roads_net_helpers.py). Skips when the
network has not been built yet.
"""

from __future__ import annotations

import math

import pytest

from pipeline.config import SIM_BBOX, SIM_EXTRACT_BBOX, TUNNEL_EXTRACT_BBOX
from tests.contracts.roads_net_helpers import NET_PATH, OSM_PATH, load_net, load_osm

pytestmark = pytest.mark.skipif(not NET_PATH.exists(), reason="sim/net/roads.net.xml not built yet")

TOL_DEG = 0.0002  # ~20 m: junction shapes and corner smoothing reach slightly past the OSM nodes


def _allows_passenger(lane: dict[str, str]) -> bool:
    if "allow" in lane:
        return "passenger" in lane["allow"].split() or lane["allow"] == "all"
    if "disallow" in lane:
        return "passenger" not in lane["disallow"].split()
    return True


def test_network_is_left_hand():
    net = load_net()
    assert net.root_attrs.get("lefthand") == "true"


def test_built_with_original_names_and_no_absolute_paths():
    net = load_net()
    assert '<output.original-names value="true"/>' in net.header
    assert "/Users/" not in net.header and "/home/" not in net.header


def test_projection_is_utm_wgs84():
    loc = load_net().location
    assert "+proj=utm" in loc["projParameter"] and "+datum=WGS84" in loc["projParameter"]


def test_has_a_plausible_size():
    net = load_net()
    assert len(net.edges) > 1000
    assert len(net.junctions) > 500


def test_every_edge_records_its_osm_ways():
    net = load_net()
    missing = [e.id for e in net.edges.values() if not e.ways]
    assert not missing, f"{len(missing)} edges without an origId: {missing[:10]}"


@pytest.mark.skipif(not OSM_PATH.exists(), reason="osm_sim_area.osm.gz not built yet")
def test_every_osm_way_is_a_highway_in_the_extract():
    net, osm = load_net(), load_osm()
    bad = sorted(
        {
            (e.id, w)
            for e in net.edges.values()
            for w in e.ways
            if "highway" not in osm.ways.get(w, ([], {}))[1]
        }
    )
    assert not bad, f"edges whose OSM way is not a highway in the extract: {bad[:10]}"


# OSM tags that mark a bus lane (approved Phase 2c: bus lanes are closed to cars, sim/bus_lanes.py).
BUS_TAG_PREFIXES = ("bus:lanes", "psv:lanes", "lanes:bus", "lanes:psv", "busway")


def _has_bus_tags(tags: dict[str, str]) -> bool:
    return any(k.startswith(BUS_TAG_PREFIXES) for k in tags)


def _is_bus_only(lane: dict[str, str]) -> bool:
    allow = lane.get("allow", "").split()
    return "bus" in allow and "passenger" not in allow


@pytest.mark.skipif(not OSM_PATH.exists(), reason="osm_sim_area.osm.gz not built yet")
def test_every_lane_allows_passenger_cars():
    """Every lane is open to cars, except bus-only lanes (approved Phase 2c contract change):
    a bus-only lane must come from an OSM bus tag on one of its edge's ways, and its edge must
    keep at least one car lane."""
    net, osm = load_net(), load_osm()
    bad, untagged, no_car_lane, bus_only = [], [], [], 0
    for e in net.edges.values():
        for ln in e.lanes:
            if _allows_passenger(ln):
                continue
            if not _is_bus_only(ln):
                bad.append(ln["id"])
                continue
            bus_only += 1
            if not any(_has_bus_tags(osm.ways.get(w, ([], {}))[1]) for w in e.ways):
                untagged.append((ln["id"], e.ways))
            if not any(_allows_passenger(x) for x in e.lanes):
                no_car_lane.append(e.id)
    assert not bad, f"{len(bad)} lanes cars may not use that are not bus lanes: {bad[:10]}"
    assert not untagged, f"bus-only lanes without an OSM bus tag on their way: {untagged[:10]}"
    assert not no_car_lane, f"edges with no lane left for cars: {no_car_lane[:10]}"
    assert bus_only < 0.05 * sum(len(e.lanes) for e in net.edges.values())


def _xy(point: str) -> tuple[float, float]:
    x, y = point.split(",")[:2]
    return float(x), float(y)


def _left_to_right(e) -> list[int]:
    """Lane indices of an edge ordered left to right in its direction of travel, from the lane
    shapes themselves (independent of SUMO's left-hand lane index convention)."""
    ref = e.lanes[0]["shape"].split()
    (x0, y0), (x1, y1) = _xy(ref[0]), _xy(ref[1])  # first segment: edges can curve
    norm = math.hypot(x1 - x0, y1 - y0) or 1.0
    lx, ly = -(y1 - y0) / norm, (x1 - x0) / norm  # unit vector pointing left of travel
    offs = []
    for i, ln in enumerate(e.lanes):
        sx, sy = _xy(ln["shape"].split()[0])
        offs.append(((sx - x0) * lx + (sy - y0) * ly, i))
    return [i for _, i in sorted(offs, reverse=True)]


@pytest.mark.skipif(not OSM_PATH.exists(), reason="osm_sim_area.osm.gz not built yet")
def test_bus_lanes_sit_where_osm_puts_them():
    """Where OSM gives each lane (`bus:lanes=yes|yes|yes|designated`, left to right) the bus-only
    lanes are exactly those lanes (the last one is the RIGHT, median-side lane in Melbourne); where
    OSM only gives a count (`lanes:bus=1`) or `busway=lane` on a one-way road, the bus lane is the
    kerb (leftmost) lane. Checked on edges with one OSM way whose lane count matches the tag."""
    net, osm = load_net(), load_osm()
    checked_explicit = checked_kerb = 0
    wrong = []
    for e in net.edges.values():
        if len(e.ways) != 1 or len(e.lanes) < 2:
            continue
        tags = osm.ways.get(e.ways[0], ([], {}))[1]
        if not _has_bus_tags(tags) or tags.get("oneway") not in ("yes", "true", "1"):
            continue
        order = _left_to_right(e)
        actual = [_is_bus_only(e.lanes[i]) for i in order]
        explicit = tags.get("bus:lanes") or tags.get("psv:lanes")
        if explicit:
            want = [v.strip() in ("designated", "only") for v in explicit.split("|")]
            if len(want) != len(e.lanes):
                continue
            checked_explicit += 1
        else:
            if not any(actual):
                continue  # skipped by the sim (e.g. no lanes tag); test_every_lane covers the rest
            n_bus = sum(actual)
            want = [True] * n_bus + [False] * (len(e.lanes) - n_bus)
            if tags.get("busway:right") == "lane":
                want = want[::-1]
            checked_kerb += 1
        if actual != want:
            wrong.append((e.id, e.ways[0], explicit, actual, want))
    assert not wrong, f"bus lanes on the wrong lane (edge, way, tag, got L->R, want): {wrong[:10]}"
    assert checked_explicit >= 1, "no explicit bus:lanes edge checked (Power Street expected)"
    assert checked_kerb >= 10


def test_edges_and_connections_reference_existing_objects():
    net = load_net()
    for e in net.edges.values():
        assert e.frm in net.junctions and e.to in net.junctions, e.id
        assert e.frm != e.to or len(e.shape) > 2, f"degenerate self-loop {e.id}"
        for t in e.out:
            assert t in net.edges, f"{e.id} connects to unknown edge {t}"


def test_every_traffic_light_junction_has_a_program():
    net = load_net()
    tls = [j for j, a in net.junctions.items() if a.get("type") == "traffic_light"]
    assert tls, "no traffic lights at all"
    unknown = sorted(set(net.tl_junctions) - net.tl_ids)
    assert not unknown, f"connections controlled by a missing tlLogic: {unknown[:10]}"
    idle = sorted(net.tl_ids - set(net.tl_junctions))
    assert not idle, f"tlLogics that control no connection: {idle[:10]}"
    controlled = set().union(*net.tl_junctions.values())
    missing = [j for j in tls if j not in controlled]
    assert not missing, f"traffic-light junctions without a signal program: {missing[:10]}"


def _inside(b, lon: float, lat: float) -> bool:
    return (
        b.west - TOL_DEG <= lon <= b.east + TOL_DEG
        and b.south - TOL_DEG <= lat <= b.north + TOL_DEG
    )


def _is_tunnel_motorway(osm, way: int) -> bool:
    tags = osm.ways.get(way, ([], {}))[1]
    return tags.get("tunnel") == "yes" and tags.get("highway") in ("motorway", "motorway_link")


def test_original_boundary_is_inside_the_extract_bbox():
    """SIM_EXTRACT_BBOX, except that the CityLink motorway tunnel ends may reach
    TUNNEL_EXTRACT_BBOX (approved Phase 2c); which features reach it is checked per junction."""
    west, south, east, north = (float(v) for v in load_net().location["origBoundary"].split(","))
    b, t = SIM_EXTRACT_BBOX, TUNNEL_EXTRACT_BBOX
    assert b.west - TOL_DEG <= west and east <= t.east + TOL_DEG
    assert b.south - TOL_DEG <= south and north <= b.north + TOL_DEG
    # and it actually covers the simulated area
    assert west <= SIM_BBOX.west and east >= SIM_BBOX.east
    assert south <= SIM_BBOX.south and north >= SIM_BBOX.north


@pytest.mark.skipif(not OSM_PATH.exists(), reason="osm_sim_area.osm.gz not built yet")
def test_junctions_are_inside_the_extract_bbox():
    """Junctions and edge shapes lie in SIM_EXTRACT_BBOX; the only exception is a CityLink
    motorway tunnel end (every edge at the junction is on a `tunnel=yes` motorway way), which may
    reach TUNNEL_EXTRACT_BBOX."""
    net, osm = load_net(), load_osm()
    b, t = SIM_EXTRACT_BBOX, TUNNEL_EXTRACT_BBOX
    inc, outg = net.incoming, net.outgoing
    out, tunnel_ends = [], []
    for jid, a in net.junctions.items():
        lon, lat = net.lonlat(float(a["x"]), float(a["y"]))
        if _inside(b, lon, lat):
            continue
        edges = inc.get(jid, []) + outg.get(jid, [])
        tunnel = bool(edges) and all(
            e.ways and all(_is_tunnel_motorway(osm, w) for w in e.ways) for e in edges
        )
        if tunnel and _inside(t, lon, lat):
            tunnel_ends.append(jid)
        else:
            out.append((jid, round(lon, 5), round(lat, 5)))
    assert not out, f"junctions outside the extract bbox (not a tunnel end): {out[:10]}"
    assert len(tunnel_ends) <= 4, (
        f"more tunnel ends outside the extract than expected: {tunnel_ends}"
    )
    shapes = []
    for e in net.edges.values():
        if all(_inside(b, *net.lonlat(x, y)) for x, y in e.shape):
            continue
        tunnel = e.ways and all(_is_tunnel_motorway(osm, w) for w in e.ways)
        if not (tunnel and all(_inside(t, *net.lonlat(x, y)) for x, y in e.shape)):
            shapes.append(e.id)
    assert not shapes, f"edges reaching outside the extract bbox (not a tunnel): {shapes[:10]}"
