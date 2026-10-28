"""SUMO floating-car data -> sim/out/trips_<scenario>.json (deck.gl TripsLayer, contract format).

Each trip is `{id, type, path: [[lon, lat, z_m], ...], timestamps: [s, ...]}`:

- lon/lat: SUMO's vehicle position on its lane (left-hand network, so on the left of the road),
  converted with the network's own projection; 6 decimals (~0.1 m).
- timestamps: seconds from the start of recording (the end of the warm-up), 0..900.
- z: road height from data/processed/roads_3d.geojson, the single source of road heights:
  * on a normal edge, the sample is projected onto the 3D line of the OSM way(s) the edge carries
    (lane param origId; the nearest of them) and z is interpolated along that line;
  * inside a junction (internal lanes, ":..."), z is interpolated by distance travelled between
    the last height before the junction and the first one after it;
  * then smoothed so no two consecutive samples differ by more than MAX_STEP_M: around any larger
    step (joined junctions whose roads end at different heights) the heights are re-interpolated
    by distance over a window that grows until every step fits. Such samples are `smoothed`.
- trips split where a vehicle teleports (it vanishes from the data, or jumps);
- where a 0.5 s step jumps from one edge to the next without a sample inside the junction, one
  point is added in the middle of the junction lane SUMO drove (timed by distance), so the drawn
  path follows the turn instead of cutting the corner;
- points that lie, within SIMPLIFY_XY_M / SIMPLIFY_Z_M, on the straight constant-speed motion
  between their neighbours are dropped (TripsLayer interpolates exactly that), which keeps turns
  and stops crisp; consecutive points kept in the file never differ by more than 1 m in height.
"""

from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

MAX_STEP_M = 1.0
STEP_TARGET_M = 0.95  # smoothing aims a little under the limit (rounding to 0.01 m)
SIMPLIFY_XY_M = 0.25
SIMPLIFY_Z_M = 0.05
SIMPLIFY_MAX_SPAN = 240  # samples (2 min at 0.5 s) between two kept points at most
JUMP_M = 40.0  # a jump this much beyond what the speed allows is a teleport
VEHICLE_TYPE = "passenger"


@dataclass(slots=True)
class Sample:
    t: float
    x: float
    y: float
    lane: str
    speed: float
    pos: float = 0.0  # position along the lane (m)
    z: float | None = None
    z_ref: float | None = None  # roads_3d height at the projected position (None in junctions)
    way: int | None = None
    tunnel: bool = False
    junction: bool = False  # on an internal junction lane
    smoothed: bool = False
    proj_dist_m: float | None = None  # distance from the sample to the way line it was mapped to


@dataclass
class Trip:
    id: str
    vehicle: str
    samples: list[Sample] = field(default_factory=list)
    kept: list[int] = field(default_factory=list)  # indices of samples written to the file


# --- reading --------------------------------------------------------------------------------


def read_fcd(path: Path, begin: float, end: float) -> dict[str, list[Sample]]:
    """Vehicle id -> samples in time order, for begin <= t <= end."""
    out: dict[str, list[Sample]] = defaultdict(list)
    for _, el in ET.iterparse(path, events=("end",)):
        if el.tag != "timestep":
            continue
        t = float(el.get("time"))
        if begin <= t <= end:
            for v in el.iter("vehicle"):
                out[v.get("id")].append(
                    Sample(
                        t,
                        float(v.get("x")),
                        float(v.get("y")),
                        v.get("lane", ""),
                        float(v.get("speed", 0)),
                        float(v.get("pos", 0)),
                    )
                )
        el.clear()
    return out


# --- heights --------------------------------------------------------------------------------


