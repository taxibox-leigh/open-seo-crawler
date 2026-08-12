from __future__ import annotations

import json
import threading
import unittest
import requests
from collections import deque
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tempfile import TemporaryDirectory
from pathlib import Path

from seo_scanner.cli import main
from seo_scanner.analyzers.image import inspect_image
from seo_scanner.analyzers.content import extract_page_content
from seo_scanner.analyzers.directives import extract_page_signals
from seo_scanner.analyzers.sitemap import parse_sitemap
from seo_scanner.analyzers.hreflang import valid_language_tag
from seo_scanner.analyzers.encoding import EncodingSignals, analyze_encoding
from seo_scanner.analyzers.document import DocumentSignals, analyze_document
from seo_scanner.analyzers.link_header import parse_link_header
from seo_scanner.analyzers.robots import parse_robots
from seo_scanner.analyzers.url_quality import analyze_url
from seo_scanner.baseline import apply_suppressions, compare_with_baseline
from seo_scanner.config import ScannerConfig
from seo_scanner.discovery import discover_css, discover_html
from seo_scanner.fetch import Fetcher, FetchResponse
from seo_scanner.runner import Scanner, _RunState, _is_browser_subresource
from seo_scanner.scope import normalize_url
from seo_scanner.models import CrawlResult, Edge, ImageReference, Issue, LinkReference, Page, RenderedPage, Resource, SitemapDocument
from seo_scanner.render import render_pages, select_render_urls


class FakeMessage:
    type = "error"
    text = "Uncaught example"


class FakeResponse:
    url = "https://example.com/missing.js"
    status = 404
    request = None

    def __init__(self) -> None:
        self.request = self

    resource_type = "script"

    def sizes(self) -> dict[str, int]:
        return {"responseBodySize": 120, "responseHeadersSize": 30}


class FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.handlers = {}

    def on(self, event, handler) -> None:
        self.handlers[event] = handler

    def goto(self, url, **_) -> None:
        self.url = url + "rendered"
        self.handlers["console"](FakeMessage())
        response = FakeResponse()
        self.handlers["request"](response.request)
        self.handlers["response"](response)

    def wait_for_timeout(self, _) -> None:
        pass

    def add_script_tag(self, **_) -> None:
        pass

    def evaluate(self, script, *_):
        if "axe.run" in script:
            return {"total": 2, "violations": [
                {"id": "button-name", "impact": "critical", "nodes": [{"target": ["button"]}], "nodes_total": 1},
                {"id": "landmark-one-main", "impact": "moderate", "nodes": [{"target": ["body"]}], "nodes_total": 2},
            ]}
        return {
            "title": "Rendered title", "meta_description": "Rendered description",
            "canonical_url": "https://example.com/rendered", "robots_directives": ["index"],
            "h1s": ["Rendered heading"], "html_language": "en-AU",
        }

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
            "/": (200, "text/html", f'''<title>Fixture</title><meta property="og:image" content="/social.png"><link rel="canonical" href="/"><link rel="alternate" hreflang="en-AU" href="/"><link rel="alternate" hreflang="en-AU" href="/page-2"><link rel="alternate" hreflang="en_AU" href="/missing-page"><a href="/page-2">next</a><a href="/missing-page">missing</a>
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
            "/social.png": (200, "image/png", b"social", {}),
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
    def test_url_quality_detects_conservative_hygiene_and_trap_signals(self) -> None:
        url = "https://example.com/Foo_Bar/Foo_Bar?utm_source=test&a=1"
        quality = analyze_url(url)
        self.assertTrue(quality.uppercase_path)
        self.assertTrue(quality.underscore_path)
        self.assertEqual(quality.repeated_segments, ["Foo_Bar"])
        self.assertEqual(quality.tracking_parameters, ["utm_source"])
        self.assertEqual(quality.query_parameter_count, 2)
        result = CrawlResult(start_url="https://example.com/", started_at="now")
        Scanner(ScannerConfig(max_url_chars=10, max_query_parameters=1))._add_url_quality_issues(result, url, quality, set())
        self.assertEqual(
            {issue.rule_id for issue in result.issues},
            {"url.too_long", "url.uppercase_path", "url.underscore_path", "url.excessive_parameters", "url.tracking_parameters", "url.repeated_segments"},
        )

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

    def test_robots_findings_cover_declared_blocked_targets(self) -> None:
        result = CrawlResult(start_url="https://example.com/", started_at="now")
        result.sitemaps = [SitemapDocument("https://example.com/sitemap.xml", 200, "urlset", ["https://example.com/private/sitemap-page"])]
        edges = {
            Edge("https://example.com/source", "https://example.com/private/canonical", "link.canonical"),
            Edge("https://example.com/source", "https://example.com/private/regional", "link.hreflang:en-AU"),
            Edge("https://example.com/source", "https://example.com/public", "link.canonical"),
        }
        policy = parse_robots("https://example.com/robots.txt", b"User-agent: *\nDisallow: /private/", "Googlebot")
        Scanner()._add_robots_issues(result, edges, policy)
        self.assertEqual(
            {issue.rule_id for issue in result.issues},
            {"canonical.blocked_target", "hreflang.target_blocked", "sitemap.url_blocked"},
        )
        canonical = next(issue for issue in result.issues if issue.rule_id == "canonical.blocked_target")
        self.assertEqual(canonical.evidence["target_url"], "https://example.com/private/canonical")

    def test_page_content_extracts_metadata_headings_and_visible_words(self) -> None:
        content = extract_page_content('''<title> Example title </title><meta NAME="Description" content="A useful summary">
            <meta name="viewport" content="width=device-width"><meta property="og:title" content="Shared title">
            <meta property="og:image" content="/share.webp"><meta name="twitter:card" content="summary_large_image">
            <h1>Main <span>heading</span></h1><p>Three visible words.</p><img src="/decorative.svg" alt=""><img src="/missing.webp"><script>ignored script words</script>''', "https://example.com/page")
        self.assertEqual(content.title, "Example title")
        self.assertEqual(content.meta_description, "A useful summary")
        self.assertEqual(content.h1s, ["Main heading"])
        self.assertEqual(content.word_count, 7)
        self.assertTrue(content.viewport)
        self.assertEqual(content.og_image, "https://example.com/share.webp")
        self.assertEqual(content.twitter_card, "summary_large_image")
        self.assertEqual([(image.url, image.alt) for image in content.images], [("https://example.com/decorative.svg", ""), ("https://example.com/missing.webp", None)])

    def test_content_rules_cover_missing_long_duplicate_and_thin_pages(self) -> None:
        result = CrawlResult(start_url="https://example.com/", started_at="now")
        result.pages = [
            Page("https://example.com/", "https://example.com/", 200, "text/html", 1, 1, title="Shared title", meta_description="Shared description", h1s=["One", "Two"], word_count=2, images=[ImageReference("https://example.com/missing.webp", None), ImageReference("https://example.com/decorative.svg", "")]),
            Page("https://example.com/b", "https://example.com/b", 200, "text/html", 1, 1, title="Shared title", meta_description="Shared description", word_count=0),
        ]
        scanner = Scanner(ScannerConfig(min_title_chars=1, max_title_chars=5, min_meta_description_chars=1, max_meta_description_chars=5, min_content_words=3))
        scanner._add_content_issues(result, set())
        rules = [issue.rule_id for issue in result.issues]
        self.assertEqual(rules.count("content.duplicate_title"), 2)
        self.assertEqual(rules.count("content.duplicate_meta_description"), 2)
        self.assertIn("content.multiple_h1", rules)
        self.assertIn("content.h1_missing", rules)
        self.assertEqual(rules.count("content.thin"), 2)
        self.assertEqual(rules.count("content.image_alt_missing"), 1)

    def test_empty_alt_is_split_into_decorative_and_content(self) -> None:
        """alt="" is correct markup for decoration and a defect on content imagery."""
        html = (
            '<img src="/photo-of-the-team.jpg" alt="">'                      # content: flag
            '<img src="/icon-arrow.svg" alt="">'                             # decorative filename
            '<img src="/shape.png" alt="" role="presentation">'              # explicit role
            '<img src="/thing.jpg" alt="" aria-hidden="true">'               # explicitly hidden
            '<a href="/x" aria-label="Read the guide"><img src="/cover.jpg" alt=""></a>'  # parent named
            '<img src="https://www.googletagmanager.com/beacon.gif" alt="">'  # third party
            '<img src="/described.jpg" alt="A team member packing a box">'    # fine
            '<img src="/forgotten.jpg">'                                      # missing, not empty
        )
        content = extract_page_content(html, "https://example.com/page")
        states = {image.url.rsplit("/", 1)[-1]: image.alt_state for image in content.images}
        self.assertEqual(states["photo-of-the-team.jpg"], "empty_content")
        self.assertEqual(states["icon-arrow.svg"], "empty_decorative")
        self.assertEqual(states["shape.png"], "empty_decorative")
        self.assertEqual(states["thing.jpg"], "empty_decorative")
        self.assertEqual(states["cover.jpg"], "empty_decorative")
        self.assertEqual(states["beacon.gif"], "empty_decorative")
        self.assertEqual(states["described.jpg"], "present")
        self.assertEqual(states["forgotten.jpg"], "missing")

        result = CrawlResult(start_url="https://example.com/", started_at="now")
        result.pages = [Page("https://example.com/page", "https://example.com/page", 200, "text/html", 1, 1,
                             title="A title of reasonable length", meta_description="A description of reasonable length for the page.",
                             h1s=["Heading"], word_count=500, images=content.images)]
        scanner = Scanner(ScannerConfig(min_title_chars=1, max_title_chars=80, min_meta_description_chars=1, max_meta_description_chars=200, min_content_words=3))
        scanner._add_content_issues(result, set())
        empty = [issue for issue in result.issues if issue.rule_id == "content.image_alt_empty"]
        self.assertEqual(len(empty), 1)
        self.assertEqual(empty[0].evidence["images"], ["https://example.com/photo-of-the-team.jpg"])
        # The genuinely-missing alt still belongs to the original rule.
        missing = [issue for issue in result.issues if issue.rule_id == "content.image_alt_missing"]
        self.assertEqual(len(missing), 1)

    def test_decorative_only_page_raises_no_empty_alt_issue(self) -> None:
        html = '<img src="/spacer.gif" alt=""><img src="/pattern-dots.svg" alt="">'
        content = extract_page_content(html, "https://example.com/page")
        result = CrawlResult(start_url="https://example.com/", started_at="now")
        result.pages = [Page("https://example.com/page", "https://example.com/page", 200, "text/html", 1, 1,
                             title="A title of reasonable length", meta_description="A description of reasonable length for the page.",
                             h1s=["Heading"], word_count=500, images=content.images)]
        scanner = Scanner(ScannerConfig(min_title_chars=1, max_title_chars=80, min_meta_description_chars=1, max_meta_description_chars=200, min_content_words=3))
        scanner._add_content_issues(result, set())
        self.assertNotIn("content.image_alt_empty", {issue.rule_id for issue in result.issues})

    def test_content_rules_also_cover_non_indexable_pages(self) -> None:
        """Noindex pages have real content defects; they were silently skipped."""
        result = CrawlResult(start_url="https://example.com/", started_at="now")
        result.pages = [
            Page("https://example.com/", "https://example.com/", 200, "text/html", 1, 1,
                 title="A perfectly reasonable title", meta_description="A description of a sensible length for search.", h1s=["One"], word_count=500),
            Page("https://example.com/private", "https://example.com/private", 200, "text/html", 1, 1,
                 robots_directives=["noindex"], word_count=500),
        ]
        scanner = Scanner(ScannerConfig(min_title_chars=1, max_title_chars=80, min_meta_description_chars=1, max_meta_description_chars=200, min_content_words=3))
        scanner._add_content_issues(result, set())

        noindex_issues = [issue for issue in result.issues if issue.url.endswith("/private")]
        rules = {issue.rule_id for issue in noindex_issues}
        self.assertIn("content.title_missing", rules)
        self.assertIn("content.meta_description_missing", rules)
        self.assertIn("content.h1_missing", rules)

        # Tagged as non-indexable and dropped one severity level.
        title_issue = next(issue for issue in noindex_issues if issue.rule_id == "content.title_missing")
        self.assertFalse(title_issue.evidence["indexable"])
        self.assertEqual(title_issue.severity, "warning")  # rule is error
        # The indexable page keeps its natural severity and tag.
        og_issue = next(issue for issue in result.issues if issue.rule_id == "social.og_image_missing" and not issue.url.endswith("/private"))
        self.assertTrue(og_issue.evidence["indexable"])
        self.assertEqual(og_issue.severity, "warning")

        # canonical.missing stays indexable-only: a noindex page needs no canonical.
        self.assertNotIn("canonical.missing", rules)

    def test_internal_nofollow_is_reported_on_a_noindex_page(self) -> None:
        """The only page on the audited site with internal nofollow links was
        noindex, so the rule silently never fired."""
        result = CrawlResult(start_url="https://example.com/", started_at="now")
        result.pages = [
            Page("https://example.com/hub", "https://example.com/hub", 200, "text/html", 1, 1,
                 robots_directives=["noindex", "nofollow"], word_count=500,
                 links=[LinkReference("https://example.com/a", "A", True),
                        LinkReference("https://example.com/b", "B", True)]),
        ]
        scanner = Scanner(ScannerConfig(min_title_chars=1, max_title_chars=80, min_meta_description_chars=1, max_meta_description_chars=200, min_content_words=3))
        scanner._add_content_issues(result, set())
        nofollow = [issue for issue in result.issues if issue.rule_id == "link.internal_nofollow"]
        self.assertEqual(len(nofollow), 1)
        self.assertEqual(nofollow[0].evidence["links"], ["https://example.com/a", "https://example.com/b"])
        self.assertFalse(nofollow[0].evidence["indexable"])

    def test_sitemap_coverage_skips_pages_nobody_lists(self) -> None:
        """The inverse sitemap rule, with the exclusions that keep it honest."""
        result = CrawlResult(start_url="https://example.com/", started_at="now")
        result.sitemaps = [SitemapDocument(url="https://example.com/sitemap.xml", kind="urlset", status=200,
                                           urls=["https://example.com/"])]
        listed = Page("https://example.com/", "https://example.com/", 200, "text/html", 1, 1)
        missing = Page("https://example.com/real-page", "https://example.com/real-page", 200, "text/html", 1, 1)
        paginated = Page("https://example.com/blog/page/2/", "https://example.com/blog/page/2/", 200, "text/html", 1, 1)
        parameterised = Page("https://example.com/booking?service=mss", "https://example.com/booking?service=mss", 200, "text/html", 1, 1)
        document = Page("https://example.com/checklist.pdf", "https://example.com/checklist.pdf", 200, "text/html", 1, 1)
        noindexed = Page("https://example.com/private", "https://example.com/private", 200, "text/html", 1, 1, robots_directives=["noindex"])
        result.pages = [listed, missing, paginated, parameterised, document, noindexed]
        Scanner()._add_sitemap_issues(result, set())
        flagged = {issue.url for issue in result.issues if issue.rule_id == "sitemap.page_missing"}
        self.assertEqual(flagged, {"https://example.com/real-page"})

    def test_sitemap_coverage_is_silent_without_a_sitemap(self) -> None:
        result = CrawlResult(start_url="https://example.com/", started_at="now")
        result.pages = [Page("https://example.com/a", "https://example.com/a", 200, "text/html", 1, 1)]
        Scanner()._add_sitemap_issues(result, set())
        self.assertNotIn("sitemap.page_missing", {issue.rule_id for issue in result.issues})

    def test_og_url_mismatch_is_reported_against_the_canonical(self) -> None:
        result = CrawlResult(start_url="https://example.com/", started_at="now")
        result.pages = [
            Page("https://example.com/a", "https://example.com/a", 200, "text/html", 1, 1,
                 title="A title of reasonable length", meta_description="A description of reasonable length for the page.",
                 h1s=["Heading"], word_count=500, og_url="https://example.com/somewhere-else"),
            # Trailing-slash-only difference must not be reported.
            Page("https://example.com/b/", "https://example.com/b/", 200, "text/html", 1, 1,
                 title="Another title of reasonable length", meta_description="Another description of reasonable length here.",
                 h1s=["Heading"], word_count=500, og_url="https://example.com/b"),
        ]
        scanner = Scanner(ScannerConfig(min_title_chars=1, max_title_chars=80, min_meta_description_chars=1, max_meta_description_chars=200, min_content_words=3))
        scanner._add_content_issues(result, set())
        flagged = {issue.url for issue in result.issues if issue.rule_id == "social.og_url_canonical_mismatch"}
        self.assertEqual(flagged, {"https://example.com/a"})

    def test_pages_linking_to_redirects_are_counted_by_source_page(self) -> None:
        """Ahrefs counts the pages that need editing; we counted destinations."""
        result = CrawlResult(start_url="https://example.com/", started_at="now")
        result.pages = [
            Page("https://example.com/", "https://example.com/", 200, "text/html", 1, 1,
                 links=[LinkReference("https://example.com/old", "Old", False),
                        LinkReference("https://example.com/fine", "Fine", False)]),
            Page("https://example.com/old", "https://example.com/new", 200, "text/html", 1, 1,
                 redirect_hops=["https://example.com/old", "https://example.com/new"]),
            Page("https://example.com/fine", "https://example.com/fine", 200, "text/html", 1, 1),
        ]
        Scanner(ScannerConfig(min_site_pages_for_link_metrics=99))._add_architecture_issues(result, set())
        sources = [issue for issue in result.issues if issue.rule_id == "link.redirect_source"]
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].url, "https://example.com/")
        self.assertEqual(sources[0].evidence["links"], ["https://example.com/old"])

    def test_inlink_rules_count_dofollow_and_flag_mixed_rel(self) -> None:
        result = CrawlResult(start_url="https://example.com/", started_at="now")
        # /target gets one dofollow link and one nofollow link; /lonely gets one
        # dofollow link only. Sources include a noindex page, whose followed
        # links still pass equity.
        result.pages = [
            Page("https://example.com/", "https://example.com/", 200, "text/html", 1, 1,
                 links=[LinkReference("https://example.com/target", "Target", False),
                        LinkReference("https://example.com/lonely", "Lonely", False)]),
            Page("https://example.com/noindexed", "https://example.com/noindexed", 200, "text/html", 1, 1,
                 robots_directives=["noindex"],
                 links=[LinkReference("https://example.com/target", "Target", True)]),
            Page("https://example.com/target", "https://example.com/target", 200, "text/html", 1, 1,
                 links=[LinkReference("https://example.com/", "Home", False)]),
            Page("https://example.com/lonely", "https://example.com/lonely", 200, "text/html", 1, 1,
                 links=[LinkReference("https://example.com/", "Home", False)]),
        ]
        scanner = Scanner(ScannerConfig(min_site_pages_for_link_metrics=1, min_internal_inlinks=1))
        scanner._add_architecture_issues(result, set())
        by_url = {}
        for issue in result.issues:
            by_url.setdefault(issue.url, set()).add(issue.rule_id)
        self.assertIn("architecture.single_dofollow_inlink", by_url["https://example.com/target"])
        self.assertIn("architecture.mixed_rel_inlinks", by_url["https://example.com/target"])
        self.assertIn("architecture.single_dofollow_inlink", by_url["https://example.com/lonely"])
        # One dofollow source only — no mixed-rel finding.
        self.assertNotIn("architecture.mixed_rel_inlinks", by_url["https://example.com/lonely"])

    def test_internal_link_and_page_semantics(self) -> None:
        content = extract_page_content(
            '<h1>Primary</h1><h3>Skipped</h3><a href="http://example.com/target" rel="nofollow"><img alt=""></a>',
            "https://example.com/page",
        )
        self.assertEqual(content.heading_levels, [1, 3])
        self.assertEqual(content.links, [LinkReference("http://example.com/target", "", True)])

        pages = [
            Page(
                "https://example.com/missing", "https://example.com/missing", 200, "text/html", 1, 1,
                title="404", meta_description="Short", h1s=["Page not found"], word_count=5,
                heading_levels=[1, 3], links=content.links,
            ),
            Page("https://example.com/other", "https://example.com/other", 200, "text/html", 1, 1, title="Another useful page title", meta_description="A sufficiently descriptive summary for another useful page on the site.", h1s=["Page not found"], word_count=200),
        ]
        result = CrawlResult(start_url="https://example.com/", started_at="now", pages=pages)
        Scanner()._add_content_issues(result, set())
        rules = [issue.rule_id for issue in result.issues]
        for rule_id in (
            "content.title_too_short", "content.meta_description_too_short",
            "content.heading_order_skipped", "content.duplicate_h1", "link.text_missing",
            "link.internal_nofollow", "link.insecure_internal", "page.soft_404",
        ):
            self.assertIn(rule_id, rules)

        architecture = CrawlResult(start_url="https://example.com/", started_at="now", pages=[
            Page(
                f"https://example.com/{index}", f"https://example.com/{index}", 200, "text/html", 1, 1,
                links=[LinkReference("https://example.com/1", "One")] if index == 0 else [],
            )
            for index in range(5)
        ])
        architecture.start_url = "https://example.com/0"
        edges = {Edge("https://example.com/0", "https://example.com/1", "a.href")}
        Scanner()._add_architecture_issues(architecture, edges)
        self.assertIn("architecture.low_inlinks", {issue.rule_id for issue in architecture.issues})
        self.assertIn("architecture.no_outlinks", {issue.rule_id for issue in architecture.issues})

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
        self.assertEqual(pages[0].request_count, 1)
        self.assertEqual(pages[0].transfer_bytes, 150)
        self.assertEqual(pages[0].network_requests[0]["resource_type"], "script")
        self.assertEqual(pages[0].title, "Rendered title")
        self.assertEqual(pages[0].canonical_url, "https://example.com/rendered")

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

    def test_bounded_retries_and_specific_fetch_failures(self) -> None:
        transient = FetchResponse("https://example.com/", "https://example.com/", 503, "text/plain", b"busy", 4, 1, [], False, {})
        recovered = FetchResponse("https://example.com/", "https://example.com/", 200, "text/html", b"<html><head></head><body>ok</body></html>", 40, 1, [], False, {})

        class SequenceFetcher:
            def __init__(self, values):
                self.values = list(values)

            def get(self, *_):
                value = self.values.pop(0)
                if isinstance(value, Exception):
                    raise value
                return value

        result = CrawlResult(start_url="https://example.com/", started_at="now")
        state = _RunState(result.start_url, result, float("inf"), deque(), set())
        scanner = Scanner(ScannerConfig(max_fetch_attempts=3, retry_backoff_seconds=0, max_retry_after_seconds=0))
        scanner._fetch_page(SequenceFetcher([transient, recovered]), state, result.start_url)
        self.assertEqual(result.pages[0].fetch_attempts, 2)
        self.assertEqual(result.coverage.bytes_downloaded, len(transient.body) + len(recovered.body))
        self.assertNotIn("page.http_error", {issue.rule_id for issue in result.issues})

        failed = CrawlResult(start_url="https://example.com/", started_at="now")
        failed_state = _RunState(failed.start_url, failed, float("inf"), deque(), set())
        scanner._fetch_page(SequenceFetcher([requests.exceptions.SSLError("certificate failed")]), failed_state, failed.start_url)
        self.assertEqual([issue.rule_id for issue in failed.issues], ["page.tls_error"])
        self.assertEqual(failed.issues[0].evidence["attempts"], 1)

        resource_result = CrawlResult(start_url="https://example.com/", started_at="now")
        scanner._fetch_resource(
            SequenceFetcher([transient, recovered]), resource_result, resource_result.start_url,
            "https://example.com/app.js", "script", {}, set(), 1000,
        )
        self.assertEqual(resource_result.resources[0].fetch_attempts, 2)

    def test_rendered_network_thresholds_and_truncation_emit_findings(self) -> None:
        rendered = RenderedPage(
            "https://example.com/", request_count=3, transfer_bytes=301,
            network_requests=[{"url": "https://example.com/app.js", "status": 200, "resource_type": "script", "transfer_bytes": 301}],
            network_requests_truncated=True,
        )
        result = CrawlResult(start_url="https://example.com/", started_at="now")
        result.pages = [Page("https://example.com/", "https://example.com/", 200, "text/html", 1, 1)]
        scanner = Scanner(ScannerConfig(render_enabled=True, max_render_request_count=2, max_render_transfer_bytes=300))
        with patch("seo_scanner.runner.render_pages", return_value=([rendered], "")):
            scanner._run_rendered_diagnostics(result)
        self.assertEqual(
            {issue.rule_id for issue in result.issues},
            {"render.excessive_requests", "render.excessive_transfer", "render.network_inventory_truncated"},
        )

    def test_optional_axe_accessibility_is_bounded_and_classified(self) -> None:
        with TemporaryDirectory() as directory:
            axe_script = Path(directory) / "axe.min.js"
            axe_script.write_text("window.axe = {};", encoding="utf-8")
            config = ScannerConfig(
                render_enabled=True, accessibility_enabled=True, axe_script_path=str(axe_script),
                max_rendered_pages=1, render_settle_ms=0,
            )
            pages, setup_error = render_pages(["https://example.com/"], config, browser_factory=FakePlaywright)
        self.assertEqual(setup_error, "")
        self.assertEqual(pages[0].accessibility_violations_total, 2)
        self.assertTrue(pages[0].accessibility_truncated)
        result = CrawlResult(start_url="https://example.com/", started_at="now", pages=[Page("https://example.com/", "https://example.com/", 200, "text/html", 1, 1)])
        with patch("seo_scanner.runner.render_pages", return_value=(pages, "")):
            Scanner(config)._run_rendered_diagnostics(result)
        self.assertEqual(
            {issue.rule_id for issue in result.issues if issue.rule_id.startswith("accessibility.")},
            {"accessibility.critical_violations", "accessibility.violations", "accessibility.inventory_truncated"},
        )

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

    def test_http_declarations_cover_non_html_and_rendered_parity(self) -> None:
        header = parse_link_header(
            "https://example.com/file.pdf",
            '</canonical>; rel="canonical", </regional>; rel="alternate"; hreflang="en-AU"; title="English, Australia"',
        )
        self.assertEqual(header.canonical_urls, ["https://example.com/canonical"])
        self.assertEqual([(item.language, item.url) for item in header.hreflang], [("en-AU", "https://example.com/regional")])

        response = FetchResponse(
            "https://example.com/file.pdf", "https://example.com/file.pdf", 200,
            "application/pdf", b"pdf", 3, 1, [], False,
            {"x-robots-tag": "noindex", "link": '</canonical>; rel="canonical", </regional>; rel="alternate"; hreflang="en-AU"'},
        )
        result = CrawlResult(start_url="https://example.com/", started_at="now")
        state = _RunState(result.start_url, result, float("inf"), deque(), set())

        class StubFetcher:
            def get(self, *_):
                return response

        Scanner()._fetch_page(StubFetcher(), state, response.requested_url)
        self.assertEqual(result.pages[0].robots_directives, ["noindex"])
        self.assertEqual(result.pages[0].header_canonical_urls, ["https://example.com/canonical"])
        self.assertEqual(set(state.page_queue), {"https://example.com/canonical", "https://example.com/regional"})

        html = b'<html><head><link rel="canonical" href="/html-canonical"></head><body></body></html>'
        response = FetchResponse(
            "https://example.com/page", "https://example.com/page", 200,
            "text/html", html, len(html), 1, [], False,
            {"link": '</header-canonical>; rel="canonical"'},
        )
        conflict = CrawlResult(start_url="https://example.com/", started_at="now")
        conflict_state = _RunState(conflict.start_url, conflict, float("inf"), deque(), set())
        Scanner()._fetch_page(StubFetcher(), conflict_state, response.requested_url)
        self.assertEqual(
            {issue.rule_id for issue in conflict.issues if issue.rule_id.startswith("canonical.")},
            {"canonical.header_html_conflict", "canonical.multiple"},
        )

        raw = Page(
            "https://example.com/", "https://example.com/", 200, "text/html", 1, 1,
            title="Raw title", meta_description="Raw description", h1s=["Raw heading"],
            html_language="en", html_canonical_urls=["https://example.com/raw"],
            html_robots_directives=["noindex"],
        )
        rendered = RenderedPage(
            raw.url, raw.url, title="Rendered title", meta_description="Rendered description",
            canonical_url="https://example.com/rendered", robots_directives=["index"],
            h1s=["Rendered heading"], html_language="fr",
        )
        comparison = CrawlResult(start_url=raw.url, started_at="now")
        Scanner()._add_rendered_signal_issues(comparison, raw, rendered)
        self.assertEqual(
            {issue.rule_id for issue in comparison.issues},
            {"render.title_changed", "render.meta_description_changed", "render.canonical_changed", "render.robots_changed", "render.h1_changed", "render.language_changed"},
        )

    def test_document_language_integrity_findings(self) -> None:
        html = b'<html lang="en-AU"><head><link rel="alternate" hreflang="fr-FR" href="/page"></head></html>'
        response = FetchResponse(
            "https://example.com/page", "https://example.com/page", 200,
            "text/html", html, len(html), 1, [], False,
            {"content-language": "de-DE, not_a_language"},
        )
        result = CrawlResult(start_url="https://example.com/", started_at="now")
        state = _RunState("https://example.com/", result, float("inf"), deque(), set())

        class StubFetcher:
            def get(self, *_):
                return response

        Scanner()._fetch_page(StubFetcher(), state, response.requested_url)
        language_rules = {issue.rule_id for issue in result.issues if issue.rule_id.startswith("language.")}
        self.assertEqual(language_rules, {"language.content_invalid", "language.html_content_conflict", "language.html_hreflang_conflict"})
        self.assertEqual(result.pages[0].html_language, "en-AU")
        self.assertEqual(result.pages[0].content_languages, ["de-DE", "not_a_language"])

    def test_document_encoding_integrity_and_effective_charset(self) -> None:
        body = b" " * 1020 + b'<meta charset="windows-1252"><meta charset="invalid-charset">'
        signals = analyze_encoding(body, "text/html; charset=utf-8")
        self.assertEqual(signals.http_charset, "utf-8")
        self.assertEqual(signals.meta_charsets, ["windows-1252", "invalid-charset"])
        self.assertEqual(signals.invalid_charsets, ["invalid-charset"])
        self.assertEqual(signals.effective_charset, "utf-8")
        self.assertGreater(signals.meta_charset_offsets[0], 1024)

        response = FetchResponse(
            "https://example.com/", "https://example.com/", 200,
            "text/html", body, len(body), 1, [], False,
            {"content-type": "text/html; charset=utf-8"},
        )
        result = CrawlResult(start_url=response.requested_url, started_at="now")
        state = _RunState(response.requested_url, result, float("inf"), deque(), set())

        class StubFetcher:
            def get(self, *_):
                return response

        Scanner()._fetch_page(StubFetcher(), state, response.requested_url)
        encoding_rules = {issue.rule_id for issue in result.issues if issue.rule_id.startswith("encoding.")}
        self.assertEqual(encoding_rules, {"encoding.invalid", "encoding.conflict", "encoding.meta_late"})
        self.assertEqual(result.pages[0].http_charset, "utf-8")
        self.assertEqual(result.pages[0].meta_charsets, ["windows-1252", "invalid-charset"])

    def test_document_structure_duplicate_content_and_image_delivery(self) -> None:
        document = analyze_document(
            '<html><head><title>One</title><title>Two</title></head><body>'
            '<meta name="description" content="outside"><body></body></html>'
        )
        self.assertEqual((document.head_count, document.body_count, document.title_count), (1, 2, 2))
        self.assertTrue(document.meta_description_outside_head)

        repeated = " ".join(f"word{index}" for index in range(120))
        first_content = extract_page_content(f"<html><body>{repeated}</body></html>")
        second_content = extract_page_content(f"<html><body>{repeated}</body></html>")
        similar_content = extract_page_content(f"<html><body>{repeated} additional</body></html>")
        pages = [
            Page("https://example.com/a", "https://example.com/a", 200, "text/html", 1, 1, word_count=120, visible_text_hash=first_content.visible_text_hash, visible_text_fingerprint=first_content.visible_text_fingerprint,
                 images=[ImageReference("https://example.com/hero.jpg", "Hero")]),
            Page("https://example.com/b", "https://example.com/b", 200, "text/html", 1, 1, word_count=120, visible_text_hash=second_content.visible_text_hash, visible_text_fingerprint=second_content.visible_text_fingerprint),
            Page("https://example.com/c", "https://example.com/c", 200, "text/html", 1, 1, word_count=121, visible_text_hash=similar_content.visible_text_hash, visible_text_fingerprint=similar_content.visible_text_fingerprint),
        ]
        result = CrawlResult(start_url="https://example.com/", started_at="now", pages=pages, resources=[
            Resource("https://example.com/hero.jpg", kind="image", status=200, bytes=150_000, image_width=1600, image_height=900, image_format="JPEG"),
        ])
        scanner = Scanner(ScannerConfig(min_duplicate_content_words=100, min_responsive_image_width=1000, min_legacy_image_bytes=100_000))
        scanner._add_duplicate_body_issues(result, pages, set())
        scanner._add_image_delivery_issues(result, pages, set())
        rules = [issue.rule_id for issue in result.issues]
        self.assertEqual(rules.count("content.duplicate_body"), 2)
        self.assertEqual(rules.count("content.near_duplicate_body"), 3)
        self.assertIn("image.missing_dimensions", rules)
        self.assertIn("image.missing_responsive_source", rules)
        self.assertIn("image.legacy_format", rules)

    def test_missing_and_empty_html_language_are_distinct(self) -> None:
        missing = extract_page_signals("https://example.com/", "<html><body></body></html>")
        empty = extract_page_signals("https://example.com/", '<html lang=""><body></body></html>')
        self.assertFalse(missing.html_language_declared)
        self.assertTrue(empty.html_language_declared)
        self.assertEqual(empty.html_language, "")

    def test_indexability_declarations_retain_all_values_and_conflicts(self) -> None:
        signals = extract_page_signals(
            "https://example.com/page",
            '''<link rel="canonical" href="/first"><link rel="canonical" href="/second">
            <link rel="canonical"><link rel="canonical" href="mailto:test@example.com">
            <meta name="robots" content="index, noindex, follow, nofollow">
            <meta http-equiv="Refresh" content="2.5; URL='/next'">''',
        )
        self.assertEqual(signals.canonical_urls, ["https://example.com/first", "https://example.com/second"])
        self.assertEqual(signals.invalid_canonical_values, ["<missing href>", "mailto:test@example.com"])
        self.assertEqual(
            signals.robots_conflicts,
            ["index conflicts with noindex/none", "follow conflicts with nofollow/none"],
        )
        self.assertEqual(signals.meta_refresh_delay, 2.5)
        self.assertEqual(signals.meta_refresh_url, "https://example.com/next")

    def test_indexability_response_rules_and_canonical_queueing(self) -> None:
        html = b'''<link rel="canonical" href="/first"><link rel="canonical" href="/second">
            <link rel="canonical"><meta name="robots" content="all, noindex">
            <meta http-equiv="refresh" content="0;url=/next">'''
        response = FetchResponse(
            "https://example.com/page", "https://example.com/page", 200,
            "text/html", html, len(html), 1, [], False, {"refresh": "0; url=/header-target"},
        )
        result = CrawlResult(start_url="https://example.com/", started_at="now")
        state = _RunState("https://example.com/", result, float("inf"), deque(), set())

        class StubFetcher:
            def get(self, *_):
                return response

        Scanner()._fetch_page(StubFetcher(), state, response.requested_url)
        self.assertEqual(
            {"canonical.multiple", "canonical.invalid", "directive.conflicting_robots", "page.meta_refresh", "page.refresh_header", "language.html_missing", "encoding.missing", "document.head_missing", "document.body_missing"},
            {issue.rule_id for issue in result.issues},
        )
        self.assertEqual(set(state.page_queue), {"https://example.com/first", "https://example.com/second"})
        self.assertEqual(
            {edge.target_url for edge in state.edges if edge.context == "link.canonical"},
            {"https://example.com/first", "https://example.com/second"},
        )
        self.assertEqual(result.pages[0].refresh_header, "0; url=/header-target")

    def test_missing_canonical_only_applies_to_indexable_html(self) -> None:
        result = CrawlResult(start_url="https://example.com/", started_at="now")
        result.pages = [
            Page("https://example.com/indexable", "https://example.com/indexable", 200, "text/html", 1, 1),
            Page("https://example.com/noindex", "https://example.com/noindex", 200, "text/html", 1, 1, robots_directives=["noindex"]),
            Page("https://example.com/image", "https://example.com/image", 200, "image/png", 1, 1),
            Page("https://example.com/invalid", "https://example.com/invalid", 200, "text/html", 1, 1, invalid_canonical_values=["<missing href>"]),
        ]
        Scanner()._add_content_issues(result, set())
        missing = [issue.url for issue in result.issues if issue.rule_id == "canonical.missing"]
        self.assertEqual(missing, ["https://example.com/indexable"])

    def test_jsonld_block_types_and_structural_duplicates(self) -> None:
        signals = extract_page_signals(
            "https://example.com/",
            '''<script type="application/ld+json">{"@graph":[{"@type":"Organization"},{"@type":["WebSite","Thing"]}]}</script>
            <script type="application/ld+json"> { "@graph": [ { "@type": "Organization" }, { "@type": ["WebSite", "Thing"] } ] } </script>''',
        )
        self.assertEqual(signals.jsonld_errors, [])
        self.assertEqual(signals.jsonld_blocks[0]["types"], ["Organization", "Thing", "WebSite"])
        self.assertEqual(signals.duplicate_jsonld_blocks, [{"block_indices": [1, 2], "types": ["Organization", "Thing", "WebSite"]}])

    def test_jsonld_structural_integrity_and_local_references(self) -> None:
        signals = extract_page_signals(
            "https://example.com/",
            '''<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@id":"#thing","@type":[]},{"@id":"#missing"}]}</script>
            <script type="application/ld+json">{"@type":"Article"}</script>
            <script type="application/ld+json">42</script>''',
        )
        self.assertTrue(any("@type must" in item for item in signals.jsonld_integrity_errors))
        self.assertTrue(any("root must" in item for item in signals.jsonld_integrity_errors))
        self.assertIn("Block 2: typed data has no @context", signals.jsonld_integrity_warnings)
        self.assertIn("Local @id reference #missing has no definition on the page", signals.jsonld_integrity_warnings)

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
        with self.assertRaises(ValueError):
            ScannerConfig(near_duplicate_similarity=1.1)

    def test_page_transport_rules_cover_size_speed_and_redirect_chains(self) -> None:
        response = FetchResponse(
            "https://example.com/old", "https://example.com/final", 200,
            "text/html", b"body", 500, 4000,
            ["https://example.com/old", "https://example.com/hop", "https://example.com/final"],
            False, {},
        )
        result = CrawlResult(start_url="https://example.com/", started_at="now")
        scanner = Scanner(ScannerConfig(max_page_size=100, max_page_duration_ms=100))
        scanner._add_page_response_issues(result, response.requested_url, response, extract_page_signals(response.final_url, "", ""), EncodingSignals(), DocumentSignals(), set())
        self.assertEqual(
            {issue.rule_id for issue in result.issues},
            {"page.oversized", "page.slow_response", "page.redirect_chain", "language.html_missing", "encoding.missing", "document.head_missing", "document.body_missing"},
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
        self.assertTrue(any(resource.url.endswith("social.png") for resource in result.resources))

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
            ndjson_output = Path(directory) / "report.ndjson"
            sarif_output = Path(directory) / "report.sarif"
            code = main([url, "--output", str(output), "--resource-csv", str(csv_output), "--ndjson", str(ndjson_output), "--sarif", str(sarif_output), "--quiet"])
            report = json.loads(output.read_text(encoding="utf-8"))
            csv_text = csv_output.read_text(encoding="utf-8-sig")
            ndjson = [json.loads(line) for line in ndjson_output.read_text(encoding="utf-8").splitlines()]
            sarif = json.loads(sarif_output.read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(report["schema_version"], "1.25")
        self.assertEqual(report["status"], "complete")
        self.assertIn("cache_control", csv_text)
        self.assertEqual(ndjson[0]["type"], "scan")
        self.assertEqual(ndjson[-1]["type"], "summary")
        self.assertIn("issue", {record["type"] for record in ndjson})
        self.assertEqual(sarif["version"], "2.1.0")
        self.assertEqual(sarif["runs"][0]["results"][0]["partialFingerprints"]["issueId"], report["issues"][0]["issue_id"])

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
