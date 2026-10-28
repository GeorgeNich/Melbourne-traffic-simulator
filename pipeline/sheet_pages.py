"""Page images out of the DTP configuration-sheet PDFs, without a PDF renderer.

The sheets are rasterised PDFs written by Pillow ("% created by Pillow ... PDF driver"): every
page is a single JPEG image XObject (DCTDecode). We read the page tree, find each page's image
object and return its JPEG bytes unchanged. Only this simple structure is supported; anything
else raises ValueError (no attempt to render arbitrary PDFs). Input is untrusted data: it is only
parsed as bytes, nothing in it is executed.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_OBJ = re.compile(rb"(\d+) 0 obj\s*<<(.*?)>>\s*(stream\r?\n)?", re.S)


def _objects(pdf: bytes) -> dict[int, tuple[bytes, int | None]]:
    """object number -> (dictionary bytes, offset of stream data or None)."""
    out = {}
    for m in _OBJ.finditer(pdf):
        out[int(m.group(1))] = (m.group(2), m.end() if m.group(3) else None)
    return out


def _ref(d: bytes, key: bytes) -> list[int]:
    m = re.search(rb"/" + key + rb"\s*\[([^\]]*)\]", d)
    if m:
        return [int(x) for x in re.findall(rb"(\d+) 0 R", m.group(1))]
    m = re.search(rb"/" + key + rb"\s+(\d+) 0 R", d)
    return [int(m.group(1))] if m else []


def page_jpegs(pdf: bytes) -> list[bytes]:
    """JPEG bytes of each page, in page order."""
    if not pdf.startswith(b"%PDF"):
        raise ValueError("not a PDF")
    objs = _objects(pdf)
    cat = next((d for d, _ in objs.values() if re.search(rb"/Type\s*/Catalog", d)), None)
    if cat is None:
        raise ValueError("no catalog")
    pages_root = objs[_ref(cat, b"Pages")[0]][0]
    out = []
    for pnum in _ref(pages_root, b"Kids"):
        pd = objs[pnum][0]
        if re.search(rb"/Type\s*/Pages\b", pd):
            raise ValueError("nested page tree not supported")
        # Pillow nests the resources dictionary inside the page dictionary; the image is the only
        # XObject, referenced as "/image N 0 R" (or similar) after "/XObject <<".
        om = re.search(rb"(?<![0-9])" + str(pnum).encode() + rb" 0 obj", pdf)
        if om is None:
            raise ValueError(f"page object {pnum} not found")
        whole = pdf[om.start() :]
        whole = whole[: whole.index(b"endobj")]
        xm = re.search(rb"/XObject\s*<<\s*/\w+\s+(\d+) 0 R", whole)
        if not xm:
            raise ValueError(f"page object {pnum}: no image XObject")
        idict, start = objs[int(xm.group(1))]
        if start is None or b"/DCTDecode" not in idict:
            raise ValueError(f"page object {pnum}: image is not a JPEG stream")
        length = int(re.search(rb"/Length\s+(\d+)", idict).group(1))
        data = pdf[start : start + length]
        if not data.startswith(b"\xff\xd8"):
            raise ValueError(f"page object {pnum}: stream is not JPEG")
        out.append(data)
    return out


def write_pages(pdf_path: Path, out_dir: Path, pages: list[int] | None = None) -> list[Path]:
    """Write pages (1-based; default all) as `<stem>_p<NN>.jpg` in `out_dir`."""
    jpgs = page_jpegs(pdf_path.read_bytes())
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for i, data in enumerate(jpgs, start=1):
        if pages is not None and i not in pages:
            continue
        p = out_dir / f"{pdf_path.stem}_p{i:02d}.jpg"
        p.write_bytes(data)
        written.append(p)
    return written


def downscale_jpeg(src: Path, dest: Path, max_px: int = 1600) -> Path:
    """A smaller copy via macOS `sips` (built in); falls back to a plain copy elsewhere."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["sips", "-Z", str(max_px), "-s", "formatOptions", "70", str(src), "--out", str(dest)],
            check=True,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        dest.write_bytes(src.read_bytes())
    return dest
