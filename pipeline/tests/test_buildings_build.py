"""Build logic on small inline fixtures (no network, no raw files)."""

import json

import shapely
from shapely.geometry import Polygon, box, shape
from shapely.strtree import STRtree

from pipeline.buildings import (
    HEIGHT_SOURCES,
    PROPERTY_KEYS,
    ComIndex,
    OsmFeature,
    build_features,
    clean_geometry,
    validate_feature_collection,
    write_geojson,
)
from pipeline.config import BBox

# A tiny test bbox; the real ones live in pipeline/config.py.
BB = BBox(west=144.950, south=-37.820, east=144.960, north=-37.810)


def sq(x0, y0, size=0.001):
    return box(x0, y0, x0 + size, y0 + size)


def osm(fid, geom, is_part=False, **tags):
    tags = {k.replace("__", ":"): v for k, v in tags.items()}
    if "building" not in tags and "building:part" not in tags:
        tags["building:part" if is_part else "building"] = "yes"
    return OsmFeature(fid, tags, geom, is_part)


def com_index(items):
    geoms = [g for g, _ in items]
    return ComIndex(geoms, [h for _, h in items], STRtree(geoms))


def fc_of(features):
    return {"type": "FeatureCollection", "features": features}


def by_id(features):
    return {f["properties"]["id"]: f for f in features}


def test_every_height_source_and_schema():
    feats = [
        osm("w1", sq(144.951, -37.819), height="45 m"),
        osm("w2", sq(144.953, -37.819), building__levels="10"),
        osm("w3", sq(144.955, -37.819)),
        osm("w4", sq(144.957, -37.819), building="house"),
    ]
    com = com_index([(sq(144.9551, -37.8189, 0.0008), 33.0)])
    out, _ = build_features(feats, com, BB)
    assert validate_feature_collection(fc_of(out), BB) == []
    got = {
        k: (v["properties"]["height_source"], v["properties"]["height_m"])
        for k, v in by_id(out).items()
    }
    assert got == {
        "w1": ("osm_height", 45.0),
        "w2": ("osm_levels", 32.0),
        "w3": ("com_open_data", 33.0),
        "w4": ("type_default", 7.0),
    }
    for f in out:
        p = f["properties"]
        assert tuple(sorted(p)) == tuple(sorted(PROPERTY_KEYS))
        assert isinstance(p["id"], str)
        assert isinstance(p["height_m"], float) and p["height_m"] > 0
        assert p["levels"] is None or isinstance(p["levels"], int | float)
        assert p["height_source"] in HEIGHT_SOURCES
    assert by_id(out)["w2"]["properties"]["levels"] == 10
    assert by_id(out)["w1"]["properties"]["levels"] is None


def test_com_height_not_used_when_osm_has_one():
    com = com_index([(sq(144.951, -37.819), 99.0)])
    out, _ = build_features([osm("w1", sq(144.951, -37.819), building__levels="2")], com, BB)
    assert out[0]["properties"]["height_source"] == "osm_levels"


# Two CoM footprints inside one polygon: a big low podium and a small tall tower.
PODIUM = sq(144.951, -37.819, 0.0008)
TOWER = sq(144.95192, -37.81892, 0.00006)


def test_com_tallest_footprint_wins_for_whole_buildings():
    com = com_index([(PODIUM, 20.0), (TOWER, 150.0)])
    out, _ = build_features([osm("w1", sq(144.951, -37.819))], com, BB)
    assert out[0]["properties"]["height_m"] == 150.0


def test_com_tallest_footprint_wins_for_parts_and_orphan_parts():
    com = com_index([(PODIUM, 20.0), (TOWER, 150.0)])
    outline = osm("w2", sq(144.9505, -37.8195, 0.002), height="5")
    part = osm("w3", sq(144.951, -37.819), is_part=True)
    out, _ = build_features([outline, part], com, BB)
    assert by_id(out)["w3"]["properties"]["height_m"] == 150.0
    out, stats = build_features([osm("w4", sq(144.951, -37.819), is_part=True)], com, BB)
    assert stats["orphan_parts_kept_standalone"] == 1
    assert by_id(out)["w4"]["properties"]["height_m"] == 150.0


def test_com_largest_overlap_wins_for_outline_remainders():
    # The remainder (outline minus the part in the far corner) contains PODIUM and TOWER.
    com = com_index([(PODIUM, 20.0), (TOWER, 150.0)])
    outline = osm("w5", sq(144.951, -37.819, 0.002))
    part = osm("w6", sq(144.9522, -37.8178, 0.0008), is_part=True, height="60")
    out, _ = build_features([outline, part], com, BB)
    rem = by_id(out)["w5"]["properties"]
    assert (rem["height_m"], rem["height_source"]) == (20.0, "com_open_data")


