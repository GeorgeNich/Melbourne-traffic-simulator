"""Shared lookups between the OSM extract and the SUMO network, used by every network check.

- `read_osm`: nodes, ways and restriction relations from an OSM XML file (gzip or plain).
- `NetIndex`: for a SUMO network, which edges carry each OSM way (lane param `origId`), and which
  junction each OSM node ended up in (itself, a joined cluster, or the interior of an edge).
"""

from __future__ import annotations

import gzip
import math
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class OsmData:
    nodes: dict[int, tuple[float, float]] = field(default_factory=dict)  # id -> (lon, lat)
    node_tags: dict[int, dict[str, str]] = field(default_factory=dict)
    ways: dict[int, tuple[list[int], dict[str, str]]] = field(default_factory=dict)
    # id -> ([(type, ref, role)], tags)
    relations: dict[int, tuple[list[tuple[str, int, str]], dict[str, str]]] = field(
        default_factory=dict
    )

    def way_name(self, wid: int) -> str:
        tags = self.ways.get(wid, ([], {}))[1]
        return tags.get("name") or tags.get("ref") or f"way {wid}"


def _open(path: Path):
    return gzip.open(path, "rb") if path.suffix == ".gz" else open(path, "rb")


def read_osm(path: Path) -> OsmData:
    data = OsmData()
    with _open(path) as fh:
        for _, el in ET.iterparse(fh, events=("end",)):
            tag = el.tag
            if tag == "node":
                nid = int(el.get("id"))
                data.nodes[nid] = (float(el.get("lon")), float(el.get("lat")))
                tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
                if tags:
                    data.node_tags[nid] = tags
                el.clear()
            elif tag == "way":
                refs = [int(n.get("ref")) for n in el.findall("nd")]
                tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
                data.ways[int(el.get("id"))] = (refs, tags)
                el.clear()
            elif tag == "relation":
                members = [
                    (m.get("type"), int(m.get("ref")), m.get("role")) for m in el.findall("member")
                ]
                tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
                data.relations[int(el.get("id"))] = (members, tags)
                el.clear()
    return data


def read_joined(path: Path) -> list[list[str]]:
    """Node-id groups from netconvert's --junctions.join-output file."""
    if not path.exists():
        return []
    root = ET.parse(path).getroot()
    return [j.get("nodes").split() for j in root.iter("join")]


def way_ids(orig: str | None) -> list[int]:
    out = []
    for tok in (orig or "").split():
        try:
            out.append(int(tok))
        except ValueError:
            continue
    return out


class NetIndex:
    """Lookups on a sumolib network that tie it back to OSM ids."""

    def __init__(self, net, joined_groups: list[list[str]] | None = None):
        self.net = net
        self.edges_by_way: dict[int, list] = defaultdict(list)
        self.ways_by_edge: dict[str, list[int]] = {}
        for edge in net.getEdges():
            ids: list[int] = []
            for lane in edge.getLanes():
                for w in way_ids(lane.getParam("origId")):
                    if w not in ids:
                        ids.append(w)
            if not ids:  # fall back to the edge id ("123#4", "-123#0")
                head = edge.getID().lstrip("-").split("#")[0]
                ids = way_ids(head)
            self.ways_by_edge[edge.getID()] = ids
            for w in ids:
                self.edges_by_way[w].append(edge)

        self.junction_of_node: dict[str, str] = {}
        node_ids = {n.getID() for n in net.getNodes()}
        for nid in node_ids:
            self.junction_of_node[nid] = nid
        clusters = [n for n in node_ids if n.startswith("cluster_")]
        for group in joined_groups or []:
            jid = self._cluster_for(group, clusters)
            if jid is None:
                continue
            for nid in group:
                self.junction_of_node[nid] = jid

    @staticmethod
    def _cluster_for(group: list[str], clusters: list[str]) -> str | None:
        members = set(group)
        exact = "cluster_" + "_".join(sorted(group))
        if exact in clusters:
            return exact
        best = None
        for cid in clusters:
            toks = cid[len("cluster_") :].split("_")
            listed = [t for t in toks if not t.startswith("#")]
            more = [t for t in toks if t.startswith("#")]
            n = len(listed) + (int(more[0][1:].removesuffix("more")) if more else 0)
            if n == len(members) and set(listed) <= members:
                best = cid
                break
        return best

    def junction(self, osm_node: int) -> str | None:
        return self.junction_of_node.get(str(osm_node))

    def xy(self, lon: float, lat: float) -> tuple[float, float]:
        return self.net.convertLonLat2XY(lon, lat)

    def nearest_junction(self, x: float, y: float, radius: float) -> tuple[str | None, float]:
        best, best_d = None, math.inf
        for node in self.net.getNodes():
            nx, ny = node.getCoord()[:2]
            d = math.hypot(nx - x, ny - y)
            if d < best_d:
                best, best_d = node.getID(), d
        return (best, best_d) if best_d <= radius else (None, best_d)
