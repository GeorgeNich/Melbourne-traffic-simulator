"""Bus lanes closed to private cars, from the OSM lane access tags in the extract.

Victorian Road Rules r.154-158: only buses (and the vehicles a sign allows) may drive in a bus lane,
except for up to 100 m to enter or leave the road or to turn (r.158). The extract tags bus lanes
three ways, and netconvert only understands the first:

- `bus:lanes` / `psv:lanes` (+ `:forward` / `:backward`): one value per lane, left to right, e.g.
  `yes|yes|yes|designated`. netconvert's --osm.lane-access makes a `designated` lane bus-only
  (and maps OSM's left-to-right order onto SUMO lane indices in a left-hand network).
- `lanes:bus` / `lanes:psv` (+ `:forward` / `:backward`): only a COUNT of bus lanes, not which.
- `busway=lane` / `busway:left|right|both=lane`: a bus lane on that side of the road.

netconvert ignores the count tags, and turns `busway:left=lane` on a one-way road into a separate
bus-only edge running AGAINST the one-way (dropped by --keep-edges.by-vclass passenger, but it also
splits the real edge). So before the OSM import, `write_osm` copies the extract to sim/work/ with
each tagged way's bus lanes rewritten as an explicit `bus:lanes` value, and the count / busway
tags removed; everything else is byte-for-byte the extract's data. netconvert then builds the
connections with the bus lanes in place: cars turn from the lanes they may use.

Placement rule (an assumption, labelled in the road-rules report): a counted or `busway=lane` bus
lane is the KERB lane (leftmost in Melbourne's left-hand traffic), which is where Victorian bus
lanes run (r.158 lets cars cross into one to turn left). An explicit side (`busway:right`) or an
explicit `bus:lanes` value wins. Ways are skipped, and listed, when the placement would be a
guess: no `lanes` tag (the lane count is unknown), as many bus lanes as lanes (it would close the
road), or a two-way road whose count does not say which direction.

Hours: the extract carries no `:conditional` hours for any bus lane, so every bus lane is closed
to cars at all times. Many Melbourne bus lanes operate in peak hours only; Phase 3 should revisit
this per scenario (see the road-rules report).
"""

from __future__ import annotations

import gzip
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from sim.checks.netindex import OsmData

ONEWAY_YES = {"yes", "true", "1"}
EXPLICIT_KEYS = (
    "bus:lanes",
    "bus:lanes:forward",
    "bus:lanes:backward",
    "psv:lanes",
    "psv:lanes:forward",
    "psv:lanes:backward",
)
COUNT_KEYS = ("lanes:bus", "lanes:psv")
BUSWAY_KEYS = ("busway", "busway:left", "busway:right", "busway:both")
# Removed from the netconvert copy once rewritten as bus:lanes (see the module docstring).
REWRITTEN_KEYS = (
    *COUNT_KEYS,
    *(f"{k}:{d}" for k in COUNT_KEYS for d in ("forward", "backward")),
    *BUSWAY_KEYS,
)


@dataclass
class BusLaneWay:
    way: int
    name: str
    source: str  # the tag(s) the bus lanes were read from
    lanes: int | None
    bus_lanes: int
    side: str  # "left" (kerb) / "right" / "explicit"
    new_tags: dict[str, str] = field(default_factory=dict)  # added to the netconvert copy
    skipped: str | None = None  # why nothing was closed


def _int(v: str | None) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _is_oneway(tags: dict[str, str]) -> bool:
    return (
        tags.get("oneway", "").lower() in ONEWAY_YES
        or tags.get("junction") == "roundabout"
        or tags.get("highway") in ("motorway", "motorway_link")
    )


def lane_values(lanes: int, bus: int, side: str) -> str:
    """bus:lanes value, OSM order (left to right): `bus` designated lanes on `side`."""
    vals = ["yes"] * lanes
    idx = range(bus) if side == "left" else range(lanes - bus, lanes)
    for i in idx:
        vals[i] = "designated"
    return "|".join(vals)


