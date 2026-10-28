"""Build data/processed/roads_3d.geojson from osm_sim_area.osm.gz, the raw OSM PBF (clearance tags,
railways) and the City of Melbourne 3D Point Cloud 2018 tiles (measured deck heights).

Usage:
    uv run python -m pipeline.roads       # extract + this file (the one command, from data/raw/)
    uv run python -m pipeline.roads_3d    # this file only, from the existing extract

Contract (CLAUDE.md): RFC 7946 FeatureCollection; one LineString per way in the extract, coordinates
`[lon, lat, z_m]` (lon/lat to 6 decimals, z to 0.01 m, every line densified to <= 10 m spacing);
properties exactly `way_id` (int), `highway` (str), `layer` (int, default 0), `bridge` (bool),
`tunnel` (bool), `name` (str or null), `z_source` (str). This file is the single source of road
(and car) heights. Heights are visual only: SUMO is 2D and never sees them.

**Clearance-based heights (PLAN.md Decisions log, 2026-10-26).** OSM `layer` (with `bridge` /
`tunnel`) gives only the ORDER at a crossing; how high a deck sits comes from what is beneath it:

    1. Crossings. Wherever two ways (roads, or a road and a railway) cross in 2D without a shared
       OSM node and on different effective layers (a bridge counts as layer >= 1, the same rule as
       pipeline.checks.grade_separation), the upper way at the crossing is at least
           z(lower way at the crossing) + separation
       where separation, in order of preference (`Crossing.source`):
         pointcloud  the deck-to-road separation measured in the City of Melbourne 3D Point
                     Cloud 2018 at this crossing, where the sample is good
                     (pipeline/deck_pointcloud.py; road-over-road only). This is an ANCHOR,
                     not a lower bound (rule 8): the deck sits exactly that far above the road.
                     Over a freeway in a cutting it is measured against street level (the
                     cutting is still at z = 0 in Stage A), so those crossings are < 4 m apart
                     and listed as `pending_trench_depth` with the cutting's depth for Stage B;
         maxheight   the lower way's numeric OSM `maxheight:physical` / `maxheight` /
                     `maxheight:signed` clearance (in that order) + the CALIBRATED deck
                     thickness: a LOWER BOUND only (a signed limit), never an anchor, and never
                     used on a crossing the point cloud measured;
         default     a CALIBRATED separation (`Crossing.basis`): the median of the upper
                     deck's OWN point-cloud separations if it has any ("deck"), else the
                     median for its road class ("class": freeway = motorway, trunk and their
                     links; other), else the global median ("global"), else the constant
                     DEFAULT_SEPARATION_M ("constant").
       Calibration (`calibrate`, orchestrator decisions 2026-10-26) uses the point-cloud
       measurements of this build; a class or global median needs n >= CALIBRATION_MIN_N (5).
       The deck thickness added to `maxheight` is the median (measured separation - maxheight)
       per class if n >= 5, else global if n >= 5, else DECK_THICKNESS_M. Values and n are in the
       build summary and in data/processed/checks/deck_pointcloud.json ("calibration"); the
       current values are printed by every build.
       Stacks build up: a deck over a deck over a road is 2 x separation above the road.
       Railways (railway = rail, light_rail, tram, subway, ... from data/raw/, RAIL_TYPES) count as
       "beneath"; they get heights internally by the same rules but are not written out.
       Not applied (Stage B lowers instead of raising, rule 9): crossings where either way is a
       real tunnel (any `tunnel` except `building_passage`), or where the upper way is not above
       ground (effective layer <= 0, e.g. a `layer=-1` cutting under a ground road).
    2. Deck profiles. Every road bridge (not tunnels) gets a point-cloud profile, deck surface
       minus street level: the at-grade approach roads 50-90 m out (past the embankment; not
       *_link ramps), else the median of at-grade streets nearby, widening to 1 km over water
       (the Bolte Bridge), never a cutting floor or water; the deck's top surface where water or
       ground shows past its edge. Profiles are interpolated between clean vertices, not beyond.
       A deck with crossing pins is profiled only away from them, and only over water. Profile
       vertices are anchors; a vertex another measurement makes impossible is unpinned. A
       bridge without a profile is at least OPEN_DECK_M (1.5 m), or, over water, the median of
       the measured street river bridges (`layer_floors.river_bridges`). Measured service /
       car-park ramps may be as steep as SERVICE_RAMP_GRADE (12 %, George 2026-10-26; service
       bridges are densified to SERVICE_RAMP_SPACING_M so the 1 m step rule still holds).
       A profile is dropped if another measurement makes it impossible within the grade limits,
       or if it holds the deck below today's signed clearance of a road beneath (a deck rebuilt
       after the 2018 capture, e.g. Wurundjeri Way over Dudley St).
    3. No sag. Along a continuous elevated deck (a chain of ways on effective layer >= 1 joined
       end to end) the deck does not dip between two higher points: each vertex is at least
       min(highest point before it, highest point after it) on the chain.
    4. Ramps. Every vertex is at least (any neighbour's z - grade x distance): ramps fall at
       MAX_GRADE (6 %) per metre. A short *_link way (<= SHORT_LINK_MAX_M) gets SHORT_LINK_GRADE
       (8 %) ONLY where 6 % cannot satisfy a crossing (the ramp from a deck down to the road it
       crosses is too short for 6 %); those ways are listed in the build summary.
    5. Everything else is 0. Heights are the LEAST solution of 1-4 (a fixed point: z only rises
       until every rule holds), so decks are no higher than their constraints need and ramps come
       down as soon as the grade allows.
    6. A crossing whose separation cannot be met within the grade limits (its lower road joins
       the upper deck too close by, even at 8 %) is dropped from rule 1 and listed in the build
       summary (and shows up as order_mismatch / separated_low in grade_separation.json).
    7. Layer floor (orchestrator decisions 2026-10-26, for viaducts built after the 2018 capture,
       e.g. the West Gate Tunnel decks along Footscray Rd). A FREEWAY-class road bridge way
       (motorway, trunk and their links) with nothing beneath (on top at no rule-1 crossing), no
       measured height, NOT over water (river bridges keep rule 2; waterway lines and water areas
       from the raw PBF) and left by the other rules at the bare open-deck height gets a floor
       of effective layer x the freeway-class separation. Each floor is tried in way-id order and
       kept only if heights converge, no NEW rule-1 crossing falls below CHECK_CLEARANCE_M and no
       measured vertex is lifted; otherwise the highest floor that passes is used (bisection),
       or none. Floors are estimates, so rule 8 caps them too (`effective_max_m` in the report).
       `layer_floors` in deck_pointcloud.json lists full, reduced and dropped floors and the
       bridges not floored (not freeway / over water / held up by the rules). Floored ways are
       `estimated`.
    8. Measured precedence (orchestrator decisions 2026-10-26: measured always wins; estimates
       bend around measurements, never the reverse; `measured_caps`). Pins: each point-cloud
       separation over a grounded lower road (an at-grade road, or a deck held by its own
       profile) pins both ends of its crossing segment at exactly lower + separation, and each
       profile vertex is pinned at its height. Phase A (measured requirements, grades, the bare
       open-deck height, no-sag capped by the pins) adds the binding `maxheight` anchors. Their
       `upper_bounds` cap EVERY requirement (through grades; under a capped deck the lower way <=
       deck - measured separation, or deck - CHECK_CLEARANCE_M - BEND_MARGIN_M for an estimated
       crossing, or just <= the deck over a cutting). So estimates bend (an estimated deck under
       a measured one comes down, to no less than 4.03 m over its own road), no-sag cannot lift
       a measured deck, and a deck may sag where the cloud shows a sag. A point-cloud separation
       over an ESTIMATED deck is relative: where the measured deck above is anchored, the deck
       beneath follows it (lower >= upper - separation).
       Conflicts between measurements: each separation is relative to its own local road, the
       terrain slopes and the model's ground is flat, so neighbouring measurements can be
       impossible to hold together within the grade limits. After solving, a point-cloud pin
       that ends up more than PIN_TOL_M above or PIN_UNDER_TOL_M below its measurement is
       DEMOTED, not discarded: it stays as a lower bound at its measured value
       (`pointcloud_soft`, record `used: false`, reason "contradicts neighbouring measurements;
       kept as a lower bound", listed in `precedence.pins_dropped`), so it is drawn as close to
       the measurement as its neighbours allow and `maxheight` never replaces it; the solve
       repeats. Spots where two kept measurements still
       leave less than 4 m are listed in `precedence.conflicts_below_ground` (road vertices;
       internal railways apart) and show as separated_low in grade_separation.json.

    9. Below street level (Stage B, `stage_b` / `lower_below_ground`, after rules 1-8; decks
       and their anchors are not moved):
       - tunnels (`tunnel_target`: any tunnel but `building_passage` / `covered`, layer <= 0 or
         untagged) get -TUNNEL_DEPTH_PER_LEVEL_M x max(-layer, 1), ramped down from the portals
         at the grade limits; building passages and positive-layer tunnels (Kings Way 12278361,
         23212187) keep their heights;
       - a road or railway over a tunnel stays `tunnel_separation` above it; where a portal is
         too close, the approach road outside the portal ramps down only as far as needed;
       - under river-type water (`river_bed`) a tunnel stays at or below UNDER_WATER_MAX_Z_M
         (water surface - assumed river depth - cover - roof; assumptions, see the constants);
       - the CityLink cutting under Power St takes its depth MEASURED in the point cloud
         (`deck_pointcloud.measure_cutting`, and the depth measured at its crossings where the
         floor is hidden), ramped to 0 at its ends; measurements the grade limits cannot hold
         are drawn as close as they allow and listed. A deck separation measured to the cutting
         floor is held as (separation - cutting depth) over the flat Stage A ground
         (`Crossing.lower_depth`), so it is drawn as measured once the cutting is lowered.
       Portals whose outside road is not in the extract (the Domain tunnel's east end) stay at
       street level (`read_tunnel_context`).

`z_source` (per feature; orchestrator decision 2026-10-26):
    measured   at least HALF of the way's raised vertices (z > 0) are held by measurements: a
               point-cloud pin or profile vertex at its measured height, a binding `maxheight`
               anchor, a deck vertex at the cap the measurements put on it, or a deck vertex
               between two such vertices on the same deck chain. Which measurement is in
               data/processed/checks/deck_pointcloud.json (`anchors`, records) and the OSM tags
               (maxheight; a maxheight anchor adds the calibrated deck thickness).
    estimated  some vertex is above (or, Stage B, below) 0 and fewer than half are held by
               measurements (the rules with a calibrated separation, the open-deck height, sag
               fill, a layer floor or a ramp, possibly bent under a measured deck by rule 8;
               tunnel depths and lowered approach roads).
    ground     every vertex is at z = 0.
    (Stage B: "raised" in `measured` means z != 0; measured cutting vertices count as held.)

Constants (used only when there are too few point-cloud measurements to calibrate, e.g. in the
synthetic tests): DECK_THICKNESS_M = 1.2 m (a typical concrete overpass), DEFAULT_CLEARANCE_M =
5.3 m (Australian main-road overpasses are built to ~5.4 m; the 17 numeric `maxheight` values on
roads beneath drivable decks here are 4.3-5.5 m, median 5.0 m, signed limits below the physical
clearance), so DEFAULT_SEPARATION_M = 6.5 m. OPEN_DECK_M = 1.5 m is always a rule estimate.
On the real build the calibrated values replace the first two: the point cloud showed them to be
low (separations exceed the same crossing's `maxheight` by a median of 3.5 m, not 1.2 m).

Guarantees (checked by `smoothness` and the tests): no step > MAX_STEP_M between consecutive
vertices, one z per shared OSM node, ground roads away from structures exactly 0. CityLink motorway
tunnels may reach TUNNEL_EXTRACT_BBOX (pipeline/config.py); everything else SIM_EXTRACT_BBOX.
"""

from __future__ import annotations

import heapq
import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipeline.config import SIM_EXTRACT_BBOX, TUNNEL_EXTRACT_BBOX, BBox

REPO_ROOT = Path(__file__).resolve().parent.parent
IN_PATH = REPO_ROOT / "data" / "processed" / "osm_sim_area.osm.gz"
RAW_PBF = REPO_ROOT / "data" / "raw" / "bbbike_melbourne.osm.pbf"
OUT_PATH = REPO_ROOT / "data" / "processed" / "roads_3d.geojson"

DECK_THICKNESS_M = 1.2
DEFAULT_CLEARANCE_M = 5.3
DEFAULT_SEPARATION_M = DEFAULT_CLEARANCE_M + DECK_THICKNESS_M  # 6.5 m surface to surface
OPEN_DECK_M = 1.5
CALIBRATION_MIN_N = 5  # below this many measurements the constants above are kept
CHECK_CLEARANCE_M = 4.0  # pipeline.checks.grade_separation CLEARANCE_M: below it is separated_low
BEND_MARGIN_M = 0.03  # a bent estimate keeps CHECK_CLEARANCE_M + this (z rounding)
PIN_TOL_M = 0.3  # a point-cloud separation may end up at most this much above its value ...
PIN_UNDER_TOL_M = 0.015  # ... and at most this much below it (z storage rounding). A pin that
# cannot hold to this is not discarded: a crossing is demoted to a lower bound at its measured
# value (`pointcloud_soft`), a profile vertex is unpinned; either is still drawn as close to its
# measurement as the measurements around it allow (centimetres in this extract), and `maxheight`
# never takes over. qa requires every listed anchor to be within 0.015 m of its measurement.
# Measured service / car-park ramps may exceed MAX_GRADE, up to ~12 % (George 2026-10-26; the
# Crown ramp 1132332719 measures 11-13 %). Service bridges are densified to
# SERVICE_RAMP_SPACING_M so a 12 % ramp still keeps the hard 1 m step rule (MAX_STEP_M):
# 0.12 x 8 m = 0.96 m.
SERVICE_RAMP_GRADE = 0.12
SERVICE_RAMP_SPACING_M = 8.0
RIVER_DECK_MIN_N = 3  # river-bridge open-deck height = median of this many measured, else rule
PIN_ROUNDS = 10
TRENCH_NEAR_M = 150.0  # an unmeasured crossing this close to a measured cutting on the same road
FREEWAY_HIGHWAYS = frozenset({"motorway", "trunk", "motorway_link", "trunk_link"})
WATER_TAGS = frozenset({"river", "canal", "stream", "drain", "ditch", "riverbank", "dock"})
TUNNEL_DEPTH_PER_LEVEL_M = 10.0  # Stage B (PLAN.md Decisions log): -10 m x level
TUNNELS_ENABLED = True  # Stage B: tunnels are lowered (`tunnel_target`, `lower_below_ground`)
# Stage B, tunnels under water (George 2026-10-26: below the river bed, estimated and labelled).
# No open river-bed survey or tunnel long-section was found, so the bed is an ASSUMPTION:
WATER_SURFACE_Z_M = 0.0  # the water surface, taken at the model's flat street level (the real
# Yarra surface is lower than its banks; not measured here, so the bed may be deeper than this)
RIVER_DEPTH_M = 6.0  # assumed river depth (George: ~5-6 m for the Yarra; no bathymetry used)
RIVER_COVER_M = 5.0  # assumed ground between the river bed and the tunnel roof
TUNNEL_ROOF_M = 6.0  # assumed road surface to the top of the tunnel structure
UNDER_WATER_MAX_Z_M = WATER_SURFACE_Z_M - RIVER_DEPTH_M - RIVER_COVER_M - TUNNEL_ROOF_M  # -17 m
# A road over a lowered tunnel needs the tunnel's numeric `maxheight` + DECK_THICKNESS_M (the
# roof), else DEFAULT_SEPARATION_M, road surface to road surface; approach roads are lowered
# only as far as that needs (`lower_below_ground` step 1).
CUT_PIN_MIN_M = 0.5  # a measured cutting vertex at least this far below street level is pinned
MAX_GRADE = 0.06
SHORT_LINK_GRADE = 0.08
SHORT_LINK_MAX_M = 150.0
MAX_SPACING_M = 10.0
MAX_STEP_M = 1.0
COORD_DECIMALS = 6
Z_DECIMALS = 2
MAX_ROUNDS = 200
Z_LIMIT_M = 120.0  # above this the fixed point is diverging (a cycle the checks missed)
POINT_TOL_M = 0.05  # an intersection point this close to a shared node is that node
MERGE_TOL_M = 1.0  # intersection points of one pair closer than this are one crossing
OVERLAP_STEP_M = 5.0

