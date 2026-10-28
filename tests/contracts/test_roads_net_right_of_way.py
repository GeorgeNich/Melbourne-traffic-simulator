"""Right of way at unsignalised junctions in sim/net/roads.net.xml (qa-reviewer, not sim/ code).

Australian Road Rules (Road Safety Road Rules 2017 (Vic)):
- r.72: at an intersection without signals or signs, give way to a vehicle on your RIGHT. In a
  left-hand SUMO network that is junction type `left_before_right`; SUMO's `right_before_left`
  makes cars give way to the left (qa re-ran a TraCI micro-test: northbound and westbound cars
  meeting at an equal cross, left-hand network: with right_before_left the northbound car went
  first, with left_before_right the westbound car, on the northbound car's right, went first).
- r.69: at a T-intersection without signals or signs, a driver on the terminating road gives way
  to vehicles on the continuing road. So the continuing road's straight-on movements must not
  yield (SUMO link state `m` minor / `s` stop) at an unsignalised T-intersection.

The T-intersection test reads the network with sumolib (SUMO's own reader, not sim/ code) and the
OSM extract with gzip + ElementTree. A junction counts as a T-intersection when exactly three
roads meet (by neighbouring junction) and two of them leave it within THROUGH_DEG of a straight
line. It is only judged when:
- the junction is unsignalised (priority, priority_stop, left_before_right, right_before_left);
- every road there is a minor street (residential, unclassified, living_street, tertiary), so no
  arterial hierarchy or slip-lane merge (*_link) could make the bend the major road;
- no OSM highway=stop / give_way node lies within SIGN_NEAR_M (a sign can reverse the rule);
- no road there carries an OSM `junction=roundabout` way: at a roundabout entries give way to the
  ring (ARR r.114 overrides r.69), checked by the roundabout test below instead.
netconvert breaks ties between equal-class roads by speed, so a side street at the 50 km/h
default meeting a street signed 40 km/h wins priority unless the build sets it explicitly.

The cross-intersection test applies r.72 the same way: an unsignalised junction of four roads of
one minor class, with no sign nearby, must be `left_before_right` (give way to the right), not a
`priority` junction where netconvert picked one street as major (it does so whenever the roads
are at or above its 49 km/h right-before-left threshold, i.e. every 50 km/h street).

The roundabout test (ARR r.114) covers the junctions on OSM `junction=roundabout` ways: a link
that continues around the ring never yields, and an entry onto the ring yields wherever a
circulating link reaches the same lane.
"""

from __future__ import annotations

import gzip
import math
import xml.etree.ElementTree as ET
from functools import cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NET_PATH = ROOT / "sim" / "net" / "roads.net.xml"
OSM_PATH = ROOT / "data" / "processed" / "osm_sim_area.osm.gz"

UNSIGNALISED = {"priority", "priority_stop", "left_before_right", "right_before_left"}
MINOR = {
    "highway.residential",
    "highway.unclassified",
    "highway.living_street",
    "highway.tertiary",
}
THROUGH_DEG = 160.0  # the two continuing arms are within 20 degrees of straight
SIGN_NEAR_M = 30.0

pytestmark = pytest.mark.skipif(
    not (NET_PATH.exists() and OSM_PATH.exists()), reason="network or OSM extract not built yet"
)


@cache
def _net():
    import sumolib

    return sumolib.net.readNet(str(NET_PATH))


@cache
def _sign_xy() -> list[tuple[float, float]]:
    net = _net()
    out = []
    with gzip.open(OSM_PATH) as f:
        for _, el in ET.iterparse(f):
            if el.tag == "node":
                for t in el.findall("tag"):
                    if t.get("k") == "highway" and t.get("v") in ("stop", "give_way"):
                        out.append(net.convertLonLat2XY(float(el.get("lon")), float(el.get("lat"))))
            if el.tag in ("node", "way", "relation"):
                el.clear()
    return out


@cache
def _roundabout_ways() -> frozenset[int]:
    out = set()
    with gzip.open(OSM_PATH) as f:
        for _, el in ET.iterparse(f):
            if el.tag == "way" and any(
                t.get("k") == "junction" and t.get("v") in ("roundabout", "circular")
                for t in el.findall("tag")
            ):
                out.add(int(el.get("id")))
            if el.tag in ("node", "way", "relation"):
                el.clear()
    return frozenset(out)


def _ways(edge) -> set[int]:
    return {int(w) for ln in edge.getLanes() for w in ln.getParam("origId", "").split() if w}


def _on_ring(edge) -> bool:
    return bool(_ways(edge) & _roundabout_ways())


def _arms(node) -> dict[str, list]:
    """Neighbouring junction -> [(is_outgoing, edge)]: one entry per road meeting here."""
    arms: dict[str, list] = {}
    for e in node.getOutgoing():
        arms.setdefault(e.getToNode().getID(), []).append((True, e))
    for e in node.getIncoming():
        arms.setdefault(e.getFromNode().getID(), []).append((False, e))
    return arms


def _heading(outgoing: bool, edge) -> float:
    """Direction from the junction outwards along the road (degrees)."""
    s = edge.getShape()
    a, b = (s[0], s[1]) if outgoing else (s[-1], s[-2])
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))


def test_no_right_before_left_junctions_in_a_left_hand_network():
    net = _net()
    with NET_PATH.open(encoding="utf-8") as f:
        head = f.read(20_000)
    assert 'lefthand="true"' in head
    wrong = [n.getID() for n in net.getNodes() if n.getType() == "right_before_left"]
    assert not wrong, (
        f"{len(wrong)} right_before_left junctions give way to the LEFT in a left-hand network "
        f"(ARR r.72 is give way to the right): {wrong[:10]}"
    )


