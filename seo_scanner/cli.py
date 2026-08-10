from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import ScannerConfig
from .runner import Scanner
from .reports import write_resource_csv
from .baseline import apply_suppressions, compare_with_baseline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="open-seo-scanner", description="Run an unattended technical SEO scan")
    parser.add_argument("url")
    parser.add_argument("-c", "--config", type=Path, help="JSON configuration file")
    parser.add_argument("-o", "--output", type=Path, default=Path("seo-scan.json"))
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--max-resources", type=int)
    parser.add_argument("--max-duration-seconds", type=float)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--resource-csv", type=Path, help="Optional flat resource inventory CSV")
    parser.add_argument("--baseline", type=Path, help="Previous scanner JSON report to compare")
    parser.add_argument("--ignore-issues", type=Path, help="JSON array or newline-delimited stable issue IDs to suppress")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    values = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
    for key in ("max_pages", "max_resources", "max_duration_seconds"):
        value = getattr(args, key)
        if value is not None:
            values[key] = value
    try:
        config = ScannerConfig.from_dict(values)
        progress = None if args.quiet else lambda item: print(json.dumps({"type": "progress", **item}), file=sys.stderr, flush=True)
        result = Scanner(config, progress).scan(args.url)
        if args.ignore_issues:
            apply_suppressions(result, _read_issue_ids(args.ignore_issues))
        if args.baseline:
            compare_with_baseline(result, json.loads(args.baseline.read_text(encoding="utf-8")))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
        if args.resource_csv:
            write_resource_csv(result, args.resource_csv)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"open-seo-scanner: {exc}", file=sys.stderr)
        return 2
    if result.status == "partial":
        return 3
    return 1 if any(issue.severity == "error" and not issue.suppressed for issue in result.issues) else 0


def _read_issue_ids(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("ignore issue file must be a JSON string array or one issue ID per line")
    return value