Z_SOURCES = ("measured", "estimated", "ground")
PROPERTY_KEYS = ("way_id", "highway", "layer", "bridge", "tunnel", "name", "z_source")
NOT_A_STRUCTURE = frozenset({"no", "false", "0"})
MAXHEIGHT_KEYS = ("maxheight:physical", "maxheight", "maxheight:signed")
# Physical track that is there today (abandoned / razed / proposed / construction are not).
RAIL_TYPES = frozenset(
    {
        "rail",
        "light_rail",
        "tram",
        "subway",
        "narrow_gauge",
        "monorail",
        "funicular",
        "preserved",
        "disused",
    }
)
# Coordinates may leave the extract bbox a little where a clipped way loops out and back in.
BBOX_TOLERANCE_DEG = 0.01

_M_PER_DEG_LAT = 111_320.0


# --- tags -------------------------------------------------------------------------------------


def parse_layer(value: str | None) -> int:
    """OSM `layer` as an int; 0 when missing or unparseable ("1;2" -> 1)."""
    if value is None:
        return 0
    head = value.split(";")[0].strip()
    try:
        return int(round(float(head)))
    except ValueError:
        return 0


def _structure(tags: dict[str, str], key: str) -> str | None:
    v = tags.get(key)
    if v is None or v.strip().lower() in NOT_A_STRUCTURE:
        return None
    return v.strip().lower()


def is_bridge(tags: dict[str, str]) -> bool:
    return _structure(tags, "bridge") is not None


def is_tunnel(tags: dict[str, str]) -> bool:
    """The contract's `tunnel` property: any tunnel value, building_passage included."""
    return _structure(tags, "tunnel") is not None


def is_real_tunnel(tags: dict[str, str]) -> bool:
    """A tunnel Stage B will lower (any tunnel except `building_passage`)."""
    t = _structure(tags, "tunnel")
    return t is not None and t != "building_passage"


def effective_layer(tags: dict[str, str]) -> int:
    """Layer used for the over/under order (same rule as pipeline.checks.grade_separation)."""
    layer = parse_layer(tags.get("layer"))
    if is_bridge(tags):
        return max(layer, 1)
    if is_real_tunnel(tags):
        return min(layer, -1)
    return layer


_FEET_INCHES = re.compile(r"^(\d+(?:\.\d+)?)\s*'\s*(?:(\d+(?:\.\d+)?)\s*\")?$")
_NUMBER_UNIT = re.compile(r"^(\d+(?:[.,]\d+)?)\s*(m|metres|meters|ft|feet)?$")


def parse_maxheight(value: str | None) -> float | None:
    """A clearance in metres from an OSM maxheight value, or None if it is not a number.

    "4.3", "4.3 m", "4,3", "14'6\"", "14 ft" parse; "default", "below_default", "none", "no",
    "unsigned", "4.3;4.5" (ambiguous) and non-positive values do not.
    """
    if value is None:
        return None
    v = value.strip().lower()
    m = _FEET_INCHES.match(v)
    if m:
        out = float(m.group(1)) * 0.3048 + float(m.group(2) or 0) * 0.0254
    else:
        m = _NUMBER_UNIT.match(v)
        if not m:
            return None
        out = float(m.group(1).replace(",", "."))
        if m.group(2) in ("ft", "feet"):
            out *= 0.3048
    return out if 0 < out < 20 else None


def measured_clearance(tags: dict[str, str]) -> tuple[float, str] | None:
    """(clearance m, tag key) from the first numeric MAXHEIGHT_KEYS tag, else None."""
    for k in MAXHEIGHT_KEYS:
        c = parse_maxheight(tags.get(k))
        if c is not None:
            return c, k
    return None


def tunnel_target(tags: dict[str, str]) -> float | None:
    """Stage B: the target depth of a tunnel way (negative z), or None if it is not lowered.

    -TUNNEL_DEPTH_PER_LEVEL_M x max(-layer, 1) for a real tunnel (any `tunnel` value except
    `building_passage` and `covered`, a roofed road at street level) with layer <= 0 or no
    layer: -10 / -20 / -30 m for layers -1 / -2 / -3. None for `tunnel=building_passage` and
    for tunnels with a positive layer (e.g. the elevated Kings Way ways 12278361 and 23212187,
    building_passage at layer 2), which keep their deck / ramp height.
    """
    t = _structure(tags, "tunnel")
    if not TUNNELS_ENABLED or t is None or t in ("building_passage", "covered"):
        return None
    layer = parse_layer(tags.get("layer"))
    if layer > 0:
        return None
    return -TUNNEL_DEPTH_PER_LEVEL_M * max(-layer, 1)


# --- graph ------------------------------------------------------------------------------------


@dataclass
class Way:
    id: int
    nodes: list[int]
    tags: dict[str, str]
    rail: bool = False  # internal only: railways are never written out


@dataclass
class Graph:
    lon: list[float] = field(default_factory=list)
    lat: list[float] = field(default_factory=list)
    adj: list[list[tuple[int, float, int]]] = field(default_factory=list)  # (nbr, metres, way)
    lines: dict[int, list[int]] = field(default_factory=dict)  # way id -> vertex indices
    node_vertex: dict[tuple[bool, int], int] = field(default_factory=dict)  # (rail, node) -> v
    grade: dict[int, float] = field(default_factory=dict)  # way id -> max grade
    ways: dict[int, Way] = field(default_factory=dict)

    def add_vertex(self, lon: float, lat: float) -> int:
        self.lon.append(lon)
        self.lat.append(lat)
        self.adj.append([])
        return len(self.lon) - 1


def metres(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    k = math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lon2 - lon1) * k, lat2 - lat1) * _M_PER_DEG_LAT


def way_length(way: Way, coords: dict[int, tuple[float, float]]) -> float:
    pts = [coords[n] for n in way.nodes]
    return sum(metres(*a, *b) for a, b in zip(pts, pts[1:], strict=False))


def is_short_link(way: Way, coords: dict[int, tuple[float, float]]) -> bool:
    hw = way.tags.get("highway", "")
    return hw.endswith("_link") and way_length(way, coords) <= SHORT_LINK_MAX_M


def build_graph(
    ways: list[Way], coords: dict[int, tuple[float, float]], spacing: float = MAX_SPACING_M
) -> Graph:
    """Densify every way to <= `spacing` m; OSM nodes become shared vertices.

    Railway nodes are kept apart from road nodes (a level crossing or a tram track sharing a
    street node does not join the two networks), so railways never lift or lower a road.
    """
    g = Graph()

    def node_v(rail: bool, nid: int) -> int:
        key = (rail, nid)
        if key not in g.node_vertex:
            g.node_vertex[key] = g.add_vertex(*coords[nid])
        return g.node_vertex[key]

    for way in ways:
        g.ways[way.id] = way
        g.grade[way.id] = MAX_GRADE
        step = spacing
        if way.tags.get("highway") == "service" and is_bridge(way.tags):
            step = min(spacing, SERVICE_RAMP_SPACING_M)  # room for a SERVICE_RAMP_GRADE ramp
        line = [node_v(way.rail, way.nodes[0])]
        for a, b in zip(way.nodes, way.nodes[1:], strict=False):
            (lon1, lat1), (lon2, lat2) = coords[a], coords[b]
            d = metres(lon1, lat1, lon2, lat2)
            n = max(1, math.ceil(d / step - 1e-9))
            for k in range(1, n):
                f = k / n
                line.append(g.add_vertex(lon1 + f * (lon2 - lon1), lat1 + f * (lat2 - lat1)))
            line.append(node_v(way.rail, b))
            seg = d / n
            for u, v in zip(line[-n - 1 :], line[-n:], strict=False):
                g.adj[u].append((v, seg, way.id))
                g.adj[v].append((u, seg, way.id))
        g.lines[way.id] = line
    return g


# --- crossings --------------------------------------------------------------------------------


@dataclass
class Crossing:
    """Rule 1 at one crossing point: z(up_a), z(up_b) >= z_lower(point) + separation."""

    upper: int  # way ids
    lower: int
    up_a: int  # the upper way's segment containing the point (vertex indices)
    up_b: int
    low_a: int  # the lower way's segment, and the point's fraction along it
    low_b: int
    low_f: float
    separation: float
    clearance_m: float | None  # measured clearance (None = default)
    clearance_key: str | None
    lon: float
    lat: float
    kind: str
    dropped: str | None = None  # why rule 1 is not applied here, if it is not
    source: str = "default"  # where `separation` comes from: pointcloud / maxheight / default
    basis: str = "constant"  # for maxheight/default: which calibration (deck / class / global)
    pointcloud: dict[str, Any] | None = None  # the point-cloud sample, used or not
    up_f: float = 0.5  # the point's fraction along the upper segment (up_a -> up_b)
    trench: dict[str, Any] | None = None  # lower road in a cutting: Stage B depth (point cloud)
    # Stage B: a point-cloud separation measured to the floor of a cutting (not to street level)
    # is held in the (flat-ground) Stage A solve as `separation` = measured - lower_depth, the
    # cutting's measured depth below street level here; Stage B lowers the cutting by that much,
    # so the drawn separation is the measured one (`offset_cutting_crossings`)
    lower_depth: float = 0.0

    @property
    def measured(self) -> bool:
        return self.source != "default"

    @property
    def soft(self) -> bool:
        """A point-cloud separation that contradicts its neighbours within the grade limits:
        kept as a (capped) lower bound at its measured value, never replaced by `maxheight`."""
        return self.source == "pointcloud_soft"

    @property
    def pinned(self) -> bool:
        """A point-cloud separation is an ANCHOR, not a lower bound (qa review, 2026-10-26):
        the upper deck sits exactly that far above the lower road at the crossing."""
        return self.source == "pointcloud"


class _Proj:
    def __init__(self, lon0: float, lat0: float):
        self.lon0, self.lat0 = lon0, lat0
        self.kx = _M_PER_DEG_LAT * math.cos(math.radians(lat0))

    def xy(self, lon: float, lat: float) -> tuple[float, float]:
        return ((lon - self.lon0) * self.kx, (lat - self.lat0) * _M_PER_DEG_LAT)

    def lonlat(self, x: float, y: float) -> tuple[float, float]:
        return (self.lon0 + x / self.kx, self.lat0 + y / _M_PER_DEG_LAT)


def _near(p: tuple[float, float], qs: list[tuple[float, float]], tol: float) -> bool:
    return any(math.hypot(p[0] - q[0], p[1] - q[1]) <= tol for q in qs)


def order(wa: Way, wb: Way) -> tuple[Way, Way] | None:
    """(upper, lower) if rule 1 applies to a crossing of these two ways, else None."""
    if is_real_tunnel(wa.tags) or is_real_tunnel(wb.tags):
        return None  # Stage B: separated by lowering the tunnel, not by raising the road above
    la, lb = effective_layer(wa.tags), effective_layer(wb.tags)
    if la == lb:
        return None
    upper, lower = (wa, wb) if la > lb else (wb, wa)
    if effective_layer(upper.tags) <= 0:
        return None  # the upper way is not above ground: Stage B lowers the other one
    return upper, lower


def crossing_points(
    ways: list[Way], coords: dict[int, tuple[float, float]], proj: _Proj, order_fn: Any = None
) -> list[tuple[Way, Way, float, float, str]]:
    """(upper, lower, x, y, kind) for every 2D crossing of two ways without a shared OSM node
    where rule 1 applies (`order`, or `order_fn`). Same geometry rules as
    grade_separation.find_crossings; an overlap (collinear without shared nodes) gives a point
    every OVERLAP_STEP_M."""
    order_fn = order_fn or order
    import shapely
    from shapely.geometry import LineString
    from shapely.strtree import STRtree

    xy_of = {n: proj.xy(*coords[n]) for w in ways for n in w.nodes}
    geoms = [LineString([xy_of[n] for n in w.nodes]) for w in ways]
    tree = STRtree(geoms)
    left, right = tree.query(geoms, predicate="intersects")
    out = []
    for i, j in zip(left.tolist(), right.tolist(), strict=True):
        if i >= j:
            continue
        wa, wb = ways[i], ways[j]
        ud = order_fn(wa, wb)
        if ud is None:
            continue
        shared = set(wa.nodes) & set(wb.nodes) if wa.rail == wb.rail else set()
        shared_xy = [xy_of[n] for n in shared]
        inter = geoms[i].intersection(geoms[j])
        found: list[tuple[tuple[float, float], str]] = []
        for part in shapely.get_parts(inter):
            if part.is_empty:
                continue
            if part.geom_type == "Point":
                p = (part.x, part.y)
                if not _near(p, shared_xy, POINT_TOL_M):
                    found.append((p, "cross"))
            elif part.geom_type == "LineString":
                ends = [part.coords[0], part.coords[-1]]
                if all(_near(e, shared_xy, POINT_TOL_M) for e in ends):
                    continue  # a shared stretch of road, not a crossing
                n = max(1, math.ceil(part.length / OVERLAP_STEP_M))
                for k in range(n + 1):
                    q = part.interpolate(k / n, normalized=True)
                    if not _near((q.x, q.y), shared_xy, POINT_TOL_M):
                        found.append(((q.x, q.y), "overlap"))
        kept: list[tuple[tuple[float, float], str]] = []
        for p, kind in found:
            if not _near(p, [q for q, _ in kept], MERGE_TOL_M):
                kept.append((p, kind))
        for (x, y), kind in kept:
            out.append((ud[0], ud[1], x, y, kind))
    return out


