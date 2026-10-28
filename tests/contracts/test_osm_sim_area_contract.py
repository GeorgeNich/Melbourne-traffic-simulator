"""Contract test for data/processed/osm_sim_area.osm.gz (CLAUDE.md "Data contracts").

Written by qa-reviewer, independently of pipeline.roads: the extract is parsed with the standard
library (gzip + ElementTree), and only the extract bbox comes from pipeline/config.py (the single
source of truth for bboxes). Skips when the file is absent.

The clip, as built: a drivable way with at least two nodes inside SIM_EXTRACT_BBOX is kept from its
first to its last inside node, so every way starts and ends inside the bbox; interior nodes may
loop slightly outside it (seen: 3 nodes, at most 4.8 m out).

Phase 2 Stage B (orchestrator, 2026-10-27; data/SOURCES.md "Extended tunnel clip"): motorway tunnel
ways (`tunnel=yes`, `highway=motorway` / `motorway_link`) are clipped at TUNNEL_EXTRACT_BBOX
instead, which is SIM_EXTRACT_BBOX with its east edge moved to BUILDINGS_BBOX's east edge, so the
Burnley (8066124) and Domain (8066126) tunnels keep their Yarra crossings. Every other way keeps
the SIM_EXTRACT_BBOX clip, and `<bounds>` stay SIM_EXTRACT_BBOX.

Stage C request (sim-engineer): bus-lane and conditional speed tags (`bus:lanes*`, `lanes:bus*`,
`busway` and its `:left` / `:right` / `:both` variants, `maxspeed:conditional`) are kept exactly as
tagged.

The last group of tests compares the extract with the raw BBBike download in data/raw/ (read
only, local file, no network). They skip when the raw file or osmium is not available, e.g. in a
worktree without the data/raw symlink.
"""

from __future__ import annotations

import gzip
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from pipeline.config import BUILDINGS_BBOX, SIM_EXTRACT_BBOX, TUNNEL_EXTRACT_BBOX, BBox

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "data" / "processed" / "osm_sim_area.osm.gz"
RAW_PBF = ROOT / "data" / "raw" / "bbbike_melbourne.osm.pbf"

