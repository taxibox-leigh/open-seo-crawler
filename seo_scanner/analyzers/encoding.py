from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field


@dataclass
class EncodingSignals:
    http_charset: str = ""
    meta_charsets: list[str] = field(default_factory=list)
    invalid_charsets: list[str] = field(default_factory=list)
    meta_charset_offsets: list[int] = field(default_factory=list)

    @property
    def effective_charset(self) -> str:
        for value in [self.http_charset, *self.meta_charsets]:
            if _canonical_charset(value):
                return value
        return "utf-8"

    @property
    def canonical_charsets(self) -> set[str]:
        return {
            canonical for value in [self.http_charset, *self.meta_charsets]
            if (canonical := _canonical_charset(value))
        }


_CHARSET_PARAMETER = re.compile(r"(?:^|;)\s*charset\s*=\s*['\"]?([^\s;'\"]+)", re.I)
_META_TAG = re.compile(br"<meta\b[^>]*>", re.I)
_META_CHARSET = re.compile(br"\scharset\s*=\s*(?:['\"]\s*)?([^\s'\"/>;]+)", re.I)
_HTTP_EQUIV = re.compile(br"\bhttp-equiv\s*=\s*(?:['\"]\s*)?content-type\b", re.I)


def analyze_encoding(body: bytes, content_type_header: str) -> EncodingSignals:
    parameter = _CHARSET_PARAMETER.search(content_type_header)
    http_charset = parameter.group(1).strip() if parameter else ""
    meta_charsets: list[str] = []
    offsets: list[int] = []
    for match in _META_TAG.finditer(body):
        tag = match.group(0)
        charset = _META_CHARSET.search(tag)
        if charset and (b"charset" in tag.lower() and (b"http-equiv" not in tag.lower() or _HTTP_EQUIV.search(tag))):
            meta_charsets.append(charset.group(1).decode("ascii", errors="replace").strip())
            offsets.append(match.end())
    declared = [value for value in [http_charset, *meta_charsets] if value]
    invalid = sorted({value for value in declared if not _canonical_charset(value)})
    return EncodingSignals(http_charset, meta_charsets, invalid, offsets)


def _canonical_charset(value: str) -> str:
    if not value:
        return ""
    try:
        return codecs.lookup(value).name
    except LookupError:
        return ""