def _locate(xy: list[tuple[float, float]], x: float, y: float) -> tuple[int, float]:
    """(segment index, fraction along it) of the point of polyline `xy` nearest (x, y)."""
    best = (math.inf, 0, 0.0)
    for i, ((x1, y1), (x2, y2)) in enumerate(zip(xy, xy[1:], strict=False)):
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        f = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / L2))
        d = math.hypot(x1 + f * dx - x, y1 + f * dy - y)
        if d < best[0]:
            best = (d, i, f)
    return best[1], best[2]


def build_crossings(
    g: Graph, coords: dict[int, tuple[float, float]], proj: _Proj
) -> list[Crossing]:
    ways = list(g.ways.values())
    vxy = [proj.xy(lo, la) for lo, la in zip(g.lon, g.lat, strict=True)]
    out = []
    for up, low, x, y, kind in crossing_points(ways, coords, proj):
        lu, ll = g.lines[up.id], g.lines[low.id]
        iu, fu = _locate([vxy[v] for v in lu], x, y)
        il, fl = _locate([vxy[v] for v in ll], x, y)
        mc = None if low.rail else measured_clearance(low.tags)
        sep = (mc[0] + DECK_THICKNESS_M) if mc else DEFAULT_SEPARATION_M
        lon, lat = proj.lonlat(x, y)
        out.append(
            Crossing(
                upper=up.id,
                lower=low.id,
                up_a=lu[iu],
                up_b=lu[iu + 1],
                low_a=ll[il],
                low_b=ll[il + 1],
                low_f=fl,
                separation=sep,
                clearance_m=mc[0] if mc else None,
                clearance_key=mc[1] if mc else None,
                lon=lon,
                lat=lat,
                kind=kind,
                source="maxheight" if mc else "default",
                up_f=fu,
            )
        )
    return out


# --- heights ----------------------------------------------------------------------------------


def envelope(g: Graph, sources: dict[int, float]) -> list[float]:
    """Least z >= sources (and >= 0) with z[v] >= z[u] - grade x distance along every edge.

    That is max over sources of (value - grade-weighted network distance), a max of cones, so
    |dz| <= grade x length on every edge.
    """
    z = [0.0] * len(g.lon)
    heap = []
    for v, h in sources.items():
        if h > z[v]:
            z[v] = h
            heap.append((-h, v))
    heapq.heapify(heap)
    grade = g.grade
    while heap:
        neg, u = heapq.heappop(heap)
        zu = -neg
        if zu < z[u]:
            continue
        for v, d, w in g.adj[u]:
            cand = zu - grade[w] * d
            if cand > z[v]:
                z[v] = cand
                heapq.heappush(heap, (-cand, v))
    return z


def drop_costs(g: Graph, start: int, limit: float) -> dict[int, tuple[float, int | None]]:
    """Dijkstra in grade x metres from `start`, up to `limit`: vertex -> (cost, parent)."""
    best: dict[int, tuple[float, int | None]] = {start: (0.0, None)}
    heap = [(0.0, start)]
    while heap:
        c, u = heapq.heappop(heap)
        if c > best[u][0] or c > limit:
            continue
        for v, d, w in g.adj[u]:
            nc = c + g.grade[w] * d
            if nc <= limit and nc < best.get(v, (math.inf, None))[0]:
                best[v] = (nc, u)
                heapq.heappush(heap, (nc, v))
    return best


def _infeasible_path(g: Graph, c: Crossing) -> list[int] | None:
    """If the lower road is joined to the upper deck so closely that a ramp at the current
    grades cannot drop `separation` between them, the vertices of that shortest path."""
    worst: tuple[float, list[int]] | None = None
    for u in (c.up_a, c.up_b):
        reach = drop_costs(g, u, c.separation)
        ca = reach.get(c.low_a, (math.inf, None))[0]
        cb = reach.get(c.low_b, (math.inf, None))[0]
        drop = (1 - c.low_f) * ca + c.low_f * cb  # the most the lower point can sit below u
        if drop < c.separation - 1e-9 and (worst is None or drop < worst[0]):
            path = []
            for end in (c.low_a, c.low_b):
                v: int | None = end if end in reach else None
                while v is not None:
                    path.append(v)
                    v = reach[v][1]
            worst = (drop, path)
    return None if worst is None else worst[1]


def resolve_grades(
    g: Graph, crossings: list[Crossing], coords: dict[int, tuple[float, float]]
) -> list[int]:
    """Rule 4/6: steepen short links to SHORT_LINK_GRADE only where a crossing needs it, then
    drop the crossings that still cannot be separated. Returns the steepened way ids."""
    steepened: set[int] = set()
    short = {w.id for w in g.ways.values() if not w.rail and is_short_link(w, coords)}
    for c in crossings:
        path = _infeasible_path(g, c)
        if path is None:
            continue
        on_path = {w for v in path for _, _, w in g.adj[v]} & short
        on_path = {w for w in on_path if g.grade[w] < SHORT_LINK_GRADE}
        for w in on_path:
            g.grade[w] = SHORT_LINK_GRADE
            steepened.add(w)
        if on_path:
            path = _infeasible_path(g, c)
        if path is not None:
            c.dropped = "lower road joins the upper deck too close for the grade limits"
    # steepening only ever makes other crossings easier, but re-check the ones kept
    for c in crossings:
        if c.dropped is None and _infeasible_path(g, c) is not None:
            c.dropped = "lower road joins the upper deck too close for the grade limits"
    return sorted(steepened)


def deck_chains(g: Graph) -> list[list[int]]:
    """Vertex sequences of continuous elevated decks (rule 3): ways on effective layer >= 1
    (not real tunnels) joined end to end where exactly two of them meet and none passes."""
    decks = [
        w.id for w in g.ways.values() if effective_layer(w.tags) >= 1 and not is_real_tunnel(w.tags)
    ]
    ends: dict[int, list[int]] = defaultdict(list)
    inner: Counter = Counter()
    for wid in decks:
        line = g.lines[wid]
        ends[line[0]].append(wid)
        ends[line[-1]].append(wid)
        for v in line[1:-1]:
            inner[v] += 1

    def partner(wid: int, v: int) -> int | None:
        ws = ends[v]
        if len(ws) == 2 and inner[v] == 0 and ws[0] != ws[1]:
            return ws[1] if ws[0] == wid else ws[0]
        return None

    seen: set[int] = set()
    chains = []
    for start in decks:
        if start in seen:
            continue
        # walk back to one end of this chain (or around a ring)
        wid, v = start, g.lines[start][0]
        visited = {wid}
        while (p := partner(wid, v)) is not None and p not in visited:
            visited.add(p)
            line = g.lines[p]
            v = line[0] if line[-1] == v else line[-1]
            wid = p
        # walk forward from there, orienting each way
        seq: list[int] = []
        cur, entry = wid, v
        while cur is not None and cur not in seen:
            seen.add(cur)
            line = g.lines[cur]
            if line[0] != entry:
                line = line[::-1]
            seq.extend(line if not seq else line[1:])
            exit_v = line[-1]
            nxt = partner(cur, exit_v)
            cur, entry = (nxt, exit_v) if nxt is not None else (None, exit_v)
        chains.append(seq)
    return chains


def _sag_fill(chains: list[list[int]], z: list[float], req: dict[int, float]) -> None:
    for seq in chains:
        zs = [z[v] for v in seq]
        pre = list(zs)
        for i in range(1, len(pre)):
            pre[i] = max(pre[i - 1], pre[i])
        suf = list(zs)
        for i in range(len(suf) - 2, -1, -1):
            suf[i] = max(suf[i + 1], suf[i])
        for i, v in enumerate(seq):
            fill = min(pre[i], suf[i])  # always >= z[v]; kept every round so z never drops
            if fill > req.get(v, 0.0):
                req[v] = fill


def _upper_z(z: list[float], c: Crossing) -> float:
    return (1 - c.up_f) * z[c.up_a] + c.up_f * z[c.up_b]


def is_deck_way(w: Way) -> bool:
    return is_bridge(w.tags) or effective_layer(w.tags) >= 1


def deck_vertices(g: Graph) -> set[int]:
    """Vertices on elevated ways (bridges, or effective layer >= 1)."""
    out: set[int] = set()
    for w in g.ways.values():
        if is_bridge(w.tags) or effective_layer(w.tags) >= 1:
            out.update(g.lines[w.id])
    return out


def _lower_z(z: list[float], c: Crossing) -> float:
    return (1 - c.low_f) * z[c.low_a] + c.low_f * z[c.low_b]


def solve(
    g: Graph,
    crossings: list[Crossing],
    open_decks: dict[int, float] | None = None,
    floors: dict[int, float] | None = None,
    caps: list[float] | None = None,
    bridge_base: bool = True,
    sag: bool = True,
    anchored: set[int] | None = None,
) -> tuple[list[float], int]:
    """The least heights satisfying rules 1-4, 7 (fixed-point iteration). Returns (z, rounds).

    `open_decks` (vertex -> m) are measured open-deck heights; they replace OPEN_DECK_M there.
    `floors` (vertex -> m) are the rule-7 layer floors. `caps` (per vertex, from
    `measured_caps`) bound every requirement: estimated ones (open-deck height, floors, sag fill,
    crossings with an estimated separation) bend around the measurements; a measured one is
    only cut where two measurements disagree (the anchors win; reported as a conflict).
    `anchored` = vertices held by measurements (see the `floating` comment below).
    `bridge_base=False` drops the open-deck height (phase A of `measured_caps`).
    """
    open_decks = open_decks or {}
    inf = math.inf
    cap = caps if caps is not None else None
    est_base: dict[int, float] = {}
    meas_base: dict[int, float] = dict(open_decks)
    if bridge_base:
        deck_base = getattr(g, "deck_base", {})  # e.g. river bridges: measured median height
        for w in g.ways.values():
            if is_bridge(w.tags) and not is_tunnel(w.tags):
                for v in g.lines[w.id]:
                    if v not in open_decks:
                        est_base[v] = deck_base.get(v, OPEN_DECK_M)
        # unpinned deck-profile vertices: their measurement stays a lower bound (bent by the caps)
        for v, h in getattr(g, "soft_deck", {}).items():
            if v not in open_decks:
                est_base[v] = max(est_base.get(v, 0.0), h)
    for v, h in (floors or {}).items():
        est_base[v] = max(est_base.get(v, 0.0), h)
    chains = deck_chains(g)
    active = [c for c in crossings if c.dropped is None]
    # a pinned separation over an ESTIMATED deck (not grounded): where the measured deck above
    # is anchored, the deck beneath follows it (lower >= upper - separation), so the measurement
    # holds exactly; an unanchored pair keeps the separation as a lower bound (rule 1), which
    # avoids feeding back small disagreements between measurements (e.g. 26619046 over CityLink)
    anchored = anchored or set()
    floating = [
        c
        for c in active
        if c.pinned and is_deck_way(g.ways[c.lower]) and c.up_a in anchored and c.up_b in anchored
    ]

    def merge(est: dict[int, float], meas: dict[int, float]) -> dict[int, float]:
        # every requirement is held at the caps: estimates bend; a MEASURED requirement above
        # its cap is a conflict between measurements (reported by the caller) and the anchors
        # win. This also bounds every value, so the iteration cannot creep on a cycle of
        # slightly inconsistent measurements.
        out: dict[int, float] = {}
        for src in (meas, est):
            for v, h in src.items():
                if cap is not None and cap[v] < inf:
                    h = min(h, cap[v])
                if h > out.get(v, 0.0):
                    out[v] = h
        return out

    z = envelope(g, merge(est_base, meas_base))
    for rounds in range(1, MAX_ROUNDS + 1):
        est = dict(est_base)
        meas = dict(meas_base)
        for c in active:
            need = _lower_z(z, c) + c.separation
            tgt = meas if c.measured else est
            for v in (c.up_a, c.up_b):
                if need > tgt.get(v, 0.0):
                    tgt[v] = need
        for c in floating:
            low_need = _upper_z(z, c) - c.separation
            for v in (c.low_a, c.low_b):
                if low_need > meas.get(v, 0.0):
                    meas[v] = low_need
        if sag:
            _sag_fill(chains, z, est)
        new = envelope(g, merge(est, meas))
        if max(new) > Z_LIMIT_M:
            raise RuntimeError(f"heights diverge (> {Z_LIMIT_M} m): an unresolved cycle")
        if all(abs(a - b) < 1e-7 for a, b in zip(new, z, strict=True)):
            return new, rounds
        z = new
    raise RuntimeError(f"heights did not converge in {MAX_ROUNDS} rounds")


def upper_bounds(
    g: Graph, crossings: list[Crossing], anchors: dict[int, float]
) -> tuple[list[float], int]:
    """Per-vertex upper bounds implied by measured anchors (vertex -> measured height): through
    every grade (|dz| <= grade x length, both ways) and every crossing under a bounded deck (the
    lower way <= deck - separation for a point-cloud pin, deck - CHECK_CLEARANCE_M -
    BEND_MARGIN_M for anything else: an estimate, a `maxheight` lower bound, a demoted pin).
    Unbounded vertices stay at +inf. Returns (bounds, vertices whose bound is below 0, i.e.
    measurements that cannot all hold)."""
    inf = math.inf
    n = len(g.lon)
    U = [inf] * n
    for v, h in anchors.items():
        U[v] = min(U[v], h)
    active = [c for c in crossings if c.dropped is None]
    for _ in range(MAX_ROUNDS):
        heap = [(U[v], v) for v in range(n) if U[v] < inf]
        heapq.heapify(heap)
        while heap:
            uu, u = heapq.heappop(heap)
            if uu > U[u]:
                continue
            for v, d, w in g.adj[u]:
                cand = uu + g.grade[w] * d
                if cand < U[v] - 1e-12:
                    U[v] = cand
                    heapq.heappush(heap, (cand, v))
        changed = False
        for c in active:
            m = min(U[c.up_a], U[c.up_b])
            if m == inf:
                continue
            if c.pinned:
                b = m - c.separation
            elif c.trench is not None:
                b = m  # over a cutting (Stage B lowers it): the deck need only stay above it
            else:
                b = m - (CHECK_CLEARANCE_M + BEND_MARGIN_M)
            for v in (c.low_a, c.low_b):
                if b < U[v] - 1e-9:
                    U[v] = b
                    changed = True
        if not changed:
            break
    return U, sum(1 for u in U if u < -1e-6)


