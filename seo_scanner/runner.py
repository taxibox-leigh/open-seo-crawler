from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from .config import ScannerConfig
from .discovery import DiscoveredResource, discover_css, discover_html
from .fetch import Fetcher
from .models import Coverage, CrawlResult, Edge, Issue, Page, Resource
from .rules import get_rule
from .scope import normalize_url, same_origin

ProgressCallback = Callable[[dict[str, object]], None]


class Scanner:
    def __init__(self, config: ScannerConfig | None = None, progress: ProgressCallback | None = None) -> None:
        self.config = config or ScannerConfig()
        self.progress = progress

    def scan(self, start_url: str) -> CrawlResult:
        start = normalize_url(start_url, start_url)
        if not start:
            raise ValueError("start_url must be an absolute HTTP(S) URL")
        result = CrawlResult(start_url=start, started_at=_now())
        deadline = time.monotonic() + self.config.max_duration_seconds
        page_queue = deque([start])
        seen_pages: set[str] = set()
        pending_resources: dict[str, str] = {}
        seen_resources: set[str] = set()
        edges: set[Edge] = set()

        with Fetcher(self.config.user_agent, self.config.timeout_seconds) as fetcher:
            while page_queue:
                if self._limit(result, deadline, len(seen_pages) >= self.config.max_pages, "max_pages"):
                    break
                if self._limit(result, deadline, result.coverage.bytes_downloaded >= self.config.max_total_bytes, "max_total_bytes"):
                    break
                url = page_queue.popleft()
                if url in seen_pages:
                    continue
                seen_pages.add(url)
                try:
                    response = fetcher.get(url, self._remaining_bytes(result))
                except requests.RequestException as exc:
                    result.errors.append(f"{url}: {exc}")
                    result.issues.append(self._issue("page.fetch_failed", "page", url, str(exc)))
                    continue
                result.coverage.bytes_downloaded += len(response.body)
                result.pages.append(Page(url, response.final_url, response.status, response.content_type, len(response.body), response.duration_ms, _title(response.body, response.content_type)))
                result.coverage.pages_fetched += 1
                if response.status < 400 and response.content_type in ("text/html", "application/xhtml+xml"):
                    html = response.body.decode(_encoding(response.content_type), errors="replace")
                    links, resources, found_edges = discover_html(response.final_url, html)
                    edges.update(found_edges)
                    for link in links:
                        if same_origin(start, link) and link not in seen_pages:
                            page_queue.append(link)
                    for item in resources:
                        if self.config.follow_external_resources or same_origin(start, item.url):
                            pending_resources.setdefault(item.url, item.kind)
                self._emit(result, len(page_queue), len(pending_resources) - len(seen_resources), url)

            resource_queue = deque(pending_resources)
            while resource_queue:
                url = resource_queue.popleft()
                kind = pending_resources[url]
                if url in seen_resources:
                    continue
                if self._limit(result, deadline, len(seen_resources) >= self.config.max_resources, "max_resources"):
                    break
                if self._limit(result, deadline, result.coverage.bytes_downloaded >= self.config.max_total_bytes, "max_total_bytes"):
                    break
                discovered = self._fetch_resource(fetcher, result, start, url, kind, pending_resources, edges)
                resource_queue.extend(item for item in discovered if item not in seen_resources and item not in resource_queue)
                seen_resources.add(url)
                self._emit(result, len(page_queue), len(pending_resources) - len(seen_resources), url)

        result.edges = sorted(edges, key=lambda edge: (edge.source_url, edge.target_url, edge.context))
        result.coverage.pages_queued = len(page_queue)
        result.coverage.resources_discovered = len(pending_resources)
        result.finished_at = _now()
        result.status = "partial" if not result.coverage.complete else "complete"
        return result

    def _fetch_resource(self, fetcher: Fetcher, result: CrawlResult, start_url: str, url: str, kind: str, pending: dict[str, str], edges: set[Edge]) -> list[str]:
        discovered_urls: list[str] = []
        try:
            response = fetcher.get(url, min(self.config.max_resource_bytes, self._remaining_bytes(result)))
        except requests.RequestException as exc:
            result.resources.append(Resource(url=url, kind=kind))
            result.errors.append(f"{url}: {exc}")
            result.issues.append(self._issue("resource.fetch_failed", "resource", url, str(exc), edges))
            return discovered_urls
        size = len(response.body)
        result.coverage.bytes_downloaded += size
        result.coverage.resources_fetched += 1
        resource = Resource(url, response.final_url, kind, response.status, response.content_type, size, response.duration_ms, response.redirect_hops, response.truncated)
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
        return discovered_urls

    def _limit(self, result: CrawlResult, deadline: float, condition: bool, reason: str) -> bool:
        actual = "max_duration_seconds" if time.monotonic() >= deadline else reason if condition else None
        if not actual:
            return False
        if result.coverage.complete:
            result.coverage.complete = False
            result.coverage.limit_reason = actual
            result.issues.append(self._issue("crawl.limit_reached", "crawl", result.start_url, f"Crawl stopped at {actual}", evidence={"limit": actual}))
        return True

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
