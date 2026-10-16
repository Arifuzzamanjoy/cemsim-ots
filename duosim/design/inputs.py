"""Reference images for design-by-image (photos, sketches, drawings, screenshots of other CAD tools).

Images arrive as data URLs (`data:image/png;base64,...`) in the JSON body.  The declared type is never
trusted: the bytes are sniffed, and only PNG, JPEG, WebP and GIF of bounded size are forwarded to the
vision model.  Nothing is decoded or rendered server-side.
"""

from __future__ import annotations

import base64
import binascii
import re
import struct
from dataclasses import dataclass

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 6 * 1024 * 1024
MAX_PIXELS = 50_000_000
_DATA_URL = re.compile(r"^data:([a-z/+.\-]+);base64,(.*)$", re.S)


class ImageError(ValueError):
    pass


@dataclass(frozen=True)
class Image:
    mime: str
    data: bytes
    width: int | None = None
    height: int | None = None

    def b64(self) -> str:
        return base64.b64encode(self.data).decode()

    def data_url(self) -> str:
        return f"data:{self.mime};base64,{self.b64()}"


def sniff(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def dimensions(mime: str, data: bytes) -> tuple[int, int] | None:
    try:
        if mime == "image/png" and len(data) >= 24:
            return struct.unpack(">II", data[16:24])
        if mime == "image/gif" and len(data) >= 10:
            return struct.unpack("<HH", data[6:10])
        if mime == "image/jpeg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                    return w, h
                seg = struct.unpack(">H", data[i + 2 : i + 4])[0]
                i += 2 + seg
        if mime == "image/webp" and len(data) >= 30:
            chunk = data[12:16]
            if chunk == b"VP8X":
                w = 1 + int.from_bytes(data[24:27], "little")
                h = 1 + int.from_bytes(data[27:30], "little")
                return w, h
            if chunk == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if chunk == b"VP8L":
                b = int.from_bytes(data[21:25], "little")
                return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
    except struct.error:
        return None
    return None


def parse_image(value: object, index: int = 0) -> Image:
    where = f"images[{index}]"
    if not isinstance(value, str):
        raise ImageError(f"{where}: must be a data URL string")
    m = _DATA_URL.match(value.strip()[: MAX_IMAGE_BYTES * 2])
    if not m:
        raise ImageError(f"{where}: must be a data:image/...;base64, URL")
    if len(m.group(2)) > MAX_IMAGE_BYTES * 4 // 3 + 8:
        raise ImageError(f"{where}: larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    try:
        data = base64.b64decode(m.group(2), validate=True)
    except (binascii.Error, ValueError):
        raise ImageError(f"{where}: invalid base64") from None
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError(f"{where}: larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    mime = sniff(data)
    if mime is None:
        raise ImageError(f"{where}: not a PNG, JPEG, WebP or GIF image")
    dim = dimensions(mime, data)
    if dim and (dim[0] < 8 or dim[1] < 8 or dim[0] * dim[1] > MAX_PIXELS):
        raise ImageError(f"{where}: {dim[0]}x{dim[1]} px is outside the accepted size")
    return Image(mime, data, *(dim or (None, None)))


def parse_images(values: list | None) -> list[Image]:
    if not values:
        return []
    if not isinstance(values, list):
        raise ImageError("images must be a list of data URLs")
    if len(values) > MAX_IMAGES:
        raise ImageError(f"at most {MAX_IMAGES} images per request")
    return [parse_image(v, i) for i, v in enumerate(values)]
