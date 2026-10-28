"""Explicit, one-off downloads into data/raw/. Never run implicitly by the build.

Usage:
    uv run python -m pipeline.download            # fetch any missing OSM / City of Melbourne file
    uv run python -m pipeline.download --force    # re-fetch even if present
    uv run python -m pipeline.download --vicmap-only   # only the Vicmap verification layers
    uv run python -m pipeline.download --pointcloud-plan   # list the point-cloud tiles + size only
    uv run python -m pipeline.download --pointcloud-only   # fetch them (<= POINTCLOUD_MAX_BYTES)

Politeness rules (CLAUDE.md, PLAN.md): one Overpass query for the whole building ring, a sensible
timeout, a descriptive User-Agent and **no retries**. If Overpass refuses or times out, it is not
asked again: the fallback is the BBBike "Melbourne" regional extract (OSM PBF, ~90 MB), which the
build clips locally to the bboxes in `pipeline/config.py`.

Vicmap Transport (verification only, see data/SOURCES.md) is fetched ONLY with --vicmap-only, never
by the default run: one WFS GetFeature request per layer for
SIM_EXTRACT_BBOX, no paging, no retries. If the server returns fewer features than it matched, the
file is kept as `.truncated` and the download stops with an error rather than asking again.

City of Melbourne 3D Point Cloud 2018 (road deck heights, see data/SOURCES.md) is fetched ONLY with
--pointcloud-only. The publisher offers one 4.3 GB zip of .las tiles; we read the zip's central
directory with a few small HTTP range requests, pick the tiles whose footprint in the published
Tile_Index.KML intersects BUILDINGS_BBOX (which contains SIM_EXTRACT_BBOX: the sim-extract tiles
are used for road decks now, the rest of the ring for building heights later, one download pass),
and fetch each tile with ONE range request (no retries). Phase 2 stacked tiers (2026-10-27): the
selection also takes every tile whose footprint polygon intersects BUILDINGS_AREA (the City of
Melbourne municipality UNION BUILDINGS_BBOX, pipeline/config.py), for building heights.
A tile already on disk is skipped. If the selection is larger than POINTCLOUD_MAX_BYTES the run
stops before downloading anything (pass --allow-large once the size has been approved).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from pathlib import Path

from pipeline.config import BUILDINGS_BBOX, SIM_EXTRACT_BBOX, BBox, buildings_area

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"

OSM_RAW = RAW_DIR / "osm_buildings_bbox.osm"
OSM_FALLBACK_RAW = RAW_DIR / "bbbike_melbourne.osm.pbf"
COM_RAW = RAW_DIR / "com_2023_building_footprints.geojson"
BOUNDARY_RAW = RAW_DIR / "com_municipal_boundary.geojson"
VICMAP_LAYERS = {
    "tr_road": "vicmap_tr_road.geojson",
    "tr_road_infrastructure": "vicmap_tr_road_infrastructure.geojson",
}

USER_AGENT = (
    "MelbourneTrafficTwin/0.1 (open-data traffic twin project; "
    "one-off bbox download, cached locally, no retries)"
)
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_TIMEOUT_S = 300
BBBIKE_URL = "https://download.bbbike.org/osm/bbbike/Melbourne/Melbourne.osm.pbf"
COM_EXPORT_URL = (
    "https://data.melbourne.vic.gov.au/api/explore/v2.1/catalog/datasets/"
    "2023-building-footprints/exports/geojson"
)

BOUNDARY_EXPORT_URL = (
    "https://data.melbourne.vic.gov.au/api/explore/v2.1/catalog/datasets/"
    "municipal-boundary/exports/geojson"
)

POINTCLOUD_DIR = RAW_DIR / "com_pointcloud_2018"
POINTCLOUD_ZIP_URL = "https://opendatasoft-s3.s3.ap-southeast-2.amazonaws.com/attachments/CoM_Point_Cloud_2018_LAS.zip"
POINTCLOUD_INDEX_URL = (
    "https://data.melbourne.vic.gov.au/api/explore/v2.1/catalog/datasets/"
    "city-of-melbourne-3d-point-cloud-2018/attachments/tile_index_kml"
)
POINTCLOUD_MAX_BYTES = 3_000_000_000  # above this, stop and ask (orchestrator instruction)
RANGE_BLOCK = 1 << 16  # central-directory reads are cached in 64 kB blocks

VICMAP_WFS_URL = "https://opendata.maps.vic.gov.au/geoserver/wfs"
VICMAP_MAX_FEATURES = 10000  # 4,782 road lines, 3,223 points in SIM_EXTRACT_BBOX (2026-10-26)


def vicmap_wfs_url(layer: str, bbox: BBox = SIM_EXTRACT_BBOX) -> str:
    """One WFS 2.0 GetFeature request: `layer` inside `bbox`, GeoJSON in lon/lat (EPSG:4326)."""
    q = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": f"open-data-platform:{layer}",
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "count": str(VICMAP_MAX_FEATURES),
        # WFS 2.0 with an EPSG URN takes lat/lon axis order in the bbox.
        "bbox": f"{bbox.south},{bbox.west},{bbox.north},{bbox.east},urn:ogc:def:crs:EPSG::4326",
    }
    return VICMAP_WFS_URL + "?" + urllib.parse.urlencode(q)


def download_vicmap(force: bool = False, raw_dir: Path = RAW_DIR) -> list[Path]:
    out = []
    for layer, name in VICMAP_LAYERS.items():
        dest = raw_dir / name
        if dest.exists() and not force:
            print(f"skip: {dest.name} already present")
            out.append(dest)
            continue
        url = vicmap_wfs_url(layer)
        print(f"Vicmap WFS: {url}")
        _fetch(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), dest, 300)
        fc = json.loads(dest.read_text())
        matched, n = fc.get("numberMatched"), len(fc.get("features", []))
        if fc.get("type") != "FeatureCollection" or (isinstance(matched, int) and matched > n):
            bad = dest.with_suffix(".truncated")
            dest.replace(bad)
            raise SystemExit(f"{layer}: {n} of {matched} features; see {bad}. Not retrying.")
        print(f"wrote {dest} ({n} features, {dest.stat().st_size / 1e6:.1f} MB)")
        out.append(dest)
    return out


# --- City of Melbourne point cloud ----------------------------------------------------------


def parse_tile_index(kml: str) -> dict[str, list[tuple[float, float]]]:
    """Tile name -> footprint ring [(lon, lat), ...] from the published Tile_Index.KML."""
    out = {}
    for pm in re.findall(r"<Placemark>(.*?)</Placemark>", kml, flags=re.S):
        name = re.search(r"<name>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</name>", pm, flags=re.S)
        coords = re.search(r"<coordinates>(.*?)</coordinates>", pm, flags=re.S)
        if not name or not coords:
            continue
        ring = []
        for tok in coords.group(1).split():
            lon, lat = (float(v) for v in tok.split(",")[:2])
            ring.append((lon, lat))
        out[name.group(1).strip()] = ring
    return out


def tiles_in_bbox(index: dict[str, list[tuple[float, float]]], bbox: BBox) -> list[str]:
    """Tiles whose footprint's bounding box intersects `bbox` (tiles are ~500 m squares)."""
    keep = []
    for name, ring in index.items():
        lons = [p[0] for p in ring]
        lats = [p[1] for p in ring]
        if (
            min(lons) <= bbox.east
            and max(lons) >= bbox.west
            and min(lats) <= bbox.north
            and max(lats) >= bbox.south
        ):
            keep.append(name)
    return sorted(keep)


