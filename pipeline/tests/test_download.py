"""Download helpers without any network: URLs, the truncation guard, what runs by default."""

from __future__ import annotations

import json
import urllib.parse

import pytest

from pipeline import download
from pipeline.config import SIM_EXTRACT_BBOX


def test_vicmap_wfs_url_is_one_bbox_request_in_lat_lon_axis_order():
    url = download.vicmap_wfs_url("tr_road")
    base, query = url.split("?", 1)
    assert base == download.VICMAP_WFS_URL
    q = dict(urllib.parse.parse_qsl(query))
    assert q["request"] == "GetFeature" and q["version"] == "2.0.0"
    assert q["typeNames"] == "open-data-platform:tr_road"
    assert q["outputFormat"] == "application/json"
    b = SIM_EXTRACT_BBOX
    # WFS 2.0 + EPSG URN: latitude first (south, west, north, east).
    assert q["bbox"] == f"{b.south},{b.west},{b.north},{b.east},urn:ogc:def:crs:EPSG::4326"
    south, west = (float(v) for v in q["bbox"].split(",")[:2])
    assert -90 <= south <= 90 and south < 0 < west  # Melbourne: negative lat, positive lon


def _fake_fetch(payloads: dict[str, dict], calls: list[str]):
    def fetch(req, dest, timeout_s):
        calls.append(req.full_url)
        assert "MelbourneTrafficTwin" in req.get_header("User-agent")
        layer = next(k for k in payloads if f"%3A{k}&" in req.full_url)
        dest.write_text(json.dumps(payloads[layer]))

    return fetch


def _fc(n: int, matched: int) -> dict:
    feats = [{"type": "Feature", "geometry": None, "properties": {}} for _ in range(n)]
    return {"type": "FeatureCollection", "numberMatched": matched, "features": feats}


def test_download_vicmap_writes_each_layer_once(monkeypatch, tmp_path):
    calls: list[str] = []
    payloads = {"tr_road": _fc(3, 3), "tr_road_infrastructure": _fc(2, 2)}
    monkeypatch.setattr(download, "_fetch", _fake_fetch(payloads, calls))
    paths = download.download_vicmap(raw_dir=tmp_path)
    assert [p.name for p in paths] == list(download.VICMAP_LAYERS.values())
    assert len(calls) == 2
    # Cached: a second run asks nothing.
    download.download_vicmap(raw_dir=tmp_path)
    assert len(calls) == 2


def test_download_vicmap_truncated_result_stops_without_retry(monkeypatch, tmp_path):
    calls: list[str] = []
    payloads = {"tr_road": _fc(2, 5), "tr_road_infrastructure": _fc(1, 1)}
    monkeypatch.setattr(download, "_fetch", _fake_fetch(payloads, calls))
    with pytest.raises(SystemExit, match="2 of 5"):
        download.download_vicmap(raw_dir=tmp_path)
    assert len(calls) == 1  # no retry, and the next layer is not asked for
    assert not (tmp_path / "vicmap_tr_road.geojson").exists()
    assert (tmp_path / "vicmap_tr_road.truncated").exists()


