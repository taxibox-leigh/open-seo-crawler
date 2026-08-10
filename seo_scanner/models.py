from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = "1.3"


@dataclass
class Issue:
    rule_id: str
    title: str
    severity: str
    entity_type: str
    url: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    referring_urls: list[str] = field(default_factory=list)
    remediation: str = ""


@dataclass
class Page:
    url: str
    final_url: str
    status: int
    content_type: str
    bytes: int
    duration_ms: int
    title: str = ""
    truncated: bool = False
    redirect_hops: list[str] = field(default_factory=list)
    canonical_url: str = ""
    robots_directives: list[str] = field(default_factory=list)
    invalid_robots_directives: list[str] = field(default_factory=list)
    jsonld_errors: list[str] = field(default_factory=list)


@dataclass
class Resource:
    url: str
    final_url: str = ""
    kind: str = "other"
    status: int | None = None
    content_type: str = ""
    bytes: int = 0
    duration_ms: int = 0
    redirect_hops: list[str] = field(default_factory=list)
    truncated: bool = False
    cache_control: str = ""
    content_encoding: str = ""
    etag: str = ""
    last_modified: str = ""
    image_width: int | None = None
    image_height: int | None = None
    image_format: str = ""
    content_hash: str = ""


@dataclass(frozen=True)
class Edge:
    source_url: str
    target_url: str
    context: str


@dataclass
class Coverage:
    pages_fetched: int = 0
    pages_queued: int = 0
    resources_discovered: int = 0
    resources_fetched: int = 0
    bytes_downloaded: int = 0
    complete: bool = True
    limit_reason: str | None = None
    sitemaps_fetched: int = 0
    sitemap_urls_discovered: int = 0


@dataclass
class SitemapDocument:
    url: str
    status: int | None = None
    kind: str = "unknown"
    urls: list[str] = field(default_factory=list)
    child_sitemaps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class CrawlResult:
    start_url: str
    started_at: str
    finished_at: str = ""
    schema_version: str = SCHEMA_VERSION
    status: str = "running"
    pages: list[Page] = field(default_factory=list)
    resources: list[Resource] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    coverage: Coverage = field(default_factory=Coverage)
    sitemaps: list[SitemapDocument] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
