"""am_peak calibration: GEH, counts -> edgedata, hold-out determinism, the scenario clock, the
export windows, and schema checks on the committed am_peak exports (when present)."""

from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from sim import am_peak, edge_stats
from sim import calibrate as cal
from sim.bus_lane_hours import BY_KEY
from sim.bus_lane_hours import active as bus_lane_active
from sim.conditional_speeds import active as speed_active
from sim.config import CHECKS_DIR, OUT_DIR

TZ = cal.TZ

# --- GEH --------------------------------------------------------------------------------------


def test_geh_known_values():
    assert cal.geh(100, 100) == 0.0
    assert cal.geh(0, 0) == 0.0
    assert cal.geh(150, 100) == pytest.approx(math.sqrt(20))  # 4.47 < 5
    assert cal.geh(100, 150) == pytest.approx(math.sqrt(20))  # symmetric
    assert cal.geh(0, 50) == pytest.approx(10.0)
    assert cal.geh(1000, 1160) < 5 < cal.geh(1000, 1170)


def test_geh_rejects_negative_flows():
    with pytest.raises(ValueError):
        cal.geh(-1, 10)


# --- counts -> approaches -> edgedata -------------------------------------------------------


def _row(site, edge, direction, hh, mm, n, day=cal.SCENARIO_DATE):
    return {
        "site_id": site,
        "edge_id": edge,
        "direction": direction,
        "bin_start": datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ),
        "vehicles": n,
    }


def test_approach_counts_window_and_clock():
    rows = [
        _row("1", "e1", "NB", 6, 15, 99),  # before 06:30: outside
        _row("1", "e1", "NB", 6, 30, 10),  # t = 0
        _row("1", "e1", "NB", 7, 0, 20),  # t = 1800
        _row("1", "e1", "NB", 9, 15, 30),  # t = 9900, last bin
        _row("1", "e1", "NB", 9, 30, 99),  # 09:30: outside
        _row("2", "e2", "SE", 8, 0, 5),
    ]
    c = cal.approach_counts(rows, cal.scenario_start())
    assert c == {
        cal.Approach("1", "e1", "NB"): {0: 10, 1800: 20, 9900: 30},
        cal.Approach("2", "e2", "SE"): {5400: 5},
    }


def test_approach_counts_rejects_naive_and_off_grid_times():
    naive = _row("1", "e1", "NB", 7, 0, 1)
    naive["bin_start"] = naive["bin_start"].replace(tzinfo=None)
    with pytest.raises(ValueError):
        cal.approach_counts([naive], cal.scenario_start())
    with pytest.raises(ValueError):
        cal.approach_counts([_row("1", "e1", "NB", 7, 5, 1)], cal.scenario_start())


def test_approach_counts_uses_local_time_across_utc():
    # 06:30 AEDT is 19:30 UTC the day before: the same instant gives t = 0
    r = _row("1", "e1", "NB", 6, 30, 7)
    r["bin_start"] = r["bin_start"].astimezone(datetime.now().astimezone().tzinfo)
    assert cal.approach_counts([r], cal.scenario_start()) == {cal.Approach("1", "e1", "NB"): {0: 7}}


def test_one_count_per_edge_never_sums():
    a = cal.Approach("10", "e", "NB")
    b = cal.Approach("9", "e", "NB")
    c = cal.Approach("11", "f", "SB")
    counts = {a: {0: 100, 900: 100}, b: {0: 50}, c: {0: 7}}
    kept, dropped = cal.one_count_per_edge(counts)
    assert kept == {a: {0: 100, 900: 100}, c: {0: 7}}  # most bins wins, no sum
    assert [d["approach"] for d in dropped] == [b.key]
    # with equal bins, the lowest site id is kept (numerically: 9 before 10)
    kept2, _ = cal.one_count_per_edge({a: {0: 100}, b: {0: 50}})
    assert kept2 == {b: {0: 50}}


def test_edgedata_xml_omits_missing_bins():
    a = cal.Approach("1", "e1", "NB")
    b = cal.Approach("2", "e2", "SB")
    xml = cal.edgedata_xml({a: {0: 10, 1800: 30}, b: {900: 4}}, begin=0, end=2700)
    root = ET.fromstring(xml)
    got = {
        (int(iv.get("begin")), int(iv.get("end"))): {
            e.get("id"): int(e.get("entered")) for e in iv.iter("edge")
        }
        for iv in root.iter("interval")
    }
    assert got == {
        (0, 900): {"e1": 10},
        (900, 1800): {"e2": 4},  # e1 has no bin here: omitted, not zero
        (1800, 2700): {"e1": 30},
    }


