"""An internal URL that redirects off-site must not become a crawled page.

`/review-link.php` on the audited site 302s to Google Maps. The crawler
followed the chain, recorded the Google page as one of the site's own, and
then ran every page rule against it — including the JS-vs-source diff, which
duly reported a critical difference on a third party's page.

The hop is worth reporting. The destination is not ours to audit.
"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests


def _page(body: str, title: str) -> bytes:
    return (
        f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{title}</title></head><body>{body}</body></html>"
    ).encode()


class OffsiteHandler(BaseHTTPRequestHandler):
    """Stands in for both the audited site and the third-party destination."""

    offsite_base = ""

    def do_GET(self):  # noqa: N802 - stdlib naming
        if self.path == "/":
            body = _page(
                '<h1>Home</h1><a href="/review-link">leave a review</a>'
                f"<p>{'Body copy for the offsite redirect fixture. ' * 20}</p>",
                "Home page of the offsite redirect fixture",
            )
        elif self.path == "/review-link":
            self.send_response(302)
            self.send_header("Location", f"{self.offsite_base}/maps/place")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        elif self.path == "/maps/place":
            body = _page("<h1>Somebody else's page</h1>", "Third party maps page")
        else:
            body = _page("<h1>Missing</h1>", "Missing")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class OffsiteRedirectTest(unittest.TestCase):
    """Drives the crawl endpoint the way the audit routine does."""

    @classmethod
    def setUpClass(cls):
        import app

        cls.site = ThreadingHTTPServer(("127.0.0.1", 0), OffsiteHandler)
        cls.offsite = ThreadingHTTPServer(("127.0.0.1", 0), OffsiteHandler)
        # Two distinct hosts: 127.0.0.1 and localhost resolve to the same
        # machine but differ as netlocs, which is what scope keys on.
        OffsiteHandler.offsite_base = f"http://localhost:{cls.offsite.server_address[1]}"
        for server in (cls.site, cls.offsite):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.site.server_address[1]}/"
        app.app.config["TESTING"] = True
        cls.client = app.app.test_client()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.site, cls.offsite):
            server.shutdown()
            server.server_close()

    def _crawl(self):
        response = self.client.post("/crawl", json={
            "url": self.base, "max_pages": 10, "max_workers": 1, "crawl_delay": 0,
            "max_depth": 3, "render_js": False, "compare_no_js": False,
            "ignore_robots": True, "ignore_noindex": False,
            "exclude_patterns": "", "include_patterns": "",
        })
        pages, complete = [], None
        for raw in response.get_data(as_text=True).splitlines():
            if not raw.startswith("data: "):
                continue
            payload = raw[6:]
            if payload == "[DONE]":
                break
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "page":
                pages.append(event["data"])
            elif event.get("type") == "complete":
                complete = event
        return pages, complete

    def test_offsite_destination_is_not_recorded_as_a_page(self):
        pages, complete = self._crawl()
        crawled = [page["url"] for page in pages]
        self.assertTrue(any("127.0.0.1" in url for url in crawled), msg=crawled)
        self.assertFalse(
            [url for url in crawled if "localhost" in url],
            msg=f"third-party page was crawled as ours: {crawled}",
        )

    def test_the_hop_itself_is_still_reported(self):
        _, complete = self._crawl()
        reports = (complete or {}).get("reports", {})
        offsite = reports.get("offsite_redirects") or []
        self.assertTrue(offsite, msg="the off-site redirect was dropped entirely")
        self.assertIn("review-link", offsite[0]["url"])
        self.assertIn("localhost", offsite[0]["final_url"])


if __name__ == "__main__":
    unittest.main()
