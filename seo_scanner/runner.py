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
from .analyzers.alt_text import ALT_EMPTY_CONTENT
from .analyzers.content import PageContent, extract_page_content
from .analyzers.directives import PageSignals, extract_page_signals
from .analyzers.sitemap import parse_sitemap, sitemap_locations_from_robots
from .analyzers.robots import RobotsPolicy, parse_robots
from .analyzers.url_quality import UrlQuality, analyze_url
from .analyzers.hreflang import analyze_hreflang, valid_language_tag
from .analyzers.encoding import EncodingSignals, analyze_encoding
from .analyzers.document import DocumentSignals, analyze_document
from .analyzers.link_header import parse_link_header
from .discovery import DiscoveredResource, discover_css, discover_html
from .fetch import Fetcher, FetchResponse
from .models import Coverage, CrawlResult, Edge, ExternalLinkTarget, Issue, Page, RenderedPage, Resource, RobotsDocument, SitemapDocument
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
        raw_pages = {alias: page for page in result.pages for alias in {page.url, page.final_url} if alias}
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
            raw = raw_pages.get(page.url) or raw_pages.get(page.final_url)
            if page.seo_signals_error:
                result.issues.append(self._issue("render.seo_signals_unavailable", "page", page.url, page.seo_signals_error))
            elif raw and not page.error:
                self._add_rendered_signal_issues(result, raw, page)
            if page.failed_requests:
                result.issues.append(self._issue("render.failed_requests", "page", page.url, f"Browser rendering encountered {len(page.failed_requests)} failed requests", evidence={"requests": page.failed_requests}))
            if page.console_errors:
                result.issues.append(self._issue("render.console_errors", "page", page.url, f"Browser rendering emitted {len(page.console_errors)} console errors", evidence={"errors": page.console_errors}))
            if page.request_count > self.config.max_render_request_count:
                result.issues.append(self._issue("render.excessive_requests", "page", page.url, f"Rendered page made {page.request_count} network requests", evidence={"request_count": page.request_count, "threshold": self.config.max_render_request_count}))
            if page.transfer_bytes > self.config.max_render_transfer_bytes:
                result.issues.append(self._issue("render.excessive_transfer", "page", page.url, f"Rendered page transferred {page.transfer_bytes} bytes", evidence={"transfer_bytes": page.transfer_bytes, "threshold": self.config.max_render_transfer_bytes}))
            if page.network_requests_truncated:
                result.issues.append(self._issue("render.network_inventory_truncated", "page", page.url, "Rendered network inventory reached its configured request limit", evidence={"captured": len(page.network_requests), "observed": page.request_count, "limit": self.config.max_render_network_requests_per_page}))
            if page.accessibility_error:
                result.issues.append(self._issue("accessibility.unavailable", "page", page.url, page.accessibility_error))
            critical = [item for item in page.accessibility_violations if item.get("impact") in {"critical", "serious"}]
            other = [item for item in page.accessibility_violations if item.get("impact") not in {"critical", "serious"}]
            if critical:
                result.issues.append(self._issue("accessibility.critical_violations", "page", page.url, f"axe-core found {len(critical)} critical or serious violation types", evidence={"violations": critical}))
            if other:
                result.issues.append(self._issue("accessibility.violations", "page", page.url, f"axe-core found {len(other)} accessibility violation types", evidence={"violations": other}))
            if page.accessibility_truncated:
                result.issues.append(self._issue("accessibility.inventory_truncated", "page", page.url, "Accessibility evidence reached its configured limit", evidence={"captured_violation_types": len(page.accessibility_violations), "observed_violation_types": page.accessibility_violations_total}))

    def _add_rendered_signal_issues(self, result: CrawlResult, raw: Page, rendered: RenderedPage) -> None:
        rendered_canonical = normalize_url(rendered.final_url or raw.final_url or raw.url, rendered.canonical_url) if rendered.canonical_url else ""
        comparisons = (
            ("render.title_changed", _normalized_text(raw.title), _normalized_text(rendered.title), "Page title changes after JavaScript rendering"),
            ("render.meta_description_changed", _normalized_text(raw.meta_description), _normalized_text(rendered.meta_description), "Meta description changes after JavaScript rendering"),
            ("render.canonical_changed", raw.html_canonical_urls[0] if raw.html_canonical_urls else "", rendered_canonical or "", "Canonical declaration changes after JavaScript rendering"),
            ("render.robots_changed", sorted(set(raw.html_robots_directives)), sorted(set(rendered.robots_directives)), "Robots meta directives change after JavaScript rendering"),
            ("render.h1_changed", [_normalized_text(item) for item in raw.h1s], [_normalized_text(item) for item in rendered.h1s], "H1 headings change after JavaScript rendering"),
            ("render.language_changed", raw.html_language.casefold(), rendered.html_language.casefold(), "HTML language changes after JavaScript rendering"),
        )
        for rule_id, raw_value, rendered_value, message in comparisons:
            if raw_value != rendered_value:
                result.issues.append(self._issue(rule_id, "page", raw.url, message, evidence={"raw": raw_value, "rendered": rendered_value, "rendered_url": rendered.final_url}))

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
        response, fetch_error, attempts, downloaded = self._fetch_with_retries(fetcher, url, self.config.max_page_bytes, remaining, state.deadline)
        if fetch_error or response is None:
            exc = fetch_error or requests.RequestException("No response returned")
            result.errors.append(f"{url}: {exc}")
            result.issues.append(self._issue(_request_error_rule("page", exc), "page", url, str(exc), evidence={"attempts": attempts}))
            return False
        result.coverage.bytes_downloaded += downloaded
        html = ""
        signals = PageSignals()
        encoding = EncodingSignals()
        document = DocumentSignals()
        content = PageContent()
        url_quality = analyze_url(url)
        if response.status < 400 and response.content_type in ("text/html", "application/xhtml+xml"):
            encoding = analyze_encoding(response.body, response.headers.get("content-type", ""))
            html = response.body.decode(encoding.effective_charset, errors="replace")
            signals = extract_page_signals(response.final_url, html, response.headers.get("x-robots-tag", ""))
            content = extract_page_content(html, response.final_url)
            document = analyze_document(html)
        else:
            signals = extract_page_signals(response.final_url, "", response.headers.get("x-robots-tag", ""))
        header_links = parse_link_header(response.final_url, response.headers.get("link", ""))
        signals.header_canonical_urls = header_links.canonical_urls
        signals.header_hreflang = header_links.hreflang
        signals.canonical_urls.extend(header_links.canonical_urls)
        signals.invalid_canonical_values.extend(header_links.invalid_canonical_values)
        signals.hreflang.extend(header_links.hreflang)
        signals.canonical_url = signals.canonical_urls[0] if signals.canonical_urls else ""
        result.pages.append(Page(
            url=url, final_url=response.final_url, status=response.status, content_type=response.content_type,
            bytes=len(response.body), duration_ms=response.duration_ms, title=content.title,
            meta_description=content.meta_description, h1s=content.h1s, word_count=content.word_count,
            truncated=response.truncated, redirect_hops=response.redirect_hops, canonical_url=signals.canonical_url,
            canonical_urls=signals.canonical_urls, invalid_canonical_values=signals.invalid_canonical_values,
            robots_directives=signals.robots_directives, invalid_robots_directives=signals.invalid_robots_directives,
            robots_conflicts=signals.robots_conflicts, meta_refresh_url=signals.meta_refresh_url,
            meta_refresh_delay=signals.meta_refresh_delay, refresh_header=response.headers.get("refresh", ""),
            jsonld_errors=signals.jsonld_errors, jsonld_blocks=signals.jsonld_blocks,
            jsonld_integrity_errors=signals.jsonld_integrity_errors,
            jsonld_integrity_warnings=signals.jsonld_integrity_warnings,
            hreflang=signals.hreflang, declared_bytes=response.declared_bytes,
            viewport=content.viewport, og_title=content.og_title,
            og_description=content.og_description, og_image=content.og_image,
            twitter_card=content.twitter_card, twitter_image=content.twitter_image,
            images=content.images, url_length=url_quality.length,
            query_parameter_count=url_quality.query_parameter_count,
            html_language=signals.html_language,
            content_languages=_content_languages(response.headers.get("content-language", "")),
            http_charset=encoding.http_charset, meta_charsets=encoding.meta_charsets,
            visible_text_hash=content.visible_text_hash,
            visible_text_fingerprint=content.visible_text_fingerprint,
            document_head_count=document.head_count, document_body_count=document.body_count,
            title_count=document.title_count, meta_description_count=document.meta_description_count,
            header_canonical_urls=signals.header_canonical_urls,
            header_hreflang=signals.header_hreflang,
            html_canonical_urls=signals.html_canonical_urls,
            html_robots_directives=signals.html_robots_directives,
            links=content.links, heading_levels=content.heading_levels,
            fetch_attempts=attempts,
        ))
        result.coverage.pages_fetched += 1
        self._add_page_response_issues(result, url, response, signals, encoding, document, state.edges)
        self._add_url_quality_issues(result, url, url_quality, state.edges)
        if html:
            self._queue_page_discoveries(state, response.final_url, html, signals, content)
        else:
            self._queue_declared_targets(state, response.final_url, signals)
        if response.truncated:
            result.issues.append(self._issue("page.response_truncated", "page", url, f"Page download stopped after {len(response.body)} bytes", state.edges, {"bytes_read": len(response.body), "limit": fetch_limit}))
            if downloaded >= remaining:
                self._set_limit(result, "max_total_bytes")
                return True
            self._mark_incomplete(result, "max_page_bytes")
        return False

    def _add_page_response_issues(self, result: CrawlResult, url: str, response: FetchResponse, signals: PageSignals, encoding: EncodingSignals, document: DocumentSignals, edges: set[Edge]) -> None:
        linked = any(edge.target_url == url and edge.context == "a.href" for edge in edges)
        if response.status >= 400 and not linked:
            result.issues.append(self._issue("page.http_error", "page", url, f"Page returns HTTP {response.status}", edges, {"status": response.status}))
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
        if signals.robots_conflicts:
            result.issues.append(self._issue("directive.conflicting_robots", "page", url, "Contradictory robots directives were found", edges, {"conflicts": signals.robots_conflicts, "directives": signals.robots_directives}))
        content_languages = _content_languages(response.headers.get("content-language", ""))
        if response.status < 400 and response.content_type in ("text/html", "application/xhtml+xml"):
            if document.head_count == 0:
                result.issues.append(self._issue("document.head_missing", "page", url, "HTML source has no explicit head element", edges))
            elif document.head_count > 1:
                result.issues.append(self._issue("document.multiple_head", "page", url, f"HTML source contains {document.head_count} head elements", edges, {"count": document.head_count}))
            if document.body_count == 0:
                result.issues.append(self._issue("document.body_missing", "page", url, "HTML source has no explicit body element", edges))
            elif document.body_count > 1:
                result.issues.append(self._issue("document.multiple_body", "page", url, f"HTML source contains {document.body_count} body elements", edges, {"count": document.body_count}))
            if document.title_count > 1:
                result.issues.append(self._issue("document.multiple_title", "page", url, f"HTML source contains {document.title_count} title elements", edges, {"count": document.title_count}))
            if document.meta_description_count > 1:
                result.issues.append(self._issue("document.multiple_meta_description", "page", url, f"HTML source contains {document.meta_description_count} meta descriptions", edges, {"count": document.meta_description_count}))
            if document.title_outside_head or document.meta_description_outside_head:
                result.issues.append(self._issue("document.metadata_outside_head", "page", url, "Title or meta description appears outside the document head", edges, {"title_outside_head": document.title_outside_head, "meta_description_outside_head": document.meta_description_outside_head}))
            if not encoding.http_charset and not encoding.meta_charsets:
                result.issues.append(self._issue("encoding.missing", "page", url, "HTML response has no HTTP or meta charset declaration", edges))
            if encoding.invalid_charsets:
                result.issues.append(self._issue("encoding.invalid", "page", url, "HTML response declares invalid character encodings", edges, {"charsets": encoding.invalid_charsets}))
            if len(encoding.canonical_charsets) > 1:
                result.issues.append(self._issue("encoding.conflict", "page", url, "HTTP and HTML character encoding declarations do not agree", edges, {"http_charset": encoding.http_charset, "meta_charsets": encoding.meta_charsets}))
            late_offsets = [offset for offset in encoding.meta_charset_offsets if offset > 1024]
            if late_offsets:
                result.issues.append(self._issue("encoding.meta_late", "page", url, "Meta charset declaration is not entirely within the first 1024 bytes", edges, {"declaration_end_offsets": late_offsets}))
            if not signals.html_language_declared:
                result.issues.append(self._issue("language.html_missing", "page", url, "Root html element has no lang attribute", edges))
            elif not signals.html_language or not valid_language_tag(signals.html_language):
                result.issues.append(self._issue("language.html_invalid", "page", url, f"Invalid html lang value: {signals.html_language or '<empty>'}", edges, {"language": signals.html_language}))
            invalid_content_languages = [item for item in content_languages if not valid_language_tag(item)]
            if invalid_content_languages:
                result.issues.append(self._issue("language.content_invalid", "page", url, "Content-Language contains invalid language tags", edges, {"languages": invalid_content_languages}))
            if signals.html_language and content_languages and signals.html_language.lower() not in {item.lower() for item in content_languages}:
                result.issues.append(self._issue("language.html_content_conflict", "page", url, "HTML and HTTP language declarations do not agree", edges, {"html_language": signals.html_language, "content_languages": content_languages}))
            self_languages = sorted({item.language for item in signals.hreflang if item.url == response.final_url and item.language.lower() != "x-default"})
            if signals.html_language and self_languages and signals.html_language.lower() not in {item.lower() for item in self_languages}:
                result.issues.append(self._issue("language.html_hreflang_conflict", "page", url, "HTML language does not match a self-referencing hreflang value", edges, {"html_language": signals.html_language, "self_hreflang_languages": self_languages}))
        canonical_count = len(signals.canonical_urls) + len(signals.invalid_canonical_values)
        if signals.html_canonical_urls and signals.header_canonical_urls and set(signals.html_canonical_urls) != set(signals.header_canonical_urls):
            result.issues.append(self._issue("canonical.header_html_conflict", "page", url, "HTTP Link and HTML canonical declarations do not agree", edges, {"html_canonical_urls": signals.html_canonical_urls, "header_canonical_urls": signals.header_canonical_urls}))
        if canonical_count > 1:
            result.issues.append(self._issue("canonical.multiple", "page", url, f"Page contains {canonical_count} canonical declarations", edges, {"canonical_urls": signals.canonical_urls, "invalid_values": signals.invalid_canonical_values}))
        if signals.invalid_canonical_values:
            result.issues.append(self._issue("canonical.invalid", "page", url, "One or more canonical declarations cannot be resolved", edges, {"values": signals.invalid_canonical_values}))
        if signals.meta_refresh_delay is not None:
            result.issues.append(self._issue("page.meta_refresh", "page", url, "Page uses a meta refresh directive", edges, {"delay_seconds": signals.meta_refresh_delay, "target_url": signals.meta_refresh_url}))
        if response.headers.get("refresh"):
            result.issues.append(self._issue("page.refresh_header", "page", url, "Page response uses an HTTP Refresh header", edges, {"refresh": response.headers["refresh"]}))
        if signals.jsonld_errors:
            result.issues.append(self._issue("structured_data.invalid_jsonld", "page", url, "One or more JSON-LD blocks are invalid", edges, {"errors": signals.jsonld_errors}))
        if signals.duplicate_jsonld_blocks:
            result.issues.append(self._issue("structured_data.duplicate_jsonld", "page", url, "Identical JSON-LD script blocks appear more than once", edges, {"duplicates": signals.duplicate_jsonld_blocks}))
        if signals.jsonld_integrity_errors:
            result.issues.append(self._issue("structured_data.invalid_shape", "page", url, "JSON-LD contains structurally invalid keyword values", edges, {"errors": signals.jsonld_integrity_errors}))
        missing_context = [item for item in signals.jsonld_integrity_warnings if "no @context" in item]
        if missing_context:
            result.issues.append(self._issue("structured_data.missing_context", "page", url, "Typed JSON-LD has no declared context", edges, {"warnings": missing_context}))
        unresolved = [item for item in signals.jsonld_integrity_warnings if "no definition" in item]
        if unresolved:
            result.issues.append(self._issue("structured_data.unresolved_fragment", "page", url, "JSON-LD references undefined local identifiers", edges, {"warnings": unresolved}))

    def _add_url_quality_issues(self, result: CrawlResult, url: str, quality: UrlQuality, edges: set[Edge]) -> None:
        if quality.length > self.config.max_url_chars:
            result.issues.append(self._issue("url.too_long", "page", url, f"URL contains {quality.length} characters", edges, {"characters": quality.length, "threshold": self.config.max_url_chars}))
        if quality.uppercase_path:
            result.issues.append(self._issue("url.uppercase_path", "page", url, "URL path contains uppercase characters", edges))
        if quality.underscore_path:
            result.issues.append(self._issue("url.underscore_path", "page", url, "URL path contains underscore characters", edges))
        if quality.query_parameter_count > self.config.max_query_parameters:
            result.issues.append(self._issue("url.excessive_parameters", "page", url, f"URL contains {quality.query_parameter_count} query parameters", edges, {"count": quality.query_parameter_count, "threshold": self.config.max_query_parameters}))
        if quality.tracking_parameters:
            result.issues.append(self._issue("url.tracking_parameters", "page", url, "URL contains tracking parameters", edges, {"parameters": quality.tracking_parameters}))
        if quality.repeated_segments:
            result.issues.append(self._issue("url.repeated_segments", "page", url, "URL repeats adjacent path segments", edges, {"segments": quality.repeated_segments}))

    def _queue_page_discoveries(self, state: _RunState, final_url: str, html: str, signals: PageSignals, content: PageContent) -> None:
        links, resources, found_edges = discover_html(final_url, html)
        state.edges.update(found_edges)
        self._queue_declared_targets(state, final_url, signals)
        for link in links:
            if same_origin(state.start_url, link):
                self._enqueue_page(state, link)
        for item in resources:
            if self.config.follow_external_resources or same_origin(state.start_url, item.url):
                state.pending_resources.setdefault(item.url, item.kind)
        for context, url in (("meta.og:image", content.og_image), ("meta.twitter:image", content.twitter_image)):
            if not url or (not self.config.follow_external_resources and not same_origin(state.start_url, url)):
                continue
            state.pending_resources.setdefault(url, "image")
            state.edges.add(Edge(final_url, url, context))

    def _queue_declared_targets(self, state: _RunState, final_url: str, signals: PageSignals) -> None:
        for canonical_url in dict.fromkeys(signals.canonical_urls):
            state.edges.add(Edge(final_url, canonical_url, "link.canonical"))
            if same_origin(state.start_url, canonical_url):
                self._enqueue_page(state, canonical_url)
        for reference in signals.hreflang:
            state.edges.add(Edge(final_url, reference.url, f"link.hreflang:{reference.language}"))
            if same_origin(state.start_url, reference.url):
                self._enqueue_page(state, reference.url)

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
            discovered, total_bytes_truncated = self._fetch_resource(fetcher, state.result, state.start_url, url, kind, state.pending_resources, state.edges, remaining, state.deadline)
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
        for edge in sorted(edges, key=lambda item: (item.context, item.source_url, item.target_url)):
            if policy.allows(edge.target_url):
                continue
            if edge.context == "link.canonical":
                result.issues.append(self._issue("canonical.blocked_target", "page", edge.source_url, "Canonical target is disallowed by robots.txt", edges, {"target_url": edge.target_url, "user_agent": policy.document.user_agent}))
            elif edge.context.startswith("link.hreflang:"):
                result.issues.append(self._issue("hreflang.target_blocked", "page", edge.source_url, "Hreflang target is disallowed by robots.txt", edges, {"target_url": edge.target_url, "language": edge.context.partition(":")[2], "user_agent": policy.document.user_agent}))
        for url in sorted({url for sitemap in result.sitemaps for url in sitemap.urls}):
            if not policy.allows(url):
                result.issues.append(self._issue("sitemap.url_blocked", "page", url, "Sitemap URL is disallowed by robots.txt", edges, {"user_agent": policy.document.user_agent}))

    def _fetch_resource(self, fetcher: Fetcher, result: CrawlResult, start_url: str, url: str, kind: str, pending: dict[str, str], edges: set[Edge], remaining_total_bytes: int, deadline: float = float("inf")) -> tuple[list[str], bool]:
        discovered_urls: list[str] = []
        response, fetch_error, attempts, downloaded = self._fetch_with_retries(fetcher, url, self.config.max_resource_bytes, remaining_total_bytes, deadline)
        if fetch_error or response is None:
            exc = fetch_error or requests.RequestException("No response returned")
            result.resources.append(Resource(url=url, kind=kind, fetch_attempts=attempts))
            result.errors.append(f"{url}: {exc}")
            result.issues.append(self._issue(_request_error_rule("resource", exc), "resource", url, str(exc), edges, {"attempts": attempts}))
            return discovered_urls, False
        size = len(response.body)
        result.coverage.bytes_downloaded += downloaded
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
            fetch_attempts=attempts,
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
        total_bytes_truncated = response.truncated and downloaded >= remaining_total_bytes
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

        # Link equity flows from every followed link, including links on pages
        # that are themselves noindex, so the source set is all HTML pages.
        html_pages = [page for page in result.pages if _is_html_page(page)]
        indexable = [page for page in html_pages if _is_indexable_html(page)]
        if len(indexable) >= self.config.min_site_pages_for_link_metrics:
            dofollow_sources: dict[str, set[str]] = {}
            nofollow_sources: dict[str, set[str]] = {}
            outgoing_targets: dict[str, set[str]] = {}
            for source_page in html_pages:
                source_url = source_page.final_url or source_page.url
                for link in source_page.links:
                    if not same_origin(result.start_url, link.url):
                        continue
                    if link.nofollow:
                        nofollow_sources.setdefault(link.url, set()).add(source_url)
                        continue
                    dofollow_sources.setdefault(link.url, set()).add(source_url)
                    outgoing_targets.setdefault(source_url, set()).add(link.url)
            for page in html_pages:
                page_indexable = _is_indexable_html(page)
                aliases_for_page = {page.url, page.final_url}
                dofollow_in = set().union(*(dofollow_sources.get(alias, set()) for alias in aliases_for_page))
                nofollow_in = set().union(*(nofollow_sources.get(alias, set()) for alias in aliases_for_page))
                outgoing_count = len(set().union(*(outgoing_targets.get(alias, set()) for alias in aliases_for_page)))

                def add(rule_id: str, message: str, evidence: dict[str, object]) -> None:
                    payload = dict(evidence)
                    payload["indexable"] = page_indexable
                    severity = None if page_indexable else _downgraded_severity(get_rule(rule_id).severity)
                    result.issues.append(self._issue(rule_id, "page", page.url, message, edges, payload, severity))

                if page.url != result.start_url and len(dofollow_in) < self.config.min_internal_inlinks:
                    add("architecture.low_inlinks", f"Page has {len(dofollow_in)} incoming dofollow internal links", {"count": len(dofollow_in), "threshold": self.config.min_internal_inlinks})
                # A page whose only inbound equity comes through one link is a
                # single edit away from being orphaned.
                if page.url != result.start_url and len(dofollow_in) == 1:
                    add("architecture.single_dofollow_inlink", "Page has exactly one incoming dofollow internal link", {"source_urls": sorted(dofollow_in)})
                # Mixed rel on links to the same target is nearly always an
                # accident of templating rather than a deliberate policy.
                if dofollow_in and nofollow_in:
                    add("architecture.mixed_rel_inlinks", f"Page receives {len(dofollow_in)} dofollow and {len(nofollow_in)} nofollow internal links", {"dofollow_urls": sorted(dofollow_in)[:20], "nofollow_urls": sorted(nofollow_in)[:20], "dofollow_count": len(dofollow_in), "nofollow_count": len(nofollow_in)})
                if outgoing_count == 0 and page_indexable:
                    add("architecture.no_outlinks", "Page has no outgoing internal HTML links", {})

    def _add_content_issues(self, result: CrawlResult, edges: set[Edge]) -> None:
        # Content defects exist on noindex and canonicalised pages too, and
        # commercial auditors report them as a separate bucket. Evaluate every
        # HTML page; findings on non-indexable pages are tagged and dropped one
        # severity level so triage can tell the two apart.
        html_pages = [page for page in result.pages if _is_html_page(page)]
        pages = [page for page in html_pages if _is_indexable_html(page)]
        for page in html_pages:
            indexable = _is_indexable_html(page)

            def add(rule_id: str, message: str, evidence: dict[str, object] | None = None) -> None:
                payload = dict(evidence or {})
                payload["indexable"] = indexable
                severity = None if indexable else _downgraded_severity(get_rule(rule_id).severity)
                result.issues.append(self._issue(rule_id, "page", page.url, message, edges, payload, severity))

            if indexable and not page.canonical_urls and not page.invalid_canonical_values:
                add("canonical.missing", "Indexable page has no canonical declaration")
            if not page.title:
                add("content.title_missing", "Page has no title element")
            elif len(page.title) < self.config.min_title_chars:
                add("content.title_too_short", f"Title contains {len(page.title)} characters", {"characters": len(page.title), "threshold": self.config.min_title_chars})
            elif len(page.title) > self.config.max_title_chars:
                add("content.title_too_long", f"Title contains {len(page.title)} characters", {"characters": len(page.title), "threshold": self.config.max_title_chars})
            if not page.meta_description:
                add("content.meta_description_missing", "Page has no meta description")
            elif len(page.meta_description) < self.config.min_meta_description_chars:
                add("content.meta_description_too_short", f"Meta description contains {len(page.meta_description)} characters", {"characters": len(page.meta_description), "threshold": self.config.min_meta_description_chars})
            elif len(page.meta_description) > self.config.max_meta_description_chars:
                add("content.meta_description_too_long", f"Meta description contains {len(page.meta_description)} characters", {"characters": len(page.meta_description), "threshold": self.config.max_meta_description_chars})
            if not page.h1s:
                add("content.h1_missing", "Page has no H1 heading")
            elif len(page.h1s) > 1:
                add("content.multiple_h1", f"Page contains {len(page.h1s)} H1 headings", {"count": len(page.h1s), "headings": page.h1s})
            skipped = [(left, right) for left, right in zip(page.heading_levels, page.heading_levels[1:]) if right > left + 1]
            if skipped:
                add("content.heading_order_skipped", "Heading hierarchy skips one or more levels", {"transitions": skipped, "levels": page.heading_levels})
            if page.word_count < self.config.min_content_words:
                add("content.thin", f"Page contains approximately {page.word_count} visible words", {"words": page.word_count, "threshold": self.config.min_content_words})
            if not page.viewport:
                add("content.viewport_missing", "Page has no viewport meta tag")
            missing_alt = sorted({image.url for image in page.images if image.alt is None})
            if missing_alt:
                add("content.image_alt_missing", f"Page contains {len(missing_alt)} images without alt attributes", {"images": missing_alt})
            empty_alt = sorted({image.url for image in page.images if image.alt_state == ALT_EMPTY_CONTENT})
            if empty_alt:
                add("content.image_alt_empty", f"Page contains {len(empty_alt)} content images with an empty alt attribute", {"images": empty_alt})
            empty_links = sorted({link.url for link in page.links if not link.text and _same_hostname(page.final_url or page.url, link.url)})
            if empty_links:
                add("link.text_missing", f"Page contains {len(empty_links)} internal links without descriptive text", {"links": empty_links})
            nofollow_links = sorted({link.url for link in page.links if link.nofollow and _same_hostname(page.final_url or page.url, link.url)})
            if nofollow_links:
                add("link.internal_nofollow", f"Page contains {len(nofollow_links)} nofollow internal links", {"links": nofollow_links})
            insecure_links = sorted({link.url for link in page.links if (page.final_url or page.url).startswith("https://") and link.url.startswith("http://") and _same_hostname(page.final_url or page.url, link.url)})
            if insecure_links:
                add("link.insecure_internal", f"HTTPS page contains {len(insecure_links)} insecure internal links", {"links": insecure_links})
            not_found_text = " ".join([page.title, *page.h1s]).casefold()
            if page.status == 200 and page.word_count <= self.config.max_soft_404_words and _looks_not_found(not_found_text):
                add("page.soft_404", "HTTP 200 page has a not-found title or primary heading and little content", {"words": page.word_count, "threshold": self.config.max_soft_404_words})
            if not page.og_title:
                add("social.og_title_missing", "Page has no og:title value")
            if not page.og_description:
                add("social.og_description_missing", "Page has no og:description value")
            if not page.og_image:
                add("social.og_image_missing", "Page has no og:image value")
            if not page.twitter_card:
                add("social.twitter_card_missing", "Page has no twitter:card value")
        # Duplicate content only matters between pages eligible to rank, so
        # these stay on the indexable set.
        self._add_duplicate_content_issues(result, pages, edges, "title", "content.duplicate_title")
        self._add_duplicate_content_issues(result, pages, edges, "meta_description", "content.duplicate_meta_description")
        self._add_duplicate_content_issues(result, [page for page in pages if page.h1s], edges, "primary_h1", "content.duplicate_h1")
        self._add_duplicate_body_issues(result, pages, edges)
        self._add_image_delivery_issues(result, pages, edges)

    def _add_duplicate_body_issues(self, result: CrawlResult, pages: list[Page], edges: set[Edge]) -> None:
        eligible = [page for page in pages if page.word_count >= self.config.min_duplicate_content_words and page.visible_text_hash]
        exact: dict[str, list[Page]] = {}
        for page in eligible:
            exact.setdefault(page.visible_text_hash, []).append(page)
        exact_pairs: set[frozenset[str]] = set()
        for digest, matches in exact.items():
            if len(matches) < 2:
                continue
            urls = sorted(page.url for page in matches)
            for page in matches:
                result.issues.append(self._issue("content.duplicate_body", "page", page.url, f"Visible content is identical across {len(matches)} indexable pages", edges, {"sha256": digest, "urls": urls}))
            exact_pairs.update(frozenset((left.url, right.url)) for index, left in enumerate(matches) for right in matches[index + 1:])

        similar: dict[str, set[str]] = {}
        max_distance = int(64 * (1 - self.config.near_duplicate_similarity))
        for index, left in enumerate(eligible):
            if not left.visible_text_fingerprint:
                continue
            for right in eligible[index + 1:]:
                pair = frozenset((left.url, right.url))
                if pair in exact_pairs or not right.visible_text_fingerprint:
                    continue
                distance = (int(left.visible_text_fingerprint, 16) ^ int(right.visible_text_fingerprint, 16)).bit_count()
                if distance <= max_distance:
                    similar.setdefault(left.url, set()).add(right.url)
                    similar.setdefault(right.url, set()).add(left.url)
        for page in eligible:
            matches = sorted(similar.get(page.url, set()))
            if matches:
                result.issues.append(self._issue("content.near_duplicate_body", "page", page.url, f"Visible content is substantially similar to {len(matches)} other indexable pages", edges, {"similar_urls": matches, "threshold": self.config.near_duplicate_similarity}))

    def _add_image_delivery_issues(self, result: CrawlResult, pages: list[Page], edges: set[Edge]) -> None:
        resources = {resource.url: resource for resource in result.resources if resource.kind == "image" and resource.status is not None and resource.status < 400}
        for page in pages:
            missing_dimensions = sorted({image.url for image in page.images if image.width is None or image.height is None})
            if missing_dimensions:
                result.issues.append(self._issue("image.missing_dimensions", "page", page.url, f"Page contains {len(missing_dimensions)} images without positive width and height attributes", edges, {"images": missing_dimensions}))
            missing_responsive = sorted({
                image.url for image in page.images
                if not image.responsive and (resource := resources.get(image.url))
                and resource.image_width is not None and resource.image_width >= self.config.min_responsive_image_width
            })
            if missing_responsive:
                result.issues.append(self._issue("image.missing_responsive_source", "page", page.url, f"Page loads {len(missing_responsive)} large images without srcset", edges, {"images": missing_responsive, "minimum_width": self.config.min_responsive_image_width}))
        for resource in resources.values():
            if resource.bytes >= self.config.min_legacy_image_bytes and resource.image_format.lower() in {"jpeg", "png", "gif", "bmp", "tiff"}:
                result.issues.append(self._issue("image.legacy_format", "resource", resource.url, f"{resource.bytes}-byte image is served as {resource.image_format}", edges, {"bytes": resource.bytes, "format": resource.image_format, "threshold": self.config.min_legacy_image_bytes}))

    def _add_duplicate_content_issues(self, result: CrawlResult, pages: list[Page], edges: set[Edge], attribute: str, rule_id: str) -> None:
        groups: dict[str, list[Page]] = {}
        for page in pages:
            raw_value = page.h1s[0] if attribute == "primary_h1" and page.h1s else getattr(page, attribute, "")
            value = " ".join(str(raw_value).split())
            if value:
                groups.setdefault(value.casefold(), []).append(page)
        for matches in groups.values():
            if len(matches) < 2:
                continue
            urls = sorted(page.url for page in matches)
            for page in matches:
                raw_value = page.h1s[0] if attribute == "primary_h1" else getattr(page, attribute)
                result.issues.append(self._issue(rule_id, "page", page.url, f"Content is shared by {len(matches)} indexable pages", edges, {"value": raw_value, "urls": urls}))

    def _fetch_with_retries(self, fetcher: Fetcher, url: str, max_response_bytes: int, byte_budget: int, deadline: float) -> tuple[FetchResponse | None, requests.RequestException | None, int, int]:
        started = time.monotonic()
        attempts = 0
        downloaded = 0
        last_response: FetchResponse | None = None
        retryable_statuses = {408, 425, 429, 500, 502, 503, 504}
        while attempts < self.config.max_fetch_attempts and downloaded < byte_budget and time.monotonic() < deadline:
            attempts += 1
            try:
                response = fetcher.get(url, min(max_response_bytes, byte_budget - downloaded))
            except requests.RequestException as exc:
                retryable = isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)) and not isinstance(exc, (requests.exceptions.SSLError, requests.exceptions.TooManyRedirects))
                if not retryable or attempts >= self.config.max_fetch_attempts:
                    return None, exc, attempts, downloaded
                if not self._retry_pause(self.config.retry_backoff_seconds * (2 ** (attempts - 1)), deadline):
                    return None, exc, attempts, downloaded
                continue
            last_response = response
            downloaded += len(response.body)
            if response.status not in retryable_statuses or attempts >= self.config.max_fetch_attempts or downloaded >= byte_budget:
                response.duration_ms = round((time.monotonic() - started) * 1000)
                return response, None, attempts, downloaded
            retry_after = response.headers.get("retry-after", "")
            delay = min(float(retry_after), self.config.max_retry_after_seconds) if retry_after.replace(".", "", 1).isdigit() else self.config.retry_backoff_seconds * (2 ** (attempts - 1))
            if not self._retry_pause(delay, deadline):
                response.duration_ms = round((time.monotonic() - started) * 1000)
                return response, None, attempts, downloaded
        if last_response:
            last_response.duration_ms = round((time.monotonic() - started) * 1000)
        return last_response, None, attempts, downloaded

    @staticmethod
    def _retry_pause(delay: float, deadline: float) -> bool:
        if time.monotonic() + delay >= deadline:
            return False
        if delay:
            time.sleep(delay)
        return True

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

    def _issue(self, rule_id: str, entity: str, url: str, message: str, edges: set[Edge] | None = None, evidence: dict[str, object] | None = None, severity: str | None = None) -> Issue:
        rule = get_rule(rule_id)
        refs = _root_referrers(url, edges or set())
        return Issue(rule.id, rule.title, severity or rule.severity, entity, url, message, evidence or {}, refs, rule.remediation)

    def _emit(self, result: CrawlResult, pages_queued: int, resources_queued: int, latest_url: str) -> None:
        if self.progress:
            self.progress({"pages_fetched": result.coverage.pages_fetched, "pages_queued": pages_queued, "resources_fetched": result.coverage.resources_fetched, "resources_queued": max(0, resources_queued), "errors": len(result.errors), "latest_url": latest_url, "updated_at": _now()})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_languages(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _normalized_text(value: str) -> str:
    return " ".join(value.split())


def _same_hostname(left: str, right: str) -> bool:
    return bool(urlsplit(left).hostname and urlsplit(left).hostname == urlsplit(right).hostname)


def _looks_not_found(value: str) -> bool:
    return bool(re.search(r"\b(?:404|page (?:not found|does not exist|doesn't exist|cannot be found)|content not found|we (?:could not|couldn't) find)\b", value))


def _request_error_rule(entity: str, error: requests.RequestException) -> str:
    if isinstance(error, requests.exceptions.TooManyRedirects):
        return f"{entity}.redirect_loop"
    if isinstance(error, requests.exceptions.SSLError):
        return f"{entity}.tls_error"
    if isinstance(error, requests.exceptions.Timeout):
        return f"{entity}.timeout"
    return f"{entity}.fetch_failed"


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


_SEVERITY_ORDER = ("error", "warning", "info")


def _downgraded_severity(severity: str) -> str:
    """One step less severe, floored at info."""
    try:
        return _SEVERITY_ORDER[min(_SEVERITY_ORDER.index(severity) + 1, len(_SEVERITY_ORDER) - 1)]
    except ValueError:
        return severity


def _is_html_page(page: Page) -> bool:
    """Fetched HTML worth analysing, indexable or not."""
    if page.status >= 400 or page.truncated:
        return False
    return page.content_type in ("text/html", "application/xhtml+xml")


def _is_indexable_html(page: Page) -> bool:
    if page.status >= 400 or page.redirect_hops or page.truncated or page.content_type not in ("text/html", "application/xhtml+xml"):
        return False
    if {"noindex", "none"} & set(page.robots_directives):
        return False
    return not page.canonical_url or page.canonical_url in {page.url, page.final_url}
