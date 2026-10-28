"""qa-reviewer: height_source must describe where height_m actually came from.

CLAUDE.md: `height_source` records where each height came from "so nothing looks measured when it is
estimated". These tests fail on commit 31a9277 (outline remainders capped to a type-default part
height keep their `osm_height` / `osm_levels` label; the cap ignores City of Melbourne heights).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from shapely.geometry import box

from pipeline.buildings import ComIndex, OsmFeature, build_features
from pipeline.config import BUILDINGS_BBOX

PATH = Path(__file__).resolve().parents[2] / "data" / "processed" / "buildings.geojson"
METRES_PER_LEVEL = 3.2


@pytest.mark.skipif(not PATH.exists(), reason="buildings.geojson not built yet")
def test_osm_levels_height_equals_levels_times_3_2() -> None:
    feats = json.loads(PATH.read_text(encoding="utf-8"))["features"]
    bad = [
        f["properties"]
        for f in feats
        if f["properties"]["height_source"] == "osm_levels"
        and (
            f["properties"]["levels"] is None
            or abs(f["properties"]["height_m"] - f["properties"]["levels"] * METRES_PER_LEVEL)
            > 0.051
        )
    ]
    assert not bad, f"{len(bad)} osm_levels features whose height_m != levels x 3.2: {bad[:5]}"


def _sq(cx: float, cy: float, half: float):
    return box(cx - half, cy - half, cx + half, cy + half)


def test_outline_remainder_not_capped_by_type_default_when_part_has_com_height() -> None:
    """Mirrors Convesso (w711024215, height=102) in the real data: its only building:part has no
    height tags but a 102 m City of Melbourne height, yet the outline remainder is emitted at
    9.0 m (the part's *type default*) still labelled `osm_height`."""
    outline = OsmFeature(
        "w1", {"building": "yes", "height": "102"}, _sq(144.951, -37.819, 0.002), is_part=False
    )
    part = OsmFeature("w2", {"building:part": "yes"}, _sq(144.951, -37.819, 0.0008), is_part=True)
    outline.parts.append(part)
    com_geom = _sq(144.951, -37.819, 0.0008)
    from shapely.strtree import STRtree

    com = ComIndex([com_geom], [102.0], STRtree([com_geom]))
    out, _ = build_features([outline, part], com, BUILDINGS_BBOX)
    props = {f["properties"]["id"]: f["properties"] for f in out}
    assert props["w2"]["height_m"] == 102.0
    rem = props["w1"]
    # Either the remainder keeps a real height, or its label must not claim it is the OSM tag.
    assert rem["height_m"] > 9.0 or rem["height_source"] != "osm_height", rem