def test_terminating_road_gives_way_at_unsignalised_t_intersections():
    net, signs = _net(), _sign_xy()
    judged, bad = 0, []
    for n in net.getNodes():
        if n.getType() not in UNSIGNALISED:
            continue
        arms = _arms(n)
        if len(arms) != 3:
            continue
        edges = [e for lst in arms.values() for _, e in lst]
        if any(e.getType() not in MINOR for e in edges):
            continue
        if any(_on_ring(e) for e in edges):
            continue  # a roundabout: r.114 (test_roundabout_entries_give_way_to_the_ring)
        x, y = n.getCoord()
        if any(math.hypot(sx - x, sy - y) < SIGN_NEAR_M for sx, sy in signs):
            continue
        heading = {nb: _heading(*lst[0]) for nb, lst in arms.items()}
        nbs = list(arms)
        straightness, through = max(
            (abs((heading[a] - heading[b] + 180) % 360 - 180), (a, b))
            for i, a in enumerate(nbs)
            for b in nbs[i + 1 :]
        )
        if straightness < THROUGH_DEG:
            continue
        through_in = [e for nb in through for out, e in arms[nb] if not out]
        if not through_in:
            continue
        judged += 1
        yields = sorted(
            {
                c.getState()
                for e in through_in
                for cs in e.getOutgoing().values()
                for c in cs
                if c.getDirection() == "s" and c.getState() in ("m", "s")
            }
        )
        if yields:
            (stem,) = (nb for nb in nbs if nb not in through)
            lon, lat = net.convertXY2LonLat(x, y)
            bad.append(
                (
                    n.getID(),
                    arms[stem][0][1].getName() or "(unnamed)",
                    through_in[0].getName() or "(unnamed)",
                    f"{lon:.5f},{lat:.5f}",
                )
            )
    assert judged >= 100, f"only {judged} T-intersections judged; the test would be vacuous"
    assert not bad, (
        f"{len(bad)} of {judged} unsignalised, unsigned minor-street T-intersections make the "
        f"continuing road give way to the terminating one (ARR r.69), "
        f"(junction, terminating road, continuing road, lon,lat): {bad[:15]}"
    )


def test_unsigned_cross_intersections_of_equal_streets_give_way_to_the_right():
    """ARR r.72 applies at any unsigned, unsignalised intersection, whatever the speed limit.

    netconvert only builds right_before_left / left_before_right junctions where every road is
    below --junctions.right-before-left.speed-threshold (49 km/h by default); at 50 km/h it picks
    one road as major instead. Judged: unsignalised junctions with four roads, all of one minor
    class, no OSM stop / give_way sign within SIGN_NEAR_M."""
    net, signs = _net(), _sign_xy()
    judged, bad = 0, []
    for n in net.getNodes():
        if n.getType() not in UNSIGNALISED:
            continue
        arms = _arms(n)
        if len(arms) != 4:
            continue
        edges = [e for lst in arms.values() for _, e in lst]
        types = {e.getType() for e in edges}
        if len(types) != 1 or not types <= MINOR:
            continue
        x, y = n.getCoord()
        if any(math.hypot(sx - x, sy - y) < SIGN_NEAR_M for sx, sy in signs):
            continue
        judged += 1
        if n.getType() != "left_before_right":
            lon, lat = net.convertXY2LonLat(x, y)
            names = sorted({e.getName() or "(unnamed)" for e in edges})
            kmh = sorted({round(e.getSpeed() * 3.6) for e in edges})
            bad.append((n.getID()[:32], n.getType(), names, kmh, f"{lon:.5f},{lat:.5f}"))
    assert judged >= 10, f"only {judged} cross intersections judged; the test would be vacuous"
    assert not bad, (
        f"{len(bad)} of {judged} unsigned cross intersections of equal minor streets are not "
        f"give-way-to-the-right (left_before_right in a left-hand network): {bad[:12]}"
    )


def test_roundabout_entries_give_way_to_the_ring():
    """ARR r.114: give way to vehicles already in the roundabout (OSM junction=roundabout ways)."""
    net = _net()
    nodes = {
        n
        for e in net.getEdges()
        if _on_ring(e)
        for n in (e.getFromNode(), e.getToNode())
        if n.getType() != "traffic_light"
    }
    circulating, entries, bad = 0, 0, []
    for n in nodes:
        ring_to_lane: set[str] = set()
        links = [(e, c) for e in n.getIncoming() for cs in e.getOutgoing().values() for c in cs]
        for e, c in links:
            if _on_ring(e) and _on_ring(c.getTo()):
                circulating += 1
                ring_to_lane.add(c.getToLane().getID())
                if c.getState() in ("m", "s"):
                    bad.append(("circulating link yields", n.getID(), e.getID(), c.getTo().getID()))
        for e, c in links:
            if not _on_ring(e) and _on_ring(c.getTo()) and c.getToLane().getID() in ring_to_lane:
                entries += 1
                if c.getState() not in ("m", "s"):
                    bad.append(("entry has priority", n.getID(), e.getID(), c.getTo().getID()))
    assert circulating >= 20 and entries >= 10, (
        f"only {circulating} circulating / {entries} entry links found; the test would be vacuous"
    )
    assert not bad, f"{len(bad)} roundabout links break ARR r.114: {bad[:12]}"
