from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tempfile import TemporaryDirectory
from pathlib import Path

from seo_scanner.cli import main
from seo_scanner.analyzers.image import inspect_image
from seo_scanner.config import ScannerConfig
from seo_scanner.discovery import discover_css, discover_html
from seo_scanner.runner import Scanner
from seo_scanner.scope import normalize_url


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        routes = {
            "/": (200, "text/html", b'''<title>Fixture</title><a href="/page-2">next</a>
                <img src="/broken.png"><img srcset="/small.webp 1x, /large.webp 2x">
                <script src="/wrong.js"></script><link rel="stylesheet" href="/style.css">''', {}),
            "/page-2": (200, "text/html", b'<img src="/redirect.png">', {}),
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
        self.assertEqual(result.coverage.pages_fetched, 2)
        self.assertGreaterEqual(result.coverage.resources_fetched, 8)
        ids = {issue.rule_id for issue in result.issues}
        self.assertTrue({"resource.http_error", "resource.redirect", "resource.mime_mismatch", "resource.oversized", "resource.missing_compression", "resource.weak_cache", "resource.duplicate_payload"} <= ids)
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
        self.assertTrue(result.pages[0].truncated)

    def test_cli_writes_report_and_meaningful_exit_code(self) -> None:
        with FixtureServer() as url, TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            csv_output = Path(directory) / "resources.csv"
            code = main([url, "--output", str(output), "--resource-csv", str(csv_output), "--quiet"])
            report = json.loads(output.read_text(encoding="utf-8"))
            csv_text = csv_output.read_text(encoding="utf-8-sig")
        self.assertEqual(code, 1)
        self.assertEqual(report["schema_version"], "1.1")
        self.assertEqual(report["status"], "complete")
        self.assertIn("cache_control", csv_text)


if __name__ == "__main__":
    unittest.main()
