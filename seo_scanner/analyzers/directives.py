from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from ..scope import normalize_url
from ..models import HreflangReference


@dataclass
class PageSignals:
    html_language: str = ""
    html_language_declared: bool = False
    canonical_url: str = ""
    canonical_urls: list[str] = field(default_factory=list)
    invalid_canonical_values: list[str] = field(default_factory=list)
    robots_directives: list[str] = field(default_factory=list)
    invalid_robots_directives: list[str] = field(default_factory=list)
    robots_conflicts: list[str] = field(default_factory=list)
    meta_refresh_url: str = ""
    meta_refresh_delay: float | None = None
    jsonld_errors: list[str] = field(default_factory=list)
    jsonld_blocks: list[dict[str, object]] = field(default_factory=list)
    duplicate_jsonld_blocks: list[dict[str, object]] = field(default_factory=list)
    jsonld_integrity_errors: list[str] = field(default_factory=list)
    jsonld_integrity_warnings: list[str] = field(default_factory=list)
    hreflang: list[HreflangReference] = field(default_factory=list)
    html_canonical_urls: list[str] = field(default_factory=list)
    header_canonical_urls: list[str] = field(default_factory=list)
    header_hreflang: list[HreflangReference] = field(default_factory=list)
    html_robots_directives: list[str] = field(default_factory=list)


_SIMPLE_DIRECTIVES = {"all", "none", "index", "noindex", "follow", "nofollow", "noarchive", "nosnippet", "noimageindex", "nocache", "notranslate", "nopagereadaloud"}
_VALUE_DIRECTIVES = {"max-snippet", "max-image-preview", "max-video-preview", "unavailable_after", "indexifembedded"}


