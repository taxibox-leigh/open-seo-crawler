from __future__ import annotations

from dataclasses import dataclass, fields
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
    render_sample_strategy: str = "first"
    max_click_depth: int = 3
    max_title_chars: int = 60
    max_meta_description_chars: int = 160
    min_content_words: int = 200
    robots_user_agent: str = "Googlebot"
    max_robots_bytes: int = 512000

    def __post_init__(self) -> None:
        positive = ("max_pages", "max_resources", "max_total_bytes", "max_resource_bytes", "max_resource_size", "max_image_width", "max_image_height", "min_compression_bytes", "min_cache_seconds", "max_sitemaps", "max_urls_per_sitemap", "max_sitemap_bytes", "max_external_links", "max_link_bytes", "timeout_seconds", "max_duration_seconds", "max_rendered_pages", "render_navigation_timeout_ms", "max_render_events_per_page", "max_click_depth", "max_title_chars", "max_meta_description_chars", "min_content_words", "max_robots_bytes")
        for name in positive:
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if self.external_delay_seconds < 0:
            raise ValueError("external_delay_seconds must not be negative")
        if self.render_settle_ms < 0:
            raise ValueError("render_settle_ms must not be negative")
        if self.render_sample_strategy not in {"first", "daily_rotation"}:
            raise ValueError("render_sample_strategy must be first or daily_rotation")
        if not self.robots_user_agent.strip():
            raise ValueError("robots_user_agent must not be empty")

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "ScannerConfig":
        allowed = {item.name for item in fields(cls)}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
        return cls(**values)
