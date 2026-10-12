"""Tiny PNG reader (8-bit RGB/RGBA, non-interlaced) so tests can measure what a screenshot shows
without Pillow.  Returns pixel rows; enough to assert "the 3-D view drew something"."""

from __future__ import annotations

import struct
import zlib


def decode(png: bytes) -> tuple[int, int, int, list[bytes]]:
    """-> (width, height, channels, rows) with filters undone."""
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, w = 8, b"", None
    while pos < len(png):
        (ln,) = struct.unpack(">I", png[pos : pos + 4])
        typ, data = png[pos + 4 : pos + 8], png[pos + 8 : pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", data)
            if depth != 8 or ctype not in (2, 6) or interlace:
                raise ValueError(f"unsupported PNG (depth={depth} type={ctype} interlace={interlace})")
        elif typ == b"IDAT":
            idat += data
        elif typ == b"IEND":
            break
    ch = 3 if ctype == 2 else 4
    raw = zlib.decompress(idat)
    stride = w * ch
    rows: list[bytes] = []
    prev = bytearray(stride)
    i = 0
    for _ in range(h):
        f = raw[i]
        line = bytearray(raw[i + 1 : i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - ch] if x >= ch else 0
            b = prev[x]
            c = prev[x - ch] if x >= ch else 0
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + ((a + b) >> 1)) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 255
        rows.append(bytes(line))
        prev = line
    return w, h, ch, rows


def non_background_fraction(png: bytes, tol: int = 12) -> float:
    """Share of pixels that differ from the top-left corner colour (the view background)."""
    w, h, ch, rows = decode(png)
    bg = rows[0][:3]
    n = 0
    for r in rows:
        for x in range(0, w * ch, ch):
            if abs(r[x] - bg[0]) > tol or abs(r[x + 1] - bg[1]) > tol or abs(r[x + 2] - bg[2]) > tol:
                n += 1
    return n / (w * h)
