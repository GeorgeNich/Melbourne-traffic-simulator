"""Polite, cached downloads for the Phase 3 SCATS counts into data/raw/scats/ (never by the build).

Usage:
    uv run python -m pipeline.scats_download --sites      # site list + CKAN metadata (small)
    uv run python -m pipeline.scats_download --volumes    # the 12 typical-weekday daily CSVs
    uv run python -m pipeline.scats_download --from 2026-10-06 --to 2026-10-25   # any range
    uv run python -m pipeline.scats_download --sheets-plan   # list the config sheets + sizes only
    uv run python -m pipeline.scats_download --sheets     # fetch the config sheets of SHEET_SITES
    uv run python -m pipeline.scats_download --aadt       # 2019 AADT (verification only)

Politeness (CLAUDE.md, data/SOURCES.md): a descriptive User-Agent, no retries, and nothing larger
than needed. The SCATS volume data and the configuration sheets are published only as large ZIPs
(0.1-3.6 GB). We never download a whole ZIP: `HttpRangeFile` reads the ZIP's central directory
with a few small range requests and each wanted member is then fetched with ONE range request.
A file already on disk (same size / sha256 in the manifest) is skipped.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

from pipeline.download import RAW_DIR, USER_AGENT, HttpRangeFile, _fetch, extract_member

SCATS_RAW = RAW_DIR / "scats"
SHEETS_RAW = SCATS_RAW / "sheets"
SITES_CSV = SCATS_RAW / "victorian_traffic_signals.csv"
AADT_RAW = SCATS_RAW / "yearly_aadt_volume_2019.geojson"

CKAN_API = "https://opendata.transport.vic.gov.au/api/3/action/package_show?id="
CKAN_DATASETS = (
    "traffic-signal-volume-data",
    "victorian-traffic-signals",
    "traffic-signal-configuration-data-sheets",
    "historical-annual-average-daily-traffic-volume",
)
SITES_URL = (
    "https://opendata.transport.vic.gov.au/dataset/923af458-363d-469f-bc5e-84746a80b9a2/"
    "resource/1036a318-8867-4dc6-bd84-fc3bcd6cbdb9/download/victorian_traffic_signals.csv"
)
AADT_URL = (
    "https://opendata.transport.vic.gov.au/dataset/26fafd1a-8d59-4da0-93cd-29f371147d8f/"
    "resource/425799c9-658c-41cf-b9b0-6c9a145856cf/download/yearly_aadt_volume_2019.geojson"
)

VOLUME_DATASET = "traffic-signal-volume-data"
_MONTHS = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]


def volume_zip_url(day: dt.date, ckan_json: Path | None = None) -> str:
    """The published ZIP that holds `day`'s CSV: the monthly ZIP if there is one (2025 on), else
    the yearly ZIP (2014-2024). Resolved from the cached CKAN metadata, not hard-coded."""
    ckan_json = ckan_json or SCATS_RAW / "ckan" / f"{VOLUME_DATASET}.json"
    res = json.loads(ckan_json.read_text())["result"]["resources"]
    by_name = {r["name"].strip().lower(): r["url"] for r in res}
    month = f"traffic signal volume data {_MONTHS[day.month - 1]} {day.year}"
    year = f"traffic signal volume data {day.year}"
    if month in by_name:
        return by_name[month]
    if year in by_name:
        return by_name[year]
    raise SystemExit(f"{day}: no published ZIP for that month or year in {ckan_json.name}")


# Typical weekdays (data/SOURCES.md, "Typical weekdays used"): Tue-Thu of the four most recent
# weeks that are not Victorian public holidays or government school holidays and were published
# by 2026-10-27. School holidays 19 Sep - 4 Oct 2026 (vic.gov.au); next public holiday 3 Nov 2026.
TYPICAL_WEEKDAYS: tuple[dt.date, ...] = tuple(
    dt.date.fromisoformat(d)
    for d in (
        "2026-09-15",
        "2026-09-16",
        "2026-09-17",
        "2026-10-06",
        "2026-10-07",
        "2026-10-08",
        "2026-10-13",
        "2026-10-14",
        "2026-10-15",
        "2026-10-20",
        "2026-10-21",
        "2026-10-22",
    )
)
SCHOOL_HOLIDAYS_2026 = (
    (dt.date(2026, 4, 3), dt.date(2026, 4, 19)),
    (dt.date(2026, 6, 27), dt.date(2026, 7, 12)),
    (dt.date(2026, 9, 19), dt.date(2026, 10, 4)),
    (dt.date(2026, 12, 19), dt.date(2027, 1, 27)),
)
# Victorian public holidays we have checked (data/SOURCES.md References). Only used to skip days.
PUBLIC_HOLIDAYS_CHECKED = (dt.date(2026, 11, 3),)  # Melbourne Cup (Tue)

_SHEET = (
    "https://opendata.transport.vic.gov.au/dataset/48ec6c0a-994e-4f27-b3b8-bd7ffa86deda/resource/"
)
SHEET_ZIPS = {
    "1000-1249": _SHEET + "2e05206b-68f1-4cf4-8f19-f5018268a9dd/download/1000-1249.zip",
    "1250-1499": _SHEET + "de622044-f9d4-4519-8791-de319cdacb5b/download/1250-1499.zip",
    "1500-1749": _SHEET + "cf716f1b-5ed7-45c6-a5ab-8862127e42ef/download/1500-1749.zip",
    "2000-2249": _SHEET + "09216011-dfce-4464-8bfc-3cd2b5061312/download/2000-2249.zip",
    "2250-2499": _SHEET + "8c37a4c5-183b-425e-ad85-64378222eb76/download/2250-2499.zip",
    "2750-2999": _SHEET + "128af789-f38d-4b98-819e-2505ab67023b/download/2750-2999.zip",
    "3000-3249": _SHEET + "6aab4802-4221-439c-a075-13e35f8030cc/download/3000-3249.zip",
    "3250-3499": _SHEET + "a0e35471-5dd6-46d6-b3d8-19607b984e38/download/3250-3499.zip",
    "3500-3749": _SHEET + "a5649b06-de45-4caf-a9c2-919890af593b/download/3500-3749.zip",
    "3750-3999": _SHEET + "f6d92c58-0da3-4dbc-904f-dd4ef7f81c2b/download/3750-3999.zip",
    "4000-4249": _SHEET + "5ace0e72-f922-41fb-8832-a665b563d16d/download/4000-4249.zip",
    "4250-4499": _SHEET + "60ad5e65-c858-4445-9692-51ac26f6f345/download/4250-4499.zip",
    "4500-4749": _SHEET + "1bc2b6e5-8ad9-4603-8090-a72e1bd3ec14/download/4500-4749.zip",
    "4750-4999": _SHEET + "60f07908-0abd-419f-aee7-db37607d7f06/download/4750-4999.zip",
}
SHEETS_MAX_BYTES = 3_000_000_000  # stop and ask above this


def is_typical_weekday(day: dt.date) -> bool:
    """Tue/Wed/Thu, not in a 2026 school holiday, not a listed public holiday."""
    if day.weekday() not in (1, 2, 3):
        return False
    if any(a <= day <= b for a, b in SCHOOL_HOLIDAYS_2026):
        return False
    return day not in PUBLIC_HOLIDAYS_CHECKED


def sheet_zip_for(site_no: int) -> str | None:
    """The configuration-sheet ZIP range ("4500-4749") that holds `site_no`, if we know its URL."""
    for rng in SHEET_ZIPS:
        lo, hi = (int(v) for v in rng.split("-"))
        if lo <= site_no <= hi:
            return rng
    return None


def _req(url: str, method: str = "GET") -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method=method)


def _remote_zip(url: str) -> tuple[HttpRangeFile, zipfile.ZipFile]:
    with urllib.request.urlopen(_req(url, "HEAD"), timeout=120) as resp:
        size = int(resp.headers["Content-Length"])
        if resp.headers.get("Accept-Ranges") != "bytes":
            raise SystemExit(f"{url}: no range requests; stopping (not downloading the whole ZIP)")
    remote = HttpRangeFile(url, size)
    return remote, zipfile.ZipFile(remote)


def _fetch_member(remote: HttpRangeFile, m: zipfile.ZipInfo) -> bytes:
    slack = 30 + len(m.filename.encode()) + 1024  # local header + extra field
    raw = remote.fetch(m.header_offset, m.header_offset + m.compress_size + slack)
    return extract_member(raw, m)


def _load_manifest(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _save_manifest(path: Path, manifest: dict) -> None:
    path.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")


def download_sites(force: bool = False) -> None:
    """The site register and the four datasets' CKAN metadata (licence provenance)."""
    SCATS_RAW.mkdir(parents=True, exist_ok=True)
    ckan = SCATS_RAW / "ckan"
    ckan.mkdir(exist_ok=True)
    for ds in CKAN_DATASETS:
        dest = ckan / f"{ds}.json"
        if dest.exists() and not force:
            print(f"skip: {dest.name}")
            continue
        _fetch(_req(CKAN_API + ds), dest, 60)
        lic = json.loads(dest.read_text())["result"].get("license_id")
        print(f"wrote {dest.name} (license_id {lic})")
        if lic != "CC-BY-4.0":
            raise SystemExit(f"{ds}: licence {lic!r} is not CC-BY-4.0; stop and ask")
    if SITES_CSV.exists() and not force:
        print(f"skip: {SITES_CSV.name}")
    else:
        _fetch(_req(SITES_URL), SITES_CSV, 120)
        print(f"wrote {SITES_CSV} ({SITES_CSV.stat().st_size} bytes)")


