"""Validate George's AI reading of the DTP signal configuration sheets; write a verified reference.

Inputs:
- `data/raw/signal_sheets_ai/`: unchanged snapshot of the AI output (`<site>.json`, index.csv,
  diagrams/), copied from ~/Downloads/signal_sheets on 2026-10-27. A derived reference, not a
  source (data/SOURCES.md).
- `data/raw/scats/sheets/`: our own copy of the DTP sheets (rasterised PDF or Excel-HTML).
- `pipeline/reference/signal_sheets_reread.txt`: the data engineer's own reading of the original
  sheet pages (sample sites and priority sites), each value cited to its page.
- `sim/net/roads.net.xml`, `data/processed/osm_sim_area.osm.gz`, the site register,
  `data/processed/counts.parquet` (priority sites).

Outputs:
- `pipeline/reference/signal_sheets_verified.json` (read by sim-engineer).
- `data/processed/checks/signal_sheets_validation.json`.

Error-rate rule (fixed before the re-read, task 3C step 3), per field group, from the random
sample only: <= 5 % -> AI values kept (errors found fixed); 5-15 % -> re-read for priority sites,
others `ai_unverified`; > 15 % -> AI group rejected, re-read for priority sites only.
An "item" (the unit of the error rate):
- timings: one (phase, field) value, fields min green, max extension green, yellow, all red,
  special all red, over the phases of the sheet's sequence;
- phases: the sequence string (1 item) + the signal-group set of each phase (1 item per phase);
- pedestrians: one (ped, field) value, fields walk, clearance 1, clearance 2, over the peds the
  sheet uses;
- lane_layout: one lane of one approach (use + arrows must both match; a lane on a wrong or
  reversed approach, a missing lane and an extra lane each count as one wrong item).
A missing AI value where the sheet has one is wrong; both empty is right.

Run: uv run python -m pipeline.signal_sheets
"""

from __future__ import annotations

import hashlib
import html
import json
import random
import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

from pipeline.config import SIM_EXTRACT_BBOX

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW = REPO_ROOT / "data" / "raw"
AI_DIR = RAW / "signal_sheets_ai"
SHEETS_RAW = RAW / "scats" / "sheets"
REF_DIR = Path(__file__).resolve().parent / "reference"
REREAD_PATH = REF_DIR / "signal_sheets_reread.txt"
OUT_PATH = REF_DIR / "signal_sheets_verified.json"
CHECKS_DIR = REPO_ROOT / "data" / "processed" / "checks"
VALIDATION_PATH = CHECKS_DIR / "signal_sheets_validation.json"
COUNTS_PATH = REPO_ROOT / "data" / "processed" / "counts.parquet"
OSM_PATH = REPO_ROOT / "data" / "processed" / "osm_sim_area.osm.gz"

SNAPSHOT_DATE = "2026-10-27"
SAMPLE_SEED = 20261027
SAMPLE_SIZE = 25
SAMPLE_MIN_LANE_LAYOUT = 12
SAMPLE_FORCED = (2922,)
SAMPLE_ONE_OF = (2902, 2903, 2904, 2909)  # the Lonsdale sites with reversed AI directions

# Plausible ranges (seconds), inclusive.
RANGES = {
    "minimum_green": (4, 30),
    "maximum_extension_green": (0, 90),
    "yellow": (3, 6),
    "all_red": (0, 6),
    "special_all_red": (0, 6),
}
TIMING_FIELDS = tuple(RANGES)
PED_FIELDS = ("walk", "clearance_1", "clearance_2")
FIELD_GROUPS = ("timings", "phases", "pedestrians", "lane_layout")
THRESH_LOW, THRESH_HIGH = 0.05, 0.15
ITEM_RULES = {
    "timings": "one (phase, field) value: min green, max extension green, yellow, all red, "
    "special all red (0 == blank for special all red)",
    "phases": "the V.A. sequence (letters only) + the non-pedestrian signal-group set of each "
    "phase; a phase the AI left empty is wrong",
    "pedestrians": "one (ped, field) value: walk, clearance 1, clearance 2 "
    "(0 == blank for clearance 2)",
    "lane_layout": "one lane of one approach the AI listed: use class must match, arrows too "
    "where drawn; missing/extra lanes are wrong; approaches we could not read are not compared",
}

PRIORITY_STREETS = re.compile(
    r"\bFLINDERS\b(?! ?(LANE|LA)\b)|\bSPENCER\b|\bKING\b(?!S)|\bWILLIAM\b|\bELIZABETH\b|"
    r"\bSWANSTON\b|\bLA ?TROBE\b|\bWURUNDJERI\b|\bKINGS ?WAY\b|\bFOOTSCRAY\b",
    re.I,
)

HEADING_WORDS = {
    "northbound": "NB",
    "southbound": "SB",
    "eastbound": "EB",
    "westbound": "WB",
    "north-east bound": "NE",
    "north-west bound": "NW",
    "south-east bound": "SE",
    "south-west bound": "SW",
}
HEADINGS8 = ("NB", "NE", "EB", "SE", "SB", "SW", "WB", "NW")
OPPOSITE = {h: HEADINGS8[(i + 4) % 8] for i, h in enumerate(HEADINGS8)}
# AI arrow words -> turn letters (L/T/R/U), and back.
ARROW_LETTER = {"left": "L", "through": "T", "right": "R", "uturn": "U", "u-turn": "U"}
OSM_TURN = {
    "left": "L",
    "slight_left": "L",
    "sharp_left": "L",
    "merge_to_left": "L",
    "through": "T",
    "right": "R",
    "slight_right": "R",
    "sharp_right": "R",
    "merge_to_right": "R",
    "reverse": "U",
}


# --------------------------------------------------------------------------------------------
# AI output: load and normalise


def load_ai(ai_dir: Path = AI_DIR) -> dict[int, dict]:
    out = {}
    for p in sorted(ai_dir.glob("*.json")):
        if p.stem.isdigit():
            out[int(p.stem)] = json.loads(p.read_text())
    return out


def arrows_to_letters(arrows: str | None) -> str:
    """'through+left' -> 'LT'; 'left_through_right' -> 'LTR'; unknown words are dropped."""
    if not arrows:
        return ""
    letters = {ARROW_LETTER[w] for w in re.split(r"[+_ ,/]+", arrows.lower()) if w in ARROW_LETTER}
    return "".join(c for c in "LTRU" if c in letters)