# --- hold-out ---------------------------------------------------------------------------------


def test_holdout_is_deterministic_and_order_free():
    sites = [str(s) for s in range(2900, 2945)]
    h1 = cal.holdout_sites(sites)
    h2 = cal.holdout_sites(list(reversed(sites)) + sites[:3])  # order and duplicates ignored
    assert h1 == h2 == sorted(h1)
    assert len(h1) == round(0.15 * 45) == 7
    assert set(h1) <= set(sites)
    assert cal.holdout_sites(sites, seed=cal.HOLDOUT_SEED + 1) != h1


def test_holdout_of_the_real_counts_matches_the_report():
    rep_path = CHECKS_DIR / "am_peak_calibration.json"
    if not rep_path.exists():
        pytest.skip("no am_peak calibration report yet")
    rep = json.loads(rep_path.read_text())
    rows = cal.read_counts(am_peak.COUNTS_PATH)
    kept, _ = cal.one_count_per_edge(cal.approach_counts(rows, cal.scenario_start()))
    assert cal.holdout_sites({a.site_id for a in kept}) == rep["demand_build"]["held_out_sites"]


# --- scoring ----------------------------------------------------------------------------------


def test_score_hourly_and_half_hour_scaled():
    a = cal.Approach("1", "e1", "NB")
    obs = {a: {t: 100 for t in range(1800, 10800, 900)}}
    sim = {"e1": {t: 100 for t in range(1800, 10800, 900)}}
    sim["e1"][1800] = 40  # 07:00-07:15 short by 60
    rows = {r["window"]: r for r in cal.score(obs, sim)}
    assert rows["07-08"]["counted_vph"] == 400 and rows["07-08"]["modelled_vph"] == 340
    assert rows["07-08"]["geh"] == round(cal.geh(340, 400), 2)
    assert rows["08-09"]["geh"] == 0.0
    assert rows["09-0930"]["counted_vph"] == 400  # 200 in half an hour, x2


def test_score_skips_windows_with_missing_bins():
    a = cal.Approach("1", "e1", "NB")
    obs = {a: {t: 100 for t in range(1800, 10800, 900) if t != 6300}}
    rows = {r["window"]: r for r in cal.score(obs, {})}
    assert rows["08-09"]["geh"] is None and rows["08-09"]["missing_bins"] == 1
    assert rows["07-08"]["geh"] == pytest.approx(cal.geh(0, 400), abs=0.01)
    s = cal.summarise(list(rows.values()))
    assert s["gate"]["n"] == 1  # only 07-08 scored in the gate windows


def test_summarise_gate_share():
    rows = [
        {"site_id": "1", "window": "07-08", "geh": 1.0},
        {"site_id": "1", "window": "08-09", "geh": 6.0},
        {"site_id": "2", "window": "07-08", "geh": 4.9},
        {"site_id": "2", "window": "09-0930", "geh": 9.0},  # not a gate window
    ]
    s = cal.summarise(rows)
    assert s["gate"]["n"] == 3 and s["gate"]["geh_lt_5"] == 2
    assert s["gate"]["share"] == round(2 / 3, 4)
    assert s["sites_all_approaches_ok"] == {"site_hours": 3, "ok": 2, "share": round(2 / 3, 4)}


def test_read_interval_counts(tmp_path: Path):
    p = tmp_path / "c.xml"
    p.write_text(
        '<meandata><interval begin="0.00" end="900.00" id="c">'
        '<edge id="a" left="12" entered="10" departed="3"/></interval>'
        '<interval begin="900.00" end="1800.00" id="c"><edge id="a" left="1"/></interval>'
        "</meandata>"
    )
    assert cal.read_interval_counts(p) == {"a": {0: 12, 900: 1}}
    assert cal.read_interval_counts(p, "used") == {"a": {0: 13, 900: 0}}


def test_fitted_counts_by_departure_interval():
    v = [(10.0, ["x", "e1", "e2"]), (899.0, ["e1"]), (900.0, ["e2", "e2"])]
    assert cal.fitted_counts(v, {"e1", "e2"}) == {"e1": {0: 2}, "e2": {0: 1, 900: 1}}


# --- candidate routes -------------------------------------------------------------------------


def test_od_pairs_are_seeded_and_cover_all_classes():
    w = {
        "entry": [("in1", 8.0), ("in2", 2.0)],
        "exit": [("out1", 1.0)],
        "internal_src": [("i1", 1.0), ("i2", 1.0)],
        "internal_dst": [("i1", 1.0), ("i3", 1.0)],
    }
    p1 = cal.sample_od_pairs(w, n=400)
    assert p1 == cal.sample_od_pairs(w, n=400)
    assert p1 != cal.sample_od_pairs(w, n=400, seed=1)
    assert {c for c, _, _ in p1} == {"b2b", "b2i", "i2b", "i2i"}
    assert all(f != t for _, f, t in p1)
    entries = [f for c, f, _ in p1 if c == "b2b"]
    assert entries.count("in1") > 2 * entries.count("in2")  # lanes x class weights


