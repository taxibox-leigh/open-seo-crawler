from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlsplit


@dataclass
class UrlQuality:
    length: int
    query_parameter_count: int = 0
    uppercase_path: bool = False
    underscore_path: bool = False
    repeated_segments: list[str] = field(default_factory=list)
    tracking_parameters: list[str] = field(default_factory=list)


def analyze_url(url: str) -> UrlQuality:
    parts = urlsplit(url)
    path = unquote(parts.path)
    segments = [item for item in path.split("/") if item]
    repeated = sorted({
        segments[index] for index in range(1, len(segments))
        if segments[index].casefold() == segments[index - 1].casefold()
    })
    parameters = parse_qsl(parts.query, keep_blank_values=True)
    tracking_names = sorted({
        name for name, _ in parameters
        if name.casefold().startswith("utm_")
        or name.casefold() in {"gclid", "dclid", "fbclid", "msclkid", "mc_cid", "mc_eid"}
    })
    return UrlQuality(
        length=len(url), query_parameter_count=len(parameters),
        uppercase_path=any(character.isupper() for character in path),
        underscore_path="_" in path, repeated_segments=repeated,
        tracking_parameters=tracking_names,
    )