def extract_page_signals(page_url: str, html: str, x_robots_tag: str = "") -> PageSignals:
    soup = BeautifulSoup(html, "lxml")
    html_tag = soup.find("html")
    html_language_declared = bool(html_tag and html_tag.has_attr("lang"))
    html_language = str(html_tag.get("lang", "")).strip() if html_tag else ""
    canonical_tags = soup.select('link[rel~="canonical"]')
    canonical_urls: list[str] = []
    invalid_canonicals: list[str] = []
    for canonical in canonical_tags:
        raw_value = str(canonical.get("href", "")).strip()
        target = normalize_url(page_url, raw_value) if raw_value else None
        if target:
            canonical_urls.append(target)
        else:
            invalid_canonicals.append(raw_value or "<missing href>")
    canonical_url = canonical_urls[0] if canonical_urls else None
    raw_directives = [tag.get("content", "") for tag in soup.select('meta[name="robots" i], meta[name="googlebot" i], meta[name="bingbot" i]')]
    html_directives = sorted(set(_parse_directives(raw_directives)))
    if x_robots_tag:
        raw_directives.append(x_robots_tag)
    directives = _parse_directives(raw_directives)
    invalid = sorted({item for item in directives if not _valid_directive(item)})
    conflicts = _directive_conflicts(directives)
    refresh_url = ""
    refresh_delay = None
    refresh = soup.find("meta", attrs={"http-equiv": re.compile(r"^refresh$", re.I)})
    if refresh:
        match = re.match(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(?:;\s*url\s*=\s*['\"]?([^'\"]+))?", str(refresh.get("content", "")), re.I)
        if match:
            refresh_delay = float(match.group(1))
            refresh_url = normalize_url(page_url, match.group(2).strip()) if match.group(2) else ""
    jsonld_errors: list[str] = []
    jsonld_blocks: list[dict[str, object]] = []
    blocks_by_value: dict[str, list[int]] = {}
    parsed_blocks: list[tuple[int, object]] = []
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
        parsed_blocks.append((index, parsed))
        jsonld_blocks.append({"index": index, "types": _jsonld_types(parsed)})
    duplicates = [
        {"block_indices": indices, "types": _jsonld_types(json.loads(value))}
        for value, indices in blocks_by_value.items() if len(indices) > 1
    ]
    integrity_errors, integrity_warnings = _jsonld_integrity(parsed_blocks)
    hreflang: list[HreflangReference] = []
    for tag in soup.select('link[rel~="alternate"][hreflang][href]'):
        target = normalize_url(page_url, tag.get("href", ""))
        language = tag.get("hreflang", "").strip()
        if target and language:
            hreflang.append(HreflangReference(language, target))
    return PageSignals(
        html_language=html_language, html_language_declared=html_language_declared,
        canonical_url=canonical_url or "", canonical_urls=canonical_urls,
        invalid_canonical_values=invalid_canonicals,
        robots_directives=sorted(set(directives)), invalid_robots_directives=invalid,
        robots_conflicts=conflicts, meta_refresh_url=refresh_url or "",
        meta_refresh_delay=refresh_delay, jsonld_errors=jsonld_errors,
        jsonld_blocks=jsonld_blocks, duplicate_jsonld_blocks=duplicates,
        jsonld_integrity_errors=integrity_errors,
        jsonld_integrity_warnings=integrity_warnings, hreflang=hreflang,
        html_canonical_urls=canonical_urls,
        html_robots_directives=html_directives,
    )


def _jsonld_integrity(blocks: list[tuple[int, object]]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    definitions: set[str] = set()
    fragment_references: set[str] = set()

    def visit(value: object, block: int, path: str) -> None:
        if isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, block, f"{path}[{index}]")
            return
        if not isinstance(value, dict):
            return
        kind = value.get("@type")
        if "@type" in value and not (
            isinstance(kind, str) and kind.strip()
            or isinstance(kind, list) and kind and all(isinstance(item, str) and item.strip() for item in kind)
        ):
            errors.append(f"Block {block} {path}: @type must be a non-empty string or string array")
        context = value.get("@context")
        if "@context" in value and not isinstance(context, (str, dict, list)):
            errors.append(f"Block {block} {path}: @context must be a string, object, or array")
        graph = value.get("@graph")
        if "@graph" in value and not isinstance(graph, (dict, list)):
            errors.append(f"Block {block} {path}: @graph must be an object or array")
        identifier = value.get("@id")
        if "@id" in value and not (isinstance(identifier, str) and identifier.strip()):
            errors.append(f"Block {block} {path}: @id must be a non-empty string")
        elif isinstance(identifier, str) and identifier.startswith("#"):
            if set(value) <= {"@id", "@context"}:
                fragment_references.add(identifier)
            else:
                definitions.add(identifier)
        for key, child in value.items():
            if key != "@context":
                visit(child, block, f"{path}.{key}")

    for block, value in blocks:
        if not isinstance(value, (dict, list)):
            errors.append(f"Block {block}: root must be an object or array")
            continue
        if _jsonld_types(value) and not _has_context(value):
            warnings.append(f"Block {block}: typed data has no @context")
        visit(value, block, "$")
    for identifier in sorted(fragment_references - definitions):
        warnings.append(f"Local @id reference {identifier} has no definition on the page")
    return sorted(set(errors)), sorted(set(warnings))


def _has_context(value: object) -> bool:
    if isinstance(value, dict):
        return "@context" in value or any(_has_context(child) for child in value.values())
    if isinstance(value, list):
        return any(_has_context(child) for child in value)
    return False


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


def _directive_conflicts(directives: list[str]) -> list[str]:
    values = set(directives)
    conflicts: list[str] = []
    if "index" in values and values & {"noindex", "none"}:
        conflicts.append("index conflicts with noindex/none")
    if "follow" in values and values & {"nofollow", "none"}:
        conflicts.append("follow conflicts with nofollow/none")
    if "all" in values and values & {"noindex", "nofollow", "none"}:
        conflicts.append("all conflicts with restrictive directives")
    return conflicts
