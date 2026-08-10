from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tempfile import TemporaryDirectory
from pathlib import Path

from seo_scanner.cli import main
from seo_scanner.analyzers.image import inspect_image
from seo_scanner.analyzers.directives import extract_page_signals
from seo_scanner.analyzers.sitemap import parse_sitemap
from seo_scanner.analyzers.hreflang import valid_language_tag
from seo_scanner.config import ScannerConfig
from seo_scanner.discovery import discover_css, discover_html
from seo_scanner.runner import Scanner
from seo_scanner.scope import normalize_url


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
    def test_url_normalization(self) -> None:
        self.assertEqual(normalize_url("https://EXAMPLE.com/a/", "../img.png#x"), "https://example.com/img.png")
        self.assertIsNone(normalize_url("https://example.com", "data:image/png,x"))

    def test_discovery_retains_context(self) -> None:
        pages, resources, edges = discover_html("https://example.com/", '<a href="/p"><img srcset="a.webp 1x, b.webp 2x">')
        self.assertEqual(pages, ["https://example.com/p"])
        self.assertEqual({item.url for item in resources}, {"https://example.com/a.webp", "https://example.com/b.webp"})
        self.assertIn("srcset", {edge.context for edge in edges})

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

    def test_config_rejects_unknown_and_nonpositive_values(self) -> None:
        with self.assertRaises(ValueError):
            ScannerConfig.from_dict({"surprise": True})
        with self.assertRaises(ValueError):
            ScannerConfig(max_pages=0)


class IntegrationTests(unittest.TestCase):
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
        self.assertEqual(report["schema_version"], "1.5")
        self.assertEqual(report["status"], "complete")
        self.assertIn("cache_control", csv_text)

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
