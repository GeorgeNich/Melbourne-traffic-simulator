"""Contract test for sim/out/edge_stats_<scenario>.parquet (Phase 3A).

Written by qa-reviewer, independently of sim/ (it imports nothing from sim/: the network is read by
tests/contracts/roads_net_helpers.py, SUMO's outputs with ElementTree). Every scenario with a stats
file in sim/out/ is checked.

CLAUDE.md "Data contracts": `edge_id`, `t_bin` (60 s), `mean_speed_mps`, `speed_limit_mps`,
`speed_ratio`, `vehicle_count`; edge ids match roads.net.xml; speeds in m/s;
`speed_ratio = mean_speed_mps / speed_limit_mps`. Documented by the writer (sim/edge_stats.py) and
checked here: a missing (edge_id, t_bin) means "no vehicle on the edge in that minute, no data";
there is no NaN or null anywhere.

What it checks:
- Exactly the six columns, in order, typed string / int32 / float64 / float64 / float64 / int32;
  no nulls; every float finite.
- (edge_id, t_bin) unique; t_bin a multiple of 60 in [0, meta.duration_s) (seconds from scenario
  start, the clock of trips_<scenario>.json).
- edge_id is a non-internal car edge of roads.net.xml.
- speed_limit_mps is the edge's lane speed in roads.net.xml (scenarios without time-of-day speed
  overrides: baseline), mean_speed_mps >= 0 and below MAX_RATIO x the limit, speed_ratio =
  mean / limit to 4 decimals, vehicle_count >= 1.
- Raw SUMO cross-check (only where sim/work/<scenario>/edgestats60.xml exists, i.e. after a local
  `sim.run`): every non-internal edge with sampledSeconds > 0 in a recording bin has a row, every
  row has that SUMO speed, and vehicle_count is the carried-forward balance
  (on + departed + entered, at least 1).
- Independent FCD recompute (only where sim/work/<scenario>/fcd.xml exists): per edge and bin, the
  time-mean speed of the vehicles whose FRONT is on the edge (FCD every step) agrees with
  mean_speed_mps (median and 95th percentile limits, rows with >= 10 vehicle-seconds; SUMO also
  counts the back of a car still overlapping the edge, so a few rows differ more); vehicle_count is
  never below the distinct FCD vehicles; every edge-bin FCD sees for more than one step has a row.
"""

from __future__ import annotations

import json
import math
import statistics
import xml.etree.ElementTree as ET
from collections import defaultdict
from functools import cache
from pathlib import Path

import pytest

from .roads_net_helpers import NET_PATH, load_net

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "sim" / "out"
WORK = ROOT / "sim" / "work"

COLUMNS = [
    ("edge_id", "string"),
    ("t_bin", "int32"),
    ("mean_speed_mps", "double"),
    ("speed_limit_mps", "double"),
    ("speed_ratio", "double"),
    ("vehicle_count", "int32"),
]
BIN_S = 60
MAX_RATIO = 2.0  # SUMO's default speedFactor distribution is capped at 2
WARMUP_S = 300  # sim/run.py: t = 0 of the scenario clock is the end of the warm-up
FCD_STEP_S = 0.5
FCD_MIN_VEH_S = 10.0
FCD_MEDIAN_TOL = 0.2  # m/s
FCD_P95_TOL = 1.0  # m/s
# Scenarios whose limits are exactly the network's (no time-of-day overrides).
NETWORK_LIMIT_SCENARIOS = {"baseline"}

SCENARIOS = sorted(
    p.name[len("edge_stats_") : -len(".parquet")] for p in OUT.glob("edge_stats_*.parquet")
)

pytestmark = pytest.mark.skipif(
    not SCENARIOS, reason="no sim/out/edge_stats_*.parquet exported yet"
)


@cache
def _table(scenario: str):
    import pyarrow.parquet as pq

    # Through a file object, not a path: once libsumo is loaded in the same process (as by
    # sim/tests/test_sumo_toolchain.py earlier in the session), pyarrow's local filesystem fails
    # with "Attempted to register factory for scheme 'file'" (libsumo bundles its own Arrow).
    with (OUT / f"edge_stats_{scenario}.parquet").open("rb") as fh:
        return pq.read_table(fh)