def test_unique_routes_drops_counted_endings_and_short_routes(tmp_path: Path):
    p = tmp_path / "r.xml"
    p.write_text(
        "<routes>"
        '<vehicle id="b2b_0" depart="0"><route edges="a b c"/></vehicle>'
        '<vehicle id="b2b_1" depart="0"><route edges="a b c"/></vehicle>'  # duplicate
        '<vehicle id="b2i_2" depart="0"><route edges="a b"/></vehicle>'  # ends on counted b
        '<vehicle id="i2i_3" depart="0"><route edges="c d"/></vehicle>'  # too short
        "</routes>"
    )
    lengths = {"a": 200.0, "b": 100.0, "c": 100.0, "d": 50.0}
    assert cal.unique_routes(p, {"b"}, lengths) == [("b2b", ["a", "b", "c"])]


# --- the scenario clock and export windows ----------------------------------------------------


def test_scenario_clock():
    assert cal.local_time(0) == datetime(2026, 10, 23, 6, 30, tzinfo=TZ)
    assert cal.local_time(cal.WARMUP_S).strftime("%H:%M") == "07:00"
    assert cal.local_time(cal.END_S).strftime("%H:%M") == "09:30"
    assert cal.local_time(am_peak.FCD_BEGIN).strftime("%H:%M") == "07:30"
    assert cal.local_time(am_peak.FCD_END).strftime("%H:%M") == "08:00"
    assert cal.local_time(0).weekday() == 4  # Friday
    assert cal.local_time(0).utcoffset() == timedelta(hours=11)  # AEDT


def test_clock_time_road_rules():
    # bus lanes (City of Melbourne hours): Lonsdale south 07:00-09:30, City Rd inbound 06:30-10:00
    assert not bus_lane_active(BY_KEY["lonsdale_south"], cal.local_time(1799))
    assert bus_lane_active(BY_KEY["lonsdale_south"], cal.local_time(1800))
    assert bus_lane_active(BY_KEY["city_rd_inbound"], cal.local_time(0))
    assert not bus_lane_active(BY_KEY["queen_east"], cal.local_time(5400))  # PM only
    # school zone from 08:00
    school = "40 @ (Mo-Fr 08:00-09:30, 14:30-16:00; PH off; SH off)"
    assert not speed_active(school, cal.local_time(5399))
    assert speed_active(school, cal.local_time(5400))


def test_edge_stats_time_dependent_limit(tmp_path: Path):
    p = tmp_path / "e.xml"
    body = "".join(
        f'<interval begin="{b}" end="{b + 60}" id="s">'
        f'<edge id="a" sampledSeconds="30" speed="8.0" departed="{1 if b == 0 else 0}"/>'
        "</interval>"
        for b in (5340, 5400)
    )
    p.write_text(f"<meandata>{body}</meandata>")

    def limit_at(edge, b):
        return 40 / 3.6 if b >= 5400 else None

    rows, _ = edge_stats.read_bins(p, 0.0, 10800.0, {"a": 60 / 3.6}, limit_at)
    assert [(r["t_bin"], r["speed_limit_mps"]) for r in rows] == [
        (5340, 60 / 3.6),
        (5400, 40 / 3.6),
    ]
    assert rows[1]["speed_ratio"] == round(8.0 / (40 / 3.6), 4)


def test_trips_json_uses_the_scenario_clock():
    from sim.export_trips import Sample, Trip, trips_json

    class _Net:
        def getLocationOffset(self):
            return (0.0, 0.0)

        def getGeoProj(self):
            return lambda x, y, inverse=True: (x * 0 + 144.96, y * 0 - 37.81)

    tr = Trip(
        "v", "v", [Sample(5400.0, 0, 0, "e_0", 1.0, z=0.0), Sample(5401.0, 1, 0, "e_0", 1.0, z=0.0)]
    )
    tr.kept = [0, 1]
    data = json.loads(trips_json([tr], _Net(), 0.0))
    assert data[0]["timestamps"] == [5400, 5401]


# --- committed exports ------------------------------------------------------------------------

TRIPS = OUT_DIR / "trips_am_peak.json"
META = OUT_DIR / "am_peak.meta.json"
STATS = OUT_DIR / "edge_stats_am_peak.parquet"
REPORT = CHECKS_DIR / "am_peak_calibration.json"
needs_exports = pytest.mark.skipif(
    not (META.exists() and STATS.exists()), reason="am_peak not exported yet"
)