def norm_lane_layout(layout: dict | None) -> dict[str, list[dict]] | None:
    """Both AI shapes -> {approach: [{lane, use, arrows}]}, arrows as turn letters ('LT').

    Shape 1 (66 sites): approach -> list of {lane, use, arrows, ...}. Shape 2 (20 sites):
    approach -> {lanes: n, movements: [...]} (one movement per lane, from the left kerb)."""
    if not layout:
        return None
    out: dict[str, list[dict]] = {}
    for appr, v in layout.items():
        if isinstance(v, list):
            out[appr] = [
                {
                    "lane": ln.get("lane"),
                    "use": ln.get("use"),
                    "arrows": arrows_to_letters(ln.get("arrows")),
                }
                for ln in v
            ]
        elif isinstance(v, dict):
            lanes = []
            for i, m in enumerate(v.get("movements") or [], start=1):
                use = "tram" if "tram" in str(m) else "general"
                lanes.append({"lane": i, "use": use, "arrows": arrows_to_letters(m)})
            out[appr] = lanes
    return out


def parse_approach(key: str) -> tuple[str, str | None]:
    """'Elizabeth St northbound' -> ('Elizabeth St', 'NB'); 'Flinders Lane WB' -> (.., 'WB')."""
    k = key.strip()
    for word, h in HEADING_WORDS.items():
        if k.lower().endswith(" " + word):
            return k[: -len(word) - 1].strip(), h
    m = re.match(r"^(.*?)\s+(NB|SB|EB|WB|NE|NW|SE|SW)$", k)
    if m:
        return m.group(1).strip(), m.group(2)
    return k, None


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------------------------
# Automatic checks on the AI output (pure; no sheets needed)


def check_schema(d: dict) -> list[str]:
    errs = []
    for k in ("site_no", "site_name", "phases", "timings", "detectors"):
        if k not in d:
            errs.append(f"missing key {k}")
    ph = d.get("phases")
    if not isinstance(ph, dict) or not {"sequence", "signal_groups_by_phase"} <= set(ph):
        errs.append("phases: not {sequence, signal_groups_by_phase}")
    t = d.get("timings")
    if not isinstance(t, dict) or not {"phases", "pedestrians"} <= set(t):
        errs.append("timings: not {phases, pedestrians}")
    else:
        for p, v in (t.get("phases") or {}).items():
            if not isinstance(v, dict) or not set(TIMING_FIELDS) <= set(v):
                errs.append(f"timings.phases.{p}: missing fields")
            else:
                for f in TIMING_FIELDS:
                    if v[f] is not None and _num(v[f]) is None:
                        errs.append(f"timings.phases.{p}.{f}: not a number ({v[f]!r})")
        for p, v in (t.get("pedestrians") or {}).items():
            if not re.fullmatch(r"P\d", p):
                errs.append(f"timings.pedestrians: bad key {p}")
            if not isinstance(v, dict) or not set(PED_FIELDS) <= set(v):
                errs.append(f"timings.pedestrians.{p}: missing fields")
    if not isinstance(d.get("detectors"), list):
        errs.append("detectors: not a list")
    ll = d.get("lane_layout")
    if ll is not None:
        if not isinstance(ll, dict):
            errs.append("lane_layout: not an object")
        else:
            for appr, v in ll.items():
                if parse_approach(appr)[1] is None:
                    errs.append(f"lane_layout: approach {appr!r} has no heading")
                if isinstance(v, dict):
                    errs.append(
                        f"lane_layout.{appr}: summary shape (lanes/movements), not per lane"
                    )
                    if v.get("lanes") != len(v.get("movements") or []):
                        errs.append(f"lane_layout.{appr}: lanes != len(movements)")
    return errs


def check_ranges(d: dict) -> list[str]:
    out = []
    for p, v in ((d.get("timings") or {}).get("phases") or {}).items():
        for f, (lo, hi) in RANGES.items():
            x = _num(v.get(f)) if isinstance(v, dict) else None
            if x is not None and not lo <= x <= hi:
                out.append(f"{p}.{f}={x:g} outside {lo}-{hi}")
    return out


def check_phase_consistency(d: dict) -> list[str]:
    out = []
    ph = d.get("phases") or {}
    seq = ph.get("sequence") or ""
    seq_letters = sorted(set(re.findall(r"[A-G]", seq)))
    t_letters = sorted(((d.get("timings") or {}).get("phases") or {}).keys())
    if seq_letters != t_letters:
        out.append(f"sequence {seq!r} phases {seq_letters} != timings phases {t_letters}")
    sg = ph.get("signal_groups_by_phase") or {}
    if sorted(sg) != seq_letters and sg:
        out.append(f"signal_groups_by_phase keys {sorted(sg)} != sequence phases {seq_letters}")
    groups = set()
    for p, gs in sg.items():
        for g in gs or []:
            if not re.fullmatch(r"\d{1,2}", str(g)) or not 1 <= int(g) <= 24:
                out.append(f"phase {p}: signal group {g!r} is not a group number 1-24")
            groups.add(str(g))
        if not gs and seq_letters:
            out.append(f"phase {p}: no signal groups")
    for det in d.get("detectors") or []:
        g = det.get("signal_group")
        if g not in (None, "") and str(g) not in groups:
            out.append(f"detector {det.get('detector_no')}: signal group {g} in no phase")
    return out


def check_register(site: int, reg: dict, bbox=SIM_EXTRACT_BBOX) -> list[str]:
    s = reg.get(site)
    if s is None:
        return ["not in victorian_traffic_signals.csv"]
    if not (bbox.west <= s.lon <= bbox.east and bbox.south <= s.lat <= bbox.north):
        return [f"outside SIM_BBOX + buffer ({s.lon:.5f}, {s.lat:.5f})"]
    return []


# --------------------------------------------------------------------------------------------
# Our reading of htm sheets: the timing tables are text in the Excel-HTML export (no OCR).


class _Rows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def html_rows(text: str) -> list[list[str]]:
    p = _Rows()
    p.feed(text)
    return [[html.unescape(c).replace("\xa0", " ").strip() for c in r] for r in p.rows]


def _cell_num(c: str) -> float | None:
    """'3.0' -> 3.0, '0*' -> 0.0 (footnote mark), '-' / '' -> None."""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)", c or "")
    return float(m.group(1)) if m else None


