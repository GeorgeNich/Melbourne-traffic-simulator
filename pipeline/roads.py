"""Build data/processed/osm_sim_area.osm.gz (and, by default, roads_3d.geojson) from data/raw/.

Usage:
    uv run python -m pipeline.roads              # extract + roads_3d.geojson (the one command)
    uv run python -m pipeline.roads --extract-only

Contract (CLAUDE.md): OSM XML (gzip) clipped to SIM_BBOX + SIM_BUFFER_M (`SIM_EXTRACT_BBOX`) with:
    - ways whose `highway` is usable by cars (DRIVABLE_HIGHWAYS), minus `area=yes` and ways where
      motor vehicles are not allowed (`motorcar` > `motor_vehicle` > `vehicle` > `access` = no);
    - every node those ways use, with node tags such as `highway=traffic_signals`;
    - `type=restriction` relations whose from/via/to members are all kept;
    - tags reduced to the keys netconvert and roads_3d need (`keep_tag`).

Clipping: a way that has at least one node inside the extract bbox is kept from its first to its
last inside node (so each way stays one connected piece with its original id). Ways that only pass
through the bbox without a node inside it are dropped (rare at this scale). Exception (Phase 2
Stage B, orchestrator 2026-10-27): CityLink motorway tunnel ways (`extended_tunnel_clip`:
`tunnel=yes`, `highway=motorway` / `motorway_link`) are clipped at TUNNEL_EXTRACT_BBOX, which
reaches BUILDINGS_BBOX's east edge, so the Burnley and Domain tunnels keep their Yarra crossings
(pipeline/config.py). The file's <bounds> stay SIM_EXTRACT_BBOX.

Bus-lane and conditional speed tags (`bus:lanes*`, `lanes:bus*`, `busway` and its side variants,
`maxspeed:conditional`) are kept exactly as tagged for sim-engineer (Stage C request).

No personal data: OSM user names, uids, changesets and timestamps are not written.

Source: OpenStreetMap via the BBBike Melbourne extract (ODbL 1.0, see data/SOURCES.md).
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.config import SIM_EXTRACT_BBOX, TUNNEL_EXTRACT_BBOX, BBox

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_PBF = REPO_ROOT / "data" / "raw" / "bbbike_melbourne.osm.pbf"
OUT_PATH = REPO_ROOT / "data" / "processed" / "osm_sim_area.osm.gz"

DRIVABLE_HIGHWAYS = frozenset(
    {
        "motorway",
        "trunk",
        "primary",
        "secondary",
        "tertiary",
        "motorway_link",
        "trunk_link",
        "primary_link",
        "secondary_link",
        "tertiary_link",
        "unclassified",
        "residential",
        "living_street",
        "service",
    }
)

# Most specific first: the first of these keys present on a way decides motor-vehicle access.
ACCESS_KEYS = ("motorcar", "motor_vehicle", "vehicle", "access")
NO_ACCESS_VALUES = frozenset({"no"})

KEEP_KEYS = frozenset(
    {
        "highway",
        "name",
        "ref",
        "oneway",
        "lanes",
        "maxspeed",
        "bridge",
        "tunnel",
        "layer",
        "junction",
        "access",
        "motor_vehicle",
        "motorcar",
        # bus lanes and conditional limits, kept exactly as tagged (sim-engineer, Stage C);
        # `lanes:bus*` is covered by the `lanes:` prefix
        "busway",
        "busway:left",
        "busway:right",
        "busway:both",
        "maxspeed:conditional",
        # relation tags, so netconvert can read turn restrictions
        "type",
        "restriction",
        "except",
    }
)
# `bus:lanes`, `bus:lanes:forward` / `:backward` / `:conditional` and their combinations
KEEP_PREFIXES = ("lanes:", "turn:lanes", "restriction:", "bus:lanes")

RESTRICTION_ROLES = ("from", "via", "to")


def keep_tag(key: str) -> bool:
    return key in KEEP_KEYS or key.startswith(KEEP_PREFIXES)


def filter_tags(tags: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in tags.items() if keep_tag(k)}


def motor_access_denied(tags: dict[str, str]) -> bool:
    for key in ACCESS_KEYS:
        if key in tags:
            return tags[key].strip().lower() in NO_ACCESS_VALUES
    return False


def is_drivable(tags: dict[str, str]) -> bool:
    """True for a `highway` way that cars may use (see module docstring)."""
    if tags.get("highway") not in DRIVABLE_HIGHWAYS:
        return False
    if tags.get("area") == "yes":
        return False
    return not motor_access_denied(tags)


EXTENDED_CLIP_HIGHWAYS = frozenset({"motorway", "motorway_link"})


def extended_tunnel_clip(tags: dict[str, str]) -> bool:
    """A CityLink-style motorway tunnel way, clipped at TUNNEL_EXTRACT_BBOX (module docstring)."""
    return tags.get("tunnel") == "yes" and tags.get("highway") in EXTENDED_CLIP_HIGHWAYS


def clip_box(tags: dict[str, str], bbox: BBox, tunnel_bbox: BBox | None) -> BBox:
    """The bbox a way with these tags is clipped at."""
    return tunnel_bbox if tunnel_bbox is not None and extended_tunnel_clip(tags) else bbox


def inside(bbox: BBox, lon: float, lat: float) -> bool:
    return bbox.west <= lon <= bbox.east and bbox.south <= lat <= bbox.north


def clip_span(nodes: list[tuple[int, float, float]], bbox: BBox) -> list[tuple[int, float, float]]:
    """Nodes from the first to the last one inside `bbox` ([] if fewer than two remain)."""
    idx = [i for i, (_, lon, lat) in enumerate(nodes) if inside(bbox, lon, lat)]
    if not idx:
        return []
    span = nodes[idx[0] : idx[-1] + 1]
    return span if len(span) >= 2 else []


@dataclass
class Extract:
    nodes: dict[int, tuple[float, float]] = field(default_factory=dict)  # id -> (lon, lat)
    node_tags: dict[int, dict[str, str]] = field(default_factory=dict)
    ways: dict[int, tuple[list[int], dict[str, str]]] = field(default_factory=dict)
    relations: dict[int, tuple[list[tuple[str, int, str]], dict[str, str]]] = field(
        default_factory=dict
    )
    stats: Counter = field(default_factory=Counter)


@dataclass
class RawData:
    """What one pass over the PBF collects, before selection."""

    node_tags: dict[int, dict[str, str]] = field(default_factory=dict)
    ways: list[tuple[int, list[tuple[int, float, float]], dict[str, str]]] = field(
        default_factory=list
    )
    restrictions: list[tuple[int, list[tuple[str, int, str]], dict[str, str]]] = field(
        default_factory=list
    )
    dropped_no_access: int = 0


def read_pbf(path: Path, bbox: BBox, tunnel_bbox: BBox | None = None) -> RawData:
    import osmium  # lazy: unit tests don't need the C extension

    # Loose box for node tags: a clipped way can keep a few nodes just outside `bbox`.
    margin = 0.01
    outer = bbox if tunnel_bbox is None else tunnel_bbox
    loose = BBox(
        min(bbox.west, outer.west) - margin,
        min(bbox.south, outer.south) - margin,
        max(bbox.east, outer.east) + margin,
        max(bbox.north, outer.north) + margin,
    )
    raw = RawData()
    fp = (
        osmium.FileProcessor(str(path))
        .with_locations()
        .with_filter(osmium.filter.KeyFilter("highway", "type"))
    )
    for obj in fp:
        if obj.is_node():
            loc = obj.location
            if loc.valid() and inside(loose, loc.lon, loc.lat):
                tags = filter_tags(dict(obj.tags))
                if tags:
                    raw.node_tags[obj.id] = tags
        elif obj.is_way():
            tags = dict(obj.tags)
            if tags.get("highway") not in DRIVABLE_HIGHWAYS:
                continue
            nodes = [(n.ref, n.lon, n.lat) for n in obj.nodes if n.location.valid()]
            box = clip_box(tags, bbox, tunnel_bbox)
            if not any(inside(box, lon, lat) for _, lon, lat in nodes):
                continue
            if not is_drivable(tags):
                raw.dropped_no_access += 1
                continue
            raw.ways.append((obj.id, nodes, tags))
        elif obj.is_relation():
            tags = dict(obj.tags)
            if tags.get("type") == "restriction":
                members = [(m.type, m.ref, m.role) for m in obj.members]
                raw.restrictions.append((obj.id, members, tags))
    return raw


def select(raw: RawData, bbox: BBox, tunnel_bbox: BBox | None = None) -> Extract:
    """Clip ways, collect their nodes, keep restrictions whose members all survive.

    With `tunnel_bbox`, ways passing `extended_tunnel_clip` are clipped at it instead of `bbox`;
    the stats count those ways and the nodes the wider clip adds beyond a `bbox` clip."""
    ex = Extract()
    ex.stats["ways_dropped_no_motor_access"] = raw.dropped_no_access
    for wid, nodes, tags in raw.ways:
        box = clip_box(tags, bbox, tunnel_bbox)
        span = clip_span(nodes, box)
        if not span:
            ex.stats["ways_dropped_too_short_after_clip"] += 1
            continue
        if box is not bbox:
            narrow = clip_span(nodes, bbox)
            ex.stats["extended_tunnel_clip_ways"] += 1
            ex.stats["extended_tunnel_clip_ways_added"] += not narrow
            ex.stats["extended_tunnel_clip_nodes_added"] += len(span) - len(narrow)
        for ref, lon, lat in span:
            ex.nodes[ref] = (lon, lat)
        ex.ways[wid] = ([ref for ref, _, _ in span], filter_tags(tags))
    ex.node_tags = {nid: t for nid, t in raw.node_tags.items() if nid in ex.nodes}

    way_nodes = {wid: set(refs) for wid, (refs, _) in ex.ways.items()}
    for rid, members, tags in raw.restrictions:
        if not _restriction_ok(members, way_nodes, ex.nodes):
            ex.stats["restrictions_dropped"] += 1
            continue
        kept = [(t, ref, role) for t, ref, role in members if role in RESTRICTION_ROLES]
        ex.relations[rid] = (kept, filter_tags(tags))
    return ex


def _restriction_ok(
    members: list[tuple[str, int, str]],
    way_nodes: dict[int, set[int]],
    nodes: dict[int, tuple[float, float]],
) -> bool:
    by_role: dict[str, list[tuple[str, int]]] = {r: [] for r in RESTRICTION_ROLES}
    for mtype, ref, role in members:
        if role in by_role:
            by_role[role].append((mtype, ref))
    if not by_role["from"] or not by_role["to"] or not by_role["via"]:
        return False
    for mtype, ref in by_role["from"] + by_role["to"]:
        if mtype != "w" or ref not in way_nodes:
            return False
    for mtype, ref in by_role["via"]:
        if mtype == "n":
            if ref not in nodes:
                return False
            # the clipped from/to ways must still touch the via node
            for _, wref in by_role["from"] + by_role["to"]:
                if ref not in way_nodes[wref]:
                    return False
        elif mtype == "w":
            if ref not in way_nodes:
                return False
        else:
            return False
    return True


def write_osm(ex: Extract, path: Path, bbox: BBox) -> None:
    import osmium
    from osmium.osm.mutable import Node, Relation, Way

    path.parent.mkdir(parents=True, exist_ok=True)
    header = osmium.io.Header()
    header.add_box(osmium.osm.Box(bbox.west, bbox.south, bbox.east, bbox.north))
    header.set("generator", "melbourne-traffic-twin pipeline.roads")
    tmp = path.with_suffix(".tmp.osm.gz")
    if tmp.exists():
        tmp.unlink()
    writer = osmium.SimpleWriter(str(tmp), header=header)
    try:
        for nid in sorted(ex.nodes):
            lon, lat = ex.nodes[nid]
            writer.add_node(
                Node(id=nid, version=1, location=(lon, lat), tags=ex.node_tags.get(nid, {}))
            )
        for wid in sorted(ex.ways):
            refs, tags = ex.ways[wid]
            writer.add_way(Way(id=wid, version=1, nodes=refs, tags=tags))
        for rid in sorted(ex.relations):
            members, tags = ex.relations[rid]
            writer.add_relation(Relation(id=rid, version=1, members=members, tags=tags))
    finally:
        writer.close()
    tmp.replace(path)


def summarise(ex: Extract) -> str:
    hw = Counter(tags.get("highway") for _, tags in ex.ways.values())
    signals = sum(1 for t in ex.node_tags.values() if t.get("highway") == "traffic_signals")
    lines = [
        f"ways: {len(ex.ways)}",
        f"nodes: {len(ex.nodes)}",
        f"restrictions: {len(ex.relations)}",
        f"traffic_signals nodes: {signals}",
        "highway: " + ", ".join(f"{k}={v}" for k, v in hw.most_common()),
    ]
    lines += [f"{k}: {v}" for k, v in sorted(ex.stats.items())]
    return "\n".join(lines)


def build_extract(
    pbf: Path = RAW_PBF,
    out: Path = OUT_PATH,
    bbox: BBox = SIM_EXTRACT_BBOX,
    tunnel_bbox: BBox | None = TUNNEL_EXTRACT_BBOX,
) -> str:
    if not pbf.exists():
        raise FileNotFoundError(f"{pbf} missing; see data/SOURCES.md (BBBike Melbourne extract)")
    ex = select(read_pbf(pbf, bbox, tunnel_bbox), bbox, tunnel_bbox)
    write_osm(ex, out, bbox)
    return summarise(ex)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--extract-only", action="store_true", help="skip roads_3d.geojson")
    args = ap.parse_args(argv)
    print(build_extract())
    print(f"wrote {OUT_PATH} ({OUT_PATH.stat().st_size / 1e6:.2f} MB)")
    if not args.extract_only:
        from pipeline import roads_3d

        print(roads_3d.build())
    return 0


if __name__ == "__main__":
    sys.exit(main())