def test_rule_b_uses_largest_overlap_for_remainders():
    com = com_index([(PODIUM, 20.0), (TOWER, 150.0)])
    outline = osm("w7", sq(144.951, -37.819, 0.002), height="200")
    part = osm("w8", sq(144.9522, -37.8178, 0.0008), is_part=True, height="200")
    out, _ = build_features([outline, part], com, BB)
    rem = by_id(out)["w7"]["properties"]
    assert (rem["height_m"], rem["height_source"]) == (20.0, "com_open_data")


def test_com_overlap_tie_broken_by_fraction_inside():
    # Largest-overlap mode (remainders): same overlap area, so the footprint lying wholly inside
    # beats the one partly outside.
    whole = box(144.9510, -37.8190, 144.9515, -37.8185)  # fully inside
    half = box(144.9515, -37.8190, 144.9523, -37.8185)  # same overlap, 62.5% inside
    com = com_index([(half, 70.0), (whole, 30.0)])
    poly = sq(144.951, -37.819)
    assert com.height_for(poly, largest_overlap=True) == 30.0
    assert com.height_for(poly) == 70.0  # tallest mode


def test_com_cover_fallback_only_for_similar_sized_structure():
    poly = sq(144.951, -37.819, 0.001)
    # Covers 60% of the polygon but only a third of it lies inside (so rule 1 doesn't apply);
    # at 1.8x the polygon's area it is taken as the same building.
    similar = com_index([(box(144.9514, -37.8190, 144.9532, -37.8180), 40.0)])
    out, _ = build_features([osm("w1", poly)], similar, BB)
    assert out[0]["properties"]["height_source"] == "com_open_data"
    assert out[0]["properties"]["height_m"] == 40.0
    # A much bigger structure (e.g. a station around an elevator part) is not used.
    huge = com_index([(sq(144.9505, -37.8195, 0.003), 55.0)])
    out, _ = build_features([osm("w1", poly)], huge, BB)
    assert out[0]["properties"]["height_source"] == "type_default"


def test_com_mostly_outside_is_not_matched():
    com = com_index([(sq(144.9518, -37.8190), 80.0)])  # overlaps only a thin strip
    out, _ = build_features([osm("w1", sq(144.951, -37.819))], com, BB)
    assert out[0]["properties"]["height_source"] == "type_default"


def test_clip_to_bbox_and_no_coordinates_outside():
    straddle = box(144.9495, -37.8155, 144.9505, -37.8145)  # crosses the west edge
    outside = sq(144.940, -37.815)
    out, _ = build_features([osm("w1", straddle), osm("w2", outside)], None, BB)
    assert [f["properties"]["id"] for f in out] == ["w1"]
    xs, ys = shapely.get_coordinates(shape(out[0]["geometry"])).T
    assert xs.min() >= BB.west and xs.max() <= BB.east
    assert ys.min() >= BB.south and ys.max() <= BB.north


def test_coordinates_rounded_and_rfc7946_orientation():
    g = Polygon(
        [(144.9512345678, -37.8187654321), (144.9512345678, -37.8177654321), (144.9522, -37.8177)]
    )  # clockwise input
    out, _ = build_features([osm("w1", g)], None, BB)
    geom = out[0]["geometry"]
    for x, y in geom["coordinates"][0]:
        assert round(x, 6) == x and round(y, 6) == y
    assert shape(geom).exterior.is_ccw


def test_multipolygon_relation_with_hole_kept():
    outer = sq(144.951, -37.819, 0.002)
    hole = sq(144.9515, -37.8185, 0.001)
    courtyard = outer.difference(hole)
    out, _ = build_features([osm("r9", courtyard, height="20")], None, BB)
    geom = out[0]["geometry"]
    assert out[0]["properties"]["id"] == "r9"
    assert geom["type"] == "Polygon" and len(geom["coordinates"]) == 2
    assert not shapely.LinearRing(geom["coordinates"][1]).is_ccw  # hole is clockwise


def test_building_parts_replace_outline_without_overlap():
    outline = osm("w10", sq(144.951, -37.819, 0.002), height="20")
    tower = osm("w11", sq(144.9512, -37.8188, 0.0008), is_part=True, height="250")
    crown = osm("w12", sq(144.9514, -37.8186, 0.0004), is_part=True, height="260", min_height="240")
    out, stats = build_features([outline, tower, crown], None, BB)
    assert validate_feature_collection(fc_of(out), BB) == []
    ids = by_id(out)
    assert set(ids) == {"w10", "w11", "w12"}
    assert ids["w10"]["properties"]["height_m"] == 20.0  # podium remainder
    geoms = {k: shape(v["geometry"]) for k, v in ids.items()}
    # no footprint drawn twice
    for a in geoms:
        for b in geoms:
            if a < b:
                assert geoms[a].intersection(geoms[b]).area < 1e-12
    # total footprint equals the outline's
    total = sum(g.area for g in geoms.values())
    assert abs(total - sq(144.951, -37.819, 0.002).area) < 1e-10


