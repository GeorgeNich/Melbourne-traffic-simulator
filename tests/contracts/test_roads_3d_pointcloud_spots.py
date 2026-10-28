"""qa spot checks: roads_3d deck heights vs the City of Melbourne 3D Point Cloud 2018, read here.

Written by qa-reviewer, independently of pipeline.deck_pointcloud (own reader: laspy + pyproj,
EPSG:7844 -> EPSG:28355 as published, histogram-mode surface in a 2 m disc). Skips when the raw
tiles are not in this checkout's data/raw/ (they are gitignored; worktrees symlink them).

Each spot is a deck vertex (or the deck at a crossing point) and the STREET level it should be
measured from. Street levels (point-cloud heights, the cloud's own datum; only differences matter)
are qa readings of at-grade road surfaces next to the spot. Expected: drawn z within TOL_M of
(deck surface - street level). The tolerance is visual, not survey grade: decks are drawn on flat
model ground, so 1.5 m.

The Bolte, Docklands viaduct and West Gate over Kings Way spots were findings of the Stage A
re-review (fixed in round 3).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
ROADS_3D = ROOT / "data" / "processed" / "roads_3d.geojson"
PC_DIR = ROOT / "data" / "raw" / "com_pointcloud_2018"
TOL_M = 1.5
DISC_M = 2.0

pytestmark = pytest.mark.skipif(
    not (PC_DIR / "MANIFEST.json").exists(), reason="point-cloud tiles not in data/raw/"
)


# (label, way_id, vertex index or None, crossing way or None, street level, marks)
SPOTS = [
    ("Kings Way flyover over Whiteman St", 11589928, None, 581714224, 1.05, ()),
    ("West Gate 50123947 over Clarke St", 50123947, None, 26664760, 1.4, ()),
    ("West Gate 484853234 over Moray St", 484853234, None, 51253611, 1.13, ()),
    (
        "West Gate 484853234 over Kings Way",
        484853234,
        None,
        980542677,
        0.9,
        (),
    ),
    ("La Trobe St over Southern Cross Access Rd", 26431519, None, 741754566, 9.7, ()),
    ("CityLink 207128345 over Footscray Rd", 207128345, None, 581658408, 2.1, ()),
    ("Princes Bridge mid-span", 52137278, 6, None, 10.15, ()),
    ("Queens Bridge crown", 208652070, 5, None, 2.93, ()),
    ("Power St over the CityLink cutting", 5234831, 6, None, 1.2, ()),
    (
        "Bolte Bridge 34314083 mid-span",
        34314083,
        24,
        None,
        2.29,
        (),
    ),
    (
        "CityLink 285898777 Docklands viaduct",
        285898777,
        30,
        None,
        2.29,
        (),
    ),
    (
        "Footscray Rd Offramp 1306290847",
        1306290847,
        15,
        None,
        2.29,
        (),
    ),
]


@pytest.fixture(scope="module")
def roads() -> dict[int, dict]:
    fc = json.loads(ROADS_3D.read_text(encoding="utf-8"))
    return {f["properties"]["way_id"]: f for f in fc["features"]}


@pytest.fixture(scope="module")
def tiles() -> dict[Path, tuple]:
    import laspy

    out = {}
    for p in sorted(PC_DIR.glob("*.las")):
        with laspy.open(p) as fh:
            out[p] = (fh.header.mins, fh.header.maxs)
    return out


def _to_mga(lon: float, lat: float) -> tuple[float, float]:
    from pyproj import Transformer

    return Transformer.from_crs("EPSG:7844", "EPSG:28355", always_xy=True).transform(lon, lat)


def _surface(tiles: dict, x: float, y: float) -> float:
    """Top surface in a DISC_M disc: the highest histogram mode (0.5 m bins, >= 6 points)."""
    import laspy

    zs = []
    for p, (mn, mx) in tiles.items():
        if mn[0] - DISC_M <= x <= mx[0] + DISC_M and mn[1] - DISC_M <= y <= mx[1] + DISC_M:
            las = laspy.read(p)
            px, py = np.asarray(las.x), np.asarray(las.y)
            m = (px - x) ** 2 + (py - y) ** 2 <= DISC_M**2
            zs.append(np.asarray(las.z)[m])
    z = np.concatenate(zs) if zs else np.empty(0)
    assert len(z) >= 15, f"too few points at ({x:.1f}, {y:.1f})"
    h, e = np.histogram(z, bins=np.arange(np.floor(z.min()), np.ceil(z.max()) + 0.6, 0.5))
    peaks = [
        (e[k] + e[k + 1]) / 2
        for k in range(len(h))
        if h[k] >= 6
        and h[k] >= (h[k - 1] if k else 0)
        and h[k] >= (h[k + 1] if k + 1 < len(h) else 0)
    ]
    top = max(peaks)
    return float(np.median(z[np.abs(z - top) <= 0.5]))


def _crossing(a: list, b: list) -> tuple[float, float]:
    from shapely.geometry import LineString

    inter = LineString([c[:2] for c in a]).intersection(LineString([c[:2] for c in b]))
    g = next(iter(getattr(inter, "geoms", [inter])))
    return g.x, g.y


def _z_at(line: list, lon: float, lat: float) -> float:
    k = math.cos(math.radians(lat))
    best = (math.inf, 0.0)
    for a, b in zip(line, line[1:], strict=False):
        dx, dy = (b[0] - a[0]) * k, b[1] - a[1]
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else ((lon - a[0]) * k * dx + (lat - a[1]) * dy) / L2
        t = max(0.0, min(1.0, t))
        d = math.hypot(a[0] + t * (b[0] - a[0]) - lon, a[1] + t * (b[1] - a[1]) - lat)
        if d < best[0]:
            best = (d, a[2] + t * (b[2] - a[2]))
    return best[1]


@pytest.mark.parametrize(
    ("label", "way", "vertex", "cross", "street"),
    [pytest.param(*s[:5], marks=s[5], id=s[0]) for s in SPOTS],
)
def test_deck_height_matches_point_cloud(
    roads, tiles, label: str, way: int, vertex: int | None, cross: int | None, street: float
):
    line = roads[way]["geometry"]["coordinates"]
    if cross is None:
        lon, lat, drawn = line[vertex]
    else:
        lon, lat = _crossing(line, roads[cross]["geometry"]["coordinates"])
        drawn = _z_at(line, lon, lat) - _z_at(roads[cross]["geometry"]["coordinates"], lon, lat)
    deck = _surface(tiles, *_to_mga(lon, lat))
    want = deck - street
    assert abs(drawn - want) <= TOL_M, f"{label}: drawn {drawn:.2f} m, cloud {want:.2f} m"


# --- Stage B: the CityLink cutting under Power St --------------------------------------------
# Street level around the cutting: qa reading, median of 31 at-grade road surfaces (Sturt St,
# Grant St, Bale Circuit, the Power St ramp) 40-120 m from the cutting: 1.66 (cloud datum).
CUTTING_STREET = 1.66
# The cutting is measured (z_source measured); qa's own readings agree within ~0.3 m. Tighter
# than the deck tolerance: the floor is open to the sky and the street level is local.
CUTTING_TOL_M = 0.75
# (label, way, vertex): cutting-floor vertices open to the sky
CUTTING_SPOTS = [
    ("Kings-CityLink ramp 5234953 v12", 5234953, 12),
    ("Kings-CityLink ramp 5234953 v16", 5234953, 16),
    ("Kings-CityLink ramp 5234953 v20", 5234953, 20),
    ("CityLink 5234624 v9", 5234624, 9),
    ("CityLink 5234624 v12", 5234624, 12),
    ("CityLink 5234624 v19", 5234624, 19),
    ("Kings-CityLink ramp 1175874270 v3", 1175874270, 3),
    ("Kings-CityLink ramp 1175874270 v6", 1175874270, 6),
    ("CityLink 5234646 v3", 5234646, 3),
    ("CityLink 5234646 v9", 5234646, 9),
    ("CityLink-Kings ramp 5234957 v8", 5234957, 8),
]


@pytest.mark.parametrize(
    ("label", "way", "vertex"), [pytest.param(*s, id=s[0]) for s in CUTTING_SPOTS]
)
def test_cutting_depth_matches_point_cloud(roads, tiles, label: str, way: int, vertex: int):
    lon, lat, drawn = roads[way]["geometry"]["coordinates"][vertex]
    want = _surface(tiles, *_to_mga(lon, lat)) - CUTTING_STREET
    assert want < -2.0, f"{label}: not in the cutting ({want:.2f} m)"
    assert abs(drawn - want) <= CUTTING_TOL_M, f"{label}: drawn {drawn:.2f} m, cloud {want:.2f} m"


# (label, deck way, cutting way): decks over the cutting, drawn height above street level
DECK_OVER_CUTTING_SPOTS = [
    ("West Gate 26619046 over the cutting ramp 5234953", 26619046, 5234953),
    ("Power St 5234831 over CityLink 5234646", 5234831, 5234646),
    ("Power St 5234818 over CityLink 5234646", 5234818, 5234646),
    ("Power St 19012681 over the ramp 5234957", 19012681, 5234957),
    ("West Gate 50123940 over the ramp 5234953", 50123940, 5234953),
]


@pytest.mark.parametrize(
    ("label", "way", "cross"), [pytest.param(*s, id=s[0]) for s in DECK_OVER_CUTTING_SPOTS]
)
def test_deck_over_cutting_matches_point_cloud(roads, tiles, label: str, way: int, cross: int):
    """Stage B: the deck keeps its height above street level and the cutting floor is drawn
    below it (26619046 came down 0.55 m to its measurement over the shallow end of the ramp)."""
    line = roads[way]["geometry"]["coordinates"]
    lower = roads[cross]["geometry"]["coordinates"]
    lon, lat = _crossing(line, lower)
    drawn = _z_at(line, lon, lat)
    want = _surface(tiles, *_to_mga(lon, lat)) - CUTTING_STREET
    assert abs(drawn - want) <= CUTTING_TOL_M, f"{label}: drawn {drawn:.2f} m, cloud {want:.2f} m"
    assert _z_at(lower, lon, lat) < drawn - 4.0, f"{label}: cutting floor not 4 m below the deck"
