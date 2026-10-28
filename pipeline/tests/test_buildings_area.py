"""The building extent: City of Melbourne boundary UNION BUILDINGS_BBOX (pipeline/config.py)."""

import pytest
from shapely.geometry import box

from pipeline.config import (
    BUILDING_LANDMARKS,
    BUILDINGS_AREA_BBOX,
    BUILDINGS_AREA_BOUNDARY_FILE,
    BUILDINGS_BBOX,
    SIM_BBOX,
    buildings_area,
    com_boundary,
)
from pipeline.download import RAW_DIR

needs_boundary = pytest.mark.skipif(
    not (RAW_DIR / BUILDINGS_AREA_BOUNDARY_FILE).exists(), reason="boundary not downloaded"
)


def test_area_bbox_contains_the_ring_bbox():
    assert BUILDINGS_AREA_BBOX.contains(BUILDINGS_BBOX)
    assert BUILDINGS_AREA_BBOX.contains(SIM_BBOX)


@needs_boundary
def test_area_is_boundary_union_ring_and_bbox_is_its_envelope():
    area = buildings_area()
    cob = com_boundary()
    assert area.contains(box(*BUILDINGS_BBOX).buffer(-1e-9))
    assert area.contains(cob.buffer(-1e-9))
    assert box(*BUILDINGS_AREA_BBOX).contains(area)
    # the envelope is rounded outward by less than 0.001 degrees on every side
    w, s, e, n = area.bounds
    b = BUILDINGS_AREA_BBOX
    assert 0 <= w - b.west < 1e-3 and 0 <= s - b.south < 1e-3
    assert 0 <= b.east - e < 1e-3 and 0 <= b.north - n < 1e-3
    # the City of Melbourne is ~37.7 km2 (the published municipal area)
    import math

    km2 = cob.area * 111_320.0 * 110_950.0 * math.cos(math.radians(-37.81)) / 1e6
    assert 36 < km2 < 39


@needs_boundary
def test_landmarks_are_inside_the_city_of_melbourne():
    from shapely.geometry import Point

    cob = com_boundary()
    assert all(cob.contains(Point(x, y)) for x, y in BUILDING_LANDMARKS.values())