class HeightModel:
    """z from roads_3d.geojson, looked up through the OSM ways each SUMO edge carries."""

    def __init__(self, roads_3d: Path, net, ways_by_edge: dict[str, list[int]], tunnel_ways):
        from shapely.geometry import LineString

        self.lines: dict[int, tuple] = {}
        for f in json.loads(roads_3d.read_text())["features"]:
            wid = f["properties"]["way_id"]
            coords = f["geometry"]["coordinates"]
            xy = [net.convertLonLat2XY(c[0], c[1]) for c in coords]
            line = LineString(xy)
            seg = np.hypot(np.diff([p[0] for p in xy]), np.diff([p[1] for p in xy]))
            cum = np.concatenate([[0.0], np.cumsum(seg)])
            self.lines[wid] = (line, cum, np.array([c[2] for c in coords], dtype=float))
        self.ways_by_edge = ways_by_edge
        self.tunnel_ways = set(tunnel_ways)

    def z_at(self, edge: str, x: float, y: float) -> tuple[float, int, float] | None:
        """(z, way id, distance to the way line) for a position on `edge`, or None."""
        from shapely.geometry import Point

        p = Point(x, y)
        best = None
        for w in self.ways_by_edge.get(edge, []):
            if w not in self.lines:
                continue
            line, cum, z = self.lines[w]
            d = line.distance(p)
            if best is None or d < best[2]:
                s = line.project(p)
                best = (float(np.interp(s, cum, z)), w, d)
        return best


def load_height_model(net, osm, roads_3d: Path, joined: Path) -> HeightModel:
    """The height model for `net`: roads_3d lines looked up through each edge's OSM ways (lane
    param origId), tunnel ways from the OSM extract. Shared by the trips and edge-geometry
    exports so both give a road the same height."""
    from sim.checks.netindex import NetIndex, read_joined

    idx = NetIndex(net, read_joined(joined))
    return HeightModel(roads_3d, net, idx.ways_by_edge, tunnel_ways(osm))


def assign_heights(samples: list[Sample], heights: HeightModel, cache: dict | None = None) -> None:
    cache = {} if cache is None else cache
    for s in samples:
        if s.lane.startswith(":"):
            s.junction = True
            continue
        key = (s.lane, round(s.pos, 1))  # same lane position, same height (stopped cars)
        if key not in cache:
            cache[key] = heights.z_at(s.lane.rsplit("_", 1)[0], s.x, s.y)
        hit = cache[key]
        if hit is not None:
            s.z_ref, s.way, s.proj_dist_m = hit
            s.tunnel = s.way in heights.tunnel_ways


def _cumdist(samples: list[Sample]) -> np.ndarray:
    xs = np.array([s.x for s in samples])
    ys = np.array([s.y for s in samples])
    return np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(xs), np.diff(ys)))])


def fill_and_smooth(samples: list[Sample]) -> None:
    """Set s.z for a trip: roads_3d heights, junctions interpolated, steps limited."""
    n = len(samples)
    if n == 0:
        return
    d = _cumdist(samples)
    known = [i for i, s in enumerate(samples) if s.z_ref is not None]
    if not known:
        for s in samples:
            s.z = 0.0
        return
    z = np.interp(d, d[known], [samples[i].z_ref for i in known])
    # stationary stretches have equal distances: np.interp then takes the right value; fine.
    smoothed = np.zeros(n, dtype=bool)
    i = 0
    while i < n - 1:
        if abs(z[i + 1] - z[i]) <= MAX_STEP_M:
            i += 1
            continue
        a, b = i, i + 1
        while True:
            span = d[b] - d[a]
            if span > 0:
                cand = z[a] + (z[b] - z[a]) * (d[a : b + 1] - d[a]) / span
            else:
                cand = np.linspace(z[a], z[b], b - a + 1)
            if np.max(np.abs(np.diff(cand))) <= STEP_TARGET_M or (a == 0 and b == n - 1):
                break
            a, b = max(0, a - 1), min(n - 1, b + 1)
        if np.max(np.abs(np.diff(cand))) > STEP_TARGET_M:
            cand = np.linspace(z[a], z[b], b - a + 1)  # last resort: by sample, not distance
        z[a : b + 1] = cand
        smoothed[a + 1 : b] = True
        i = max(a, 0)
    for s, zz, sm in zip(samples, z, smoothed, strict=True):
        s.z = float(zz)
        s.smoothed = bool(sm)


# --- trips ----------------------------------------------------------------------------------


