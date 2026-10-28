"""Measured heights in roads_3d.geojson vs data/processed/checks/deck_pointcloud.json.

Written by qa-reviewer, independently of pipeline.roads_3d / pipeline.deck_pointcloud (imports
neither). The report is committed, so a missing report FAILS here. The raw point-cloud tiles are
not committed (data/raw/ is gitignored); the checks that read them skip when they are absent.

PLAN.md Decisions log 2026-10-26: the City of Melbourne 3D Point Cloud 2018 (CC BY 4.0) is a
measured height source (`z_source: measured`); "measured always wins": estimates bend around
measurements, never the reverse. Calibrated separations and deck thickness are medians of the
measurements of the build, not constants.

What it checks:
- licence + attribution text, and the attribution matches data/SOURCES.md exactly;
- summary counts add up to the records;
- calibration (separation medians and n per class, decks with their own median) recomputed here
  from the used road-over-road records and roads_3d's `highway` of the upper way;
- every used crossing measurement holds in roads_3d (upper minus lower at the crossing point is
  the measured separation), every used open-deck profile holds at every vertex, every listed
  anchor is at its measured height in roads_3d, and `precedence.anchors_moved == 0`;
- `z_source` follows the orchestrator's half rule (2026-10-26) as far as the report allows, for
  raised decks and (Stage B) for the CityLink cutting lowered to its measured depth;
- Stage B: crossing records over the cutting measured against street level
  (`street_level_ref`) are compared with the deck's height above street level (z = 0 in the flat
  model), not with the distance to the lowered cutting floor; `trench_crossings` agree with the
  drawn heights (deck to cutting floor = the measured separation to the floor), all clear 4 m with
  the right order, and nothing is left in `pending_trench_depth`; every cutting-profile vertex is
  drawn at its measured depth, or listed in `below_ground.pins_demoted` with its drawn value;
- `inputs_sha256` matches the current files;
- the tiles named in the report are the tiles over SIM_EXTRACT_BBOX in the published tile index,
  with the sha256 recorded in MANIFEST.json (only when data/raw/ has them).

Tolerance MEASURED_TOL_M = 0.05 m: roads_3d stores z to 0.01 m and lon/lat to 1e-6 deg (<= 0.11 m,
i.e. <= 0.01 m of z at the <= 8 % grades on each of the two ways); the report rounds separations
and heights to 0.01 m. Together <= 0.04 m.

Known issues (Stage A, accepted by the orchestrator): three dropped pins are drawn > 1 m below
their measurement (DROPPED_PIN_EXCEPTIONS); any new one fails.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from collections import Counter
from pathlib import Path

import pytest

from pipeline.config import SIM_EXTRACT_BBOX

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "data" / "processed" / "checks" / "deck_pointcloud.json"
ROADS_3D = ROOT / "data" / "processed" / "roads_3d.geojson"
SOURCES = ROOT / "data" / "SOURCES.md"
PC_DIR = ROOT / "data" / "raw" / "com_pointcloud_2018"

MEASURED_TOL_M = 0.05
# Cutting depths below the point cloud's stated 0.25 m absolute accuracy are not pinned.
CUTTING_MIN_DEPTH_M = 0.25
# Stage B: a measured cutting floor the grade limits can't hold is drawn this close at most
# (data-engineer's report: <= 0.36 m).
DEMOTED_TOL_M = 0.4
# "binding" = the stored z equals the measurement up to storage rounding (z to 0.01 m)
BINDING_TOL_M = 0.015
OVERRIDE_TOL_M = 0.5  # two x the stated 0.25 m absolute accuracy of the point cloud
FREEWAY = {"motorway", "trunk", "motorway_link", "trunk_link"}
MIN_N = 5
M_PER_DEG = 111_320.0


@pytest.fixture(scope="module")
def report() -> dict:
    assert REPORT.exists(), "deck_pointcloud.json missing (reports are committed)"
    return json.loads(REPORT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def roads() -> dict[int, dict]:
    fc = json.loads(ROADS_3D.read_text(encoding="utf-8"))
    return {f["properties"]["way_id"]: f for f in fc["features"]}


def _z_at(line: list[list[float]], lon: float, lat: float) -> float:
    kx = M_PER_DEG * math.cos(math.radians(lat))
    px, py = lon * kx, lat * M_PER_DEG
    best = (math.inf, 0.0)
    for (x0, y0, z0), (x1, y1, z1) in zip(line, line[1:], strict=False):
        ax, ay, bx, by = x0 * kx, y0 * M_PER_DEG, x1 * kx, y1 * M_PER_DEG
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
        d = math.hypot(ax + t * dx - px, ay + t * dy - py)
        if d < best[0]:
            best = (d, z0 + t * (z1 - z0))
    return best[1]


def _used(report: dict, kind: str) -> list[dict]:
    return [r for r in report["records"] if r["kind"] == kind and r.get("used")]


def _dz(r: dict, roads: dict) -> float:
    """The drawn counterpart of a crossing record's `separation_m`: deck minus lower road, or,
    for a record measured against street level (`street_level_ref`, a deck over the CityLink
    cutting, Stage B), deck minus street level (z = 0 in the flat model)."""
    up = roads[r["upper_way"]]["geometry"]["coordinates"]
    if r.get("street_level_ref"):
        return _z_at(up, r["lon"], r["lat"])
    lo = roads[r["lower_way"]]["geometry"]["coordinates"]
    return _z_at(up, r["lon"], r["lat"]) - _z_at(lo, r["lon"], r["lat"])


# --- report shape ---------------------------------------------------------------------------------


def test_licence_and_attribution(report: dict):
    assert "OpenStreetMap" in report["licence"] and "ODbL" in report["licence"]
    assert "City of Melbourne" in report["licence"] and "CC BY 4.0" in report["licence"]
    attribution = report["attribution"]
    assert "City of Melbourne" in attribution and "CC BY 4.0" in attribution
    assert attribution in SOURCES.read_text(encoding="utf-8"), "attribution differs from SOURCES.md"


def test_sources_row_for_the_point_cloud():
    text = SOURCES.read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if "3D Point Cloud 2018" in line and "|" in line]
    assert rows, "no SOURCES.md row for the City of Melbourne 3D Point Cloud 2018"
    row = rows[0]
    needles = ("CC BY 4.0", "data.melbourne.vic.gov.au", "Photogrammetry", "exception", "2026-")
    for needle in needles:
        assert needle in row, needle


def test_summary_counts_match_records(report: dict):
    got: Counter = Counter()
    for r in report["records"]:
        got[f"{r['kind']}_{'used' if r.get('used') else 'not_used'}"] += 1
        if not r.get("used"):
            got[f"reason: {r.get('reason')}"] += 1
    assert dict(got) == report["summary"]


def test_records_inside_extract_and_ways_exist(report: dict, roads: dict):
    b = SIM_EXTRACT_BBOX
    for r in report["records"]:
        assert b.west <= r["lon"] <= b.east and b.south <= r["lat"] <= b.north, r
        assert r["upper_way"] in roads, r["upper_way"]
        if r["kind"] == "crossing" and not r["lower_is_rail"]:
            assert r["lower_way"] in roads, r["lower_way"]


# --- calibration is derived from the measurements -------------------------------------------------


def test_calibration_is_the_median_of_the_used_measurements(report: dict, roads: dict):
    used = [r for r in _used(report, "crossing") if not r["lower_is_rail"]]
    seps = [r["separation_m"] for r in used]

    def is_fw(r: dict) -> bool:
        return roads[r["upper_way"]]["properties"]["highway"] in FREEWAY

    fw = [r["separation_m"] for r in used if is_fw(r)]
    other = [r["separation_m"] for r in used if not is_fw(r)]
    cal = report["calibration"]

    def med(xs: list[float]) -> float | None:
        return round(statistics.median(xs), 2) if len(xs) >= MIN_N else None

    assert cal["min_n"] == MIN_N
    assert cal["separation_global_n"] == len(seps) and cal["separation_global_m"] == med(seps)
    assert cal["separation_freeway_n"] == len(fw) and cal["separation_freeway_m"] == med(fw)
    assert cal["separation_other_n"] == len(other) and cal["separation_other_m"] == med(other)
    assert cal["decks_with_own_median"] == len({r["upper_way"] for r in used})
    # Deck thickness (orchestrator decision, Stage A round 3): from ALL measured point-cloud /
    # maxheight pairs, i.e. every valid point-cloud separation over a road with a numeric
    # maxheight, used as a pin or kept as a lower bound ("contradicts neighbouring
    # measurements"). The report records maxheight + the 1.2 m constant (DECK_THICKNESS_M) as
    # `maxheight_separation_m`, so clearance = that - 1.2.
    pairs = [
        r["separation_m"] - (r["maxheight_separation_m"] - 1.2)
        for r in report["records"]
        if r["kind"] == "crossing"
        and not r["lower_is_rail"]
        and r.get("maxheight_separation_m") is not None
        and r.get("separation_m") is not None
        and (r.get("used") or "contradicts neighbouring measurements" in (r.get("reason") or ""))
    ]
    assert cal["deck_thickness_global_n"] == len(pairs)
    assert cal["deck_thickness_global_m"] == (med(pairs) if len(pairs) >= MIN_N else 1.2)
    assert report["layer_floors"]["separation_per_layer_m"] == (
        cal["separation_freeway_m"] or cal["separation_global_m"]
    )


# --- measured precedence -------------------------------------------------------------------------


def test_measured_crossings_hold_in_roads_3d(report: dict, roads: dict):
    under = []
    for r in _used(report, "crossing"):
        dz = _dz(r, roads)
        if dz < r["separation_m"] - MEASURED_TOL_M:
            under.append((r["upper_way"], r["lower_way"], r["separation_m"], round(dz, 2)))
    assert not under, f"{len(under)} measured separations undercut in roads_3d: {under[:5]}"


def test_measured_open_decks_hold_in_roads_3d(report: dict, roads: dict):
    under = []
    for r in _used(report, "open_deck"):
        coords = roads[r["upper_way"]]["geometry"]["coordinates"]
        assert len(coords) == len(r["heights_m"]), r["upper_way"]
        for c, h in zip(coords, r["heights_m"], strict=True):
            if c[2] < h - MEASURED_TOL_M:
                under.append((r["upper_way"], h, c[2]))
    assert not under, f"{len(under)} open-deck vertices below their measurement: {under[:5]}"


def _anchor_positions(report: dict, roads: dict) -> set[tuple[float, float]]:
    out = set()
    for a in report["anchors"]:
        c = roads[a["way"]]["geometry"]["coordinates"][a["vertex_index"]]
        out.add((c[0], c[1]))
    return out


def test_anchors_hold_their_measured_value(report: dict, roads: dict):
    """Every vertex held by a measurement (report `anchors`) is at its measured height, and the
    report's final height is the one stored in roads_3d."""
    anchors = report["anchors"]
    assert anchors, "no anchors listed"
    off = []
    for a in anchors:
        z = roads[a["way"]]["geometry"]["coordinates"][a["vertex_index"]][2]
        if (
            abs(a["final_z_m"] - z) > 0.005
            or abs(a["final_z_m"] - a["measured_z_m"]) > BINDING_TOL_M
        ):
            off.append((a, z))
    assert not off, f"{len(off)} anchors not at their measurement: {off[:5]}"
    assert report["precedence"]["anchors_moved"] == 0