def test_default_download_does_not_fetch_vicmap(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(download, "download_osm", lambda force=False: called.append("osm"))
    monkeypatch.setattr(download, "download_com", lambda force=False: called.append("com"))
    monkeypatch.setattr(download, "download_vicmap", lambda force=False: called.append("vicmap"))
    monkeypatch.setattr(download, "download_boundary", lambda force=False: called.append("cob"))
    download.main([])
    assert called == ["osm", "com", "cob"]
    called.clear()
    download.main(["--boundary-only"])
    assert called == ["cob"]
    called.clear()
    download.main(["--vicmap-only"])
    assert called == ["vicmap"]


# --- City of Melbourne point cloud (no network) -----------------------------------------------

KML = """<?xml version="1.0" encoding="iso-8859-1"?><kml><Document><Folder>
<Placemark><name><![CDATA[Tile_+001_+001]]></name><Polygon><outerBoundaryIs><LinearRing>
<coordinates>144.950,-37.820,0 144.956,-37.820,0 144.956,-37.815,0 144.950,-37.815,0
144.950,-37.820,0</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
<Placemark><name><![CDATA[Tile_+009_+009]]></name><Polygon><outerBoundaryIs><LinearRing>
<coordinates>145.100,-37.700,0 145.106,-37.700,0 145.106,-37.695,0 145.100,-37.695,0
145.100,-37.700,0</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
</Folder></Document></kml>"""


def test_tile_index_parse_and_bbox_selection():
    idx = download.parse_tile_index(KML)
    assert set(idx) == {"Tile_+001_+001", "Tile_+009_+009"}
    assert idx["Tile_+001_+001"][0] == (144.950, -37.820)
    assert download.tiles_in_bbox(idx, SIM_EXTRACT_BBOX) == ["Tile_+001_+001"]
    assert download.tile_member(["LAS/Tile_+001_+001.las"], "Tile_+001_+001") == (
        "LAS/Tile_+001_+001.las"
    )


def _zip_bytes() -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("LAS/Tile_+001_+001.las", b"LASF" + bytes(5000), zipfile.ZIP_DEFLATED)
        zf.writestr("LAS/Tile_+002_+001.las", b"LASF" + b"x" * 300, zipfile.ZIP_STORED)
    return buf.getvalue()


class _Resp:
    def __init__(self, data: bytes):
        self.data, self.status = data, 206

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_range_reader_lists_the_zip_and_extracts_one_member_per_request():
    import zipfile

    blob = _zip_bytes()
    calls = []

    def opener(req, timeout):
        assert "MelbourneTrafficTwin" in req.get_header("User-agent")
        start, end = (int(v) for v in req.get_header("Range")[6:].split("-"))
        calls.append((start, end))
        return _Resp(blob[start : end + 1])

    remote = download.HttpRangeFile("https://example.invalid/x.zip", len(blob), opener)
    zf = zipfile.ZipFile(remote)
    assert zf.namelist() == ["LAS/Tile_+001_+001.las", "LAS/Tile_+002_+001.las"]
    n_dir = remote.requests
    for name, want in (
        ("LAS/Tile_+001_+001.las", b"LASF" + bytes(5000)),
        ("LAS/Tile_+002_+001.las", b"LASF" + b"x" * 300),
    ):
        m = zf.getinfo(name)
        raw = remote.fetch(m.header_offset, m.header_offset + m.compress_size + 30 + 1024 + 50)
        assert download.extract_member(raw, m) == want
    assert remote.requests == n_dir + 2  # one range request per tile


def test_pointcloud_download_stops_above_the_size_limit(monkeypatch, tmp_path):
    import zipfile

    big = zipfile.ZipInfo("LAS/Tile_+001_+001.las")
    big.compress_size = download.POINTCLOUD_MAX_BYTES + 1
    big.file_size = big.compress_size
    monkeypatch.setattr(download, "POINTCLOUD_DIR", tmp_path)
    (tmp_path / "Tile_Index.KML").write_text(KML, encoding="iso-8859-1")
    monkeypatch.setattr(
        download, "pointcloud_plan", lambda: (download.HttpRangeFile("x", 1), None, [big], [])
    )
    with pytest.raises(SystemExit, match="ask first"):
        download.download_pointcloud()
    assert download.download_pointcloud(plan_only=True) == []


def test_tiles_in_area_uses_the_footprint_polygon(monkeypatch):
    from shapely.geometry import box

    idx = download.parse_tile_index(KML)
    assert download.tiles_in_area(idx, box(144.955, -37.818, 144.96, -37.81)) == ["Tile_+001_+001"]
    assert download.tiles_in_area(idx, box(144.957, -37.818, 144.96, -37.81)) == []
    # the selection is the BUILDINGS_BBOX tiles plus those over BUILDINGS_AREA
    monkeypatch.setattr(download, "buildings_area", lambda: box(145.101, -37.699, 145.102, -37.698))
    assert download.pointcloud_selection(idx) == ["Tile_+001_+001", "Tile_+009_+009"]


def test_download_boundary_once_and_checks_one_feature(monkeypatch, tmp_path):
    import json

    calls = []

    def fake_fetch(req, dest, timeout):
        calls.append(req.full_url)
        n = 1 if len(calls) == 1 else 2
        fc = {"type": "FeatureCollection", "features": [{"type": "Feature"}] * n}
        dest.write_text(json.dumps(fc))

    monkeypatch.setattr(download, "_fetch", fake_fetch)
    dest = tmp_path / "b.geojson"
    assert download.download_boundary(dest=dest) == dest
    assert download.download_boundary(dest=dest) == dest  # cached: no second request
    assert calls == [download.BOUNDARY_EXPORT_URL]
    with pytest.raises(SystemExit, match="expected 1 feature"):
        download.download_boundary(force=True, dest=dest)
    assert (tmp_path / "b.unexpected").exists()