def split_trips(vehicle: str, samples: list[Sample], dt: float) -> list[Trip]:
    """One trip per continuous stretch: a time gap or an impossible jump starts a new one."""
    trips: list[Trip] = []
    cur: list[Sample] = []
    for s in samples:
        if cur:
            p = cur[-1]
            gap = s.t - p.t > dt * 1.5
            jump = math.hypot(s.x - p.x, s.y - p.y) > max(s.speed, p.speed) * (s.t - p.t) + JUMP_M
            if gap or jump:
                trips.append(Trip("", vehicle, cur))
                cur = []
        cur.append(s)
    if cur:
        trips.append(Trip("", vehicle, cur))
    for k, tr in enumerate(trips):
        tr.id = vehicle if k == 0 else f"{vehicle}#{k}"
    return trips


def simplify(samples: list[Sample]) -> list[int]:
    """Indices to keep: a point is dropped when linear motion in time between the kept points
    around it reproduces it within SIMPLIFY_XY_M (horizontal) and SIMPLIFY_Z_M (height)."""
    n = len(samples)
    if n <= 2:
        return list(range(n))
    t = np.array([s.t for s in samples])
    x = np.array([s.x for s in samples])
    y = np.array([s.y for s in samples])
    z = np.array([s.z for s in samples])
    keep = [0]
    a = 0
    while a < n - 1:
        b = a + 1
        while b + 1 < n and b + 1 - a <= SIMPLIFY_MAX_SPAN:
            c = b + 1
            f = (t[a + 1 : c] - t[a]) / (t[c] - t[a])
            ex = x[a] + f * (x[c] - x[a]) - x[a + 1 : c]
            ey = y[a] + f * (y[c] - y[a]) - y[a + 1 : c]
            ez = z[a] + f * (z[c] - z[a]) - z[a + 1 : c]
            if (
                np.max(np.hypot(ex, ey)) > SIMPLIFY_XY_M
                or np.max(np.abs(ez)) > SIMPLIFY_Z_M
                or abs(z[c] - z[a]) > STEP_TARGET_M  # file points also step <= 1 m in height
            ):
                break
            b = c
        keep.append(b)
        a = b
    return keep


class JunctionPaths:
    """Lanes SUMO drives between two lanes (junction lanes, short edges), from the network."""

    MAX_HOPS = 4

    def __init__(self, net):
        self.net = net
        self.cache: dict[tuple[str, str], tuple[list, str] | None] = {}
        self.lanes: dict[tuple[str, str], list[str]] = {}

    def _next(self, lane_id: str) -> list[str]:
        try:
            lane = self.net.getLane(lane_id)
        except (KeyError, AttributeError):
            return []
        out = []
        for c in lane.getOutgoing():
            via = c.getViaLaneID()
            out.append(via if via else c.getToLane().getID())
        return out

    def lanes_between(self, from_lane: str, to_lane: str) -> list[str]:
        self.via_shape(from_lane, to_lane)
        return self.lanes.get((from_lane, to_lane), [])

    def via_shape(self, from_lane: str, to_lane: str) -> tuple[list, str] | None:
        """(shape, first lane id) of the lanes driven strictly between the two lanes, or None if
        they are adjacent or not linked within MAX_HOPS."""
        key = (from_lane, to_lane)
        if key in self.cache:
            return self.cache[key]
        to_edge = to_lane.rsplit("_", 1)[0]
        found = None
        frontier = [[from_lane]]
        for _ in range(self.MAX_HOPS):
            nxt_frontier = []
            for path in frontier:
                for n in self._next(path[-1]):
                    if n == to_lane or n.rsplit("_", 1)[0] == to_edge:
                        found = path[1:]
                        break
                    if n not in path:
                        nxt_frontier.append([*path, n])
                if found is not None:
                    break
            if found is not None:
                break
            frontier = nxt_frontier
        result = None
        self.lanes[key] = list(found or [])
        if found:
            pts = [p for ln in found for p in self.net.getLane(ln).getShape()]
            result = (pts, found[0]) if len(pts) >= 2 else None
        self.cache[key] = result
        return result


