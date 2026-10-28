"""Build data/processed/buildings.geojson from the raw downloads in data/raw/.

Usage:
    uv run python -m pipeline.buildings              # rebuild from data/raw/ (no network)
    uv run python -m pipeline.buildings --download   # fetch missing raw files first, then rebuild

Contract (CLAUDE.md, stacked tiers approved 2026-10-27): RFC 7946 FeatureCollection, WGS84,
Polygon/MultiPolygon only, clipped to BUILDINGS_AREA (the City of Melbourne municipality UNION
BUILDINGS_BBOX, pipeline/config.py); properties exactly `id`, `height_m`, `levels`,
`height_source`, `min_height_m`. Each feature is a building PIECE drawn from `min_height_m` to
`height_m` (metres above the model's flat ground), so towers sit on their podiums and sky bridges
float over the street.

**Inside the City of Melbourne** (the municipal boundary) buildings are the City of Melbourne 2023
Building Footprints' stacked tiers (pipeline/building_tiers.py: older captures replaced, gaps and
sloping-ground bases tidied, thin tiers merged, near-identical tiers merged, at most MAX_TIERS
stacked), each checked against the 3D Point Cloud 2018 (pipeline/building_pointcloud.py):
    com_pointcloud_2018   the cloud's roof agrees with the tier top within AGREE_M (3 m)
    com_open_data         the cloud can't see the roof clearly, or disagrees (no CoM capture is
                          older than the cloud, so CoM is kept; listed in the report)
Crowns: where the cloud over a structure's upper roofs rises clearly above its CoM top (crown
frames and masts the CoM tops leave out: 101 and 120 Collins St) and the cloud shows the structure
as CoM has it, a crown piece `c<structure_id>_crown` (com_pointcloud_2018) stands on the CoM top up
to the cloud's 98th percentile (building_pointcloud.crowns).
An elevated tier with nothing of its structure beneath it whose ground beside it (point cloud)
is about as high as its base stands on sloping ground and goes down to 0.
OSM buildings that CoM tiers cover (>= COM_MATCH_SHARE of their area) are replaced by them; the
rest ("OSM-only") keep the OSM height chain below, minus the area CoM covers.

Open space under buildings (`carve`), everywhere (outside the City of Melbourne too, orchestrator
2026-10-27):
  - OSM elevated pieces: an OSM piece with `min_height` (else `building:min_level` x 3.2 m) that
    OSM maps nothing beneath (>= VOID_MIN_SHARE open: sky bridges over Little Lonsdale and
    Little Bourke St, canopies) starts at that height.
  - Roads through buildings: where a drivable at-grade road (data/processed/roads_3d.geojson)
    runs >= ROAD_IN_BUILDING_MIN_M inside a piece, the road's corridor is cleared up to its OSM
    `maxheight`, else ROAD_CLEARANCE_M (an assumption); where it passes under an OSM elevated
    piece, all the space OSM leaves open under that piece is cleared up to its min_height
    (Convention Centre Place, Waterfall Lane). CoM tiers are carved by roads only.
  - A CoM structure that a public street passes through and that the current OSM map does not
    have (OSM covers < STALE_OSM_COVER of it) is gone (Metro Tunnel construction sheds over
    Franklin St / A'Beckett St / Domain Rd): it is dropped, not carved.
Carved fragments over a void are separate pieces `<id>-v<n>`.

**Outside the City of Melbourne** (the rest of BUILDINGS_BBOX): OSM heights, from the ground
except where the open-space rules above apply (OSM `min_height`, roads through buildings).

Everywhere: OSM building parts that are underground only (`location=underground`, or every
`level` value < 0, e.g. the Metro Tunnel station boxes) are not drawn.

Ids: OSM pieces keep their OSM ids (`w<way>` / `r<relation>`); CoM tiers are
`c<structure_id>_<objectid of the tier's lowest CoM slice>`; carved fragments add `-v<n>`.
A verification report (counts, the point-cloud comparison, landmarks) goes to
data/processed/checks/buildings_heights.json.

OSM height rules, first match wins:
    1. OSM `height` (units parsed: "45", "45 m", "150 ft", "150'")       -> osm_height
    2. OSM `building:levels` x 3.2 m                                       -> osm_levels
    3. City of Melbourne 2023 Building Footprints (tallest matching
       footprint; largest overlap for outline remainders; ComIndex)        -> com_open_data
    4. Default by OSM building type (TYPE_DEFAULT_HEIGHTS)                 -> type_default

`building:part` handling (no double counting): a building outline that contains parts is emitted as
its parts plus the outline area no part covers. That remainder keeps the outline's id and runs its
own height chain on the outline's tags (no cap from the parts). Exception: if the outline's tagged
height (osm_height / osm_levels) is >= the tallest part's final height, the tag probably states the
whole building's height, so a City of Melbourne match for the remainder polygon is used instead
(com_open_data); with no match the outline's own height and label are kept.
Parts outside every outline are emitted as standalone buildings unless other outlines cover them.
Finally, OSM footprints are made non-overlapping where their height ranges overlap: the taller
keeps the area, so nothing is extruded twice (`remove_overlaps`).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import shapely
from shapely.geometry import MultiPolygon, Polygon, box, mapping, shape
from shapely.strtree import STRtree

from pipeline.config import BUILDINGS_AREA_BBOX, BBox

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "processed" / "buildings.geojson"

METRES_PER_LEVEL = 3.2
MAX_PLAUSIBLE_HEIGHT_M = 500.0  # tallest in Melbourne is ~317 m; anything above is a tagging error
COORD_DECIMALS = 6
HEIGHT_DECIMALS = 1
HEIGHT_SOURCES = (
    "osm_height",
    "osm_levels",
    "com_open_data",
    "com_pointcloud_2018",
    "type_default",
)
PROPERTY_KEYS = ("id", "height_m", "levels", "height_source", "min_height_m")

# Rough default heights (metres) by OSM `building=*` value, used only when nothing better exists.
# These are labelled `type_default` in the output so they never look measured.
TYPE_DEFAULT_HEIGHTS: dict[str, float] = {
    # small structures
    "shed": 3.0,
    "garage": 3.0,
    "garages": 3.0,
    "carport": 3.0,
    "hut": 3.0,
    "kiosk": 3.0,
    "toilets": 3.0,
    "cabin": 3.0,
    "service": 3.0,
    "transformer_tower": 3.0,
    "roof": 4.0,
    # houses
    "house": 7.0,
    "detached": 7.0,
    "semidetached_house": 7.0,
    "terrace": 7.0,
    "bungalow": 5.0,
    # multi-storey residential / commercial / civic
    "residential": 10.0,
    "apartments": 12.0,
    "dormitory": 12.0,
    "hotel": 15.0,
    "commercial": 12.0,
    "office": 15.0,
    "retail": 8.0,
    "supermarket": 8.0,
    "industrial": 8.0,
    "warehouse": 8.0,
    "school": 10.0,
    "university": 15.0,
    "college": 12.0,
    "hospital": 15.0,
    "public": 12.0,
    "civic": 12.0,
    "government": 12.0,
    "train_station": 10.0,
    "transportation": 8.0,
    "parking": 12.0,
    "church": 15.0,
    "cathedral": 25.0,
    "chapel": 8.0,
    "grandstand": 15.0,
    "stadium": 30.0,
    "sports_centre": 10.0,
    "construction": 9.0,
}
DEFAULT_HEIGHT_M = 9.0  # building=yes and anything unlisted (~3 storeys)

_NUM = r"(\d+(?:[.,]\d+)?)"
_RE_METRES = re.compile(rf"^~?\s*{_NUM}\s*(?:m|metres?|meters?)?$", re.IGNORECASE)
_RE_FEET = re.compile(rf"^~?\s*{_NUM}\s*(?:ft|feet|foot|'|′)$", re.IGNORECASE)
_RE_FEET_INCHES = re.compile(r"^(\d+)\s*(?:'|′|ft)\s*(\d+(?:\.\d+)?)\s*(?:\"|″|in)$", re.IGNORECASE)
_RE_FIRST_NUMBER = re.compile(_NUM)


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def _plausible(h: float | None) -> float | None:
    if h is None or not math.isfinite(h) or h <= 0 or h > MAX_PLAUSIBLE_HEIGHT_M:
        return None
    return h


def parse_height(value: str | None) -> float | None:
    """Parse an OSM `height` tag to metres. Returns None if missing, unparseable or implausible.

    Accepts "45", "45.5", "45,5", "45 m", "45m", "150 ft", "150'", "12'6\"". For multi-valued tags
    ("45;50") the first value is used.
    """
    if value is None:
        return None
    v = value.strip().split(";")[0].strip()
    if not v:
        return None
    if m := _RE_METRES.match(v):
        return _plausible(_num(m.group(1)))
    if m := _RE_FEET.match(v):
        return _plausible(_num(m.group(1)) * 0.3048)
    if m := _RE_FEET_INCHES.match(v):
        return _plausible(int(m.group(1)) * 0.3048 + float(m.group(2)) * 0.0254)
    return None


def parse_levels(value: str | None) -> float | None:
    """Parse `building:levels`: the first number in the value ("18/19" -> 18, "3;4" -> 3).
    Returns None if missing, non-numeric ("Ground Floor") or not > 0."""
    if value is None:
        return None
    m = _RE_FIRST_NUMBER.search(value)
    if not m:
        return None
    n = _num(m.group(1))
    if n <= 0 or n * METRES_PER_LEVEL > MAX_PLAUSIBLE_HEIGHT_M:
        return None
    return n


def type_default_height(building_value: str | None) -> float:
    return TYPE_DEFAULT_HEIGHTS.get((building_value or "").strip().lower(), DEFAULT_HEIGHT_M)


def choose_height(tags: dict[str, str], com_height: float | None) -> tuple[float, str]:
    """Apply the height rules in order. Returns (height_m, height_source)."""
    if (h := parse_height(tags.get("height"))) is not None:
        return h, "osm_height"
    if (n := parse_levels(tags.get("building:levels"))) is not None:
        return n * METRES_PER_LEVEL, "osm_levels"
    if (c := _plausible(com_height)) is not None:
        return c, "com_open_data"
    btype = tags.get("building") or tags.get("building:part")
    return type_default_height(btype), "type_default"


def levels_value(tags: dict[str, str]) -> int | None:
    """`levels` for the output: an int for a whole number of levels, else null."""
    n = parse_levels(tags.get("building:levels"))
    if n is None or not n.is_integer():
        return None
    return int(n)


# --------------------------------------------------------------------------------------------
# OSM


@dataclass
class OsmFeature:
    id: str  # "w<way id>" or "r<relation id>"
    tags: dict[str, str]
    geom: Polygon | MultiPolygon
    is_part: bool
    parts: list[OsmFeature] = field(default_factory=list)


def _is_building(tags: dict[str, str]) -> bool:
    b = tags.get("building")
    return b is not None and b != "no"


def _is_part(tags: dict[str, str]) -> bool:
    p = tags.get("building:part")
    return p is not None and p != "no" and not _is_building(tags)


def _levels_list(value: str | None) -> list[float]:
    """Numbers in an OSM `level` value ("-1", "-2;-1", "-1;0", "-3--1")."""
    if not value:
        return []
    return [float(m) for m in re.findall(r"-?\d+(?:\.\d+)?", value.replace("--", " -"))]


def is_underground(tags: dict[str, str]) -> bool:
    """Underground only: `location=underground`, or every OSM `level` value below 0."""
    if tags.get("location") == "underground":
        return True
    lv = _levels_list(tags.get("level"))
    return bool(lv) and max(lv) < 0


def _skip(tags: dict[str, str]) -> bool:
    return is_underground(tags)


def osm_min_height(tags: dict[str, str]) -> float | None:
    """Base of an OSM building (part) above ground: `min_height`, else `building:min_level` x
    3.2 m. None if untagged or not > 0."""
    if (h := parse_height(tags.get("min_height"))) is not None:
        return h
    n = parse_levels(tags.get("building:min_level"))
    return n * METRES_PER_LEVEL if n is not None else None


def load_osm(
    path: Path, bbox: BBox, area: Any | None = None, skipped: list[str] | None = None
) -> list[OsmFeature]:
    """Read building and building:part areas (closed ways and multipolygon relations) in bbox
    (and intersecting `area`, if given). Ids of underground-only ones go to `skipped`."""
    import osmium  # imported lazily so unit tests don't need the C extension loaded

    wkb = osmium.geom.WKBFactory()
    # Generous pre-filter so a building that only crosses the bbox edge is still read.
    pad = 0.02
    pre = box(bbox.west - pad, bbox.south - pad, bbox.east + pad, bbox.north + pad)
    clip = box(*bbox) if area is None else area
    shapely.prepare(clip)
    out: list[OsmFeature] = []
    fp = (
        osmium.FileProcessor(str(path))
        .with_areas(osmium.filter.KeyFilter("building", "building:part"))
        .with_filter(osmium.filter.KeyFilter("building", "building:part"))
    )
    for area in fp:
        if not area.is_area():
            continue
        tags = {t.k: t.v for t in area.tags}
        if not (_is_building(tags) or _is_part(tags)):
            continue
        if _skip(tags):
            if skipped is not None:
                skipped.append(("w" if area.from_way() else "r") + str(area.orig_id()))
            continue
        # Cheap bbox test on the first outer ring's first node before building geometry.
        try:
            first = next(iter(next(iter(area.outer_rings()))))
        except StopIteration:
            continue
        if not pre.contains(shapely.Point(first.lon, first.lat)):
            continue
        try:
            hexwkb = wkb.create_multipolygon(area)
        except RuntimeError:  # osmium could not assemble the rings; nothing to emit
            continue
        geom = shapely.from_wkb(hexwkb)
        if geom.is_empty or not geom.intersects(clip):
            continue
        fid = ("w" if area.from_way() else "r") + str(area.orig_id())
        out.append(OsmFeature(fid, tags, geom, _is_part(tags)))
    return out


def attach_parts(features: list[OsmFeature]) -> tuple[list[OsmFeature], dict[str, int]]:
    """Assign each building:part to the outline that contains it (largest overlap).

    Returns the outlines (each with `.parts` filled), the orphan parts that no outline contains,
    and counts for the report.
    """
    outlines = [f for f in features if not f.is_part]
    parts = [f for f in features if f.is_part]
    for o in outlines:
        o.parts = []
    tree = STRtree([o.geom for o in outlines])
    orphans: list[OsmFeature] = []
    for p in parts:
        best, best_frac = None, 0.0
        pa = p.geom.area or 1e-30
        for i in tree.query(p.geom, predicate="intersects"):
            frac = outlines[i].geom.intersection(p.geom).area / pa
            if frac > best_frac:
                best, best_frac = outlines[i], frac
        if best is not None and best_frac >= 0.5:
            best.parts.append(p)
        else:
            orphans.append(p)
    stats = {
        "outlines": len(outlines),
        "parts": len(parts),
        "outlines_replaced_by_parts": sum(1 for o in outlines if o.parts),
        "orphan_parts": len(orphans),
    }
    return outlines, orphans, stats


# --------------------------------------------------------------------------------------------
# City of Melbourne


@dataclass
class ComIndex:
    """City of Melbourne `Structure` footprints, each with its top above the structure's ground."""

    geoms: list[Any]
    heights: list[float]
    tree: STRtree

    def height_for(
        self, geom: Polygon | MultiPolygon, largest_overlap: bool = False
    ) -> float | None:
        """Height of the CoM footprint matching an OSM polygon, or None.

        1. Candidates are footprints with at least half of their area inside the polygon.
           Default (whole buildings, parts, orphan parts): the TALLEST candidate wins, because
           CoM footprints are stacked (tower on podium) and the tallest is the building's height.
           `largest_overlap=True` (outline remainders only, which are the low part by
           definition): the candidate with the largest overlap area wins (ties: larger fraction
           inside).
        2. Otherwise, a single footprint covering at least half of the polygon is used, but only
           if it is no more than COM_MAX_AREA_RATIO times the polygon's area. A much bigger
           footprint is a different building (e.g. a tiny elevator part inside a station), so
           the polygon falls through to the next rule instead.
        """
        idx = self.tree.query(geom, predicate="intersects")
        if len(idx) == 0:
            return None
        ga = geom.area or 1e-30
        best_inside: tuple[float, float, float] | None = None  # (overlap, frac inside, h)
        tallest: float | None = None
        best_cover: tuple[float, float, float] | None = None  # (fraction of polygon, area, h)
        for i in sorted(int(j) for j in idx):
            g = self.geoms[i]
            if g.area <= 0:
                continue
            inter = g.intersection(geom).area
            frac_inside = inter / g.area
            if frac_inside >= 0.5:
                h = self.heights[i]
                tallest = h if tallest is None else max(tallest, h)
                # Overlap compared at 1e-9 of the polygon's area so float noise doesn't beat
                # the tie-break on fraction inside.
                key = (round(inter / ga, 9), frac_inside, self.heights[i])
                if best_inside is None or key[:2] > best_inside[:2]:
                    best_inside = key
            cover = inter / ga
            if (
                cover >= 0.5
                and g.area <= COM_MAX_AREA_RATIO * ga
                and (best_cover is None or cover > best_cover[0])
            ):
                best_cover = (cover, g.area, self.heights[i])
        if best_inside is not None:
            return best_inside[2] if largest_overlap else tallest
        if best_cover is not None:
            return best_cover[2]
        return None