def test_measured_label_follows_the_half_rule(report: dict, roads: dict):
    """Orchestrator decision 2026-10-26: `measured` only when >= half of a way's raised vertices
    are held by measurements, else `estimated` even if one measurement binds. The anchors are a
    subset of the held vertices (held also counts deck vertices at a measured cap and between two
    held vertices on a deck chain, which the report does not list), so: a way with >= half of its
    raised vertices anchored must be `measured`, and a `measured` way must be raised."""
    pos = _anchor_positions(report, roads)
    cutting = {r["way"] for r in report["cutting_profiles"] if r["used"]}
    wrong = []
    for w, f in roads.items():
        c = f["geometry"]["coordinates"]
        raised = [x for x in c if x[2] > 0]
        zs = f["properties"]["z_source"]
        if zs == "measured" and not raised and w not in cutting:
            wrong.append((w, "measured but neither raised nor a measured cutting"))
        if raised:
            share = sum((x[0], x[1]) in pos for x in raised) / len(raised)
            if share >= 0.5 and zs != "measured":
                wrong.append((w, zs, round(share, 2)))
    assert not wrong, wrong[:10]


CROSSING_PIN_NEAR_M = 12.0  # a crossing's floor pin holds the vertices next to it (<= 10 m apart)


def _cutting_held(report: dict, roads: dict, tol: float) -> dict[int, tuple[int, int, int]]:
    """way -> (vertices off street level (z != 0, "raised" in Stage B's half rule), of those
    drawn at their measured profile depth within `tol`, of the rest those within
    CROSSING_PIN_NEAR_M of a measured trench crossing on that way (the floor depth measured at
    the crossing, which the report does not list per vertex))."""
    out = {}
    for r in report["cutting_profiles"]:
        if not r["used"]:
            continue
        w = r["way"]
        c = roads[w]["geometry"]["coordinates"]
        assert len(c) == len(r["depths_m"]) == len(r["measured"]), w
        off = [i for i, x in enumerate(c) if x[2] != 0]
        held = {
            i
            for i in off
            if r["measured"][i]
            and r["depths_m"][i] is not None
            and abs(c[i][2] + r["depths_m"][i]) <= tol
        }
        near = {
            i
            for i in off
            if i not in held
            and any(
                t["lower_way"] == w
                and t["measured"]
                and _metres(c[i], [t["lon"], t["lat"]]) <= CROSSING_PIN_NEAR_M
                for t in report["trench_crossings"]
            )
        }
        out[w] = (len(off), len(held), len(near))
    return out