# Outline remainder rule. Outline is a 0.002 square; the part sits in its south-west corner;
# REM_COM is a City of Melbourne footprint lying wholly in the remainder (the L-shaped podium).
OUTLINE = sq(144.951, -37.819, 0.002)
PART = sq(144.951, -37.819, 0.0008)
REM_COM = sq(144.9520, -37.8180, 0.0008)


def test_remainder_uses_own_chain_no_cap_from_parts():
    # (a) Outline taller than its only part and no CoM data: keeps its own height and label.
    outline = osm("w20", OUTLINE, height="300")
    part = osm("w21", PART, is_part=True, height="100")
    out, stats = build_features([outline, part], None, BB)
    rem = by_id(out)["w20"]["properties"]
    assert (rem["height_m"], rem["height_source"]) == (300.0, "osm_height")
    assert stats["outline_remainders_whole_height_tag_to_com"] == 0


def test_remainder_whole_height_tag_without_com_match_keeps_own_label():
    # (b) Tag >= tallest part, CoM exists but nothing matches the remainder: no invention.
    outline = osm("w22", OUTLINE, height="300")
    part = osm("w23", PART, is_part=True, height="100")
    com = com_index([(sq(144.958, -37.812), 50.0)])  # elsewhere
    out, _ = build_features([outline, part], com, BB)
    rem = by_id(out)["w22"]["properties"]
    assert (rem["height_m"], rem["height_source"]) == (300.0, "osm_height")


def test_remainder_lower_than_part_keeps_own_tag_even_with_com():
    # (a) Podium tagged lower than the tower part (Eureka-style): the tag is the podium height.
    outline = osm("w30", OUTLINE, building__levels="3")
    part = osm("w31", PART, is_part=True, height="250")
    com = com_index([(REM_COM, 40.0)])
    out, _ = build_features([outline, part], com, BB)
    rem = by_id(out)["w30"]["properties"]
    assert (rem["height_m"], rem["height_source"], rem["levels"]) == (9.6, "osm_levels", 3)


def test_untagged_remainder_uses_com_then_type_default():
    # (a) No outline tags: the remainder's own chain reaches CoM, else the type default.
    outline = osm("w40", OUTLINE)
    part = osm("w41", PART, is_part=True, height="50")
    out, _ = build_features([outline, part], com_index([(REM_COM, 12.0)]), BB)
    assert by_id(out)["w40"]["properties"]["height_source"] == "com_open_data"
    assert by_id(out)["w40"]["properties"]["height_m"] == 12.0
    out, _ = build_features([osm("w42", OUTLINE), osm("w43", PART, is_part=True)], None, BB)
    rem = by_id(out)["w42"]["properties"]
    assert (rem["height_m"], rem["height_source"]) == (9.0, "type_default")


def test_remainder_whole_height_tag_replaced_by_com_match():
    # (b) Convesso-style: outline height=102 equals the tower part, so it is the whole building's
    # height; the podium remainder takes its City of Melbourne height instead.
    outline = osm("w50", OUTLINE, height="102")
    part = osm("w51", PART, is_part=True, height="102")
    out, stats = build_features([outline, part], com_index([(REM_COM, 20.0)]), BB)
    rem = by_id(out)["w50"]["properties"]
    assert (rem["height_m"], rem["height_source"]) == (20.0, "com_open_data")
    assert by_id(out)["w51"]["properties"]["height_m"] == 102.0
    assert stats["outline_remainders_whole_height_tag_to_com"] == 1


def test_remainder_whole_height_levels_tag_replaced_by_com_match():
    # (b) also applies when the outline height came from building:levels.
    outline = osm("w60", OUTLINE, building__levels="30")
    part = osm("w61", PART, is_part=True, height="50")
    out, _ = build_features([outline, part], com_index([(REM_COM, 15.0)]), BB)
    rem = by_id(out)["w60"]["properties"]
    assert (rem["height_m"], rem["height_source"]) == (15.0, "com_open_data")