def tiles_in_area(index: dict[str, list[tuple[float, float]]], area) -> list[str]:
    """Tiles whose footprint polygon intersects the shapely geometry `area` (lon/lat)."""
    from shapely.geometry import Polygon

    return sorted(name for name, ring in index.items() if Polygon(ring).intersects(area))


def pointcloud_selection(index: dict[str, list[tuple[float, float]]]) -> list[str]:
    """The tiles to hold on disk: over BUILDINGS_BBOX (the original pass) or BUILDINGS_AREA."""
    return sorted(
        set(tiles_in_bbox(index, BUILDINGS_BBOX)) | set(tiles_in_area(index, buildings_area()))
    )


class HttpRangeFile(io.RawIOBase):
    """Read-only, seekable view of a remote file through HTTP range requests (for zipfile to read
    the central directory). Reads are served from cached RANGE_BLOCK blocks; `requests` counts
    the HTTP requests made."""

    def __init__(self, url: str, size: int, opener=urllib.request.urlopen):
        self.url, self.size, self.pos, self.requests = url, size, 0, 0
        self._open = opener
        self._blocks: dict[int, bytes] = {}

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        base = {0: 0, 1: self.pos, 2: self.size}[whence]
        self.pos = base + offset
        return self.pos

    def fetch(self, start: int, end: int) -> bytes:
        """Bytes [start, end) in ONE request (end is clamped to the file size)."""
        end = min(end, self.size)
        req = urllib.request.Request(
            self.url, headers={"User-Agent": USER_AGENT, "Range": f"bytes={start}-{end - 1}"}
        )
        self.requests += 1
        with self._open(req, timeout=600) as resp:
            if getattr(resp, "status", 206) != 206:
                raise SystemExit(f"server ignored the range request (HTTP {resp.status}); stopping")
            data = resp.read()
        if len(data) != end - start:
            raise SystemExit(f"short range read {len(data)} != {end - start}; not retrying")
        return data

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            n = self.size - self.pos
        out = bytearray()
        while n > 0 and self.pos < self.size:
            b = self.pos // RANGE_BLOCK
            if b not in self._blocks:
                self._blocks[b] = self.fetch(b * RANGE_BLOCK, (b + 1) * RANGE_BLOCK)
            block = self._blocks[b]
            off = self.pos - b * RANGE_BLOCK
            take = block[off : off + n]
            out += take
            self.pos += len(take)
            n -= len(take)
        return bytes(out)

    def readinto(self, buf) -> int:  # type: ignore[override]
        data = self.read(len(buf))
        buf[: len(data)] = data
        return len(data)