def _metres(a: list[float], b: list[float]) -> float:
    k = math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * k, b[1] - a[1]) * M_PER_DEG


def test_cutting_label_follows_the_half_rule(report: dict, roads: dict):
    """data/SOURCES.md + pipeline docstring (Stage B): a cutting way is `measured` where at least
    half of its vertices off street level are held by measurements (its point-cloud profile, or
    the floor depth measured at a crossing). Bounds from the report: a way with >= half of them
    at their profile depth (binding, BINDING_TOL_M) must be `measured`; a `measured` way has
    >= half at their profile depth (MEASURED_TOL_M) or next to a measured crossing."""
    strict = _cutting_held(report, roads, BINDING_TOL_M)
    loose = _cutting_held(report, roads, MEASURED_TOL_M)
    assert strict, "no used cutting profile"
    wrong = []
    for w, (n_off, n_held, _) in strict.items():
        zs = roads[w]["properties"]["z_source"]
        if n_off and 2 * n_held >= n_off and zs != "measured":
            wrong.append((w, zs, "held", n_held, "of", n_off))
        n_off2, n_held2, n_near2 = loose[w]
        if zs == "measured" and 2 * (n_held2 + n_near2) < n_off2:
            wrong.append((w, zs, "held at most", n_held2 + n_near2, "of", n_off2))
    assert not wrong, wrong
    measured = {w for w in strict if roads[w]["properties"]["z_source"] == "measured"}
    assert len(measured) >= 5, sorted(measured)  # data-engineer's report: 5 ways


