"""Car-free streets stay car-free in sim/net/roads.net.xml (qa-reviewer, independent of sim/ code).

- No network edge carries an OSM way that is closed to public cars (access / motor_vehicle /
  motorcar = no, private or permit; the most specific key decides).
- No car edge runs along Swanston Street between Flinders St and La Trobe St, the Bourke Street
  Mall, or the named car-free laneways. Checked geometrically against the raw OSM download
  (these ways are not in the drivable extract): a car edge that runs parallel to a car-free way
  within a few metres of it, away from its ends, fails. Cross streets are perpendicular.
- Shared zones (highway=living_street) are legal for cars in Victoria at 10 km/h unless signed
  otherwise (Road Safety Road Rules 2017 (Vic), r. 24 and r. 83): every living_street edge runs at
  its signed maxspeed or at 10 km/h.
"""

from __future__ import annotations

import math
from functools import cache

import pytest

from pipeline.config import SIM_BBOX
from tests.contracts.roads_net_helpers import (
    CAR_HIGHWAYS,
    NET_PATH,
    NO_PUBLIC_CARS,
    OSM_PATH,
    RAW_PBF,
    load_net,
    load_osm,
    motor_access,
)

pytestmark = pytest.mark.skipif(
    not (NET_PATH.exists() and OSM_PATH.exists()), reason="network or OSM extract not built yet"
)

LANEWAYS = {
    "Degraves Street",
    "Centre Place",
    "Hardware Lane",
    "Block Place",
    "Howey Place",
    "Hosier Lane",
    "AC/DC Lane",
    "Duckboard Place",
    "Rutledge Lane",
    "Royal Arcade",
}
TRIM_M = 15.0  # ignore the ends of a car-free way, where cross streets legitimately meet it
NEAR_M = 4.0
PARALLEL_DEG = 30.0
SHARED_ZONE_DEFAULT_MPS = 10 / 3.6


def test_no_edge_carries_a_way_closed_to_public_cars():
    net, osm = load_net(), load_osm()
    bad = []
    for e in net.edges.values():
        for w in e.ways:
            tags = osm.ways[w][1]
            if tags.get("highway") not in CAR_HIGHWAYS or motor_access(tags) in NO_PUBLIC_CARS:
                bad.append((e.id, w, tags.get("name"), tags.get("highway"), motor_access(tags)))
    assert not bad, f"car edges built from ways cars may not use: {bad}"


def test_shared_zones_run_at_their_signed_or_default_speed():
    net, osm = load_net(), load_osm()
    bad = []
    for e in net.edges.values():
        if e.type != "highway.living_street":
            continue
        signed = [osm.ways[w][1].get("maxspeed") for w in e.ways]
        limits = [float(s) / 3.6 for s in signed if s and s.replace(".", "").isdigit()]
        cap = max(limits) if limits else SHARED_ZONE_DEFAULT_MPS
        if e.speed > cap + 0.01:
            bad.append((e.id, e.name, round(e.speed * 3.6, 1), signed))
    assert not bad, f"shared-zone edges faster than allowed: {bad}"


@cache
def _raw_car_free_ways() -> dict[str, list[list[tuple[float, float]]]]:
    """Car-free polylines (lon, lat) from the raw download, grouped by check."""
    import osmium

    osm = load_osm()
    ways_at = osm.ways_at()
    # Swanston St car-free section: between its Flinders St and La Trobe St intersections.
    swanston_nodes = {
        n for refs, t in osm.ways.values() if t.get("name") == "Swanston Street" for n in refs
    }

    def lat_of(street: str) -> list[float]:
        return [
            osm.nodes[n][1]
            for n in swanston_nodes
            if any(osm.ways[w][1].get("name") == street for w in ways_at[n])
        ]

    south = max(lat_of("Flinders Street"))  # north edge of the Flinders St intersection
    north = min(lat_of("La Trobe Street"))  # south edge of the La Trobe St intersection

    out: dict[str, list[list[tuple[float, float]]]] = {
        "swanston": [],
        "bourke_mall": [],
        "laneways": [],
    }
    fp = (
        osmium.FileProcessor(str(RAW_PBF))
        .with_locations()
        .with_filter(osmium.filter.KeyFilter("highway"))
    )
    for obj in fp:
        if not obj.is_way():
            continue
        tags = dict(obj.tags)
        try:
            pts = [(n.lon, n.lat) for n in obj.nodes]
        except osmium.InvalidLocationError:
            continue
        if not all(
            SIM_BBOX.west <= x <= SIM_BBOX.east and SIM_BBOX.south <= y <= SIM_BBOX.north
            for x, y in pts
        ):
            continue
        name, hw = tags.get("name"), tags.get("highway")
        car_free = hw not in CAR_HIGHWAYS or motor_access(tags) in NO_PUBLIC_CARS
        if name == "Swanston Street" and car_free and all(south < y < north for _, y in pts):
            out["swanston"].append(pts)
        elif name == "Bourke Street" and hw == "pedestrian":
            out["bourke_mall"].append(pts)
        elif name in LANEWAYS and car_free:
            out["laneways"].append(pts)
    return out


@pytest.mark.skipif(not RAW_PBF.exists(), reason="data/raw/bbbike_melbourne.osm.pbf not available")
@pytest.mark.parametrize("check", ["swanston", "bourke_mall", "laneways"])
def test_no_car_edge_runs_along_a_car_free_street(check: str):
    from shapely.geometry import LineString, Point
    from shapely.strtree import STRtree

    net = load_net()
    segs, owner = [], []
    for e in net.edges.values():
        for a, b in zip(e.shape, e.shape[1:], strict=False):
            if a != b:
                segs.append(LineString([a, b]))
                owner.append(e)
    tree = STRtree(segs)

    ways = _raw_car_free_ways()[check]
    assert ways, f"no car-free ways found for {check}: check is vacuous"
    hits = set()
    for pts in ways:
        line = LineString([net.xy(x, y) for x, y in pts])
        if line.length <= 2 * TRIM_M:
            continue
        d = TRIM_M
        while d <= line.length - TRIM_M:
            p, q = line.interpolate(d), line.interpolate(min(d + 1.0, line.length))
            bearing = math.atan2(q.y - p.y, q.x - p.x)
            for i in tree.query(Point(p.x, p.y).buffer(NEAR_M)):
                s = segs[int(i)]
                if s.distance(p) > NEAR_M:
                    continue
                (x1, y1), (x2, y2) = s.coords
                diff = abs(
                    (math.atan2(y2 - y1, x2 - x1) - bearing + math.pi) % (2 * math.pi) - math.pi
                )
                if diff < math.radians(PARALLEL_DEG) or diff > math.radians(180 - PARALLEL_DEG):
                    hits.add((owner[int(i)].id, owner[int(i)].name))
            d += 5.0
    assert not hits, f"car edges along car-free {check}: {sorted(hits)}"
