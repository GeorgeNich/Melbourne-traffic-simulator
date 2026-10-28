"""Point-cloud roof statistic and the cloud-vs-CoM rule (pipeline/building_pointcloud.py)."""

import numpy as np
from shapely.geometry import box

from pipeline.building_pointcloud import (
    AGREE_M,
    MIN_POINTS,
    apply_rule,
    crown_of,
    datum_offset,
    ground_low,
    roof_regions,
    roof_surface,
    shows_structure,
)
from pipeline.building_tiers import Tier

rng = np.random.default_rng(7)


def test_flat_roof_with_plant_and_parapet_minorities():
    roof = 40 + rng.normal(0, 0.05, 400)
    plant = 43 + rng.normal(0, 0.3, 60)  # lift overrun, AC units
    edge = 10 + rng.normal(0, 1, 40)  # a lower roof seen past the edge
    r = roof_surface(np.concatenate([roof, plant, edge]).astype(np.float32))
    assert r.z is not None and abs(r.z - 40) < 0.1 and r.method == "median"


def test_tower_roof_with_facade_points_uses_the_dominant_level():
    roof = 240 + rng.normal(0, 0.1, 300)
    facade = rng.uniform(20, 240, 500)  # wall points spread over the whole height
    r = roof_surface(np.concatenate([roof, facade]).astype(np.float32))
    assert r.method == "dominant_level" and abs(r.z - 240) < 0.3


def test_too_few_points_or_no_dominant_surface_is_not_used():
    assert roof_surface(np.full(MIN_POINTS - 1, 5.0, dtype=np.float32)).z is None
    r = roof_surface(rng.uniform(0, 100, 500).astype(np.float32))
    assert r.z is None and r.reason


def test_rule_confirms_within_tolerance():
    assert apply_rule(100.0, 100.0 + AGREE_M - 0.1, "20180528") == (100.0, "com_pointcloud_2018")


def test_rule_keeps_newer_com_capture_when_the_cloud_disagrees():
    # a tower finished after 2018: the 2018 cloud shows the construction site
    assert apply_rule(260.0, -10.0, "20230511") == (260.0, "com_open_data")
    assert apply_rule(30.0, 25.0, "20180528") == (30.0, "com_open_data")  # same capture: keep CoM


def test_rule_prefers_the_cloud_only_over_an_older_com_capture():
    assert apply_rule(30.0, 22.0, "20150101") == (22.0, "com_pointcloud_2018")


def test_rule_without_a_measurement_keeps_com():
    assert apply_rule(30.0, None, "20180528") == (30.0, "com_open_data")


def _tier(oid, geom, base, top, date="20180528", flat=True):
    return Tier("1", [str(oid)], geom, base, top, 10.0, [date], flat=flat)


def test_datum_offset_is_the_median_over_flat_2018_tiers():
    class R:
        def __init__(self, z):
            self.z, self.method = z, "median"

    tiers = [_tier(i, box(0, 0, 1, 1), 0, 20) for i in range(300)]
    roofs = [R(30.0 + 0.2) for _ in tiers]  # ground 10 + top 20 = 30 AHD; cloud 0.2 m higher
    d = datum_offset(tiers, roofs, [30.0] * 300)
    assert d["used"] and abs(d["offset_m"] - 0.2) < 1e-6
    # too few tiers: not used, offset 0
    assert not datum_offset(tiers[:10], roofs[:10], [30.0] * 10)["used"]


def test_roof_region_excludes_what_stands_on_the_roof():
    x, y = 144.95, -37.81
    d = 0.0003
    podium = _tier(1, box(x - d, y - d, x + d, y + d), 0, 20)
    tower = _tier(2, box(x - d / 3, y - d / 3, x + d / 3, y + d / 3), 20, 100)
    rp, rt = roof_regions([podium, tower])
    assert rp is not None and rt is not None
    assert not rp.intersects(rt)  # the tower's footprint is not part of the podium's roof
    assert rp.area < podium.geom.area * 1e10  # in MGA metres, smaller than the full footprint


def test_ground_low_is_a_low_percentile():
    zs = np.concatenate([np.full(80, 2.0), np.full(20, 30.0)]).astype(np.float32)
    assert ground_low(zs) == 2.0
    assert ground_low(np.ones(5, dtype=np.float32)) is None


# --- crowns ---------------------------------------------------------------------------------


def _crown_pts(n, x0, x1, y0, y1, z):
    return np.column_stack([rng.uniform(x0, x1, n), rng.uniform(y0, y1, n), np.full(n, z)])


_REGION = box(0, 0, 40, 40)
_COM_TOP = 100.0


def _crown(pts):
    return crown_of(pts, _REGION, 0.0, 0.0, _COM_TOP)


def test_crown_accepted_with_percentile_top():
    pts = np.vstack([_crown_pts(400, 10, 30, 10, 30, 120.0), _crown_pts(3, 15, 16, 15, 16, 160.0)])
    c = _crown(pts)
    assert isinstance(c, tuple)
    hull, top, n, area = c
    assert 119.0 <= top <= 121.0  # the 3 antenna outliers do not set the top
    assert n == 403 and area > 300


def test_crown_rejected_as_crane_like():
    # a 39 m x 3 m jib: returns bunched at a dozen spots along it (few occupied cells, big hull)
    x = np.repeat(np.linspace(0.5, 38.5, 12), 15) + rng.uniform(0, 0.1, 180)
    y = np.tile([20.5, 23.5, 20.5], 60) + rng.uniform(0, 0.1, 180)
    pts = np.column_stack([x, y, np.full(180, 130.0)])
    assert _crown(pts) == "crane"


def test_crown_too_small_or_too_few_points():
    assert _crown(_crown_pts(40, 10, 11, 10, 11, 120.0)) is None  # 1 m2 footprint
    assert _crown(_crown_pts(10, 10, 30, 10, 30, 120.0)) is None  # under MIN points


def test_crown_needs_a_clear_excess_over_the_com_top():
    assert _crown(_crown_pts(400, 10, 30, 10, 30, 104.0)) is None  # only 4 m
    assert _crown(_crown_pts(400, 10, 30, 10, 30, 100.5)) is None  # not above the rise


def test_crown_not_confirmed_by_the_cloud():
    t = _tier(1, box(0, 0, 10, 10), 0.0, 100.0)
    r = roof_surface(np.full(200, 60.0))  # the cloud roof is 40 m below the CoM top
    assert not shows_structure([t], [r], 0.0, None)
    assert not shows_structure([t], [None], 0.0, 60.0)
    assert shows_structure([t], [roof_surface(np.full(200, 110.0))], 0.0, None)