def _tab_files(htm: Path) -> list[Path]:
    return sorted((htm.parent / f"{htm.stem}_files").glob("sheet*.htm"))


def _read(p: Path) -> str:
    return p.read_text(encoding="windows-1252", errors="replace")


def parse_htm_sheet(htm: Path) -> dict:
    """Timings, pedestrian times, V.A. sequence and approach definitions from the text tabs.

    Returns {timings: {A: {field: s}}, pedestrians: {P2: {...}}, sequence, approach_groups:
    {A: [groups]}, pages: {what: tab file}}; keys absent if not found."""
    out: dict = {"pages": {}}
    labels = {
        "MINIMUM GREEN": "minimum_green",
        "MAXIMUM EXTENSION GREEN": "maximum_extension_green",
        "YELLOW": "yellow",
        "ALL RED": "all_red",
        "SPECIAL ALL RED": "special_all_red",
    }
    ped_labels = {
        "WALK*": "walk",
        "WALK": "walk",
        "CLEARANCE 1": "clearance_1",
        "CLEARANCE 2": "clearance_2",
    }
    for tab in _tab_files(htm):
        text = _read(tab)
        flat = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text)))
        rows = html_rows(text)
        if "PHASE TIMESETTINGS" in flat and "MINIMUM GREEN" in flat and "timings" not in out:
            out["pages"]["timings"] = tab.name
            phases: dict[str, dict] = {}
            peds: dict[str, dict] = {}
            in_ped = False
            for r in rows:
                if not r:
                    continue
                lab = re.sub(r"\s*\(\d+\)|\*", "", r[0].upper()).strip()
                if lab.startswith("PEDESTRIAN TIMESETTINGS"):
                    in_ped = True
                if not in_ped and lab in labels and len(r) >= 9:
                    for i, letter in enumerate("ABCDEFG"):
                        x = _cell_num(r[2 + i])
                        phases.setdefault(letter, {})[labels[lab]] = x
                elif in_ped and lab in ped_labels and len(r) >= 10:
                    for i in range(8):
                        x = _cell_num(r[2 + i])
                        peds.setdefault(f"P{i + 1}", {})[ped_labels[lab]] = x
            out["timings"] = phases
            out["pedestrians"] = peds
        for r in rows:
            cells = [c for c in r if c]
            for i, c in enumerate(cells[:-1]):
                nxt = cells[i + 1]
                if "sequence" in out or not re.fullmatch(r"[A-G][A-G0-9 ]*", nxt):
                    continue
                if c.upper() in ("V.A. SEQUENCE", "REVN. & V.A. SEQUENCE"):
                    out["sequence"], out["pages"]["sequence"] = nxt.strip(), tab.name
                elif c.upper().startswith("1 (NO Y+)") and "flexi_sequence" not in out:
                    out["flexi_sequence"] = nxt.strip()
        if "APPROACH DEFINITIONS" in flat and "approach_groups" not in out:
            out["pages"]["approach_definitions"] = tab.name
            ag: dict[str, list[str]] = {}
            for r in rows:
                cells = [c for c in r if c]
                if len(cells) >= 4 and re.fullmatch(r"\d+", cells[0]):
                    timer = next((c for c in cells if re.fullmatch(r"[A-G]\d\d", c)), None)
                    if timer:
                        g = cells[cells.index(timer) + 1]
                        if re.fullmatch(r"\d+", g) and g not in ag.setdefault(timer[0], []):
                            ag[timer[0]].append(g)
            out["approach_groups"] = ag
    if "sequence" not in out and "flexi_sequence" in out:
        out["sequence"] = out["flexi_sequence"]
        out["pages"]["sequence"] = (
            "FLEXILINK OPERATION, phase sequence 1 (no V.A. sequence printed)"
        )
    if "timings" in out:
        out["timings"] = {
            p: v for p, v in out["timings"].items() if any(x is not None for x in v.values())
        }
    if "pedestrians" in out:
        out["pedestrians"] = {
            p: v for p, v in out["pedestrians"].items() if any(x is not None for x in v.values())
        }
    return out


# --------------------------------------------------------------------------------------------
# Comparisons (each returns a list of {item, ai, ours, ok})


ZERO_IS_BLANK = {"special_all_red", "clearance_2"}  # 0 on the sheet == no such interval


def _eq(a, b, zero_is_blank: bool = False) -> bool:
    fa, fb = _num(a), _num(b)
    if zero_is_blank:
        fa = None if fa == 0 else fa
        fb = None if fb == 0 else fb
    if fa is None and fb is None:
        return True
    if fa is None or fb is None:
        return False
    return abs(fa - fb) < 1e-6


def _ai_timing(d: dict, phase: str, f: str):
    return (((d.get("timings") or {}).get("phases") or {}).get(phase) or {}).get(f)


def compare_timings(ai: dict, ours: dict[str, dict]) -> list[dict]:
    """`ours`: {phase: {field: seconds or None}} for every phase with a value on the sheet."""
    out = []
    for p, vals in ours.items():
        for f in TIMING_FIELDS:
            a, o = _ai_timing(ai, p, f), vals.get(f)
            ok = _eq(a, o, f in ZERO_IS_BLANK)
            out.append({"item": f"{p}.{f}", "ai": a, "ours": o, "ok": ok})
    return out


def compare_pedestrians(ai: dict, ours: dict[str, dict]) -> list[dict]:
    aiped = (ai.get("timings") or {}).get("pedestrians") or {}
    used = {k for k, v in aiped.items() if any(_num(v.get(f)) for f in PED_FIELDS)}
    out = []
    for p in sorted(set(ours) | used):
        for f in PED_FIELDS:
            a = (aiped.get(p) or {}).get(f)
            o = (ours.get(p) or {}).get(f)
            out.append(
                {"item": f"{p}.{f}", "ai": a, "ours": o, "ok": _eq(a, o, f in ZERO_IS_BLANK)}
            )
    return out


def seq_letters(seq: str | None) -> str:
    """'A B' / 'CAB1' / 'AB1CDF' -> phase letters only ('AB', 'CAB', 'ABCDF')."""
    return "".join(re.findall(r"[A-G]", seq or ""))