def measured_caps(
    g: Graph, crossings: list[Crossing], open_decks: dict[int, float]
) -> tuple[list[float], dict[str, Any]]:
    """Measured precedence (orchestrator decisions 2026-10-26: measured always wins; estimates
    bend around measurements, never the reverse; a point-cloud separation is an anchor).

    1. Pins. A point-cloud separation over a GROUNDED lower road (a way that is not an elevated
       deck) pins the upper deck at both ends of its crossing segment at exactly lower + the
       separation, so the deck may sag where the cloud shows it sag. A point-cloud open-deck
       height pins its vertex. (A point-cloud separation over an estimated deck is relative:
       `solve` makes that deck follow, lower >= upper - separation.)
    2. Anchors: the pins only. `maxheight` is a lower bound (a signed limit), never an anchor.
       Between anchors a deck is held only by the grade cones from them, so it may sag where
       the cloud shows a sag.
    Their `upper_bounds` cap every estimated requirement in the real solve."""
    measured = [c for c in crossings if c.dropped is None and c.measured]
    on_deck = deck_vertices(g)
    z0, _ = solve(g, measured, open_decks, bridge_base=False, sag=False)
    pins: dict[int, float] = {v: h for v, h in open_decks.items() if h > 0}
    for c in measured:
        # grounded lower: an at-grade road, or a deck held by its own measured profile
        if c.pinned and all(v not in on_deck or v in pins for v in (c.low_a, c.low_b)):
            val = _lower_z(z0, c) + c.separation
            for v in (c.up_a, c.up_b):
                pins[v] = min(pins.get(v, math.inf), val)
    # anchors are the point-cloud pins only: a `maxheight` clearance is a lower bound by nature
    # (a signed limit), never an anchor
    anchors = dict(pins)
    anchor_vs = set(pins)
    z_a = z0
    caps, negative = upper_bounds(g, crossings, anchors)
    vway: dict[int, list[int]] = defaultdict(list)
    for wid, line in g.lines.items():
        for v in line:
            vway[v].append(wid)
    conflicts, rail_conflicts = [], []
    for v in range(len(caps)):
        if caps[v] >= -1e-6:
            continue
        rec = {
            "ways": sorted(vway[v]),
            "names": sorted({str(g.ways[w].tags.get("name")) for w in vway[v]}),
            "bound_m": round(caps[v], 2),
            "lon": round(g.lon[v], 6),
            "lat": round(g.lat[v], 6),
        }
        # railways are internal (never drawn): listed apart from the road conflicts
        (rail_conflicts if all(g.ways[w].rail for w in vway[v]) else conflicts).append(rec)
    negative = len(conflicts)
    return caps, {
        "conflicts_below_ground": conflicts,
        "rail_conflicts_below_ground": rail_conflicts,
        "anchors": len(anchor_vs),
        "pins": len(pins),
        "pinned_vertices": len(anchors),
        "vertices_bounded_below_zero": negative,
        "z_a": z_a,
        "pinned": anchors,
    }


def compute_heights(
    g: Graph,
    crossings: list[Crossing],
    coords: dict[int, tuple[float, float]],
    open_decks: dict[int, float] | None = None,
    floors: dict[int, float] | None = None,
    caps: list[float] | None = None,
    anchored: set[int] | None = None,
) -> tuple[list[float], dict[str, Any]]:
    resolve_grades(g, crossings, coords)
    steepened = sorted(w for w, gr in g.grade.items() if gr > MAX_GRADE)
    z, rounds = solve(g, crossings, open_decks, floors, caps, anchored=anchored)
    out = []
    for v in z:
        r = round(v, Z_DECIMALS)
        out.append(0.0 if abs(r) < 10**-Z_DECIMALS else r)
    return out, {"steepened_links": steepened, "rounds": rounds, "z_raw": z}


def road_class(tags: dict[str, str]) -> str:
    return "freeway" if tags.get("highway") in FREEWAY_HIGHWAYS else "other"


def calibrate(g: Graph, crossings: list[Crossing]) -> dict[str, Any]:
    """Separations for the crossings the point cloud did not measure (orchestrator decisions
    2026-10-26), from the point-cloud measurements used in this build:

      default separation, in order: (1) per deck: the median of the UPPER way's own measured
      separations, if it has any; (2) per class of the upper way (freeway = motorway, trunk and
      their links; other): the class median if n >= CALIBRATION_MIN_N; (3) the global median if
      n >= CALIBRATION_MIN_N; (4) DEFAULT_SEPARATION_M.
      deck thickness added to a `maxheight` clearance: median (measured separation - maxheight)
      per class of the upper way if n >= CALIBRATION_MIN_N, else global if n >= that, else
      DECK_THICKNESS_M.
    Point-cloud separations are never changed. Sets `separation` and `basis` on the others."""
    import statistics

    def med(xs: list[float]) -> float | None:
        return statistics.median(xs) if len(xs) >= CALIBRATION_MIN_N else None

    pc = [
        c
        for c in crossings
        if c.source == "pointcloud" and c.dropped is None and not g.ways[c.lower].rail
    ]
    by_deck: dict[int, list[float]] = defaultdict(list)
    by_class: dict[str, list[float]] = defaultdict(list)
    th_class: dict[str, list[float]] = defaultdict(list)
    for c in pc:
        k = road_class(g.ways[c.upper].tags)
        by_deck[c.upper].append(c.separation + c.lower_depth)
        by_class[k].append(c.separation + c.lower_depth)
        if c.clearance_m is not None:
            th_class[k].append(c.separation + c.lower_depth - c.clearance_m)
    seps = [c.separation + c.lower_depth for c in pc]
    # deck thickness: every valid point-cloud measurement with a `maxheight` on the same crossing
    # (in use or demoted), not only the pins in use (orchestrator, qa round 2)
    th_class = defaultdict(list)
    for c in crossings:
        if (
            c.source in ("pointcloud", "pointcloud_soft")
            and c.dropped is None
            and c.clearance_m is not None
            and not g.ways[c.lower].rail
        ):
            th_class[road_class(g.ways[c.upper].tags)].append(
                c.separation + c.lower_depth - c.clearance_m
            )
    pairs = [x for xs in th_class.values() for x in xs]
    g_sep = med(seps)
    g_th = med(pairs)
    sep_class = {k: med(by_class.get(k, [])) for k in ("freeway", "other")}
    th_cls = {k: med(th_class.get(k, [])) for k in ("freeway", "other")}
    deck_med = {w: statistics.median(xs) for w, xs in by_deck.items()}
    basis_count: Counter = Counter()
    for c in crossings:
        if c.source in ("pointcloud", "pointcloud_soft"):
            continue
        k = road_class(g.ways[c.upper].tags)
        if c.source == "maxheight":
            if th_cls[k] is not None:
                c.separation, c.basis = c.clearance_m + th_cls[k], "class"
            elif g_th is not None:
                c.separation, c.basis = c.clearance_m + g_th, "global"
            else:
                c.separation, c.basis = c.clearance_m + DECK_THICKNESS_M, "constant"
        elif c.upper in deck_med:
            c.separation, c.basis = deck_med[c.upper], "deck"
        elif sep_class[k] is not None:
            c.separation, c.basis = sep_class[k], "class"
        elif g_sep is not None:
            c.separation, c.basis = g_sep, "global"
        else:
            c.separation, c.basis = DEFAULT_SEPARATION_M, "constant"
        basis_count[f"{c.source}:{c.basis}"] += 1

    def rnd(x: float | None) -> float | None:
        return None if x is None else round(x, 2)

    return {
        "min_n": CALIBRATION_MIN_N,
        "separation_global_m": rnd(g_sep) if g_sep is not None else DEFAULT_SEPARATION_M,
        "separation_global_n": len(seps),
        "separation_freeway_m": rnd(sep_class["freeway"]),
        "separation_freeway_n": len(by_class.get("freeway", [])),
        "separation_other_m": rnd(sep_class["other"]),
        "separation_other_n": len(by_class.get("other", [])),
        "decks_with_own_median": len(deck_med),
        "deck_thickness_global_m": rnd(g_th) if g_th is not None else DECK_THICKNESS_M,
        "deck_thickness_global_n": len(pairs),
        "deck_thickness_freeway_m": rnd(th_cls["freeway"]),
        "deck_thickness_freeway_n": len(th_class.get("freeway", [])),
        "deck_thickness_other_m": rnd(th_cls["other"]),
        "deck_thickness_other_n": len(th_class.get("other", [])),
        "crossings_by_basis": dict(sorted(basis_count.items())),
        "note": "a class value of null means n < min_n: the global value (or constant) is used",
    }


def class_separation(calibration: dict[str, Any], cls: str) -> float:
    v = calibration.get(f"separation_{cls}_m")
    return v if v is not None else calibration["separation_global_m"]


def crossing_dz(g: Graph, z: list[float], c: Crossing, proj: _Proj) -> float:
    """z(upper) - z(lower) at the crossing point, both interpolated (as the check does)."""
    x, y = proj.xy(c.lon, c.lat)
    ax, ay = proj.xy(g.lon[c.up_a], g.lat[c.up_a])
    bx, by = proj.xy(g.lon[c.up_b], g.lat[c.up_b])
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    f = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / L2))
    return (1 - f) * z[c.up_a] + f * z[c.up_b] - _lower_z(z, c)


WATER_NEAR_M = 20.0  # a deck this close to a waterway line or water area is a river bridge
WATER_CHAIN_M = 60.0  # ... and so are pieces of the same deck chain this close to the water


def over_water(g: Graph, wid: int, water: list[Any], proj: _Proj) -> bool:
    from shapely.geometry import LineString

    line = LineString([proj.xy(g.lon[v], g.lat[v]) for v in g.lines[wid]])
    return any(line.distance(w) <= WATER_NEAR_M for w in water)


def river_bridge_ways(g: Graph, water: list[Any], proj: _Proj) -> set[int]:
    """Bridge ways over water, extended to the pieces of the same deck chain within
    WATER_CHAIN_M of the water (a river bridge split into pieces in OSM, e.g. the Swan Street
    Bridge 985383991 / 985383992), not to a whole viaduct that happens to cross a creek."""
    on_water = {
        w.id
        for w in g.ways.values()
        if not w.rail
        and is_bridge(w.tags)
        and not is_tunnel(w.tags)  # a tunnel under water is never a river bridge
        and over_water(g, w.id, water, proj)
    }
    vway: dict[int, set[int]] = defaultdict(set)
    for wid, line in g.lines.items():
        if not g.ways[wid].rail and is_bridge(g.ways[wid].tags) and not is_tunnel(g.ways[wid].tags):
            for v in line:
                vway[v].add(wid)
    from shapely.geometry import LineString

    def near_water(wid: int) -> bool:
        line = LineString([proj.xy(g.lon[v], g.lat[v]) for v in g.lines[wid]])
        return any(line.distance(w) <= WATER_CHAIN_M for w in water)

    out = set(on_water)
    for seq in deck_chains(g):
        ws = {w for v in seq for w in vway.get(v, ())}
        if ws & on_water:
            out |= {w for w in ws if near_water(w)}
    return out


def floor_candidates(
    g: Graph,
    crossings: list[Crossing],
    open_decks: dict[int, float],
    z: list[float],
    water: list[Any],
    proj: _Proj,
) -> tuple[list[int], dict[str, list[int]]]:
    """Rule 7 candidates: FREEWAY-class road bridge ways (motorway, trunk and their links) with
    nothing beneath (on top at no rule-1 crossing), no measured height (no point-cloud open-deck
    profile), not over water, and left by the other rules at the bare open-deck height (max z <=
    OPEN_DECK_M). Also returns the other bare bridge ways, by why they are excluded."""
    on_top = {c.upper for c in crossings if c.dropped is None}
    measured = set(open_decks)
    rivers = river_bridge_ways(g, water, proj)
    keep, excluded = [], {"not_freeway": [], "over_water": [], "held_up_by_rules": []}
    for w in sorted(g.ways.values(), key=lambda w: w.id):
        if w.rail or not is_bridge(w.tags) or is_tunnel(w.tags) or w.id in on_top:
            continue
        line = g.lines[w.id]
        if any(v in measured for v in line):
            continue
        if road_class(w.tags) != "freeway":
            if max(z[v] for v in line) <= OPEN_DECK_M + 0.01:
                excluded["not_freeway"].append(w.id)
        elif w.id in rivers and effective_layer(w.tags) <= 1:
            # a street-level river bridge (e.g. Swan St): the measured river-deck median; a
            # freeway viaduct over water on layer >= 2 (e.g. the Bolte) may be floored
            excluded["over_water"].append(w.id)
        elif max(z[v] for v in line) > OPEN_DECK_M + 0.01:
            excluded["held_up_by_rules"].append(w.id)
        else:
            keep.append(w.id)
    return keep, excluded


FLOOR_TOL_M = 0.1  # bisection resolution for a reduced floor