COM_MAX_AREA_RATIO = 2.0


def load_com(path: Path | None, bbox: BBox) -> ComIndex | None:
    """One entry per CoM `Structure` footprint (stacked footprints stay separate).

    Height = this footprint's top minus its structure's base elevation (both metres AHD), so a
    podium footprint carries the podium height and a tower footprint the tower height. Footprints
    are not merged by `structure_id`: CoM structures can span a station or a whole precinct
    (Southern Cross, the Convention Centre), which would hand a tower's height to everything.
    """
    if path is None or not path.exists():
        return None
    data = json.loads(path.read_text())
    clip = box(*bbox)
    geoms, heights = [], []
    for f in data["features"]:
        p = f.get("properties") or {}
        if p.get("footprint_type") != "Structure" or not f.get("geometry"):
            continue
        top, base = p.get("footprint_max_elevation"), p.get("structure_min_elevation")
        if top is None or base is None:
            continue
        h = _plausible(float(top) - float(base))
        if h is None:
            continue
        g = _polygons_only(shapely.make_valid(shape(f["geometry"])))
        if g is None or not g.intersects(clip):
            continue
        geoms.append(g)
        heights.append(h)
    return ComIndex(geoms, heights, STRtree(geoms))


# --------------------------------------------------------------------------------------------
# Geometry output


