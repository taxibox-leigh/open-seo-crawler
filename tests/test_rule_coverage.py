"""Rule coverage harness.

A rule sitting at zero findings is either a clean site or a broken rule, and
the report alone cannot tell those apart. Two rules — content.image_alt_missing
and link.internal_nofollow — were silently dead for exactly that reason, and
were only caught by comparing against a commercial audit.

This module runs the scanner against a fixture site built to violate as much
as a static server can, then asserts that every rule in RULES either fires or
appears in UNFIXTURED with a reason. Adding a rule without a fixture fails the
suite; so does a rule that stops firing.
"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from seo_scanner.config import ScannerConfig
from seo_scanner.rules import RULES
from seo_scanner.runner import Scanner

LONG_TEXT = "Real body copy that reads like a sentence and carries meaning. " * 30
SHARED_BODY = "<p>" + ("Identical duplicated body content across two pages. " * 40) + "</p>"


def _page(body: str, head: str = "") -> bytes:
    return f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\">{head}</head><body>{body}</body></html>".encode()


class CoverageHandler(BaseHTTPRequestHandler):
    """Serves a site that violates as many rules as a static server can."""

    def routes(self, base: str) -> dict:
        return {
            "/robots.txt": (200, "text/plain", (
                f"User-agent: *\nDisallow: /blocked\nSitemap: {base}/sitemap.xml\n"
                f"Sitemap: {base}/sitemap-broken.xml\n"     # sitemap.http_error
                f"Sitemap: {base}/sitemap-invalid.xml\n"    # sitemap.invalid_xml
                "Crawl-delay\n"  # malformed line -> robots.invalid_syntax
            ).encode(), {}),
            "/sitemap-broken.xml": (500, "application/xml", b"server error", {}),
            "/sitemap-invalid.xml": (200, "application/xml", b"<<not xml at all", {}),
            "/sitemap.xml": (200, "application/xml", (
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                f'<url><loc>{base}/</loc></url>'
                f'<url><loc>{base}/</loc></url>'                       # duplicate_url
                f'<url><loc>{base}/gone</loc></url>'                   # url_http_error
                f'<url><loc>{base}/redirected</loc></url>'             # url_redirect
                f'<url><loc>{base}/noindexed</loc><lastmod>nope</lastmod></url>'  # url_noindex + invalid_lastmod
                f'<url><loc>{base}/canonicalised</loc></url>'          # url_noncanonical
                f'<url><loc>{base}/blocked</loc></url>'                # url_blocked
                f'<url><loc>{base}/orphan</loc></url>'                 # sitemap_orphan
                f'<url><loc>{base}/sitemap-only-gone</loc></url>'      # page.http_error (unlinked)
                '</urlset>'
            ).encode(), {}),
            "/": (200, "text/html", _page(
                f"<h1>Fixture home</h1><p>{LONG_TEXT}</p>"
                '<a href="/thin">Thin page</a>'
                '<a href="/dupe-a">Duplicate A</a><a href="/dupe-b">Duplicate B</a>'
                '<a href="/Upper_Case/Upper_Case?utm_source=x&a=1&b=2&c=3&d=4">messy url</a>'
                '<a href="/malformed">malformed doc</a><a href="/depth-1">deep</a>'
                '<a href="/gone">broken link</a><a href="/redirected">redirect link</a>'
                '<a href="/noindexed">noindexed</a><a href="/canonicalised">canonicalised</a>'
                '<a href="/structured">structured data</a><a href="/encoding-late">encoding</a>'
                '<a href="/soft-404">soft 404</a><a href="/refresh">refresh</a>'
                '<a href="/mixed-target">mixed rel target</a>'
                '<a href="/mixed-target" rel="nofollow">mixed rel target again</a>'
                '<a href="/hreflang-a">hreflang</a><a href="/hub">hub</a>'
                '<a href="/"><img src="/photo-of-something.png" alt=""></a>'
                '<img src="/no-alt.png"><img src="/huge.png" alt="Huge image">'
                '<a href="/no-anchor-text"></a>',
                '<title>Fixture home page with a reasonable title</title>'
                '<meta name="description" content="A home page description that sits comfortably inside the usual length limits for a snippet.">'
                f'<link rel="canonical" href="{base}/">'
                '<meta property="og:title" content="Home"><meta property="og:description" content="Home">'
                '<meta property="og:image" content="/social.png"><meta name="twitter:card" content="summary">'
                '<link rel="stylesheet" href="/style.css"><script src="/script.js"></script>'
            ), {}),
            # Reached from home; links onward to every single-purpose fixture
            # page so they all get crawled.
            "/hub": (200, "text/html", _page(
                f"<h1>Hub</h1><p>{LONG_TEXT}</p>"
                '<a href="/canonical-multiple">a</a><a href="/canonical-redirect">b</a>'
                '<a href="/canonical-blocked">c</a><a href="/canonical-header">d</a>'
                '<a href="/canonical-chain">e</a><a href="/canonical-loop-a">f</a>'
                '<a href="/canonical-loop-b">g</a><a href="/canonical-gone">h</a>'
                '<a href="/lang-invalid">i</a><a href="/lang-missing">j</a>'
                '<a href="/lang-hreflang-conflict">k</a>'
                '<a href="/encoding-missing">l</a><a href="/encoding-invalid">m</a>'
                '<a href="/encoding-conflict">n</a><a href="/verbose">o</a>'
                '<a href="/robots-conflict">p</a><a href="/noindex-canonical">q</a>'
                '<a href="/no-structure">r</a><a href="/bad-shapes">s</a>'
                '<a href="/broken-resources">t</a>',
                "<title>A hub page linking to every fixture page</title>"
                '<meta name="description" content="A hub page whose only job is to make every other fixture page reachable from the crawl seed.">',
            ), {}),
            # Title and meta past the maximum, plus two H1s.
            "/verbose": (200, "text/html", _page(
                f"<h1>First heading</h1><h1>Second heading</h1><p>{LONG_TEXT}</p>",
                "<title>" + ("A deliberately overlong page title that runs well past sixty characters " * 2) + "</title>"
                '<meta name="description" content="' + ("A deliberately overlong meta description that runs past the configured maximum length for a snippet. " * 3) + '">',
            ), {}),
            "/robots-conflict": (200, "text/html", _page(
                f"<h1>Conflicting directives</h1><p>{LONG_TEXT}</p>",
                '<title>A page declaring contradictory robots directives</title>'
                '<meta name="robots" content="index, noindex, follow, nofollow">',
            ), {}),
            "/noindex-canonical": (200, "text/html", _page(
                f"<h1>Noindex plus canonical</h1><p>{LONG_TEXT}</p>",
                '<title>A noindex page that also canonicalises elsewhere</title>'
                '<meta name="robots" content="noindex">'
                f'<link rel="canonical" href="{base}/">',
            ), {}),
            "/canonical-chain": (200, "text/html", _page(
                f"<h1>Canonical chain</h1><p>{LONG_TEXT}</p>",
                '<title>A page whose canonical target canonicalises onward</title>'
                f'<link rel="canonical" href="{base}/canonicalised">',
            ), {}),
            "/canonical-loop-a": (200, "text/html", _page(
                f"<h1>Loop A</h1><p>{LONG_TEXT}</p>",
                '<title>The first half of a canonical loop between two pages</title>'
                f'<link rel="canonical" href="{base}/canonical-loop-b">',
            ), {}),
            "/canonical-loop-b": (200, "text/html", _page(
                f"<h1>Loop B</h1><p>{LONG_TEXT}</p>",
                '<title>The second half of a canonical loop between two pages</title>'
                f'<link rel="canonical" href="{base}/canonical-loop-a">',
            ), {}),
            "/canonical-gone": (200, "text/html", _page(
                f"<h1>Canonical to a 404</h1><p>{LONG_TEXT}</p>",
                '<title>A page whose canonical target returns an HTTP error</title>'
                f'<link rel="canonical" href="{base}/sitemap-only-gone">',
            ), {}),
            "/lang-hreflang-conflict": (200, "text/html", (
                "<!doctype html><html lang=\"de\"><head><meta charset=\"utf-8\">"
                "<title>A page whose lang attribute contradicts its own hreflang</title>"
                f'<link rel="alternate" hreflang="en-AU" href="{base}/lang-hreflang-conflict">'
                "</head><body><h1>Language conflict</h1>"
                f"<p>{LONG_TEXT}</p></body></html>"
            ).encode(), {}),
            # No <head> or <body> tags at all.
            "/no-structure": (200, "text/html", (
                "<!doctype html><title>A document with no head or body elements</title>"
                f"<h1>No structure</h1><p>{LONG_TEXT}</p>"
            ).encode(), {}),
            "/bad-shapes": (200, "text/html", _page(
                f"<h1>Bad shapes</h1><p>{LONG_TEXT}</p>",
                "<title>A page whose JSON-LD uses invalid value shapes</title>"
                '<script type="application/ld+json">{"@context":42,"@type":["Thing",7],"@id":{"not":"a string"}}</script>',
            ), {}),
            "/broken-resources": (200, "text/html", _page(
                f'<h1>Broken resources</h1><p>{LONG_TEXT}</p>'
                '<img src="/missing-image.png" alt="Missing"><img src="/empty-image.png" alt="Empty">'
                '<img src="/redirect-image.png" alt="Redirecting">'
                '<img src="/blocked/asset.png" alt="Blocked by robots">'
                '<img src="/photograph.jpg" alt="A legacy format photograph">',
                "<title>A page referencing resources that all misbehave</title>",
            ), {}),
            # Content defects: short title, no meta description, no h1, skipped
            # headings, thin body, duplicate payload target.
            "/thin": (200, "text/html", _page(
                "<h2>Sub</h2><h4>Skipped</h4><p>Tiny.</p>",
                "<title>Thin</title>",
            ), {}),
            "/dupe-a": (200, "text/html", _page(
                f"<h1>Shared heading</h1>{SHARED_BODY}",
                '<title>A duplicated title shared between two pages</title>'
                '<meta name="description" content="A duplicated meta description shared between two separate pages on the site.">',
            ), {}),
            "/dupe-b": (200, "text/html", _page(
                f"<h1>Shared heading</h1>{SHARED_BODY}",
                '<title>A duplicated title shared between two pages</title>'
                '<meta name="description" content="A duplicated meta description shared between two separate pages on the site.">',
            ), {}),
            "/Upper_Case/Upper_Case": (200, "text/html", _page(
                f"<h1>Messy URL</h1><p>{LONG_TEXT}</p>",
                "<title>A page reached through a deliberately messy URL</title>",
            ), {}),
            # Document structure: two heads, two bodies, two titles, two meta
            # descriptions, metadata outside head.
            "/malformed": (200, "text/html", (
                "<!doctype html><html lang=\"en\"><head><title>First title</title>"
                "<meta name=\"description\" content=\"First description for the malformed document fixture page.\">"
                "</head><head><title>Second title</title>"
                "<meta name=\"description\" content=\"Second description for the malformed document fixture page.\">"
                "</head><body><h1>Malformed</h1>"
                f"<p>{LONG_TEXT}</p></body><body><p>second body</p></body></html>"
            ).encode(), {}),
            "/encoding-late": (200, "text/html", (
                "<!doctype html><html lang=\"en\"><head><title>Charset declared far too late in the document</title>"
                + ("<!-- padding padding padding padding -->" * 40)
                + "<meta charset=\"utf-8\"></head><body><h1>Late charset</h1>"
                + f"<p>{LONG_TEXT}</p></body></html>"
            ).encode(), {}),
            "/structured": (200, "text/html", _page(
                f"<h1>Structured data</h1><p>{LONG_TEXT}</p>",
                "<title>A page carrying deliberately broken structured data</title>"
                '<script type="application/ld+json">{"broken":}</script>'
                '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Article","name":"Same"}</script>'
                '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Article","name":"Same"}</script>'
                '<script type="application/ld+json">{"@type":"Thing","name":"No context"}</script>'
                '<script type="application/ld+json">{"@context":"https://schema.org","@type":"WebPage","about":{"@id":"#nowhere"}}</script>'
                '<script type="application/ld+json">["not an object"]</script>',
            ), {}),
            "/soft-404": (200, "text/html", _page(
                "<h1>Page not found</h1><p>Sorry, nothing here.</p>",
                "<title>Page not found on this fixture site</title>",
            ), {}),
            "/refresh": (200, "text/html", _page(
                f"<h1>Refresh</h1><p>{LONG_TEXT}</p>",
                '<title>A page that redirects using a meta refresh tag</title>'
                '<meta http-equiv="refresh" content="5;url=/">',
            ), {"Refresh": "5; url=/"}),
            "/noindexed": (200, "text/html", _page(
                "<p>Thin noindex body.</p>",
                '<meta name="robots" content="noindex, nofollow, nonsense">'
                '<a href="/thin" rel="nofollow">nofollowed internal</a>',
            ), {}),
            "/canonicalised": (200, "text/html", _page(
                f"<h1>Canonicalised</h1><p>{LONG_TEXT}</p>",
                f'<title>A page that canonicalises to another URL entirely</title>'
                f'<link rel="canonical" href="{base}/">',
            ), {}),
            "/canonical-multiple": (200, "text/html", _page(
                f"<h1>Multiple canonicals</h1><p>{LONG_TEXT}</p>",
                '<title>A page declaring more than one canonical URL</title>'
                f'<link rel="canonical" href="{base}/"><link rel="canonical" href="{base}/thin">'
                '<link rel="canonical" href="not a url">',
            ), {}),
            "/canonical-redirect": (200, "text/html", _page(
                f"<h1>Canonical to redirect</h1><p>{LONG_TEXT}</p>",
                '<title>A page whose canonical target is a redirect</title>'
                f'<link rel="canonical" href="{base}/redirected">',
            ), {}),
            "/canonical-blocked": (200, "text/html", _page(
                f"<h1>Canonical blocked</h1><p>{LONG_TEXT}</p>",
                '<title>A page whose canonical target is blocked by robots</title>'
                f'<link rel="canonical" href="{base}/blocked">',
            ), {}),
            "/canonical-header": (200, "text/html", _page(
                f"<h1>Header conflict</h1><p>{LONG_TEXT}</p>",
                '<title>A page whose HTTP and HTML canonicals disagree</title>'
                f'<link rel="canonical" href="{base}/thin">',
            ), {"Link": f"<{base}/dupe-a>; rel=\"canonical\""}),
            "/hreflang-a": (200, "text/html", _page(
                f"<h1>Hreflang A</h1><p>{LONG_TEXT}</p>",
                '<title>The first page of a deliberately broken hreflang cluster</title>'
                f'<link rel="alternate" hreflang="english" href="{base}/hreflang-b">'
                f'<link rel="alternate" hreflang="en-AU" href="{base}/hreflang-b">'
                f'<link rel="alternate" hreflang="en-AU" href="{base}/thin">'
                f'<link rel="alternate" hreflang="fr" href="{base}/gone">'
                f'<link rel="alternate" hreflang="de" href="{base}/redirected">'
                f'<link rel="alternate" hreflang="es" href="{base}/noindexed">'
                f'<link rel="alternate" hreflang="it" href="{base}/canonicalised">'
                f'<link rel="alternate" hreflang="ja" href="{base}/blocked">',
            ), {}),
            "/hreflang-b": (200, "text/html", _page(
                f"<h1>Hreflang B</h1><p>{LONG_TEXT}</p>",
                '<title>The second page of a deliberately broken hreflang cluster</title>',
            ), {}),
            "/lang-invalid": (200, "text/html", (
                "<!doctype html><html lang=\"english\"><head><meta charset=\"utf-8\">"
                "<title>A page declaring an invalid HTML language attribute</title></head>"
                f"<body><h1>Invalid language</h1><p>{LONG_TEXT}</p></body></html>"
            ).encode(), {"Content-Language": "not a language"}),
            "/lang-missing": (200, "text/html", (
                "<!doctype html><html><head><meta charset=\"utf-8\">"
                "<title>A page with no HTML language declaration at all</title></head>"
                f"<body><h1>No language</h1><p>{LONG_TEXT}</p></body></html>"
            ).encode(), {}),
            "/encoding-missing": (200, "text/html", (
                "<!doctype html><html lang=\"en\"><head>"
                "<title>A page that declares no character encoding anywhere</title></head>"
                f"<body><h1>No encoding</h1><p>{LONG_TEXT}</p></body></html>"
            ).encode(), {"Content-Type": "text/html"}),
            "/encoding-invalid": (200, "text/html", (
                "<!doctype html><html lang=\"en\"><head><meta charset=\"not-a-charset\">"
                "<title>A page declaring a character encoding that does not exist</title></head>"
                f"<body><h1>Bad encoding</h1><p>{LONG_TEXT}</p></body></html>"
            ).encode(), {"Content-Type": "text/html"}),
            "/encoding-conflict": (200, "text/html", (
                "<!doctype html><html lang=\"en\"><head><meta charset=\"iso-8859-1\">"
                "<title>A page whose HTTP and HTML encodings disagree</title></head>"
                f"<body><h1>Conflicting encoding</h1><p>{LONG_TEXT}</p></body></html>"
            ).encode(), {"Content-Type": "text/html; charset=utf-8"}),
            "/depth-1": (200, "text/html", _page(f'<h1>One</h1><p>{LONG_TEXT}</p><a href="/depth-2">two</a>', "<title>The first page in a deliberately deep chain</title>"), {}),
            "/depth-2": (200, "text/html", _page(f'<h1>Two</h1><p>{LONG_TEXT}</p><a href="/depth-3">three</a>', "<title>The second page in a deliberately deep chain</title>"), {}),
            "/depth-3": (200, "text/html", _page(f'<h1>Three</h1><p>{LONG_TEXT}</p><a href="/depth-4">four</a>', "<title>The third page in a deliberately deep chain</title>"), {}),
            "/depth-4": (200, "text/html", _page(f"<h1>Four</h1><p>{LONG_TEXT}</p>", "<title>The fourth page in a deliberately deep chain</title>"), {}),
            "/mixed-target": (200, "text/html", _page(f"<h1>Mixed</h1><p>{LONG_TEXT}</p>", "<title>A page linked with both dofollow and nofollow links</title>"), {}),
            "/no-anchor-text": (200, "text/html", _page(f"<h1>No anchor</h1><p>{LONG_TEXT}</p>", "<title>A page reached only through an empty anchor</title>"), {}),
            "/orphan": (200, "text/html", _page(f"<h1>Orphan</h1><p>{LONG_TEXT}</p>", "<title>A sitemap page with no incoming internal links</title>"), {}),
            "/redirected": (302, "text/plain", b"", {"Location": "/redirect-hop"}),
            "/redirect-hop": (302, "text/plain", b"", {"Location": "/"}),
            "/gone": (404, "text/html", b"<title>Gone</title>", {}),
            "/blocked": (200, "text/html", _page("<h1>Blocked</h1>", "<title>Blocked</title>"), {}),
            # Resources.
            "/style.css": (200, "text/css", (b"body{background:url('/nested.png')}" + b"/* padding */" * 2000), {}),
            "/script.js": (200, "text/html", b"<html>not javascript</html>", {}),
            "/nested.png": (200, "image/png", b"png-bytes", {}),
            "/missing-image.png": (404, "image/png", b"gone", {}),
            "/empty-image.png": (200, "image/png", b"", {}),
            "/redirect-image.png": (302, "text/plain", b"", {"Location": "/nested.png"}),
            "/blocked/asset.png": (200, "image/png", b"blocked-bytes", {}),
            "/photograph.jpg": (200, "image/jpeg", _legacy_jpeg(), {}),
            "/social.png": (200, "image/png", b"social-bytes", {}),
            "/no-alt.png": (200, "image/png", b"png-bytes", {}),
            "/photo-of-something.png": (200, "image/png", b"png-bytes", {}),
            "/huge.png": (200, "image/png", _huge_png(), {}),
        }

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        base = f"http://{self.headers['Host']}"
        path = self.path.split("?", 1)[0]
        status, mime, body, headers = self.routes(base).get(path, (404, "text/plain", b"not found", {}))
        self.send_response(status)
        if not any(key.lower() == "content-type" for key in headers):
            self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        pass


def _legacy_jpeg(width: int = 1200, height: int = 800, padding: int = 6000) -> bytes:
    """A JPEG header the analyzer can read, padded past the legacy-format threshold."""
    import struct
    sof = (bytes([0xFF, 0xC0]) + struct.pack(">H", 11) + bytes([8])
           + struct.pack(">HH", height, width) + bytes([1, 1, 0x11, 0]))
    return bytes([0xFF, 0xD8]) + sof + bytes([0xFF, 0xDA]) + bytes(padding) + bytes([0xFF, 0xD9])


def _huge_png() -> bytes:
    """A PNG header declaring dimensions past the configured threshold."""
    import struct
    ihdr = struct.pack(">II", 8000, 6000) + b"\x08\x02\x00\x00\x00"
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr
            + b"\x00\x00\x00\x00" + b"\x00" * 64)


# Rules this fixture cannot reach, with the reason. Anything here is untested
# by the harness — shrink this list, do not grow it casually.
UNFIXTURED = {
    # Require a transport-level failure a static fixture server cannot stage.
    "resource.fetch_failed": "needs a connection failure",
    "resource.timeout": "needs a hanging server",
    "resource.tls_error": "needs a broken certificate",
    "resource.redirect_loop": "needs a resource redirect cycle",
    "resource.response_truncated": "needs a response past the byte limit",
    "page.fetch_failed": "needs a connection failure",
    "page.timeout": "needs a hanging server",
    "page.tls_error": "needs a broken certificate",
    "page.redirect_loop": "needs a page redirect cycle",
    "page.response_truncated": "needs a response past the byte limit",
    "page.slow_response": "needs a slow server; timing-dependent",
    "sitemap.fetch_failed": "needs a connection failure",
    "external_link.fetch_failed": "needs an unreachable external host",
    "external_link.http_error": "needs a failing external host",
    "external_link.redirect": "needs a redirecting external host",
    "robots.unavailable": "needs robots.txt to fail at transport level",
    # Need HTTPS, which the fixture server does not serve.
    "resource.mixed_content": "needs an HTTPS page referencing HTTP",
    "link.insecure_internal": "needs an HTTPS page linking to HTTP",
    # Need limits far below anything worth serving in a fixture.
    "crawl.limit_reached": "covered by the limits test",
    "robots.byte_limit": "needs a robots.txt past the byte limit",
    "sitemap.url_limit": "needs 50,000 sitemap URLs",
    "sitemap.byte_limit": "needs a sitemap past the byte limit",
    "sitemap.recursion_limit": "needs deeply nested sitemap indexes",
    "resource.oversized": "needs a resource past the size threshold",
    "page.oversized": "needs an HTML page past the size threshold",
    "accessibility.inventory_truncated": "needs a browser and many violations",
    "render.network_inventory_truncated": "needs a browser",
    # Browser-only; covered by the rendered-diagnostics tests with a fake page.
    "render.unavailable": "covered by the rendered diagnostics tests",
    "render.navigation_failed": "covered by the rendered diagnostics tests",
    "render.failed_requests": "covered by the rendered diagnostics tests",
    "render.console_errors": "covered by the rendered diagnostics tests",
    "render.excessive_requests": "covered by the rendered diagnostics tests",
    "render.excessive_transfer": "covered by the rendered diagnostics tests",
    "render.seo_signals_unavailable": "covered by the rendered diagnostics tests",
    "render.title_changed": "covered by the rendered diagnostics tests",
    "render.meta_description_changed": "covered by the rendered diagnostics tests",
    "render.canonical_changed": "covered by the rendered diagnostics tests",
    "render.robots_changed": "covered by the rendered diagnostics tests",
    "render.h1_changed": "covered by the rendered diagnostics tests",
    "render.language_changed": "covered by the rendered diagnostics tests",
    "accessibility.violations": "covered by the rendered diagnostics tests",
    "accessibility.critical_violations": "covered by the rendered diagnostics tests",
    "accessibility.unavailable": "covered by the rendered diagnostics tests",
}


class RuleCoverageTest(unittest.TestCase):
    fired: dict[str, int] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), CoverageHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        base = f"http://127.0.0.1:{cls.server.server_port}/"
        config = ScannerConfig(
            max_pages=100, max_resources=100, max_click_depth=2,
            min_internal_inlinks=2, min_site_pages_for_link_metrics=1,
            min_content_words=100, min_title_chars=15, max_title_chars=60,
            min_meta_description_chars=70, max_meta_description_chars=160,
            max_url_chars=40, max_query_parameters=3,
            max_image_width=2000, max_image_height=2000,
            min_cache_seconds=604800, min_compression_bytes=1000,
            min_legacy_image_bytes=1000, min_responsive_image_width=500,
            render_enabled=False, accessibility_enabled=False,
        )
        cls.result = Scanner(config).scan(base)
        cls.fired = {rule_id: count for rule_id, count in cls.result.rule_coverage.items() if count}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_rule_coverage_block_matches_the_issue_list(self) -> None:
        self.assertEqual(set(self.result.rule_coverage), set(RULES))
        recounted: dict[str, int] = {}
        for issue in self.result.issues:
            recounted[issue.rule_id] = recounted.get(issue.rule_id, 0) + 1
        self.assertEqual(self.fired, recounted)

    def test_every_rule_is_fixtured_or_explicitly_excused(self) -> None:
        missing = sorted(set(RULES) - set(self.fired) - set(UNFIXTURED))
        self.assertEqual(missing, [], msg=(
            "These rules produced no finding against the coverage fixture. "
            "Either extend the fixture to trigger them or add them to "
            "UNFIXTURED with a reason:\n  " + "\n  ".join(missing)
        ))

    def test_excused_rules_are_still_real_rules(self) -> None:
        """UNFIXTURED must not rot after a rule is renamed or removed."""
        stale = sorted(set(UNFIXTURED) - set(RULES))
        self.assertEqual(stale, [], msg="UNFIXTURED names rules that no longer exist")

    def test_excused_rules_are_not_actually_firing(self) -> None:
        """If an excused rule starts firing, the excuse is obsolete."""
        excused_but_firing = sorted(set(UNFIXTURED) & set(self.fired))
        self.assertEqual(excused_but_firing, [], msg="Remove these from UNFIXTURED — the fixture reaches them")

    def test_findings_carry_evidence_and_remediation(self) -> None:
        for issue in self.result.issues:
            self.assertTrue(issue.title, msg=issue.rule_id)
            self.assertTrue(issue.remediation, msg=issue.rule_id)
            self.assertTrue(issue.message, msg=issue.rule_id)
            self.assertIn(issue.severity, ("error", "warning", "info"), msg=issue.rule_id)
            json.dumps(issue.evidence)  # evidence must survive serialization


if __name__ == "__main__":
    unittest.main()