def compare_phases(
    ai: dict,
    sequence: str,
    by_phase: dict[str, list[str]] | None,
    peds: set[str] = frozenset(),
    phase_letters: list[str] | None = None,
) -> list[dict]:
    """Sequence (letters only) + the vehicle/tram/bicycle signal groups of each phase.

    Pedestrian groups are left out on both sides (the AI listed them inconsistently, as 'P1' or
    not at all). With `by_phase` None (groups not read), a phase for which the AI lists no group
    is still wrong (every phase runs at least one group); other phases are not compared."""
    ph = ai.get("phases") or {}
    a_seq = seq_letters(ph.get("sequence"))
    out = [
        {
            "item": "sequence",
            "ai": ph.get("sequence"),
            "ours": sequence,
            "ok": a_seq == seq_letters(sequence),
        }
    ]
    aisg = ph.get("signal_groups_by_phase") or {}

    def veh(gs):
        return sorted(
            {str(g) for g in gs or []}
            - set(peds)
            - {g for g in map(str, gs or []) if g.startswith("P")}
        )

    letters = list(by_phase) if by_phase is not None else (phase_letters or [])
    for p in letters:
        a = veh(aisg.get(p))
        if by_phase is None:
            if not a:
                out.append({"item": f"{p}.signal_groups", "ai": a, "ours": "not read", "ok": False})
            continue
        o = veh(by_phase.get(p))
        out.append({"item": f"{p}.signal_groups", "ai": a, "ours": o, "ok": a == o})
    return out


USE_CLASS = {
    "general": "car",
    "tram+general": "car",
    "bus+general": "car",
    "shared tram-service": "car",
    "tram": "tram",
    "tram+pedestrian mall": "tram",
    "bicycle": "bicycle",
    "bus": "bus",
}


def compare_lane_layout(
    ai_layout: dict | None, ours: dict[str, list[dict] | None] | None
) -> list[dict]:
    """Per lane; approaches matched by (street core, heading). An approach we could not read
    (None) is not compared; arrows we could not see (None) are not compared, only the use."""
    from pipeline.count_mapping import street_core

    def keyed(lay):
        out = {}
        for appr, lanes in (lay or {}).items():
            st, h = parse_approach(appr)
            out[(street_core(st), h)] = (
                appr,
                None if lanes is None else {ln["lane"]: ln for ln in lanes},
            )
        return out

    a, o = keyed(norm_lane_layout(ai_layout) if ai_layout else None), keyed(ours)
    res = []
    for k in sorted(set(a) | set(o), key=str):
        an, al = a.get(k, (None, {}))
        on, ol = o.get(k, (None, {}))
        if ol is None:
            continue
        name = on or an
        for lane in sorted(set(al) | set(ol), key=lambda x: (x is None, x)):
            x, y = al.get(lane), ol.get(lane)
            ok = bool(x and y)
            if ok:
                ok = USE_CLASS.get(x.get("use"), x.get("use")) == USE_CLASS.get(
                    y.get("use"), y.get("use")
                )
                if ok and y.get("arrows") is not None:
                    ok = x.get("arrows") == y.get("arrows")
            res.append(
                {
                    "item": f"{name} lane {lane}",
                    "ai": None if x is None else f"{x.get('use')}/{x.get('arrows') or '-'}",
                    "ours": None if y is None else f"{y.get('use')}/{y.get('arrows') or '-'}",
                    "ok": ok,
                }
            )
    return res


# --------------------------------------------------------------------------------------------
# Our own reading: pipeline/reference/signal_sheets_reread.txt (format in its header)


def _page(tok: str) -> str:
    m = re.fullmatch(r"p(\d+)", tok)
    if m:
        return f"PDF page {int(m.group(1))}"
    m = re.fullmatch(r"t(\d+)", tok)
    if m:
        return f"htm tab sheet{m.group(1)}.htm"
    return tok


def _val(tok: str) -> float | None:
    return None if tok == "-" else float(tok)


def _movements(spec: str) -> list[dict]:
    """'Elizabeth St SB LTR; La Trobe St EB T' -> [{street, heading, turn}, ...]."""
    out = []
    for part in spec.split(";"):
        words = part.split()
        if len(words) < 3:
            raise ValueError(f"bad movement {part!r}")
        street, heading, turns = " ".join(words[:-2]), words[-2], words[-1]
        if heading not in HEADINGS8 or not re.fullmatch(r"[LTRU]+", turns):
            raise ValueError(f"bad movement {part!r}")
        out += [{"street": street, "heading": heading, "turn": t} for t in turns]
    return out


KIND = {"v": "vehicle", "p": "pedestrian", "t": "tram", "b": "bicycle", "bus": "bus"}


def parse_reread(text: str) -> dict[int, dict]:
    """The reread file -> {site: {timings, pedestrians, phases, signal_groups, lane_layout,
    ped_groups, notes}}, each with its 'page'."""
    sites: dict[int, dict] = {}
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") and not line.startswith("##"):
            continue
        if line.startswith("## "):
            cur = sites.setdefault(int(line[3:].split()[0]), {"notes": []})
            continue
        kind, rest = line.split(" ", 1)
        if kind == "N":
            cur["notes"].append(rest)
            continue
        fields = [f.strip() for f in rest.split("|")]
        page, body = _page(fields[0]), fields[1:]
        if kind == "T":
            ph = {}
            for f in body:
                w = f.split()
                ph[w[0]] = dict(zip(TIMING_FIELDS, map(_val, w[1:6]), strict=True))
            cur["timings"] = {"page": page, "phases": ph}
        elif kind == "P":
            ped = {}
            for f in body:
                w = f.split()
                ped[w[0]] = dict(zip(PED_FIELDS, map(_val, w[1:4]), strict=True))
            cur["pedestrians"] = {"page": page, "values": ped}
        elif kind == "S":
            by = {w.split()[0]: w.split()[1:] for w in body[1:]}
            cur["phases"] = {"page": page, "sequence": body[0], "by_phase": by or None}
        elif kind == "Q":
            cur["ped_groups"] = set(body[0].split())
        elif kind == "G":
            groups = {}
            for f in body:
                w = f.split()
                g, k = w[0], KIND[w[1]]
                if k == "pedestrian":
                    groups[g] = {
                        "kind": k,
                        "ped": w[2],
                        "movements": [
                            {"street": " ".join(w[3:]), "heading": None, "turn": "crossing"}
                        ],
                    }
                else:
                    groups[g] = {"kind": k, "movements": _movements(" ".join(w[2:]))}
            cur["signal_groups"] = {"page": page, "groups": groups}
            cur["ped_groups"] = {g for g, v in groups.items() if v["kind"] == "pedestrian"}
        elif kind == "L":
            lay: dict[str, list[dict] | None] = {}
            for f in body:
                appr, lanes = (x.strip() for x in f.split(":", 1))
                street, h = parse_approach(appr)
                key = f"{street} {h}"
                if lanes == "?":
                    lay[key] = None
                    continue
                lay[key] = []
                for i, ln in enumerate(lanes.split(","), start=1):
                    use, arrows = ln.strip().split("/")
                    lay[key].append(
                        {"lane": i, "use": use, "arrows": None if arrows == "-" else arrows}
                    )
            cur["lane_layout"] = {"page": page, "approaches": lay}
        else:
            raise ValueError(f"unknown line type {kind!r}: {raw}")
    return sites


