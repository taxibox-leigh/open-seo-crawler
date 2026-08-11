from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from ..scope import normalize_url
from ..models import HreflangReference


@dataclass
class PageSignals:
    canonical_url: str = ""
    robots_directives: list[str] = field(default_factory=list)
    invalid_robots_directives: list[str] = field(default_factory=list)
    jsonld_errors: list[str] = field(default_factory=list)
    jsonld_blocks: list[dict[str, object]] = field(default_factory=list)
    duplicate_jsonld_blocks: list[dict[str, object]] = field(default_factory=list)
    hreflang: list[HreflangReference] = field(default_factory=list)


_SIMPLE_DIRECTIVES = {"all", "none", "index", "noindex", "follow", "nofollow", "noarchive", "nosnippet", "noimageindex", "nocache", "notranslate", "nopagereadaloud"}
_VALUE_DIRECTIVES = {"max-snippet", "max-image-preview", "max-video-preview", "unavailable_after", "indexifembedded"}


def extract_page_signals(page_url: str, html: str, x_robots_tag: str = "") -> PageSignals:
    soup = BeautifulSoup(html, "lxml")
    canonical = soup.select_one('link[rel~="canonical"][href]')
    canonical_url = normalize_url(page_url, canonical.get("href", "")) if canonical else None
    raw_directives = [tag.get("content", "") for tag in soup.select('meta[name="robots" i], meta[name="googlebot" i], meta[name="bingbot" i]')]
    if x_robots_tag:
        raw_directives.append(x_robots_tag)
    directives = _parse_directives(raw_directives)
    invalid = sorted({item for item in directives if not _valid_directive(item)})
    jsonld_errors: list[str] = []
    jsonld_blocks: list[dict[str, object]] = []
    blocks_by_value: dict[str, list[int]] = {}
    for index, tag in enumerate(soup.select('script[type="application/ld+json" i]'), start=1):
        value = tag.string or tag.get_text()
        if not value.strip():
            jsonld_errors.append(f"Block {index} is empty")
            continue
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            jsonld_errors.append(f"Block {index}: {exc.msg} at line {exc.lineno} column {exc.colno}")
            continue
        canonical_value = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        blocks_by_value.setdefault(canonical_value, []).append(index)
        jsonld_blocks.append({"index": index, "types": _jsonld_types(parsed)})
    duplicates = [
        {"block_indices": indices, "types": _jsonld_types(json.loads(value))}
        for value, indices in blocks_by_value.items() if len(indices) > 1
    ]
    hreflang: list[HreflangReference] = []
    for tag in soup.select('link[rel~="alternate"][hreflang][href]'):
        target = normalize_url(page_url, tag.get("href", ""))
        language = tag.get("hreflang", "").strip()
        if target and language:
            hreflang.append(HreflangReference(language, target))
    return PageSignals(canonical_url or "", sorted(set(directives)), invalid, jsonld_errors, jsonld_blocks, duplicates, hreflang)


def _jsonld_types(value: object) -> list[str]:
    found: set[str] = set()

    def visit(item: object) -> None:
        if isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, dict):
            kind = item.get("@type")
            if isinstance(kind, str):
                found.add(kind)
            elif isinstance(kind, list):
                found.update(entry for entry in kind if isinstance(entry, str))
            if "@graph" in item:
                visit(item["@graph"])

    visit(value)
    return sorted(found)


def _parse_directives(values: list[str]) -> list[str]:
    tokens: list[str] = []
    for value in values:
        # X-Robots-Tag can prefix a user agent (`googlebot: noindex`).
        value = re.sub(r"(?:^|,)\s*[a-z][\w-]*\s*:\s*(?=(?:no)?(?:index|follow|archive|snippet|imageindex)|all|none)", ",", value, flags=re.I)
        tokens.extend(item.strip().lower() for item in value.split(",") if item.strip())
    return tokens


def _valid_directive(value: str) -> bool:
    if value in _SIMPLE_DIRECTIVES or value == "indexifembedded":
        return True
    name, separator, argument = value.partition(":")
    return bool(separator and argument.strip() and name.strip() in _VALUE_DIRECTIVES)