def daterange(start: dt.date, end: dt.date) -> list[dt.date]:
    return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]


def volume_path(day: dt.date) -> Path:
    return SCATS_RAW / f"VSDATA_{day:%Y%m%d}.csv.gz"


def download_volumes(days: tuple[dt.date, ...] | list[dt.date] = TYPICAL_WEEKDAYS) -> list[Path]:
    """One daily CSV per day, out of its monthly (or yearly) ZIP, stored gzipped (lossless).
    Cached: a day already in the manifest is never fetched again. A day not yet published is
    reported and skipped (no retry)."""
    SCATS_RAW.mkdir(parents=True, exist_ok=True)
    mpath = SCATS_RAW / "MANIFEST.json"
    manifest = _load_manifest(mpath)
    todo = [d for d in sorted(set(days)) if volume_path(d).name not in manifest]
    if todo and not (SCATS_RAW / "ckan" / f"{VOLUME_DATASET}.json").exists():
        download_sites()
    by_zip: dict[str, list[dt.date]] = {}
    for d in todo:
        by_zip.setdefault(volume_zip_url(d), []).append(d)
    for url, ds in by_zip.items():
        remote, zf = _remote_zip(url)
        names = {Path(n).name: n for n in zf.namelist()}
        for d in ds:
            member = names.get(f"VSDATA_{d:%Y%m%d}.csv")
            if member is None:
                print(f"{d}: not (yet) in {url.rsplit('/', 1)[-1]}; skipped")
                continue
            info = zf.getinfo(member)
            data = _fetch_member(remote, info)
            dest = volume_path(d)
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(gzip.compress(data, compresslevel=6, mtime=0))
            tmp.replace(dest)
            manifest[dest.name] = {
                "zip_url": url,
                "zip_bytes": remote.size,
                "zip_member": info.filename,
                "zip_offset": info.header_offset,
                "compressed_bytes": info.compress_size,
                "bytes": info.file_size,
                "crc32": f"{info.CRC:08x}",
                "sha256_csv": hashlib.sha256(data).hexdigest(),
                "downloaded": dt.date.today().isoformat(),
            }
            _save_manifest(mpath, manifest)
            print(
                f"wrote {dest.name} ({info.compress_size / 1e6:.1f} MB zipped, "
                f"{info.file_size / 1e6:.1f} MB csv)"
            )
        print(f"{url.rsplit('/', 1)[-1]}: {remote.requests} range requests")
    return [volume_path(d) for d in sorted(set(days)) if volume_path(d).exists()]