def test_cutting_profiles_drawn_at_their_measured_depth(report: dict, roads: dict):
    """Every measured cutting vertex is drawn at -depth, unless the grade limits can't hold it:
    then it is listed in `below_ground.pins_demoted` with its drawn value, <= DEMOTED_TOL_M off,
    and never deeper than measured."""
    demoted = {(d["lon"], d["lat"]): d for d in report["below_ground"]["pins_demoted"]}
    off, unlisted, n = [], [], 0
    for r in report["cutting_profiles"]:
        if not r["used"]:
            continue
        c = roads[r["way"]]["geometry"]["coordinates"]
        for i, (x, d, m) in enumerate(zip(c, r["depths_m"], r["measured"], strict=True)):
            if not m or d is None or d < CUTTING_MIN_DEPTH_M:
                continue  # unmeasured, or within the cloud's accuracy of street level
            n += 1
            if abs(x[2] + d) <= MEASURED_TOL_M:
                continue
            dem = demoted.get((x[0], x[1]))
            if dem is None:
                unlisted.append((r["way"], i, -d, x[2]))
                continue
            assert dem["measured_z_m"] == pytest.approx(-d, abs=0.011), (r["way"], i, dem)
            assert dem["final_z_m"] == pytest.approx(x[2], abs=0.011), (r["way"], i, dem)
            if not (x[2] >= -d - MEASURED_TOL_M and abs(x[2] + d) <= DEMOTED_TOL_M):
                off.append((r["way"], i, -d, x[2]))
    assert n >= 50, n
    assert not unlisted, f"cutting vertices off their measurement and not listed: {unlisted[:5]}"
    assert not off, f"demoted cutting vertices too far from their measurement: {off[:5]}"


def test_estimated_ways_with_anchors_exist(report: dict, roads: dict):
    """The half rule is in force: some ways have a binding measurement but stay `estimated`."""
    pos = _anchor_positions(report, roads)
    some = [
        w
        for w, f in roads.items()
        if f["properties"]["z_source"] == "estimated"
        and any((x[0], x[1]) in pos for x in f["geometry"]["coordinates"] if x[2] > 0)
    ]
    assert some