def extract_member(raw: bytes, info: zipfile.ZipInfo) -> bytes:
    """Decompress one member from bytes that start at its local file header."""
    sig, *_rest = struct.unpack("<4s26s", raw[:30])
    if sig != b"PK\x03\x04":
        raise SystemExit(f"{info.filename}: no local file header at its offset")
    name_len, extra_len = struct.unpack("<HH", raw[26:30])
    start = 30 + name_len + extra_len
    data = raw[start : start + info.compress_size]
    if len(data) != info.compress_size:
        raise SystemExit(f"{info.filename}: truncated member data")
    if info.compress_type == zipfile.ZIP_STORED:
        out = data
    elif info.compress_type == zipfile.ZIP_DEFLATED:
        out = zlib.decompress(data, -15)
    else:
        raise SystemExit(f"{info.filename}: unsupported zip compression {info.compress_type}")
    if zlib.crc32(out) & 0xFFFFFFFF != info.CRC:
        raise SystemExit(f"{info.filename}: CRC mismatch")
    return out


def tile_member(names: list[str], tile: str) -> str | None:
    """The zip member for a tile name from the index (matched on the file stem)."""
    for n in names:
        stem = Path(n).name.rsplit(".", 1)[0]
        if stem.lower() == tile.lower():
            return n
    return None


