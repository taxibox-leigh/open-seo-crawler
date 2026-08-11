from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

from ..models import CrawlResult


def write_ndjson(result: CrawlResult, path: Path) -> None:
    """Write one independently parseable inventory record per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        _write(handle, "scan", {
            "schema_version": result.schema_version,
            "start_url": result.start_url,
            "started_at": result.started_at,
        })
        for kind, values in (
            ("page", result.pages),
            ("resource", result.resources),
            ("edge", result.edges),
            ("issue", result.issues),
            ("sitemap", result.sitemaps),
            ("external_link", result.external_links),
            ("rendered_page", result.rendered_pages),
        ):
            for value in values:
                _write(handle, kind, asdict(value))
        if result.robots is not None:
            _write(handle, "robots", asdict(result.robots))
        _write(handle, "summary", {
            "status": result.status,
            "finished_at": result.finished_at,
            "coverage": asdict(result.coverage),
            "errors": result.errors,
            "comparison": asdict(result.comparison) if result.comparison else None,
        })


def _write(handle: Any, record_type: str, value: dict[str, Any]) -> None:
    handle.write(json.dumps({"type": record_type, **value}, separators=(",", ":"), ensure_ascii=False) + "\n")
