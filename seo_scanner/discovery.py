from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from .models import Edge
from .scope import normalize_url


@dataclass(frozen=True)
class DiscoveredResource:
    url: str
    kind: str


_CSS_URL = re.compile(r"(?:url\(\s*(['\"]?)(.*?)\1\s*\)|@import\s+(?:url\()?\s*(['\"])(.*?)\3)", re.I)


def _srcset(value: str) -> list[str]:
    return [candidate.strip().split()[0] for candidate in value.split(",") if candidate.strip()]


def discover_html(page_url: str, html: str) -> tuple[list[str], list[DiscoveredResource], list[Edge]]:
    soup = BeautifulSoup(html, "lxml")
    pages: list[str] = []
    resources: dict[str, str] = {}
    edges: set[Edge] = set()

    def add(value: str | None, kind: str, context: str) -> None:
        if not value:
            return
        target = normalize_url(page_url, value)
        if target:
            resources.setdefault(target, kind)
            edges.add(Edge(page_url, target, context))

    for anchor in soup.select("a[href]"):
        target = normalize_url(page_url, anchor.get("href", ""))
        if target:
            pages.append(target)
            rel = anchor.get("rel") or []
            rel_value = " ".join(str(token).lower() for token in rel) if isinstance(rel, list) else str(rel).lower()
            edges.add(Edge(page_url, target, "a.href", rel_value.strip()))
    for tag in soup.select("img[src], input[type=image][src]"):
        add(tag.get("src"), "image", "img.src")
    for tag in soup.select("img[srcset], source[srcset]"):
        for value in _srcset(tag.get("srcset", "")):
            add(value, "image", "srcset")
    for tag in soup.select("script[src]"):
        add(tag.get("src"), "script", "script.src")
    for tag in soup.select("link[href]"):
        rel = {str(item).lower() for item in tag.get("rel", [])}
        kind = "stylesheet" if "stylesheet" in rel else "image" if rel & {"icon", "apple-touch-icon"} else "manifest" if "manifest" in rel else "other"
        if rel & {"stylesheet", "icon", "apple-touch-icon", "manifest", "preload", "prefetch", "modulepreload"}:
            add(tag.get("href"), kind, "link." + (next(iter(rel)) if rel else "href"))
    for tag in soup.select("video[src], audio[src], source[src], track[src], iframe[src], embed[src]"):
        add(tag.get("src"), "media", f"{tag.name}.src")
    for tag in soup.select("object[data]"):
        add(tag.get("data"), "document", "object.data")
    for tag in soup.select("[style]"):
        for item in discover_css(page_url, tag.get("style", "")):
            add(item.url, item.kind, "style.url")
    return list(dict.fromkeys(pages)), [DiscoveredResource(url, kind) for url, kind in resources.items()], sorted(edges, key=lambda e: (e.target_url, e.context))


def discover_css(css_url: str, css: str) -> list[DiscoveredResource]:
    found: dict[str, str] = {}
    for match in _CSS_URL.finditer(css):
        value = match.group(2) or match.group(4)
        target = normalize_url(css_url, value)
        if target:
            suffix = target.split("?", 1)[0].lower()
            kind = "stylesheet" if value.lower().endswith(".css") or match.group(4) else "font" if suffix.endswith((".woff", ".woff2", ".ttf", ".otf")) else "image" if suffix.endswith((".png", ".apng", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".heic", ".heif", ".jxl", ".ico", ".bmp", ".tif", ".tiff")) else "other"
            found.setdefault(target, kind)
    return [DiscoveredResource(url, kind) for url, kind in found.items()]