@cache
def _rows(scenario: str) -> dict:
    return _table(scenario).to_pydict()


def _meta(scenario: str) -> dict:
    path = OUT / f"{scenario}.meta.json"
    assert path.exists(), f"{path.name} missing next to edge_stats_{scenario}.parquet"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_schema_types_and_no_nulls(scenario: str):
    t = _table(scenario)
    assert [(f.name, str(f.type)) for f in t.schema] == COLUMNS
    assert t.num_rows > 0, "no rows"
    nulls = {c: t.column(c).null_count for c in t.column_names if t.column(c).null_count}
    assert not nulls, f"null values: {nulls}"
    d = _rows(scenario)
    for c in ("mean_speed_mps", "speed_limit_mps", "speed_ratio"):
        bad = [v for v in d[c] if not math.isfinite(v)]
        assert not bad, f"{c}: NaN / infinite values: {bad[:10]}"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_keys_unique_and_t_bin_on_the_60_s_grid_of_the_scenario(scenario: str):
    d = _rows(scenario)
    duration = _meta(scenario)["duration_s"]
    keys = list(zip(d["edge_id"], d["t_bin"], strict=True))
    assert len(set(keys)) == len(keys), "duplicate (edge_id, t_bin) rows"
    bad = sorted({t for t in d["t_bin"] if t % BIN_S or not 0 <= t < duration})
    assert not bad, f"t_bin not a multiple of {BIN_S} in [0, {duration}): {bad[:10]}"
    assert set(d["t_bin"]) == set(range(0, duration, BIN_S)), (
        "a bin of the scenario has no rows at all"
    )


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_edges_limits_ratios_and_counts(scenario: str):
    net = load_net(NET_PATH)
    d = _rows(scenario)
    unknown, limit_off, ratio_off, speed_off, count_off = [], [], [], [], []
    for e, t, s, lim, r, n in zip(
        d["edge_id"],
        d["t_bin"],
        d["mean_speed_mps"],
        d["speed_limit_mps"],
        d["speed_ratio"],
        d["vehicle_count"],
        strict=True,
    ):
        edge = net.edges.get(e)
        if edge is None:
            unknown.append(e)
            continue
        if scenario in NETWORK_LIMIT_SCENARIOS and abs(lim - edge.speed) > 1e-6:
            limit_off.append((e, lim, edge.speed))
        if not (lim > 0 and abs(r - s / lim) <= 5e-5 + 1e-9):
            ratio_off.append((e, t, s, lim, r))
        if not 0 <= s <= MAX_RATIO * lim:
            speed_off.append((e, t, s, lim))
        if n < 1:
            count_off.append((e, t, n))
    assert not unknown, f"edge ids not in roads.net.xml (or internal): {sorted(set(unknown))[:10]}"
    assert not limit_off, f"speed_limit_mps differs from the network's lane speed: {limit_off[:10]}"
    assert not ratio_off, f"speed_ratio != mean_speed_mps / speed_limit_mps: {ratio_off[:10]}"
    assert not speed_off, f"mean speed negative or above {MAX_RATIO}x the limit: {speed_off[:10]}"
    assert not count_off, (
        f"vehicle_count < 1 on a row (a row means someone was there): {count_off[:10]}"
    )


_COUNTERS = ("departed", "entered", "arrived", "left", "vaporized", "teleported")


