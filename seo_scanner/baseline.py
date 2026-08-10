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
