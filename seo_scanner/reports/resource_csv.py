from __future__ import annotations

import csv
from pathlib import Path

from ..models import CrawlResult


def write_resource_csv(result: CrawlResult, path: Path) -> None:
    """Write a flat asset inventory suitable for sorting and spreadsheet review."""
    referrers: dict[str, set[str]] = {}
    for edge in result.edges:
        referrers.setdefault(edge.target_url, set()).add(edge.source_url)
    issue_ids: dict[str, set[str]] = {}
    for issue in result.issues:
        issue_ids.setdefault(issue.url, set()).add(issue.rule_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["url", "kind", "status", "final_url", "content_type", "bytes", "duration_ms", "width", "height", "format", "cache_control", "content_encoding", "referrers", "issues"])
        writer.writeheader()
        for resource in sorted(result.resources, key=lambda item: (-item.bytes, item.url)):
            writer.writerow({
                "url": resource.url, "kind": resource.kind, "status": resource.status, "final_url": resource.final_url,
                "content_type": resource.content_type, "bytes": resource.bytes, "duration_ms": resource.duration_ms,
                "width": resource.image_width, "height": resource.image_height, "format": resource.image_format,
                "cache_control": resource.cache_control, "content_encoding": resource.content_encoding,
                "referrers": " | ".join(sorted(referrers.get(resource.url, set()))),
                "issues": " | ".join(sorted(issue_ids.get(resource.url, set()))),
            })
