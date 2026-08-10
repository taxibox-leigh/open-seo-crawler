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
from bs4 import BeautifulSoup

from .config import ScannerConfig
from .analyzers.image import inspect_image
from .analyzers.directives import PageSignals, extract_page_signals
from .analyzers.sitemap import parse_sitemap, sitemap_locations_from_robots
from .discovery import DiscoveredResource, discover_css, discover_html
from .fetch import Fetcher, FetchResponse
from .models import Coverage, CrawlResult, Edge, Issue, Page, Resource, SitemapDocument
from .rules import get_rule
from .scope import normalize_url, same_origin

ProgressCallback = Callable[[dict[str, object]], None]


@dataclass
class _RunState:
    start_url: str
    result: CrawlResult
    deadline: float
    page_queue: deque[str] = field(default_factory=deque)
    seen_pages: set[str] = field(default_factory=set)
    pending_resources: dict[str, str] = field(default_factory=dict)
    seen_resources: set[str] = field(default_factory=set)
    edges: set[Edge] = field(default_factory=set)


class Scanner:
    def __init__(self, config: ScannerConfig | None = None, progress: ProgressCallback | None = None) -> None:
        self.config = config or ScannerConfig()
        self.progress = progress

    def scan(self, start_url: str) -> CrawlResult:
        start = normalize_url(start_url, start_url)
        if not start:
            raise ValueError("start_url must be an absolute HTTP(S) URL")
        result = CrawlResult(start_url=start, started_at=_now())
        state = _RunState(start, result, time.monotonic() + self.config.max_duration_seconds, deque([start]))

        with Fetcher(self.config.user_agent, self.config.timeout_seconds) as fetcher:
            if self.config.discover_sitemaps:
                for sitemap_page in self._crawl_sitemaps(fetcher, result, start, state.deadline):
                    if same_origin(start, sitemap_page) and sitemap_page not in state.page_queue:
                        state.page_queue.append(sitemap_page)
            self._crawl_pages(fetcher, state)
            self._crawl_resources(fetcher, state)

        result.edges = sorted(state.edges, key=lambda edge: (edge.source_url, edge.target_url, edge.context))
        self._add_graph_issues(result, state.edges)
        self._add_duplicate_issues(result, state.edges)
        self._add_canonical_issues(result, state.edges)
        self._add_sitemap_issues(result, state.edges)
        result.coverage.pages_queued = len(state.page_queue)
        result.coverage.resources_discovered = len(state.pending_resources)
        result.finished_at = _now()
        result.status = "partial" if not result.coverage.complete else "complete"
        return result

    def _crawl_pages(self, fetcher: Fetcher, state: _RunState) -> None:
        while state.page_queue:
            if self._limit(state.result, state.deadline, len(state.seen_pages) >= self.config.max_pages, "max_pages"):
                return
            if self._limit(state.result, state.deadline, state.result.coverage.bytes_downloaded >= self.config.max_total_bytes, "max_total_bytes"):
                return
            url = state.page_queue.popleft()
            if url in state.seen_pages:
                continue
            state.seen_pages.add(url)
            if self._fetch_page(fetcher, state, url):
                return
            self._emit(state.result, len(state.page_queue), len(state.pending_resources) - len(state.seen_resources), url)

    def _fetch_page(self, fetcher: Fetcher, state: _RunState, url: str) -> bool:
        result = state.result
        try:
            response = fetcher.get(url, self._remaining_bytes(result))
        except requests.RequestException as exc:
            result.errors.append(f"{url}: {exc}")
            result.issues.append(self._issue("page.fetch_failed", "page", url, str(exc)))
            return False
        result.coverage.bytes_downloaded += len(response.body)
        html = ""
        signals = PageSignals()
        if response.status < 400 and response.content_type in ("text/html", "application/xhtml+xml"):
            html = response.body.decode(_encoding(response.content_type), errors="replace")
            signals = extract_page_signals(response.final_url, html, response.headers.get("x-robots-tag", ""))
        result.pages.append(Page(
            url=url, final_url=response.final_url, status=response.status, content_type=response.content_type,
            bytes=len(response.body), duration_ms=response.duration_ms, title=_title(response.body, response.content_type),
            truncated=response.truncated, redirect_hops=response.redirect_hops, canonical_url=signals.canonical_url,
            robots_directives=signals.robots_directives, invalid_robots_directives=signals.invalid_robots_directives,
            jsonld_errors=signals.jsonld_errors,
        ))
        result.coverage.pages_fetched += 1
        if response.truncated:
            self._set_limit(result, "max_total_bytes")
            return True
        self._add_page_response_issues(result, url, response, signals, state.edges)
        if html:
            self._queue_page_discoveries(state, response.final_url, html, signals)
        return False

    def _add_page_response_issues(self, result: CrawlResult, url: str, response: FetchResponse, signals: PageSignals, edges: set[Edge]) -> None:
        linked = any(edge.target_url == url and edge.context == "a.href" for edge in edges)
        if response.status >= 400 and linked:
            result.issues.append(self._issue("link.http_error", "page", url, f"Internal link target returns HTTP {response.status}", edges, {"status": response.status}))
        if response.redirect_hops and linked:
            result.issues.append(self._issue("link.redirect", "page", url, f"Internal link redirects to {response.final_url}", edges, {"hops": response.redirect_hops}))
        if signals.invalid_robots_directives:
            result.issues.append(self._issue("directive.invalid_robots", "page", url, "Unsupported robots directives were found", edges, {"directives": signals.invalid_robots_directives}))
        if signals.jsonld_errors:
            result.issues.append(self._issue("structured_data.invalid_jsonld", "page", url, "One or more JSON-LD blocks are invalid", edges, {"errors": signals.jsonld_errors}))

    def _queue_page_discoveries(self, state: _RunState, final_url: str, html: str, signals: PageSignals) -> None:
        links, resources, found_edges = discover_html(final_url, html)
        state.edges.update(found_edges)
        if signals.canonical_url:
            state.edges.add(Edge(final_url, signals.canonical_url, "link.canonical"))
            if same_origin(state.start_url, signals.canonical_url) and signals.canonical_url not in state.seen_pages:
                state.page_queue.append(signals.canonical_url)
        for link in links:
            if same_origin(state.start_url, link) and link not in state.seen_pages:
                state.page_queue.append(link)
        for item in resources:
            if self.config.follow_external_resources or same_origin(state.start_url, item.url):
                state.pending_resources.setdefault(item.url, item.kind)

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

    def _crawl_sitemaps(self, fetcher: Fetcher, result: CrawlResult, start_url: str, deadline: float) -> list[str]:
        robots_url = normalize_url(start_url, "/robots.txt")
        default_sitemap = normalize_url(start_url, "/sitemap.xml")
        candidates: list[str] = [default_sitemap] if default_sitemap else []
        if robots_url and time.monotonic() < deadline:
            try:
                robots_remaining = self._remaining_bytes(result)
                robots = fetcher.get(robots_url, min(self.config.max_resource_bytes, robots_remaining))
                result.coverage.bytes_downloaded += len(robots.body)
                if robots.truncated and robots_remaining <= self.config.max_resource_bytes:
                    self._set_limit(result, "max_total_bytes")
                    return []
                if robots.status < 400:
                    candidates = list(dict.fromkeys(sitemap_locations_from_robots(robots.final_url, robots.body) + candidates))
            except requests.RequestException:
                pass
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
        return discovered_pages

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
        for target in sorted({edge.target_url for edge in edges if edge.source_url.startswith("https://") and edge.target_url.startswith("http://")}):
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


