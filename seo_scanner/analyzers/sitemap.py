from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from gzip import GzipFile
from io import BytesIO

from lxml import etree

from ..scope import normalize_url


@dataclass
class ParsedSitemap:
    kind: str = "unknown"
    urls: list[str] = field(default_factory=list)
    child_sitemaps: list[str] = field(default_factory=list)
    invalid_lastmod: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def parse_sitemap(sitemap_url: str, body: bytes, max_uncompressed_bytes: int = 52_428_800) -> ParsedSitemap:
    parsed = ParsedSitemap()
    if body.startswith(b"\x1f\x8b"):
        try:
            with GzipFile(fileobj=BytesIO(body)) as archive:
                body = archive.read(max_uncompressed_bytes + 1)
        except OSError as exc:
            parsed.errors.append(f"Invalid gzip sitemap: {exc}")
            return parsed
    if len(body) > max_uncompressed_bytes:
        parsed.errors.append(f"Uncompressed sitemap exceeds {max_uncompressed_bytes} bytes")
        return parsed
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False, huge_tree=False)
        root = etree.fromstring(body, parser=parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        parsed.errors.append(str(exc))
        return parsed
    name = etree.QName(root).localname.lower()
    if name not in {"urlset", "sitemapindex"}:
        parsed.errors.append(f"Unsupported root element: {name}")
        return parsed
    parsed.kind = name
    entry_name = "url" if name == "urlset" else "sitemap"
    for entry in root.xpath(f'./*[local-name()="{entry_name}"]'):
        locations = entry.xpath('./*[local-name()="loc"]/text()')
        if not locations:
            parsed.errors.append(f"{entry_name} entry is missing loc")
            continue
        target = normalize_url(sitemap_url, str(locations[0]))
        if not target:
            parsed.errors.append(f"Invalid loc: {locations[0]}")
            continue
        if name == "urlset":
            parsed.urls.append(target)
            lastmods = entry.xpath('./*[local-name()="lastmod"]/text()')
            if lastmods and not _valid_lastmod(str(lastmods[0]).strip()):
                parsed.invalid_lastmod.append({"url": target, "lastmod": str(lastmods[0]).strip()})
        else:
            parsed.child_sitemaps.append(target)
    return parsed


def sitemap_locations_from_robots(robots_url: str, body: bytes) -> list[str]:
    locations: list[str] = []
    for raw_line in body.decode("utf-8", errors="replace").splitlines():
        name, separator, value = raw_line.partition(":")
        if separator and name.strip().lower() == "sitemap":
            target = normalize_url(robots_url, value.strip())
            if target:
                locations.append(target)
    return list(dict.fromkeys(locations))


def _valid_lastmod(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False
