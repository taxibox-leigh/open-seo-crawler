"""Severity model for the rendered-vs-source comparison.

Image counts and external links differ on almost every page of a site that
lazy-loads images or embeds a chat widget. When they could raise severity, a
691-page production crawl reported 555 pages at 'medium' — enough noise to
bury the ~20 pages whose indexable content genuinely changes under JavaScript.
They are still recorded, under `context`, but they no longer set severity.
"""
from __future__ import annotations

import unittest

from app import _compute_js_diff


def _page(**overrides):
    base = {
        "title": "A title", "meta_description": "A description", "h1": "A heading",
        "schema_types": ["WebPage"], "word_count": 1000,
        "internal_links": 50, "external_links": 5,
        "images_total": 40, "images_no_alt": 0,
    }
    base.update(overrides)
    return base


def _source(**overrides):
    base = {
        "title": "A title", "meta_description": "A description", "h1": "A heading",
        "schema_types": ["WebPage"], "word_count": 1000,
        "internal_links_count": 50, "external_links_count": 5,
        "images_count": 40, "images_no_alt": 0,
    }
    base.update(overrides)
    return base


class JsDiffSeverityTest(unittest.TestCase):
    def test_identical_pages_report_nothing(self):
        diff = _compute_js_diff(_page(), _source())
        self.assertEqual(diff, {"severity": "none", "fields": [], "context": []})

    def test_lazy_loaded_images_do_not_raise_severity(self):
        """The 512-page case: more images after JS, nothing else changed."""
        diff = _compute_js_diff(_page(images_total=56), _source(images_count=48))
        self.assertEqual(diff["severity"], "none")
        self.assertEqual(diff["fields"], [])
        self.assertIn("images_total", diff["context"])

    def test_injected_widget_links_do_not_raise_severity(self):
        """The 303-page case: a chat widget adds external links."""
        diff = _compute_js_diff(_page(external_links=12), _source(external_links_count=5))
        self.assertEqual(diff["severity"], "none")
        self.assertIn("external_links", diff["context"])

    def test_alt_count_changes_are_evidence_only(self):
        diff = _compute_js_diff(_page(images_no_alt=3), _source(images_no_alt=0))
        self.assertEqual(diff["severity"], "none")
        self.assertIn("images_no_alt", diff["context"])

    def test_title_change_is_critical(self):
        diff = _compute_js_diff(_page(title="Rendered title"), _source())
        self.assertEqual(diff["severity"], "critical")
        self.assertEqual(diff["fields"], ["title"])

    def test_schema_injected_by_js_is_critical(self):
        """Real finding from production: AboutPage only exists after render."""
        diff = _compute_js_diff(
            _page(schema_types=["WebPage", "AboutPage"]), _source(schema_types=["WebPage"]))
        self.assertEqual(diff["severity"], "critical")
        self.assertIn("schema_types", diff["fields"])

    def test_heading_and_word_count_are_high(self):
        self.assertEqual(_compute_js_diff(_page(h1="Rendered"), _source())["severity"], "high")
        self.assertEqual(_compute_js_diff(_page(word_count=300), _source())["severity"], "high")

    def test_js_only_navigation_is_high_not_medium(self):
        """A menu that only exists after JS changes what a crawler can reach."""
        diff = _compute_js_diff(_page(internal_links=50), _source(internal_links_count=2))
        self.assertEqual(diff["severity"], "high")
        self.assertIn("internal_links", diff["fields"])

    def test_noise_alongside_a_real_finding_keeps_the_real_severity(self):
        diff = _compute_js_diff(
            _page(title="Rendered title", images_total=90), _source(images_count=40))
        self.assertEqual(diff["severity"], "critical")
        self.assertEqual(diff["fields"], ["title"])
        self.assertIn("images_total", diff["context"])


if __name__ == "__main__":
    unittest.main()
