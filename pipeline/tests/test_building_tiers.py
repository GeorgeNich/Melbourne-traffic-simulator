"""Stacked City of Melbourne tiers (pipeline/building_tiers.py) on small inline slices."""

from collections import Counter

from shapely.geometry import box

from pipeline.building_tiers import (
    MAX_TIERS,
    THIN_M,
    Tier,
    build_tiers,
    max_stack,
    unsupported_elevated,
)

X, Y = 144.95, -37.81
M = 1 / 100_000  # ~1 m in degrees (good enough for ratios)


def sq(half_m: float, dx_m: float = 0.0, dy_m: float = 0.0):
    cx, cy = X + dx_m * M, Y + dy_m * M
    return box(cx - half_m * M, cy - half_m * M, cx + half_m * M, cy + half_m * M)


def t(oid, geom, base, top, date="20180528", sid="1", flat=True):
    return Tier(sid, [str(oid)], geom, base, top, 10.0, [date], flat=flat)


def run(*slices):
    st = Counter()
    out = build_tiers({"1": list(slices)}, st)
    return sorted(out, key=lambda x: (x.base, x.top)), st


def test_tower_on_podium_keeps_both_tiers_and_ids():
    out, _ = run(t(1, sq(30), 0, 20), t(2, sq(10), 20, 150))
    assert [(x.base, x.top) for x in out] == [(0, 20), (20, 150)]
    assert [x.id for x in out] == ["c1_1", "c1_2"]


def test_thin_tier_merges_into_most_similar_neighbour():
    # 1.5 m slice between a podium and a tower, same footprint as the tower
    out, st = run(t(1, sq(30), 0, 20), t(2, sq(10), 20, 21.5), t(3, sq(10), 21.5, 150))
    assert st["thin_merged"] == 1
    assert [(x.base, x.top) for x in out] == [(0, 20), (20, 150)]
    assert all(x.thickness >= THIN_M for x in out)
    assert out[1].objectids == ["2", "3"] and out[1].id == "c1_2"


def test_small_thin_plant_on_a_wide_roof_is_dropped():
    out, st = run(t(1, sq(30), 0, 20), t(2, sq(2), 20, 21.5))
    assert st["thin_dropped_small"] == 1
    assert [(x.base, x.top) for x in out] == [(0, 20)]


def test_thin_canopy_alone_stays():
    out, _ = run(t(1, sq(5), 0, 1.5))
    assert [(x.base, x.top) for x in out] == [(0, 1.5)]


def test_near_identical_stacked_tiers_merge_no_staircase():
    out, st = run(t(1, sq(30), 0, 10), t(2, sq(29.5), 10, 20), t(3, sq(29), 20, 30))
    assert st["similar_merged"] == 2
    assert [(x.base, x.top) for x in out] == [(0, 30)]


def test_cap_limits_stack_depth():
    slices = [t(i, sq(40 - 3 * i), 5 * i, 5 * i + 5) for i in range(10)]
    out, st = run(*slices)
    assert max_stack(out) <= MAX_TIERS
    assert st["cap_merged"] >= 1
    assert out[0].base == 0 and max(x.top for x in out) == 50


def test_side_by_side_parts_are_not_merged_by_the_cap():
    slices = [t(i, sq(4, dx_m=20 * i), 0, 10 + i) for i in range(9)]
    out, _ = run(*slices)
    assert len(out) == 9


def test_older_capture_superseded_by_newer():
    out, st = run(t(1, sq(10), 0, 30, "20180528"), t(2, sq(10), 0, 45, "20230511"))
    assert st["superseded_older_capture"] == 1
    assert [(x.base, x.top, x.id) for x in out] == [(0, 45, "c1_2")]


def test_lower_roof_inside_taller_slice_loses_shared_area():
    out, st = run(t(1, sq(20), 0, 8), t(2, sq(8), 0, 40))
    assert st["z_contained_trimmed"] == 1
    low = next(x for x in out if x.top == 8)
    assert not low.geom.contains(sq(7).centroid)  # the tower's footprint is cut out


def test_unsupported_low_slice_on_sloping_ground_goes_to_ground():
    out, st = run(t(1, sq(10), 1.5, 9))
    assert st["terrain_base_to_ground"] == 1 and out[0].base == 0


def test_high_unsupported_slice_stays_elevated():
    # a sky bridge 6 m up between two parts of a structure
    out, st = run(t(1, sq(5, dx_m=-20), 0, 20), t(2, sq(5, dx_m=20), 0, 20), t(3, sq(4), 6, 10))
    bridge = next(x for x in out if x.objectids == ["3"])
    assert bridge.base == 6 and st["elevated_kept"] == 1
    assert unsupported_elevated(out) == [bridge]


def test_gap_under_a_slice_is_closed_onto_its_support():
    out, st = run(t(1, sq(20), 0, 20), t(2, sq(10), 23, 60))
    assert st["gap_closed"] == 1
    assert [(x.base, x.top) for x in out] == [(0, 20), (20, 60)]


def test_spire_resting_on_a_small_plinth_keeps_its_base():
    # Arts Centre: wide roof to 30 m, a plinth 30-42.5 m, the spire from 42.5 m (1/4 over it)
    out, st = run(
        t(1, sq(30), 0, 30), t(2, sq(4, dx_m=2), 30, 42.5), t(3, sq(2, dx_m=7), 42.5, 150)
    )
    assert st["gap_closed"] == 0
    spire = next(x for x in out if x.top == 150)
    assert spire.base == 42.5
