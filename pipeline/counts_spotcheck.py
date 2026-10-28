"""George's spot-check pack for the SCATS mapping: data/processed/checks/counts_spotcheck/.

For each site in SPOTCHECK_SITES:
- `<site>_sheet.jpg`: the DETECTOR MAP part of page 2 of OUR downloaded configuration sheet
  (PDF sheets only; an htm sheet is linked instead: open its Group Allocation tab in a browser);
- `<site>_edges.svg`: the SUMO network within RADIUS_M of the site, with each mapped approach
  edge drawn in colour and labelled `<heading> dets [...]` (grey = other edges);
- `index.html`: both side by side, with the approach table, for a quick visual check.
Verification output only; nothing here feeds a contract file.
"""

from __future__ import annotations

import html
import json
import math
import subprocess
from pathlib import Path

from pipeline.sheet_pages import page_jpegs

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "data" / "processed" / "checks" / "counts_spotcheck"
SPOTCHECK_SITES = (
    2397,
    2902,
    2903,
    2908,
    2910,
    2922,
    2929,
    4437,
    4560,
    4566,
    4567,
    4584,
    4600,
    4880,
    4881,
)
RADIUS_M = 140.0
COLOURS = ("#d62728", "#1f77b4", "#2ca02c", "#9467bd", "#ff7f0e", "#8c564b")


def sheet_crop(pdf: Path, dest: Path, max_px: int = 1100) -> Path:
    """Lower part of PDF page 2 (where the DETECTOR MAP is drawn), downscaled with `sips`."""
    pages = page_jpegs(pdf.read_bytes())
    tmp = dest.with_suffix(".full.jpg")
    tmp.write_bytes(pages[1] if len(pages) > 1 else pages[0])
    try:
        out = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(tmp)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        w, h = int(out[-3]), int(out[-1])
        subprocess.run(
            ["sips", "-c", str(h * 45 // 100), str(w), "--cropOffset", str(h * 52 // 100), "0"]
            + [str(tmp), "--out", str(tmp)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["sips", "-Z", str(max_px), "-s", "formatOptions", "60", str(tmp), "--out", str(dest)],
            check=True,
            capture_output=True,
        )
    finally:
        tmp.unlink(missing_ok=True)
    return dest


def edges_svg(index, site_xy: tuple[float, float], mapped: list[dict], dest: Path) -> Path:
    """SVG of the SUMO edges near the site; mapped approach edges coloured and labelled."""
    cx, cy = site_xy
    size, scale = 600, 600 / (2 * RADIUS_M)

    def tr(x: float, y: float) -> tuple[float, float]:
        return (size / 2 + (x - cx) * scale, size / 2 - (y - cy) * scale)

    colour_of = {m["edge_id"]: COLOURS[i % len(COLOURS)] for i, m in enumerate(mapped)}
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}" font-family="sans-serif">',
        f'<rect width="{size}" height="{size}" fill="white"/>',
    ]
    for e in index.net.getEdges():
        if e.getFunction() == "internal":
            continue
        shp = e.getShape()
        if min(math.hypot(x - cx, y - cy) for x, y in shp) > RADIUS_M:
            continue
        pts = " ".join(f"{a:.1f},{b:.1f}" for a, b in (tr(x, y) for x, y in shp))
        col = colour_of.get(e.getID())
        width = 5 if col else 1.5
        parts.append(
            f'<polyline points="{pts}" fill="none" stroke="{col or "#bbb"}" '
            f'stroke-width="{width}" marker-end="url(#a)"/>'
        )
        if col:
            lx, ly = tr(*shp[len(shp) // 2])
            label = next(
                f"{m['direction']} dets {m['detectors']}"
                for m in mapped
                if m["edge_id"] == e.getID()
            )
            parts.append(
                f'<text x="{lx + 4:.0f}" y="{ly - 4:.0f}" font-size="13" fill="{col}">'
                f"{html.escape(label)}</text>"
            )
    sx, sy = tr(cx, cy)
    parts.append(f'<circle cx="{sx:.0f}" cy="{sy:.0f}" r="5" fill="black"/>')
    parts.append('<text x="10" y="20" font-size="13">N ↑ (SUMO map, metres)</text>')
    parts.insert(
        2,
        '<defs><marker id="a" markerUnits="userSpaceOnUse" markerWidth="12" '
        'markerHeight="12" refX="9" refY="4" '
        'orient="auto"><path d="M0,0 L10,4 L0,8 z" fill="#555"/></marker></defs>',
    )
    parts.append("</svg>")
    dest.write_text("\n".join(parts) + "\n")
    return dest


def build(index, report: dict, ref: dict, sites_reg: dict) -> list[dict]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    day = report["per_day"][0]
    by_site: dict[str, list[dict]] = {}
    for a in day["approaches_used"]:
        by_site.setdefault(a["site_id"], []).append(a)
    sheet_of = {s["site"]: s for s in ref["sites"]}
    entries = []
    for site in SPOTCHECK_SITES:
        s = sheet_of[site]
        mapped = by_site.get(str(site), [])
        sheet = REPO_ROOT / s["sheet_file"]
        img = None
        if sheet.suffix == ".pdf":
            img = sheet_crop(sheet, OUT_DIR / f"{site}_sheet.jpg").name
        reg = sites_reg[site]
        svg = edges_svg(index, index.xy(reg.lon, reg.lat), mapped, OUT_DIR / f"{site}_edges.svg")
        entries.append(
            {
                "site": site,
                "name": reg.name,
                "sheet_file": s["sheet_file"],
                "sheet_page": s["sheet_page"],
                "sheet_image": f"data/processed/checks/counts_spotcheck/{img}" if img else None,
                "edges_image": f"data/processed/checks/counts_spotcheck/{svg.name}",
                "approaches": [
                    {
                        k: a[k]
                        for k in ("approach_id", "street", "direction", "detectors", "edge_id")
                    }
                    for a in mapped
                ],
                "note": s["note"],
                "george_ai_comparison": s["george_ai_comparison"],
                "george_verdict": None,
            }
        )
    rows = []
    for e in entries:
        apps = "<br>".join(
            html.escape(f"{a['street']} {a['direction']}: dets {a['detectors']} -> {a['edge_id']}")
            for a in e["approaches"]
        )
        sheet = (
            f'<img src="{Path(e["sheet_image"]).name}" width="560">'
            if e["sheet_image"]
            else f"htm sheet: open <code>{html.escape(e['sheet_file'])}</code> "
            "(Group Allocation tab) in a browser"
        )
        rows.append(
            f"<tr><td><b>{e['site']}</b><br>{html.escape(e['name'])}<br><small>{apps}</small>"
            f"<br><small>{html.escape(e['note'])}</small></td><td>{sheet}</td>"
            f'<td><img src="{Path(e["edges_image"]).name}" width="420"></td></tr>'
        )
    (OUT_DIR / "index.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>SCATS mapping spot-check</title>"
        "<h1>SCATS detector to SUMO edge: spot-check (Phase 3A)</h1>"
        "<p>Left: our copy of the DTP detector map. Right: the SUMO approach edges the "
        "counts were mapped to (coloured; arrows show travel direction). Check that each "
        "coloured edge is the approach whose detectors are listed.</p>"
        "<table border=1 cellpadding=4>" + "".join(rows) + "</table>\n"
    )
    (OUT_DIR / "spotcheck.json").write_text(json.dumps(entries, indent=1) + "\n")
    return entries