def apply_floors(
    g: Graph,
    crossings: list[Crossing],
    coords: dict[int, tuple[float, float]],
    open_decks: dict[int, float],
    candidates: list[int],
    separation: float,
    proj: _Proj,
    caps: list[float] | None = None,
    anchored: set[int] | None = None,
) -> tuple[list[float], dict[str, Any], dict[str, Any]]:
    """Rule 7: a floor of effective layer x calibrated separation on each candidate way, one way
    at a time (way-id order). A floor stands only if heights still converge, every rule-1
    crossing keeps >= CHECK_CLEARANCE_M (0 order mismatch, 0 separated_low) and no MEASURED anchor
    is lifted above its measurement (measured heights outrank rule estimates). If the full floor
    fails, the highest floor that passes is found by bisection (`reduced`); if none above
    OPEN_DECK_M passes, the floor is dropped for that way. All three are reported."""
    _, base_info = compute_heights(g, crossings, coords, open_decks, caps=caps, anchored=anchored)
    zb = base_info["z_raw"]
    anchors = measured_vertices(zb, crossings, open_decks)
    # crossings already below the check's clearance without any floor (measurements that cannot
    # all hold); a floor is only rejected for making a NEW one
    base_low = {
        id(c)
        for c in crossings
        if c.dropped is None and crossing_dz(g, zb, c, proj) < CHECK_CLEARANCE_M - 0.01
    }

    def floors_for(hs: dict[int, float]) -> dict[int, float]:
        out: dict[int, float] = {}
        for wid, h in hs.items():
            for v in g.lines[wid]:
                out[v] = max(out.get(v, 0.0), h)
        return out

    def ok(hs: dict[int, float]) -> tuple[list[float], dict[str, Any]] | None:
        try:
            z, info = compute_heights(
                g, crossings, coords, open_decks, floors_for(hs), caps, anchored
            )
        except RuntimeError:
            return None
        zr = info["z_raw"]
        for c in crossings:
            if (
                c.dropped is None
                and id(c) not in base_low
                and crossing_dz(g, zr, c, proj) < CHECK_CLEARANCE_M - 0.01
            ):
                return None
        if any(zr[v] > zb[v] + 0.05 for v in anchors):
            return None
        return z, info

    kept: dict[int, float] = {}
    report: dict[str, Any] = {
        "crossings_below_check_clearance_without_floors": len(base_low),
        "candidates": candidates,
        "full": {},
        "reduced": {},
        "dropped": [],
    }
    for wid in candidates:
        target = effective_layer(g.ways[wid].tags) * separation
        if ok({**kept, wid: target}) is not None:
            kept[wid] = target
            report["full"][str(wid)] = round(target, 2)
            continue
        lo, hi = OPEN_DECK_M, target  # lo passes (it is the open-deck height already)
        while hi - lo > FLOOR_TOL_M:
            mid = (lo + hi) / 2
            if ok({**kept, wid: mid}) is not None:
                lo = mid
            else:
                hi = mid
        if lo > OPEN_DECK_M + FLOOR_TOL_M:
            kept[wid] = lo
            report["reduced"][str(wid)] = {"floor_m": round(lo, 2), "rule_m": round(target, 2)}
        else:
            report["dropped"].append(wid)
    res = ok(kept)
    if res is None:  # cannot happen: every kept floor passed with the ones before it
        raise RuntimeError("layer floors: inconsistent result")
    z, info = res
    return z, info, report


def mark_trenches(g: Graph, crossings: list[Crossing], proj: _Proj) -> list[dict[str, Any]]:
    """Crossings over a road in a cutting (pending_trench_depth): the point-cloud crossings that
    found one (`Crossing.trench`, set by deck_pointcloud), and every other crossing over the same
    lower road within TRENCH_NEAR_M of one (depth from the nearest measurement). Returns the
    report list read by pipeline.checks.grade_separation."""
    found: dict[int, list[tuple[float, float, dict[str, Any]]]] = defaultdict(list)
    for c in crossings:
        if c.trench is not None and "nearest_measured_m" not in c.trench:
            found[c.lower].append((*proj.xy(c.lon, c.lat), c.trench))
    for c in crossings:
        if c.dropped is not None or c.trench is not None or c.lower not in found:
            continue
        x, y = proj.xy(c.lon, c.lat)
        d, tr = min(
            ((math.hypot(x - a, y - b), t) for a, b, t in found[c.lower]), key=lambda q: q[0]
        )
        if d <= TRENCH_NEAR_M:
            c.trench = {"trench_depth_m": tr["trench_depth_m"], "nearest_measured_m": round(d, 1)}
    out = []
    for c in crossings:
        if c.dropped is None and c.trench is not None:
            out.append(
                {
                    "upper_way": c.upper,
                    "lower_way": c.lower,
                    "lon": round(c.lon, 6),
                    "lat": round(c.lat, 6),
                    "measured": "nearest_measured_m" not in c.trench,
                    **c.trench,
                }
            )
    return out


def measured_vertices(
    z: list[float], crossings: list[Crossing], open_decks: dict[int, float] | None = None
) -> set[int]:
    """Vertices whose height is set by a measurement (that constraint is binding): a crossing
    separation from the point cloud or a `maxheight` tag, or a point-cloud open-deck height."""
    out = {v for v, h in (open_decks or {}).items() if abs(z[v] - h) < 1e-6 and h > 0}
    for c in crossings:
        if c.dropped is None and c.measured:
            need = _lower_z(z, c) + c.separation
            for v in (c.up_a, c.up_b):
                if abs(z[v] - need) < 1e-6:
                    out.add(v)
    return out


# --- Stage B: tunnels and cuttings below street level ------------------------------------------


def _cone_up(
    g: Graph, sources: dict[int, float], free: set[int], stop: list[float]
) -> dict[int, float]:
    """min over sources of (value + grade x road distance), through `free` vertices only; a
    vertex at or above `stop` (its height without this pass) is not expanded: past it, the
    cone can no longer go below `stop` anywhere (`stop` is grade-Lipschitz)."""
    best: dict[int, float] = {}
    heap = []
    for v, h in sources.items():
        if v in free and h < best.get(v, math.inf):
            best[v] = h
            heap.append((h, v))
    heapq.heapify(heap)
    while heap:
        h, u = heapq.heappop(heap)
        if h > best[u] or h >= stop[u] - 1e-9:
            continue
        for v, d, w in g.adj[u]:
            if v not in free:
                continue
            cand = h + g.grade[w] * d
            if cand < best.get(v, math.inf) - 1e-12:
                best[v] = cand
                heapq.heappush(heap, (cand, v))
    return best


def _cone_down(g: Graph, sources: dict[int, float], region: set[int]) -> dict[int, float]:
    """max over sources of (value - grade x road distance) on the `region` vertices, through
    `region` only (sources may lie outside it, on its boundary)."""
    best: dict[int, float] = dict(sources)
    heap = [(-h, v) for v, h in sources.items()]
    heapq.heapify(heap)
    while heap:
        neg, u = heapq.heappop(heap)
        h = -neg
        if h < best[u]:
            continue
        for v, d, w in g.adj[u]:
            if v not in region:
                continue
            cand = h - g.grade[w] * d
            if cand > best.get(v, -math.inf) + 1e-12:
                best[v] = cand
                heapq.heappush(heap, (-cand, v))
    return best


def _lower_pass(
    g: Graph,
    z_hi: list[float],
    free: set[int],
    upper_src: dict[int, float],
    hard: dict[int, float],
) -> tuple[list[float], dict[int, float]]:
    """One lowering pass: z = max(L, min(z_hi, D)) on the free vertices that D reaches below
    z_hi, where D = the up-cone of `upper_src` (z <= value: targets, caps, pins) and L = the
    down-cone of the region's boundary (vertices outside it keep z_hi) and of the `hard` pins
    (z >= value). Grade-Lipschitz (max / min of grade-Lipschitz functions) and never above
    z_hi. Returns (z, L on the region) (L tells how deep the region can go)."""
    D = _cone_up(g, upper_src, free, z_hi)
    region = {v for v, h in D.items() if h < z_hi[v] - 1e-9}
    src: dict[int, float] = {}
    for v in region:
        for u, _, _ in g.adj[v]:
            if u not in region:
                src[u] = z_hi[u]
    for v, h in hard.items():
        if v in region:
            src[v] = max(src.get(v, -math.inf), h)
    L = _cone_down(g, src, region)
    z = list(z_hi)
    for v in region:
        z[v] = min(z_hi[v], max(L.get(v, -math.inf), min(z_hi[v], D[v])))
    return z, {v: L.get(v, -math.inf) for v in region}


def _min_merge(*bounds: dict[int, float]) -> dict[int, float]:
    """Upper bounds from several sources: the tightest (lowest) per vertex."""
    out: dict[int, float] = {}
    for b in bounds:
        for v, h in b.items():
            if h < out.get(v, math.inf):
                out[v] = h
    return out


def lower_below_ground(
    g: Graph,
    z_main: list[float],
    *,
    targets: dict[int, float],
    interior: set[int],
    fixed: set[int],
    pins: dict[int, float],
    water_caps: dict[int, float],
    tunnel_crossings: list[Crossing],
) -> tuple[list[float], dict[str, Any]]:
    """Stage B heights below street level, on top of the Stage A solution `z_main` (>= 0).

    Step 1 (lower only as far as something requires): free = every road vertex not in `fixed`
    (railways, raised deck vertices, tunnel portals whose outside road is not in the extract).
    Upper bounds: measured cutting `pins` (z <= pin), `water_caps` (z <= the river-bed limit) and
    `tunnel_crossings` caps (the tunnel <= the road above - separation, at both ends of its
    crossing segment); lower bounds: the pins that can hold (z >= pin). Approach roads come down
    only as far as those need, at the grade limits (6 %, 8 % short links).
    Step 2 (tunnels to their target depth): free = `interior` (vertices only on lowered tunnel
    ways); upper bounds: `targets` (z <= -10 m x level) and the same caps; every other vertex
    keeps its step-1 height, so each tunnel ramps down from its portals at the grade limits and
    sits at its target wherever the portals are far enough away.
    Crossing caps depend on the road above (which may be lowered too): both steps repeat until
    the caps settle.

    Pins (each a measured cutting vertex) that the grade limits cannot hold against fixed
    vertices or other pins are demoted to upper bounds only (`pins_demoted`): drawn as close to
    the measurement as the neighbours allow, like a demoted Stage A point-cloud pin."""
    road = {v for w, line in g.lines.items() if not g.ways[w].rail for v in line}
    free1 = road - fixed
    hard = {v: h for v, h in pins.items() if v in free1}
    report: dict[str, Any] = {
        "pins": len(pins),
        "pins_on_fixed_vertices": sorted(v for v in pins if v not in free1),
    }
    demoted: dict[int, float] = {}
    for _ in range(len(hard) + 1):
        _, L = _lower_pass(g, z_main, free1, _min_merge(hard, demoted), hard)
        worst = max(((L.get(v, -math.inf) - h, v) for v, h in hard.items()), default=(0.0, None))
        if worst[1] is None or worst[0] <= PIN_UNDER_TOL_M:
            break
        v = worst[1]
        demoted[v] = hard.pop(v)
    z = list(z_main)
    caps: dict[int, float] = {}
    rounds = 0
    for rounds in range(1, MAX_ROUNDS + 1):
        new_caps = dict(water_caps)
        for c in tunnel_crossings:
            cap = _upper_z(z, c) - c.separation
            for v in (c.low_a, c.low_b):
                new_caps[v] = min(new_caps.get(v, math.inf), cap)
        if rounds > 1 and all(abs(new_caps[v] - caps.get(v, math.inf)) < 1e-7 for v in new_caps):
            break
        caps = new_caps
        z1, _ = _lower_pass(g, z_main, free1, _min_merge(caps, hard, demoted), hard)
        z2, _ = _lower_pass(g, z1, interior - fixed, _min_merge(targets, caps), {})
        z = z2
    else:
        raise RuntimeError("below-ground caps did not settle")
    report.update(
        rounds=rounds,
        pins_held=len(hard),
        pins_demoted=[
            {
                "lon": round(g.lon[v], 6),
                "lat": round(g.lat[v], 6),
                "measured_z_m": round(h, 2),
                "final_z_m": round(z[v], 2),
            }
            for v, h in sorted(demoted.items())
        ],
        held_pins=hard,
    )
    return z, report


def tunnel_order(wa: Way, wb: Way) -> tuple[Way, Way] | None:
    """(upper, lower) where the lower way is a tunnel Stage B lowers (`tunnel_target`) on a
    lower effective layer than the other way (a road, a railway or another tunnel); else None."""
    la, lb = effective_layer(wa.tags), effective_layer(wb.tags)
    if la == lb:
        return None
    upper, lower = (wa, wb) if la > lb else (wb, wa)
    if lower.rail or tunnel_target(lower.tags) is None:
        return None
    return upper, lower


def tunnel_separation(tags: dict[str, str]) -> float:
    """Road surface above to tunnel road surface: the tunnel's numeric clearance (`maxheight`)
    + DECK_THICKNESS_M (its roof), else DEFAULT_SEPARATION_M; never below the check's 4 m."""
    mc = measured_clearance(tags)
    sep = mc[0] + DECK_THICKNESS_M if mc else DEFAULT_SEPARATION_M
    return max(sep, CHECK_CLEARANCE_M + BEND_MARGIN_M)


def build_tunnel_crossings(
    g: Graph, coords: dict[int, tuple[float, float]], proj: _Proj
) -> list[Crossing]:
    """Every 2D crossing over a lowered tunnel (`tunnel_order`), as a Crossing whose
    `separation` is `tunnel_separation` of the tunnel (source "tunnel")."""
    vxy = [proj.xy(lo, la) for lo, la in zip(g.lon, g.lat, strict=True)]
    out = []
    for up, low, x, y, kind in crossing_points(list(g.ways.values()), coords, proj, tunnel_order):
        lu, ll = g.lines[up.id], g.lines[low.id]
        iu, fu = _locate([vxy[v] for v in lu], x, y)
        il, fl = _locate([vxy[v] for v in ll], x, y)
        lon, lat = proj.lonlat(x, y)
        out.append(
            Crossing(
                upper=up.id,
                lower=low.id,
                up_a=lu[iu],
                up_b=lu[iu + 1],
                low_a=ll[il],
                low_b=ll[il + 1],
                low_f=fl,
                separation=tunnel_separation(low.tags),
                clearance_m=None,
                clearance_key=None,
                lon=lon,
                lat=lat,
                kind=kind,
                source="tunnel",
                up_f=fu,
            )
        )
    return out


def water_geometries(rings: list[Any], proj: _Proj) -> list[Any]:
    """Shapely geometries in local metres: an (outer, [inner, ...], label) triple or a closed ring
    is a Polygon, an open line a LineString."""
    from shapely.geometry import LineString, Polygon

    out = []
    for r in rings:
        if isinstance(r, tuple):
            outer, inners = r[0], r[1]
            out.append(
                Polygon(
                    [proj.xy(*p) for p in outer], [[proj.xy(*p) for p in ring] for ring in inners]
                )
            )
            continue
        xy = [proj.xy(*p) for p in r]
        if len(xy) >= 4 and xy[0] == xy[-1]:
            out.append(Polygon(xy))
        elif len(xy) >= 2:
            out.append(LineString(xy))
    return out


def river_bed(label: dict[str, str], geom: Any) -> bool:
    """Does the river-bed rule apply under this water area (RIVER_BED_WATER)?"""
    if label.get("water") in RIVER_BED_WATER or label.get("waterway") in WATER_AREA_WATERWAYS:
        return True
    if "water" in label:
        return False  # pond, fountain, reflecting pool, ...
    return geom.geom_type == "Polygon" and geom.area >= RIVER_BED_MIN_AREA_M2