def _title(body: bytes, content_type: str) -> str:
    if content_type not in ("text/html", "application/xhtml+xml"):
        return ""
    title = BeautifulSoup(body, "lxml").title
    return title.get_text(" ", strip=True) if title else ""


def _expected_mime(kind: str) -> tuple[str, ...]:
    return {"image": ("image/",), "stylesheet": ("text/css",), "script": ("application/javascript", "text/javascript"), "font": ("font/", "application/font"), "manifest": ("application/manifest",)}.get(kind, ())


def _root_referrers(url: str, edges: set[Edge]) -> list[str]:
    incoming: dict[str, set[str]] = {}
    targets = {edge.target_url for edge in edges}
    for edge in edges:
        incoming.setdefault(edge.target_url, set()).add(edge.source_url)
    roots: set[str] = set()
    queue = deque(incoming.get(url, set()))
    visited: set[str] = set()
    while queue:
        source = queue.popleft()
        if source in visited:
            continue
        visited.add(source)
        parents = incoming.get(source, set())
        if parents:
            queue.extend(parents)
        elif source not in targets or source != url:
            roots.add(source)
    return sorted(roots or incoming.get(url, set()))


def _is_compressible(content_type: str) -> bool:
    return content_type.startswith("text/") or content_type in {"application/javascript", "application/json", "application/manifest+json", "application/xml", "image/svg+xml"}


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