def _edgedata(scenario: str) -> Path:
    return WORK / scenario / "edgestats60.xml"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_rows_match_sumo_edgedata(scenario: str):
    path = _edgedata(scenario)
    if not path.exists():
        pytest.skip(f"{path} not present (run `uv run python -m sim.run {scenario}` locally)")
    duration = _meta(scenario)["duration_s"]
    d = _rows(scenario)
    rows = {
        (e, t): (s, n)
        for e, t, s, n in zip(
            d["edge_id"], d["t_bin"], d["mean_speed_mps"], d["vehicle_count"], strict=True
        )
    }
    on: dict[str, int] = defaultdict(int)
    expected: dict[tuple[str, int], tuple[float, int]] = {}
    for _, iv in ET.iterparse(path, events=("end",)):
        if iv.tag != "interval":
            continue
        b = float(iv.get("begin"))
        for el in iv.iter("edge"):
            e = el.get("id")
            n = {k: int(float(el.get(k, 0) or 0)) for k in _COUNTERS}
            present = on[e] + n["departed"] + n["entered"]
            on[e] = present - n["arrived"] - n["left"] - n["vaporized"] - n["teleported"]
            t = round(b - WARMUP_S)
            if (
                e.startswith(":")
                or not 0 <= t < duration
                or float(el.get("sampledSeconds", 0)) <= 0
            ):
                continue
            expected[(e, t)] = (float(el.get("speed")), max(present, 1))
        iv.clear()
    missing = sorted(set(expected) - set(rows))
    extra = sorted(set(rows) - set(expected))
    assert not missing, (
        f"{len(missing)} edge-bins with vehicle time in SUMO but no row: {missing[:10]}"
    )
    assert not extra, f"{len(extra)} rows with no vehicle time in SUMO: {extra[:10]}"
    off = [
        (k, rows[k], v)
        for k, v in expected.items()
        if abs(rows[k][0] - v[0]) > 1e-9 or rows[k][1] != v[1]
    ]
    assert not off, f"{len(off)} rows differ from SUMO's edgeData (speed, count): {off[:10]}"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_independent_recompute_from_fcd(scenario: str):
    fcd = WORK / scenario / "fcd.xml"
    if not fcd.exists():
        pytest.skip(f"{fcd} not present (run `uv run python -m sim.run {scenario}` locally)")
    duration = _meta(scenario)["duration_s"]
    speed_sum: dict[tuple[str, int], float] = defaultdict(float)
    steps: dict[tuple[str, int], int] = defaultdict(int)
    vehicles: dict[tuple[str, int], set] = defaultdict(set)
    for _, el in ET.iterparse(fcd, events=("end",)):
        if el.tag != "timestep":
            continue
        t = float(el.get("time")) - WARMUP_S
        if 0 <= t < duration:
            b = int(t // BIN_S) * BIN_S
            for v in el.iter("vehicle"):
                lane = v.get("lane")
                if lane.startswith(":"):
                    continue
                k = (lane.rsplit("_", 1)[0], b)
                speed_sum[k] += float(v.get("speed"))
                steps[k] += 1
                vehicles[k].add(v.get("id"))
        el.clear()
    d = _rows(scenario)
    rows = {
        (e, t): (s, n)
        for e, t, s, n in zip(
            d["edge_id"], d["t_bin"], d["mean_speed_mps"], d["vehicle_count"], strict=True
        )
    }
    # A vehicle seen for a single step at a bin's first instant belongs to the previous bin in
    # SUMO's accounting; seen for more than one step, its edge-bin must have a row.
    missing = [k for k, n in steps.items() if n > 1 and k not in rows]
    assert not missing, f"{len(missing)} edge-bins with cars in the FCD but no row: {missing[:10]}"
    undercount = [
        (k, rows[k][1], len(vs)) for k, vs in vehicles.items() if k in rows and rows[k][1] < len(vs)
    ]
    assert not undercount, f"vehicle_count below the distinct FCD vehicles: {undercount[:10]}"
    diffs = sorted(
        abs(speed_sum[k] / steps[k] - rows[k][0])
        for k in steps
        if k in rows and steps[k] * FCD_STEP_S >= FCD_MIN_VEH_S
    )
    assert len(diffs) > 1000, f"too few comparable rows ({len(diffs)})"
    median, p95 = statistics.median(diffs), diffs[int(0.95 * len(diffs))]
    assert median <= FCD_MEDIAN_TOL, f"median |FCD speed - mean_speed_mps| {median:.3f} m/s"
    assert p95 <= FCD_P95_TOL, f"95th percentile |FCD speed - mean_speed_mps| {p95:.3f} m/s"
