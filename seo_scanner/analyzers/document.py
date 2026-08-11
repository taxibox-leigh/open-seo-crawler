from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DocumentSignals:
    head_count: int = 0
    body_count: int = 0
    title_count: int = 0
    meta_description_count: int = 0
    title_outside_head: bool = False
    meta_description_outside_head: bool = False


_OPEN_TAG = re.compile(r"<\s*(head|body|title)\b[^>]*>", re.I)
_HEAD_CLOSE = re.compile(r"<\s*/\s*head\s*>", re.I)
_META_TAG = re.compile(r"<\s*meta\b[^>]*>", re.I)
_DESCRIPTION_NAME = re.compile(r"\bname\s*=\s*(?:['\"]\s*)?description(?:\s*['\"])?(?:\s|/?>)", re.I)


def analyze_document(html: str) -> DocumentSignals:
    tags = [(match.group(1).lower(), match.start()) for match in _OPEN_TAG.finditer(html)]
    head_positions = [position for name, position in tags if name == "head"]
    body_count = sum(name == "body" for name, _ in tags)
    title_positions = [position for name, position in tags if name == "title"]
    descriptions = [match.start() for match in _META_TAG.finditer(html) if _DESCRIPTION_NAME.search(match.group(0))]
    head_end = _HEAD_CLOSE.search(html, head_positions[0] if head_positions else 0)
    head_range = (head_positions[0], head_end.start()) if head_positions and head_end else None

    def outside(position: int) -> bool:
        return head_range is not None and not (head_range[0] <= position < head_range[1])

    return DocumentSignals(
        head_count=len(head_positions), body_count=body_count,
        title_count=len(title_positions), meta_description_count=len(descriptions),
        title_outside_head=any(outside(position) for position in title_positions),
        meta_description_outside_head=any(outside(position) for position in descriptions),
    )