def _polygons_only(g: Any) -> Polygon | MultiPolygon | None:
    if g is None or g.is_empty:
        return None
    if isinstance(g, Polygon | MultiPolygon):
        return g
    polys: list[Polygon] = []
    for part in getattr(g, "geoms", []):
        if isinstance(part, Polygon):
            polys.append(part)
        elif isinstance(part, MultiPolygon):
            polys.extend(part.geoms)
    polys = [p for p in polys if not p.is_empty and p.area > 0]
    if not polys:
        return None
    return polys[0] if len(polys) == 1 else MultiPolygon(polys)


def _clip_geom(clip: BBox | Any) -> Any:
    if isinstance(clip, BBox):
        return box(*clip)
    shapely.prepare(clip)
    return clip


def clean_geometry(geom: Any, clip: BBox | Any) -> Polygon | MultiPolygon | None:
    """Clip to a bbox or a (prepared) shapely area, snap to a 1e-6 degree grid (keeps validity),
    orient per RFC 7946."""
    g = geom if geom.is_valid else shapely.make_valid(geom)
    c = _clip_geom(clip)
    if not c.contains(g):
        g = g.intersection(c)
    g = _polygons_only(g)
    if g is None:
        return None
    g = _polygons_only(shapely.set_precision(g, 10**-COORD_DECIMALS))
    if g is None or not g.is_valid or g.area <= 0:
        return None
    # RFC 7946: exterior rings counter-clockwise, holes clockwise.
    return shapely.orient_polygons(g, exterior_cw=False)


def _round_coords(obj: Any) -> Any:
    if isinstance(obj, float):
        return round(obj, COORD_DECIMALS)
    if isinstance(obj, list | tuple):
        return [_round_coords(x) for x in obj]
    return obj


def to_feature(fid: str, geom: Polygon | MultiPolygon, props: dict[str, Any]) -> dict:
    gj = mapping(geom)
    return {
        "type": "Feature",
        "geometry": {"type": gj["type"], "coordinates": _round_coords(gj["coordinates"])},
        "properties": {"id": fid, **props},
    }


def _area_m2(g: Any) -> float:
    """Approximate planar area in m² for small WGS84 geometries (local equirectangular)."""
    if g.is_empty:
        return 0.0
    lat = math.radians(g.centroid.y)
    return g.area * 111_320.0 * 110_950.0 * math.cos(lat)


MIN_PIECE_AREA_M2 = 2.0  # slivers left after subtracting overlaps are dropped


@dataclass
class Piece:
    id: str
    geom: Any
    tags: dict[str, str]
    is_remainder: bool = False  # outline area not covered by its parts


def split_outline(o: OsmFeature) -> list[Piece]:
    """Turn one outline (+ its building:parts) into pieces: the parts, plus the outline area no
    part covers. The remainder (last piece, `is_remainder=True`) keeps the outline's id and tags
    (Melbourne mappers often tag the podium height on the outline, e.g. Eureka Tower height=20);
    `build_features` caps it at the tallest part's final height.
    """
    if not o.parts:
        return [Piece(o.id, o.geom, o.tags)]
    parts = [shapely.make_valid(p.geom) for p in o.parts]
    rest = shapely.make_valid(o.geom).difference(shapely.union_all(parts))
    return [Piece(p.id, g, p.tags) for p, g in zip(o.parts, parts, strict=True)] + [
        Piece(o.id, rest, o.tags, is_remainder=True)
    ]


