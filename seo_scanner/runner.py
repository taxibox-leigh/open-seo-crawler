from __future__ import annotations

import time
import hashlib
import re
from dataclasses import dataclass, field
from collections import deque
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlsplit

import requests
from .config import ScannerConfig
from .analyzers.image import inspect_image
from .analyzers.content import PageContent, extract_page_content
from .analyzers.directives import PageSignals, extract_page_signals
from .analyzers.sitemap import parse_sitemap, sitemap_locations_from_robots
from .analyzers.robots import RobotsPolicy, parse_robots
from .analyzers.hreflang import analyze_hreflang
from .discovery import DiscoveredResource, discover_css, discover_html
from .fetch import Fetcher, FetchResponse
from .models import Coverage, CrawlResult, Edge, ExternalLinkTarget, Issue, Page, Resource, RobotsDocument, SitemapDocument
from .rules import get_rule
from .scope import normalize_url, same_origin
from .render import render_pages, select_render_urls

ProgressCallback = Callable[[dict[str, object]], None]


@dataclass
class _RunState:
    start_url: str
    result: CrawlResult
    deadline: float
    page_queue: deque[str] = field(default_factory=deque)
    queued_pages: set[str] = field(default_factory=set)
    seen_pages: set[str] = field(default_factory=set)
    pending_resources: dict[str, str] = field(default_factory=dict)
    seen_resources: set[str] = field(default_factory=set)
    edges: set[Edge] = field(default_factory=set)
    robots_policy: RobotsPolicy | None = None


