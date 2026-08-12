from __future__ import annotations

from typing import Any, Iterable

from .models import BaselineComparison, CrawlResult, Issue


def apply_suppressions(result: CrawlResult, issue_ids: Iterable[str]) -> None:
    suppressed = set(issue_ids)
    for issue in result.issues:
        issue.suppressed = issue.issue_id in suppressed


def compare_with_baseline(result: CrawlResult, baseline: dict[str, Any]) -> BaselineComparison:
    current = {issue.issue_id: issue for issue in result.issues}
    previous = {_baseline_issue_id(item): item for item in baseline.get("issues", []) if isinstance(item, dict)}
    previous.pop("", None)
    current_active = {issue_id for issue_id, issue in current.items() if not issue.suppressed}
    previous_active = {issue_id for issue_id, issue in previous.items() if not issue.get("suppressed", False)}
    comparison = BaselineComparison(
        new_issue_ids=sorted(current_active - previous_active),
        persistent_issue_ids=sorted(current_active & previous_active),
        resolved_issue_ids=sorted(previous_active - set(current)),
        suppressed_issue_ids=sorted(issue_id for issue_id, issue in current.items() if issue.suppressed),
    )
    result.comparison = comparison
    return comparison


# Fields worth diffing between runs, and the rule each change reports under.
# These are context, not defects: a changed H1 is only interesting next to
# something else. They are info severity for that reason, and consumers should
# not raise a ticket per finding — a site-wide re-template would produce one
# for every page.
_TRACKED_FIELDS = (
    ("title", "content.title_changed"),
    ("meta_description", "content.meta_description_changed"),
    ("h1s", "content.h1_changed"),
    ("word_count", "content.word_count_changed"),
)

# Below this, a word-count move is normal page churn rather than a content edit.
_WORD_COUNT_TOLERANCE = 0.25


def add_content_change_issues(result: CrawlResult, baseline: dict[str, Any]) -> None:
    """Report per-page content changes against the previous run."""
    from .rules import get_rule

    previous = {
        str(page.get("url")): page
        for page in baseline.get("pages", []) if isinstance(page, dict) and page.get("url")
    }
    if not previous:
        return
    for page in result.pages:
        before = previous.get(page.url)
        if before is None:
            continue
        for field_name, rule_id in _TRACKED_FIELDS:
            old = before.get(field_name)
            new = getattr(page, field_name, None)
            if old is None or new is None or old == new:
                continue
            if field_name == "word_count":
                if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
                    continue
                if abs(new - old) <= max(old, 1) * _WORD_COUNT_TOLERANCE:
                    continue
            rule = get_rule(rule_id)
            result.issues.append(Issue(
                rule.id, rule.title, rule.severity, "page", page.url,
                f"{field_name} changed since the previous run",
                {"previous": old, "current": new}, [], rule.remediation,
            ))


def _baseline_issue_id(item: dict[str, Any]) -> str:
    existing = item.get("issue_id")
    if isinstance(existing, str) and existing:
        return existing
    try:
        legacy = Issue(
            rule_id=str(item["rule_id"]), title=str(item.get("title", "")), severity=str(item.get("severity", "warning")),
            entity_type=str(item["entity_type"]), url=str(item["url"]), message=str(item.get("message", "")),
        )
    except KeyError:
        return ""
    return legacy.issue_id
