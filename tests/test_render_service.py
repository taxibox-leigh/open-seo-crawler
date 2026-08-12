"""Regression tests for JS rendering.

The bug these guard against: Playwright's sync API binds to the greenlet that
created it, so a browser built in the request/generator thread raised
"Cannot switch to a different thread" on every call made from a
ThreadPoolExecutor worker. The crawl still completed — it just silently
recorded js_rendered=False for every page, which looks identical to a site
with no JavaScript.
"""
from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from app import RenderService, _crawl_page

# Server-rendered HTML says "raw"; the script rewrites it to "rendered" and
# injects a paragraph that only exists after JS runs.
FIXTURE_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>raw title</title>
<meta name="description" content="raw description"></head>
<body>
<h1 id="heading">raw heading</h1>
<div id="mount"></div>
<script>
  document.title = 'rendered title';
  document.getElementById('heading').textContent = 'rendered heading';
  document.getElementById('mount').innerHTML =
    '<p>' + 'js-only paragraph '.repeat(40) + '</p>';
</script>
</body></html>
"""


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib naming
        body = FIXTURE_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _playwright_available():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception:
        return False
    return True


@unittest.skipUnless(_playwright_available(), "playwright is not installed")
class RenderServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_renders_from_a_foreign_thread(self):
        """The whole point: render() must work when called off-thread."""
        renderer = RenderService(user_agent="test-agent")
        self.assertTrue(renderer.start(), msg=renderer.error)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: renderer.render(self.base), range(4)))
            for html, error in results:
                self.assertIsNone(error)
                self.assertIn("rendered title", html)
                self.assertIn("js-only paragraph", html)
        finally:
            renderer.stop()

    def test_crawl_page_marks_pages_as_rendered(self):
        renderer = RenderService(user_agent="test-agent")
        self.assertTrue(renderer.start(), msg=renderer.error)
        session = requests.Session()
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                page = pool.submit(
                    _crawl_page, self.base, session, "127.0.0.1",
                    renderer=renderer, capture_no_js=True,
                ).result()
        finally:
            session.close()
            renderer.stop()

        self.assertTrue(page["js_rendered"], msg=page.get("render_errors"))
        self.assertEqual(page.get("render_errors", []), [])
        # Parsed fields must come from the rendered DOM, not the raw HTML.
        self.assertEqual(page["title"], "rendered title")
        self.assertEqual(page["h1"], "rendered heading")
        # And the no-JS comparison must see the pre-render version.
        self.assertEqual(page["non_js"]["title"], "raw title")

    def test_unavailable_browser_reports_instead_of_raising(self):
        renderer = RenderService(user_agent="test-agent")
        renderer.available = False
        renderer.error = "browser missing"
        html, error = renderer.render(self.base)
        self.assertIsNone(html)
        self.assertEqual(error, "browser missing")
        renderer.stop()


if __name__ == "__main__":
    unittest.main()
