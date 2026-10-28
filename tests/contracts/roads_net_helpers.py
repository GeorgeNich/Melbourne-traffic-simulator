"""Shared, independent readers for the sim/net/roads.net.xml contract tests (qa-reviewer).

Nothing here imports sim/ code: the network is parsed with ElementTree, the OSM extract with gzip +
ElementTree, coordinates are converted with pyproj from the network's own <location> header, and
OSM nodes are mapped to junctions from the junction ids and sim/net/joined_junctions.xml.
Results are cached, so the 12 MB network is parsed once per test session.
"""

from __future__ import annotations

import gzip
import math
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NET_PATH = ROOT / "sim" / "net" / "roads.net.xml"
JOINED_PATH = ROOT / "sim" / "net" / "joined_junctions.xml"
OSM_PATH = ROOT / "data" / "processed" / "osm_sim_area.osm.gz"
ROADS_3D_PATH = ROOT / "data" / "processed" / "roads_3d.geojson"
RAW_PBF = ROOT / "data" / "raw" / "bbbike_melbourne.osm.pbf"

# OSM highway values SUMO's OSM typemap opens to passenger cars (highway=service is dropped).
CAR_HIGHWAYS = {
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "unclassified",
    "residential",
    "living_street",
    "motorway_link",
    "trunk_link",
    "primary_link",
    "secondary_link",
    "tertiary_link",
}
ACCESS_KEYS = ("motorcar", "motor_vehicle", "vehicle", "access")  # most specific first
NO_PUBLIC_CARS = {"no", "private", "permit"}


def motor_access(tags: dict[str, str]) -> str | None:
    for key in ACCESS_KEYS:
        if key in tags:
            return tags[key].strip().lower()
    return None


@dataclass
class Edge:
    id: str
    frm: str
    to: str
    name: str
    type: str
    ways: list[int]
    lanes: list[dict[str, str]]  # lane attributes (allow / disallow / speed ...)
    shape: list[tuple[float, float]]
    out: dict[str, set[str]] = field(default_factory=dict)  # to edge -> SUMO dirs

    @property
    def speed(self) -> float:
        return max(float(ln["speed"]) for ln in self.lanes)


@dataclass
class Net:
    root_attrs: dict[str, str]
    header: str
    location: dict[str, str]
    edges: dict[str, Edge]
    junctions: dict[str, dict[str, str]]
    tl_ids: set[str]
    # tlLogic id -> junctions whose incoming connections it controls
    tl_junctions: dict[str, set[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        from pyproj import Transformer

        ox, oy = (float(v) for v in self.location["netOffset"].split(","))
        self._off = (ox, oy)
        self._to_ll = Transformer.from_crs(
            self.location["projParameter"], "EPSG:4326", always_xy=True
        )
        self._to_xy = Transformer.from_crs(
            "EPSG:4326", self.location["projParameter"], always_xy=True
        )

    def lonlat(self, x: float, y: float) -> tuple[float, float]:
        return self._to_ll.transform(x - self._off[0], y - self._off[1])

    def xy(self, lon: float, lat: float) -> tuple[float, float]:
        ux, uy = self._to_xy.transform(lon, lat)
        return ux + self._off[0], uy + self._off[1]

    @property
    def incoming(self) -> dict[str, list[Edge]]:
        inc: dict[str, list[Edge]] = defaultdict(list)
        for e in self.edges.values():
            inc[e.to].append(e)
        return inc

    @property
    def outgoing(self) -> dict[str, list[Edge]]:
        out: dict[str, list[Edge]] = defaultdict(list)
        for e in self.edges.values():
            out[e.frm].append(e)
        return out


def _shape(s: str) -> list[tuple[float, float]]:
    pts = []
    for p in s.split():
        x, y = p.split(",")[:2]
        pts.append((float(x), float(y)))
    return pts


@cache
def load_net(path: Path = NET_PATH) -> Net:
    text = path.read_text(encoding="utf-8")
    header = text[: text.find("<net ")]
    root = ET.fromstring(text)
    loc = root.find("location")
    edges: dict[str, Edge] = {}
    for el in root.iter("edge"):
        if el.get("function") == "internal":
            continue
        ways: list[int] = []
        lanes = []
        for ln in el.iter("lane"):
            lanes.append(dict(ln.attrib))
            for p in ln.iter("param"):
                if p.get("key") == "origId":
                    for tok in p.get("value", "").split():
                        if tok.lstrip("-").isdigit() and int(tok) not in ways:
                            ways.append(int(tok))
        shape = el.get("shape") or (lanes[0]["shape"] if lanes else "")
        edges[el.get("id")] = Edge(
            el.get("id"),
            el.get("from"),
            el.get("to"),
            el.get("name", ""),
            el.get("type", ""),
            ways,
            lanes,
            _shape(shape),
        )
    tl_junctions: dict[str, set[str]] = {}
    for c in root.iter("connection"):
        f, t = c.get("from"), c.get("to")
        if f in edges and t in edges:
            edges[f].out.setdefault(t, set()).add(c.get("dir"))
            if c.get("tl"):
                tl_junctions.setdefault(c.get("tl"), set()).add(edges[f].to)
    junctions = {
        j.get("id"): dict(j.attrib) for j in root.iter("junction") if j.get("type") != "internal"
    }
    tl_ids = {t.get("id") for t in root.iter("tlLogic")}
    return Net(dict(root.attrib), header, dict(loc.attrib), edges, junctions, tl_ids, tl_junctions)


@dataclass
class Osm:
    nodes: dict[int, tuple[float, float]]
    ways: dict[int, tuple[list[int], dict[str, str]]]
    rels: dict[int, tuple[list[tuple[str, int, str]], dict[str, str]]]

    def ways_at(self) -> dict[int, set[int]]:
        out: dict[int, set[int]] = defaultdict(set)
        for w, (refs, _) in self.ways.items():
            for n in refs:
                out[n].add(w)
        return out


@cache
def load_osm(path: Path = OSM_PATH) -> Osm:
    nodes, ways, rels = {}, {}, {}
    with gzip.open(path) as fh:
        for _, el in ET.iterparse(fh, events=("end",)):
            if el.tag == "node":
                nodes[int(el.get("id"))] = (float(el.get("lon")), float(el.get("lat")))
                el.clear()
            elif el.tag == "way":
                tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
                ways[int(el.get("id"))] = ([int(n.get("ref")) for n in el.findall("nd")], tags)
                el.clear()
            elif el.tag == "relation":
                tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
                mem = [
                    (m.get("type"), int(m.get("ref")), m.get("role")) for m in el.findall("member")
                ]
                rels[int(el.get("id"))] = (mem, tags)
                el.clear()
    return Osm(nodes, ways, rels)


@cache
def junction_of_osm_node() -> dict[int, str]:
    """OSM node id -> SUMO junction id, for nodes that are (or were joined into) a junction."""
    net = load_net()
    out: dict[int, str] = {}
    clusters: dict[str, set[str]] = {}
    for jid in net.junctions:
        if jid.startswith("cluster_"):
            clusters[jid] = {t for t in jid[len("cluster_") :].split("_") if not t.startswith("#")}
        elif jid.isdigit():
            out[int(jid)] = jid
    if JOINED_PATH.exists():
        for j in ET.parse(JOINED_PATH).getroot().iter("join"):
            group = set(j.get("nodes", "").split())
            hits = [c for c, toks in clusters.items() if toks <= group]
            if len(hits) == 1:
                for n in group:
                    out[int(n)] = hits[0]
    for c, toks in clusters.items():
        for t in toks:
            out.setdefault(int(t), c)
    return out


def dist_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])
