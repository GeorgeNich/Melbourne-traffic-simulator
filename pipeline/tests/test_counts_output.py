"""The committed Phase 3A outputs against their contract and against each other.

counts.parquet (CLAUDE.md contract), counts_report.json, freeway_reference.json and the
reference detector map. Skips cleanly if a file is missing (e.g. in a fresh checkout before the
build); the build itself also validates the parquet before writing it.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PARQUET = ROOT / "data" / "processed" / "counts.parquet"
REPORT = ROOT / "data" / "processed" / "checks" / "counts_report.json"
FREEWAY = ROOT / "data" / "processed" / "checks" / "freeway_reference.json"
DETMAP = ROOT / "pipeline" / "reference" / "scats_detector_map.json"
NET = ROOT / "sim" / "net" / "roads.net.xml"


def _need(p: Path):
    if not p.exists():
        pytest.skip(f"{p.relative_to(ROOT)} not built")


@pytest.fixture(scope="module")
def table():
    import pyarrow.parquet as pq

    _need(PARQUET)
    return pq.read_table(PARQUET)


@pytest.fixture(scope="module")
def report():
    _need(REPORT)
    return json.loads(REPORT.read_text())


def test_parquet_matches_contract(table):
    from pipeline.counts import CONTRACT_COLUMNS, DIRECTIONS, TZ

    assert tuple(table.column_names) == CONTRACT_COLUMNS
    assert table.schema.field("bin_start").type.tz == "Australia/Melbourne"
    d = table.to_pydict()
    assert set(d["direction"]) <= DIRECTIONS
    assert min(d["vehicles"]) >= 0
    for b in d["bin_start"][:500]:
        assert b.minute % 15 == 0 and b.second == 0
        assert b.astimezone(TZ).utcoffset() == dt.timedelta(hours=11)  # AEDT in Oct
    keys = list(zip(d["site_id"], d["edge_id"], d["bin_start"], strict=True))
    assert len(keys) == len(set(keys))


def test_parquet_edges_exist_in_network(table):
    import re

    _need(NET)
    edges = set(re.findall(r'<edge id="([^":][^"]*)"', NET.read_text()))
    missing = set(table.column("edge_id").to_pylist()) - edges
    assert not missing, f"edge ids not in roads.net.xml: {sorted(missing)[:5]}"


def test_enough_sites_on_target_day(table, report):
    target = report["target_day"]
    day = [b.date().isoformat() for b in table.column("bin_start").to_pylist()]
    assert target in day
    sites = {
        s for s, d in zip(table.column("site_id").to_pylist(), day, strict=True) if d == target
    }
    assert len(sites) >= 40, f"only {len(sites)} usable sites on {target} (stop-and-report < ~40)"


def test_report_agrees_with_parquet(table, report):
    d = report["per_day"][0]
    used = {(a["site_id"], a["edge_id"]) for a in d["approaches_used"]}
    rows = set(
        zip(table.column("site_id").to_pylist(), table.column("edge_id").to_pylist(), strict=True)
    )
    assert used == rows
    assert report["parquet"]["rows"] == table.num_rows
    assert d["detectors_dropped_for_faults"] == sum(
        v["status"].startswith("drop") for v in d["detector_status"].values()
    )
    for a in d["approaches_dropped"]:
        assert a["reason"]


def test_report_records_inputs_and_licence(report):
    assert "CC BY 4.0" in report["licence"]
    shas = report["inputs_sha256"]
    for key in ("sim/net/roads.net.xml", "pipeline/reference/scats_detector_map.json"):
        assert key in shas
    assert report["recheck_10pct"]["sites"]
    assert len(report["spot_check"]["sites"]) >= 15


def test_detector_map_rows_are_cited_and_consistent():
    ref = json.loads(DETMAP.read_text())
    assert ref["rows"]
    seen = set()
    for r in ref["rows"]:
        assert r["source"] in ("george_ai_verified", "data_engineer_read")
        assert r["sheet_file"].startswith("data/raw/scats/sheets/")
        assert r["sheet_page"] and r["street"] and r["heading"]
        assert r["approach_coverage"] in ("full", "partial")
        key = (r["site"], r["detector"])
        assert key not in seen, f"detector {key} counted on two approaches"
        seen.add(key)


def test_freeway_reference_is_check_only_and_labelled():
    _need(FREEWAY)
    fw = json.loads(FREEWAY.read_text())
    assert "never used for fitting" in fw["description"]
    assert "UPSTREAM BOUND" in fw["tirtl"]["note"]
    assert {"504_E", "505_W"} <= set(fw["tirtl"]["sites"])
    for s in fw["tirtl"]["sites"].values():
        assert len(s["bins_15min"]) == 96 and sum(s["bins_15min"]) == s["vehicles_day"]
    assert fw["aadt_2019"]["segments_with_edges"] > 0