def test_orphan_part_emitted_standalone_unless_covered():
    lone = osm(
        "w80", sq(144.958, -37.812), is_part=True, building__part="hotel", building__levels="10"
    )
    covering = osm("w81", sq(144.953, -37.816, 0.002), height="30")
    hidden = osm("w82", sq(144.9535, -37.8155, 0.0005), is_part=True, height="5")
    # hidden attaches to w81 as a normal part. w85 straddles two outlines, each holding less than
    # half of it, so it is an orphan, but one that other outlines cover.
    left = osm("w83", box(144.9510, -37.8190, 144.9520, -37.8180), height="12")
    right = osm("w84", box(144.9520, -37.8190, 144.9524, -37.8180), height="12")
    # 40% in w83, 40% in w84, 20% in neither: an orphan that is 80% covered.
    straddle = osm("w85", box(144.9516, -37.8188, 144.9526, -37.8182), is_part=True, height="40")
    out, stats = build_features([lone, covering, hidden, left, right, straddle], None, BB)
    ids = by_id(out)
    assert ids["w80"]["properties"]["height_source"] == "osm_levels"
    assert ids["w80"]["properties"]["levels"] == 10
    assert "w85" not in ids  # covered by other outlines, so not emitted as a standalone
    assert stats["orphan_parts_kept_standalone"] == 1
    assert stats["orphan_parts_skipped_covered"] == 1
    assert validate_feature_collection(fc_of(out), BB) == []


def test_parts_already_attached_are_not_duplicated():
    outline = osm("w90", sq(144.951, -37.819, 0.002), height="20")
    part = osm("w91", sq(144.951, -37.819, 0.0008), is_part=True, height="50")
    outline.parts.append(part)
    out, _ = build_features([outline, part], None, BB)
    assert sorted(f["properties"]["id"] for f in out) == ["w90", "w91"]


def test_nested_outlines_taller_keeps_area():
    podium = osm("w30", sq(144.951, -37.819, 0.002), height="20")
    tower = osm("w31", sq(144.9515, -37.8185, 0.0005), height="200")
    out, _ = build_features([podium, tower], None, BB)
    g = {k: shape(v["geometry"]) for k, v in by_id(out).items()}
    assert g["w30"].intersection(g["w31"]).area < 1e-12
    assert g["w31"].area == sq(144.9515, -37.8185, 0.0005).area


def test_invalid_geometry_is_repaired_and_degenerate_dropped():
    bowtie = Polygon(
        [(144.951, -37.819), (144.952, -37.818), (144.952, -37.819), (144.951, -37.818)]
    )
    sliver = Polygon([(144.955, -37.815), (144.9550000001, -37.815), (144.955, -37.814)])
    out, _ = build_features([osm("w1", bowtie), osm("w2", sliver)], None, BB)
    assert [f["properties"]["id"] for f in out] == ["w1"]
    g = shape(out[0]["geometry"])
    assert g.is_valid and not g.is_empty
    assert clean_geometry(sq(144.0, -37.0), BB) is None


def test_validator_catches_violations():
    good, _ = build_features([osm("w1", sq(144.951, -37.819), height="10")], None, BB)
    assert validate_feature_collection(fc_of(good), BB) == []

    def broken(mutate):
        f = json.loads(json.dumps(good[0]))
        mutate(f)
        return validate_feature_collection(fc_of([f]), BB)

    assert broken(lambda f: f["properties"].update(extra=1))
    assert broken(lambda f: f["properties"].pop("levels"))
    assert broken(lambda f: f["properties"].update(height_m=0))
    assert broken(lambda f: f["properties"].update(height_m="10"))
    assert broken(lambda f: f["properties"].update(height_source="test_fixture"))
    assert broken(lambda f: f["properties"].update(levels=-1))
    assert broken(lambda f: f["properties"].update(levels=2.5))
    assert broken(lambda f: f.update(geometry={"type": "Point", "coordinates": [144.951, -37.819]}))
    assert broken(lambda f: f["geometry"]["coordinates"][0].__setitem__(0, [145.5, -37.819]))
    assert broken(
        lambda f: f["geometry"]["coordinates"][0].__setitem__(0, [144.9510001234, -37.819])
    )


def test_write_geojson_roundtrip(tmp_path):
    out, _ = build_features([osm("w1", sq(144.951, -37.819), height="10")], None, BB)
    p = tmp_path / "b.geojson"
    write_geojson(out, p)
    fc = json.loads(p.read_text())
    assert "crs" not in fc
    assert validate_feature_collection(fc, BB) == []


def test_validator_catches_clockwise_exterior():
    good, _ = build_features([osm("w1", sq(144.951, -37.819), height="10")], None, BB)
    f = json.loads(json.dumps(good[0]))
    f["geometry"]["coordinates"][0].reverse()
    assert validate_feature_collection(fc_of([f]), BB)
