from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    severity: str
    remediation: str


RULES = {
    rule.id: rule
    for rule in (
        Rule("resource.fetch_failed", "Resource could not be fetched", "error", "Restore the resource or remove/update every reference to it."),
        Rule("resource.http_error", "Resource returns an HTTP error", "error", "Serve a successful resource response or update its referring pages."),
        Rule("resource.redirect", "Resource redirects", "warning", "Reference the final resource URL directly."),
        Rule("resource.mime_mismatch", "Resource MIME type does not match its use", "error", "Return the expected Content-Type and resource body."),
        Rule("resource.empty", "Resource response is empty", "warning", "Serve valid content or remove the resource reference."),
        Rule("resource.oversized", "Resource exceeds the configured size threshold", "warning", "Compress, resize, split, or defer the resource."),
        Rule("resource.response_truncated", "Resource exceeded the download byte limit", "warning", "Raise the safe byte limit or reduce the resource size."),
        Rule("resource.missing_compression", "Compressible resource is not encoded", "warning", "Enable Brotli or gzip transfer compression for text-based assets."),
        Rule("resource.weak_cache", "Static resource has a short or missing cache lifetime", "warning", "Serve fingerprinted static assets with a long-lived Cache-Control max-age."),
        Rule("resource.mixed_content", "HTTPS page references an HTTP resource", "error", "Update the resource reference to HTTPS."),
        Rule("image.oversized_dimensions", "Image dimensions exceed the configured threshold", "warning", "Resize the source image and serve responsive variants sized for their display context."),
        Rule("image.invalid", "Image response cannot be decoded", "error", "Serve a valid image payload matching its declared media type."),
        Rule("resource.duplicate_payload", "Multiple resource URLs return identical content", "info", "Consolidate duplicate assets where separate URLs are unnecessary."),
        Rule("crawl.limit_reached", "Crawl stopped at a configured limit", "error", "Increase the limit or reduce crawl scope so the audit covers the whole site."),
        Rule("page.fetch_failed", "Page could not be fetched", "error", "Restore the page or remove links to it."),
    )
}


def get_rule(rule_id: str) -> Rule:
    try:
        return RULES[rule_id]
    except KeyError as exc:
        raise ValueError(f"Unknown rule ID: {rule_id}") from exc