def sheet_members(zf: zipfile.ZipFile, rng: str, site_no: int) -> list[zipfile.ZipInfo]:
    """The ZIP members that make up one site's sheet: `<rng>/<site>.pdf`, or `<rng>/<site>.htm` +
    everything under `<rng>/<site>_files/`."""
    want_pdf = f"{site_no}.pdf"
    want_htm = f"{site_no}.htm"
    want_dir = f"{site_no}_files/"
    out = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        parts = info.filename.split("/")
        tail = "/".join(parts[1:]) if len(parts) > 1 else parts[0]
        if tail.lower() in (want_pdf, want_htm) or tail.lower().startswith(want_dir):
            out.append(info)
    return out


def _site_span(members: list[zipfile.ZipInfo]) -> tuple[int, int]:
    """Byte range [start, end) of the ZIP that holds all of a site's members (they sit together;
    the gaps are a few small entries), so the whole site comes in ONE range request."""
    start = min(m.header_offset for m in members)
    end = max(
        m.header_offset + 30 + len(m.filename.encode()) + 1024 + m.compress_size for m in members
    )
    return start, end


def download_sheets(
    sites: list[int],
    plan_only: bool = False,
    allow_large: bool = False,
    pdf_sites: set[int] | None = None,
) -> None:
    """Configuration sheets for `sites`, ONE range request per site. `pdf_sites`: if given, a PDF
    sheet (rasterised, 5-50 MB each) is fetched only for these sites; htm sheets are small and
    always fetched."""
    SHEETS_RAW.mkdir(parents=True, exist_ok=True)
    mpath = SHEETS_RAW / "MANIFEST.json"
    manifest = _load_manifest(mpath)
    by_zip: dict[str, list[int]] = {}
    for s in sites:
        rng = sheet_zip_for(s)
        if rng is None:
            print(f"site {s}: no configuration-sheet ZIP range known; skipped")
            continue
        by_zip.setdefault(rng, []).append(s)
    plan: list[tuple[str, HttpRangeFile, int, list[zipfile.ZipInfo]]] = []
    missing: list[int] = []
    skipped_pdf: list[int] = []
    for rng, ss in sorted(by_zip.items()):
        remote, zf = _remote_zip(SHEET_ZIPS[rng])
        for s in ss:
            members = sheet_members(zf, rng, s)
            if not members:
                missing.append(s)
                continue
            is_pdf = any(m.filename.lower().endswith(".pdf") for m in members)
            if is_pdf and pdf_sites is not None and s not in pdf_sites:
                skipped_pdf.append(s)
                continue
            plan.append((rng, remote, s, members))
        print(f"{rng}: {len(ss)} sites, central directory read in {remote.requests} requests")
    todo = [p for p in plan if any(m.filename not in manifest for m in p[3])]
    total = sum(_site_span(p[3])[1] - _site_span(p[3])[0] for p in todo)
    print(
        f"sheets: {len(plan)} sites planned ({sum(len(p[3]) for p in plan)} members); to fetch "
        f"{len(todo)} sites, {total / 1e6:.1f} MB in {len(todo)} requests; "
        f"no sheet in any ZIP: {missing}; PDF not requested: {len(skipped_pdf)} sites"
    )
    if plan_only:
        return
    if total > SHEETS_MAX_BYTES and not allow_large:
        raise SystemExit(f"{total / 1e9:.2f} GB > {SHEETS_MAX_BYTES / 1e9:.1f} GB: ask first")
    root = SHEETS_RAW.resolve()
    for rng, remote, site, members in todo:
        start, end = _site_span(members)
        buf = remote.fetch(start, end)
        for m in members:
            data = extract_member(buf[m.header_offset - start :], m)
            dest = SHEETS_RAW / m.filename
            if root not in dest.resolve().parents:
                raise SystemExit(f"unsafe member path {m.filename!r}; stopping")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            manifest[m.filename] = {
                "site": site,
                "zip_url": SHEET_ZIPS[rng],
                "zip_offset": m.header_offset,
                "compressed_bytes": m.compress_size,
                "bytes": m.file_size,
                "sha256": hashlib.sha256(data).hexdigest(),
                "downloaded": dt.date.today().isoformat(),
            }
        _save_manifest(mpath, manifest)
        print(f"site {site}: {len(members)} members, {(end - start) / 1e6:.1f} MB")
    print(f"done: {len(todo)} sites written under {SHEETS_RAW}")


