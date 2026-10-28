"""Contract test for data/processed/counts.parquet (Phase 3A).

Written by qa-reviewer, independently of pipeline/ (it imports nothing from pipeline/ except the
bbox config: the network is read by tests/contracts/roads_net_helpers.py, the raw SCATS files with
csv + gzip).

CLAUDE.md "Data contracts": `site_id`, `edge_id`, `direction` (approach heading: NB/SB/EB/WB/...),
`bin_start` (15-min bins, Australia/Melbourne local time), `vehicles`. Edge IDs always match
sim/net/roads.net.xml. Never invent data (constraint 5): no bin is filled or patched.

What it checks:
- Exactly the five columns, in order; strings, a tz-aware Australia/Melbourne timestamp, an integer
  count; no nulls; vehicles >= 0 and below MAX_PER_BIN.
- bin_start on a 15-minute boundary of local time; no key (site_id, edge_id, bin_start) twice; one
  direction per (site, edge); no edge carries counts from two sites (if that ever happens, the sim
  needs an explicit rule: two sites' loops on one SUMO edge count the same cars).
- edge_id is a non-internal edge of roads.net.xml that cars may use, ending at a traffic-light
  junction (an approach to a signal), within SITE_NEAR_M of the site in the DTP signal register,
  and its last segment points the way `direction` says (within HEADING_TOL_DEG: catches reversed
  approaches, the error George's AI made often).
- Raw recompute (where data/raw/scats/ exists): every row equals the sum of its approach's detectors
  in the day's raw VSDATA file (approach -> edge from counts_report.json, detectors from
  pipeline/reference/scats_detector_map.json); no row uses a bin in which any of those detectors is
  -1 (alarm); every bin valid for all of them is present (nothing dropped silently).
- Detector map rows behind the counts are vehicle stop-line loops on 'full' coverage approaches,
  each citing a sheet file that exists in data/raw.
- data/SOURCES.md lists every DTP dataset used, with CC BY 4.0.
- Fault screening (xfail, strict): the AM 07:00-10:00 count of every approach lies within
  [1/AM_QC_RATIO, AM_QC_RATIO] of the median of its QC days (same weekday, earlier weeks).
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import json
import math
import statistics
from collections import defaultdict
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from .roads_net_helpers import NET_PATH, load_net

ROOT = Path(__file__).resolve().parents[2]
COUNTS = ROOT / "data" / "processed" / "counts.parquet"
REPORT = ROOT / "data" / "processed" / "checks" / "counts_report.json"
DETECTOR_MAP = ROOT / "pipeline" / "reference" / "scats_detector_map.json"
SOURCES = ROOT / "data" / "SOURCES.md"
RAW = ROOT / "data" / "raw" / "scats"
SITES_CSV = RAW / "victorian_traffic_signals.csv"

TZ = "Australia/Melbourne"
COLUMNS = ["site_id", "edge_id", "direction", "bin_start", "vehicles"]
HEADINGS = {"NB": 0, "NE": 45, "EB": 90, "SE": 135, "SB": 180, "SW": 225, "WB": 270, "NW": 315}
HEADING_TOL_DEG = 60.0
SITE_NEAR_M = 80.0
MAX_PER_BIN = 1000  # 4,000 veh/h on one approach: far above any CBD stop line
AM = (28, 40)  # bins 07:00-10:00
AM_QC_RATIO = 1.5
DATASETS = [
    "traffic-signal-volume-data",
    "victorian-traffic-signals",
    "traffic-signal-configuration-data-sheets",
    "historical-annual-average-daily-traffic-volume",
    "tirtl-traffic-counts",
]

pytestmark = pytest.mark.skipif(not COUNTS.exists(), reason="counts.parquet not built yet")


@cache
def _table():
    import pyarrow.parquet as pq

    with COUNTS.open("rb") as fh:  # a file object, never a path (CLAUDE.md, libsumo / Arrow)
        return pq.read_table(fh)


@cache
def _rows() -> dict:
    return _table().to_pydict()


@cache
def _report() -> dict:
    return json.loads(REPORT.read_text(encoding="utf-8"))


@cache
def _map() -> dict:
    return json.loads(DETECTOR_MAP.read_text(encoding="utf-8"))


def _approaches() -> list[dict]:
    """(approach used on some day) with its site, edge, direction and detectors."""
    out = {}
    for day in _report()["per_day"]:
        for a in day["approaches_used"]:
            out[(a["site_id"], a["edge_id"])] = a
    return list(out.values())


@cache
def _raw_day(day: dt.date) -> dict[tuple[int, int], list[int]] | None:
    path = RAW / f"VSDATA_{day:%Y%m%d}.csv.gz"
    if not path.exists():
        return None
    out = {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        r = csv.reader(fh)
        header = next(r)
        cols = [header.index(f"V{k:02d}") for k in range(96)]
        for row in r:
            out[(int(row[0]), int(row[2]))] = [int(row[c]) for c in cols]
    return out


def test_schema_types_and_values():
    import pyarrow as pa

    t = _table()
    assert t.column_names == COLUMNS
    s = t.schema
    for c in ("site_id", "edge_id", "direction"):
        assert pa.types.is_string(s.field(c).type), f"{c}: {s.field(c).type}"
    bt = s.field("bin_start").type
    assert pa.types.is_timestamp(bt) and bt.tz == TZ, f"bin_start {bt}: not a {TZ} timestamp"
    assert pa.types.is_integer(s.field("vehicles").type), f"vehicles {s.field('vehicles').type}"
    nulls = {c: t.column(c).null_count for c in COLUMNS if t.column(c).null_count}
    assert not nulls, f"nulls: {nulls}"
    assert t.num_rows > 0
    v = _rows()["vehicles"]
    bad = [x for x in v if not 0 <= x <= MAX_PER_BIN]
    assert not bad, f"vehicles outside [0, {MAX_PER_BIN}]: {bad[:10]}"


def test_bins_are_15_minute_local_time_and_keys_unique():
    d = _rows()
    tz = ZoneInfo(TZ)
    off = [b for b in d["bin_start"] if b.astimezone(tz).minute % 15 or b.second or b.microsecond]
    assert not off, f"bin_start not on a 15-minute boundary: {off[:5]}"
    keys = list(zip(d["site_id"], d["edge_id"], d["bin_start"], strict=True))
    assert len(keys) == len(set(keys)), "duplicate (site_id, edge_id, bin_start)"
    dirs = defaultdict(set)
    sites = defaultdict(set)
    for s, e, r in zip(d["site_id"], d["edge_id"], d["direction"], strict=True):
        dirs[(s, e)].add(r)
        sites[e].add(s)
    assert not [k for k, v in dirs.items() if len(v) > 1], "one approach edge with two directions"
    shared = {e: sorted(v) for e, v in sites.items() if len(v) > 1}
    assert not shared, (
        f"edges with counts from several sites (the sim needs a rule for these): {shared}"
    )


def _cars(lane: dict[str, str]) -> bool:
    if "allow" in lane:
        return "passenger" in lane["allow"].split() or "all" in lane["allow"].split()
    if "disallow" in lane:
        return "passenger" not in lane["disallow"].split()
    return True


def test_edges_are_signal_approaches_near_the_site_heading_the_right_way():
    net = load_net(NET_PATH)
    d = _rows()
    approaches = sorted(set(zip(d["site_id"], d["edge_id"], d["direction"], strict=True)))
    bad_dir = sorted({r for r in d["direction"]} - set(HEADINGS))
    assert not bad_dir, f"directions not in {sorted(HEADINGS)}: {bad_dir}"
    sites = {}
    if SITES_CSV.exists():
        with SITES_CSV.open(encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                if r["LATITUDE"] and r["LONGITUDE"]:
                    sites[r["SITE_NO"]] = (float(r["LONGITUDE"]), float(r["LATITUDE"]))
    unknown, not_cars, not_signal, wrong_way, far = [], [], [], [], []
    for s, e, r in approaches:
        edge = net.edges.get(e)
        if edge is None:
            unknown.append(e)
            continue
        if not any(_cars(ln) for ln in edge.lanes):
            not_cars.append(e)
        if net.junctions.get(edge.to, {}).get("type") != "traffic_light":
            not_signal.append((s, e, net.junctions.get(edge.to, {}).get("type")))
        (x0, y0), (x1, y1) = edge.shape[-2], edge.shape[-1]
        bearing = math.degrees(math.atan2(x1 - x0, y1 - y0)) % 360
        if abs((bearing - HEADINGS[r] + 180) % 360 - 180) > HEADING_TOL_DEG:
            wrong_way.append((s, e, r, round(bearing)))
        if s in sites:
            lon, lat = net.lonlat(x1, y1)
            slon, slat = sites[s]
            m = math.hypot(
                (lon - slon) * 111_320 * math.cos(math.radians(lat)), (lat - slat) * 111_320
            )
            if m > SITE_NEAR_M:
                far.append((s, e, round(m)))
    assert not unknown, f"edge ids not in roads.net.xml: {unknown[:10]}"
    assert not not_cars, f"edges cars may not use: {not_cars[:10]}"
    assert not not_signal, f"approach edges not ending at a traffic light: {not_signal[:10]}"
    assert not wrong_way, f"edge heading disagrees with direction (reversed?): {wrong_way[:10]}"
    assert not far, f"approach edges ending > {SITE_NEAR_M} m from the site: {far[:10]}"


def test_detector_map_rows_behind_the_counts():
    rows = defaultdict(list)
    for r in _map()["rows"]:
        rows[r["approach_id"]].append(r)
    allowed = set(_map()["source_values"])
    bad, missing_sheet = [], []
    for a in _approaches():
        for r in rows[a["approach_id"]]:
            if (
                r["type"] != "vehicle_stopline"
                or r["approach_coverage"] != "full"
                or r["source"] not in allowed
                or r["detector"] not in a["detectors"]
            ):
                bad.append((a["approach_id"], r["detector"], r["type"], r["approach_coverage"]))
            if RAW.exists() and not (ROOT / r["sheet_file"]).exists():
                missing_sheet.append(r["sheet_file"])
        if sorted(a["detectors"]) != sorted(r["detector"] for r in rows[a["approach_id"]]):
            bad.append((a["approach_id"], "detectors differ from the map"))
    assert not bad, f"counted detectors that are not full-coverage stop-line loops: {bad[:10]}"
    assert not missing_sheet, (
        f"cited sheets missing from data/raw: {sorted(set(missing_sheet))[:10]}"
    )


def test_every_row_is_the_raw_scats_sum_and_nothing_is_filled():
    d = _rows()
    tz = ZoneInfo(TZ)
    days = sorted({b.astimezone(tz).date() for b in d["bin_start"]})
    if _raw_day(days[0]) is None:
        pytest.skip("data/raw/scats not present")
    got = {
        (s, e, b.astimezone(tz)): v
        for s, e, b, v in zip(
            d["site_id"], d["edge_id"], d["bin_start"], d["vehicles"], strict=True
        )
    }
    mismatch, from_alarm, dropped = [], [], []
    checked = 0
    for day in days:
        raw = _raw_day(day)
        assert raw is not None, f"no raw SCATS file for {day} (a row with no source)"
        start = dt.datetime.combine(day, dt.time(0), tz)
        for a in _approaches():
            series = [raw.get((int(a["site_id"]), det)) for det in a["detectors"]]
            for k in range(96):
                b = start + dt.timedelta(minutes=15 * k)
                key = (a["site_id"], a["edge_id"], b)
                vals = [s[k] if s is not None else -1 for s in series]
                if any(v < 0 for v in vals):
                    if key in got:
                        from_alarm.append(key)
                    continue
                if key not in got:
                    dropped.append(key)
                    continue
                checked += 1
                if got[key] != sum(vals):
                    mismatch.append((key, got[key], sum(vals)))
    assert checked == len(got), (
        f"rows not explained by an approach of the report: {len(got) - checked}"
    )
    assert not from_alarm, f"rows in bins where a detector is -1 (filled?): {from_alarm[:10]}"
    assert not mismatch, f"rows that are not the raw detector sum: {mismatch[:10]}"
    assert not dropped, f"valid raw bins missing from a kept approach: {dropped[:10]}"


def test_sources_lists_every_dataset_with_its_licence():
    text = SOURCES.read_text(encoding="utf-8")
    for ds in DATASETS:
        line = next((ln for ln in text.splitlines() if ds in ln and ln.startswith("|")), None)
        assert line is not None, f"data/SOURCES.md has no table row for {ds}"
        assert "CC BY 4.0" in line, f"{ds}: no CC BY 4.0 licence in its SOURCES.md row"


def _am_total(raw: dict, a: dict) -> int | None:
    """An approach's 07:00-10:00 total in one raw day, or None if any of its bins is missing."""
    vs = [raw.get((int(a["site_id"]), det)) for det in a["detectors"]]
    if any(v is None for v in vs):
        return None
    s = [sum(v[k] for v in vs) for k in range(*AM)]
    return None if any(x < 0 for x in s) else sum(s)


def test_am_counts_are_within_the_qc_range():
    d = _rows()
    tz = ZoneInfo(TZ)
    days = sorted({b.astimezone(tz).date() for b in d["bin_start"]})
    by_day = {r["date"]: r["qc_days"] for r in _report()["per_day"]}
    out = []
    for day in days:
        raw = _raw_day(day)
        if raw is None:
            pytest.skip("data/raw/scats not present")
        qc = [_raw_day(dt.date.fromisoformat(q)) for q in by_day[day.isoformat()]]
        for a in _approaches():
            t = _am_total(raw, a)
            q = [x for x in (_am_total(r, a) for r in qc if r is not None) if x]
            if t is None or not q:
                continue
            ratio = t / statistics.median(q)
            if not 1 / AM_QC_RATIO <= ratio <= AM_QC_RATIO:
                out.append(
                    (
                        day.isoformat(),
                        a["approach_id"],
                        a["street"],
                        a["direction"],
                        round(ratio, 2),
                    )
                )
    assert not out, f"AM 07-10 counts far from the QC days (detector fault?): {out}"
