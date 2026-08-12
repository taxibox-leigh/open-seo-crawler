from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
import hashlib

SCHEMA_VERSION = "1.24"


@dataclass(frozen=True)
class HreflangReference:
    language: str
    url: str


@dataclass(frozen=True)
class ImageReference:
    url: str
    alt: str | None = None
    width: int | None = None
    height: int | None = None
    responsive: bool = False
    # One of the ALT_* constants in analyzers.alt_text. Empty alt is valid
    # markup for decorative images and a defect on content imagery, so the
    # two are distinguished at parse time where the DOM context is available.
    alt_state: str = "present"


@dataclass(frozen=True)
class LinkReference:
    url: str
    text: str = ""
    nofollow: bool = False


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
    meta_description: str = ""
    h1s: list[str] = field(default_factory=list)
    word_count: int = 0
    crawl_depth: int | None = None
    truncated: bool = False
    redirect_hops: list[str] = field(default_factory=list)
    redirect_statuses: list[int] = field(default_factory=list)
    canonical_url: str = ""
    canonical_urls: list[str] = field(default_factory=list)
    invalid_canonical_values: list[str] = field(default_factory=list)
    robots_directives: list[str] = field(default_factory=list)
    invalid_robots_directives: list[str] = field(default_factory=list)
    robots_conflicts: list[str] = field(default_factory=list)
    meta_refresh_url: str = ""
    meta_refresh_delay: float | None = None
    refresh_header: str = ""
    jsonld_errors: list[str] = field(default_factory=list)
    jsonld_blocks: list[dict[str, Any]] = field(default_factory=list)
    jsonld_integrity_errors: list[str] = field(default_factory=list)
    jsonld_integrity_warnings: list[str] = field(default_factory=list)
    hreflang: list[HreflangReference] = field(default_factory=list)
    declared_bytes: int | None = None
    viewport: bool = False
    og_title: str = ""
    og_description: str = ""
    og_image: str = ""
    og_url: str = ""
    twitter_card: str = ""
    twitter_image: str = ""
    images: list[ImageReference] = field(default_factory=list)
    url_length: int = 0
    query_parameter_count: int = 0
    html_language: str = ""
    content_languages: list[str] = field(default_factory=list)
    http_charset: str = ""
    meta_charsets: list[str] = field(default_factory=list)
    visible_text_hash: str = ""
    visible_text_fingerprint: str = ""
    document_head_count: int = 0
    document_body_count: int = 0
    title_count: int = 0
    meta_description_count: int = 0
    header_canonical_urls: list[str] = field(default_factory=list)
    header_hreflang: list[HreflangReference] = field(default_factory=list)
    html_canonical_urls: list[str] = field(default_factory=list)
    html_robots_directives: list[str] = field(default_factory=list)
    links: list[LinkReference] = field(default_factory=list)
    heading_levels: list[int] = field(default_factory=list)
    fetch_attempts: int = 1


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
    redirect_statuses: list[int] = field(default_factory=list)
    truncated: bool = False
    cache_control: str = ""
    content_encoding: str = ""
    etag: str = ""
    last_modified: str = ""
    image_width: int | None = None
    image_height: int | None = None
    image_format: str = ""
    content_hash: str = ""
    fetch_attempts: int = 1


@dataclass(frozen=True)
class Edge:
    source_url: str
    target_url: str
    context: str
    # rel tokens on the source anchor, space-joined and lowercased. Empty for
    # non-anchor edges. Without this the exported graph cannot tell a followed
    # link from a nofollowed one, which is what inlink-quality rules count.
    rel: str = ""


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
    rendered_pages_attempted: int = 0
    rendered_pages_succeeded: int = 0
    rendered_pages_available: int = 0
    rendered_sample_slice: int = 0
    rendered_sample_slices: int = 0


@dataclass
class RenderedPage:
    url: str
    final_url: str = ""
    duration_ms: int = 0
    console_errors: list[str] = field(default_factory=list)
    failed_requests: list[dict[str, Any]] = field(default_factory=list)
    network_requests: list[dict[str, Any]] = field(default_factory=list)
    request_count: int = 0
    transfer_bytes: int = 0
    network_requests_truncated: bool = False
    accessibility_violations: list[dict[str, Any]] = field(default_factory=list)
    accessibility_violations_total: int = 0
    accessibility_truncated: bool = False
    accessibility_error: str = ""
    title: str = ""
    meta_description: str = ""
    canonical_url: str = ""
    robots_directives: list[str] = field(default_factory=list)
    h1s: list[str] = field(default_factory=list)
    html_language: str = ""
    seo_signals_error: str = ""
    error: str = ""


@dataclass
class SitemapDocument:
    url: str
    status: int | None = None
    kind: str = "unknown"
    urls: list[str] = field(default_factory=list)
    child_sitemaps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class RobotsDocument:
    url: str
    status: int | None = None
    user_agent: str = ""
    bytes: int = 0
    truncated: bool = False
    errors: list[str] = field(default_factory=list)
    blocked_pages: list[str] = field(default_factory=list)
    blocked_resources: list[str] = field(default_factory=list)


@dataclass
class ExternalLinkTarget:
    url: str
    final_url: str = ""
    status: int | None = None
    redirect_hops: list[str] = field(default_factory=list)
    redirect_statuses: list[int] = field(default_factory=list)
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
    robots: RobotsDocument | None = None
    external_links: list[ExternalLinkTarget] = field(default_factory=list)
    rendered_pages: list[RenderedPage] = field(default_factory=list)
    comparison: BaselineComparison | None = None
    # Every defined rule and how many findings it produced. A rule sitting at
    # zero is either a clean site or a broken rule, and without this block
    # there is no way to tell the two apart from the report alone.
    rule_coverage: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
