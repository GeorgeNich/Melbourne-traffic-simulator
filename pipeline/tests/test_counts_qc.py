"""Target-day QC rules, approach sums, calendar helpers and the counts.parquet validator."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pyarrow as pa
import pytest

from pipeline import counts
from pipeline.scats import DetectorDay


def dd(total_per_bin=10.0, missing=(), fully=False):
    v = np.full(96, float(total_per_bin))
    for i in missing:
        v[i] = np.nan
    if fully:
        v[:] = np.nan
    return DetectorDay(v, len(missing) if not fully else 96, fully)


def test_target_day_and_its_qc_days_skip_school_holidays():
    assert dt.date(2026, 10, 23) == counts.TARGET_DAY and counts.TARGET_DAY.weekday() == 4
    assert counts.is_normal_working_day(counts.TARGET_DAY)
    # 2 Oct and 25 Sep 2026 are in the school holidays (19 Sep - 4 Oct): skipped.
    assert counts.qc_days_for(counts.TARGET_DAY) == [
        dt.date(2026, 10, 16),
        dt.date(2026, 10, 9),
        dt.date(2026, 9, 18),
    ]
    assert not counts.is_normal_working_day(dt.date(2026, 10, 24))  # Saturday
    assert not counts.is_normal_working_day(dt.date(2026, 11, 3))  # Melbourne Cup


def test_qc_ok_and_partial_flag():
    q = counts.detector_qc(dd(10), [dd(10), dd(11), dd(9)])
    assert q.status == "ok"
    q = counts.detector_qc(dd(10, missing=(3, 4)), [dd(10), dd(10)])
    assert q.status == "flag_partial" and "2 intervals" in q.detail


def test_qc_drops_fully_alarmed_dead_and_never_counting():
    assert counts.detector_qc(dd(fully=True), [dd(10)]).status == "drop_fully_alarmed"
    assert counts.detector_qc(dd(0), [dd(10), dd(12)]).status == "drop_dead"
    assert counts.detector_qc(dd(0), [dd(0), dd(0)]).status == "drop_never_counts"
    assert counts.detector_qc(None, [dd(10)]).status == "drop_missing"


def test_qc_deviation_drop_and_flag_margins():
    qc = [dd(10), dd(12), dd(11)]  # day totals 960, 1152, 1056
    assert counts.detector_qc(dd(20), qc).status == "drop_deviation"  # 1920 > 1152 * 1.5
    assert counts.detector_qc(dd(5), qc).status == "drop_deviation"  # 480 < 960 / 1.5
    assert counts.detector_qc(dd(15), qc).status == "flag_deviation"  # 1440 > 1152 * 1.2
    assert counts.detector_qc(dd(12.5), qc).status == "ok"  # within x1.2 of the range


def test_qc_compares_only_bins_valid_on_every_day():
    # The QC day has a gap where the target is huge: those bins must not count.
    day = dd(10)
    day.values[:48] = 1000.0
    qc = dd(10, missing=range(48))
    assert counts.detector_qc(day, [qc]).status != "drop_deviation"


def test_approach_series_sums_and_keeps_gaps_as_missing():
    a = counts.Approach("1-1", 1, "X St", "NB", "full", [1, 2], ["data_engineer_read"] * 2)
    day = {(1, 1): dd(3, missing=(5,)), (1, 2): dd(4)}
    qcs = [{(1, 1): dd(3), (1, 2): dd(4)}]
    series, q, why = counts.approach_series(a, day, qcs)
    assert why == "" and series[0] == 7 and np.isnan(series[5])  # never filled


def test_approach_series_drops_on_any_faulty_detector():
    a = counts.Approach("1-1", 1, "X St", "NB", "full", [1, 2], ["data_engineer_read"] * 2)
    day = {(1, 1): dd(3), (1, 2): dd(fully=True)}
    series, _, why = counts.approach_series(a, day, [{(1, 1): dd(3), (1, 2): dd(4)}])
    assert series is None and "det 2: fully_alarmed" in why


def test_bin_starts_are_local_15_minute_bins_and_reject_dst_days():
    b = counts.bin_starts(dt.date(2026, 10, 23))
    assert len(b) == 96 and b[0].isoformat() == "2026-10-23T00:00:00+11:00"
    assert b[34].strftime("%H:%M") == "08:30"
    with pytest.raises(SystemExit):
        counts.bin_starts(dt.date(2026, 10, 4))  # AEST -> AEDT


def _table(**over):
    t0 = counts.bin_starts(dt.date(2026, 10, 23))[32]
    cols = {
        "site_id": pa.array(["2902"], pa.string()),
        "edge_id": pa.array(["e1"], pa.string()),
        "direction": pa.array(["EB"], pa.string()),
        "bin_start": pa.array([t0], pa.timestamp("s", tz="Australia/Melbourne")),
        "vehicles": pa.array([12], pa.int32()),
    }
    cols.update(over)
    return pa.table(cols)


def test_validator_accepts_contract_table():
    counts.validate_table(_table(), {"e1"})


@pytest.mark.parametrize(
    "over, edges",
    [
        ({"edge_id": pa.array(["nope"], pa.string())}, {"e1"}),
        ({"direction": pa.array(["UP"], pa.string())}, {"e1"}),
        ({"vehicles": pa.array([-1], pa.int32())}, {"e1"}),
        (
            {
                "bin_start": pa.array(
                    [dt.datetime(2026, 10, 23, 8, 7, tzinfo=counts.TZ)],
                    pa.timestamp("s", tz="Australia/Melbourne"),
                )
            },
            {"e1"},
        ),
        (
            {
                "bin_start": pa.array(
                    [dt.datetime(2026, 10, 23, 8, 0, tzinfo=dt.UTC)], pa.timestamp("s", tz="UTC")
                )
            },
            {"e1"},
        ),
    ],
)
def test_validator_rejects(over, edges):
    with pytest.raises(ValueError):
        counts.validate_table(_table(**over), edges)


def test_validator_rejects_extra_column():
    t = _table().append_column("extra", pa.array([1]))
    with pytest.raises(ValueError):
        counts.validate_table(t, {"e1"})


def _approach():
    return counts.Approach("9-1", 9, "William St", "SB", "full", [4], ["data_engineer_read"])


def _series(base=10.0, am=None, night=None):
    v = np.full(96, base)
    if am is not None:
        v[list(counts.AM_BINS)] = am
    if night is not None:
        v[list(counts.NIGHT_BINS)] = night
    return v


def _qc(base=10.0, am=None, night=None):
    return {(9, 4): DetectorDay(_series(base, am, night), 0, False)}


def test_profile_check_drops_an_am_window_far_from_the_qc_median():
    # Whole-day totals stay within x1.5 but the AM peak is 1.6x: the qa 4583-4 case.
    qcs = [_qc(am=60), _qc(am=52), _qc(am=62)]
    why = counts.profile_check(_approach(), _series(am=96), qcs)
    assert why.startswith("profile: AM 07-10") and "1.60x" in why
    assert counts.profile_check(_approach(), _series(am=80), qcs) == ""


def test_profile_check_drops_a_chattering_loop_overnight():
    qcs = [_qc(night=2), _qc(night=5), _qc(night=10)]  # night totals 32, 80, 160
    why = counts.profile_check(_approach(), _series(night=40), qcs)  # 640 > 3 x 160 and +480
    assert why.startswith("profile: overnight")
    assert counts.profile_check(_approach(), _series(night=12), qcs) == ""  # 192: fine


def test_profile_check_needs_valid_bins_and_qc_days():
    s = _series(am=500)
    s[30] = np.nan  # a gap in the AM window: not judged (the bin is dropped anyway)
    assert counts.profile_check(_approach(), s, [_qc(am=10)]) == ""
    assert counts.profile_check(_approach(), _series(am=500), []) == ""


def test_approach_series_reports_profile_drops():
    day = {(9, 4): DetectorDay(_series(am=96), 0, False)}
    series, _, why = counts.approach_series(_approach(), day, [_qc(am=60), _qc(am=52), _qc(am=62)])
    assert series is None and why.startswith("profile:")
