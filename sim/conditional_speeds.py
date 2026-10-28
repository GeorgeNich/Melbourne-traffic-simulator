"""Time-based speed limits (OSM maxspeed:conditional), e.g. 40 km/h school zones.

The extract carries `maxspeed:conditional` on a few ways, in two forms:

    40 @ (Mo-Fr 08:00-09:30, 14:30-16:00; PH off; SH off)   school zone (Dudley St)
    40 @ (08:00-24:00)                                        every day (Bay St / Crockford St)

A conditional limit is applied to a scenario only when the scenario's clock time falls inside its
hours. The baseline scenario has none: its demand is placeholder random traffic in simulation
seconds, with no time of day or weekday, so no window can be said to contain it and the network
keeps the signed (unconditional) limits. A Phase 3 scenario with a real clock (e.g. the weekday AM
peak) calls `edge_speeds` with its start time to get the edge speeds to set (TraCI
`edge.setMaxSpeed`, or an edge patch).

Only the opening-hours subset these tags use is parsed: optional weekday ranges (Mo-Fr, Sa, Su,
...), one or more HH:MM-HH:MM ranges, and `PH off` / `SH off` (public / school holidays). Holidays
are not known to a scenario, so `active` assumes an ordinary school day and says so in its result.
Anything else raises ValueError rather than being guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
_RULE = re.compile(r"^\s*(?P<speed>\d+(?:\.\d+)?)\s*@\s*\((?P<cond>[^)]*)\)\s*$")
_TIME = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


@dataclass(frozen=True)
class Window:
    days: frozenset[int]  # 0 = Monday
    start_min: int
    end_min: int  # may be 1440 (24:00)


@dataclass(frozen=True)
class ConditionalSpeed:
    kmh: float
    windows: tuple[Window, ...]
    off_on_public_holidays: bool
    off_on_school_holidays: bool


def _days(spec: str) -> frozenset[int]:
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            i, j = DAYS.index(a), DAYS.index(b)
            out.update(range(i, j + 1) if i <= j else [*range(i, 7), *range(0, j + 1)])
        else:
            out.add(DAYS.index(part))
    return frozenset(out)


def parse_hours(cond: str) -> tuple[tuple[Window, ...], bool, bool]:
    """Opening-hours subset: `Mo-Fr 07:00-09:30, 16:00-18:30; PH off; SH off` -> (windows,
    off on public holidays, off on school holidays). Shared with sim/bus_lane_hours.py."""
    ph = sh = False
    windows = []
    for clause in cond.split(";"):
        clause = clause.strip()
        if clause == "PH off":
            ph = True
            continue
        if clause == "SH off":
            sh = True
            continue
        days = frozenset(range(7))
        tokens = clause.split(" ", 1)
        if len(tokens) == 2 and tokens[0][:2] in DAYS:
            days, clause = _days(tokens[0]), tokens[1]
        for rng in clause.split(","):
            t = _TIME.match(rng.strip())
            if not t:
                raise ValueError(f"unsupported time range {rng!r} in {cond!r}")
            h1, m1, h2, m2 = map(int, t.groups())
            windows.append(Window(days, h1 * 60 + m1, h2 * 60 + m2))
    return tuple(windows), ph, sh


def in_windows(windows: tuple[Window, ...], when: datetime) -> bool:
    minute = when.hour * 60 + when.minute
    return any(when.weekday() in w.days and w.start_min <= minute < w.end_min for w in windows)


def parse(rule: str) -> ConditionalSpeed:
    m = _RULE.match(rule)
    if not m:
        raise ValueError(f"unsupported maxspeed:conditional: {rule!r}")
    windows, ph, sh = parse_hours(m["cond"])
    return ConditionalSpeed(float(m["speed"]), windows, ph, sh)


def active(rule: str, when: datetime) -> bool:
    """Does the conditional limit apply at `when` (local time), on an ordinary school day?"""
    return in_windows(parse(rule).windows, when)


def conditional_ways(osm) -> list[dict]:
    """Every extract way with maxspeed:conditional: way, name, normal and conditional limit."""
    rows = []
    for wid, (_, tags) in sorted(osm.ways.items()):
        rule = tags.get("maxspeed:conditional")
        if rule:
            rows.append(
                {
                    "way": wid,
                    "name": tags.get("name"),
                    "maxspeed": tags.get("maxspeed"),
                    "rule": rule,
                    "conditional_kmh": parse(rule).kmh,
                }
            )
    return rows


def edge_speeds(osm, idx, when: datetime) -> dict[str, float]:
    """{edge id: m/s} for network edges whose OSM ways all have a conditional limit in force at
    `when`; the lowest applies. Edges mixing conditional and plain ways are left alone."""
    out: dict[str, float] = {}
    for eid, ways in idx.ways_by_edge.items():
        rules = [osm.ways[w][1].get("maxspeed:conditional") for w in ways if w in osm.ways]
        if not rules or any(r is None for r in rules):
            continue
        live = [parse(r).kmh for r in rules if active(r, when)]
        if len(live) == len(rules):
            out[eid] = round(min(live) / 3.6, 2)
    return out