INSERT_MIN_CUT_M = 0.5  # a chord this far from the junction lane gets a point inside the junction


def insert_junction_points(samples: list[Sample], paths: JunctionPaths) -> list[Sample]:
    """Where two consecutive samples are on different edges and SUMO drove lanes between them
    with no sample on them (a junction, or a short edge between two junctions), add one sample at
    the middle of those lanes, timed by distance. Without it the browser draws the 0.5 s chord
    straight across the corner."""
    from shapely.geometry import LineString, Point

    out = [samples[0]]
    for a, b in zip(samples, samples[1:], strict=False):
        ea, eb = a.lane.rsplit("_", 1)[0], b.lane.rsplit("_", 1)[0]
        if ea != eb:
            found = paths.via_shape(a.lane, b.lane)
            if found and len(found[0]) >= 2:
                via = LineString(found[0])
                mid = via.interpolate(0.5, normalized=True)
                chord_mid = Point((a.x + b.x) / 2, (a.y + b.y) / 2)
                if via.distance(chord_mid) > INSERT_MIN_CUT_M:
                    d1 = math.hypot(mid.x - a.x, mid.y - a.y)
                    d2 = math.hypot(b.x - mid.x, b.y - mid.y)
                    f = d1 / (d1 + d2) if d1 + d2 > 0 else 0.5
                    out.append(
                        Sample(
                            round(a.t + f * (b.t - a.t), 3),
                            mid.x,
                            mid.y,
                            found[1],
                            (a.speed + b.speed) / 2,
                            0.0,
                        )
                    )
        out.append(b)
    return out


def build_trips(
    fcd: dict[str, list[Sample]],
    heights: HeightModel,
    dt: float,
    paths: JunctionPaths | None = None,
) -> list[Trip]:
    trips = []
    cache: dict = {}
    for vid in sorted(fcd):
        for tr in split_trips(vid, fcd[vid], dt):
            if len(tr.samples) < 2:
                continue
            if paths is not None:
                tr.samples = insert_junction_points(tr.samples, paths)
            assign_heights(tr.samples, heights, cache)
            fill_and_smooth(tr.samples)
            tr.kept = simplify(tr.samples)
            trips.append(tr)
    trips.sort(key=lambda tr: (tr.samples[0].t, tr.id))
    return trips


# --- writing --------------------------------------------------------------------------------


