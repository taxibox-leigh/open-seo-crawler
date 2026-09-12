from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from typing import Any


@dataclass(frozen=True)
class ScannerConfig:
    max_pages: int = 2000
    max_resources: int = 10000
    max_total_bytes: int = 1_000_000_000
    max_resource_bytes: int = 25_000_000
    max_resource_size: int = 2_000_000
    max_image_width: int = 3840
    max_image_height: int = 2160
    min_compression_bytes: int = 10_000
    min_cache_seconds: int = 604800
    max_sitemaps: int = 50
    max_urls_per_sitemap: int = 50000
    max_sitemap_bytes: int = 52_428_800
    discover_sitemaps: bool = True
    validate_external_links: bool = False
    max_external_links: int = 1000
    max_link_bytes: int = 65536
    external_delay_seconds: float = 0.2
    timeout_seconds: float = 20.0
    max_duration_seconds: float = 3600.0
    user_agent: str = "open-seo-crawler/0.1 (+https://github.com/puneetindersingh/open-seo-crawler)"
    follow_external_resources: bool = False
    render_enabled: bool = False
    max_rendered_pages: int = 25
    render_navigation_timeout_ms: int = 30000
    render_settle_ms: int = 1000
    max_render_events_per_page: int = 50
    max_render_network_requests_per_page: int = 500
    max_render_request_count: int = 100
    max_render_transfer_bytes: int = 5_000_000
    render_sample_strategy: str = "first"
    accessibility_enabled: bool = False
    axe_script_path: str = "node_modules/axe-core/axe.min.js"
    max_accessibility_violations_per_page: int = 50
    max_accessibility_nodes_per_violation: int = 20
    max_click_depth: int = 3
    max_title_chars: int = 60
    max_meta_description_chars: int = 160
    min_content_words: int = 200
    robots_user_agent: str = "Googlebot"
    max_robots_bytes: int = 512000
    max_page_bytes: int = 10_000_000
    max_page_size: int = 2_000_000
    max_page_duration_ms: int = 3000
    max_url_chars: int = 115
    max_query_parameters: int = 3
    min_duplicate_content_words: int = 100
    near_duplicate_similarity: float = 0.90
    min_responsive_image_width: int = 1000
    min_legacy_image_bytes: int = 100_000
    # Byte weight, separate from pixel dimensions: a correctly-sized image can
    # still be far too heavy.
    max_image_bytes: int = 500_000
    # Google truncates around 60 characters; under 30 is rarely a real title.
    min_title_chars: int = 30
    min_meta_description_chars: int = 50
    min_internal_inlinks: int = 2
    min_site_pages_for_link_metrics: int = 5
    max_soft_404_words: int = 100
    max_fetch_attempts: int = 3
    # Off only for staging hosts with self-signed certificates.
    verify_tls: bool = True
    retry_backoff_seconds: float = 0.5
    max_retry_after_seconds: float = 10.0

    # Templated-page targeting (content.template_term_missing/_weak). Each
    # pattern is matched against a page's URL path and must carry a named
    # "term" group — e.g. r"^/self-storage/[^/]+/(?P<term>[^/]+)/?$" derives
    # the suburb slug from a `/self-storage/<city>/<suburb>/` URL. Empty by
    # default: this is inert until an operator declares which URL shapes are
    # targeted templates on their own site — the scanner does not guess.
    template_target_patterns: list[str] = field(default_factory=list)
    # Slugs that match a target pattern's shape but are not targeting terms
    # (a city-hub sub-page like /self-storage/brisbane/cost/ has the same
    # three-segment shape as a real suburb page). Second line of defence
    # behind the body-content check.
    template_term_stoplist: list[str] = field(default_factory=list)

    # canonical.pagination_to_first_page. Deliberately NOT shared with
    # _SITEMAP_EXEMPT_PATH in runner.py, which hardcodes the same shape for a
    # different purpose (sitemap-absence exemptions) — coupling them would
    # mean a pagination-pattern change silently alters sitemap findings too.
    pagination_path_pattern: str = r"/page/(\d+)/?$"

    # Rendered-diagnostics viewport/identity. Defaults to Lighthouse's mobile
    # preset (412x823, DSF 1.75) so the two tools stop disagreeing about what
    # "the page" looks like — Google indexes the mobile rendering.
    # render_user_agent defaulting to "" and falling back to `user_agent`
    # fixes a split identity: without this the scanner fetches raw HTML as
    # user_agent but renders as Playwright's own default headless UA.
    render_viewport_width: int = 412
    render_viewport_height: int = 823
    render_device_scale_factor: float = 1.75
    render_user_agent: str = ""

    def __post_init__(self) -> None:
        positive = ("max_pages", "max_resources", "max_total_bytes", "max_resource_bytes", "max_resource_size", "max_image_width", "max_image_height", "min_compression_bytes", "min_cache_seconds", "max_sitemaps", "max_urls_per_sitemap", "max_sitemap_bytes", "max_external_links", "max_link_bytes", "timeout_seconds", "max_duration_seconds", "max_rendered_pages", "render_navigation_timeout_ms", "max_render_events_per_page", "max_render_network_requests_per_page", "max_render_request_count", "max_render_transfer_bytes", "max_accessibility_violations_per_page", "max_accessibility_nodes_per_violation", "max_click_depth", "max_title_chars", "max_meta_description_chars", "min_content_words", "max_robots_bytes", "max_page_bytes", "max_page_size", "max_page_duration_ms", "max_url_chars", "max_query_parameters", "min_duplicate_content_words", "min_responsive_image_width", "min_legacy_image_bytes", "max_image_bytes", "min_title_chars", "min_meta_description_chars", "min_internal_inlinks", "min_site_pages_for_link_metrics", "max_soft_404_words", "max_fetch_attempts", "render_viewport_width", "render_viewport_height")
        for name in positive:
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if self.external_delay_seconds < 0:
            raise ValueError("external_delay_seconds must not be negative")
        if self.render_settle_ms < 0:
            raise ValueError("render_settle_ms must not be negative")
        if self.render_sample_strategy not in {"first", "daily_rotation"}:
            raise ValueError("render_sample_strategy must be first or daily_rotation")
        if self.accessibility_enabled and not self.render_enabled:
            raise ValueError("accessibility_enabled requires render_enabled")
        if not self.axe_script_path.strip():
            raise ValueError("axe_script_path must not be empty")
        if not self.robots_user_agent.strip():
            raise ValueError("robots_user_agent must not be empty")
        if self.max_page_size > self.max_page_bytes:
            raise ValueError("max_page_size must not exceed max_page_bytes")
        if self.min_title_chars > self.max_title_chars:
            raise ValueError("min_title_chars must not exceed max_title_chars")
        if self.min_meta_description_chars > self.max_meta_description_chars:
            raise ValueError("min_meta_description_chars must not exceed max_meta_description_chars")
        if self.retry_backoff_seconds < 0 or self.max_retry_after_seconds < 0:
            raise ValueError("retry delays must not be negative")
        if not 0 < self.near_duplicate_similarity <= 1:
            raise ValueError("near_duplicate_similarity must be greater than zero and at most one")
        for pattern in self.template_target_patterns:
            try:
                compiled = re.compile(pattern)
            except re.error as e:
                raise ValueError(f"invalid template_target_patterns pattern {pattern!r}: {e}") from e
            if "term" not in compiled.groupindex:
                raise ValueError(
                    f"template_target_patterns pattern {pattern!r} must contain a 'term' named group"
                )
        try:
            pagination_compiled = re.compile(self.pagination_path_pattern)
        except re.error as e:
            raise ValueError(f"invalid pagination_path_pattern: {e}") from e
        if pagination_compiled.groups != 1:
            raise ValueError("pagination_path_pattern must contain exactly one group")
        if self.render_device_scale_factor <= 0:
            raise ValueError("render_device_scale_factor must be greater than zero")

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "ScannerConfig":
        allowed = {item.name for item in fields(cls)}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
        return cls(**values)