class Scanner:
    def __init__(self, config: ScannerConfig | None = None, progress: ProgressCallback | None = None) -> None:
        self.config = config or ScannerConfig()
        self.progress = progress

    def scan(self, start_url: str) -> CrawlResult:
        start = normalize_url(start_url, start_url)
        if not start:
            raise ValueError("start_url must be an absolute HTTP(S) URL")
        result = CrawlResult(start_url=start, started_at=_now())
        state = _RunState(
            start_url=start,
            result=result,
            deadline=time.monotonic() + self.config.max_duration_seconds,
            page_queue=deque([start]),
            queued_pages={start},
        )

        with Fetcher(self.config.user_agent, self.config.timeout_seconds) as fetcher:
            if self.config.discover_sitemaps:
                sitemap_pages, state.robots_policy = self._crawl_sitemaps(fetcher, result, start, state.deadline)
                for sitemap_page in sitemap_pages:
                    if same_origin(start, sitemap_page):
                        self._enqueue_page(state, sitemap_page)
            self._crawl_pages(fetcher, state)
            if self.config.validate_external_links:
                self._check_external_links(fetcher, state)
            self._crawl_resources(fetcher, state)

        # Keep optional browser sampling outside the raw crawl's deadline and
        # byte budgets so it cannot make otherwise complete coverage partial.
        if self.config.render_enabled:
            self._run_rendered_diagnostics(result)

        result.edges = sorted(state.edges, key=lambda edge: (edge.source_url, edge.target_url, edge.context))
        self._add_graph_issues(result, state.edges)
        self._add_duplicate_issues(result, state.edges)
        self._add_canonical_issues(result, state.edges)
        self._add_sitemap_issues(result, state.edges)
        self._add_architecture_issues(result, state.edges)
        self._add_content_issues(result, state.edges)
        self._add_robots_issues(result, state.edges, state.robots_policy)
        result.issues.extend(analyze_hreflang(result))
        result.coverage.pages_queued = len(state.page_queue)
        result.coverage.resources_discovered = len(state.pending_resources)
        result.finished_at = _now()
        result.status = "partial" if not result.coverage.complete else "complete"
        return result

    def _run_rendered_diagnostics(self, result: CrawlResult) -> None:
        urls = [
            page.final_url or page.url for page in result.pages
            if page.status < 400 and page.content_type in ("text/html", "application/xhtml+xml")
        ]
        selected, available, selected_slice, slices = select_render_urls(urls, self.config)
        rendered, setup_error = render_pages(selected, self.config)
        result.rendered_pages = rendered
        result.coverage.rendered_pages_attempted = len(rendered)
        result.coverage.rendered_pages_succeeded = sum(not page.error for page in rendered)
        result.coverage.rendered_pages_available = available
        result.coverage.rendered_sample_slice = selected_slice
        result.coverage.rendered_sample_slices = slices
        if setup_error:
            result.issues.append(self._issue("render.unavailable", "crawl", result.start_url, setup_error))
        for page in rendered:
            if page.error:
                result.issues.append(self._issue("render.navigation_failed", "page", page.url, page.error, evidence={"final_url": page.final_url}))
            if page.failed_requests:
                result.issues.append(self._issue("render.failed_requests", "page", page.url, f"Browser rendering encountered {len(page.failed_requests)} failed requests", evidence={"requests": page.failed_requests}))
            if page.console_errors:
                result.issues.append(self._issue("render.console_errors", "page", page.url, f"Browser rendering emitted {len(page.console_errors)} console errors", evidence={"errors": page.console_errors}))

    def _crawl_pages(self, fetcher: Fetcher, state: _RunState) -> None:
        while state.page_queue:
            if self._limit(state.result, state.deadline, len(state.seen_pages) >= self.config.max_pages, "max_pages"):
                return
            if self._limit(state.result, state.deadline, state.result.coverage.bytes_downloaded >= self.config.max_total_bytes, "max_total_bytes"):
                return
            url = state.page_queue.popleft()
            state.queued_pages.discard(url)
            if url in state.seen_pages:
                continue
            state.seen_pages.add(url)
            if self._fetch_page(fetcher, state, url):
                return
            self._emit(state.result, len(state.page_queue), len(state.pending_resources) - len(state.seen_resources), url)

    def _fetch_page(self, fetcher: Fetcher, state: _RunState, url: str) -> bool:
        result = state.result
        remaining = self._remaining_bytes(result)
        fetch_limit = min(self.config.max_page_bytes, remaining)
        try:
            response = fetcher.get(url, fetch_limit)
        except requests.RequestException as exc:
            result.errors.append(f"{url}: {exc}")
            result.issues.append(self._issue("page.fetch_failed", "page", url, str(exc)))
            return False
        result.coverage.bytes_downloaded += len(response.body)
        html = ""
        signals = PageSignals()
        content = PageContent()
        if response.status < 400 and response.content_type in ("text/html", "application/xhtml+xml"):
            html = response.body.decode(_encoding(response.content_type), errors="replace")
            signals = extract_page_signals(response.final_url, html, response.headers.get("x-robots-tag", ""))
            content = extract_page_content(html)
        result.pages.append(Page(
            url=url, final_url=response.final_url, status=response.status, content_type=response.content_type,
            bytes=len(response.body), duration_ms=response.duration_ms, title=content.title,
            meta_description=content.meta_description, h1s=content.h1s, word_count=content.word_count,
            truncated=response.truncated, redirect_hops=response.redirect_hops, canonical_url=signals.canonical_url,
            robots_directives=signals.robots_directives, invalid_robots_directives=signals.invalid_robots_directives,
            jsonld_errors=signals.jsonld_errors, jsonld_blocks=signals.jsonld_blocks,
            hreflang=signals.hreflang, declared_bytes=response.declared_bytes,
        ))
        result.coverage.pages_fetched += 1
        self._add_page_response_issues(result, url, response, signals, state.edges)
        if html:
            self._queue_page_discoveries(state, response.final_url, html, signals)
        if response.truncated:
            result.issues.append(self._issue("page.response_truncated", "page", url, f"Page download stopped after {len(response.body)} bytes", state.edges, {"bytes_read": len(response.body), "limit": fetch_limit}))
            if fetch_limit == remaining and remaining <= self.config.max_page_bytes:
                self._set_limit(result, "max_total_bytes")
                return True
            self._mark_incomplete(result, "max_page_bytes")
        return False

    def _add_page_response_issues(self, result: CrawlResult, url: str, response: FetchResponse, signals: PageSignals, edges: set[Edge]) -> None:
        linked = any(edge.target_url == url and edge.context == "a.href" for edge in edges)
        if response.status >= 400 and linked:
            result.issues.append(self._issue("link.http_error", "page", url, f"Internal link target returns HTTP {response.status}", edges, {"status": response.status}))
        if response.redirect_hops and linked:
            result.issues.append(self._issue("link.redirect", "page", url, f"Internal link redirects to {response.final_url}", edges, {"hops": response.redirect_hops}))
        if len(response.redirect_hops) > 1:
            result.issues.append(self._issue("page.redirect_chain", "page", url, f"Page reaches {response.final_url} through {len(response.redirect_hops)} redirects", edges, {"hops": response.redirect_hops, "final_url": response.final_url}))
        observed_size = max(response.declared_bytes or 0, len(response.body))
        if observed_size > self.config.max_page_size:
            result.issues.append(self._issue("page.oversized", "page", url, f"HTML response is {observed_size} bytes", edges, {"bytes": observed_size, "threshold": self.config.max_page_size}))
        if response.duration_ms > self.config.max_page_duration_ms:
            result.issues.append(self._issue("page.slow_response", "page", url, f"Page response took {response.duration_ms} ms", edges, {"duration_ms": response.duration_ms, "threshold": self.config.max_page_duration_ms}))
        if signals.invalid_robots_directives:
            result.issues.append(self._issue("directive.invalid_robots", "page", url, "Unsupported robots directives were found", edges, {"directives": signals.invalid_robots_directives}))
        if signals.jsonld_errors:
            result.issues.append(self._issue("structured_data.invalid_jsonld", "page", url, "One or more JSON-LD blocks are invalid", edges, {"errors": signals.jsonld_errors}))
        if signals.duplicate_jsonld_blocks:
            result.issues.append(self._issue("structured_data.duplicate_jsonld", "page", url, "Identical JSON-LD script blocks appear more than once", edges, {"duplicates": signals.duplicate_jsonld_blocks}))

    def _queue_page_discoveries(self, state: _RunState, final_url: str, html: str, signals: PageSignals) -> None:
        links, resources, found_edges = discover_html(final_url, html)
        state.edges.update(found_edges)
        if signals.canonical_url:
            state.edges.add(Edge(final_url, signals.canonical_url, "link.canonical"))
            if same_origin(state.start_url, signals.canonical_url):
                self._enqueue_page(state, signals.canonical_url)
        for reference in signals.hreflang:
            state.edges.add(Edge(final_url, reference.url, f"link.hreflang:{reference.language}"))
            if same_origin(state.start_url, reference.url):
                self._enqueue_page(state, reference.url)
        for link in links:
            if same_origin(state.start_url, link):
                self._enqueue_page(state, link)
        for item in resources:
            if self.config.follow_external_resources or same_origin(state.start_url, item.url):
                state.pending_resources.setdefault(item.url, item.kind)

    @staticmethod
    def _enqueue_page(state: _RunState, url: str) -> None:
        if url in state.seen_pages or url in state.queued_pages:
            return
        state.page_queue.append(url)
        state.queued_pages.add(url)

    def _crawl_resources(self, fetcher: Fetcher, state: _RunState) -> None:
        resource_queue = deque(state.pending_resources)
        while resource_queue:
            url = resource_queue.popleft()
            kind = state.pending_resources[url]
            if url in state.seen_resources:
                continue
            if self._limit(state.result, state.deadline, len(state.seen_resources) >= self.config.max_resources, "max_resources"):
                return
            if self._limit(state.result, state.deadline, state.result.coverage.bytes_downloaded >= self.config.max_total_bytes, "max_total_bytes"):
                return
            remaining = self._remaining_bytes(state.result)
            discovered, total_bytes_truncated = self._fetch_resource(fetcher, state.result, state.start_url, url, kind, state.pending_resources, state.edges, remaining)
            resource_queue.extend(item for item in discovered if item not in state.seen_resources and item not in resource_queue)
            state.seen_resources.add(url)
            self._emit(state.result, len(state.page_queue), len(state.pending_resources) - len(state.seen_resources), url)
            if total_bytes_truncated:
                self._set_limit(state.result, "max_total_bytes")
                return

    def _check_external_links(self, fetcher: Fetcher, state: _RunState) -> None:
        targets = sorted({edge.target_url for edge in state.edges if edge.context == "a.href" and not same_origin(state.start_url, edge.target_url)})
        state.result.coverage.external_links_discovered = len(targets)
        last_request_by_host: dict[str, float] = {}
        for url in targets[:self.config.max_external_links]:
            if self._limit(state.result, state.deadline, state.result.coverage.bytes_downloaded >= self.config.max_total_bytes, "max_total_bytes"):
                return
            host = urlsplit(url).netloc.lower()
            elapsed = time.monotonic() - last_request_by_host.get(host, 0.0)
            if host in last_request_by_host and elapsed < self.config.external_delay_seconds:
                time.sleep(self.config.external_delay_seconds - elapsed)
            try:
                response = fetcher.head(url)
                if response.status in {403, 405, 501}:
                    response = fetcher.get(url, min(self.config.max_link_bytes, self._remaining_bytes(state.result)))
                    state.result.coverage.bytes_downloaded += len(response.body)
            except requests.RequestException as exc:
                state.result.external_links.append(ExternalLinkTarget(url=url, referring_urls=_root_referrers(url, state.edges), error=str(exc)))
                state.result.issues.append(self._issue("external_link.fetch_failed", "external_link", url, str(exc), state.edges))
                last_request_by_host[host] = time.monotonic()
                continue
            last_request_by_host[host] = time.monotonic()
            state.result.coverage.external_links_checked += 1
            state.result.external_links.append(ExternalLinkTarget(url, response.final_url, response.status, response.redirect_hops, _root_referrers(url, state.edges)))
            if response.status >= 400:
                state.result.issues.append(self._issue("external_link.http_error", "external_link", url, f"External target returns HTTP {response.status}", state.edges, {"status": response.status}))
            if response.redirect_hops:
                state.result.issues.append(self._issue("external_link.redirect", "external_link", url, f"External target redirects to {response.final_url}", state.edges, {"hops": response.redirect_hops}))
        if len(targets) > self.config.max_external_links:
            self._set_limit(state.result, "max_external_links")

    def _crawl_sitemaps(self, fetcher: Fetcher, result: CrawlResult, start_url: str, deadline: float) -> tuple[list[str], RobotsPolicy | None]:
        robots_url = normalize_url(start_url, "/robots.txt")
        default_sitemap = normalize_url(start_url, "/sitemap.xml")
        candidates: list[str] = [default_sitemap] if default_sitemap else []
        robots_policy = None
        if robots_url and time.monotonic() < deadline:
            try:
                robots_remaining = self._remaining_bytes(result)
                robots_limit = min(self.config.max_robots_bytes, robots_remaining)
                robots = fetcher.get(robots_url, robots_limit)
                result.coverage.bytes_downloaded += len(robots.body)
                result.robots = RobotsDocument(
                    url=robots.final_url, status=robots.status,
                    user_agent=self.config.robots_user_agent, bytes=len(robots.body),
                    truncated=robots.truncated,
                )
                if robots.status >= 500 or robots.status == 429:
                    result.issues.append(self._issue("robots.unavailable", "robots", robots_url, f"robots.txt returns HTTP {robots.status}", evidence={"status": robots.status}))
                if robots.truncated and robots_remaining <= self.config.max_robots_bytes:
                    self._set_limit(result, "max_total_bytes")
                    return [], None
                if robots.truncated and robots_limit == self.config.max_robots_bytes:
                    result.issues.append(self._issue("robots.byte_limit", "robots", robots_url, "robots.txt exceeded the configured byte limit", evidence={"limit": self.config.max_robots_bytes}))
                if robots.status < 400:
                    robots_policy = parse_robots(robots.final_url, robots.body, self.config.robots_user_agent)
                    robots_policy.document.status = robots.status
                    robots_policy.document.bytes = len(robots.body)
                    robots_policy.document.truncated = robots.truncated
                    result.robots = robots_policy.document
                    if robots_policy.document.errors:
                        result.issues.append(self._issue("robots.invalid_syntax", "robots", robots_url, "robots.txt contains malformed directives", evidence={"errors": robots_policy.document.errors}))
                    candidates = list(dict.fromkeys(sitemap_locations_from_robots(robots.final_url, robots.body) + candidates))
            except requests.RequestException as exc:
                result.robots = RobotsDocument(url=robots_url, user_agent=self.config.robots_user_agent, errors=[str(exc)])
                result.issues.append(self._issue("robots.unavailable", "robots", robots_url, str(exc)))
        queue = deque(candidates)
        visited: set[str] = set()
        discovered_pages: list[str] = []
        all_urls: set[str] = set()
        while queue:
            if result.coverage.bytes_downloaded >= self.config.max_total_bytes:
                self._set_limit(result, "max_total_bytes")
                break
            if time.monotonic() >= deadline:
                self._set_limit(result, "max_duration_seconds")
                break
            if len(visited) >= self.config.max_sitemaps:
                self._set_limit(result, "max_sitemaps")
                result.issues.append(self._issue("sitemap.recursion_limit", "sitemap", queue[0], "Sitemap discovery reached max_sitemaps", evidence={"limit": self.config.max_sitemaps}))
                break
            url = queue.popleft()
            if url in visited or not same_origin(start_url, url):
                continue
            visited.add(url)
            remaining = self._remaining_bytes(result)
            try:
                sitemap_fetch_limit = min(self.config.max_sitemap_bytes, remaining)
                response = fetcher.get(url, sitemap_fetch_limit)
            except requests.RequestException as exc:
                result.sitemaps.append(SitemapDocument(url=url, errors=[str(exc)]))
                result.issues.append(self._issue("sitemap.fetch_failed", "sitemap", url, str(exc)))
                continue
            result.coverage.bytes_downloaded += len(response.body)
            result.coverage.sitemaps_fetched += 1
            document = SitemapDocument(url=url, status=response.status)
            result.sitemaps.append(document)
            if response.status >= 400:
                result.issues.append(self._issue("sitemap.http_error", "sitemap", url, f"Sitemap returns HTTP {response.status}", evidence={"status": response.status}))
                continue
            if response.truncated and sitemap_fetch_limit == remaining and remaining <= self.config.max_sitemap_bytes:
                document.errors.append("Sitemap download exhausted the total byte budget")
                self._set_limit(result, "max_total_bytes")
                break
            if response.truncated and sitemap_fetch_limit == self.config.max_sitemap_bytes:
                document.errors.append(f"Sitemap download exceeded {self.config.max_sitemap_bytes} bytes")
                result.issues.append(self._issue("sitemap.byte_limit", "sitemap", url, "Sitemap download exceeded the configured byte limit", evidence={"limit": self.config.max_sitemap_bytes}))
                continue
            parsed = parse_sitemap(response.final_url, response.body, self.config.max_sitemap_bytes)
            document.kind = parsed.kind
            document.urls = parsed.urls
            document.child_sitemaps = parsed.child_sitemaps
            document.errors = parsed.errors
            if parsed.errors:
                result.issues.append(self._issue("sitemap.invalid_xml", "sitemap", url, "Sitemap XML is malformed or unsupported", evidence={"errors": parsed.errors}))
            if len(parsed.urls) > self.config.max_urls_per_sitemap:
                result.issues.append(self._issue("sitemap.url_limit", "sitemap", url, f"Sitemap contains {len(parsed.urls)} URLs", evidence={"count": len(parsed.urls), "limit": self.config.max_urls_per_sitemap}))
            for item in parsed.invalid_lastmod:
                result.issues.append(self._issue("sitemap.invalid_lastmod", "sitemap", item["url"], f"Invalid lastmod: {item['lastmod']}", evidence=item))
            for page_url in parsed.urls:
                if page_url in all_urls:
                    result.issues.append(self._issue("sitemap.duplicate_url", "page", page_url, "URL appears more than once in the sitemap set"))
                else:
                    all_urls.add(page_url)
                    discovered_pages.append(page_url)
            queue.extend(item for item in parsed.child_sitemaps if item not in visited)
        result.coverage.sitemap_urls_discovered = len(all_urls)
        return discovered_pages, robots_policy

    def _add_robots_issues(self, result: CrawlResult, edges: set[Edge], policy: RobotsPolicy | None) -> None:
        if not policy or policy.document.truncated:
            return
        for page in result.pages:
            target = page.final_url or page.url
            if page.status >= 400 or policy.allows(target):
                continue
            policy.document.blocked_pages.append(page.url)
            result.issues.append(self._issue("robots.blocked_page", "page", page.url, f"{policy.document.user_agent} is disallowed by robots.txt", edges, {"user_agent": policy.document.user_agent}))
        browser_kinds = {"image", "stylesheet", "script", "font", "manifest", "video", "audio"}
        for resource in result.resources:
            target = resource.final_url or resource.url
            if resource.kind not in browser_kinds or resource.status is None or resource.status >= 400 or policy.allows(target):
                continue
            policy.document.blocked_resources.append(resource.url)
            result.issues.append(self._issue("robots.blocked_resource", "resource", resource.url, f"{policy.document.user_agent} is disallowed from fetching this page resource", edges, {"user_agent": policy.document.user_agent, "kind": resource.kind}))

    def _fetch_resource(self, fetcher: Fetcher, result: CrawlResult, start_url: str, url: str, kind: str, pending: dict[str, str], edges: set[Edge], remaining_total_bytes: int) -> tuple[list[str], bool]:
        discovered_urls: list[str] = []
        fetch_limit = min(self.config.max_resource_bytes, remaining_total_bytes)
        try:
            response = fetcher.get(url, fetch_limit)
        except requests.RequestException as exc:
            result.resources.append(Resource(url=url, kind=kind))
            result.errors.append(f"{url}: {exc}")
            result.issues.append(self._issue("resource.fetch_failed", "resource", url, str(exc), edges))
            return discovered_urls, False
        size = len(response.body)
        result.coverage.bytes_downloaded += size
        result.coverage.resources_fetched += 1
        headers = response.headers
        metadata = inspect_image(response.body) if kind == "image" and response.status < 400 else None
        resource = Resource(
            url=url, final_url=response.final_url, kind=kind, status=response.status,
            content_type=response.content_type, bytes=size, duration_ms=response.duration_ms,
            redirect_hops=response.redirect_hops, truncated=response.truncated,
            cache_control=headers.get("cache-control", ""), content_encoding=headers.get("content-encoding", ""),
            etag=headers.get("etag", ""), last_modified=headers.get("last-modified", ""),
            image_width=metadata.width if metadata else None, image_height=metadata.height if metadata else None,
            image_format=metadata.format if metadata else "", content_hash=hashlib.sha256(response.body).hexdigest() if response.body else "",
        )
        result.resources.append(resource)
        if response.status >= 400:
            result.issues.append(self._issue("resource.http_error", "resource", url, f"HTTP {response.status}", edges, {"status": response.status}))
        if response.redirect_hops:
            result.issues.append(self._issue("resource.redirect", "resource", url, f"Redirects to {response.final_url}", edges, {"hops": response.redirect_hops}))
        if size == 0 and response.status < 400:
            result.issues.append(self._issue("resource.empty", "resource", url, "Successful response has no body", edges))
        observed_size = response.declared_bytes or size
        if observed_size > self.config.max_resource_size:
            result.issues.append(self._issue("resource.oversized", "resource", url, f"Resource is {observed_size} bytes", edges, {"bytes": observed_size, "threshold": self.config.max_resource_size}))
        if response.truncated:
            result.issues.append(self._issue("resource.response_truncated", "resource", url, f"Download stopped after {size} bytes", edges, {"bytes_read": size}))
        inspected_image_types = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/heif", "image/heic", "image/svg+xml", "image/x-icon", "image/vnd.microsoft.icon", "image/bmp", "image/tiff", "image/jxl"}
        if kind == "image" and response.status < 400 and response.content_type in inspected_image_types and metadata is None and not response.truncated:
            result.issues.append(self._issue("image.invalid", "resource", url, "Image dimensions could not be read from the response body", edges))
        if metadata and ((metadata.width is not None and metadata.width > self.config.max_image_width) or (metadata.height is not None and metadata.height > self.config.max_image_height)):
            result.issues.append(self._issue("image.oversized_dimensions", "resource", url, f"Image is {metadata.width}x{metadata.height} pixels", edges, {"width": metadata.width, "height": metadata.height, "max_width": self.config.max_image_width, "max_height": self.config.max_image_height}))
        if size >= self.config.min_compression_bytes and _is_compressible(response.content_type) and not headers.get("content-encoding"):
            result.issues.append(self._issue("resource.missing_compression", "resource", url, f"{size}-byte {response.content_type} response has no Content-Encoding", edges, {"bytes": size}))
        cache_seconds = _cache_max_age(headers.get("cache-control", ""))
        if kind in {"image", "stylesheet", "script", "font"} and response.status < 400 and (cache_seconds is None or cache_seconds < self.config.min_cache_seconds):
            result.issues.append(self._issue("resource.weak_cache", "resource", url, "Static resource has no long-lived browser cache policy", edges, {"cache_control": headers.get("cache-control", ""), "minimum_seconds": self.config.min_cache_seconds}))
        expected = _expected_mime(kind)
        if expected and response.content_type and not any(response.content_type.startswith(item) for item in expected):
            result.issues.append(self._issue("resource.mime_mismatch", "resource", url, f"Expected {' or '.join(expected)} but received {response.content_type}", edges, {"expected": list(expected), "actual": response.content_type}))
        if kind == "stylesheet" and response.status < 400 and response.content_type == "text/css":
            for item in discover_css(response.final_url, response.body.decode("utf-8", errors="replace")):
                if not self.config.follow_external_resources and not same_origin(start_url, item.url):
                    continue
                pending.setdefault(item.url, item.kind)
                edges.add(Edge(url, item.url, "css.url"))
                discovered_urls.append(item.url)
        total_bytes_truncated = response.truncated and fetch_limit == remaining_total_bytes and remaining_total_bytes <= self.config.max_resource_bytes
        return discovered_urls, total_bytes_truncated

    def _add_graph_issues(self, result: CrawlResult, edges: set[Edge]) -> None:
        for target in sorted({
            edge.target_url for edge in edges
            if _is_browser_subresource(edge)
            and edge.source_url.startswith("https://")
            and edge.target_url.startswith("http://")
        }):
            result.issues.append(self._issue("resource.mixed_content", "resource", target, "HTTPS page or stylesheet references an HTTP resource", edges))

    def _add_duplicate_issues(self, result: CrawlResult, edges: set[Edge]) -> None:
        by_hash: dict[str, list[Resource]] = {}
        for resource in result.resources:
            if resource.content_hash and resource.status is not None and resource.status < 400:
                by_hash.setdefault(resource.content_hash, []).append(resource)
        for digest, resources in sorted(by_hash.items()):
            urls = sorted({item.url for item in resources})
            if len(urls) < 2:
                continue
            result.issues.append(self._issue("resource.duplicate_payload", "resource", urls[0], f"Identical content is served from {len(urls)} resource URLs", edges, {"sha256": digest, "urls": urls}))

    def _add_canonical_issues(self, result: CrawlResult, edges: set[Edge]) -> None:
        pages = {page.url: page for page in result.pages}
        canonicals = {page.url: page.canonical_url for page in result.pages if page.canonical_url}
        for page in result.pages:
            target_url = page.canonical_url
            if not target_url:
                continue
            target = pages.get(target_url)
            if target and target.status >= 400:
                result.issues.append(self._issue("canonical.http_error", "page", page.url, f"Canonical target returns HTTP {target.status}", edges, {"canonical_url": target_url, "status": target.status}))
            if target and target.redirect_hops:
                result.issues.append(self._issue("canonical.redirect", "page", page.url, f"Canonical target redirects to {target.final_url}", edges, {"canonical_url": target_url, "final_url": target.final_url, "hops": target.redirect_hops}))
            if target and target.canonical_url and target.canonical_url != target_url:
                result.issues.append(self._issue("canonical.chain", "page", page.url, f"Canonical target declares {target.canonical_url}", edges, {"canonical_url": target_url, "next_canonical_url": target.canonical_url}))
            if {"noindex", "none"} & set(page.robots_directives) and target_url != page.url:
                result.issues.append(self._issue("directive.noindex_canonical_conflict", "page", page.url, "Page is noindex and canonicalizes to another URL", edges, {"canonical_url": target_url}))
        for loop in _canonical_loops(canonicals):
            result.issues.append(self._issue("canonical.loop", "page", loop[0], "Canonical declarations form a loop", edges, {"urls": loop}))

    def _add_sitemap_issues(self, result: CrawlResult, edges: set[Edge]) -> None:
        sitemap_urls = {url for document in result.sitemaps for url in document.urls}
        pages = {page.url: page for page in result.pages}
        for url in sorted(sitemap_urls):
            page = pages.get(url)
            if not page:
                continue
            if page.status >= 400:
                result.issues.append(self._issue("sitemap.url_http_error", "page", url, f"Sitemap URL returns HTTP {page.status}", edges, {"status": page.status}))
            if page.redirect_hops:
                result.issues.append(self._issue("sitemap.url_redirect", "page", url, f"Sitemap URL redirects to {page.final_url}", edges, {"hops": page.redirect_hops, "final_url": page.final_url}))
            if {"noindex", "none"} & set(page.robots_directives):
                result.issues.append(self._issue("sitemap.url_noindex", "page", url, "Sitemap URL is noindex", edges))
            if page.canonical_url and page.canonical_url != url:
                result.issues.append(self._issue("sitemap.url_noncanonical", "page", url, f"Sitemap URL canonicalizes to {page.canonical_url}", edges, {"canonical_url": page.canonical_url}))

    def _add_architecture_issues(self, result: CrawlResult, edges: set[Edge]) -> None:
        navigation = [edge for edge in edges if edge.context == "a.href" and same_origin(result.start_url, edge.target_url)]
        adjacency: dict[str, set[str]] = {}
        incoming: set[str] = set()
        for edge in navigation:
            adjacency.setdefault(edge.source_url, set()).add(edge.target_url)
            incoming.add(edge.target_url)

        aliases = {page.url: page.final_url for page in result.pages if page.final_url}
        start_alias = aliases.get(result.start_url, result.start_url)
        depths = {result.start_url: 0, start_alias: 0}
        queue = deque(dict.fromkeys([result.start_url, start_alias]))
        while queue:
            source = queue.popleft()
            for target in sorted(adjacency.get(source, set())):
                for candidate in dict.fromkeys([target, aliases.get(target, target)]):
                    if candidate in depths:
                        continue
                    depths[candidate] = depths[source] + 1
                    queue.append(candidate)

        for page in result.pages:
            page.crawl_depth = min(
                (depths[url] for url in {page.url, page.final_url} if url in depths),
                default=None,
            )
            if page.crawl_depth is not None and page.crawl_depth > self.config.max_click_depth:
                result.issues.append(self._issue(
                    "architecture.deep_page", "page", page.url,
                    f"Page is {page.crawl_depth} clicks from the start URL",
                    edges, {"depth": page.crawl_depth, "threshold": self.config.max_click_depth},
                ))

        pages = {page.url: page for page in result.pages}
        sitemap_urls = {url for document in result.sitemaps for url in document.urls}
        for url in sorted(sitemap_urls - {result.start_url}):
            page = pages.get(url)
            if not page or page.status >= 400 or {"noindex", "none"} & set(page.robots_directives):
                continue
            if url not in incoming:
                result.issues.append(self._issue(
                    "architecture.sitemap_orphan", "page", url,
                    "Successful sitemap page has no incoming internal HTML links", edges,
                    {"sitemap_url": url},
                ))

    def _add_content_issues(self, result: CrawlResult, edges: set[Edge]) -> None:
        pages = [page for page in result.pages if _is_indexable_html(page)]
        for page in pages:
            if not page.title:
                result.issues.append(self._issue("content.title_missing", "page", page.url, "Indexable page has no title element", edges))
            elif len(page.title) > self.config.max_title_chars:
                result.issues.append(self._issue("content.title_too_long", "page", page.url, f"Title contains {len(page.title)} characters", edges, {"characters": len(page.title), "threshold": self.config.max_title_chars}))
            if not page.meta_description:
                result.issues.append(self._issue("content.meta_description_missing", "page", page.url, "Indexable page has no meta description", edges))
            elif len(page.meta_description) > self.config.max_meta_description_chars:
                result.issues.append(self._issue("content.meta_description_too_long", "page", page.url, f"Meta description contains {len(page.meta_description)} characters", edges, {"characters": len(page.meta_description), "threshold": self.config.max_meta_description_chars}))
            if not page.h1s:
                result.issues.append(self._issue("content.h1_missing", "page", page.url, "Indexable page has no H1 heading", edges))
            elif len(page.h1s) > 1:
                result.issues.append(self._issue("content.multiple_h1", "page", page.url, f"Page contains {len(page.h1s)} H1 headings", edges, {"count": len(page.h1s), "headings": page.h1s}))
            if page.word_count < self.config.min_content_words:
                result.issues.append(self._issue("content.thin", "page", page.url, f"Page contains approximately {page.word_count} visible words", edges, {"words": page.word_count, "threshold": self.config.min_content_words}))
        self._add_duplicate_content_issues(result, pages, edges, "title", "content.duplicate_title")
        self._add_duplicate_content_issues(result, pages, edges, "meta_description", "content.duplicate_meta_description")

    def _add_duplicate_content_issues(self, result: CrawlResult, pages: list[Page], edges: set[Edge], attribute: str, rule_id: str) -> None:
        groups: dict[str, list[Page]] = {}
        for page in pages:
            value = " ".join(str(getattr(page, attribute)).split())
            if value:
                groups.setdefault(value.casefold(), []).append(page)
        for matches in groups.values():
            if len(matches) < 2:
                continue
            urls = sorted(page.url for page in matches)
            for page in matches:
                result.issues.append(self._issue(rule_id, "page", page.url, f"Content is shared by {len(matches)} indexable pages", edges, {"value": getattr(page, attribute), "urls": urls}))

    def _limit(self, result: CrawlResult, deadline: float, condition: bool, reason: str) -> bool:
        actual = "max_duration_seconds" if time.monotonic() >= deadline else reason if condition else None
        if not actual:
            return False
        self._set_limit(result, actual)
        return True

    def _set_limit(self, result: CrawlResult, reason: str) -> None:
        if result.coverage.complete:
            result.coverage.complete = False
            result.coverage.limit_reason = reason
            result.issues.append(self._issue("crawl.limit_reached", "crawl", result.start_url, f"Crawl stopped at {reason}", evidence={"limit": reason}))

    @staticmethod
    def _mark_incomplete(result: CrawlResult, reason: str) -> None:
        if result.coverage.complete:
            result.coverage.complete = False
            result.coverage.limit_reason = reason

    def _remaining_bytes(self, result: CrawlResult) -> int:
        return max(1, self.config.max_total_bytes - result.coverage.bytes_downloaded)

    def _issue(self, rule_id: str, entity: str, url: str, message: str, edges: set[Edge] | None = None, evidence: dict[str, object] | None = None) -> Issue:
        rule = get_rule(rule_id)
        refs = _root_referrers(url, edges or set())
        return Issue(rule.id, rule.title, rule.severity, entity, url, message, evidence or {}, refs, rule.remediation)

    def _emit(self, result: CrawlResult, pages_queued: int, resources_queued: int, latest_url: str) -> None:
        if self.progress:
            self.progress({"pages_fetched": result.coverage.pages_fetched, "pages_queued": pages_queued, "resources_fetched": result.coverage.resources_fetched, "resources_queued": max(0, resources_queued), "errors": len(result.errors), "latest_url": latest_url, "updated_at": _now()})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encoding(_: str) -> str:
    return "utf-8"