def _z_overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Height ranges [min_height_m, height_m] share more than Z_TOL_M."""
    lo = max(a.get("min_height_m", 0.0), b.get("min_height_m", 0.0))
    return min(a["height_m"], b["height_m"]) - lo > Z_TOL_M


Z_TOL_M = 0.3


def remove_overlaps(
    items: list[tuple[str, Any, dict[str, Any]]], bbox: BBox | Any
) -> tuple[list[tuple[str, Any, dict[str, Any]]], int]:
    """Make footprints non-overlapping where their height ranges overlap: the taller one keeps
    the area.

    OSM pieces are extruded from the ground here, so overlapping footprints (stacked
    building:parts such as Eureka Tower's crown over its shaft, a tower outline nested inside a
    podium outline) would otherwise be drawn twice. Pieces stacked one above the other (height
    ranges apart) both stay. Ties are broken by id so the result is deterministic.
    """
    order = sorted(range(len(items)), key=lambda i: (-items[i][2]["height_m"], items[i][0]))
    rank = {i: r for r, i in enumerate(order)}
    geoms = [it[1] for it in items]
    tree = STRtree(geoms)
    out, dropped = [], 0
    for i, (fid, g, props) in enumerate(items):
        taller = [
            geoms[j]
            for j in tree.query(g, predicate="intersects")
            if rank[j] < rank[i] and _z_overlap(props, items[j][2])
        ]
        if taller:
            cut = g.difference(shapely.union_all(taller))
            if cut.equals(g):
                out.append((fid, g, props))
                continue
            g2 = clean_geometry(cut, bbox)
            if g2 is None or _area_m2(g2) < MIN_PIECE_AREA_M2:
                dropped += 1
                continue
            g = g2
        out.append((fid, g, props))
    return out, dropped


def resolve_height(
    tags: dict[str, str], geom: Any, com: ComIndex | None, remainder: bool = False
) -> tuple[float, str, int | None]:
    """Full height chain for one footprint: (height_m, height_source, levels).

    `remainder=True` matches CoM by largest overlap instead of tallest (see ComIndex.height_for).
    """
    com_h = None
    needs_com = (
        parse_height(tags.get("height")) is None
        and parse_levels(tags.get("building:levels")) is None
    )
    if needs_com and com is not None:
        com_h = com.height_for(geom, largest_overlap=remainder)
    h, src = choose_height(tags, com_h)
    return h, src, levels_value(tags)


ORPHAN_COVERED_FRAC = 0.5  # an orphan part at least this much inside other outlines is skipped


@dataclass
class Void:
    """Open space under an elevated piece or around a road, carved out of pieces up to
    `clearance_m` above ground (`carve`)."""

    geom: Any
    clearance_m: float
    source: str  # "osm:<id>" (an OSM piece's min_height) or "road:<way id>"
    piece_id: str | None = None  # road voids: the piece the road passes through
    highway: str | None = None  # road voids: the road's class


VOID_MIN_M2 = 5.0  # smaller open areas under an elevated OSM piece are mapping noise
VOID_OPEN_M = 0.5  # ... as are strips narrower than 2 x this
VOID_MIN_SHARE = 0.5  # an OSM piece counts as elevated if at least this much of it is open below


def _buffer_m(g: Any, metres: float) -> Any:
    """Buffer a lon/lat geometry by `metres` (local equirectangular scaling)."""
    from shapely import affinity

    if g.is_empty:
        return g
    c = g.centroid
    k = math.cos(math.radians(c.y))
    flat = affinity.scale(g, xfact=k, yfact=1.0, origin=(c.x, c.y))
    out = flat.buffer(metres / 111_320.0, join_style="mitre")
    return affinity.scale(out, xfact=1.0 / k, yfact=1.0, origin=(c.x, c.y))


def _opened(g: Any, metres: float) -> Any:
    """Morphological opening: removes strips narrower than 2 x `metres`."""
    if g.is_empty:
        return g
    return _buffer_m(_buffer_m(g, -metres), metres).intersection(g)


def osm_voids(
    items: list[tuple[str, Any, dict[str, Any]]],
    tags_by_id: dict[str, dict[str, str]],
    inside: Any | None,
) -> list[Void]:
    """Open space under OSM elevated pieces (inside the City of Melbourne): the piece's footprint
    minus every other OSM piece whose own base (OSM min_height, else 0) is below its
    min_height (what OSM maps beneath it: a podium under a tower leaves nothing open). Only
    pieces that are mostly open below (>= VOID_MIN_SHARE: sky bridges, canopies, the Convention
    Centre Place bridge) count; a tower part over its podium with a few slivers open is not."""
    geoms = [g for _, g, _ in items]
    tree = STRtree(geoms)
    raw = {fid: osm_min_height(tags_by_id.get(fid, {})) or 0.0 for fid, _, _ in items}
    out: list[Void] = []
    for fid, g, props in items:
        c = raw[fid]
        if c <= 0 or c >= props["height_m"] - Z_TOL_M:
            continue
        if inside is not None and not inside(g):
            continue
        below = [
            geoms[j]
            for j in tree.query(g, predicate="intersects")
            if items[j][0] != fid and raw[items[j][0]] < c - Z_TOL_M
        ]
        v = g.difference(shapely.union_all(below)) if below else g
        v = _polygons_only(_opened(v, VOID_OPEN_M))
        if v is None or _area_m2(v) < VOID_MIN_M2 or v.area < VOID_MIN_SHARE * g.area:
            continue
        out.append(Void(v, round(c, HEIGHT_DECIMALS), f"osm:{fid}"))
    return out


def osm_items(
    osm: list[OsmFeature], com: ComIndex | None, clip: BBox | Any
) -> tuple[list[tuple[str, Any, dict[str, Any]]], dict[str, Any]]:
    """OSM buildings -> non-overlapping pieces from the ground with the OSM height chain."""
    outlines, orphans, stats = attach_parts(osm)

    # Orphan building:parts come back as standalone buildings unless other outlines cover them.
    outline_tree = STRtree([o.geom for o in outlines])
    orphans_kept = 0
    groups: list[OsmFeature] = list(outlines)
    for p in orphans:
        near = [outlines[i].geom for i in outline_tree.query(p.geom, predicate="intersects")]
        covered = (
            shapely.union_all(near).intersection(p.geom).area / (p.geom.area or 1e-30)
            if near
            else 0.0
        )
        if covered < ORPHAN_COVERED_FRAC:
            groups.append(OsmFeature(p.id, p.tags, p.geom, is_part=False))
            orphans_kept += 1

    items: list[tuple[str, Any, dict[str, Any]]] = []
    dropped = remainders = whole_height_to_com = 0
    for o in groups:
        pieces = split_outline(o)
        # Final heights of the parts (full chain incl. City of Melbourne), for the remainder rule.
        part_heights = [
            resolve_height(pc.tags, clean_geometry(pc.geom, clip) or pc.geom, com)
            for pc in pieces
            if not pc.is_remainder
        ]
        for piece in pieces:
            g = clean_geometry(piece.geom, clip)
            if g is None or _area_m2(g) < MIN_PIECE_AREA_M2:
                dropped += 1
                continue
            h, src, levels = resolve_height(piece.tags, g, com, remainder=piece.is_remainder)
            if piece.is_remainder:
                remainders += 1
                tallest_part = max(t[0] for t in part_heights)
                if src in ("osm_height", "osm_levels") and h >= tallest_part and com is not None:
                    # The outline tag likely states the whole building's height, not the podium.
                    c = _plausible(com.height_for(g, largest_overlap=True))
                    if c is not None:
                        h, src = c, "com_open_data"
                        whole_height_to_com += 1
            props = {
                "height_m": round(h, HEIGHT_DECIMALS),
                "levels": levels,
                "height_source": src,
                "min_height_m": 0.0,
            }
            items.append((piece.id, g, props))
    items, overlap_dropped = remove_overlaps(items, clip)
    stats["orphan_parts_kept_standalone"] = orphans_kept
    stats["orphan_parts_skipped_covered"] = len(orphans) - orphans_kept
    stats["outline_remainders_kept"] = remainders
    stats["outline_remainders_whole_height_tag_to_com"] = whole_height_to_com
    stats["dropped_empty_or_sliver"] = dropped
    stats["dropped_fully_inside_taller"] = overlap_dropped
    return items, stats


def build_features(
    osm: list[OsmFeature], com: ComIndex | None, bbox: BBox | Any
) -> tuple[list[dict], dict[str, Any]]:
    """OSM-only build (the behaviour outside the City of Melbourne): features from the ground."""
    items, stats = osm_items(osm, com, bbox)
    features = [to_feature(fid, g, props) for fid, g, props in items]
    features.sort(key=lambda f: f["properties"]["id"])
    return features, stats


# --------------------------------------------------------------------------------------------
# City of Melbourne tiers, OSM matching, carving


COM_MATCH_SHARE = 0.5  # an OSM piece this much covered by CoM tiers is replaced by them
SLIVER_OPEN_M = 0.75  # an OSM remnant (after removing CoM cover) narrower than 1.5 m is dropped


def tier_items(
    tiers: list[Any], sources: dict[str, tuple[float, str]], clip: BBox | Any
) -> tuple[list[tuple[str, Any, dict[str, Any]]], int]:
    """CoM tiers -> pieces. `sources`: tier id -> (height_m, height_source) after the cloud
    rule. Returns the items and the number dropped (empty after clipping / too thin)."""
    out, dropped = [], 0
    for t in tiers:
        h, src = sources.get(t.id, (t.top, "com_open_data"))
        base = round(t.base, HEIGHT_DECIMALS)
        h = round(h, HEIGHT_DECIMALS)
        g = clean_geometry(t.geom, clip)
        if g is None or _area_m2(g) < MIN_PIECE_AREA_M2 or h - base < 0.1:
            dropped += 1
            continue
        props = {"height_m": h, "levels": None, "height_source": src, "min_height_m": base}
        out.append((t.id, g, props))
    return out, dropped


def crown_items(crowns: list[Any], clip: BBox | Any) -> list[tuple[str, Any, dict[str, Any]]]:
    """Crown pieces (building_pointcloud.crowns) on top of their structures: from the CoM top
    to the cloud's crown top, `c<structure_id>_crown`."""
    out = []
    for c in crowns:
        g = clean_geometry(c.geom, clip)
        if g is None or _area_m2(g) < MIN_PIECE_AREA_M2:
            continue
        props = {
            "height_m": round(c.top, HEIGHT_DECIMALS),
            "levels": None,
            "height_source": "com_pointcloud_2018",
            "min_height_m": round(c.base, HEIGHT_DECIMALS),
        }
        out.append((f"c{c.structure_id}_crown", g, props))
    return out