def pointcloud_plan(
    bbox: BBox | None = None, out_dir: Path = POINTCLOUD_DIR
) -> tuple[HttpRangeFile, zipfile.ZipFile, list[zipfile.ZipInfo], list[str]]:
    """Tile index + zip central directory -> the zip members to fetch. Small requests only.
    `bbox=None` (default): `pointcloud_selection` (BUILDINGS_BBOX and BUILDINGS_AREA)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    idx_path = out_dir / "Tile_Index.KML"
    if not idx_path.exists():
        _fetch(
            urllib.request.Request(POINTCLOUD_INDEX_URL, headers={"User-Agent": USER_AGENT}),
            idx_path,
            120,
        )
    index = parse_tile_index(idx_path.read_text(encoding="iso-8859-1"))
    tiles = pointcloud_selection(index) if bbox is None else tiles_in_bbox(index, bbox)
    head = urllib.request.Request(
        POINTCLOUD_ZIP_URL, headers={"User-Agent": USER_AGENT}, method="HEAD"
    )
    with urllib.request.urlopen(head, timeout=120) as resp:
        size = int(resp.headers["Content-Length"])
        if resp.headers.get("Accept-Ranges") != "bytes":
            raise SystemExit("the zip host does not accept range requests; stopping")
    remote = HttpRangeFile(POINTCLOUD_ZIP_URL, size)
    zf = zipfile.ZipFile(remote)
    names = zf.namelist()
    members, missing = [], []
    for t in tiles:
        m = tile_member(names, t)
        (members.append(zf.getinfo(m)) if m else missing.append(t))
    return remote, zf, members, missing


def _on_disk(m: zipfile.ZipInfo) -> bool:
    dest = POINTCLOUD_DIR / Path(m.filename).name
    return dest.exists() and dest.stat().st_size == m.file_size


def download_pointcloud(allow_large: bool = False, plan_only: bool = False) -> list[Path]:
    remote, zf, members, missing = pointcloud_plan()
    total = sum(m.compress_size for m in members)
    index = parse_tile_index((POINTCLOUD_DIR / "Tile_Index.KML").read_text(encoding="iso-8859-1"))
    sim = set(tiles_in_bbox(index, SIM_EXTRACT_BBOX))
    in_sim = [m for m in members if Path(m.filename).name.rsplit(".", 1)[0] in sim]
    on_disk = [m for m in members if _on_disk(m)]
    todo = [m for m in members if not _on_disk(m)]
    print(
        f"already on disk: {len(on_disk)} tiles; still to fetch: {len(todo)} tiles, "
        f"{sum(m.compress_size for m in todo) / 1e9:.2f} GB to transfer, "
        f"{sum(m.file_size for m in todo) / 1e9:.2f} GB on disk"
    )
    print(
        f"tiles over BUILDINGS_BBOX + BUILDINGS_AREA: {len(members)} in the zip "
        f"({len(missing)} in the index but not in the zip: {missing}); "
        f"{total / 1e9:.2f} GB to transfer, "
        f"{sum(m.file_size for m in members) / 1e9:.2f} GB on disk; of which over "
        f"SIM_EXTRACT_BBOX: {len(in_sim)} tiles, "
        f"{sum(m.compress_size for m in in_sim) / 1e9:.2f} GB; rest of the ring: "
        f"{len(members) - len(in_sim)} tiles, "
        f"{(total - sum(m.compress_size for m in in_sim)) / 1e9:.2f} GB; "
        f"{remote.requests} range requests so far (index + central directory)"
    )
    if plan_only:
        return []
    todo_bytes = sum(m.compress_size for m in todo)
    if todo_bytes > POINTCLOUD_MAX_BYTES and not allow_large:
        raise SystemExit(
            f"{todo_bytes / 1e9:.2f} GB > {POINTCLOUD_MAX_BYTES / 1e9:.1f} GB: ask first"
        )
    manifest_path = POINTCLOUD_DIR / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    out = []
    for m in members:
        dest = POINTCLOUD_DIR / Path(m.filename).name
        if dest.exists() and dest.stat().st_size == m.file_size:
            out.append(dest)
            continue
        slack = 30 + len(m.filename.encode()) + 1024  # local header + extra field
        raw = remote.fetch(m.header_offset, m.header_offset + m.compress_size + slack)
        data = extract_member(raw, m)
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
        manifest[dest.name] = {
            "zip_member": m.filename,
            "zip_offset": m.header_offset,
            "compressed_bytes": m.compress_size,
            "bytes": m.file_size,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
        print(f"wrote {dest.name} ({m.file_size / 1e6:.1f} MB)")
        out.append(dest)
    print(f"done: {len(out)} tiles, {remote.requests} range requests in total")
    return out


def overpass_query(bbox: BBox, timeout_s: int = OVERPASS_TIMEOUT_S) -> str:
    """Everything netconvert and the building pipeline need, for one bbox.

    All nodes/ways/relations touching the bbox, plus every node of those ways, plus the member
    ways and nodes of building multipolygons and turn restrictions. Other relations (admin
    boundaries, routes, coastline) are kept as bare relation records without recursing into their
    members, so the extract does not balloon to the whole state.
    """
    b = f"{bbox.south},{bbox.west},{bbox.north},{bbox.east}"
    return f"""[out:xml][timeout:{timeout_s}][maxsize:1073741824];