@needs_exports
def test_meta_contract():
    meta = json.loads(META.read_text())
    assert set(meta) == {
        "scenario",
        "demand",
        "duration_s",
        "seed",
        "sumo_version",
        "vehicles",
        "teleports",
        "tunnel_vehicles",
        "generated_at",
        "start_local",
    }
    assert meta["start_local"] == "2026-10-23T06:30:00+11:00"
    assert meta["scenario"] == "am_peak"
    assert meta["demand"] == "calibrated_counts"
    assert meta["duration_s"] == cal.END_S
    assert 0 <= meta["tunnel_vehicles"] <= meta["vehicles"]


@needs_exports
def test_trips_window_is_0730_0800():
    if not TRIPS.exists():
        pytest.skip("trips_am_peak.json not committed (size)")
    trips = json.loads(TRIPS.read_text())
    meta = json.loads(META.read_text())
    # meta.vehicles counts distinct vehicles; teleports split trips into "<id>", "<id>#1", …
    assert len({re.sub(r"#\d+$", "", tr["id"]) for tr in trips}) == meta["vehicles"]
    ts = [t for tr in trips for t in tr["timestamps"]]
    assert min(ts) == am_peak.FCD_BEGIN and max(ts) <= am_peak.FCD_END
    for tr in trips:
        assert set(tr) == {"id", "type", "path", "timestamps"}
        assert len(tr["path"]) == len(tr["timestamps"]) >= 2
        assert all(b > a for a, b in zip(tr["timestamps"], tr["timestamps"][1:], strict=False))


@needs_exports
def test_edge_stats_cover_the_whole_run_on_the_0630_clock():
    import pyarrow.parquet as pq

    with STATS.open("rb") as fh:  # file object: libsumo may be loaded in this process
        t = pq.read_table(fh)
    assert t.schema.names == edge_stats.COLUMNS
    d = t.to_pydict()
    assert set(d["t_bin"]) == set(range(0, cal.END_S, 60))
    for s, lim, r, n in zip(
        d["mean_speed_mps"], d["speed_limit_mps"], d["speed_ratio"], d["vehicle_count"], strict=True
    ):
        assert lim > 0 and s >= 0 and n >= 1
        assert r == round(s / lim, 4)


@needs_exports
def test_edge_stats_match_raw_sumo_edgedata_on_the_am_peak_clock():
    """The qa raw cross-check, on this scenario's clock (t_bin = simulation time)."""
    raw = am_peak.work_dir() / am_peak.EDGE_STATS_FILE
    if not raw.exists():
        pytest.skip("no local am_peak run in sim/work")
    import pyarrow.parquet as pq

    with STATS.open("rb") as fh:
        d = pq.read_table(fh).to_pydict()
    rows = {
        (e, t): (s, n)
        for e, t, s, n in zip(
            d["edge_id"], d["t_bin"], d["mean_speed_mps"], d["vehicle_count"], strict=True
        )
    }
    on: dict[str, int] = defaultdict(int)
    expected = {}
    for _, iv in ET.iterparse(raw, events=("end",)):
        if iv.tag != "interval":
            continue
        b = round(float(iv.get("begin")))
        for el in iv.iter("edge"):
            e = el.get("id")
            n = {
                k: int(float(el.get(k, 0) or 0))
                for k in ("departed", "entered", "arrived", "left", "vaporized", "teleported")
            }
            present = on[e] + n["departed"] + n["entered"]
            # a teleport is counted in `left` too (see sim/edge_stats.py _EXITS)
            on[e] = present - n["arrived"] - n["left"] - n["vaporized"]
            if e.startswith(":") or float(el.get("sampledSeconds", 0)) <= 0:
                continue
            expected[(e, b)] = (float(el.get("speed")), max(present, 1))
        iv.clear()
    assert set(expected) == set(rows)
    assert all(rows[k] == v for k, v in expected.items())


@needs_exports
def test_calibration_report_is_complete():
    rep = json.loads(REPORT.read_text())
    for k in ("gate", "calibration", "validation_held_out", "freeway_aadt_2019", "settings"):
        assert k in rep
    cal_sites = set(rep["demand_build"]["calibration_sites"])
    held = set(rep["demand_build"]["held_out_sites"])
    assert not cal_sites & held
    assert {r["site_id"] for r in rep["geh_calibration"]} <= cal_sites
    assert {r["site_id"] for r in rep["geh_held_out"]} <= held
    assert rep["demand"] == "calibrated_counts"