def match_osm_to_com(
    osm: list[tuple[str, Any, dict[str, Any]]],
    com_cover: Any,
    inside: Any,
    clip: BBox | Any,
) -> tuple[list[tuple[str, Any, dict[str, Any]]], dict[str, int]]:
    """Inside the City of Melbourne, an OSM piece mostly covered by CoM tiers is replaced by
    them; a less covered one keeps the area CoM does not cover (strips narrower than
    2 x SLIVER_OPEN_M dropped). Outside, OSM pieces pass unchanged."""
    shapely.prepare(com_cover)
    out, st = [], Counter()
    for fid, g, props in osm:
        if not inside(g):
            st["outside_com"] += 1
            out.append((fid, g, props))
            continue
        if not com_cover.intersects(g):
            st["inside_com_osm_only"] += 1
            out.append((fid, g, props))
            continue
        covered = com_cover.intersection(g).area / (g.area or 1e-30)
        if covered >= COM_MATCH_SHARE:
            st["inside_com_replaced_by_com"] += 1
            continue
        rest = _polygons_only(_opened(g.difference(com_cover), SLIVER_OPEN_M))
        g2 = clean_geometry(rest, clip) if rest is not None else None
        if g2 is None or _area_m2(g2) < MIN_PIECE_AREA_M2:
            st["inside_com_remnant_dropped"] += 1
            continue
        st["inside_com_osm_only_trimmed"] += 1
        out.append((fid, g2, props))
    return out, dict(st)


ROAD_CLEARANCE_M = 4.5  # assumed clearance over a road through a building (no OSM maxheight)
ROAD_IN_BUILDING_MIN_M = 3.0  # a road centreline at least this long inside a piece passes under
ROAD_AT_GRADE_M = 0.5  # road vertices within this of street level (roads_3d z) are at grade
DRIVABLE = frozenset(
    {
        "motorway",
        "trunk",
        "primary",
        "secondary",
        "tertiary",
        "unclassified",
        "residential",
        "living_street",
        "service",
        "busway",
    }
)


def road_voids(
    roads_fc: dict,
    road_tags: dict[int, dict[str, str]],
    pieces: list[tuple[str, Any, dict[str, Any]]],
    osm: list[Void],
) -> list[Void]:
    """Open space around drivable at-grade roads where they pass through a building piece.

    Roads: `roads_3d.geojson` ways (not tunnels), split into segments whose ends are both within
    ROAD_AT_GRADE_M of street level; a segment run at least ROAD_IN_BUILDING_MIN_M long inside a
    piece passes under it. The void is the road's half-width corridor (deck_pointcloud
    `half_width`: lanes x 3.3 m / 2, class defaults) inside that piece, up to the way's OSM
    `maxheight` (numeric) else ROAD_CLEARANCE_M (an assumption); where the corridor passes under
    an OSM elevated piece (`osm` voids), the whole open space OSM maps under that piece is
    cleared up to the piece's own min_height instead."""
    from shapely.geometry import LineString, MultiLineString

    from pipeline.deck_pointcloud import half_width

    geoms = [g for _, g, _ in pieces]
    tree = STRtree(geoms)
    otree = STRtree([v.geom for v in osm]) if osm else None
    out: list[Void] = []
    for f in roads_fc["features"]:
        p = f["properties"]
        if p.get("tunnel") or p.get("highway") not in DRIVABLE:
            continue
        coords = f["geometry"]["coordinates"]
        segs = [
            (a[:2], b[:2])
            for a, b in zip(coords, coords[1:], strict=False)
            if abs(a[2]) <= ROAD_AT_GRADE_M and abs(b[2]) <= ROAD_AT_GRADE_M
        ]
        if not segs:
            continue
        line = shapely.line_merge(MultiLineString([LineString(s) for s in segs]))
        hits = tree.query(line, predicate="intersects")
        if len(hits) == 0:
            continue
        tags = road_tags.get(int(p["way_id"]), {"highway": p["highway"]})
        default = parse_height(tags.get("maxheight")) or ROAD_CLEARANCE_M
        parts: list[tuple[Any, float]] | None = None
        for j in sorted(int(k) for k in hits):
            if _length_m(line.intersection(geoms[j])) < ROAD_IN_BUILDING_MIN_M:
                continue
            if parts is None:
                corridor = _buffer_m(line, half_width(tags))
                parts = []
                over = (
                    [osm[k] for k in otree.query(corridor, predicate="intersects")] if otree else []
                )
                for v in over:
                    # the road passes under an OSM elevated piece: all the space OSM leaves
                    # open under it is cleared (the Convention Centre Place bridge), not just
                    # the road's width
                    parts.append((v.geom, v.clearance_m))
                rest = (
                    corridor.difference(shapely.union_all([v.geom for v in over]))
                    if over
                    else corridor
                )
                parts.append((rest, default))
            for cg, clearance in parts:
                v = _polygons_only(cg.intersection(geoms[j]))
                if v is None or _area_m2(v) < VOID_MIN_M2 / 2:
                    continue
                out.append(
                    Void(
                        v,
                        round(clearance, HEIGHT_DECIMALS),
                        f"road:{p['way_id']}",
                        pieces[j][0],
                        p["highway"],
                    )
                )
    return out


def _length_m(g: Any) -> float:
    """Approximate length in metres of a small lon/lat line geometry."""
    if g.is_empty:
        return 0.0
    k = math.cos(math.radians(g.centroid.y))
    from shapely import affinity

    return affinity.scale(g, xfact=k, yfact=1.0).length * 111_320.0


