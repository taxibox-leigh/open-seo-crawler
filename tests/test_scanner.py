from __future__ import annotations

import json
import threading
import unittest
import requests
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tempfile import TemporaryDirectory
from pathlib import Path

from seo_scanner.cli import main
from seo_scanner.analyzers.image import inspect_image
from seo_scanner.analyzers.content import extract_page_content
from seo_scanner.analyzers.directives import extract_page_signals
from seo_scanner.analyzers.sitemap import parse_sitemap
from seo_scanner.analyzers.hreflang import valid_language_tag
from seo_scanner.analyzers.robots import parse_robots
from seo_scanner.baseline import apply_suppressions, compare_with_baseline
from seo_scanner.config import ScannerConfig
from seo_scanner.discovery import discover_css, discover_html
from seo_scanner.fetch import Fetcher, FetchResponse
from seo_scanner.runner import Scanner, _is_browser_subresource
from seo_scanner.scope import normalize_url
from seo_scanner.models import CrawlResult, Edge, Issue, Page, Resource, SitemapDocument
from seo_scanner.render import render_pages, select_render_urls


class FakeMessage:
    type = "error"
    text = "Uncaught example"


class FakeResponse:
    url = "https://example.com/missing.js"
    status = 404


class FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.handlers = {}

    def on(self, event, handler) -> None:
        self.handlers[event] = handler

    def goto(self, url, **_) -> None:
        self.url = url + "rendered"
        self.handlers["console"](FakeMessage())
        self.handlers["response"](FakeResponse())

    def wait_for_timeout(self, _) -> None:
        pass

    def close(self) -> None:
        pass


class FakeBrowser:
    def new_page(self) -> FakePage:
        return FakePage()

    def close(self) -> None:
        pass


class FakePlaywright:
    chromium = None

    def __init__(self) -> None:
        self.chromium = self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def launch(self, **_) -> FakeBrowser:
        return FakeBrowser()


class FixtureHandler(BaseHTTPRequestHandler):
    external_url = ""
    external_url_2 = ""

    def do_GET(self) -> None:
        base = f"http://{self.headers['Host']}"
        external_anchor = f'<a href="{self.external_url}">external</a>' if self.external_url else ""
        external_anchor += f'<a href="{self.external_url_2}">external 2</a>' if self.external_url_2 else ""
        routes = {
            "/robots.txt": (200, "text/plain", f"User-agent: *\nSitemap: {base}/sitemap.xml\n".encode(), {}),
            "/sitemap.xml": (200, "application/xml", f'''<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>{base}/sitemap-pages.xml</loc></sitemap><sitemap><loc>{base}/sitemap-duplicate.xml</loc></sitemap></sitemapindex>'''.encode(), {}),
            "/sitemap-pages.xml": (200, "application/xml", f'''<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{base}/</loc></url><url><loc>{base}/page-2</loc><lastmod>not-a-date</lastmod></url><url><loc>{base}/missing-page</loc></url></urlset>'''.encode(), {}),
            "/sitemap-duplicate.xml": (200, "application/xml", f'''<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{base}/</loc></url></urlset>'''.encode(), {}),
            "/": (200, "text/html", f'''<title>Fixture</title><link rel="canonical" href="/"><link rel="alternate" hreflang="en-AU" href="/"><link rel="alternate" hreflang="en-AU" href="/page-2"><link rel="alternate" hreflang="en_AU" href="/missing-page"><a href="/page-2">next</a><a href="/missing-page">missing</a>
                {external_anchor}<img src="/broken.png"><img srcset="/small.webp 1x, /large.webp 2x">
                <script src="/wrong.js"></script><link rel="stylesheet" href="/style.css">'''.encode(), {}),
            "/page-2": (200, "text/html", b'<meta name="robots" content="noindex, nonsense"><link rel="canonical" href="/canonical-hop"><link rel="alternate" hreflang="en-AU" href="/"><script type="application/ld+json">{"broken":}</script><img src="/redirect.png">', {}),
            "/canonical-hop": (302, "text/plain", b"", {"Location": "/canonical-final"}),
            "/canonical-final": (200, "text/html", b'<link rel="canonical" href="/page-2">', {}),
            "/broken.png": (404, "image/png", b"missing", {}),
            "/small.webp": (200, "image/webp", b"RIFF\x12\x00\x00\x00WEBPVP8X\x0a\x00\x00\x00\x00\x00\x00\x00\x01\x00\x00\x02\x00\x00", {"Cache-Control": "public, max-age=31536000"}),
            "/large.webp": (200, "image/webp", b"x" * 128, {}),
            "/wrong.js": (200, "text/html", b"<html>not js</html>", {}),
            "/style.css": (200, "text/css", b"body{background:url('/nested.png')} @font-face{src:url('/font.woff2')}", {}),
            "/nested.png": (200, "image/png", b"png", {}),
            "/font.woff2": (200, "font/woff2", b"font", {}),
            "/redirect.png": (302, "text/plain", b"", {"Location": "/small.webp"}),
        }
        status, mime, body, headers = routes.get(self.path, (404, "text/plain", b"no", {}))
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        pass