def under_water_vertices(g: Graph, water: list[Any], proj: _Proj) -> dict[int, list[int]]:
    """Both end vertices of every lowered-tunnel segment (<= 10 m) that intersects a water area:
    vertex -> indices of the water geometries above it (a tunnel along a bank is not under it)."""
    from shapely.geometry import LineString
    from shapely.strtree import STRtree

    if not water:
        return {}
    tree = STRtree(water)
    out: dict[int, list[int]] = {}
    for w, way in g.ways.items():
        if way.rail or tunnel_target(way.tags) is None:
            continue
        line = g.lines[w]
        for a, b in zip(line, line[1:], strict=False):
            seg = LineString([proj.xy(g.lon[a], g.lat[a]), proj.xy(g.lon[b], g.lat[b])])
            hits = tree.query(seg, predicate="intersects").tolist()
            for v in (a, b) if hits else ():
                out[v] = sorted(set(out.get(v, [])) | set(hits))
    return out


def profile_depth_at(g: Graph, c: Crossing) -> float | None:
    """The measured cutting depth (>= 0) at a crossing's point on its lower way, interpolated
    between the two profile vertices of the crossing segment; None if either is unmeasured."""
    for rec in getattr(g, "cutting_profiles", []):
        if not rec.get("used") or rec["way"] != c.lower:
            continue
        line = g.lines[c.lower]
        ds = dict(zip(line, rec["depths_m"], strict=True))
        a, b = ds.get(c.low_a), ds.get(c.low_b)
        if a is None or b is None:
            return None
        return max(0.0, (1 - c.low_f) * a + c.low_f * b)
    return None


def offset_cutting_crossings(g: Graph, crossings: list[Crossing]) -> None:
    """Point-cloud separations measured to the floor of a measured cutting (the lower road was
    sampled directly, not referenced to street level): see `Crossing.lower_depth`."""
    for c in crossings:
        if c.source not in ("pointcloud", "pointcloud_soft") or c.dropped is not None:
            continue
        if c.trench is not None:
            continue  # measured against street level already (Stage A trench crossings)
        d = profile_depth_at(g, c)
        if d and d >= CUT_PIN_MIN_M:
            c.lower_depth = d
            c.separation -= d
            if c.pointcloud is not None:
                c.pointcloud["cutting_depth_at_crossing_m"] = round(d, 2)


def cutting_pins(g: Graph, crossings: list[Crossing] | None = None) -> dict[int, float]:
    """Measured cutting heights: vertex -> z (= -depth below street level).

    1. Profile vertices (pipeline.deck_pointcloud.measure_cutting, `g.cutting_profiles`) at least
       CUT_PIN_MIN_M deep; a vertex on two profiles keeps the shallower value.
    2. Where the profile has no value (the cutting floor is hidden under the bridges), the
       cutting depth measured at a crossing over it (`Crossing.trench`, measured, not the
       nearest-measurement estimate) pins both ends of the lower way's crossing segment."""
    pins: dict[int, float] = {}
    for rec in getattr(g, "cutting_profiles", []):
        if not rec.get("used"):
            continue
        for v, d in zip(g.lines[rec["way"]], rec["depths_m"], strict=True):
            if d is not None and d >= CUT_PIN_MIN_M:
                pins[v] = max(pins.get(v, -math.inf), -d)
    from_profiles = set(pins)
    for c in crossings or []:
        tr = c.trench
        if c.dropped is not None or tr is None or "nearest_measured_m" in tr:
            continue
        for v in (c.low_a, c.low_b):
            if v not in from_profiles:
                pins[v] = max(pins.get(v, -math.inf), -tr["trench_depth_m"])
    return pins


def stage_b(
    g: Graph,
    z_main: list[float],
    coords: dict[int, tuple[float, float]],
    proj: _Proj,
    crossings: list[Crossing],
    tunnel_water: list[Any] | None = None,
    portal_nodes: set[int] | None = None,
) -> tuple[list[float], dict[str, Any]]:
    """Tunnels and cuttings below street level (see `lower_below_ground`). Returns (z, report)."""
    ways_at: dict[int, list[int]] = defaultdict(list)
    for w, line in g.lines.items():
        for v in line:
            ways_at[v].append(w)
    tgt = {w: tunnel_target(way.tags) for w, way in g.ways.items() if not way.rail}
    lowered = {w for w, t in tgt.items() if t is not None}
    interior = {
        v
        for w in lowered
        for v in g.lines[w]
        if all(not g.ways[w2].rail and tgt.get(w2) is not None for w2 in ways_at[v])
    }
    targets = {v: max(tgt[w] for w in ways_at[v]) for v in interior}  # the shallowest
    fixed = {
        v
        for w, way in g.ways.items()
        if way.rail or is_deck_way(way)
        for v in g.lines[w]
        if way.rail or z_main[v] > 1e-6
    }
    portals = {g.node_vertex[(False, n)] for n in portal_nodes or () if (False, n) in g.node_vertex}
    fixed |= portals
    water = water_geometries(tunnel_water or [], proj)
    labels = [r[2] if isinstance(r, tuple) and len(r) > 2 else {} for r in (tunnel_water or [])]
    river = {i for i, (gm, lab) in enumerate(zip(water, labels, strict=True)) if river_bed(lab, gm)}
    wet_all = under_water_vertices(g, water, proj)
    wet = {v: [i for i in ix if i in river] for v, ix in wet_all.items()}
    wet = {v: ix for v, ix in wet.items() if ix}
    tcs = build_tunnel_crossings(g, coords, proj)
    pins = cutting_pins(g, crossings)
    z, rep = lower_below_ground(
        g,
        z_main,
        targets=targets,
        interior=interior,
        fixed=fixed,
        pins=pins,
        water_caps={v: UNDER_WATER_MAX_Z_M for v in wet},
        tunnel_crossings=tcs,
    )
    tunnels = []
    for w in sorted(lowered):
        line = g.lines[w]
        zs = [z[v] for v in line]
        under = [z[v] for v in line if v in wet]
        above = sorted({i for v in line for i in wet.get(v, [])})
        shallow = sorted({i for v in line for i in wet_all.get(v, [])} - set(above))
        tunnels.append(
            {
                "way": w,
                "name": g.ways[w].tags.get("name"),
                "highway": g.ways[w].tags.get("highway"),
                "layer": parse_layer(g.ways[w].tags.get("layer")),
                "target_z_m": tgt[w],
                "length_m": round(way_length(g.ways[w], coords), 1),
                "min_z_m": round(min(zs), 2),
                "max_z_m": round(max(zs), 2),
                "at_target_share": round(
                    sum(1 for v in line if z[v] <= tgt[w] + 0.01) / len(line), 2
                ),
                "under_water_vertices": len(under),
                "under_water_max_z_m": round(max(under), 2) if under else None,
                "under_water": [labels[i] for i in above],
                "under_shallow_water": [labels[i] for i in shallow],
            }
        )
    tunnel_cross = []
    for c in tcs:
        dz = crossing_dz(g, z, c, proj)
        tunnel_cross.append(
            {
                "upper_way": c.upper,
                "upper_name": g.ways[c.upper].tags.get("name"),
                "upper_is_rail": g.ways[c.upper].rail,
                "lower_way": c.lower,
                "lower_name": g.ways[c.lower].tags.get("name"),
                "lon": round(c.lon, 6),
                "lat": round(c.lat, 6),
                "separation_m": round(c.separation, 2),
                "dz_m": round(dz, 2),
                "short_of_separation": dz < c.separation - 0.02,
            }
        )
    trench_cross = []
    for c in crossings:
        if c.dropped is not None or c.trench is None:
            continue
        dz = crossing_dz(g, z, c, proj)
        rec = {
            "upper_way": c.upper,
            "lower_way": c.lower,
            "lon": round(c.lon, 6),
            "lat": round(c.lat, 6),
            "measured": "nearest_measured_m" not in c.trench,
            **c.trench,
            "cutting_z_m": round(_lower_z(z, c), 2),
            "dz_m": round(dz, 2),
        }
        trench_cross.append(rec)
    road_vs = {v for w, line in g.lines.items() if not g.ways[w].rail for v in line}
    in_b = {v for w in lowered for v in g.lines[w]}
    cut_ways = {r["way"] for r in getattr(g, "cutting_profiles", []) if r.get("used")}
    in_b |= {v for w in cut_ways for v in g.lines[w]}
    approach = [v for v in road_vs - in_b if z[v] < z_main[v] - 1e-6]
    approach_ways = sorted(
        {w for v in approach for w in ways_at[v] if w not in lowered and w not in cut_ways}
    )
    held = rep.pop("held_pins")
    rep.update(
        assumptions={
            "tunnel_depth_per_level_m": TUNNEL_DEPTH_PER_LEVEL_M,
            "water_surface_z_m": WATER_SURFACE_Z_M,
            "river_depth_m": RIVER_DEPTH_M,
            "river_cover_m": RIVER_COVER_M,
            "tunnel_roof_m": TUNNEL_ROOF_M,
            "under_water_max_z_m": UNDER_WATER_MAX_Z_M,
            "note": "estimated, not surveyed: tunnel depth = -10 m x level, ramped at the "
            "portals within the grade limits; under water, road surface <= water surface - "
            "assumed river depth - assumed cover - assumed tunnel roof. See data/SOURCES.md.",
        },
        tunnels=tunnels,
        tunnel_crossings=tunnel_cross,
        trench_crossings=trench_cross,
        cutting_profiles=getattr(g, "cutting_profiles", []),
        cutting_ways=sorted(cut_ways),
        approach_vertices_lowered=len(approach),
        approach_ways_lowered=approach_ways,
        approach_max_depth_m=round(-min((z[v] for v in approach), default=0.0), 2),
        under_water_vertices=len(wet),
        water_caps_unmet=[
            {"lon": round(g.lon[v], 6), "lat": round(g.lat[v], 6), "z_m": round(z[v], 2)}
            for v in sorted(wet)
            if z[v] > UNDER_WATER_MAX_Z_M + 0.01
        ],
        portal_vertices_fixed=len(portals),
        water_areas=len(water),
    )
    return z, {**rep, "held_pins": held}


# --- io ---------------------------------------------------------------------------------------


def read_extract(path: Path) -> tuple[list[Way], dict[int, tuple[float, float]]]:
    import osmium  # lazy: unit tests don't need the C extension

    coords: dict[int, tuple[float, float]] = {}
    ways: list[Way] = []
    for obj in osmium.FileProcessor(str(path)):
        if obj.is_node():
            coords[obj.id] = (obj.location.lon, obj.location.lat)
        elif obj.is_way():
            ways.append(Way(obj.id, [n.ref for n in obj.nodes], dict(obj.tags)))
    return ways, coords


def read_raw_context(
    pbf: Path, road_ids: set[int], bbox: BBox = SIM_EXTRACT_BBOX
) -> tuple[
    dict[int, dict[str, str]],
    list[Way],
    dict[int, tuple[float, float]],
    list[list[tuple[float, float]]],
]:
    """One pass over the regional PBF: the clearance tags (MAXHEIGHT_KEYS) of the extract's
    roads (the extract keeps only the tags netconvert needs), every railway (RAIL_TYPES) with
    a node in `bbox`, clipped like the roads (first to last node inside), and water: waterway
    lines (WATER_TAGS) and closed `natural=water` / `water=*` ways touching `bbox` (rule 7)."""
    import osmium

    from pipeline.roads import clip_span

    clearance: dict[int, dict[str, str]] = {}
    rails: list[Way] = []
    rcoords: dict[int, tuple[float, float]] = {}
    water: list[list[tuple[float, float]]] = []
    fp = (
        osmium.FileProcessor(str(pbf))
        .with_locations()
        .with_filter(osmium.filter.KeyFilter("highway", "railway", "waterway", "natural", "water"))
    )
    for obj in fp:
        if not obj.is_way():
            continue
        wt = obj.tags.get("waterway")
        if wt in WATER_TAGS or obj.tags.get("natural") == "water" or "water" in obj.tags:
            pts = [(n.lon, n.lat) for n in obj.nodes if n.location.valid()]
            if any(
                bbox.west <= lon <= bbox.east and bbox.south <= lat <= bbox.north
                for lon, lat in pts
            ):
                water.append(pts)
            continue
        if obj.id in road_ids:
            t = {k: obj.tags[k] for k in MAXHEIGHT_KEYS if k in obj.tags}
            if t:
                clearance[obj.id] = t
            continue
        rt = obj.tags.get("railway")
        if rt not in RAIL_TYPES or obj.tags.get("area") == "yes":
            continue
        nodes = [(n.ref, n.lon, n.lat) for n in obj.nodes if n.location.valid()]
        span = clip_span(nodes, bbox)
        if not span:
            continue
        keep = ("railway", "layer", "bridge", "tunnel", "name")
        rails.append(
            Way(
                obj.id,
                [r for r, _, _ in span],
                {k: obj.tags[k] for k in keep if k in obj.tags},
                True,
            )
        )
        for r, lon, lat in span:
            rcoords[r] = (lon, lat)
    return clearance, rails, rcoords, water


WATER_AREA_WATERWAYS = frozenset({"riverbank", "dock", "canal"})
# The river-bed rule (UNDER_WATER_MAX_Z_M) is for rivers, docks, harbours and lakes: water areas
# with one of these `water` / `waterway` values, or untagged `natural=water` of at least
# RIVER_BED_MIN_AREA_M2. Ponds and fountains (e.g. the Southbank promenade pond, way 991043364,
# over service tunnel 23909994) are a metre deep, not a river: a tunnel under one keeps its
# normal depth; it is listed in the report (`under_shallow_water`).
RIVER_BED_WATER = frozenset({"river", "canal", "harbour", "lake", "reservoir", "lagoon", "oxbow"})
RIVER_BED_MIN_AREA_M2 = 10_000.0


def read_tunnel_context(
    pbf: Path, tunnel_ends: set[int], road_ids: set[int], bbox: BBox = TUNNEL_EXTRACT_BBOX
) -> tuple[list[Any], set[int]]:
    """One pass over the regional PBF for Stage B:
    water areas touching `bbox` (ways and multipolygon relations, e.g. the Yarra, relation
    954522: `natural=water`, any `water=*`, `waterway=riverbank / dock / canal`; not culverts or
    covered water) as (outer ring, inner rings, label) in lon/lat; and the `tunnel_ends` (OSM
    node ids)
    used by a drivable road that is not a lowered tunnel and not in the extract (`road_ids`):
    tunnel portals whose outside road was clipped away, e.g. the Domain tunnel's east portal."""
    import osmium

    from pipeline.roads import DRIVABLE_HIGHWAYS

    water: list[Any] = []
    portals: set[int] = set()
    fp = (
        osmium.FileProcessor(str(pbf))
        .with_areas()
        .with_filter(osmium.filter.KeyFilter("highway", "natural", "water", "waterway"))
    )
    for obj in fp:
        if obj.is_way() and obj.id not in road_ids:
            t = dict(obj.tags)
            if t.get("highway") in DRIVABLE_HIGHWAYS and tunnel_target(t) is None:
                portals |= {n.ref for n in obj.nodes if n.ref in tunnel_ends}
            continue
        if not obj.is_area():
            continue
        t = dict(obj.tags)
        if "tunnel" in t or t.get("covered") == "yes":
            continue
        if not (
            t.get("natural") == "water" or "water" in t or t.get("waterway") in WATER_AREA_WATERWAYS
        ):
            continue
        for outer in obj.outer_rings():
            ring = [(n.lon, n.lat) for n in outer]
            if not any(
                bbox.west <= lon <= bbox.east and bbox.south <= lat <= bbox.north
                for lon, lat in ring
            ):
                continue
            inners = [[(n.lon, n.lat) for n in r] for r in obj.inner_rings(outer)]
            kind = "relation" if not obj.from_way() else "way"
            label = {
                "osm": f"{kind}/{obj.orig_id()}",
                **{k: t[k] for k in ("name", "natural", "water", "waterway") if k in t},
            }
            water.append((ring, inners, label))
    return water, portals