def error_rate(items: list[dict]) -> float | None:
    return None if not items else sum(not i["ok"] for i in items) / len(items)


def decision(rate: float | None) -> str:
    if rate is None:
        return "no_sample_items"
    if rate <= THRESH_LOW:
        return "use_ai_fix_errors"
    if rate <= THRESH_HIGH:
        return "reread_priority_others_unverified"
    return "reject_ai_reread_priority"


# --------------------------------------------------------------------------------------------
# Sample and priority sets


def draw_sample(
    with_sheet: list[int], lane_layout_sites: set[int], seed: int = SAMPLE_SEED
) -> list[int]:
    """25 sites: 2922 + one of 2902/2903/2904/2909, topped up to >= 12 lane_layout sites, then
    the rest at random from every site with a sheet. Deterministic for a seed."""
    rng = random.Random(seed)
    pool = sorted(with_sheet)
    picked = [s for s in SAMPLE_FORCED if s in pool]
    picked.append(rng.choice([s for s in SAMPLE_ONE_OF if s in pool]))
    ll = [s for s in pool if s in lane_layout_sites and s not in picked]
    n_ll = sum(s in lane_layout_sites for s in picked)
    picked += rng.sample(ll, max(0, SAMPLE_MIN_LANE_LAYOUT - n_ll))
    rest = [s for s in pool if s not in picked]
    picked += rng.sample(rest, SAMPLE_SIZE - len(picked))
    return sorted(picked)


def priority_sites(reg: dict, ai: dict, counts_sites: set[int]) -> list[int]:
    out = set(counts_sites)
    for s in ai:
        name = reg[s].name if s in reg else ai[s].get("site_name") or ""
        if PRIORITY_STREETS.search(name):
            out.add(s)
    return sorted(out)


def counts_sites(path: Path = COUNTS_PATH) -> set[int]:
    import pyarrow.parquet as pq

    with path.open("rb") as fh:
        return {
            int(x) for x in pq.read_table(fh, columns=["site_id"]).column("site_id").to_pylist()
        }


def sheet_file(site: int, sheets_raw: Path = SHEETS_RAW) -> Path | None:
    for ext in ("pdf", "htm"):
        hits = sorted(sheets_raw.glob(f"*/{site}.{ext}"))
        if hits:
            return hits[0]
    return None


# --------------------------------------------------------------------------------------------
# Network checks (sumolib + OSM turn:lanes)


def osm_turn_lanes(path: Path = OSM_PATH) -> dict[str, dict[str, str]]:
    """way id -> {'turn:lanes': ..., 'turn:lanes:forward': ..., ...} for ways that have them."""
    import osmium

    out = {}
    for w in osmium.FileProcessor(str(path), osmium.osm.WAY):
        tags = {k: v for k, v in w.tags if k.startswith("turn:lanes")}
        if tags:
            out[str(w.id)] = tags | {"oneway": w.tags.get("oneway", "")}
    return out


def osm_arrows(tags: dict[str, str], forward: bool = True) -> list[str] | None:
    """OSM turn:lanes (left to right in the direction of travel) -> per-lane turn letters."""
    v = tags.get("turn:lanes") or tags.get(
        "turn:lanes:forward" if forward else "turn:lanes:backward"
    )
    if not v:
        return None
    lanes = []
    for part in v.split("|"):
        letters = {OSM_TURN.get(w.strip()) for w in part.split(";")}
        lanes.append("".join(c for c in "LTRU" if c in letters) or "T")
    return lanes


def net_checks(site: int, ai: dict, sj, index, turn_tags: dict) -> dict:
    """Junction street names, lane counts, OSM arrows and headings for one site's lane_layout."""
    from pipeline.count_mapping import HEADINGS, angle_diff, names_match

    res: dict = {"junction": None, "approaches": []}
    if sj is None:
        res["junction"] = "no SUMO traffic-light node within 60 m"
        return res
    names = sorted({a.name for a in sj.approaches if a.name})
    res["junction"] = {"nodes": sj.nodes, "approach_names": names, "dist_m": sj.dist_m}
    layout = norm_lane_layout(ai.get("lane_layout")) or {}
    for appr, lanes in layout.items():
        street, h = parse_approach(appr)
        r: dict = {"approach": appr}
        named = [a for a in sj.approaches if names_match(a.name, street)]
        r["street_at_junction"] = bool(named) or any(names_match(n, street) for n in names)
        if h is None:
            r["heading"] = "no heading"
        else:
            same = [a for a in named if angle_diff(a.bearing, HEADINGS[h]) <= 50]
            opp = [a for a in named if angle_diff(a.bearing, HEADINGS[OPPOSITE[h]]) <= 50]
            if same:
                r["heading"] = "ok"
            elif opp:
                r["heading"] = "REVERSED (net has only the opposite heading on this street)"
            elif named:
                r["heading"] = (
                    f"no {h} approach on this street (bearings {[a.bearing for a in named]})"
                )
            else:
                r["heading"] = "street not found among approach edges"
            if same:
                e = max(same, key=lambda a: a.lanes)
                car = [
                    ln
                    for ln in lanes
                    if ln.get("use") in ("general", "tram+general", "bus+general")
                ]
                r["edge_id"] = e.edge_id
                r["lanes_ai_general"] = len(car)
                r["lanes_sumo"] = e.lanes
                r["lane_count"] = "ok" if len(car) == e.lanes else "differs"
                osm = None
                for w in e.way_ids:
                    if w in turn_tags:
                        osm = osm_arrows(turn_tags[w])
                        break
                if osm is not None and car:
                    ai_arr = [ln["arrows"] for ln in sorted(car, key=lambda x: x["lane"] or 0)]
                    r["arrows_ai"], r["arrows_osm"] = ai_arr, osm
                    r["arrows"] = "ok" if ai_arr == osm else "differs"
                else:
                    r["arrows"] = "no OSM turn:lanes"
        res["approaches"].append(r)
    return res