(node({b}); way({b}); rel({b});)->.all;
(
  rel.all["type"="multipolygon"]["building"];
  rel.all["type"="multipolygon"]["building:part"];
  rel.all["type"="restriction"];
)->.keyrels;
(way.all; way(r.keyrels);)->.ways;
(.all; .ways; node(w.ways); node(r.keyrels););
out body;
"""


def _fetch(req: urllib.request.Request, dest: Path, timeout_s: int) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp, tmp.open("wb") as fh:
        while chunk := resp.read(1 << 20):
            fh.write(chunk)
    tmp.replace(dest)


def osm_raw_path() -> Path | None:
    """The OSM extract the build should read: the Overpass file if present, else the fallback."""
    for p in (OSM_RAW, OSM_FALLBACK_RAW):
        if p.exists():
            return p
    return None


def download_osm(force: bool = False) -> Path:
    existing = osm_raw_path()
    if existing and not force:
        print(f"skip: {existing.name} already present")
        return existing
    try:
        return _download_overpass()
    except (urllib.error.URLError, TimeoutError, SystemExit) as exc:
        print(f"Overpass refused or failed ({exc}); not retrying. Falling back to BBBike extract.")
    return download_bbbike(force=True)


def download_bbbike(force: bool = False) -> Path:
    if OSM_FALLBACK_RAW.exists() and not force:
        print(f"skip: {OSM_FALLBACK_RAW.name} already present")
        return OSM_FALLBACK_RAW
    req = urllib.request.Request(BBBIKE_URL, headers={"User-Agent": USER_AGENT})
    print(f"BBBike: {BBBIKE_URL}")
    _fetch(req, OSM_FALLBACK_RAW, 600)
    print(f"wrote {OSM_FALLBACK_RAW} ({OSM_FALLBACK_RAW.stat().st_size / 1e6:.1f} MB)")
    return OSM_FALLBACK_RAW


def _download_overpass() -> Path:
    body = urllib.parse.urlencode({"data": overpass_query(BUILDINGS_BBOX)}).encode()
    req = urllib.request.Request(
        OVERPASS_URL, data=body, headers={"User-Agent": USER_AGENT}, method="POST"
    )
    print(f"Overpass: one query for {BUILDINGS_BBOX} (timeout {OVERPASS_TIMEOUT_S}s, no retry)")
    # Socket timeout a little above the server-side timeout. No retry on failure, by design.
    _fetch(req, OSM_RAW, OVERPASS_TIMEOUT_S + 60)
    head = OSM_RAW.read_bytes()[:4000]
    if b"<remark>" in head or b"<osm" not in head:
        bad = OSM_RAW.with_suffix(".osm.failed")
        OSM_RAW.replace(bad)
        raise SystemExit(f"Overpass returned an error or partial result; see {bad}. Not retrying.")
    print(f"wrote {OSM_RAW} ({OSM_RAW.stat().st_size / 1e6:.1f} MB)")
    return OSM_RAW


def download_boundary(force: bool = False, dest: Path = BOUNDARY_RAW) -> Path:
    """The City of Melbourne municipal boundary (one polygon), one export request, no retries."""
    if dest.exists() and not force:
        print(f"skip: {dest.name} already present")
        return dest
    req = urllib.request.Request(BOUNDARY_EXPORT_URL, headers={"User-Agent": USER_AGENT})
    print("City of Melbourne: Municipal boundary (GeoJSON export)")
    _fetch(req, dest, 120)
    fc = json.loads(dest.read_text())
    n = len(fc.get("features", []))
    if fc.get("type") != "FeatureCollection" or n != 1:
        bad = dest.with_suffix(".unexpected")
        dest.replace(bad)
        raise SystemExit(f"municipal boundary: expected 1 feature, got {n}; see {bad}")
    print(f"wrote {dest} ({dest.stat().st_size / 1e3:.0f} kB)")
    return dest


def download_com(force: bool = False) -> Path:
    if COM_RAW.exists() and not force:
        print(f"skip: {COM_RAW.name} already present")
        return COM_RAW
    req = urllib.request.Request(COM_EXPORT_URL, headers={"User-Agent": USER_AGENT})
    print("City of Melbourne: 2023 Building Footprints (GeoJSON export)")
    _fetch(req, COM_RAW, 600)
    print(f"wrote {COM_RAW} ({COM_RAW.stat().st_size / 1e6:.1f} MB)")
    return COM_RAW


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--force", action="store_true", help="re-download files already in data/raw/")
    ap.add_argument(
        "--osm-source",
        choices=["auto", "overpass", "bbbike"],
        default="auto",
        help="auto: one Overpass query, BBBike extract if refused (default)",
    )
    ap.add_argument(
        "--vicmap-only", action="store_true", help="only the Vicmap Transport verification layers"
    )
    ap.add_argument("--pointcloud-plan", action="store_true", help="list point-cloud tiles only")
    ap.add_argument("--pointcloud-only", action="store_true", help="fetch the point-cloud tiles")
    ap.add_argument("--allow-large", action="store_true", help="allow > POINTCLOUD_MAX_BYTES")
    ap.add_argument(
        "--boundary-only", action="store_true", help="only the City of Melbourne boundary"
    )
    args = ap.parse_args(argv)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if args.pointcloud_plan or args.pointcloud_only:
        download_pointcloud(args.allow_large, plan_only=args.pointcloud_plan)
        return 0
    if args.vicmap_only:
        download_vicmap(args.force)
        return 0
    if args.boundary_only:
        download_boundary(args.force)
        return 0
    if args.osm_source == "bbbike":
        download_bbbike(args.force)
    elif args.osm_source == "overpass":
        _download_overpass()
    else:
        download_osm(args.force)
    download_com(args.force)
    download_boundary(args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
