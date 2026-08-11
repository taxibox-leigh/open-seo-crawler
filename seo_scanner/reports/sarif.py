from __future__ import annotations

import json
from pathlib import Path

from ..models import CrawlResult


_LEVELS = {"error": "error", "warning": "warning", "info": "note"}


def write_sarif(result: CrawlResult, path: Path) -> None:
    """Write scanner findings as a SARIF 2.1.0 log."""
    rules = {}
    for issue in result.issues:
        rules.setdefault(issue.rule_id, {
            "id": issue.rule_id,
            "name": issue.title,
            "shortDescription": {"text": issue.title},
            "help": {"text": issue.remediation},
            "defaultConfiguration": {"level": _LEVELS.get(issue.severity, "warning")},
        })
    findings = []
    for issue in result.issues:
        finding = {
            "ruleId": issue.rule_id,
            "level": _LEVELS.get(issue.severity, "warning"),
            "message": {"text": issue.message},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": issue.url}}}],
            "partialFingerprints": {"issueId": issue.issue_id},
            "properties": {
                "entityType": issue.entity_type,
                "suppressed": issue.suppressed,
                "evidence": issue.evidence,
                "referringUrls": issue.referring_urls,
            },
        }
        if issue.suppressed:
            finding["suppressions"] = [{"kind": "external", "status": "accepted"}]
        findings.append(finding)
    document = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "Open SEO Scanner", "rules": list(rules.values())}},
            "automationDetails": {"id": result.start_url},
            "invocations": [{"executionSuccessful": result.status in {"complete", "partial"}, "properties": {"scanStatus": result.status, "coverage": result.coverage.__dict__}}],
            "results": findings,
        }],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