# --------------------------------------------------------------------------------------------
# Misc


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def snapshot_manifest(ai_dir: Path = AI_DIR) -> Path:
    """MANIFEST.json for the AI snapshot (file -> sha256, bytes); written once."""
    mpath = ai_dir / "MANIFEST.json"
    if mpath.exists():
        return mpath
    files = {
        str(p.relative_to(ai_dir)): {"bytes": p.stat().st_size, "sha256": sha256(p)}
        for p in sorted(ai_dir.rglob("*"))
        if p.is_file() and p.name != "MANIFEST.json"
    }
    mpath.write_text(
        json.dumps(
            {
                "copied_from": "~/Downloads/signal_sheets",
                "snapshot_date": SNAPSHOT_DATE,
                "files": files,
            },
            indent=1,
        )
    )
    return mpath


# --------------------------------------------------------------------------------------------
# Our values per site (own reading first, then the htm text tables)


def our_values(site: int, reread: dict[int, dict], htm_cache: dict | None = None) -> dict:
    """{timings, pedestrians, sequence, by_phase, signal_groups, lane_layout, ped_groups, pages}
    from our own reading; missing keys = not read."""
    r = reread.get(site, {})
    out: dict = {"pages": {}, "notes": r.get("notes", [])}
    f = sheet_file(site)
    h = {}
    if f is not None and f.suffix == ".htm":
        if htm_cache is not None and site in htm_cache:
            h = htm_cache[site]
        else:
            h = parse_htm_sheet(f)
            if htm_cache is not None:
                htm_cache[site] = h
    if "timings" in r:
        out["timings"], out["pages"]["timings"] = r["timings"]["phases"], r["timings"]["page"]
    elif h.get("timings"):
        out["timings"] = h["timings"]
        out["pages"]["timings"] = (
            f"htm tab {h['pages']['timings']} (CONTROLLER TIMESETTINGS - 1, text)"
        )
    if "pedestrians" in r:
        out["pedestrians"] = r["pedestrians"]["values"]
        out["pages"]["pedestrians"] = r["pedestrians"]["page"]
    elif "pedestrians" in h and "timings" in h.get("pages", {}):
        out["pedestrians"] = h["pedestrians"]
        out["pages"]["pedestrians"] = (
            f"htm tab {h['pages']['timings']} (PEDESTRIAN TIMESETTINGS, text)"
        )
    if "phases" in r:
        out["sequence"], out["by_phase"] = r["phases"]["sequence"], r["phases"]["by_phase"]
        out["pages"]["phases"] = r["phases"]["page"]
    elif h.get("sequence"):
        out["sequence"], out["by_phase"] = h["sequence"], None
        out["pages"]["phases"] = f"htm tab {h['pages']['sequence']} (V.A. SEQUENCE, text)"
    if "signal_groups" in r:
        out["signal_groups"] = r["signal_groups"]["groups"]
        out["pages"]["signal_groups"] = r["signal_groups"]["page"]
    if "lane_layout" in r:
        out["lane_layout"] = r["lane_layout"]["approaches"]
        out["pages"]["lane_layout"] = r["lane_layout"]["page"]
    out["ped_groups"] = r.get("ped_groups", set())
    return out


def compare_site(ai: dict, ours: dict) -> dict[str, list[dict]]:
    res: dict[str, list[dict]] = {}
    if "timings" in ours:
        res["timings"] = compare_timings(ai, ours["timings"])
    if "pedestrians" in ours:
        res["pedestrians"] = compare_pedestrians(ai, ours["pedestrians"])
    if "sequence" in ours:
        res["phases"] = compare_phases(
            ai,
            ours["sequence"],
            ours.get("by_phase"),
            ours["ped_groups"],
            list(ours.get("timings") or {}),
        )
    if "lane_layout" in ours and ai.get("lane_layout"):
        res["lane_layout"] = compare_lane_layout(ai.get("lane_layout"), ours["lane_layout"])
    return res


# --------------------------------------------------------------------------------------------
# Output


def _rel(p: Path | None) -> str | None:
    return None if p is None else str(p.relative_to(REPO_ROOT))


def _ai_timing_vals(ai: dict) -> dict[str, dict]:
    return {
        p: {f: _num(v.get(f)) for f in TIMING_FIELDS}
        for p, v in ((ai.get("timings") or {}).get("phases") or {}).items()
        if isinstance(v, dict)
    }


def _ai_peds(ai: dict) -> dict[str, dict]:
    return {
        p: {f: _num(v.get(f)) for f in PED_FIELDS}
        for p, v in ((ai.get("timings") or {}).get("pedestrians") or {}).items()
        if isinstance(v, dict) and any(_num(v.get(f)) is not None for f in PED_FIELDS)
    }


def _ai_lanes(ai: dict) -> dict | None:
    lay = norm_lane_layout(ai.get("lane_layout"))
    if not lay:
        return None
    out = {}
    for appr, lanes in lay.items():
        st, h = parse_approach(appr)
        out[f"{st} {h}"] = [{**ln, "arrows": ln["arrows"] or None} for ln in lanes]
    return out


