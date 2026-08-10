from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
import hashlib

SCHEMA_VERSION = "1.6"


@dataclass(frozen=True)
class HreflangReference:
    language: str
    url: str


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
    issue_id: str = field(init=False)
    suppressed: bool = False

    def __post_init__(self) -> None:
        identity = f"{self.rule_id}\n{self.entity_type}\n{self.url}".encode("utf-8")
        self.issue_id = f"{self.rule_id}:{hashlib.sha256(identity).hexdigest()[:16]}"


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
    hreflang: list[HreflangReference] = field(default_factory=list)


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
    external_links_discovered: int = 0
    external_links_checked: int = 0


@dataclass
class SitemapDocument:
    url: str
    status: int | None = None
    kind: str = "unknown"
    urls: list[str] = field(default_factory=list)
    child_sitemaps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class ExternalLinkTarget:
    url: str
    final_url: str = ""
    status: int | None = None
    redirect_hops: list[str] = field(default_factory=list)
    referring_urls: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class BaselineComparison:
    new_issue_ids: list[str] = field(default_factory=list)
    persistent_issue_ids: list[str] = field(default_factory=list)
    resolved_issue_ids: list[str] = field(default_factory=list)
    suppressed_issue_ids: list[str] = field(default_factory=list)


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
    external_links: list[ExternalLinkTarget] = field(default_factory=list)
    comparison: BaselineComparison | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