def test_used_crossings_are_at_their_measured_value(report: dict, roads: dict):
    """Pins: each used point-cloud separation holds in roads_3d to storage rounding."""
    off = []
    for r in _used(report, "crossing"):
        d = _dz(r, roads) - r["separation_m"]
        if abs(d) > MEASURED_TOL_M:
            off.append((r["upper_way"], r["lower_way"], r["separation_m"], round(d, 3)))
    assert not off, f"{len(off)} used separations not held: {off[:5]}"


def test_measured_crossings_not_overridden(report: dict, roads: dict):
    """Fixed after qa's Stage A review (was strict-xfail): no used measurement is exceeded."""
    over = []
    for r in _used(report, "crossing"):
        excess = _dz(r, roads) - r["separation_m"]
        if excess > OVERRIDE_TOL_M:
            over.append((r["upper_way"], r["upper_name"], r["lower_way"], round(excess, 2)))
    over.sort(key=lambda x: -x[3])
    assert not over, f"{len(over)} measured crossings raised above their measurement: {over[:5]}"


def _trench_dz(t: dict, roads: dict) -> tuple[float, float]:
    """(drawn deck minus cutting floor, measured separation to the floor)."""
    up = roads[t["upper_way"]]["geometry"]["coordinates"]
    lo = roads[t["lower_way"]]["geometry"]["coordinates"]
    dz = _z_at(up, t["lon"], t["lat"]) - _z_at(lo, t["lon"], t["lat"])
    return dz, t.get("separation_to_lower_surface_m", math.nan)


def _used_pairs(report: dict) -> set[tuple[int, int]]:
    return {(r["upper_way"], r["lower_way"]) for r in _used(report, "crossing")}


def test_nothing_pending_trench_depth(report: dict):
    """Stage B lowered the cutting (data/SOURCES.md): `pending_trench_depth` is empty."""
    assert report["pending_trench_depth"] == []


def test_trench_records_fit_roads_3d(report: dict, roads: dict):
    """Stage B `trench_crossings`: the record's `cutting_z_m` and `dz_m` are the drawn heights;
    every crossing clears 4 m with the deck on top; where the crossing's street-level pin is in
    use, the deck stands its measured separation above the cutting floor, as drawn."""
    trenches = report["trench_crossings"]
    assert len(trenches) >= 9  # the 9 Stage A pending crossings at least
    used = _used_pairs(report)
    checked = 0
    for t in trenches:
        assert 2.0 <= t["trench_depth_m"] <= 15.0, t
        lo = roads[t["lower_way"]]["geometry"]["coordinates"]
        assert _z_at(lo, t["lon"], t["lat"]) == pytest.approx(t["cutting_z_m"], abs=0.05), t
        dz, want = _trench_dz(t, roads)
        assert dz == pytest.approx(t["dz_m"], abs=0.05), t
        assert dz >= 4.0 - 0.03, (t, round(dz, 2))
        if t["measured"] and (t["upper_way"], t["lower_way"]) in used:
            # the deck's street-level pin holds exactly (test_used_crossings_...); the floor is
            # the measured trench depth, or as close as the grade limits allow (demoted)
            assert abs(dz - want) <= DEMOTED_TOL_M, (t, round(dz, 2))
            checked += 1
    assert checked >= 5


@pytest.mark.xfail(
    strict=True,
    reason="qa Stage B round 1: trench_crossings record 26619046 over 5234953 inherits "
    "trench_depth_m 6.06 from a measurement 132 m away while the cutting there is drawn at "
    "-0.55 m (the record's own cutting_depth_at_crossing_m is 0.54); report-only inconsistency",
)
def test_trench_depth_matches_the_cutting_drawn_there(report: dict):
    """A trench record's `trench_depth_m` describes the cutting at that crossing: within 0.5 m
    of the drawn cutting depth (-cutting_z_m)."""
    off = [
        (t["upper_way"], t["lower_way"], t["trench_depth_m"], t["cutting_z_m"])
        for t in report["trench_crossings"]
        if abs(t["trench_depth_m"] + t["cutting_z_m"]) > 0.5
    ]
    assert not off, off