def resolve(site: int, ai: dict, ours: dict, decisions: dict[str, str], is_priority: bool) -> dict:
    """One output object: our reading where we have it, else the AI value per the decision."""
    f = sheet_file(site)
    ai_page = "AI output; sheet page not recorded by the AI"
    prov: dict[str, dict] = {}
    if f is None:
        none = {
            "status": "no_sheet",
            "page": None,
            "notes": "no configuration sheet in the DTP ZIPs",
        }
        return {
            "site_id": str(site),
            "site_name": ai.get("site_name"),
            "sheet_date": None,
            "sheet_file": None,
            "phases": None,
            "signal_groups": None,
            "pedestrians": None,
            "lane_layout": None,
            "provenance": {g: dict(none) for g in (*FIELD_GROUPS, "signal_groups")},
        }

    def pick(group: str, our_val, ai_val, our_page: str | None):
        if our_val is not None:
            note = (
                "sheet text tables (htm), parsed"
                if our_page and "text" in our_page
                else "read from the sheet image"
            )
            prov[group] = {"status": "re_read", "page": our_page, "notes": note}
            return our_val
        d = decisions[group]
        if d == "use_ai_fix_errors":
            prov[group] = {
                "status": "ai_checked",
                "page": ai_page,
                "notes": "AI value; field group error rate <= 5 % in the sample",
            }
            return ai_val
        if d == "reread_priority_others_unverified":
            note = "AI value, not verified (sample error rate 5-15 %)"
            if is_priority:
                note += "; PRIORITY SITE NOT YET RE-READ"
            prov[group] = {"status": "ai_unverified", "page": ai_page, "notes": note}
            return ai_val
        prov[group] = {
            "status": "rejected",
            "page": None,
            "notes": "AI field group rejected (sample error rate > 15 %); "
            "not re-read (not a priority site)"
            + ("; PRIORITY SITE NOT YET RE-READ" if is_priority else ""),
        }
        return None

    timings = pick(
        "timings", ours.get("timings"), _ai_timing_vals(ai) or None, ours["pages"].get("timings")
    )
    peds = pick(
        "pedestrians",
        ours.get("pedestrians"),
        _ai_peds(ai) or None,
        ours["pages"].get("pedestrians"),
    )
    lanes_ai = _ai_lanes(ai)
    lane_layout = None
    if "lane_layout" in ours:
        lane_layout = {k: v for k, v in ours["lane_layout"].items() if v is not None} or None
        unread = [k for k, v in ours["lane_layout"].items() if v is None]
        prov["lane_layout"] = {
            "status": "re_read",
            "page": ours["pages"]["lane_layout"],
            "notes": "read from the detector map"
            + (f"; not legible, left out: {', '.join(unread)}" if unread else ""),
        }
    elif lanes_ai is None:
        prov["lane_layout"] = {
            "status": "not_read",
            "page": None,
            "notes": "no lane layout in the AI output and not re-read",
        }
    else:
        lane_layout = pick("lane_layout", None, lanes_ai, None)
        if lane_layout is not None:
            prov["lane_layout"]["page"] = "PDF page 2 (DETECTOR MAP), per the AI's source_image"

    # phases: sequence + groups per phase (ours), timings merged in per phase
    if "sequence" in ours:
        seq, by = ours["sequence"], ours.get("by_phase")
        prov["phases"] = {
            "status": "re_read",
            "page": ours["pages"]["phases"],
            "notes": "sequence and signal groups per phase read from the phasing diagram"
            if by is not None
            else "V.A. sequence from the sheet text; signal groups per phase not read",
        }
    else:
        ph = ai.get("phases") or {}
        seq = pick("phases", None, ph.get("sequence"), None)
        by = None
        if seq is not None:
            by = {
                p: [str(g) for g in gs]
                for p, gs in (ph.get("signal_groups_by_phase") or {}).items()
            }
    phases = None
    if seq is not None or timings:
        letters = sorted(set(seq_letters(seq)) | set(timings or {}) | set(by or {}))
        phases = {"sequence": seq, "by_phase": {}}
        for p in letters:
            t = (timings or {}).get(p) or {}
            phases["by_phase"][p] = {
                "signal_groups": None if by is None or p not in by else [str(g) for g in by[p]],
                "min_green_s": t.get("minimum_green"),
                "max_green_s": t.get("maximum_extension_green"),
                "yellow_s": t.get("yellow"),
                "all_red_s": t.get("all_red"),
            }
    if "signal_groups" in ours:
        sg = ours["signal_groups"]
        prov["signal_groups"] = {
            "status": "re_read",
            "page": ours["pages"]["signal_groups"],
            "notes": "read from the GROUP ALLOCATION diagram",
        }
    else:
        sg = None
        prov["signal_groups"] = {
            "status": "not_read",
            "page": None,
            "notes": "the AI left movements null; read for priority sites only",
        }
    if ours.get("notes"):
        prov["notes"] = " | ".join(ours["notes"])
    pedestrians = None
    if peds:
        pedestrians = {
            p: {
                "walk_s": v.get("walk"),
                "clearance_1_s": v.get("clearance_1"),
                "clearance_2_s": v.get("clearance_2"),
            }
            for p, v in sorted(peds.items())
        }
    return {
        "site_id": str(site),
        "site_name": ai.get("site_name"),
        "sheet_date": ai.get("document_date"),
        "sheet_file": _rel(f),
        "phases": phases,
        "signal_groups": sg,
        "pedestrians": pedestrians,
        "lane_layout": lane_layout,
        "provenance": prov,
    }


def site_name_streets(name: str | None) -> list[str]:
    """'ELIZABETH STREET / LA TROBE STREET' -> both; mid-block 'X NR Y' -> ['X']."""
    if not name:
        return []
    n = re.split(r"\b(?:NR|NEAR|BTW|AT|NORTH OF|SOUTH OF|EAST OF|WEST OF)\b", name, flags=re.I)[0]
    return [p.strip() for p in re.split(r"[/&]", n) if p.strip()]


def automatic_checks(ai: dict[int, dict], reg: dict, with_net: bool = True) -> dict[str, dict]:
    from pipeline.count_mapping import names_match

    index = turn_tags = None
    if with_net:
        from pipeline.count_mapping import NetIndex

        index, turn_tags = NetIndex(), osm_turn_lanes()
    out = {}
    for s, d in sorted(ai.items()):
        r: dict = {
            "schema": check_schema(d),
            "ranges": check_ranges(d),
            "phase_consistency": check_phase_consistency(d),
            "register_bbox": check_register(s, reg),
            "sheet": _rel(sheet_file(s)),
        }
        f = sheet_file(s)
        if f is not None and f.suffix == ".htm":
            h = parse_htm_sheet(f)
            txt = {}
            for g, items in (
                ("timings", compare_timings(d, h.get("timings", {}))),
                ("pedestrians", compare_pedestrians(d, h.get("pedestrians", {}))),
                (
                    "sequence",
                    compare_phases(d, h.get("sequence", ""), None)[:1] if h.get("sequence") else [],
                ),
            ):
                txt[g] = {"items": len(items), "wrong": [i for i in items if not i["ok"]]}
            r["htm_text_crosscheck"] = txt
        if index is not None and s in reg and f is not None:
            sj = index.site_junction(s, reg[s].lon, reg[s].lat)
            net = net_checks(s, d, sj, index, turn_tags)
            if sj is not None:
                names = net["junction"]["approach_names"]
                streets = site_name_streets(d.get("site_name") or reg[s].name)
                net["site_name_streets_at_junction"] = {
                    st: any(names_match(n, st) for n in names) for st in streets
                }
            r["net"] = net
        out[str(s)] = r
    return out