def _expected_mime(kind: str) -> tuple[str, ...]:
    return {"image": ("image/",), "stylesheet": ("text/css",), "script": ("application/javascript", "text/javascript"), "font": ("font/", "application/font"), "manifest": ("application/manifest",)}.get(kind, ())


def _root_referrers(url: str, edges: set[Edge]) -> list[str]:
    incoming: dict[str, list[Edge]] = {}
    for edge in edges:
        incoming.setdefault(edge.target_url, []).append(edge)
    roots: set[str] = set()
    queue = deque(incoming.get(url, []))
    visited: set[tuple[str, str, str]] = set()
    while queue:
        edge = queue.popleft()
        key = (edge.source_url, edge.target_url, edge.context)
        if key in visited:
            continue
        visited.add(key)
        if edge.context == "css.url" and incoming.get(edge.source_url):
            queue.extend(incoming[edge.source_url])
        else:
            roots.add(edge.source_url)
    return sorted(roots)


def _is_compressible(content_type: str) -> bool:
    return content_type.startswith("text/") or content_type in {"application/javascript", "application/json", "application/manifest+json", "application/xml", "image/svg+xml"}


def _is_browser_subresource(edge: Edge) -> bool:
    """True when the relationship causes a browser resource request."""
    direct = {"img.src", "srcset", "script.src", "css.url", "style.url", "object.data"}
    link_resources = {
        "link.stylesheet", "link.icon", "link.apple-touch-icon", "link.manifest",
        "link.preload", "link.prefetch", "link.modulepreload",
    }
    return edge.context in direct or edge.context in link_resources or edge.context in {
        "video.src", "audio.src", "source.src", "track.src", "iframe.src", "embed.src",
    }


def _cache_max_age(value: str) -> int | None:
    match = re.search(r"(?:s-maxage|max-age)\s*=\s*\"?(\d+)", value, re.I)
    return int(match.group(1)) if match else None


def _canonical_loops(canonicals: dict[str, str]) -> list[list[str]]:
    loops: set[tuple[str, ...]] = set()
    for start in canonicals:
        path: list[str] = []
        positions: dict[str, int] = {}
        current = start
        while current in canonicals and current not in positions:
            positions[current] = len(path)
            path.append(current)
            current = canonicals[current]
        if current not in positions:
            continue
        loop = path[positions[current]:]
        if len(loop) == 1 and canonicals.get(loop[0]) == loop[0]:
            continue
        rotations = [tuple(loop[index:] + loop[:index]) for index in range(len(loop))]
        loops.add(min(rotations))
    return [list(loop) for loop in sorted(loops)]


def _is_indexable_html(page: Page) -> bool:
    if page.status >= 400 or page.redirect_hops or page.truncated or page.content_type not in ("text/html", "application/xhtml+xml"):
        return False
    if {"noindex", "none"} & set(page.robots_directives):
        return False
    return not page.canonical_url or page.canonical_url in {page.url, page.final_url}