def test_measured_trench_decks_drawn_near_their_measurement(report: dict, roads: dict):
    """Fixed in Stage A round 3 (was strict-xfail: 50123940 drawn 10.1 m at street level).
    Stage B: every measured trench crossing, used or demoted, within 1 m of its separation to
    the cutting floor."""
    far = []
    for t in report["trench_crossings"]:
        if t["measured"]:
            dz, want = _trench_dz(t, roads)
            if abs(dz - want) > 1.0:
                far.append((t["upper_way"], t["lower_way"], round(want, 2), round(dz, 2)))
    assert not far, far


# Known issues (orchestrator decision, Stage A round 3): contradicting pins are kept as lower
# bounds (`pointcloud_soft`); these three are drawn > 1 m below their measurement.
#   5234995 over 154297089: flat model ground (qa reading under the stacked decks unreliable)
#   38313389 over City Road 39452127: flat model ground
#   1067558051 over 959708837: the lower way is a post-2018 West Gate Tunnel lane
DROPPED_PIN_EXCEPTIONS = {(5234995, 154297089), (38313389, 39452127), (1067558051, 959708837)}


def test_dropped_pins_stay_near_their_measurement(report: dict, roads: dict):
    """A pin dropped as 'contradicts neighbouring measurements' is drawn within 1 m of what the
    cloud measured, except the listed known issues; a new exception fails."""
    far = set()
    for p in report["precedence"]["pins_dropped"]:
        recs = [
            r
            for r in report["records"]
            if r["kind"] == "crossing"
            and r["upper_way"] == p["upper"]
            and r["lower_way"] == p["lower"]
        ]
        if abs(_dz(recs[0], roads) - p["separation_m"]) > 1.0:
            far.add((p["upper"], p["lower"]))
    new, fixed = sorted(far - DROPPED_PIN_EXCEPTIONS), sorted(DROPPED_PIN_EXCEPTIONS - far)
    assert far == DROPPED_PIN_EXCEPTIONS, f"new: {new}, fixed: {fixed}"


# --- inputs ---------------------------------------------------------------------------------------


def _tiles_over_sim_extract(kml: str) -> list[str]:
    b = SIM_EXTRACT_BBOX
    keep = []
    for pm in re.findall(r"<Placemark>(.*?)</Placemark>", kml, flags=re.S):
        name = re.search(r"<name>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</name>", pm, flags=re.S)
        coords = re.search(r"<coordinates>(.*?)</coordinates>", pm, flags=re.S)
        if not name or not coords:
            continue
        pts = [tuple(float(v) for v in t.split(",")[:2]) for t in coords.group(1).split()]
        lons, lats = [p[0] for p in pts], [p[1] for p in pts]
        lon_ok = min(lons) <= b.east and max(lons) >= b.west
        if lon_ok and min(lats) <= b.north and max(lats) >= b.south:
            keep.append(f"{name.group(1).strip()}.las")
    return sorted(keep)


def test_inputs_sha256_match_current_files(report: dict):
    shas = report["inputs_sha256"]
    for key in ("data/processed/roads_3d.geojson", "data/processed/osm_sim_area.osm.gz"):
        assert key in shas, key
    for key, sha in shas.items():
        path = ROOT / key
        if key.startswith("data/raw/") and not path.exists():
            continue  # raw downloads live only in the main checkout
        assert hashlib.sha256(path.read_bytes()).hexdigest() == sha, f"{key} changed since"


def test_tiles_read_are_the_sim_extract_tiles_with_manifest_hashes(report: dict):
    index, manifest = PC_DIR / "Tile_Index.KML", PC_DIR / "MANIFEST.json"
    if not (index.exists() and manifest.exists()):
        pytest.skip("point-cloud tiles not in this checkout's data/raw/")
    want = _tiles_over_sim_extract(index.read_text(encoding="iso-8859-1"))
    tiles = report["tiles_read"]
    assert sorted(t["tile"] for t in tiles) == want
    man = json.loads(manifest.read_text())
    for t in tiles:
        assert t["sha256"] == man[t["tile"]]["sha256"], t["tile"]
        path = PC_DIR / t["tile"]
        assert path.stat().st_size == man[t["tile"]]["bytes"], t["tile"]
    # hash two tiles in full (all 75 would read ~3.5 GB)
    for t in sorted(tiles, key=lambda t: man[t["tile"]]["bytes"])[:2]:
        h = hashlib.sha256((PC_DIR / t["tile"]).read_bytes()).hexdigest()
        assert h == t["sha256"], t["tile"]
