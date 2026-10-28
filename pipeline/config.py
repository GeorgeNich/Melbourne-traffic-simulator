"""Study-area boundaries. The single source of truth for every clip in the project.

Bboxes are (west, south, east, north) in WGS84 degrees. Approved by George, Phase 0.
"""

import math
from typing import NamedTuple


class BBox(NamedTuple):
    west: float
    south: float
    east: float
    north: float

    def contains(self, other: "BBox") -> bool:
        return (
            self.west <= other.west
            and self.south <= other.south
            and self.east >= other.east
            and self.north >= other.north
        )


# CBD (Hoddle Grid) + Southbank + Docklands: the road network that gets simulated.
SIM_BBOX = BBox(west=144.935, south=-37.833, east=144.975, north=-37.805)

# Wider ring rendered as ghost buildings only (MCG, Arts Precinct, Carlton edge, South Melbourne).
BUILDINGS_BBOX = BBox(west=144.925, south=-37.840, east=144.990, north=-37.795)

# Extra margin around SIM_BBOX for the road extract (osm_sim_area.osm.gz, roads_3d.geojson), so
# roads that leave the simulated area are not cut off right at its edge.
SIM_BUFFER_M = 300.0

_M_PER_DEG_LAT = 111_320.0


def buffered(bbox: BBox, metres: float) -> BBox:
    """`bbox` grown by `metres` on every side (equirectangular, at the bbox's mid latitude)."""
    mid_lat = (bbox.south + bbox.north) / 2
    dlat = metres / _M_PER_DEG_LAT
    dlon = metres / (_M_PER_DEG_LAT * math.cos(math.radians(mid_lat)))
    return BBox(bbox.west - dlon, bbox.south - dlat, bbox.east + dlon, bbox.north + dlat)


# SIM_BBOX + SIM_BUFFER_M: the clip for the road extract.
SIM_EXTRACT_BBOX = buffered(SIM_BBOX, SIM_BUFFER_M)

# Phase 2 Stage B (orchestrator, 2026-10-27): CityLink motorway TUNNEL ways only (`tunnel=yes`,
# `highway=motorway` / `motorway_link`; pipeline.roads.extended_tunnel_clip) are clipped at the
# building ring's east edge (BUILDINGS_BBOX.east, 144.990) instead of SIM_EXTRACT_BBOX's, in both
# osm_sim_area.osm.gz and roads_3d.geojson, so the Burnley (way 8066124, eastbound) and Domain
# (way 8066126, westbound) tunnels include their Yarra crossings (~144.9784 / ~144.9797) for the
# X-ray tunnel review. West, south and north stay SIM_EXTRACT_BBOX's. SUMO sees the longer tunnel
# edges (boundary through-routes); every other way keeps the SIM_EXTRACT_BBOX clip.
TUNNEL_EXTRACT_BBOX = BBox(
    west=SIM_EXTRACT_BBOX.west,
    south=SIM_EXTRACT_BBOX.south,
    east=BUILDINGS_BBOX.east,
    north=SIM_EXTRACT_BBOX.north,
)


# Named places George reviews closely (PLAN.md "Road network verification"). Used only to group
# results in the verification reports (pipeline/checks/); they clip nothing.
REVIEW_AREAS: dict[str, BBox] = {
    "kings_way_west_gate_interchange": BBox(144.955, -37.832, 144.962, -37.826),
    "footscray_rd_west_gate_tunnel_viaducts": BBox(144.928, -37.814, 144.946, -37.801),
    "collins_la_trobe_bridges_southern_cross": BBox(144.946, -37.8215, 144.955, -37.8125),
    "charles_grimes_bridge": BBox(144.945, -37.826, 144.951, -37.821),
}


# --- Building extent: the whole City of Melbourne (Phase 2 stacked tiers, George 2026-10-27) ---
# Render only; the simulated road network is unchanged. BUILDINGS_AREA = the City of Melbourne
# municipal boundary (City of Melbourne open data, data/raw/com_municipal_boundary.geojson, see
# data/SOURCES.md) UNION the rectangle BUILDINGS_BBOX (so South Melbourne, Albert Park edge and
# Fitzroy/Richmond edges already in the ring stay). BUILDINGS_BBOX keeps its meaning for anything
# that needs the original ring rectangle; BUILDINGS_AREA_BBOX is the envelope of BUILDINGS_AREA
# (rounded outward to 0.001 degrees; pipeline/tests/test_buildings_area.py checks it).
BUILDINGS_AREA_BOUNDARY_FILE = "com_municipal_boundary.geojson"  # in data/raw/
BUILDINGS_AREA_BBOX = BBox(west=144.896, south=-37.851, east=144.992, north=-37.775)


def com_boundary(raw_dir=None):
    """The City of Melbourne municipal boundary as a shapely (Multi)Polygon, WGS84."""
    import json
    from pathlib import Path

    from shapely.geometry import shape

    raw = Path(raw_dir) if raw_dir else Path(__file__).resolve().parent.parent / "data" / "raw"
    fc = json.loads((raw / BUILDINGS_AREA_BOUNDARY_FILE).read_text())
    return shape(fc["features"][0]["geometry"])


def buildings_area(raw_dir=None):
    """BUILDINGS_AREA: the City of Melbourne boundary UNION the BUILDINGS_BBOX rectangle."""
    from shapely.geometry import box

    return com_boundary(raw_dir).union(box(*BUILDINGS_BBOX))


# Landmarks for the building-height checks (lon, lat on the tallest part; the report lists the
# City of Melbourne structure there, its tiers, the point cloud and OSM `height`). Clip nothing.
BUILDING_LANDMARKS: dict[str, tuple[float, float]] = {
    "Eureka Tower": (144.96455, -37.82155),
    "Australia 108": (144.96368, -37.82323),
    "Rialto (south tower)": (144.95778, -37.81885),
    "120 Collins St": (144.96951, -37.81383),
    "101 Collins St": (144.97064, -37.81490),
    "Aurora Melbourne Central": (144.96226, -37.80963),
    "West Side Place Tower 1": (144.95268, -37.81466),
    "Melbourne Central office tower": (144.96212, -37.81083),
    "Arts Centre spire": (144.96843, -37.82150),
    "MCG": (144.98340, -37.81990),
    "Marvel Stadium": (144.94750, -37.81640),
    "Southern Cross Station roof": (144.95247, -37.81850),
}
