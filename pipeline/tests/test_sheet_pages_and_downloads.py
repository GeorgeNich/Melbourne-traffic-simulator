"""PDF page extraction (synthetic Pillow-style PDF) and the SCATS download helpers (no network)."""

from __future__ import annotations

import datetime as dt
import io
import json
import zipfile

import pytest

from pipeline import scats_download as sd
from pipeline.sheet_pages import page_jpegs

JPEG_A = b"\xff\xd8\xff\xe0AAAA\xff\xd9"
JPEG_B = b"\xff\xd8\xff\xe0BBBBBB\xff\xd9"


def _pillow_like_pdf(images: list[bytes], first_obj: int = 1) -> bytes:
    """Objects image/page/contents per page like Pillow; object numbers > 9 so that a naive
    search for "5 0 obj" would hit "15 0 obj"."""
    out = bytearray(b"%PDF-1.4\n% created by Pillow 11.2.1 PDF driver\n")
    kids = []
    n = first_obj
    for img in images:
        out += f"{n} 0 obj<<\n/Type /XObject\n/Subtype /Image\n/Filter /DCTDecode\n".encode()
        out += f"/Length {len(img)}\n>>stream\n".encode() + img + b"\nendstream\nendobj\n"
        out += f"{n + 1} 0 obj<<\n/Resources <<\n/XObject <<\n/image {n} 0 R\n>>\n>>\n".encode()
        out += b"/Type /Page\n>>endobj\n"
        kids.append(n + 1)
        n += 3
    out += f"{n} 0 obj<<\n/Type /Pages\n/Count {len(kids)}\n/Kids [ ".encode()
    out += " ".join(f"{k} 0 R" for k in kids).encode() + b" ]\n>>endobj\n"
    out += f"{n + 1} 0 obj<<\n/Type /Catalog\n/Pages {n} 0 R\n>>endobj\n".encode()
    return bytes(out)


def test_page_jpegs_in_page_order_without_object_number_collisions():
    pdf = _pillow_like_pdf([JPEG_A, JPEG_B, JPEG_A, JPEG_B, JPEG_A], first_obj=1)
    pages = page_jpegs(pdf)
    assert pages == [JPEG_A, JPEG_B, JPEG_A, JPEG_B, JPEG_A]


def test_page_jpegs_rejects_non_pdf():
    with pytest.raises(ValueError):
        page_jpegs(b"<html>")


def test_volume_zip_url_prefers_month_then_year(tmp_path):
    ck = tmp_path / "ckan.json"
    res = [
        {"name": "Traffic Signal Volume Data October 2026", "url": "u_oct26"},
        {"name": "Traffic Signal Volume Data 2024", "url": "u_2024"},
    ]
    ck.write_text(json.dumps({"result": {"resources": res}}))
    assert sd.volume_zip_url(dt.date(2026, 10, 23), ck) == "u_oct26"
    assert sd.volume_zip_url(dt.date(2024, 3, 5), ck) == "u_2024"
    with pytest.raises(SystemExit):
        sd.volume_zip_url(dt.date(2026, 11, 3), ck)


def test_typical_weekday_rule_and_dates():
    assert all(sd.is_typical_weekday(d) for d in sd.TYPICAL_WEEKDAYS)
    assert not sd.is_typical_weekday(dt.date(2026, 9, 22))  # school holidays
    assert not sd.is_typical_weekday(dt.date(2026, 10, 23))  # a Friday
    assert sd.daterange(dt.date(2026, 10, 30), dt.date(2026, 11, 2))[-1] == dt.date(2026, 11, 2)


def test_sheet_zip_ranges_and_members():
    assert sd.sheet_zip_for(4562) == "4500-4749"
    assert sd.sheet_zip_for(2902) == "2750-2999"
    assert sd.sheet_zip_for(99) is None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in (
            "2750-2999/2900.htm",
            "2750-2999/2900_files/sheet002.htm",
            "2750-2999/29001.pdf",
            "2750-2999/2902.pdf",
        ):
            zf.writestr(name, b"x")
    zf = zipfile.ZipFile(buf)
    assert sorted(m.filename for m in sd.sheet_members(zf, "2750-2999", 2900)) == [
        "2750-2999/2900.htm",
        "2750-2999/2900_files/sheet002.htm",
    ]
    assert [m.filename for m in sd.sheet_members(zf, "2750-2999", 2902)] == ["2750-2999/2902.pdf"]
