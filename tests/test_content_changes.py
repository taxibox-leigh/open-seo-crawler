"""Tests for run-over-run content-change reporting."""
from __future__ import annotations

import unittest

from seo_scanner.baseline import add_content_change_issues
from seo_scanner.models import CrawlResult, Page


def _result(page: Page) -> CrawlResult:
    result = CrawlResult(start_url="https://example.com/", started_at="now")
    result.pages = [page]
    return result


def _baseline(**fields) -> dict:
    base = {"url": "https://example.com/a", "title": "Original title",
            "meta_description": "Original description", "h1s": ["Original heading"],
            "word_count": 1000}
    base.update(fields)
    return {"pages": [base]}


class ContentChangeTest(unittest.TestCase):
    def test_changed_fields_are_reported_with_both_values(self):
        page = Page("https://example.com/a", "https://example.com/a", 200, "text/html", 1, 1,
                    title="New title", meta_description="Original description",
                    h1s=["Original heading"], word_count=1000)
        result = _result(page)
        add_content_change_issues(result, _baseline())
        issues = [issue for issue in result.issues if issue.rule_id == "content.title_changed"]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].evidence, {"previous": "Original title", "current": "New title"})
        self.assertEqual(issues[0].severity, "info")

    def test_unchanged_pages_report_nothing(self):
        page = Page("https://example.com/a", "https://example.com/a", 200, "text/html", 1, 1,
                    title="Original title", meta_description="Original description",
                    h1s=["Original heading"], word_count=1000)
        result = _result(page)
        add_content_change_issues(result, _baseline())
        self.assertEqual(result.issues, [])

    def test_small_word_count_movement_is_ignored(self):
        """Page churn moves the count a little; only real edits should report."""
        page = Page("https://example.com/a", "https://example.com/a", 200, "text/html", 1, 1,
                    title="Original title", meta_description="Original description",
                    h1s=["Original heading"], word_count=1100)
        result = _result(page)
        add_content_change_issues(result, _baseline())
        self.assertEqual(result.issues, [])

    def test_large_word_count_movement_is_reported(self):
        page = Page("https://example.com/a", "https://example.com/a", 200, "text/html", 1, 1,
                    title="Original title", meta_description="Original description",
                    h1s=["Original heading"], word_count=200)
        result = _result(page)
        add_content_change_issues(result, _baseline())
        self.assertEqual([issue.rule_id for issue in result.issues], ["content.word_count_changed"])

    def test_new_pages_are_not_reported_as_changes(self):
        page = Page("https://example.com/brand-new", "https://example.com/brand-new", 200, "text/html", 1, 1,
                    title="A title", h1s=["Heading"], word_count=500)
        result = _result(page)
        add_content_change_issues(result, _baseline())
        self.assertEqual(result.issues, [])

    def test_missing_baseline_pages_are_tolerated(self):
        page = Page("https://example.com/a", "https://example.com/a", 200, "text/html", 1, 1, title="New")
        result = _result(page)
        add_content_change_issues(result, {})
        self.assertEqual(result.issues, [])

    def test_all_tracked_fields_report_independently(self):
        page = Page("https://example.com/a", "https://example.com/a", 200, "text/html", 1, 1,
                    title="New title", meta_description="New description",
                    h1s=["New heading"], word_count=200)
        result = _result(page)
        add_content_change_issues(result, _baseline())
        self.assertEqual(
            {issue.rule_id for issue in result.issues},
            {"content.title_changed", "content.meta_description_changed",
             "content.h1_changed", "content.word_count_changed"},
        )


if __name__ == "__main__":
    unittest.main()
