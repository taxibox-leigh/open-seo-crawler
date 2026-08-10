from __future__ import annotations

import re
import struct
from xml.etree import ElementTree
from dataclasses import dataclass


@dataclass(frozen=True)
class ImageMetadata:
    width: int | None
    height: int | None
    format: str


def inspect_image(data: bytes) -> ImageMetadata | None:
    """Read dimensions from common web image headers without decoding pixels."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return _valid(width, height, "png")
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return _valid(width, height, "gif")
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return _webp(data)
    if data.startswith(b"\xff\xd8"):
        return _jpeg(data)
    if data.startswith(b"BM") and len(data) >= 26:
        width, height = struct.unpack("<ii", data[18:26])
        return _valid(abs(width), abs(height), "bmp")
    if data.startswith(b"\x00\x00\x01\x00") and len(data) >= 8:
        return _valid(data[6] or 256, data[7] or 256, "ico")
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return ImageMetadata(None, None, "tiff")
    if data.startswith(b"\xff\x0a") or data.startswith(b"\x00\x00\x00\x0cJXL \r\n\x87\n"):
        return ImageMetadata(None, None, "jxl")
    format_name = _isobmff_format(data)
    if format_name:
        dimensions = _ispe_dimensions(data)
        return ImageMetadata(dimensions[0], dimensions[1], format_name) if dimensions else ImageMetadata(None, None, format_name)
    svg = _svg(data)
    if svg:
        return svg
    return None


def _jpeg(data: bytes) -> ImageMetadata | None:
    offset = 2
    sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        offset += 2
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        length = struct.unpack(">H", data[offset:offset + 2])[0]
        if length < 2 or offset + length > len(data):
            return None
        if marker in sof and length >= 7:
            height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
            return _valid(width, height, "jpeg")
        offset += length
    return None


def _webp(data: bytes) -> ImageMetadata | None:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return _valid(width, height, "webp")
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return _valid((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1, "webp")
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        width, height = struct.unpack("<HH", data[26:30])
        return _valid(width & 0x3FFF, height & 0x3FFF, "webp")
    return None


def _valid(width: int, height: int, format_name: str) -> ImageMetadata | None:
    return ImageMetadata(width, height, format_name) if width > 0 and height > 0 else None


def _isobmff_format(data: bytes) -> str | None:
    if len(data) < 16 or data[4:8] != b"ftyp":
        return None
    brands = {data[offset:offset + 4] for offset in range(8, min(len(data), 64), 4)}
    if brands & {b"avif", b"avis"}:
        return "avif"
    if brands & {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
        return "heif"
    return None


def _ispe_dimensions(data: bytes) -> tuple[int, int] | None:
    offset = 0
    while True:
        offset = data.find(b"ispe", offset)
        if offset < 0:
            return None
        if offset >= 4 and offset + 16 <= len(data):
            box_size = int.from_bytes(data[offset - 4:offset], "big")
            width = int.from_bytes(data[offset + 8:offset + 12], "big")
            height = int.from_bytes(data[offset + 12:offset + 16], "big")
            if box_size >= 20 and width > 0 and height > 0:
                return width, height
        offset += 4


def _svg(data: bytes) -> ImageMetadata | None:
    prefix = data[:4096].lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
    if not (prefix.startswith(b"<svg") or (prefix.startswith(b"<?xml") and b"<svg" in prefix)):
        return None
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return None
    if root.tag.rsplit("}", 1)[-1].lower() != "svg":
        return None
    width = _svg_length(root.get("width"))
    height = _svg_length(root.get("height"))
    if (width is None or height is None) and root.get("viewBox"):
        values = re.split(r"[\s,]+", root.get("viewBox", "").strip())
        if len(values) == 4:
            try:
                width = width or round(float(values[2]))
                height = height or round(float(values[3]))
            except ValueError:
                pass
    return ImageMetadata(width, height, "svg")


def _svg_length(value: str | None) -> int | None:
    if not value:
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:px)?\s*", value, re.I)
    return round(float(match.group(1))) if match else None