def is_measured_way(zs: list[float], held: list[bool]) -> bool:
    """Orchestrator decision 2026-10-26: a way is `measured` only if at least half of its raised
    vertices (z > 0; Stage B: or lowered, z < 0) are held by measurements (an anchor at its
    measured height, a binding measured requirement, or a measured cutting pin)."""
    raised = [h for z, h in zip(zs, held, strict=True) if z != 0]
    return bool(raised) and 2 * sum(raised) >= len(raised)


def z_source(zs: list[float], measured: bool) -> str:
    if measured:
        return "measured"
    return "estimated" if any(v != 0 for v in zs) else "ground"


def to_features(
    ways: list[Way], g: Graph, z: list[float], measured: set[int]
) -> list[dict[str, Any]]:
    feats = []
    for way in ways:
        line = g.lines[way.id]
        coords = [
            [round(g.lon[v], COORD_DECIMALS), round(g.lat[v], COORD_DECIMALS), z[v]] for v in line
        ]
        props = {
            "way_id": way.id,
            "highway": way.tags.get("highway"),
            "layer": parse_layer(way.tags.get("layer")),
            "bridge": is_bridge(way.tags),
            "tunnel": is_tunnel(way.tags),
            "name": way.tags.get("name"),
            "z_source": z_source(
                [z[v] for v in line],
                is_measured_way([z[v] for v in line], [v in measured for v in line]),
            ),
        }
        feats.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": props,
            }
        )
    return feats


