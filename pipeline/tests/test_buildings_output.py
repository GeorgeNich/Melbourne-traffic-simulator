"""Checks on the committed data/processed/buildings.geojson. Skipped if the file is absent."""

import json
from collections import Counter

import pytest

from pipeline.buildings import HEIGHT_SOURCES, OUT_PATH, REPORT_PATH, validate_feature_collection
from pipeline.config import BUILDINGS_AREA_BBOX, BUILDINGS_AREA_BOUNDARY_FILE, buildings_area
from pipeline.download import RAW_DIR

pytestmark = pytest.mark.skipif(not OUT_PATH.exists(), reason="buildings.geojson not built")


@pytest.fixture(scope="module")
def fc():
    return json.loads(OUT_PATH.read_text())


@pytest.fixture(scope="module")
def props(fc):
    return {f["properties"]["id"]: f["properties"] for f in fc["features"]}


def test_contract(fc):
    area = buildings_area() if (RAW_DIR / BUILDINGS_AREA_BOUNDARY_FILE).exists() else None
    assert validate_feature_collection(fc, BUILDINGS_AREA_BBOX, area) == []


def test_not_empty_and_all_sources_known(fc):
    feats = fc["features"]
    assert len(feats) > 1000
    sources = Counter(f["properties"]["height_source"] for f in feats)
    assert set(sources) <= set(HEIGHT_SOURCES)
    # the point cloud confirms most City of Melbourne tiers
    assert sources["com_pointcloud_2018"] > sources["com_open_data"]


def test_skyline_is_plausible(fc):
    # Australia 108 (~317 m) is Melbourne's tallest; nothing should exceed ~330 m.
    heights = sorted((f["properties"]["height_m"] for f in fc["features"]), reverse=True)
    assert 290 <= heights[0] <= 330


def test_stacked_tiers_present(fc):
    lifted = [f for f in fc["features"] if f["properties"]["min_height_m"] > 0]
    assert len(lifted) > 1000
    assert all(f["properties"]["min_height_m"] < f["properties"]["height_m"] for f in lifted)
    # outside the City of Melbourne (OSM ids) pieces stand on the ground unless carved / tagged
    osm_lifted = [f for f in lifted if not f["properties"]["id"].startswith("c")]
    assert len(osm_lifted) < 100


def test_known_issues_fixed(props):
    # Metro Tunnel station boxes (OSM level < 0) are not drawn
    for wid in ("w1453811151", "w1453811185", "w1453811187", "w1453811195"):
        assert wid not in props
    for wid in ("w1453780441", "w1453780448"):
        assert wid not in props
    # sky bridges over Little Lonsdale / Little Bourke St start at their OSM min_height
    assert props["w272707125"]["min_height_m"] == 5.0
    assert props["w272707148"]["min_height_m"] == 6.0
    # Convention Centre Place under its bridge (OSM min_height 4.5) and Waterfall Lane are clear
    assert props["c800104_19666-v1"]["min_height_m"] == 4.5
    assert props["c801668_40567-v1"]["min_height_m"] >= 4.0


def test_eureka_is_a_tower_on_a_podium(props):
    eureka = sorted(
        (p["min_height_m"], p["height_m"]) for k, p in props.items() if k.startswith("c815210_")
    )
    assert eureka[0][0] == 0.0 and eureka[0][1] < 40
    assert 296 <= eureka[-1][1] <= 299
    assert len(eureka) <= 6


def test_report_written():
    r = json.loads(REPORT_PATH.read_text())
    assert r["pointcloud"]["datum_offset"]["used"]
    assert abs(r["pointcloud"]["datum_offset"]["offset_m"]) < 1.0
    assert r["pointcloud"]["cloud_replaced_com"] == 0  # no CoM capture predates the cloud


def test_file_size_under_limit():
    assert OUT_PATH.stat().st_size < 50 * 1024 * 1024


def _crowns(fc):
    return [f["properties"] for f in fc["features"] if f["properties"]["id"].endswith("_crown")]


def test_crowns_are_labelled_and_sit_on_their_parent(fc, props):
    crowns = _crowns(fc)
    assert len(crowns) > 0
    for c in crowns:
        assert c["height_source"] == "com_pointcloud_2018"
        assert c["min_height_m"] < c["height_m"]
    parents = [
        p for p in props.values() if not p["id"].endswith("_crown") and p["id"].startswith("c")
    ]
    tops = {p["height_m"] for p in parents}
    assert all(round(c["min_height_m"], 2) in {round(t, 2) for t in tops} for c in crowns)


def test_every_piece_has_base_below_top(props):
    assert all(p["min_height_m"] < p["height_m"] for p in props.values())


def test_collins_crowns(props):
    tops = sorted(p["height_m"] for p in props.values() if p["id"].endswith("_crown"))
    assert any(abs(t - 235.0) <= 2 for t in tops), "101 Collins crown ~235 m"
    assert any(abs(t - 260.0) <= 2 for t in tops), "120 Collins crown ~260 m"