def summarise_checks(checks: dict[str, dict]) -> dict:
    def count(pred):
        return sum(1 for r in checks.values() if pred(r))

    appr = [a for r in checks.values() for a in (r.get("net") or {}).get("approaches", [])]
    return {
        "sites": len(checks),
        "schema_problems": count(lambda r: r["schema"]),
        "range_outliers": count(lambda r: r["ranges"]),
        "phase_inconsistent": count(lambda r: r["phase_consistency"]),
        "not_in_register_or_outside_bbox": count(lambda r: r["register_bbox"]),
        "no_sheet": count(lambda r: r["sheet"] is None),
        "no_sumo_junction": count(lambda r: isinstance((r.get("net") or {}).get("junction"), str)),
        "site_name_street_missing_at_junction": count(
            lambda r: (
                not all(((r.get("net") or {}).get("site_name_streets_at_junction") or {}).values())
            )
        ),
        "htm_sites_with_text_mismatch": {
            g: count(
                lambda r, g=g: bool(
                    ((r.get("htm_text_crosscheck") or {}).get(g) or {}).get("wrong")
                )
            )
            for g in ("timings", "pedestrians", "sequence")
        },
        "lane_layout_approaches": len(appr),
        "approach_street_not_at_junction": sum(1 for a in appr if not a.get("street_at_junction")),
        "approach_heading": {
            k: sum(1 for a in appr if (a.get("heading") or "").startswith(k))
            for k in ("ok", "REVERSED", "no ", "street not found")
        },
        "lane_count_vs_sumo": {
            k: sum(1 for a in appr if a.get("lane_count") == k) for k in ("ok", "differs")
        },
        "arrows_vs_osm_turn_lanes": {
            k: sum(1 for a in appr if a.get("arrows") == k)
            for k in ("ok", "differs", "no OSM turn:lanes")
        },
    }


def build(with_net: bool = True) -> tuple[list[dict], dict]:
    from pipeline.scats import load_sites

    ai, reg = load_ai(), load_sites()
    reread = parse_reread(REREAD_PATH.read_text())
    with_sheet = [s for s in ai if ai[s].get("file_format")]  # a sheet exists in the DTP ZIPs
    ll_sites = {s for s in ai if ai[s].get("lane_layout")}
    sample = draw_sample(with_sheet, ll_sites)
    prio = set(priority_sites(reg, ai, counts_sites()))
    cache: dict = {}
    ours = {s: our_values(s, reread, cache) for s in ai}

    per_site, items = {}, {g: [] for g in FIELD_GROUPS}
    for s in sample:
        c = compare_site(ai[s], ours[s])
        per_site[str(s)] = {
            g: {"items": len(v), "wrong": [i for i in v if not i["ok"]]} for g, v in c.items()
        }
        for g, v in c.items():
            items[g] += v
    rates = {g: error_rate(v) for g, v in items.items()}
    decisions = {g: decision(r) for g, r in rates.items()}
    out = [resolve(s, ai[s], ours[s], decisions, s in prio) for s in sorted(ai)]

    prov_counts = {
        g: dict(sorted(Counter(o["provenance"][g]["status"] for o in out).items()))
        for g in (*FIELD_GROUPS, "signal_groups")
    }
    prio_gaps = {
        g: sorted(
            int(o["site_id"])
            for o in out
            if "PRIORITY SITE NOT YET RE-READ" in o["provenance"][g].get("notes", "")
        )
        for g in FIELD_GROUPS
    }
    checks = automatic_checks(ai, reg, with_net)
    validation = {
        "description": "Validation of George's AI reading of the DTP signal configuration sheets "
        "(task 3C). Automatic checks on all sites, a random re-read sample, error rates per "
        "field group and the fixed decision rule applied. Our readings: "
        "pipeline/reference/signal_sheets_reread.txt; htm sheets' timing tables are text "
        "and are parsed directly.",
        "ai_snapshot": f"data/raw/signal_sheets_ai/ (copied unchanged from "
        f"~/Downloads/signal_sheets on {SNAPSHOT_DATE})",
        "rule": {
            "thresholds": {
                "use_ai_fix_errors": "<= 5 %",
                "reread_priority_others_unverified": "5-15 %",
                "reject_ai_reread_priority": "> 15 %",
            },
            "items": ITEM_RULES,
            "missing_ai_value": "a value on the sheet that the AI left empty counts as wrong",
        },
        "sample": {
            "seed": SAMPLE_SEED,
            "method": "random.Random(seed): 2922 + one of 2902/2903/2904/2909, topped up to "
            ">= 12 lane_layout sites, rest from all sites with a sheet",
            "frame": len(with_sheet),
            "sites": sample,
            "lane_layout_sites_in_sample": sum(s in ll_sites for s in sample),
            "per_site": per_site,
        },
        "error_rates": {g: None if r is None else round(r, 4) for g, r in rates.items()},
        "items_compared": {g: len(v) for g, v in items.items()},
        "items_wrong": {g: sum(not i["ok"] for i in v) for g, v in items.items()},
        "decisions": decisions,
        "priority_sites": sorted(prio),
        "priority_sites_not_yet_re_read": prio_gaps,
        "provenance_counts": prov_counts,
        "automatic_checks_summary": summarise_checks(checks),
        "automatic_checks": checks,
    }
    return out, validation


def write(out: list[dict], validation: dict) -> None:
    OUT_PATH.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    CHECKS_DIR.mkdir(parents=True, exist_ok=True)
    VALIDATION_PATH.write_text(
        json.dumps(validation, indent=1, ensure_ascii=False, default=sorted) + "\n"
    )


def main() -> int:
    snapshot_manifest()
    out, validation = build()
    write(out, validation)
    from pipeline.signal_sheets_spotcheck import build as build_spotcheck

    build_spotcheck()
    print(
        json.dumps(
            {
                k: validation[k]
                for k in (
                    "error_rates",
                    "decisions",
                    "provenance_counts",
                    "priority_sites_not_yet_re_read",
                )
            },
            indent=1,
        )
    )
    print(json.dumps(validation["automatic_checks_summary"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
