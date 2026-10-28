"""George's spot-check pack for the signal-sheet reference (checks/signal_sheets_spotcheck/).

Ten sites: five lane layouts (DETECTOR MAP part of PDF page 2 beside the lanes we recorded) and
five timing/phase sets (CONTROLLER TIMESETTINGS page and PHASING DIAGRAM page beside the
phases, timings and signal groups we recorded, with the AI's values for comparison). Page images
come from OUR downloaded copy of the DTP sheets, downscaled with `sips` (as counts_spotcheck).
Verification output only; nothing here feeds a contract file.

Run: uv run python -m pipeline.signal_sheets_spotcheck
"""

from __future__ import annotations

import html
import json
import re
import subprocess
from pathlib import Path

from pipeline.sheet_pages import page_jpegs
from pipeline.signal_sheets import (
    CHECKS_DIR,
    OUT_PATH,
    REPO_ROOT,
    load_ai,
    norm_lane_layout,
    sheet_file,
)

OUT_DIR = CHECKS_DIR / "signal_sheets_spotcheck"
LANE_SITES = (2922, 2903, 2916, 4567, 4583)
TIMING_SITES = (2901, 2924, 2926, 4525, 4585)


def _page_no(page: str | None) -> int | None:
    m = re.search(r"PDF page (\d+)", page or "")
    return int(m.group(1)) if m else None


def page_image(pdf: Path, page: int, dest: Path, crop: tuple[float, float] | None = None) -> Path:
    """Page `page` (1-based) as a downscaled JPEG; `crop` = (top, bottom) fractions."""
    data = page_jpegs(pdf.read_bytes())[page - 1]
    tmp = dest.with_suffix(".full.jpg")
    tmp.write_bytes(data)
    try:
        if crop:
            out = subprocess.run(
                ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(tmp)],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.split()
            w, h = int(out[-3]), int(out[-1])
            top, bottom = crop
            subprocess.run(
                ["sips", "-c", str(int(h * (bottom - top))), str(w)]
                + ["--cropOffset", str(int(h * top)), "0", str(tmp), "--out", str(tmp)],
                check=True,
                capture_output=True,
            )
        subprocess.run(
            ["sips", "-Z", "1200", "-s", "formatOptions", "60", str(tmp), "--out", str(dest)],
            check=True,
            capture_output=True,
        )
    finally:
        tmp.unlink(missing_ok=True)
    return dest


def _table(rows: list[list[str]], head: list[str]) -> str:
    h = "".join(f"<th>{html.escape(c)}</th>" for c in head)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>" for r in rows
    )
    return f"<table><tr>{h}</tr>{body}</table>"


def _fmt(v) -> str:
    return "-" if v is None else f"{v:g}" if isinstance(v, float) else str(v)


def lanes_html(site: dict, ai: dict) -> str:
    rows = []
    for appr, lanes in (site["lane_layout"] or {}).items():
        rows.append([appr, ", ".join(f"{ln['use']}/{ln['arrows'] or '-'}" for ln in lanes)])
    ai_rows = [
        [a, ", ".join(f"{ln['use']}/{ln['arrows'] or '-'}" for ln in lanes)]
        for a, lanes in (norm_lane_layout(ai.get("lane_layout")) or {}).items()
    ]
    return (
        "<h4>Our reading (lane 1 = left kerb; use/arrows, '-' = no arrow drawn)</h4>"
        + _table(rows, ["approach", "lanes"])
        + "<h4>AI first pass (rejected for lane layouts)</h4>"
        + _table(ai_rows, ["approach", "lanes"])
    )