def to_lonlat(net, xs: np.ndarray, ys: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Vectorised sumolib convertXY2LonLat."""
    ox, oy = net.getLocationOffset()
    lon, lat = net.getGeoProj()(xs - ox, ys - oy, inverse=True)
    return np.asarray(lon), np.asarray(lat)


def trips_json(trips: list[Trip], net, t0: float) -> str:
    """The contract JSON: one trip per line, compact, fixed rounding (deterministic bytes)."""
    lines = []
    for tr in trips:
        ks = [tr.samples[i] for i in tr.kept]
        lon, lat = to_lonlat(net, np.array([s.x for s in ks]), np.array([s.y for s in ks]))
        path = ",".join(
            f"[{lo:.6f},{la:.6f},{s.z:.2f}]" for lo, la, s in zip(lon, lat, ks, strict=True)
        )
        stamps = ",".join(_num(s.t - t0) for s in ks)
        lines.append(
            f'{{"id":{json.dumps(tr.id)},"type":"{VEHICLE_TYPE}",'
            f'"path":[{path}],"timestamps":[{stamps}]}}'
        )
    return "[\n" + ",\n".join(lines) + "\n]\n"


def _num(v: float) -> str:
    r = round(v, 2)
    return str(int(r)) if r == int(r) else f"{r:g}"


# --- scenario -------------------------------------------------------------------------------


def sumo_version() -> str:
    import re
    import subprocess

    import sumolib

    out = subprocess.run(
        [sumolib.checkBinary("sumo"), "--version"], capture_output=True, text=True, check=True
    ).stdout
    m = re.search(r"Eclipse SUMO sumo (\S+)", out)
    return m.group(1) if m else "unknown"


def generated_at() -> str:
    """UTC time of the export, or SOURCE_DATE_EPOCH when set (reproducible builds).

    The trips file is byte-deterministic; this one meta field is excluded from determinism
    checks unless SOURCE_DATE_EPOCH pins it."""
    import os
    from datetime import UTC, datetime

    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    when = datetime.fromtimestamp(int(epoch), UTC) if epoch else datetime.now(UTC)
    return when.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def tunnel_ways(osm) -> set[int]:
    """OSM ways that are real tunnels (any tunnel tag except building_passage)."""
    return {
        w
        for w, (_, t) in osm.ways.items()
        if t.get("tunnel") not in (None, "no", "building_passage")
    }


def passage_ways(osm) -> set[int]:
    return {w for w, (_, t) in osm.ways.items() if t.get("tunnel") == "building_passage"}


def car_free_corridors(net, pbf: Path, to_xy):
    """{check: (street name, [xy lines])} of OSM's car-free Swanston St and Bourke St Mall ways."""
    if not pbf.exists():
        return None
    from pipeline.config import SIM_EXTRACT_BBOX
    from sim.checks.connectivity import _no_cars, read_raw_ways

    raw = read_raw_ways(pbf, SIM_EXTRACT_BBOX)

    def lines(pred):
        return [[to_xy(lo, la) for lo, la in pts] for w, (pts, t) in raw.items() if pred(pts, t)]

    swanston = lines(
        lambda pts, t: (
            t.get("name") == "Swanston Street"
            and _no_cars(t)
            and all(-37.8185 <= la <= -37.8095 for _, la in pts)
        )
    )
    mall = lines(
        lambda pts, t: (
            t.get("name") in ("Bourke Street Mall", "Bourke Street")
            and _no_cars(t)
            and all(144.9625 <= lo <= 144.9665 for lo, _ in pts)
        )
    )
    return {
        "swanston_st_flinders_to_la_trobe": ("Swanston Street", swanston),
        "bourke_st_mall": ("Bourke Street", mall),
    }


def export_scenario(
    scenario: str,
    fcd: Path,
    metrics: dict,
    out_dir: Path,
    checks_dir: Path,
    net_path: Path,
    begin: float,
    end: float,
    dt: float,
    demand: str = "placeholder_random",
    run_checks: bool = True,
    inputs: dict[str, Path] | None = None,
    clock0: float | None = None,
    duration_s: int | None = None,
    teleports: int | None = None,
    start_local: str | None = None,
) -> dict:
    """Export trips + meta (+ checks). `inputs` overrides the data paths (tests use a synthetic
    network): osm, roads_3d, buildings, joined, pbf.

    [begin, end] is the FCD window written to the file. `clock0` is the simulation second that is
    t = 0 of the scenario clock (default `begin`); `duration_s` the scenario length on that clock
    (default end - begin); `teleports` the scenario's teleport count (default the metrics'
    `teleports_in_recording`); `start_local` (optional meta field) the ISO wall-clock time of
    t = 0. The baseline uses the defaults; am_peak writes only its 07:30-08:00 window on the
    06:30 clock of its edge stats."""
    clock0 = begin if clock0 is None else clock0
    import sumolib

    from sim.checks import trips as checks
    from sim.checks.netindex import read_osm
    from sim.config import JOINED_PATH, OSM_EXTRACT, REPO_ROOT, SEED

    processed = REPO_ROOT / "data" / "processed"
    paths = {
        "osm": OSM_EXTRACT,
        "roads_3d": processed / "roads_3d.geojson",
        "buildings": processed / "buildings.geojson",
        "joined": JOINED_PATH,
        "pbf": REPO_ROOT / "data" / "raw" / "bbbike_melbourne.osm.pbf",
        **(inputs or {}),
    }
    net = sumolib.net.readNet(str(net_path), withInternal=True)
    osm = read_osm(paths["osm"])
    heights = load_height_model(net, osm, paths["roads_3d"], paths["joined"])
    data = read_fcd(fcd, begin, end)
    junction_paths = JunctionPaths(net)
    trips = build_trips(data, heights, dt, junction_paths)

    out_dir.mkdir(parents=True, exist_ok=True)
    trips_path = out_dir / f"trips_{scenario}.json"
    trips_path.write_text(trips_json(trips, net, clock0), encoding="utf-8")
    vehicles = sorted({tr.vehicle for tr in trips})
    meta = {
        "scenario": scenario,
        "demand": demand,
        "duration_s": int(round(end - begin)) if duration_s is None else int(duration_s),
        "seed": SEED,
        "sumo_version": sumo_version(),
        # distinct SUMO vehicles in the trips file (the qa contract: meta.vehicles counts
        # vehicles, not trip pieces). A teleport splits one vehicle into "<id>", "<id>#1", ...;
        # summary.split_trips gives the difference.
        "vehicles": len(vehicles),
        "teleports": int(
            metrics.get("teleports_in_recording", 0) if teleports is None else teleports
        ),
        "tunnel_vehicles": len({tr.vehicle for tr in trips if any(s.tunnel for s in tr.samples)}),
        "generated_at": generated_at(),
    }
    if start_local is not None:
        meta["start_local"] = start_local
    (out_dir / f"{scenario}.meta.json").write_text(
        json.dumps(meta, indent=1) + "\n", encoding="utf-8"
    )
    summary = {
        "trips": len(trips),
        "vehicles": len(vehicles),
        "split_trips": len(trips) - len(vehicles),
        "samples_recorded": sum(len(tr.samples) for tr in trips),
        "points_written": sum(len(tr.kept) for tr in trips),
        "bytes": trips_path.stat().st_size,
    }
    if run_checks:

        def to_xy(lon, lat):
            return net.convertLonLat2XY(lon, lat)

        def edge_name(lane: str) -> str:
            eid = lane.rsplit("_", 1)[0]
            return net.getEdge(eid).getName() if net.hasEdge(eid) else ""

        lane_shapes = {
            ln.getID(): (ln.getShape(), ln.getWidth())
            for e in net.getEdges(withInternal=True)
            for ln in e.getLanes()
        }
        edge_lanes = {
            e.getID(): [ln.getID() for ln in e.getLanes()] for e in net.getEdges(withInternal=True)
        }

        def envelope(lane: str) -> float:
            """How far a lane centre may sit from its OSM centreline: the carriageway width on
            its side of a two-way road (a reverse edge exists), half of it on a one-way road."""
            eid = lane.rsplit("_", 1)[0]
            if eid.startswith(":") or not net.hasEdge(eid):
                return 6.0
            e = net.getEdge(eid)
            width = sum(ln.getWidth() for ln in e.getLanes())
            two_way = any(r.getToNode() is e.getFromNode() for r in e.getToNode().getOutgoing())
            return width if two_way else width / 2

        xy = {n: to_xy(lo, la) for n, (lo, la) in osm.nodes.items()}
        report = {
            "scenario": scenario,
            "summary": summary,
            "alignment": checks.alignment(trips, osm, xy, envelope, edge_name),
            "corner_cutting": checks.corner_cutting(
                trips,
                lane_shapes,
                edge_lanes,
                {
                    e.getID(): (e.getFromNode().getShape(), e.getToNode().getShape())
                    for e in net.getEdges(withInternal=True)
                },
                junction_paths.lanes_between,
            ),
            "heights": checks.heights(trips),
            "buildings": checks.buildings(
                trips,
                paths["buildings"],
                to_xy,
                passage_ways(osm),
                edge_name,
                lambda x, y: tuple(round(v, 6) for v in net.convertXY2LonLat(x, y)),
            ),
            "car_free": checks.car_free(
                trips,
                car_free_corridors(net, paths["pbf"], to_xy),
                edge_name,
            ),
        }
        checks.write(report, checks_dir / f"trips_{scenario}_checks.json")
        summary["checks"] = {
            k: v.get("pass") for k, v in report.items() if isinstance(v, dict) and k != "summary"
        }
    return {"meta": meta, "summary": summary}