class FixtureServer:
    def __enter__(self) -> str:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}/"

    def __exit__(self, *_: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class UnitTests(unittest.TestCase):
    def test_robots_policy_reports_syntax_and_applies_named_agent_rules(self) -> None:
        policy = parse_robots(
            "https://example.com/robots.txt",
            b"User-agent: *\nDisallow: /private/\nAllow: /private/public\nDisallow /broken\n",
            "Googlebot",
        )
        self.assertFalse(policy.allows("https://example.com/private/page"))
        self.assertTrue(policy.allows("https://example.com/private/public"))
        self.assertIn("missing a colon", policy.document.errors[0])

    def test_robots_findings_cover_blocked_pages_and_render_resources(self) -> None:
        result = CrawlResult(start_url="https://example.com/", started_at="now")
        result.pages = [Page("https://example.com/private/page", "https://example.com/private/page", 200, "text/html", 1, 1)]
        result.resources = [Resource("https://example.com/private/app.js", "https://example.com/private/app.js", "script", 200)]
        policy = parse_robots("https://example.com/robots.txt", b"User-agent: *\nDisallow: /private/", "Googlebot")
        result.robots = policy.document
        Scanner()._add_robots_issues(result, set(), policy)
        self.assertEqual({issue.rule_id for issue in result.issues}, {"robots.blocked_page", "robots.blocked_resource"})
        self.assertEqual(result.robots.blocked_pages, ["https://example.com/private/page"])
        self.assertEqual(result.robots.blocked_resources, ["https://example.com/private/app.js"])

    def test_page_content_extracts_metadata_headings_and_visible_words(self) -> None:
        content = extract_page_content('''<title> Example title </title><meta NAME="Description" content="A useful summary">
            <h1>Main <span>heading</span></h1><p>Three visible words.</p><script>ignored script words</script>''')
        self.assertEqual(content.title, "Example title")
        self.assertEqual(content.meta_description, "A useful summary")
        self.assertEqual(content.h1s, ["Main heading"])
        self.assertEqual(content.word_count, 7)

    def test_content_rules_cover_missing_long_duplicate_and_thin_pages(self) -> None:
        result = CrawlResult(start_url="https://example.com/", started_at="now")
        result.pages = [
            Page("https://example.com/", "https://example.com/", 200, "text/html", 1, 1, title="Shared title", meta_description="Shared description", h1s=["One", "Two"], word_count=2),
            Page("https://example.com/b", "https://example.com/b", 200, "text/html", 1, 1, title="Shared title", meta_description="Shared description", word_count=0),
        ]
        scanner = Scanner(ScannerConfig(max_title_chars=5, max_meta_description_chars=5, min_content_words=3))
        scanner._add_content_issues(result, set())
        rules = [issue.rule_id for issue in result.issues]
        self.assertEqual(rules.count("content.duplicate_title"), 2)
        self.assertEqual(rules.count("content.duplicate_meta_description"), 2)
        self.assertIn("content.multiple_h1", rules)
        self.assertIn("content.h1_missing", rules)
        self.assertEqual(rules.count("content.thin"), 2)

    def test_daily_render_sample_rotates_all_sorted_urls_without_state(self) -> None:
        config = ScannerConfig(max_rendered_pages=2, render_sample_strategy="daily_rotation")
        urls = [f"https://example.com/{item}" for item in ("e", "c", "a", "d", "b")]
        first = select_render_urls(urls, config, day_of_year=1)
        second = select_render_urls(urls, config, day_of_year=2)
        third = select_render_urls(urls, config, day_of_year=3)
        wrapped = select_render_urls(urls, config, day_of_year=4)
        self.assertEqual(first, (["https://example.com/a", "https://example.com/b"], 5, 1, 3))
        self.assertEqual(second[0], ["https://example.com/c", "https://example.com/d"])
        self.assertEqual(third[0], ["https://example.com/e"])
        self.assertEqual(wrapped, first)

    def test_site_architecture_depth_and_sitemap_orphans(self) -> None:
        result = CrawlResult(start_url="https://example.com/", started_at="now")
        result.pages = [
            Page("https://example.com/", "https://example.com/", 200, "text/html", 1, 1),
            Page("https://example.com/a", "https://example.com/a", 200, "text/html", 1, 1),
            Page("https://example.com/b", "https://example.com/b", 200, "text/html", 1, 1),
            Page("https://example.com/orphan", "https://example.com/orphan", 200, "text/html", 1, 1),
        ]
        result.sitemaps = [SitemapDocument("https://example.com/sitemap.xml", 200, "urlset", ["https://example.com/", "https://example.com/orphan"])]
        edges = {
            Edge("https://example.com/", "https://example.com/a", "a.href"),
            Edge("https://example.com/a", "https://example.com/b", "a.href"),
        }
        Scanner(ScannerConfig(max_click_depth=1))._add_architecture_issues(result, edges)
        self.assertEqual([page.crawl_depth for page in result.pages], [0, 1, 2, None])
        self.assertEqual(
            {issue.rule_id for issue in result.issues},
            {"architecture.deep_page", "architecture.sitemap_orphan"},
        )

    def test_rendered_diagnostics_are_bounded_and_capture_browser_failures(self) -> None:
        config = ScannerConfig(render_enabled=True, max_rendered_pages=1, render_settle_ms=0)
        pages, setup_error = render_pages(
            ["https://example.com/a", "https://example.com/b"],
            config,
            browser_factory=FakePlaywright,
        )
        self.assertEqual(setup_error, "")
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].final_url, "https://example.com/arendered")
        self.assertEqual(pages[0].console_errors, ["Uncaught example"])
        self.assertEqual(pages[0].failed_requests[0]["status"], 404)

    def test_fetcher_only_advertises_decodable_content_encodings(self) -> None:
        fetcher = Fetcher("scanner-test", 10)
        try:
            self.assertEqual(
                fetcher.session.headers["Accept-Encoding"],
                requests.utils.default_headers()["Accept-Encoding"],
            )
            self.assertEqual(fetcher.session.headers["User-Agent"], "scanner-test")
        finally:
            fetcher.close()

    def test_url_normalization(self) -> None:
        self.assertEqual(normalize_url("https://EXAMPLE.com/a/", "../img.png#x"), "https://example.com/img.png")
        self.assertIsNone(normalize_url("https://example.com", "data:image/png,x"))
        self.assertIsNone(normalize_url("https://example.com", "http://\u200b\u200bhttps//example.com/page"))
        self.assertIsNone(normalize_url("https://example.com", "raw address with spaces"))

    def test_discovery_retains_context(self) -> None:
        pages, resources, edges = discover_html("https://example.com/", '<a href="/p"><img srcset="a.webp 1x, b.webp 2x">')
        self.assertEqual(pages, ["https://example.com/p"])
        self.assertEqual({item.url for item in resources}, {"https://example.com/a.webp", "https://example.com/b.webp"})
        self.assertIn("srcset", {edge.context for edge in edges})

    def test_mixed_content_only_applies_to_loaded_subresources(self) -> None:
        self.assertFalse(_is_browser_subresource(Edge("https://example.com", "http://other.test", "a.href")))
        self.assertFalse(_is_browser_subresource(Edge("https://example.com", "http://other.test", "link.canonical")))
        self.assertTrue(_is_browser_subresource(Edge("https://example.com", "http://other.test/image.png", "img.src")))
        self.assertTrue(_is_browser_subresource(Edge("https://example.com", "http://other.test/app.css", "link.stylesheet")))

    def test_css_discovery(self) -> None:
        found = discover_css("https://example.com/css/main.css", "@import 'theme.css';x{background:url(../a.png)}")
        self.assertEqual({item.url for item in found}, {"https://example.com/css/theme.css", "https://example.com/a.png"})

    def test_image_header_metadata(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (4000).to_bytes(4, "big") + (2200).to_bytes(4, "big")
        metadata = inspect_image(png)
        self.assertIsNotNone(metadata)
        self.assertEqual((metadata.width, metadata.height, metadata.format), (4000, 2200, "png"))
        svg = inspect_image(b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 630"></svg>')
        self.assertEqual((svg.width, svg.height, svg.format), (1200, 630, "svg"))
        avif = inspect_image(b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00avifmif1")
        self.assertEqual((avif.width, avif.height, avif.format), (None, None, "avif"))

    def test_directive_and_jsonld_parsing(self) -> None:
        signals = extract_page_signals(
            "https://example.com/page",
            '<link rel="canonical" href="/canonical"><meta name="robots" content="none, madeup"><script type="application/ld+json">{"ok": true}</script>',
            "googlebot: noarchive",
        )
        self.assertEqual(signals.canonical_url, "https://example.com/canonical")
        self.assertTrue({"none", "madeup", "noarchive"} <= set(signals.robots_directives))
        self.assertEqual(signals.invalid_robots_directives, ["madeup"])
        self.assertEqual(signals.jsonld_errors, [])

    def test_jsonld_block_types_and_structural_duplicates(self) -> None:
        signals = extract_page_signals(
            "https://example.com/",
            '''<script type="application/ld+json">{"@graph":[{"@type":"Organization"},{"@type":["WebSite","Thing"]}]}</script>
            <script type="application/ld+json"> { "@graph": [ { "@type": "Organization" }, { "@type": ["WebSite", "Thing"] } ] } </script>''',
        )
        self.assertEqual(signals.jsonld_errors, [])
        self.assertEqual(signals.jsonld_blocks[0]["types"], ["Organization", "Thing", "WebSite"])
        self.assertEqual(signals.duplicate_jsonld_blocks, [{"block_indices": [1, 2], "types": ["Organization", "Thing", "WebSite"]}])

    def test_sitemap_parser_rejects_unsupported_or_oversized_xml(self) -> None:
        unsupported = parse_sitemap("https://example.com/sitemap.xml", b"<rss></rss>")
        self.assertIn("Unsupported root element", unsupported.errors[0])
        oversized = parse_sitemap("https://example.com/sitemap.xml", b"<urlset></urlset>", max_uncompressed_bytes=4)
        self.assertIn("exceeds", oversized.errors[0])

    def test_hreflang_language_and_region_validation(self) -> None:
        self.assertTrue(valid_language_tag("en-AU"))
        self.assertTrue(valid_language_tag("zh-Hans"))
        self.assertTrue(valid_language_tag("es-419"))
        self.assertTrue(valid_language_tag("x-default"))
        self.assertFalse(valid_language_tag("zz-AU"))
        self.assertFalse(valid_language_tag("en-ZZ"))
        self.assertFalse(valid_language_tag("en_AU"))

    def test_issue_ids_are_stable_across_evidence_changes(self) -> None:
        first = Issue("link.http_error", "Broken", "error", "page", "https://example.com/x", "404", {"status": 404})
        second = Issue("link.http_error", "Broken", "error", "page", "https://example.com/x", "500", {"status": 500})
        self.assertEqual(first.issue_id, second.issue_id)

    def test_config_rejects_unknown_and_nonpositive_values(self) -> None:
        with self.assertRaises(ValueError):
            ScannerConfig.from_dict({"surprise": True})
        with self.assertRaises(ValueError):
            ScannerConfig(max_pages=0)
        with self.assertRaises(ValueError):
            ScannerConfig(max_page_bytes=100, max_page_size=101)

    def test_page_transport_rules_cover_size_speed_and_redirect_chains(self) -> None:
        response = FetchResponse(
            "https://example.com/old", "https://example.com/final", 200,
            "text/html", b"body", 500, 4000,
            ["https://example.com/old", "https://example.com/hop", "https://example.com/final"],
            False, {},
        )
        result = CrawlResult(start_url="https://example.com/", started_at="now")
        scanner = Scanner(ScannerConfig(max_page_size=100, max_page_duration_ms=100))
        scanner._add_page_response_issues(result, response.requested_url, response, extract_page_signals(response.final_url, "", ""), set())
        self.assertEqual(
            {issue.rule_id for issue in result.issues},
            {"page.oversized", "page.slow_response", "page.redirect_chain"},
        )


class IntegrationTests(unittest.TestCase):
    def test_per_page_byte_cap_is_distinct_from_total_byte_budget(self) -> None:
        with FixtureServer() as url:
            result = Scanner(ScannerConfig(max_page_bytes=32, max_page_size=32, max_total_bytes=1_000_000)).scan(url)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.coverage.limit_reason, "max_page_bytes")
        self.assertIn("page.response_truncated", {issue.rule_id for issue in result.issues})
        self.assertNotIn("crawl.limit_reached", {issue.rule_id for issue in result.issues})

    def test_complete_resource_scan_and_attribution(self) -> None:
        with FixtureServer() as url:
            result = Scanner(ScannerConfig(max_resource_size=64, min_compression_bytes=10)).scan(url)
        self.assertEqual(result.status, "complete")
        self.assertGreaterEqual(result.coverage.pages_fetched, 4)
        self.assertGreaterEqual(result.coverage.resources_fetched, 8)
        ids = {issue.rule_id for issue in result.issues}
        self.assertTrue({"resource.http_error", "resource.redirect", "resource.mime_mismatch", "resource.oversized", "resource.missing_compression", "resource.weak_cache", "resource.duplicate_payload", "link.http_error", "canonical.redirect", "canonical.chain", "canonical.loop", "directive.invalid_robots", "directive.noindex_canonical_conflict", "structured_data.invalid_jsonld", "sitemap.duplicate_url", "sitemap.invalid_lastmod", "sitemap.url_http_error", "sitemap.url_noindex", "sitemap.url_noncanonical", "hreflang.invalid_language", "hreflang.duplicate_language", "hreflang.missing_self", "hreflang.missing_return", "hreflang.target_http_error", "hreflang.target_noindex", "hreflang.target_noncanonical"} <= ids)
        self.assertEqual(result.coverage.sitemaps_fetched, 3)
        self.assertEqual(result.coverage.sitemap_urls_discovered, 3)
        broken = next(issue for issue in result.issues if issue.rule_id == "resource.http_error")
        self.assertEqual(broken.referring_urls, [url])
        self.assertTrue(any(resource.url.endswith("nested.png") for resource in result.resources))

    def test_page_cap_emits_valid_partial_result(self) -> None:
        with FixtureServer() as url:
            result = Scanner(ScannerConfig(max_pages=1)).scan(url)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.coverage.limit_reason, "max_pages")
        self.assertEqual(result.coverage.pages_queued, 2)
        self.assertEqual([issue.rule_id for issue in result.issues].count("crawl.limit_reached"), 1)
        json.dumps(result.to_dict())

    def test_resource_cap_emits_partial_result(self) -> None:
        with FixtureServer() as url:
            result = Scanner(ScannerConfig(max_resources=1)).scan(url)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.coverage.limit_reason, "max_resources")
        self.assertEqual(result.coverage.resources_fetched, 1)

    def test_total_byte_cap_cannot_claim_complete(self) -> None:
        with FixtureServer() as url:
            result = Scanner(ScannerConfig(max_total_bytes=16)).scan(url)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.coverage.limit_reason, "max_total_bytes")
        self.assertTrue(not result.pages or result.pages[0].truncated)

    def test_cli_writes_report_and_meaningful_exit_code(self) -> None:
        with FixtureServer() as url, TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            csv_output = Path(directory) / "resources.csv"
            code = main([url, "--output", str(output), "--resource-csv", str(csv_output), "--quiet"])
            report = json.loads(output.read_text(encoding="utf-8"))
            csv_text = csv_output.read_text(encoding="utf-8-sig")
        self.assertEqual(code, 1)
        self.assertEqual(report["schema_version"], "1.12")
        self.assertEqual(report["status"], "complete")
        self.assertIn("cache_control", csv_text)

    def test_cli_applies_baseline_and_ignore_file(self) -> None:
        with FixtureServer() as url, TemporaryDirectory() as directory:
            first = Path(directory) / "first.json"
            second = Path(directory) / "second.json"
            ignored = Path(directory) / "ignored.json"
            main([url, "--output", str(first), "--quiet"])
            first_report = json.loads(first.read_text(encoding="utf-8"))
            ignored_id = first_report["issues"][0]["issue_id"]
            ignored.write_text(json.dumps([ignored_id]), encoding="utf-8")
            main([url, "--output", str(second), "--baseline", str(first), "--ignore-issues", str(ignored), "--quiet"])
            second_report = json.loads(second.read_text(encoding="utf-8"))
        self.assertIn(ignored_id, second_report["comparison"]["suppressed_issue_ids"])
        self.assertGreater(len(second_report["comparison"]["persistent_issue_ids"]), 0)

    def test_optional_external_link_validation_with_get_fallback(self) -> None:
        with FixtureServer() as external_url, FixtureServer() as url:
            FixtureHandler.external_url = external_url + "external-broken"
            try:
                result = Scanner(ScannerConfig(validate_external_links=True, external_delay_seconds=0)).scan(url)
            finally:
                FixtureHandler.external_url = ""
        self.assertEqual(result.coverage.external_links_discovered, 1)
        self.assertEqual(result.coverage.external_links_checked, 1)
        issue = next(item for item in result.issues if item.rule_id == "external_link.http_error")
        self.assertEqual(issue.referring_urls, [url])

    def test_baseline_comparison_and_suppression(self) -> None:
        with FixtureServer() as url:
            result = Scanner().scan(url)
        baseline = result.to_dict()
        suppressed_id = result.issues[0].issue_id
        apply_suppressions(result, [suppressed_id])
        comparison = compare_with_baseline(result, baseline)
        self.assertIn(suppressed_id, comparison.suppressed_issue_ids)
        self.assertNotIn(suppressed_id, comparison.persistent_issue_ids)
        self.assertGreater(len(comparison.persistent_issue_ids), 0)

    def test_external_link_cap_emits_partial_coverage(self) -> None:
        with FixtureServer() as external_url, FixtureServer() as url:
            FixtureHandler.external_url = external_url + "one"
            FixtureHandler.external_url_2 = external_url + "two"
            try:
                result = Scanner(ScannerConfig(validate_external_links=True, max_external_links=1, external_delay_seconds=0)).scan(url)
            finally:
                FixtureHandler.external_url = ""
                FixtureHandler.external_url_2 = ""
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.coverage.limit_reason, "max_external_links")
        self.assertEqual(result.coverage.external_links_discovered, 2)
        self.assertEqual(result.coverage.external_links_checked, 1)


if __name__ == "__main__":
    unittest.main()