def download_aadt(force: bool = False) -> None:
    if AADT_RAW.exists() and not force:
        print(f"skip: {AADT_RAW.name}")
        return
    SCATS_RAW.mkdir(parents=True, exist_ok=True)
    _fetch(_req(AADT_URL), AADT_RAW, 600)
    print(f"wrote {AADT_RAW} ({AADT_RAW.stat().st_size / 1e6:.1f} MB)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--sites", action="store_true")
    ap.add_argument("--volumes", action="store_true", help="the typical weekdays")
    ap.add_argument("--from", dest="date_from", type=dt.date.fromisoformat, help="first day")
    ap.add_argument("--to", dest="date_to", type=dt.date.fromisoformat, help="last day")
    ap.add_argument("--sheets", action="store_true")
    ap.add_argument("--sheets-plan", action="store_true")
    ap.add_argument("--aadt", action="store_true")
    ap.add_argument("--allow-large", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--site", type=int, action="append", help="sheet sites (default: all used)")
    args = ap.parse_args(argv)
    if args.sites:
        download_sites(args.force)
    if args.volumes:
        download_volumes()
    if args.date_from:
        download_volumes(daterange(args.date_from, args.date_to or args.date_from))
    if args.sheets or args.sheets_plan:
        sites = args.site
        if not sites:
            from pipeline.scats import sheet_sites

            sites = sheet_sites()
        pdf_sites = None
        if not args.site:
            from pipeline.scats import RAW_DIR as _R
            from pipeline.scats import load_sites

            reg = load_sites(_R / "victorian_traffic_signals.csv")
            pdf_sites = {s for s in sites if reg[s].type == "INT"}
        download_sheets(
            sorted(set(sites)),
            plan_only=args.sheets_plan,
            allow_large=args.allow_large,
            pdf_sites=pdf_sites,
        )
    if args.aadt:
        download_aadt(args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
