"""Contract test for sim/out/trips_<scenario>.json and sim/out/<scenario>.meta.json (CLAUDE.md).

Written by qa-reviewer, independently of sim/export_trips.py and web/src/data/tripsContract.ts (it
imports neither; only the extract bbox from pipeline/config.py, the single source of truth for
bboxes).

Contract (CLAUDE.md "Data contracts"):
- trips_<scenario>.json: array of {id, type, path: [[lon, lat, z_m], ...], timestamps: [s, ...]},
  z from roads_3d.geojson, seconds from scenario start.
- <scenario>.meta.json: scenario, demand (placeholder_random / calibrated_counts), duration_s, seed,
  sumo_version, vehicles, teleports, tunnel_vehicles, generated_at.

What it checks, per pair:
- exactly those keys (trips and meta), with the right JSON types (no bools posing as numbers);
- ids unique non-empty strings; type a non-empty string;
- path[i] = [lon, lat, z], finite numbers, lon/lat inside SIM_EXTRACT_BBOX (+ ~10 m: roads_3d
  ways loop out of the extract by at most 4.8 m), or (contract update, ed70512) inside
  TUNNEL_EXTRACT_BBOX but only within ~10 m of a motorway `tunnel` way in roads_3d.geojson
  (the CityLink tunnels run on to the building ring's east edge); z plausible, and for real
  runs within the z range of roads_3d.geojson (the single source of car heights);
- timestamps finite, same length as path, >= 0, strictly increasing, and no later than duration_s;
- meta.scenario matches the file names (lowercase snake_case); demand is a contract value;
  generated_at is an ISO 8601 date-time with a timezone;
- meta.vehicles == number of trips; 0 <= tunnel_vehicles <= vehicles;
- test-fixture labels only in tests/fixtures/: real exports carry no "fixture-" ids, no
  demand "test_fixture" and no `_test_fixture`; the labelled fixture carries all three.

Runs on every committed sim/out/trips_*.json (skips when there is none), always on the labelled
fixture tests/fixtures/trips_fixture.json (proof that the checks work on a real-shaped file), and on
any extra directory listed in TRIPS_CONTRACT_DIRS (os.pathsep-separated; e.g. a review copy of
another branch's sim/out). Negative cases prove each rule actually fires.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path

import pytest

from pipeline.config import SIM_EXTRACT_BBOX, TUNNEL_EXTRACT_BBOX

ROOT = Path(__file__).resolve().parents[2]
SIM_OUT = ROOT / "sim" / "out"
FIXTURE_TRIPS = ROOT / "tests" / "fixtures" / "trips_fixture.json"
FIXTURE_META = ROOT / "tests" / "fixtures" / "trips_fixture.meta.json"
ROADS_3D = ROOT / "data" / "processed" / "roads_3d.geojson"

TRIP_KEYS = {"id", "type", "path", "timestamps"}
META_KEYS = {
    "scenario",
    "demand",
    "duration_s",
    "seed",
    "sumo_version",
    "vehicles",
    "teleports",
    "tunnel_vehicles",
    "generated_at",
}
FIXTURE_EXTRA_META_KEYS = {"_test_fixture", "_notes"}
# contract addition 2026-10-27 (PLAN decisions log): optional, absent for baseline
OPTIONAL_META_KEYS = {"start_local"}
DEMANDS = {"placeholder_random", "calibrated_counts"}
FIXTURE_DEMAND = "test_fixture"
FIXTURE_ID_PREFIX = "fixture-"
SCENARIO_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# ISO 8601 date-time with an explicit timezone (Z or +hh:mm).
ISO_TZ_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$")
# roads_3d ways loop out of the extract bbox by at most 4.8 m (test_roads_3d_contract.py); cars
# drive on them, so allow ~10 m.
BBOX_SLACK_DEG = 1e-4
# Plausible car heights (m) for any file (CityLink tunnels are ~-30 m in roads_3d).
PLAUSIBLE_Z = (-40.0, 40.0)
# Real runs: z comes from roads_3d.geojson, so it stays inside its z range (+ the renderer-free
# slack of interpolation rounding).
ROADS_Z_SLACK_M = 0.05
# Timestamps may run to the end of the scenario, not past it (one FCD step of slack).
DURATION_SLACK_S = 0.5


def _num(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def _int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _pairs_in(directory: Path) -> list[tuple[str, Path, Path]]:
    """(scenario, trips, meta) for every trips_<scenario>.json in a directory."""
    out = []
    for trips in sorted(directory.glob("trips_*.json")):
        scenario = trips.stem.removeprefix("trips_")
        out.append((scenario, trips, directory / f"{scenario}.meta.json"))
    return out


def _real_pairs() -> list[tuple[str, Path, Path]]:
    dirs = [SIM_OUT] + [
        Path(d) for d in os.environ.get("TRIPS_CONTRACT_DIRS", "").split(os.pathsep) if d
    ]
    return [p for d in dirs if d.is_dir() for p in _pairs_in(d)]


TUNNEL_SNAP_M = 10.0
_M_PER_DEG_LAT = 111_320.0


def _tunnel_ways() -> list[list[list[float]]]:
    """Coordinates of every motorway(_link) way flagged tunnel in roads_3d.geojson."""
    if not ROADS_3D.exists():
        return []
    fc = json.loads(ROADS_3D.read_text(encoding="utf-8"))
    return [
        f["geometry"]["coordinates"]
        for f in fc["features"]
        if f["properties"].get("tunnel") is True
        and f["properties"].get("highway") in ("motorway", "motorway_link")
    ]


def _near_tunnel_way(lon: float, lat: float, ways: list[list[list[float]]]) -> bool:
    """True when (lon, lat) is within TUNNEL_SNAP_M of a segment of a tunnel way (local metres)."""
    kx = _M_PER_DEG_LAT * math.cos(math.radians(lat))
    for coords in ways:
        for a, b in zip(coords, coords[1:], strict=False):
            ax, ay = (a[0] - lon) * kx, (a[1] - lat) * _M_PER_DEG_LAT
            bx, by = (b[0] - lon) * kx, (b[1] - lat) * _M_PER_DEG_LAT
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            u = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
            if math.hypot(ax + u * dx, ay + u * dy) <= TUNNEL_SNAP_M:
                return True
    return False


def _roads_z_range() -> tuple[float, float] | None:
    if not ROADS_3D.exists():
        return None
    fc = json.loads(ROADS_3D.read_text(encoding="utf-8"))
    zs = [c[2] for f in fc["features"] for c in f["geometry"]["coordinates"]]
    return min(zs), max(zs)


def check_meta(meta: object, *, scenario: str, fixture: bool) -> list[str]:
    """Contract errors in a parsed <scenario>.meta.json."""
    if not isinstance(meta, dict):
        return ["meta is not a JSON object"]
    errors: list[str] = []
    allowed = META_KEYS | OPTIONAL_META_KEYS | (FIXTURE_EXTRA_META_KEYS if fixture else set())
    if missing := META_KEYS - set(meta):
        errors.append(f"meta missing {sorted(missing)}")
    if extra := set(meta) - allowed:
        errors.append(f"meta has unexpected {sorted(extra)}")
    sc = meta.get("scenario")
    if not (isinstance(sc, str) and SCENARIO_RE.match(sc)):
        errors.append(f"scenario {sc!r} is not lowercase snake_case")
    elif sc != scenario:
        errors.append(f"meta.scenario {sc!r} does not match the file names ({scenario!r})")
    demand = meta.get("demand")
    if fixture:
        if demand != FIXTURE_DEMAND:
            errors.append(f"fixture demand is {demand!r}, expected {FIXTURE_DEMAND!r}")
        if meta.get("_test_fixture") is not True:
            errors.append("fixture meta lacks _test_fixture: true")
    elif demand not in DEMANDS:
        errors.append(f"demand {demand!r} not in {sorted(DEMANDS)}")
    d = meta.get("duration_s")
    if not (_num(d) and d > 0):
        errors.append(f"duration_s {d!r} is not a positive number")
    if not _int(meta.get("seed")):
        errors.append(f"seed {meta.get('seed')!r} is not an integer")
    sv = meta.get("sumo_version")
    if not (isinstance(sv, str) and sv):
        errors.append(f"sumo_version {sv!r} is not a non-empty string")
    for k in ("vehicles", "teleports", "tunnel_vehicles"):
        v = meta.get(k)
        if not (_int(v) and v >= 0):
            errors.append(f"{k} {v!r} is not a non-negative integer")
    ga = meta.get("generated_at")
    if not (isinstance(ga, str) and ISO_TZ_RE.match(ga)):
        errors.append(f"generated_at {ga!r} is not ISO 8601 with a timezone")
    else:
        try:
            datetime.fromisoformat(ga)
        except ValueError:
            errors.append(f"generated_at {ga!r} does not parse")
    return errors


def check_trips(
    trips: object,
    *,
    fixture: bool,
    duration_s: float | None = None,
    z_range: tuple[float, float] | None = None,
    max_errors: int = 30,
) -> list[str]:
    """Contract errors in a parsed trips_<scenario>.json (stops after max_errors)."""
    if not isinstance(trips, list):
        return ["top level is not an array"]
    if not trips:
        return ["no trips"]
    b = SIM_EXTRACT_BBOX
    west, south = b.west - BBOX_SLACK_DEG, b.south - BBOX_SLACK_DEG
    east, north = b.east + BBOX_SLACK_DEG, b.north + BBOX_SLACK_DEG
    tb = TUNNEL_EXTRACT_BBOX
    t_west, t_south = tb.west - BBOX_SLACK_DEG, tb.south - BBOX_SLACK_DEG
    t_east, t_north = tb.east + BBOX_SLACK_DEG, tb.north + BBOX_SLACK_DEG
    tunnel_ways = _tunnel_ways()
    zlo, zhi = PLAUSIBLE_Z
    if z_range is not None:
        zlo, zhi = z_range[0] - ROADS_Z_SLACK_M, z_range[1] + ROADS_Z_SLACK_M
    errors: list[str] = []
    ids: set[str] = set()
    for i, t in enumerate(trips):
        if len(errors) >= max_errors:
            break
        where = f"trip {i}"
        if not isinstance(t, dict):
            errors.append(f"{where}: not an object")
            continue
        if set(t) != TRIP_KEYS:
            errors.append(f"{where}: keys {sorted(t)} != {sorted(TRIP_KEYS)}")
            continue
        tid = t["id"]
        if not (isinstance(tid, str) and tid):
            errors.append(f"{where}: id {tid!r} is not a non-empty string")
        else:
            where = f"trip {tid!r}"
            if tid in ids:
                errors.append(f"{where}: duplicate id")
            ids.add(tid)
            if fixture and not tid.startswith(FIXTURE_ID_PREFIX):
                errors.append(f"{where}: fixture id lacks the {FIXTURE_ID_PREFIX!r} prefix")
            if not fixture and tid.startswith(FIXTURE_ID_PREFIX):
                errors.append(f"{where}: real export has a test-fixture id")
        if not (isinstance(t["type"], str) and t["type"]):
            errors.append(f"{where}: type {t['type']!r} is not a non-empty string")
        path, ts = t["path"], t["timestamps"]
        if not isinstance(path, list) or not isinstance(ts, list):
            errors.append(f"{where}: path / timestamps are not arrays")
            continue
        if len(path) < 2:
            errors.append(f"{where}: fewer than 2 positions")
        if len(path) != len(ts):
            errors.append(f"{where}: {len(path)} positions but {len(ts)} timestamps")
        for k, p in enumerate(path):
            if not (isinstance(p, list) and len(p) == 3 and all(_num(c) for c in p)):
                errors.append(f"{where}: position {k} {p!r} is not [lon, lat, z] of finite numbers")
                break
            lon, lat, z = p
            if not (west <= lon <= east and south <= lat <= north):
                in_tunnel_box = t_west <= lon <= t_east and t_south <= lat <= t_north
                if not (in_tunnel_box and _near_tunnel_way(lon, lat, tunnel_ways)):
                    errors.append(
                        f"{where}: position {k} {p!r} outside SIM_EXTRACT_BBOX and not on a "
                        "motorway tunnel way inside TUNNEL_EXTRACT_BBOX (lon/lat swapped?)"
                    )
                    break
            if not (zlo <= z <= zhi):
                errors.append(f"{where}: position {k} z {z} outside [{zlo:.2f}, {zhi:.2f}] m")
                break
        if not all(_num(s) for s in ts):
            errors.append(f"{where}: timestamps are not all finite numbers")
            continue
        if ts and ts[0] < 0:
            errors.append(f"{where}: negative timestamp {ts[0]}")
        bad = next((k for k in range(1, len(ts)) if not ts[k] > ts[k - 1]), None)
        if bad is not None:
            errors.append(
                f"{where}: timestamps not strictly increasing at index {bad} "
                f"({ts[bad - 1]} -> {ts[bad]})"
            )
        if duration_s is not None and ts and ts[-1] > duration_s + DURATION_SLACK_S:
            errors.append(f"{where}: last timestamp {ts[-1]} past duration_s {duration_s}")
    return errors


def check_pair(
    trips: object, meta: object, *, scenario: str, fixture: bool, z_range=None
) -> list[str]:
    errors = check_meta(meta, scenario=scenario, fixture=fixture)
    duration = (
        meta.get("duration_s") if isinstance(meta, dict) and _num(meta.get("duration_s")) else None
    )
    errors += check_trips(trips, fixture=fixture, duration_s=duration, z_range=z_range)
    if isinstance(trips, list) and isinstance(meta, dict):
        v, tv = meta.get("vehicles"), meta.get("tunnel_vehicles")
        # meta.vehicles counts distinct vehicles; a teleport splits one into "<id>", "<id>#1", …
        ids = [tr["id"] for tr in trips if isinstance(tr, dict) and isinstance(tr.get("id"), str)]
        distinct = len({re.sub(r"#\d+$", "", i) for i in ids})
        if _int(v) and v != distinct:
            errors.append(f"meta.vehicles {v} != {distinct} distinct vehicles in the file")
        if _int(v) and _int(tv) and not 0 <= tv <= v:
            errors.append(f"meta.tunnel_vehicles {tv} not in [0, vehicles {v}]")
    return errors


def _load(p: Path) -> object:
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------------
# Real exports (sim/out, + TRIPS_CONTRACT_DIRS)

REAL = _real_pairs()


@pytest.mark.skipif(not REAL, reason="no sim/out/trips_*.json exported yet")
@pytest.mark.parametrize(
    ("scenario", "trips_path", "meta_path"), REAL, ids=[f"{p[0]}:{p[1].parent}" for p in REAL]
)
def test_real_export_matches_contract(scenario: str, trips_path: Path, meta_path: Path):
    assert meta_path.exists(), f"{trips_path.name} has no {meta_path.name} beside it"
    text = trips_path.read_text(encoding="utf-8") + meta_path.read_text(encoding="utf-8")
    assert '"_test_fixture"' not in text and '"test_fixture"' not in text, (
        "test-fixture labels in a real export"
    )
    errors = check_pair(
        _load(trips_path),
        _load(meta_path),
        scenario=scenario,
        fixture=False,
        z_range=_roads_z_range(),
    )
    assert errors == [], "\n".join(errors)


def test_every_real_meta_has_its_trips():
    for d in [SIM_OUT] + [
        Path(x) for x in os.environ.get("TRIPS_CONTRACT_DIRS", "").split(os.pathsep) if x
    ]:
        for meta in d.glob("*.meta.json") if d.is_dir() else []:
            scenario = meta.name.removesuffix(".meta.json")
            assert (d / f"trips_{scenario}.json").exists(), f"{meta} has no trips_{scenario}.json"


# ---------------------------------------------------------------------------------------------
# The labelled fixture (always present): proves the checks run on a real-shaped file


@pytest.fixture(scope="module")
def fixture_pair() -> tuple[list, dict]:
    return _load(FIXTURE_TRIPS), _load(FIXTURE_META)  # type: ignore[return-value]


def test_fixture_matches_contract(fixture_pair):
    trips, meta = fixture_pair
    assert check_pair(trips, meta, scenario="fixture", fixture=True) == []


def test_fixture_is_labelled_and_as_described(fixture_pair):
    trips, meta = fixture_pair
    assert meta["demand"] == FIXTURE_DEMAND and meta["_test_fixture"] is True
    assert any("TEST FIXTURE" in n for n in meta["_notes"])
    assert any("real roads_3d depth (-30 m, estimated in roads_3d)" in n for n in meta["_notes"])
    assert not any("FAKE" in n for n in meta["_notes"])
    assert all(t["id"].startswith(FIXTURE_ID_PREFIX) for t in trips)
    # Stage B fixture: 40 cars, 7,953 points, 0-300 s, 2 tunnel cars at real roads_3d depth (-30 m).
    assert len(trips) == meta["vehicles"] == 40
    assert sum(len(t["path"]) for t in trips) == 7953
    assert min(t["timestamps"][0] for t in trips) == 0
    assert max(t["timestamps"][-1] for t in trips) <= meta["duration_s"] == 300
    below = [t["id"] for t in trips if min(p[2] for p in t["path"]) < -0.5]
    assert len(below) == meta["tunnel_vehicles"] == 2
    assert all("domain-tunnel" in i for i in below)
    assert min(p[2] for t in trips for p in t["path"]) == pytest.approx(-30, abs=0.6)


# ---------------------------------------------------------------------------------------------
# Negative cases: every rule fires on a minimal broken copy of a valid pair


def _valid() -> tuple[list, dict]:
    trips = [
        {
            "id": "veh0",
            "type": "passenger",
            "path": [[144.96, -37.818, 0.0], [144.961, -37.8177, 0.0], [144.962, -37.8174, 0.0]],
            "timestamps": [0.0, 4.5, 9.0],
        }
    ]
    meta = {
        "scenario": "baseline",
        "demand": "placeholder_random",
        "duration_s": 900,
        "seed": 42,
        "sumo_version": "1.28.0",
        "vehicles": 1,
        "teleports": 0,
        "tunnel_vehicles": 0,
        "generated_at": "2026-10-26T14:16:33Z",
    }
    return trips, meta


def _errs(mutate) -> list[str]:
    trips, meta = copy.deepcopy(_valid())
    mutate(trips, meta)
    return check_pair(trips, meta, scenario="baseline", fixture=False)


def test_valid_minimal_pair_passes():
    trips, meta = _valid()
    assert check_pair(trips, meta, scenario="baseline", fixture=False) == []


@pytest.mark.parametrize(
    ("name", "mutate", "pattern"),
    [
        ("extra trip key", lambda t, m: t[0].update(speed=3), r"keys"),
        ("missing trip key", lambda t, m: t[0].pop("type"), r"keys"),
        (
            "2D positions",
            lambda t, m: t[0].update(path=[p[:2] for p in t[0]["path"]]),
            r"not \[lon, lat, z\]",
        ),
        (
            "lat/lon swapped",
            lambda t, m: t[0].update(path=[[p[1], p[0], p[2]] for p in t[0]["path"]]),
            r"outside",
        ),
        (
            "outside bbox",
            lambda t, m: t[0]["path"][1].__setitem__(0, 144.99),
            r"outside SIM_EXTRACT_BBOX",
        ),
        (
            "tunnel-box point not on a tunnel way",
            # inside TUNNEL_EXTRACT_BBOX east of SIM_EXTRACT_BBOX, but on no motorway tunnel way
            lambda t, m: t[0]["path"][1].__setitem__(0, 144.9895),
            r"not on a motorway tunnel way",
        ),
        ("bool in position", lambda t, m: t[0]["path"][0].__setitem__(2, True), r"finite numbers"),
        ("NaN z", lambda t, m: t[0]["path"][0].__setitem__(2, float("nan")), r"finite numbers"),
        ("absurd z", lambda t, m: t[0]["path"][0].__setitem__(2, 120.0), r"z 120"),
        ("length mismatch", lambda t, m: t[0]["timestamps"].pop(), r"3 positions but 2"),
        (
            "equal timestamps",
            lambda t, m: t[0].update(timestamps=[0.0, 4.5, 4.5]),
            r"strictly increasing",
        ),
        ("backwards", lambda t, m: t[0].update(timestamps=[0.0, 9.0, 4.5]), r"strictly increasing"),
        ("negative time", lambda t, m: t[0].update(timestamps=[-1.0, 4.5, 9.0]), r"negative"),
        ("past duration", lambda t, m: m.update(duration_s=5), r"past duration_s"),
        (
            "duplicate id",
            lambda t, m: (t.append(copy.deepcopy(t[0])), m.update(vehicles=2)),
            r"duplicate id",
        ),
        ("empty id", lambda t, m: t[0].update(id=""), r"id ''"),
        ("int id", lambda t, m: t[0].update(id=7), r"id 7"),
        (
            "fixture id in a real export",
            lambda t, m: t[0].update(id="fixture-x"),
            r"test-fixture id",
        ),
        ("vehicles mismatch", lambda t, m: m.update(vehicles=2), r"meta.vehicles 2 != 1"),
        (
            "tunnel_vehicles > vehicles",
            lambda t, m: m.update(tunnel_vehicles=3),
            r"tunnel_vehicles 3",
        ),
        ("missing meta key", lambda t, m: m.pop("teleports"), r"missing \['teleports'\]"),
        ("extra meta key", lambda t, m: m.update(note="x"), r"unexpected \['note'\]"),
        ("fixture label in real meta", lambda t, m: m.update(_test_fixture=True), r"unexpected"),
        ("unknown demand", lambda t, m: m.update(demand="guessed"), r"demand 'guessed'"),
        (
            "test_fixture demand in real meta",
            lambda t, m: m.update(demand="test_fixture"),
            r"demand 'test_fixture'",
        ),
        ("scenario not snake", lambda t, m: m.update(scenario="AM-Peak"), r"snake_case"),
        ("scenario vs file name", lambda t, m: m.update(scenario="am_peak"), r"does not match"),
        ("zero duration", lambda t, m: m.update(duration_s=0), r"duration_s 0"),
        ("bool seed", lambda t, m: m.update(seed=True), r"seed True"),
        ("float seed", lambda t, m: m.update(seed=4.2), r"seed 4.2"),
        ("empty sumo_version", lambda t, m: m.update(sumo_version=""), r"sumo_version"),
        ("negative teleports", lambda t, m: m.update(teleports=-1), r"teleports -1"),
        ("float vehicles", lambda t, m: m.update(vehicles=1.0), r"vehicles 1.0"),
        (
            "naive generated_at",
            lambda t, m: m.update(generated_at="2026-10-26T14:16:33"),
            r"timezone",
        ),
        ("date-only generated_at", lambda t, m: m.update(generated_at="2026-10-26"), r"timezone"),
    ],
    ids=lambda x: x if isinstance(x, str) else "",
)
def test_rule_fires(name: str, mutate, pattern: str):
    errors = _errs(mutate)
    assert any(re.search(pattern, e) for e in errors), f"{name}: {errors}"


def test_tunnel_way_beyond_sim_bbox_is_allowed():
    """A sample on the Burnley tunnel east of SIM_EXTRACT_BBOX is fine; 50 m off it is not."""
    ways = _tunnel_ways()
    assert ways, "roads_3d.geojson has no motorway tunnel ways"
    east = SIM_EXTRACT_BBOX.east + 2 * BBOX_SLACK_DEG
    beyond = [
        (a, b) for w in ways for a, b in zip(w, w[1:], strict=False) if a[0] > east and b[0] > east
    ]
    assert beyond, "no motorway tunnel segment east of SIM_EXTRACT_BBOX"
    a, b = beyond[0]
    mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, -30.0]
    trips, meta = _valid()
    trips[0]["path"][1] = mid
    assert check_pair(trips, meta, scenario="baseline", fixture=False) == []
    off = [mid[0], mid[1] + 50 / _M_PER_DEG_LAT, -30.0]
    trips[0]["path"][1] = off
    errors = check_pair(trips, meta, scenario="baseline", fixture=False)
    assert any("not on a motorway tunnel way" in e for e in errors), errors


def test_z_outside_roads_range_fires_for_real_runs():
    trips, meta = _valid()
    trips[0]["path"][1][2] = -12.0  # a depth roads_3d doesn't have
    errors = check_pair(trips, meta, scenario="baseline", fixture=False, z_range=(0.0, 32.93))
    assert any("outside [-0.05, 32.98]" in e for e in errors), errors


def test_fixture_rules_fire():
    trips, meta = _valid()
    errors = check_pair(trips, meta, scenario="baseline", fixture=True)
    assert any("fixture id lacks" in e for e in errors)
    assert any("fixture demand" in e for e in errors)
    assert any("_test_fixture" in e for e in errors)