# Car-usable OSM highway values (CLAUDE.md: "drivable highways").
DRIVABLE = {
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
# Motor-vehicle access, most specific key first; the first one present decides.
ACCESS_KEYS = ("motorcar", "motor_vehicle", "vehicle", "access")
# Tags the contract says are kept where present ("bridge, tunnel, layer, *_link, lanes, maxspeed,
# oneway, turn:lanes"), plus the highway class itself.
REQUIRED_KEYS = (
    "highway",
    "bridge",
    "tunnel",
    "layer",
    "lanes",
    "maxspeed",
    "oneway",
    # Stage C request (kept exactly as tagged)
    "busway",
    "busway:left",
    "busway:right",
    "busway:both",
    "maxspeed:conditional",
)
REQUIRED_PREFIXES = ("turn:lanes", "lanes:", "bus:lanes")
# Everything allowed in the extract: routing tags only, so nothing that could carry personal data.
ALLOWED_KEYS = {
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
    "vehicle",
    "type",
    "restriction",
    "except",
    # Stage C request: bus lanes and conditional limits (routing tags, no personal data)
    "busway",
    "busway:left",
    "busway:right",
    "busway:both",
    "maxspeed:conditional",
}
ALLOWED_PREFIXES = ("lanes:", "turn:lanes", "restriction:", "bus:lanes")
# Ways clipped at TUNNEL_EXTRACT_BBOX (Stage B): motorway tunnels only.
EXTENDED_CLIP_HIGHWAYS = {"motorway", "motorway_link"}
# The only ways that reach past SIM_EXTRACT_BBOX through the wider clip (data/SOURCES.md).
EXTENDED_WAYS = {8066124, 8066126}
PERSONAL_ATTRS = {"user", "uid", "changeset", "timestamp"}
LOOP_OUT_DEG = 0.001  # ~100 m

pytestmark = pytest.mark.skipif(not PATH.exists(), reason="osm_sim_area.osm.gz not built yet")


def _inside(lon: float, lat: float, tol: float = 0.0, b: BBox = SIM_EXTRACT_BBOX) -> bool:
    return b.west - tol <= lon <= b.east + tol and b.south - tol <= lat <= b.north + tol


def _extended_clip(tags: dict[str, str]) -> bool:
    """A motorway tunnel way, clipped at TUNNEL_EXTRACT_BBOX (Stage B)."""
    return tags.get("tunnel") == "yes" and tags.get("highway") in EXTENDED_CLIP_HIGHWAYS


def _clip_box(tags: dict[str, str]) -> BBox:
    return TUNNEL_EXTRACT_BBOX if _extended_clip(tags) else SIM_EXTRACT_BBOX


def _motor_refused(tags: dict[str, str]) -> bool:
    for key in ACCESS_KEYS:
        if key in tags:
            return tags[key].strip().lower() == "no"
    return False


def _allowed_key(k: str) -> bool:
    return k in ALLOWED_KEYS or k.startswith(ALLOWED_PREFIXES)


def _required_key(k: str) -> bool:
    return k in REQUIRED_KEYS or k.startswith(REQUIRED_PREFIXES)


class Osm:
    def __init__(self, root: ET.Element):
        self.root = root
        self.nodes: dict[int, tuple[float, float]] = {}
        self.node_tags: dict[int, dict[str, str]] = {}
        self.ways: dict[int, tuple[list[int], dict[str, str]]] = {}
        self.rels: dict[int, tuple[list[tuple[str, int, str]], dict[str, str]]] = {}
        for el in root:
            tags = {c.get("k"): c.get("v") for c in el if c.tag == "tag"}
            oid = int(el.get("id", "0"))
            if el.tag == "node":
                self.nodes[oid] = (float(el.get("lon")), float(el.get("lat")))
                self.node_tags[oid] = tags
            elif el.tag == "way":
                self.ways[oid] = ([int(c.get("ref")) for c in el if c.tag == "nd"], tags)
            elif el.tag == "relation":
                members = [
                    (c.get("type"), int(c.get("ref")), c.get("role"))
                    for c in el
                    if c.tag == "member"
                ]
                self.rels[oid] = (members, tags)


@pytest.fixture(scope="module")
def osm() -> Osm:
    with gzip.open(PATH) as fh:  # also proves it is gzip
        root = ET.parse(fh).getroot()
    return Osm(root)


# --- format -----------------------------------------------------------------------------------


def test_parses_as_osm_xml(osm: Osm):
    assert osm.root.tag == "osm"
    assert osm.root.get("version") == "0.6"
    assert {el.tag for el in osm.root} <= {"bounds", "node", "way", "relation"}
    assert osm.nodes and osm.ways


def test_bounds_are_the_buffered_sim_bbox(osm: Osm):
    b = osm.root.find("bounds")
    assert b is not None
    got = [float(b.get(k)) for k in ("minlon", "minlat", "maxlon", "maxlat")]
    b2 = SIM_EXTRACT_BBOX
    want = [b2.west, b2.south, b2.east, b2.north]
    assert got == pytest.approx(want, abs=1e-6)


def test_no_personal_metadata(osm: Osm):
    found = {a for el in osm.root.iter() for a in el.attrib if a in PERSONAL_ATTRS}
    assert not found, f"personal-data attributes present: {found}"


def test_only_routing_tag_keys(osm: Osm):
    keys = {k for _, t in osm.ways.values() for k in t}
    keys |= {k for t in osm.node_tags.values() for k in t}
    keys |= {k for _, t in osm.rels.values() for k in t}
    extra = {k for k in keys if not _allowed_key(k)}
    assert not extra, f"unexpected tag keys: {sorted(extra)}"


# --- ways and nodes ---------------------------------------------------------------------------


def test_only_drivable_highways_and_no_motor_refused_ways(osm: Osm):
    for wid, (_, tags) in osm.ways.items():
        assert tags.get("highway") in DRIVABLE, (wid, tags.get("highway"))
        assert not _motor_refused(tags), (wid, tags)
        assert tags.get("area") != "yes", wid


def test_ways_reference_existing_nodes_and_no_orphans(osm: Osm):
    used: set[int] = set()
    for wid, (refs, _) in osm.ways.items():
        assert len(refs) >= 2, wid
        missing = [r for r in refs if r not in osm.nodes]
        assert not missing, (wid, missing[:5])
        used.update(refs)
    assert used == osm.nodes.keys(), f"{len(osm.nodes.keys() - used)} nodes not on any way"


def test_tunnel_extract_bbox_is_the_documented_one():
    """Only the east edge moves, to the building ring's east edge (orchestrator 2026-10-27)."""
    t, s = TUNNEL_EXTRACT_BBOX, SIM_EXTRACT_BBOX
    assert (t.west, t.south, t.north) == (s.west, s.south, s.north)
    assert t.east == BUILDINGS_BBOX.east


def test_ways_start_and_end_inside_buffered_bbox(osm: Osm):
    """SIM_EXTRACT_BBOX, or TUNNEL_EXTRACT_BBOX for motorway tunnel ways only."""
    for wid, (refs, tags) in osm.ways.items():
        for r in (refs[0], refs[-1]):
            assert _inside(*osm.nodes[r], b=_clip_box(tags)), (wid, r, osm.nodes[r])


def test_all_nodes_near_buffered_bbox(osm: Osm):
    """Nodes beyond SIM_EXTRACT_BBOX (+ the loop tolerance) belong only to motorway tunnel ways,
    inside TUNNEL_EXTRACT_BBOX, and only the Burnley / Domain tunnels reach out there."""
    owners: dict[int, list[dict[str, str]]] = {}
    for _, (refs, tags) in osm.ways.items():
        for r in refs:
            owners.setdefault(r, []).append(tags)
    far = []
    for n, p in osm.nodes.items():
        if _inside(*p, tol=LOOP_OUT_DEG):
            continue
        ok = all(_extended_clip(t) for t in owners[n]) and _inside(
            *p, tol=LOOP_OUT_DEG, b=TUNNEL_EXTRACT_BBOX
        )
        if not ok:
            far.append((n, p))
    assert not far, f"{len(far)} nodes more than ~100 m outside the extract bbox: {far[:3]}"
    reach = {
        wid
        for wid, (refs, _) in osm.ways.items()
        if any(not _inside(*osm.nodes[r]) for r in refs)
        and any(not _inside(*osm.nodes[r], tol=LOOP_OUT_DEG) for r in refs)
    }
    assert reach == EXTENDED_WAYS, sorted(reach)


def test_contract_tags_are_present_in_the_extract(osm: Osm):
    keys = {k for _, t in osm.ways.values() for k in t}
    for k in ("bridge", "tunnel", "layer", "lanes", "maxspeed", "oneway", "turn:lanes"):
        assert k in keys, f"no way carries `{k}`"
    assert any(t["highway"].endswith("_link") for _, t in osm.ways.values())
    signals = sum(1 for t in osm.node_tags.values() if t.get("highway") == "traffic_signals")
    assert signals > 0
    assert osm.rels, "no turn restrictions"


# --- restrictions -----------------------------------------------------------------------------


def test_restrictions_reference_only_included_members(osm: Osm):
    for rid, (members, tags) in osm.rels.items():
        assert tags.get("type") == "restriction", rid
        assert any(k == "restriction" or k.startswith("restriction:") for k in tags), rid
        roles = [role for _, _, role in members]
        assert {"from", "via", "to"} <= set(roles) and set(roles) <= {"from", "via", "to"}, rid
        for mtype, ref, _ in members:
            assert mtype in ("node", "way"), (rid, mtype)
            assert ref in (osm.nodes if mtype == "node" else osm.ways), (rid, mtype, ref)
        for mtype, ref, role in members:
            if role == "via" and mtype == "node":
                for t2, r2, role2 in members:
                    if role2 in ("from", "to"):
                        assert t2 == "way" and ref in osm.ways[r2][0], (rid, "via not on", r2)


# --- against the raw download (local, read-only) ----------------------------------------------


@pytest.fixture(scope="module")
def raw(osm: Osm):
    if not RAW_PBF.exists():
        pytest.skip("data/raw/bbbike_melbourne.osm.pbf not available")
    osmium = pytest.importorskip("osmium")
    ways, nodes, rels = {}, {}, {}
    for o in osmium.FileProcessor(str(RAW_PBF), osmium.osm.WAY).with_filter(
        osmium.filter.IdFilter(osm.ways.keys())
    ):
        ways[o.id] = dict(o.tags)
    for o in osmium.FileProcessor(str(RAW_PBF), osmium.osm.NODE).with_filter(
        osmium.filter.IdFilter(osm.nodes.keys())
    ):
        nodes[o.id] = ((o.location.lon, o.location.lat), dict(o.tags))
    for o in osmium.FileProcessor(str(RAW_PBF), osmium.osm.RELATION).with_filter(
        osmium.filter.KeyFilter("type")
    ):
        if o.tags.get("type") == "restriction":
            members = [
                ({"n": "node", "w": "way", "r": "relation"}[m.type], m.ref, m.role)
                for m in o.members
            ]
            rels[o.id] = (members, dict(o.tags))
    return osmium, ways, nodes, rels


def test_tags_copied_from_osm_unchanged(osm: Osm, raw):
    _, raw_ways, raw_nodes, raw_rels = raw
    for wid, (_, tags) in osm.ways.items():
        rt = raw_ways[wid]
        assert all(rt.get(k) == v for k, v in tags.items()), (wid, tags)  # nothing invented
        lost = {k: v for k, v in rt.items() if _required_key(k) and tags.get(k) != v}
        assert not lost, (wid, lost)  # contract tags kept where present
        assert rt.get("area") != "yes" and not _motor_refused(rt), (wid, rt)
    for nid, tags in osm.node_tags.items():
        (lon, lat), rt = raw_nodes[nid]
        assert osm.nodes[nid] == pytest.approx((lon, lat), abs=1e-7), nid
        assert all(rt.get(k) == v for k, v in tags.items()), (nid, tags)
        if rt.get("highway") == "traffic_signals":
            assert tags.get("highway") == "traffic_signals", nid
    for rid, (_, tags) in osm.rels.items():
        rt = raw_rels[rid][1]
        assert all(rt.get(k) == v for k, v in tags.items()), (rid, tags)


def test_every_drivable_way_in_the_bbox_is_kept_and_clipped_as_described(osm: Osm, raw):
    osmium = raw[0]
    missing, wrong_span = [], []
    fp = (
        osmium.FileProcessor(str(RAW_PBF), osmium.osm.NODE | osmium.osm.WAY)
        .with_locations()
        .with_filter(osmium.filter.KeyFilter("highway"))
    )
    for o in fp:
        if not o.is_way():
            continue
        tags = dict(o.tags)
        if tags.get("highway") not in DRIVABLE or tags.get("area") == "yes":
            continue
        if _motor_refused(tags):
            continue
        pts = [(n.ref, n.lon, n.lat) for n in o.nodes if n.location.valid()]
        box = _clip_box(tags)
        idx = [i for i, (_, lon, lat) in enumerate(pts) if _inside(lon, lat, b=box)]
        if not idx or idx[-1] == idx[0]:
            continue  # no node, or a single node, inside: nothing to keep
        span = [r for r, _, _ in pts[idx[0] : idx[-1] + 1]]
        if o.id not in osm.ways:
            missing.append(o.id)
        elif osm.ways[o.id][0] != span:
            wrong_span.append(o.id)
    assert not missing, f"{len(missing)} drivable ways in the bbox missing: {missing[:10]}"
    assert not wrong_span, f"{len(wrong_span)} ways not clipped as described: {wrong_span[:10]}"


def test_every_restriction_that_fits_is_kept(osm: Osm, raw):
    _, _, _, raw_rels = raw
    dropped = []
    for rid, (members, _) in raw_rels.items():
        roles = {role for _, _, role in members}
        if not {"from", "via", "to"} <= roles:
            continue
        ok = True
        for mtype, ref, role in members:
            if role not in ("from", "via", "to"):
                continue
            kept = osm.ways if mtype == "way" else osm.nodes if mtype == "node" else {}
            if ref not in kept:
                ok = False
            elif role == "via" and mtype == "node":
                ends = [r for t, r, rl in members if rl in ("from", "to") and t == "way"]
                ok = ok and all(r in osm.ways and ref in osm.ways[r][0] for r in ends)
        if ok and rid not in osm.rels:
            dropped.append(rid)
    assert not dropped, f"{len(dropped)} restrictions whose members are all kept were dropped"
