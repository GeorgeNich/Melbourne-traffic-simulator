"""Contract test for sim/out/trips_<scenario>.json and sim/out/<scenario>.meta.json.

Written by qa-reviewer, independently of sim/ (it imports nothing from sim/, only the extract bbox
from pipeline/config.py, the single source of truth for bboxes). Every scenario with a trips file
in sim/out/ is checked; the tests skip when there is none.

CLAUDE.md "Data contracts":
- trips: a JSON array of `{id, type, path: [[lon, lat, z_m], ...], timestamps: [s, ...]}`, z from
  data/processed/roads_3d.geojson.
- meta: `scenario`, `demand` (`placeholder_random` / `calibrated_counts`), `duration_s`, `seed`,
  `sumo_version`, `vehicles`, `teleports`, `tunnel_vehicles`, `generated_at`.

What it checks:
- Exact keys per trip; `id` a unique non-empty string; `type` is `passenger` (Phase 2: cars only).
- Every path point is three finite numbers `[lon, lat, z]` (lon first) inside SIM_EXTRACT_BBOX,
  give or take BBOX_SLACK_M: the extract clips ways at the bbox and SUMO draws each lane offset to
  the left of the way's centreline, so a car on a boundary edge's last metre can sit ~1-2 m
  outside (seen: 1.3 m at the east edge). Swapped lat/lon would be off by 180 km. Approved
  Phase 2c: a point on a CityLink motorway tunnel way (within TUNNEL_SNAP_M of a motorway(_link)
  `tunnel` line in roads_3d) may instead lie in TUNNEL_EXTRACT_BBOX, as in test_trips_contract.py.
- Timestamps: finite numbers, same count as the path, strictly increasing, within
  0 <= t <= meta.duration_s (seconds from scenario start, i.e. from the end of the warm-up).
- At least two points per trip (TripsLayer needs a segment).
- Exact meta keys and types; `scenario` matches the file name and is lowercase snake_case;
  `demand` is one of the two contract values (`placeholder_random` until Phase 3);
  `generated_at` is ISO 8601 UTC; `duration_s` > 0.
- `vehicles` is the number of distinct vehicles: a trip split where a vehicle teleports keeps the
  vehicle id with a `#<k>` suffix (sim/export_trips.py naming), so the distinct base ids must equal
  `vehicles`, and the number of extra pieces can't exceed `teleports` (only a teleport splits).
- `tunnel_vehicles` lies between the vehicles that are below ground ON A TUNNEL WAY (z < -0.5 m
  and the nearest below-ground roads_3d way is tagged `tunnel`, the web's X-ray rule) and the
  vehicles that ever come within TUNNEL_NEAR_M of a roads_3d tunnel line (in plan view). Approved
  Phase 2c: cars on open cuttings, underpasses and portal ramps may be below ground without being
  tunnel vehicles, but every below-ground point must lie within TUNNEL_NEAR_M of a below-ground
  roads_3d way (no car sinks where no road does).
- z within the range of roads_3d.geojson heights (heights come only from there), and consecutive
  points never differ by more than 1 m in height (Decisions log: no step > 1 m).
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from functools import cache
from pathlib import Path

import pytest

from pipeline.config import SIM_EXTRACT_BBOX, TUNNEL_EXTRACT_BBOX

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "sim" / "out"
ROADS_3D = ROOT / "data" / "processed" / "roads_3d.geojson"

TRIP_KEYS = {"id", "type", "path", "timestamps"}
META_KEYS = {
    "scenario": str,
    "demand": str,
    "duration_s": int,
    "seed": int,
    "sumo_version": str,
    "vehicles": int,
    "teleports": int,
    "tunnel_vehicles": int,
    "generated_at": str,
}
# contract addition 2026-10-27 (PLAN decisions log): optional ISO 8601 Australia/Melbourne
# wall-clock time of trips-clock 0; absent for baseline
OPTIONAL_META_KEYS = {"start_local": str}
DEMANDS = {"placeholder_random", "calibrated_counts"}
VEHICLE_TYPES = {"passenger"}  # Phase 2 decision: cars only
SPLIT_SUFFIX = re.compile(r"#\d+$")
SNAKE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")

Z_ROUND_M = 0.01  # z stored to 0.01 m
MAX_Z_STEP_M = 1.0
BELOW_GROUND_M = -0.5
TUNNEL_NEAR_M = 20.0  # a lane centre sits well within this of its tunnel's centreline
M_PER_DEG = 111_320.0
BBOX_SLACK_M = 5.0
TUNNEL_SNAP_M = 10.0  # same as test_trips_contract.py

SCENARIOS = sorted(p.name[len("trips_") : -len(".json")] for p in OUT.glob("trips_*.json"))

pytestmark = pytest.mark.skipif(not SCENARIOS, reason="no sim/out/trips_*.json exported yet")


@cache
def _trips(scenario: str) -> list:
    return json.loads((OUT / f"trips_{scenario}.json").read_text(encoding="utf-8"))


@cache
def _meta(scenario: str) -> dict:
    path = OUT / f"{scenario}.meta.json"
    assert path.exists(), f"{path.name} missing next to trips_{scenario}.json"
    return json.loads(path.read_text(encoding="utf-8"))


@cache
def _roads_3d() -> list:
    return json.loads(ROADS_3D.read_text(encoding="utf-8"))["features"]


def _is_num(x: object) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x)


def _base_id(trip_id: str) -> str:
    return SPLIT_SUFFIX.sub("", trip_id)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_trips_shape_and_keys(scenario: str):
    trips = _trips(scenario)
    assert isinstance(trips, list) and trips, "trips file must be a non-empty JSON array"
    bad_keys, bad_type, bad_id = [], [], []
    for i, tr in enumerate(trips):
        if not isinstance(tr, dict) or set(tr) != TRIP_KEYS:
            bad_keys.append((i, sorted(tr) if isinstance(tr, dict) else type(tr).__name__))
            continue
        if tr["type"] not in VEHICLE_TYPES:
            bad_type.append((tr["id"], tr["type"]))
        if not isinstance(tr["id"], str) or not tr["id"]:
            bad_id.append((i, tr["id"]))
    assert not bad_keys, f"trips without exactly {sorted(TRIP_KEYS)}: {bad_keys[:10]}"
    assert not bad_type, f"vehicle types other than {VEHICLE_TYPES}: {bad_type[:10]}"
    assert not bad_id, f"ids must be non-empty strings: {bad_id[:10]}"
    ids = [tr["id"] for tr in trips]
    dupes = sorted({i for i in ids if ids.count(i) > 1}) if len(set(ids)) != len(ids) else []
    assert not dupes, f"duplicate trip ids: {dupes[:10]}"


def _slack_deg(lat0_deg: float) -> tuple[float, float]:
    dlat = BBOX_SLACK_M / M_PER_DEG
    return BBOX_SLACK_M / (M_PER_DEG * math.cos(math.radians(lat0_deg))), dlat


def _in_bbox(b, lon: float, lat: float, dlon: float, dlat: float) -> bool:
    return b.west - dlon <= lon <= b.east + dlon and b.south - dlat <= lat <= b.north + dlat


def _motorway_tunnel_tree():
    from shapely import STRtree
    from shapely.geometry import LineString

    lines = [
        LineString([(lo, la) for lo, la, *_ in f["geometry"]["coordinates"]])
        for f in _roads_3d()
        if f["properties"].get("tunnel") is True
        and f["properties"].get("highway") in ("motorway", "motorway_link")
        and len(f["geometry"]["coordinates"]) >= 2
    ]
    return STRtree(lines) if lines else None


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_path_points_are_lon_lat_z_inside_the_extract(scenario: str):
    from shapely.geometry import Point

    b, t = SIM_EXTRACT_BBOX, TUNNEL_EXTRACT_BBOX
    dlon, dlat = _slack_deg((b.south + b.north) / 2)
    snap_deg = TUNNEL_SNAP_M / (M_PER_DEG * math.cos(math.radians((b.south + b.north) / 2)))
    tree = _motorway_tunnel_tree() if ROADS_3D.exists() else None
    bad, outside, short = [], [], []
    for tr in _trips(scenario):
        path = tr["path"]
        if not isinstance(path, list) or len(path) < 2:
            short.append(tr["id"])
            continue
        for k, p in enumerate(path):
            if not (isinstance(p, list) and len(p) == 3 and all(_is_num(v) for v in p)):
                bad.append((tr["id"], k, p))
                continue
            lon, lat, _ = p
            if _in_bbox(b, lon, lat, dlon, dlat):
                continue
            on_tunnel = tree is not None and len(
                tree.query(Point(lon, lat), predicate="dwithin", distance=snap_deg)
            )
            if not (on_tunnel and _in_bbox(t, lon, lat, dlon, dlat)):
                outside.append((tr["id"], k, lon, lat))
    assert not short, f"trips with fewer than 2 points: {short[:10]}"
    assert not bad, f"path points that are not [lon, lat, z] numbers: {bad[:10]}"
    assert not outside, (
        f"{len(outside)} points more than {BBOX_SLACK_M} m outside SIM_EXTRACT_BBOX and not on a "
        f"CityLink tunnel inside TUNNEL_EXTRACT_BBOX (or lat/lon swapped): {outside[:10]}"
    )


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_timestamps_increase_within_the_scenario(scenario: str):
    duration = _meta(scenario)["duration_s"]
    bad = []
    for tr in _trips(scenario):
        ts, path = tr["timestamps"], tr["path"]
        if not isinstance(ts, list) or len(ts) != len(path):
            bad.append((tr["id"], "length", len(ts) if isinstance(ts, list) else ts, len(path)))
            continue
        if not all(_is_num(t) for t in ts):
            bad.append((tr["id"], "not numbers"))
            continue
        if ts[0] < 0 or ts[-1] > duration:
            bad.append((tr["id"], "range", ts[0], ts[-1]))
        if any(b <= a for a, b in zip(ts, ts[1:], strict=False)):
            bad.append((tr["id"], "not strictly increasing"))
    assert not bad, f"{len(bad)} trips with bad timestamps (0..{duration} s): {bad[:10]}"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_meta_fields_and_types(scenario: str):
    meta = _meta(scenario)
    allowed = set(META_KEYS) | set(OPTIONAL_META_KEYS)
    assert set(META_KEYS) <= set(meta) <= allowed, (
        f"meta keys {sorted(meta)} != {sorted(META_KEYS)} (+ optional {sorted(OPTIONAL_META_KEYS)})"
    )
    wrong = {
        k: type(meta[k]).__name__
        for k, t in (META_KEYS | OPTIONAL_META_KEYS).items()
        if k in meta and (not isinstance(meta[k], t) or isinstance(meta[k], bool))
    }
    assert not wrong, f"meta fields of the wrong type: {wrong}"
    assert meta["scenario"] == scenario, "meta.scenario must match the file name"
    assert SNAKE.match(scenario), "scenario names are lowercase snake_case"
    assert meta["demand"] in DEMANDS, f"demand {meta['demand']!r} not in {DEMANDS}"
    assert meta["duration_s"] > 0
    assert meta["vehicles"] >= 0 and meta["teleports"] >= 0 and meta["tunnel_vehicles"] >= 0
    assert meta["sumo_version"].strip(), "sumo_version must be filled in"
    assert meta["generated_at"].endswith("Z"), "generated_at must be UTC (…Z)"
    datetime.fromisoformat(meta["generated_at"].replace("Z", "+00:00"))


def test_baseline_is_labelled_placeholder_until_phase_3():
    if "baseline" not in SCENARIOS:
        pytest.skip("no baseline scenario")
    assert _meta("baseline")["demand"] == "placeholder_random"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_vehicle_count_matches_the_trips(scenario: str):
    meta, trips = _meta(scenario), _trips(scenario)
    vehicles = {_base_id(tr["id"]) for tr in trips}
    pieces = len(trips) - len(vehicles)
    assert meta["vehicles"] == len(vehicles), (
        f"meta.vehicles {meta['vehicles']} != {len(vehicles)} distinct vehicles in the trips"
    )
    assert pieces <= meta["teleports"], (
        f"{pieces} split trips but only {meta['teleports']} teleports (only a teleport splits)"
    )


def _tunnel_lines():
    from shapely.geometry import LineString

    lines = []
    for f in _roads_3d():
        if f["properties"].get("tunnel"):
            c = f["geometry"]["coordinates"]
            if len(c) >= 2:
                lines.append(LineString([(lo, la) for lo, la, *_ in c]))
    return lines


def _xy_m(lon: float, lat: float) -> tuple[float, float]:
    lat0 = (SIM_EXTRACT_BBOX.south + SIM_EXTRACT_BBOX.north) / 2
    return (lon - 144.95) * M_PER_DEG * math.cos(math.radians(lat0)), (lat - lat0) * M_PER_DEG


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_tunnel_vehicles_is_consistent(scenario: str):
    if not ROADS_3D.exists():
        pytest.skip("roads_3d.geojson not built yet")
    from shapely import STRtree
    from shapely.geometry import LineString, Point

    meta, trips = _meta(scenario), _trips(scenario)
    assert meta["tunnel_vehicles"] <= meta["vehicles"]
    # Below-ground roads_3d ways (metres, local plane) and whether each is a real tunnel.
    under, is_tunnel = [], []
    for f in _roads_3d():
        c = f["geometry"]["coordinates"]
        if len(c) >= 2 and min(p[2] for p in c) < BELOW_GROUND_M:
            under.append(LineString([_xy_m(p[0], p[1]) for p in c]))
            tun = f["properties"].get("tunnel")
            is_tunnel.append(bool(tun) and tun != "building_passage")
    under_tree = STRtree(under) if under else None
    in_tunnel: set[str] = set()
    stray = []
    for tr in trips:
        for lo, la, z in tr["path"]:
            if z >= BELOW_GROUND_M:
                continue
            pt = Point(_xy_m(lo, la))
            if under_tree is None:
                stray.append((tr["id"], lo, la, z))
                break
            i = int(under_tree.nearest(pt))
            if under[i].distance(pt) > TUNNEL_NEAR_M:
                stray.append((tr["id"], lo, la, z))
            elif is_tunnel[i]:
                in_tunnel.add(_base_id(tr["id"]))
    assert not stray, f"below-ground points with no below-ground road nearby: {stray[:10]}"

    lines = _tunnel_lines()
    tree = STRtree(lines) if lines else None
    near: set[str] = set()
    lat0 = (SIM_EXTRACT_BBOX.south + SIM_EXTRACT_BBOX.north) / 2
    deg = TUNNEL_NEAR_M / (M_PER_DEG * math.cos(math.radians(lat0)))  # lon degrees (the larger)
    for tr in trips:
        vid = _base_id(tr["id"])
        if vid in near or tree is None:
            continue
        for lo, la, _ in tr["path"]:
            if len(tree.query(Point(lo, la), predicate="dwithin", distance=deg)):
                near.add(vid)
                break
    assert len(in_tunnel) <= meta["tunnel_vehicles"], (
        f"{len(in_tunnel)} vehicles are below ground on a tunnel way but tunnel_vehicles is "
        f"{meta['tunnel_vehicles']}"
    )
    assert meta["tunnel_vehicles"] <= len(near), (
        f"tunnel_vehicles {meta['tunnel_vehicles']} but only {len(near)} vehicles come within "
        f"{TUNNEL_NEAR_M} m of a tunnel"
    )


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_heights_come_from_roads_3d_and_ramp_smoothly(scenario: str):
    if not ROADS_3D.exists():
        pytest.skip("roads_3d.geojson not built yet")
    zs = [c[2] for f in _roads_3d() for c in f["geometry"]["coordinates"]]
    lo, hi = min(zs) - Z_ROUND_M, max(zs) + Z_ROUND_M
    out_of_range, steps = [], []
    for tr in _trips(scenario):
        path = tr["path"]
        for k, p in enumerate(path):
            if not lo <= p[2] <= hi:
                out_of_range.append((tr["id"], k, p[2]))
            if k and abs(p[2] - path[k - 1][2]) > MAX_Z_STEP_M + Z_ROUND_M:
                steps.append((tr["id"], k, path[k - 1][2], p[2]))
    assert not out_of_range, (
        f"z outside the roads_3d range [{lo:.2f}, {hi:.2f}] m: {out_of_range[:10]}"
    )
    assert not steps, f"height steps over {MAX_Z_STEP_M} m between points: {steps[:10]}"