def build_model(
    ways: list[Way],
    coords: dict[int, tuple[float, float]],
    rails: list[Way] | None = None,
    rail_coords: dict[int, tuple[float, float]] | None = None,
    measure: Any = None,
    water: list[list[tuple[float, float]]] | None = None,
    tunnel_water: list[Any] | None = None,
    portal_nodes: set[int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Features (roads only, sorted by way id) and a report of how the heights were set.

    `ways` carry their clearance tags (MAXHEIGHT_KEYS) in `tags`; `rails` are railways
    (rail=True), internal only. `measure(g, crossings)` (the point cloud, see
    pipeline.deck_pointcloud.measure_all) may replace crossing separations with measured ones
    (source "pointcloud") and returns ({vertex: measured open-deck height}, report records).
    `water` = waterway lines / water rings (lon, lat), for rule 7 (no floor over water).
    Stage B (`stage_b`): `tunnel_water` = water areas ((outer, [inner, ...]) rings or closed
    rings, lon/lat) that lowered tunnels must pass under below the assumed river bed;
    `portal_nodes` = OSM node ids of tunnel ends whose outside road is not in the extract (held
    at their Stage A height). `measure` may set `g.cutting_profiles` (measured cutting depths).
    """
    roads = sorted(ways, key=lambda w: w.id)
    rails = sorted(rails or [], key=lambda w: w.id)
    allc = dict(coords)
    allc.update(rail_coords or {})
    g = build_graph(roads + rails, allc)
    pts = [allc[n] for w in roads for n in w.nodes] or [(0.0, 0.0)]
    proj = _Proj(sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
    crossings = build_crossings(g, allc, proj)
    open_decks: dict[int, float] = {}
    records: list[dict[str, Any]] = []
    if measure is not None:
        open_decks, records = measure(g, crossings)
        offset_cutting_crossings(g, crossings)
    from shapely.geometry import LineString, Polygon

    water_geoms = []
    for ring in water or []:
        xy = [proj.xy(*p) for p in ring]
        if len(xy) >= 4 and xy[0] == xy[-1]:
            water_geoms.append(Polygon(xy))
        elif len(xy) >= 2:
            water_geoms.append(LineString(xy))
    rivers = river_bridge_ways(g, water_geoms, proj) if water_geoms else set()
    dropped_pins: list[dict[str, Any]] = []
    dropped_profiles: list[dict[str, Any]] = []
    trenches = mark_trenches(g, crossings, proj)
    open_rec = {r["upper_way"]: r for r in records if r["kind"] in ("open_deck", "deck_profile")}
    service_ramps = {w for w, r in open_rec.items() if g.ways[w].tags.get("highway") == "service"}
    river_info: dict[str, Any] = {}
    g.soft_deck = {}  # type: ignore[attr-defined]
    for w, rec in open_rec.items():
        # a deck with crossing pins is profiled away from them only where it is over water
        # (e.g. the south half of CityLink 207128345); elsewhere a street-level profile could
        # disagree with the crossing pins' local references (La Trobe St over the rail yards)
        if rec.get("deck_has_crossing_pins") and rec.get("used") and w not in rivers:
            rec.update(used=False, reason="deck with crossing pins: profile used only over water")

    def rebuild_profiles() -> None:
        """Profile pins from the profiles still used (a shared end vertex keeps the other's
        value). Only when the measurement came with records (the point-cloud build)."""
        if not open_rec:
            return
        open_decks.clear()
        for w, rec in open_rec.items():
            if rec.get("used"):
                for v, h in zip(g.lines[w], rec["heights_m"], strict=True):
                    if h is not None:
                        open_decks[v] = max(open_decks.get(v, 0.0), h)

    rebuild_profiles()

    def set_river_and_ramp_rules() -> None:
        """River-bridge base height = median of the measured river-bridge profiles (George
        2026-10-26); measured service ramps may be as steep as SERVICE_RAMP_GRADE."""
        import statistics

        river_low = sorted(w for w in rivers if effective_layer(g.ways[w].tags) <= 1)
        # the median of the measured STREET river bridges (Princes, Queens, Spencer St, ...), not
        # freeway-class viaducts that happen to cross a creek
        meas = [
            open_rec[w]["max_m"]
            for w in river_low
            if w in open_rec and open_rec[w].get("used") and road_class(g.ways[w].tags) == "other"
        ]
        h = statistics.median(meas) if len(meas) >= RIVER_DECK_MIN_N else OPEN_DECK_M
        river_info.update(
            river_deck_m=round(h, 2), river_deck_n=len(meas), river_bridge_ways=river_low
        )
        g.deck_base = {v: h for w in river_low for v in g.lines[w]}  # type: ignore[attr-defined]
        for w in service_ramps:
            used = open_rec[w].get("used")
            g.grade[w] = SERVICE_RAMP_GRADE if used else max(g.grade[w], MAX_GRADE)

    chain_of: dict[int, list[int]] = {}
    for seq in deck_chains(g):
        for v in seq:
            chain_of[v] = seq
    for _ in range(PIN_ROUNDS):
        set_river_and_ramp_rules()
        calibration = calibrate(g, crossings)
        resolve_grades(g, crossings, allc)
        caps, cap_info = measured_caps(g, crossings, open_decks)
        anchored = set(cap_info["pinned"])
        z, info = compute_heights(g, crossings, allc, open_decks, caps=caps, anchored=anchored)
        # a point-cloud separation that the solution cannot hold within PIN_TOL_M contradicts
        # the other measurements around it under the grade limits (each is relative to its own
        # local ground, and the ground slopes; the model's ground is flat): it is not used
        bad = []
        profiles_off: set[int] = set()
        for c in crossings:
            if c.dropped is not None or not c.measured:
                continue
            dz = crossing_dz(g, info["z_raw"], c, proj)
            if c.pinned and (dz > c.separation + PIN_TOL_M or dz < c.separation - PIN_UNDER_TOL_M):
                bad.append((c, dz))
            elif c.source == "maxheight" and dz < c.separation - PIN_UNDER_TOL_M:
                # a 2018 open-deck profile on the same deck holds it below a signed clearance
                # of today's road beneath: the deck was rebuilt after the capture; the sign wins
                # (a profile on the same deck chain that never reaches the clearance, or is
                # below it right at this crossing)
                need = _lower_z(info["z_raw"], c) + c.separation - PIN_TOL_M
                vs = set(chain_of.get(c.up_a, [c.up_a]))
                for w, rec in open_rec.items():
                    if not rec.get("used") or not vs & set(g.lines[w]):
                        continue
                    here = [open_decks[v] for v in (c.up_a, c.up_b) if v in open_decks]
                    if rec.get("max_m", 0.0) < need or (here and min(here) < need):
                        profiles_off.add(w)
        unpinned = False
        for w, rec in open_rec.items():
            # profile vertices the solution cannot hold (another measurement caps them lower)
            # are unpinned one by one; a profile with none left is not used
            if not rec.get("used") or w in profiles_off:
                continue
            hs = rec["heights_m"]
            off = [
                i
                for i, (v, h) in enumerate(zip(g.lines[w], hs, strict=True))
                if h is not None and info["z_raw"][v] < h - PIN_UNDER_TOL_M
            ]
            if not off:
                continue
            line = g.lines[w]
            for i in off:  # demoted, not discarded: still a lower bound at its measured value
                g.soft_deck[line[i]] = max(g.soft_deck.get(line[i], 0.0), hs[i])  # type: ignore[attr-defined]
            if len(off) == sum(h is not None for h in hs):
                profiles_off.add(w)
                rec["contradicted"] = True
            else:
                for i in off:
                    hs[i] = None
                rec["kind"] = "deck_profile"  # held at some vertices only
                rec["vertices_unpinned"] = rec.get("vertices_unpinned", 0) + len(off)
                unpinned = True
        if not bad and not profiles_off and not unpinned:
            break
        for w in sorted(profiles_off):
            rec = open_rec[w]
            rec.update(
                used=False,
                reason="contradicts neighbouring measurements beyond the grade limits"
                if rec.pop("contradicted", False)
                else "below a signed clearance on the same deck (rebuilt after 2018?)",
            )
            dropped_profiles.append({"upper": w, "max_m": rec.get("max_m")})
        if profiles_off or unpinned:
            rebuild_profiles()
        for c, dz in bad:
            rec = c.pointcloud or {}
            rec.update(
                used=False,
                reason="contradicts neighbouring measurements beyond the grade limits; "
                "kept as a lower bound",
                model_separation_m=round(dz, 2),
            )
            dropped_pins.append(
                {
                    "upper": c.upper,
                    "lower": c.lower,
                    "separation_m": round(c.separation + c.lower_depth, 2),
                    "model_m": round(dz + c.lower_depth, 2),
                }
            )
            # demoted, not dropped: its measured separation stays as a lower bound (capped by
            # the anchors), so `maxheight` never replaces a point-cloud value on the same crossing
            c.source = "pointcloud_soft"
    candidates, excluded = floor_candidates(
        g, crossings, open_decks, info["z_raw"], water_geoms, proj
    )
    floor_sep = class_separation(calibration, "freeway")
    z, info, floors = apply_floors(
        g, crossings, allc, open_decks, candidates, floor_sep, proj, caps, anchored
    )
    floors["not_floored"] = excluded
    floors["separation_per_layer_m"] = floor_sep
    # the floors are estimates, so measured caps can hold them lower than requested
    floors["effective_max_m"] = {
        str(w): round(max(info["z_raw"][v] for v in g.lines[w]), 2) for w in candidates
    }
    zr = info["z_raw"]
    info_z_raw = zr
    measured = measured_vertices(zr, crossings, open_decks)
    # what measured precedence did: estimated crossings bent below their separation, and any
    # measured anchor that still is not at its measurement (should be none)
    bent = [
        {
            "upper": c.upper,
            "lower": c.lower,
            "separation_m": round(c.separation, 2),
            "dz_m": round(crossing_dz(g, zr, c, proj), 2),
            "lon": round(c.lon, 6),
            "lat": round(c.lat, 6),
        }
        for c in crossings
        if c.dropped is None
        and not c.measured
        and crossing_dz(g, zr, c, proj) < c.separation - 0.02
    ]
    cap_info.pop("z_a")
    pinned = cap_info.pop("pinned")  # grounded anchors and measured spans: must not move
    moved = sum(1 for v, h in pinned.items() if abs(zr[v] - h) > 0.02)
    first_way: dict[int, tuple[int, int]] = {}
    for wid in sorted(g.lines):
        if g.ways[wid].rail:
            continue
        for i, v in enumerate(g.lines[wid]):
            first_way.setdefault(v, (wid, i))
    info["anchors"] = [
        {
            "way": first_way[v][0],
            "vertex_index": first_way[v][1],
            "measured_z_m": round(h, 2),
            "final_z_m": round(zr[v], 2),
        }
        for v, h in sorted(pinned.items())
        if v in first_way
    ]
    info["pinned_vertices"] = pinned
    rail_anchor_vs = sum(1 for v in pinned if v not in first_way)
    cap_info.update(
        anchors=len(info["anchors"]),
        pinned_vertices=len(info["anchors"]),
        rail_anchor_vertices=rail_anchor_vs,
        anchors_note="road vertices held by point-cloud measurements (pins and profiles); "
        "railway vertices are internal and counted apart",
    )
    cap_info.update(
        estimated_crossings_bent=bent,
        anchors_moved=moved,
        pins_dropped=dropped_pins,
        open_deck_profiles_dropped=dropped_profiles,
        profile_vertices_soft=len(g.soft_deck),  # type: ignore[attr-defined]
    )
    info["trench_crossings_stage_a"] = trenches
    info["precedence"] = cap_info
    info["calibration"] = calibration
    floors["river_bridges"] = river_info
    info["layer_floors"] = floors
    # held by measurements (for `z_source` only): a binding measured requirement, an anchor at
    # its measured height, a deck vertex at the cap the measurements put on it, and every deck
    # vertex between two such vertices on the same deck chain (determined by the measurements on
    # both sides); ramps beyond the outermost measurement are not held
    on_deck_v = deck_vertices(g)
    held = (
        set(measured)
        | {v for v, h in info["pinned_vertices"].items() if abs(info_z_raw[v] - h) <= 0.02}
        | {
            v
            for v in on_deck_v
            if info_z_raw[v] > 0 and caps[v] < math.inf and info_z_raw[v] >= caps[v] - 0.02
        }
    )
    for seq in deck_chains(g):
        idx = [i for i, v in enumerate(seq) if v in held]
        if len(idx) >= 2:
            held.update(seq[idx[0] : idx[-1] + 1])
    # Stage B: tunnels and the CityLink cutting below street level (decks and Stage A anchors
    # keep their heights; see `lower_below_ground`)
    zb, below = stage_b(g, info_z_raw, allc, proj, crossings, tunnel_water, portal_nodes)
    held_pins = below.pop("held_pins")
    held |= {v for v, h in held_pins.items() if abs(zb[v] - h) <= 0.02}
    z = []
    for v in zb:
        r = round(v, Z_DECIMALS)
        z.append(0.0 if abs(r) < 10**-Z_DECIMALS else r)
    info["below_ground"] = below
    # crossings over a cutting that still do not clear the check's 4 m (none expected once the
    # cutting is lowered)
    info["pending_trench_depth"] = [
        t for t in below["trench_crossings"] if t["dz_m"] < CHECK_CLEARANCE_M
    ]
    feats = to_features(roads, g, z, held)
    info["pointcloud_records"] = records
    info["crossings"] = crossings
    info["graph"] = g
    info["z"] = z
    return feats, info


def build_features(
    ways: list[Way],
    coords: dict[int, tuple[float, float]],
    rails: list[Way] | None = None,
    rail_coords: dict[int, tuple[float, float]] | None = None,
) -> list[dict[str, Any]]:
    return build_model(ways, coords, rails, rail_coords)[0]


def write_geojson(features: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fc = {"type": "FeatureCollection", "features": features}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(fc, separators=(",", ":"), ensure_ascii=False))
    tmp.replace(path)


# --- validation -------------------------------------------------------------------------------


def validate_feature_collection(
    fc: dict, bbox: BBox = SIM_EXTRACT_BBOX, tunnel_bbox: BBox | None = TUNNEL_EXTRACT_BBOX
) -> list[str]:
    """Contract check; returns a list of problems (empty = valid). Motorway tunnel ways may reach
    `tunnel_bbox` (the extended CityLink tunnel clip, pipeline/config.py)."""
    from pipeline.roads import EXTENDED_CLIP_HIGHWAYS

    errs: list[str] = []
    outer = bbox
    if fc.get("type") != "FeatureCollection" or not isinstance(fc.get("features"), list):
        return ["not a FeatureCollection"]
    tol = BBOX_TOLERANCE_DEG
    seen: set[int] = set()
    for i, f in enumerate(fc["features"]):
        where = f"feature {i}"
        geom, props = f.get("geometry") or {}, f.get("properties") or {}
        if f.get("type") != "Feature":
            errs.append(f"{where}: type is not Feature")
        if geom.get("type") != "LineString":
            errs.append(f"{where}: geometry is not LineString")
            continue
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            errs.append(f"{where}: fewer than 2 coordinates")
        bbox = outer
        if (
            tunnel_bbox is not None
            and props.get("tunnel") is True
            and props.get("highway") in EXTENDED_CLIP_HIGHWAYS
        ):
            bbox = tunnel_bbox
        for c in coords:
            if len(c) != 3 or not all(isinstance(x, int | float) for x in c):
                errs.append(f"{where}: coordinate {c} is not [lon, lat, z]")
                break
            lon, lat, z = c
            if not (bbox.west - tol <= lon <= bbox.east + tol):
                errs.append(f"{where}: lon {lon} outside bbox")
                break
            if not (bbox.south - tol <= lat <= bbox.north + tol):
                errs.append(f"{where}: lat {lat} outside bbox")
                break
            if round(lon, COORD_DECIMALS) != lon or round(lat, COORD_DECIMALS) != lat:
                errs.append(f"{where}: lon/lat not rounded to {COORD_DECIMALS} decimals")
                break
            if round(z, Z_DECIMALS) != z:
                errs.append(f"{where}: z not rounded to {Z_DECIMALS} decimals")
                break
        if set(props) != set(PROPERTY_KEYS):
            errs.append(f"{where}: properties {sorted(props)} != {list(PROPERTY_KEYS)}")
            continue
        wid = props["way_id"]
        if not isinstance(wid, int) or isinstance(wid, bool):
            errs.append(f"{where}: way_id not int")
        elif wid in seen:
            errs.append(f"{where}: duplicate way_id {wid}")
        else:
            seen.add(wid)
        if not isinstance(props["highway"], str):
            errs.append(f"{where}: highway not str")
        if not isinstance(props["layer"], int) or isinstance(props["layer"], bool):
            errs.append(f"{where}: layer not int")
        for k in ("bridge", "tunnel"):
            if not isinstance(props[k], bool):
                errs.append(f"{where}: {k} not bool")
        if props["name"] is not None and not isinstance(props["name"], str):
            errs.append(f"{where}: name not str/null")
        zsrc = props["z_source"]
        if zsrc not in Z_SOURCES:
            errs.append(f"{where}: z_source {zsrc!r} not in {Z_SOURCES}")
        elif coords and all(len(c) == 3 for c in coords):
            flat = all(c[2] == 0 for c in coords)
            if (zsrc == "ground") != flat:
                errs.append(f"{where}: z_source {zsrc!r} but all z = 0 is {flat}")
    return errs


def smoothness(fc: dict) -> dict[str, Any]:
    """Max step, max grade, shared-position z mismatches, share of vertices at z = 0."""
    max_step, max_grade, max_spacing = 0.0, 0.0, 0.0
    n_vertices = n_zero = 0
    zs_at: dict[tuple[float, float], set[float]] = defaultdict(set)
    for f in fc["features"]:
        c = f["geometry"]["coordinates"]
        # Every position is checked, so a shared node in mid-line is caught as well as line ends.
        for lon, lat, z in c:
            n_vertices += 1
            n_zero += z == 0
            zs_at[(lon, lat)].add(z)
        for (lo1, la1, z1), (lo2, la2, z2) in zip(c, c[1:], strict=False):
            d = metres(lo1, la1, lo2, la2)
            dz = abs(z2 - z1)
            max_step = max(max_step, dz)
            max_spacing = max(max_spacing, d)
            if d >= 5.0:  # below 5 m, rounding (z 0.01 m, lon/lat 6 dp) dominates the grade
                max_grade = max(max_grade, dz / d)
    mismatches = sum(1 for zs in zs_at.values() if max(zs) - min(zs) > 0.011)
    return {
        "max_step_m": round(max_step, 3),
        "max_grade": round(max_grade, 4),
        "max_spacing_m": round(max_spacing, 2),
        "shared_position_mismatches": mismatches,
        "vertices": n_vertices,
        "pct_vertices_z0": round(100 * n_zero / max(n_vertices, 1), 2),
    }


def summarise(features: list[dict], info: dict[str, Any] | None = None) -> str:
    fc = {"type": "FeatureCollection", "features": features}
    s = smoothness(fc)
    bridges = Counter(f["properties"]["layer"] for f in features if f["properties"]["bridge"])
    tunnels = sum(1 for f in features if f["properties"]["tunnel"])
    top = sorted(
        (
            (c[2], f["properties"]["name"], f["properties"]["way_id"], c[0], c[1])
            for f in features
            for c in f["geometry"]["coordinates"]
        ),
        key=lambda t: t[0],
        reverse=True,
    )
    lines = [f"features: {len(features)}", f"smoothness: {s}"]
    lines.append("z_source: " + str(dict(Counter(f["properties"]["z_source"] for f in features))))
    lines.append("bridges by layer: " + ", ".join(f"{k}: {v}" for k, v in sorted(bridges.items())))
    lines.append(f"tunnel ways: {tunnels}")
    if info is not None:
        cs: list[Crossing] = info["crossings"]
        g: Graph = info["graph"]
        z: list[float] = info["z"]
        road_up = [c for c in cs if not g.ways[c.upper].rail]
        lines.append(
            f"rule-1 crossings: {len(cs)} ({len(road_up)} with a road on top; "
            f"{sum(1 for c in road_up if g.ways[c.lower].rail)} of those over a railway); "
            f"separation source (road on top): "
            f"{dict(Counter(c.source for c in road_up))}; "
            f"dropped {sum(1 for c in cs if c.dropped)}; fixed point in {info['rounds']} rounds"
        )
        for src in ("pointcloud", "maxheight"):
            meas = [c for c in road_up if c.source == src and c.dropped is None]
            binding = sum(
                1
                for c in meas
                if any(
                    abs(z[v] - (_lower_z(z, c) + c.separation)) <= 0.02 for v in (c.up_a, c.up_b)
                )
            )
            lines.append(
                f"{src} anchors binding (deck exactly at the measurement): {binding}/{len(meas)}"
            )
        recs = info.get("pointcloud_records") or []
        od = [r for r in recs if r["kind"] == "open_deck"]
        lines.append(
            f"open decks (bridge ways on top of nothing): {len(od)}, point-cloud profile used "
            f"for {sum(1 for r in od if r.get('used'))}"
        )
        for c in cs:
            if c.dropped:
                lines.append(
                    f"  DROPPED {c.upper} over {c.lower} at ({c.lon:.6f}, {c.lat:.6f}): {c.dropped}"
                )
        lines.append(f"short links steepened to {SHORT_LINK_GRADE:.0%}: {info['steepened_links']}")
        if "calibration" in info:
            lines.append(f"calibration: {info['calibration']}")
            pr = info["precedence"]
            bent_dz = [b["dz_m"] for b in pr["estimated_crossings_bent"]]
            lines.append(
                f"measured precedence: {pr['anchors']} anchor vertices "
                f"({pr['pinned_vertices']} pinned incl. measured spans); "
                f"{len(pr['estimated_crossings_bent'])} estimated crossings bent under measured "
                f"decks (min dz {min(bent_dz, default=None)}); "
                f"anchors moved off their measurement: {pr['anchors_moved']}; "
                f"vertices bounded below 0: {pr['vertices_bounded_below_zero']}"
            )
            lf = info["layer_floors"]
            lines.append(
                f"layer floors: full {lf['full']}; reduced {lf['reduced']}; "
                f"dropped {lf['dropped']}; not floored: {lf['not_floored']}"
            )
        if "below_ground" in info:
            bg = info["below_ground"]
            lines.append(
                f"Stage B: {len(bg['tunnels'])} tunnel ways lowered; cutting ways "
                f"{bg['cutting_ways']}; pins held {bg['pins_held']} / {bg['pins']} "
                f"(demoted {len(bg['pins_demoted'])}); approach vertices lowered "
                f"{bg['approach_vertices_lowered']} (max {bg['approach_max_depth_m']} m) on ways "
                f"{bg['approach_ways_lowered']}; under-water tunnel vertices "
                f"{bg['under_water_vertices']} (road surface <= {UNDER_WATER_MAX_Z_M} m)"
            )
            short = [t for t in bg["tunnel_crossings"] if t["short_of_separation"]]
            lines.append(
                f"tunnel crossings: {len(bg['tunnel_crossings'])}, short of their separation: "
                f"{[(t['upper_way'], t['lower_way'], t['dz_m']) for t in short]}"
            )
            for t in bg["tunnels"]:
                if t["length_m"] >= 150 or t["under_water_vertices"]:
                    lines.append(
                        f"  tunnel {t['way']} {t['name']} layer {t['layer']}: z "
                        f"{t['min_z_m']}..{t['max_z_m']} m (target {t['target_z_m']}), "
                        f"{t['length_m']} m, under water {t['under_water_vertices']} vertices "
                        f"(max z there {t['under_water_max_z_m']})"
                    )
            for t in bg["trench_crossings"]:
                lines.append(
                    f"  over the cutting: {t['upper_way']} over {t['lower_way']}: clearance "
                    f"{t['dz_m']} m (cutting at {t['cutting_z_m']} m, measured trench depth "
                    f"{t['trench_depth_m']} m)"
                )
    seen: set[int] = set()
    lines.append("highest points (one per way):")
    for z, name, wid, lon, lat in top:
        if wid in seen:
            continue
        seen.add(wid)
        lines.append(f"  {z:6.2f} m  way {wid}  {name}  ({lon}, {lat})")
        if len(seen) == 10:
            break
    return "\n".join(lines)


def build(src: Path = IN_PATH, out: Path = OUT_PATH, pbf: Path = RAW_PBF) -> str:
    if not src.exists():
        raise FileNotFoundError(f"{src} missing; run `uv run python -m pipeline.roads` first")
    if not pbf.exists():
        raise FileNotFoundError(f"{pbf} missing; see data/SOURCES.md (BBBike Melbourne extract)")
    ways, coords = read_extract(src)
    clearance, rails, rail_coords, water = read_raw_context(pbf, {w.id for w in ways})
    for w in ways:
        w.tags.update(clearance.get(w.id, {}))
    ends = {n for w in ways if tunnel_target(w.tags) is not None for n in (w.nodes[0], w.nodes[-1])}
    tunnel_water, portal_nodes = read_tunnel_context(pbf, ends, {w.id for w in ways})
    from pipeline import deck_pointcloud

    tiles = deck_pointcloud.sim_tiles()
    features, info = build_model(
        ways,
        coords,
        rails,
        rail_coords,
        lambda g, cs: deck_pointcloud.measure_all(g, cs, tiles),
        water,
        tunnel_water,
        portal_nodes,
    )
    fc = {"type": "FeatureCollection", "features": features}
    errs = validate_feature_collection(fc)
    if errs:
        raise ValueError("roads_3d contract violations:\n" + "\n".join(errs[:20]))
    s = smoothness(fc)
    if s["max_step_m"] > MAX_STEP_M or s["shared_position_mismatches"]:
        raise ValueError(f"roads_3d smoothness check failed: {s}")
    write_geojson(features, out)
    from pipeline.checks.grade_separation import repo_key, sha256_file

    deck_pointcloud.write_report(
        info["pointcloud_records"],
        tiles,
        info["calibration"],
        info["layer_floors"],
        info["precedence"],
        info["pending_trench_depth"],
        info["anchors"],
        {repo_key(p_): sha256_file(p_) for p_ in (out, src, pbf)},
        info["below_ground"],
    )
    head = (
        f"railways read (internal only): {len(rails)}; roads with clearance tags: "
        f"{len(clearance)}; point-cloud tiles read: {len(tiles)}"
    )
    size = f"\nwrote {out} ({out.stat().st_size / 1e6:.2f} MB)\nwrote {deck_pointcloud.REPORT_PATH}"
    return head + "\n" + summarise(features, info) + size


def main() -> int:
    print(build())
    return 0


if __name__ == "__main__":
    sys.exit(main())