def _busway_side(tags: dict[str, str]) -> str | None:
    if tags.get("busway:right") == "lane":
        return "right"
    if tags.get("busway:left") == "lane" or tags.get("busway") == "lane":
        return "left"
    if tags.get("busway:both") == "lane":
        return "left"
    return None


def plan_way(wid: int, tags: dict[str, str], name: str) -> BusLaneWay | None:
    """How this way's bus lanes are closed to cars, or None if it has no bus lane tags."""
    explicit = [k for k in EXPLICIT_KEYS if k in tags]
    count_keys = [k for k in tags if k.startswith(COUNT_KEYS)]
    busway = [k for k in BUSWAY_KEYS if tags.get(k) == "lane"]
    if not (explicit or count_keys or busway):
        return None
    lanes = _int(tags.get("lanes"))
    if explicit:
        n_bus = sum(
            v.strip() in ("designated", "only") for k in explicit for v in tags[k].split("|")
        )
        return BusLaneWay(wid, name, ",".join(explicit), lanes, n_bus, "explicit")
    source = ",".join(count_keys + busway)
    side = _busway_side(tags) or "left"
    if not _is_oneway(tags):
        fwd = _int(tags.get("lanes:bus:forward")) or _int(tags.get("lanes:psv:forward"))
        bwd = _int(tags.get("lanes:bus:backward")) or _int(tags.get("lanes:psv:backward"))
        row = BusLaneWay(wid, name, source, lanes, (fwd or 0) + (bwd or 0), side)
        if not (fwd or bwd):
            row.bus_lanes = _int(tags.get("lanes:bus")) or 1
            row.skipped = "two-way road: the tags do not say which direction has the bus lane"
            return row
        new = {}
        for d, k in (("forward", fwd), ("backward", bwd)):
            n = _int(tags.get(f"lanes:{d}"))
            if not k:
                continue
            if n is None or n <= k:
                row.skipped = f"lanes:{d} missing or not more than its bus lanes"
                return row
            new[f"bus:lanes:{d}"] = lane_values(n, k, side)
        row.new_tags = new
        return row
    n_bus = (
        _int(tags.get("lanes:bus"))
        or _int(tags.get("lanes:bus:forward"))
        or _int(tags.get("lanes:psv"))
        or (1 if busway else 0)
    )
    row = BusLaneWay(wid, name, source, lanes, n_bus, side)
    if lanes is None:
        row.skipped = "no lanes tag: lane count unknown"
    elif lanes <= n_bus:
        row.skipped = f"lanes={lanes} but {n_bus} bus lane(s): closing would close the road"
    else:
        row.new_tags = {"bus:lanes": lane_values(lanes, n_bus, side)}
    return row


def plan(osm: OsmData) -> list[BusLaneWay]:
    rows = []
    for wid, (_, tags) in sorted(osm.ways.items()):
        row = plan_way(wid, tags, osm.way_name(wid))
        if row is not None:
            rows.append(row)
    return rows


def write_osm(src: Path, dst: Path, rows: list[BusLaneWay]) -> int:
    """Copy `src` (OSM XML, gzip or plain) to `dst` (plain XML) with each planned way's bus lanes
    as explicit bus:lanes tags; returns the number of ways rewritten."""
    todo = {r.way: r for r in rows if r.new_tags and not r.skipped}
    opener = gzip.open if src.suffix == ".gz" else open
    with opener(src, "rb") as fh:
        tree = ET.parse(fh)
    done = 0
    for way in tree.getroot().iter("way"):
        row = todo.get(int(way.get("id")))
        if row is None:
            continue
        for t in list(way.findall("tag")):
            if t.get("k") in REWRITTEN_KEYS:
                way.remove(t)
        for k, v in row.new_tags.items():
            ET.SubElement(way, "tag", {"k": k, "v": v})
        done += 1
    dst.parent.mkdir(parents=True, exist_ok=True)
    tree.write(dst, encoding="UTF-8", xml_declaration=True)
    return done
