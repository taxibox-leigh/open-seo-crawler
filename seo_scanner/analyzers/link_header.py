from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import HreflangReference
from ..scope import normalize_url


@dataclass
class LinkHeaderSignals:
    canonical_urls: list[str] = field(default_factory=list)
    invalid_canonical_values: list[str] = field(default_factory=list)
    hreflang: list[HreflangReference] = field(default_factory=list)


_PARAMETER = re.compile(r";\s*([\w-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^;,\s]+))", re.I)


def parse_link_header(base_url: str, value: str) -> LinkHeaderSignals:
    signals = LinkHeaderSignals()
    for item in _split_links(value):
        match = re.match(r"\s*<([^>]*)>(.*)$", item)
        if not match:
            continue
        raw_target, parameters = match.groups()
        target = normalize_url(base_url, raw_target.strip())
        values = {
            name.lower(): quoted or single_quoted or bare or ""
            for name, quoted, single_quoted, bare in _PARAMETER.findall(parameters)
        }
        rels = {rel.lower() for rel in values.get("rel", "").split()}
        if "canonical" in rels:
            if target:
                signals.canonical_urls.append(target)
            else:
                signals.invalid_canonical_values.append(raw_target.strip() or "<missing target>")
        language = values.get("hreflang", "").strip()
        if "alternate" in rels and language and target:
            signals.hreflang.append(HreflangReference(language, target))
    return signals


def _split_links(value: str) -> list[str]:
    items: list[str] = []
    start = 0
    in_angle = False
    quote = ""
    for index, character in enumerate(value):
        if quote:
            if character == quote:
                quote = ""
        elif character in {'"', "'"}:
            quote = character
        elif character == "<":
            in_angle = True
        elif character == ">":
            in_angle = False
        elif character == "," and not in_angle:
            items.append(value[start:index])
            start = index + 1
    items.append(value[start:])
    return [item.strip() for item in items if item.strip()]