def timings_html(site: dict, ai: dict) -> str:
    ph = site["phases"] or {"sequence": None, "by_phase": {}}
    rows = [
        [p, _fmt(v["min_green_s"]), _fmt(v["max_green_s"]), _fmt(v["yellow_s"])]
        + [_fmt(v["all_red_s"]), " ".join(v["signal_groups"] or ["(not read)"])]
        for p, v in ph["by_phase"].items()
    ]
    groups = [
        [
            g,
            v["kind"],
            "; ".join(f"{m['street']} {m['heading'] or ''} {m['turn']}" for m in v["movements"]),
        ]
        for g, v in sorted((site["signal_groups"] or {}).items(), key=lambda x: int(x[0]))
    ]
    aiph = ai.get("phases") or {}
    ait = (ai.get("timings") or {}).get("phases") or {}
    ai_rows = [
        [p, _fmt(v.get("minimum_green")), _fmt(v.get("maximum_extension_green"))]
        + [_fmt(v.get("yellow")), _fmt(v.get("all_red"))]
        + [" ".join((aiph.get("signal_groups_by_phase") or {}).get(p) or [])]
        for p, v in sorted(ait.items())
    ]
    return (
        f"<h4>Our reading: V.A. sequence {html.escape(str(ph['sequence']))}</h4>"
        + _table(rows, ["phase", "min green", "max ext green", "yellow", "all red", "groups"])
        + "<h4>Signal groups</h4>"
        + _table(groups, ["SG", "kind", "movements"])
        + f"<h4>AI first pass: sequence {html.escape(str(aiph.get('sequence')))}</h4>"
        + _table(ai_rows, ["phase", "min green", "max ext green", "yellow", "all red", "groups"])
    )


def build() -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ref = {int(o["site_id"]): o for o in json.loads(OUT_PATH.read_text())}
    ai = load_ai()
    parts = [
        "<html><head><meta charset='utf-8'><title>Signal sheet spot-check</title><style>"
        "body{font:13px sans-serif;margin:16px} .s{display:flex;gap:16px;border-top:2px solid #333;"
        "padding:10px 0} img{max-width:620px;border:1px solid #ccc} table{border-collapse:collapse;"
        "margin-bottom:8px} td,th{border:1px solid #bbb;padding:2px 6px;text-align:left}</style>"
        "</head><body><h1>Signal sheet spot-check (task 3C)</h1><p>Each site: the page of our own "
        "copy of the DTP configuration sheet (CC BY 4.0) beside the values in "
        "<code>pipeline/reference/signal_sheets_verified.json</code>. Please tick or correct.</p>"
    ]
    for kind, sites in (("Lane layout", LANE_SITES), ("Timings and phases", TIMING_SITES)):
        parts.append(f"<h2>{kind}</h2>")
        for s in sites:
            o, pdf = ref[s], sheet_file(s)
            imgs = []
            if kind == "Lane layout":
                p = _page_no(o["provenance"]["lane_layout"]["page"]) or 2
                imgs.append(page_image(pdf, p, OUT_DIR / f"{s}_lanes_p{p}.jpg", (0.5, 0.97)))
                right = lanes_html(o, ai[s])
            else:
                for g in ("timings", "phases", "signal_groups"):
                    p = _page_no(o["provenance"][g]["page"])
                    name = OUT_DIR / f"{s}_p{p}.jpg"
                    if p and name not in imgs:
                        imgs.append(page_image(pdf, p, name))
                right = timings_html(o, ai[s])
            pages = ", ".join(
                f"{g}: {o['provenance'][g]['page']}"
                for g in ("timings", "phases", "signal_groups", "lane_layout")
                if o["provenance"][g].get("page")
            )
            imgtags = "".join(f"<div><img src='{i.name}'></div>" for i in imgs)
            name = html.escape(o["site_name"] or "")
            notes = html.escape(o["provenance"].get("notes", ""))
            parts.append(
                f"<div class='s'><div>{imgtags}</div><div><h3>{s} {name}</h3>"
                f"<p>{html.escape(str(o['sheet_file']))}, sheet date {o['sheet_date']}<br>"
                f"{html.escape(pages)}</p>{right}<p><i>{notes}</i></p></div></div>"
            )
    parts.append("</body></html>")
    out = OUT_DIR / "index.html"
    out.write_text("\n".join(parts))
    return out


if __name__ == "__main__":
    p = build()
    print(p.relative_to(REPO_ROOT), sum(f.stat().st_size for f in p.parent.iterdir()) // 1000, "kB")