def carve(
    items: list[tuple[str, Any, dict[str, Any]]],
    voids: list[Void],
    clip: BBox | Any,
    eligible: Any | None = None,
    applies: Any | None = None,
) -> tuple[list[tuple[str, Any, dict[str, Any]]], dict[str, Any]]:
    """Clear the space under each void's clearance: a piece that reaches into a void below its
    clearance keeps its area outside the void, and over the void starts at the clearance (or
    goes, if it is not taller than that). Fragments over voids are new pieces `<id>-v<n>`; the
    first fragment keeps the id. `eligible(geom)`: only those pieces are carved;
    `applies(void, piece_id)`: only those voids carve that piece."""
    if not voids:
        return items, {"pieces_carved": 0}
    vtree = STRtree([v.geom for v in voids])
    out, st, examples = [], Counter(), {}
    for fid, g, props in items:
        hits = [voids[k] for k in vtree.query(g, predicate="intersects")]
        hits = [
            v
            for v in hits
            if v.clearance_m > props["min_height_m"] + Z_TOL_M
            and (applies is None or applies(v, fid))
        ]
        if not hits or (eligible is not None and not eligible(g)):
            out.append((fid, g, props))
            continue
        frags: list[tuple[Any, float]] = [(g, props["min_height_m"])]
        for c in sorted({v.clearance_m for v in hits}):
            vu = shapely.union_all([v.geom for v in hits if v.clearance_m == c])
            nxt = []
            for fg, base in frags:
                if base >= c - Z_TOL_M or not fg.intersects(vu):
                    nxt.append((fg, base))
                    continue
                inter = fg.intersection(vu)
                if _area_m2(inter) < 1.0:
                    nxt.append((fg, base))
                    continue
                nxt.append((fg.difference(vu), base))
                if props["height_m"] > c + Z_TOL_M:
                    nxt.append((inter, c))
            frags = nxt
        kept = []
        for fg, base in frags:
            g2 = clean_geometry(fg, clip) if not fg.is_empty else None
            if g2 is not None and _area_m2(g2) >= MIN_PIECE_AREA_M2:
                kept.append((g2, base))
        if len(kept) == 1 and kept[0][1] == props["min_height_m"] and kept[0][0].equals(g):
            out.append((fid, g, props))
            continue
        st["pieces_carved"] += 1
        st["by_source"] = st.get("by_source", Counter())  # type: ignore[assignment]
        for v in hits:
            st["by_source"][v.source.split(":")[0]] += 1  # type: ignore[index]
        raised = 0
        for k, (g2, base) in enumerate(kept):
            nid = fid if k == 0 else f"{fid}-v{k}"
            p2 = dict(props, min_height_m=round(base, HEIGHT_DECIMALS))
            raised += base > props["min_height_m"]
            out.append((nid, g2, p2))
        st["fragments_raised"] += raised
        examples[fid] = sorted({v.source for v in hits})
    st["carved"] = examples  # type: ignore[assignment]
    return out, dict(st)


# --------------------------------------------------------------------------------------------
# Validation (also used by tests)


def validate_feature_collection(fc: dict, bbox: BBox, area: Any | None = None) -> list[str]:
    """Return a list of contract violations (empty list = valid). `area` (optional): every
    feature must also lie within this shapely geometry (+ 1e-6 degree for the rounding)."""
    errors: list[str] = []
    if fc.get("type") != "FeatureCollection":
        errors.append("top-level type is not FeatureCollection")
    if "crs" in fc:
        errors.append("RFC 7946 output must not carry a crs member")
    within = None
    if area is not None:
        within = area.buffer(2 * 10**-COORD_DECIMALS)
        shapely.prepare(within)
    ids = set()
    for i, f in enumerate(fc.get("features", [])):
        where = f"feature {i}"
        props = f.get("properties") or {}
        if tuple(sorted(props)) != tuple(sorted(PROPERTY_KEYS)):
            errors.append(f"{where}: properties {sorted(props)} != {sorted(PROPERTY_KEYS)}")
            continue
        if not isinstance(props["id"], str) or not props["id"]:
            errors.append(f"{where}: id must be a non-empty string")
        elif props["id"] in ids:
            errors.append(f"{where}: duplicate id {props['id']}")
        ids.add(props["id"])
        h = props["height_m"]
        if isinstance(h, bool) or not isinstance(h, int | float) or not math.isfinite(h) or h <= 0:
            errors.append(f"{where}: height_m must be a number > 0, got {h!r}")
            continue
        mh = props["min_height_m"]
        if (
            isinstance(mh, bool)
            or not isinstance(mh, int | float)
            or not math.isfinite(mh)
            or mh < 0
            or mh >= h
        ):
            errors.append(f"{where}: min_height_m must be a number in [0, height_m), got {mh!r}")
        lv = props["levels"]
        if lv is not None and (isinstance(lv, bool) or not isinstance(lv, int) or lv <= 0):
            errors.append(f"{where}: levels must be null or an int > 0, got {lv!r}")
        if props["height_source"] not in HEIGHT_SOURCES:
            errors.append(f"{where}: height_source {props['height_source']!r} not allowed")
        geom = f.get("geometry") or {}
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            errors.append(f"{where}: geometry type {geom.get('type')!r}")
            continue
        g = shape(geom)
        if g.is_empty or not g.is_valid or g.area <= 0:
            errors.append(f"{where}: empty or invalid geometry ({shapely.is_valid_reason(g)})")
        polys = list(g.geoms) if isinstance(g, MultiPolygon) else [g]
        if not all(p.exterior.is_ccw and not any(r.is_ccw for r in p.interiors) for p in polys):
            errors.append(f"{where}: ring orientation is not RFC 7946 (exterior CCW, holes CW)")
        xs, ys = shapely.get_coordinates(g).T
        if (
            xs.min() < bbox.west
            or xs.max() > bbox.east
            or ys.min() < bbox.south
            or ys.max() > bbox.north
        ):
            errors.append(f"{where}: coordinates outside bbox")
        elif within is not None and not within.contains(g):
            errors.append(f"{where}: outside the building area")
        for x, y in zip(xs, ys, strict=True):
            if round(x, COORD_DECIMALS) != x or round(y, COORD_DECIMALS) != y:
                errors.append(f"{where}: coordinates not rounded to {COORD_DECIMALS} decimals")
                break
    return errors


# --------------------------------------------------------------------------------------------


