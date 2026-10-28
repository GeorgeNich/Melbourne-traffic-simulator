from pipeline.config import (
    BUILDINGS_BBOX,
    SIM_BBOX,
    SIM_EXTRACT_BBOX,
    TUNNEL_EXTRACT_BBOX,
    BBox,
)


def _is_valid(b: BBox) -> bool:
    return -180 <= b.west < b.east <= 180 and -90 <= b.south < b.north <= 90


def test_bboxes_are_valid():
    assert _is_valid(SIM_BBOX)
    assert _is_valid(BUILDINGS_BBOX)


def test_building_ring_contains_sim_area():
    assert BUILDINGS_BBOX.contains(SIM_BBOX)


def test_bboxes_are_in_melbourne():
    # Melbourne CBD is around 144.96 E, 37.81 S.
    for b in (SIM_BBOX, BUILDINGS_BBOX):
        assert 144.8 < b.west < b.east < 145.1
        assert -37.9 < b.south < b.north < -37.7


def test_tunnel_extract_bbox_widens_only_the_east_edge_to_the_building_ring():
    t, e = TUNNEL_EXTRACT_BBOX, SIM_EXTRACT_BBOX
    assert (t.west, t.south, t.north) == (e.west, e.south, e.north)
    assert t.east == BUILDINGS_BBOX.east > e.east
    assert t.contains(e) and BUILDINGS_BBOX.contains(BBox(t.west, -37.83, t.east, -37.81))
    # the Burnley and Domain tunnels cross the Yarra near 144.9784 / 144.9797
    assert e.east < 144.9797 < t.east
