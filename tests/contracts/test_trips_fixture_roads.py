"""The labelled trips fixture drives on real roads: tests/fixtures/trips_fixture.json vs roads_3d.

Written by qa-reviewer, independently of web/scripts/make-trips-fixture.mjs and its road graph.
frontend-engineer's claim (Phase 2 Stage C): the 40 hand-made cars follow real roads_3d.geojson
ways and their one-way tags, with z from roads_3d, except the two Domain Tunnel cars whose FAKE
depth (down to -12 m, stated in the meta) exercises the X-ray styling.

Checks:
- every fixture position is a roads_3d vertex (same lon/lat), with that vertex's z, except the
  labelled fake-depth tunnel cars, which may only go deeper than roads_3d on tunnel ways;
- every move between consecutive distinct positions is one edge of a roads_3d way, in a direction
  the way's OSM one-way tags allow (oneway=yes/1/true/-1; implied on motorway, motorway_link and
  roundabouts), read from osm_sim_area.osm.gz with the standard library;
- a stopped car repeats its exact position (no jumps while "stopped").
Skips when roads_3d.geojson or the OSM extract is missing.
"""

from __future__ import annotations

import gzip
import json
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "trips_fixture.json"
ROADS = ROOT / "data" / "processed" / "roads_3d.geojson"
EXTRACT = ROOT / "data" / "processed" / "osm_sim_area.osm.gz"
FAKE_TUNNEL_PREFIX = "fixture-domain-tunnel-"
YES = {"yes", "1", "true"}

pytestmark = pytest.mark.skipif(
    not (ROADS.exists() and EXTRACT.exists()), reason="roads_3d / OSM extract not built"
)


def _key(p: list[float]) -> tuple[float, float]:
    return (round(p[0], 6), round(p[1], 6))


@pytest.fixture(scope="module")
def roads() -> dict:
    fc = json.loads(ROADS.read_text(encoding="utf-8"))
    oneway: dict[int, int] = {}
    with gzip.open(EXTRACT) as f:
        for _, el in ET.iterparse(f):
            if el.tag != "way":
                continue
            tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
            ow = tags.get("oneway")
            implied = tags.get("highway") in {"motorway", "motorway_link"} or tags.get(
                "junction"
            ) in {"roundabout", "circular"}
            if ow == "-1":
                oneway[int(el.get("id"))] = -1
            elif ow in YES or (implied and ow != "no"):
                oneway[int(el.get("id"))] = 1
            el.clear()
    z_at: dict[tuple[float, float], set[float]] = defaultdict(set)
    tunnel_vertex: set[tuple[float, float]] = set()
    edges: set[tuple[tuple[float, float], tuple[float, float]]] = set()
    for feat in fc["features"]:
        props = feat["properties"]
        coords = feat["geometry"]["coordinates"]
        for c in coords:
            z_at[_key(c)].add(c[2])
            if props["tunnel"]:
                tunnel_vertex.add(_key(c))
        d = oneway.get(props["way_id"], 0)
        for a, b in zip(coords, coords[1:], strict=False):
            if d >= 0:
                edges.add((_key(a), _key(b)))
            if d <= 0:
                edges.add((_key(b), _key(a)))
    return {"z_at": z_at, "tunnel_vertex": tunnel_vertex, "edges": edges, "oneway": oneway}


@pytest.fixture(scope="module")
def trips() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_every_position_is_a_roads_3d_vertex_with_its_z(trips, roads):
    bad: list[str] = []
    for t in trips:
        fake = t["id"].startswith(FAKE_TUNNEL_PREFIX)
        for k, p in enumerate(t["path"]):
            zs = roads["z_at"].get(_key(p))
            if zs is None:
                bad.append(f"{t['id']}[{k}] {p[:2]} is not a roads_3d vertex")
            elif not any(abs(p[2] - z) <= 0.006 for z in zs) and not (
                fake and _key(p) in roads["tunnel_vertex"] and p[2] < min(zs)
            ):
                bad.append(f"{t['id']}[{k}] z {p[2]} vs roads_3d {sorted(zs)}")
    assert bad == [], "\n".join(bad[:20])


def test_fake_depth_only_on_the_labelled_tunnel_cars_and_tunnel_ways(trips, roads):
    deep = {t["id"] for t in trips if min(p[2] for p in t["path"]) < -0.5}
    assert deep == {t["id"] for t in trips if t["id"].startswith(FAKE_TUNNEL_PREFIX)}
    for t in trips:
        for p in t["path"]:
            if p[2] < -0.5:
                assert _key(p) in roads["tunnel_vertex"], f"{t['id']} deep off a tunnel way"


def test_every_move_follows_a_way_edge_in_an_allowed_direction(trips, roads):
    wrong: list[str] = []
    moves = 0
    for t in trips:
        path = t["path"]
        for k in range(1, len(path)):
            a, b = _key(path[k - 1]), _key(path[k])
            if a == b:
                continue  # a stop: same position repeated
            moves += 1
            if (a, b) not in roads["edges"]:
                rev = (b, a) in roads["edges"]
                wrong.append(
                    f"{t['id']} step {k}: {a}->{b} "
                    + ("against one-way" if rev else "not a way edge")
                )
    assert moves > 4000
    assert wrong == [], "\n".join(wrong[:20])


def test_stops_repeat_the_exact_position(trips):
    stops = 0
    for t in trips:
        p, ts = t["path"], t["timestamps"]
        for k in range(1, len(p)):
            if p[k] == p[k - 1]:
                stops += 1
                assert ts[k] - ts[k - 1] >= 5, (
                    f"{t['id']} stop {k} shorter than the 6-22 s signal stops"
                )
    assert stops > 0  # the fixture has signal stops (collapseStops is exercised)