def write_geojson(features: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fc = {"type": "FeatureCollection", "features": features}
    path.write_text(json.dumps(fc, separators=(",", ":"), ensure_ascii=False) + "\n")


def summarise(features: list[dict]) -> str:
    n = len(features)
    c = Counter(f["properties"]["height_source"] for f in features)
    lifted = sum(1 for f in features if f["properties"]["min_height_m"] > 0)
    lines = [f"building pieces: {n} ({lifted} with min_height_m > 0)"]
    for s in HEIGHT_SOURCES:
        lines.append(f"  {s:20s} {c[s]:7d}  {100 * c[s] / max(n, 1):5.1f}%")
    top = sorted(features, key=lambda f: -f["properties"]["height_m"])[:10]
    lines.append("tallest:")
    for f in top:
        p = f["properties"]
        lines.append(
            f"  {p['id']:>18s} {p['min_height_m']:6.1f} -> {p['height_m']:6.1f} m"
            f"  {p['height_source']}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# The whole build


REPORT_PATH = REPO_ROOT / "data" / "processed" / "checks" / "buildings_heights.json"
ROADS_3D_PATH = REPO_ROOT / "data" / "processed" / "roads_3d.geojson"
OSM_SIM_PATH = REPO_ROOT / "data" / "processed" / "osm_sim_area.osm.gz"


def load_road_tags(path: Path) -> dict[int, dict[str, str]]:
    """highway / lanes / maxheight of every way in the sim extract (for road voids)."""
    import osmium

    out = {}
    for w in osmium.FileProcessor(str(path), osmium.osm.WAY):
        t = {k: w.tags.get(k) for k in ("highway", "lanes", "maxheight") if k in w.tags}
        if "highway" in t:
            out[w.id] = t
    return out


def cloud_heights(tiers: list[Any], area: Any, log=print) -> tuple[dict, dict]:
    """Point-cloud check of every tier (pipeline/building_pointcloud.py). Returns
    {tier id: (height_m, height_source)} and the report section. Lowers the base of unsupported
    elevated tiers whose ground beside them is as high as their base (sloping ground)."""
    from pipeline import building_pointcloud as bp
    from pipeline.building_tiers import unsupported_elevated

    tiles = bp.tiles_for(area)
    log(f"point cloud: {len(tiles)} tiles, {len(tiers)} tiers")
    regions = bp.roof_regions(tiers)
    roofs = bp.measure(regions, tiles, log=log)
    tops_ahd = [t.ground_ahd + t.top for t in tiers]
    datum = bp.datum_offset(tiers, roofs, tops_ahd)
    off = datum["offset_m"]

    # Elevated tiers with nothing beneath them: sky bridge, or a slice on sloping ground?
    elev = unsupported_elevated(tiers)
    lows = bp.measure(bp.ground_rings(elev), tiles, log=None, stat=bp.ground_low)
    elevated = {"tiers": len(elev), "kept_elevated": [], "to_ground_sloping": 0, "unmeasured": 0}
    for t, low in zip(elev, lows, strict=True):
        if low is None:
            elevated["unmeasured"] += 1
            elevated["kept_elevated"].append({"id": t.id, "base_m": t.base, "ground_beside": None})
            continue
        beside = round(low - off - t.ground_ahd, 2)
        if beside >= t.base - bp.GROUND_TOL_M:
            elevated["to_ground_sloping"] += 1
            t.base = 0.0
        else:
            elevated["kept_elevated"].append(
                {"id": t.id, "base_m": t.base, "top_m": t.top, "ground_beside_m": beside}
            )

    sources: dict[str, tuple[float, str]] = {}
    reasons: Counter = Counter()
    methods: Counter = Counter()
    disagree = []
    diffs = []
    for t, r in zip(tiers, roofs, strict=True):
        cloud_h = None
        if r is None:
            reasons["no exposed roof"] += 1
        elif r.z is None:
            reasons[r.reason] += 1
        else:
            cloud_h = r.z - off - t.ground_ahd
            diffs.append(cloud_h - t.top)
            methods[r.method] += 1
        h, src = bp.apply_rule(t.top, cloud_h, t.top_date)
        sources[t.id] = (h, src)
        if cloud_h is not None and abs(cloud_h - t.top) > bp.AGREE_M:
            c = t.geom.representative_point()
            disagree.append(
                {
                    "id": t.id,
                    "com_top_m": t.top,
                    "cloud_top_m": round(cloud_h, 2),
                    "diff_m": round(cloud_h - t.top, 2),
                    "com_date": t.top_date,
                    "kept": src,
                    "lon": round(c.x, 6),
                    "lat": round(c.y, 6),
                    "roof_points": r.n_region,
                    "inlier_share": r.share,
                    "method": r.method,
                }
            )
    disagree.sort(key=lambda d: -abs(d["diff_m"]))
    import numpy as np

    d = np.asarray(diffs) if diffs else np.zeros(1)
    by_date = Counter(x["com_date"] for x in disagree)
    report = {
        "datum_offset": datum,
        "tiers": len(tiers),
        "measured": len(diffs),
        "measured_by_method": dict(methods),
        "not_measured": dict(reasons),
        "agree_within_m": bp.AGREE_M,
        "confirmed": sum(1 for s in sources.values() if s[1] == "com_pointcloud_2018"),
        "disagree": len(disagree),
        "disagree_by_com_date": dict(by_date),
        "diff_percentiles_m": {
            str(q): round(float(v), 2)
            for q, v in zip((5, 25, 50, 75, 95), np.percentile(d, [5, 25, 50, 75, 95]), strict=True)
        },
        "rule": (
            "|cloud - CoM| <= 3 m: CoM height confirmed (com_pointcloud_2018). Larger: the cloud "
            "replaces CoM only if the CoM capture predates the cloud (May 2018); otherwise CoM "
            "is kept (com_open_data)."
        ),
        "cloud_replaced_com": sum(
            1 for t in tiers if sources[t.id][0] != t.top and sources[t.id][1] != "com_open_data"
        ),
        "elevated_tiers": elevated,
        "disagreements": disagree,
    }
    crowns, crown_stats = bp.crowns(tiers, regions, roofs, off, tiles)
    report["crowns"] = crown_stats | {
        "added": len(crowns),
        "list": [
            {
                "structure": c.structure_id,
                "com_top_m": c.base,
                "crown_top_m": c.top,
                "area_m2": round(c.area_m2),
                "points": c.n,
                "com_date": c.date,
                "lon": round(c.geom.representative_point().x, 6),
                "lat": round(c.geom.representative_point().y, 6),
            }
            for c in crowns
        ],
    }
    report["_roofs"] = {t.id: r for t, r in zip(tiers, roofs, strict=True)}
    report["_crowns"] = crowns
    return sources, report


STALE_OSM_COVER = 0.25  # a CoM tier over a street that OSM covers less than this is out of date
STREETS = DRIVABLE - {"service", "busway"}  # public streets (not alleys, driveways, car parks)


def drop_stale_over_roads(
    com_items: list[tuple[str, Any, dict[str, Any]]],
    rvoids: list[Void],
    osm_pieces: list[tuple[str, Any, dict[str, Any]]],
) -> tuple[list, list[Void], list[dict]]:
    """A CoM structure with a tier that reaches down to a public street passing through it (not
    an alley, driveway or car-park aisle), where the current OSM map has (almost) no building
    (< STALE_OSM_COVER of the structure's footprint), is a structure that is gone: e.g. the
    Metro Tunnel construction sheds over Franklin St and A'Beckett St in the 2018 / 2022
    captures. All its tiers are dropped (listed in the report) instead of being carved."""
    base = {fid: props["min_height_m"] for fid, _, props in com_items}
    by_piece: dict[str, list[Void]] = {}
    for v in rvoids:
        if (
            v.piece_id in base
            and v.highway in STREETS
            and v.clearance_m > base[v.piece_id] + Z_TOL_M
        ):
            by_piece.setdefault(v.piece_id, []).append(v)
    if not by_piece:
        return com_items, rvoids, []

    def sid(fid: str) -> str:
        return fid.split("_")[0]

    structures: dict[str, list[tuple[str, Any, dict[str, Any]]]] = {}
    for it in com_items:
        structures.setdefault(sid(it[0]), []).append(it)
    tree = STRtree([g for _, g, _ in osm_pieces])
    stale: list[dict] = []
    drop: set[str] = set()
    for s in sorted({sid(f) for f in by_piece}):
        g = shapely.union_all([it[1] for it in structures[s]])
        near = [osm_pieces[i][1] for i in tree.query(g, predicate="intersects")]
        cov = shapely.union_all(near).intersection(g).area / g.area if near else 0.0
        if cov >= STALE_OSM_COVER:
            continue
        drop.update(it[0] for it in structures[s])
        c = g.representative_point()
        stale.append(
            {
                "structure": s,
                "tiers": [it[0] for it in structures[s]],
                "height_m": max(it[2]["height_m"] for it in structures[s]),
                "area_m2": round(_area_m2(g)),
                "osm_cover": round(cov, 2),
                "roads": sorted(
                    {v.source for f, vs in by_piece.items() if sid(f) == s for v in vs}
                ),
                "lon": round(c.x, 6),
                "lat": round(c.y, 6),
            }
        )
    kept = [it for it in com_items if it[0] not in drop]
    return kept, [v for v in rvoids if v.piece_id not in drop], stale


def _void_applies(v: Void, piece_id: str) -> bool:
    """City of Melbourne tiers are carved by roads only (an OSM min_height alone does not
    overrule a measured CoM tier); OSM pieces by both."""
    return not piece_id.startswith("c") or v.source.startswith("road:")


def landmark_table(
    features: list[dict], tiers: list[Any], report: dict, osm: list[OsmFeature]
) -> list[dict]:
    """For each config.BUILDING_LANDMARKS point: the City of Melbourne structure there (or, for
    an open-air point such as a stadium's pitch, the tallest within LANDMARK_SEARCH_M), its
    tiers with the point-cloud roof of each, the tallest OSM `height` on it, and the output."""
    from shapely.geometry import Point

    from pipeline.config import BUILDING_LANDMARKS

    roofs = report.get("_roofs", {})
    off = report.get("datum_offset", {}).get("offset_m", 0.0)
    by_struct: dict[str, list[Any]] = {}
    for t in tiers:
        by_struct.setdefault(t.structure_id, []).append(t)
    out_by_struct: dict[str, list[dict]] = {}
    for f in features:
        fid = f["properties"]["id"]
        if fid.startswith("c"):
            out_by_struct.setdefault(fid[1:].split("_")[0], []).append(f["properties"])
    ttree = STRtree([t.geom for t in tiers])
    otree = STRtree([o.geom for o in osm])
    rows = []
    for name, (x, y) in BUILDING_LANDMARKS.items():
        pt = Point(x, y)
        row: dict[str, Any] = {"name": name, "lon": x, "lat": y}
        here = [tiers[i] for i in ttree.query(pt.buffer(0.00015), predicate="intersects")]
        if not here:
            here = [tiers[i] for i in ttree.query(pt.buffer(LANDMARK_SEARCH_DEG))]
            row["structure_found_within_m"] = 150
        if not here:
            rows.append(row)
            continue
        sid = max(here, key=lambda t: t.top).structure_id
        st = sorted(by_struct[sid], key=lambda t: (t.base, t.top))
        foot = shapely.union_all([t.geom for t in st])

        def cloud(t: Any) -> float | None:
            r = roofs.get(t.id)
            return round(r.z - off - t.ground_ahd, 1) if r is not None and r.z is not None else None

        measured = [c for c in (cloud(t) for t in st) if c is not None]
        top = max(st, key=lambda t: t.top)
        row |= {
            "com_structure": sid,
            "com_top_m": top.top,
            "com_top_date": top.top_date,
            "cloud_top_tier_m": cloud(top),
            "cloud_highest_measured_m": max(measured) if measured else None,
            "tiers": [
                {"id": t.id, "base_m": t.base, "top_m": t.top, "cloud_m": cloud(t)} for t in st
            ],
        }
        osm_h = [
            parse_height(osm[i].tags.get("height"))
            for i in otree.query(foot, predicate="intersects")
            if foot.contains(osm[i].geom.representative_point())
        ]
        osm_h = [h for h in osm_h if h is not None]
        row["osm_height_m"] = max(osm_h) if osm_h else None
        outs = out_by_struct.get(sid, [])
        if outs:
            best = max(outs, key=lambda p: p["height_m"])
            row["output_top_m"] = best["height_m"]
            row["output_source"] = best["height_source"]
            row["output_pieces"] = len(outs)
        rows.append(row)
    return rows


LANDMARK_SEARCH_DEG = 150 / 111_320.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--download", action="store_true", help="fetch missing raw files first")
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    ap.add_argument("--report", type=Path, default=REPORT_PATH)
    args = ap.parse_args(argv)

    from pipeline import download
    from pipeline.building_tiers import build_tiers, load_slices
    from pipeline.config import buildings_area, com_boundary

    if args.download:
        download.main([])
    osm_path = download.osm_raw_path()
    if osm_path is None:
        print("No OSM extract in data/raw/. Run with --download first.", file=sys.stderr)
        return 1
    for p in (download.COM_RAW, download.BOUNDARY_RAW):
        if not p.exists():
            print(f"No {p.name} in data/raw/. Run with --download first.", file=sys.stderr)
            return 1

    area = buildings_area()
    boundary = com_boundary()
    shapely.prepare(boundary)

    def inside(g: Any) -> bool:
        return boundary.contains(g.representative_point())

    print(f"reading {osm_path.name} ...")
    underground: list[str] = []
    osm = load_osm(osm_path, BUILDINGS_AREA_BBOX, area, underground)
    print(f"reading {download.COM_RAW.name} ...")
    com = load_com(download.COM_RAW, BUILDINGS_AREA_BBOX)
    items, osm_stats = osm_items(osm, com, area)
    tags_by_id = {o.id: o.tags for o in osm}
    voids = osm_voids(items, tags_by_id, inside=None)  # everywhere (orchestrator 2026-10-27)

    tier_stats: Counter = Counter()
    tiers = build_tiers(load_slices(download.COM_RAW, area), tier_stats)
    sources, cloud = cloud_heights(tiers, area)
    com_items, tier_dropped = tier_items(tiers, sources, area)
    com_items += crown_items(cloud.pop("_crowns"), area)

    cover = shapely.union_all([g for _, g, _ in com_items])
    osm_kept, match_stats = match_osm_to_com(items, cover, inside, area)

    roads_fc = json.loads(ROADS_3D_PATH.read_text())
    road_tags = load_road_tags(OSM_SIM_PATH)
    rvoids = road_voids(roads_fc, road_tags, com_items + osm_kept, voids)
    com_items, rvoids, stale = drop_stale_over_roads(com_items, rvoids, items)
    voids += rvoids
    all_items, carve_stats = carve(com_items + osm_kept, voids, area, applies=_void_applies)

    features = [to_feature(fid, g, props) for fid, g, props in all_items]
    features.sort(key=lambda f: f["properties"]["id"])
    fc = {"type": "FeatureCollection", "features": features}
    errors = validate_feature_collection(fc, BUILDINGS_AREA_BBOX, area)
    if not features:
        errors.append("no buildings found in the area")
    if errors:
        print("\n".join(errors[:20]), file=sys.stderr)
        print(f"{len(errors)} contract violation(s); not writing output", file=sys.stderr)
        return 1
    write_geojson(features, args.out)

    landmarks = landmark_table(features, tiers, cloud, osm)
    cloud.pop("_roofs", None)
    n_struct = len({t.structure_id for t in tiers})
    report = {
        "about": (
            "Verification report for buildings.geojson (not a contract file). Derived from "
            "OpenStreetMap (ODbL 1.0) and City of Melbourne Open Data (2023 Building Footprints, "
            "3D Point Cloud 2018, Municipal boundary; CC BY 4.0)."
        ),
        "inputs_sha256": {
            p.name: _sha256(p)
            for p in (download.COM_RAW, download.BOUNDARY_RAW, ROADS_3D_PATH, OSM_SIM_PATH)
        },
        "counts": {
            "pieces": len(features),
            "com_structures": n_struct,
            "com_tiers": len(com_items),
            "pieces_min_height_gt_0": sum(
                1 for f in features if f["properties"]["min_height_m"] > 0
            ),
            "height_source": dict(Counter(f["properties"]["height_source"] for f in features)),
        },
        "osm": osm_stats,
        "osm_underground_only_skipped": sorted(underground),
        "com_tiers": dict(tier_stats) | {"dropped_after_clip": tier_dropped},
        "osm_vs_com": match_stats,
        "voids": {
            "osm_elevated": sum(1 for v in voids if v.source.startswith("osm:")),
            "road_passages": sum(1 for v in voids if v.source.startswith("road:")),
            "osm_sources": sorted({v.source for v in voids if v.source.startswith("osm:")}),
            "road_sources": sorted({v.source for v in voids if v.source.startswith("road:")}),
        },
        "carve": carve_stats,
        "stale_com_tiers_over_roads_dropped": stale,
        "pointcloud": cloud,
        "landmarks": landmarks,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=1, default=_json_default) + "\n")
    print(json.dumps({k: report[k] for k in ("counts", "com_tiers", "osm_vs_com")}, default=str))
    print(summarise(features))
    print(f"wrote {args.out} ({args.out.stat().st_size / 1e6:.1f} MB), report {args.report}")
    return 0


def _sha256(p: Path) -> str:
    import hashlib

    return hashlib.sha256(p.read_bytes()).hexdigest()


def _json_default(o: Any) -> Any:
    if isinstance(o, Counter):
        return dict(o)
    return str(o)


if __name__ == "__main__":
    sys.exit(main())
