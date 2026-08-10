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
        Rule("link.http_error", "Internal link target returns an HTTP error", "error", "Restore the target or update every internal link that references it."),
        Rule("link.redirect", "Internal link target redirects", "warning", "Update internal links to point directly to the final URL."),
        Rule("canonical.http_error", "Canonical target returns an HTTP error", "error", "Point the canonical to a successful, indexable URL."),
        Rule("canonical.redirect", "Canonical target redirects", "error", "Point the canonical directly to its final successful URL."),
        Rule("canonical.chain", "Canonical target declares another canonical", "warning", "Use a single direct canonical target across the cluster."),
        Rule("canonical.loop", "Canonical declarations form a loop", "error", "Choose one canonical URL and point every cluster member directly to it."),
        Rule("directive.noindex_canonical_conflict", "Noindex page canonicalizes elsewhere", "warning", "Use either noindex or canonical consolidation consistently for the intended outcome."),
        Rule("directive.invalid_robots", "Robots directive contains unsupported tokens", "warning", "Correct or remove invalid robots meta or X-Robots-Tag tokens."),
        Rule("structured_data.invalid_jsonld", "JSON-LD block contains invalid JSON", "error", "Correct the JSON syntax in the structured-data block."),
        Rule("sitemap.fetch_failed", "Sitemap could not be fetched", "error", "Restore the sitemap or remove its declaration from robots.txt or the sitemap index."),
        Rule("sitemap.http_error", "Sitemap returns an HTTP error", "error", "Serve the sitemap successfully or update/remove its declaration."),
        Rule("sitemap.invalid_xml", "Sitemap XML is malformed or unsupported", "error", "Serve a valid XML urlset or sitemapindex document."),
        Rule("sitemap.duplicate_url", "Sitemap contains duplicate URLs", "warning", "List each canonical URL only once across the sitemap set."),
        Rule("sitemap.invalid_lastmod", "Sitemap lastmod value is invalid", "warning", "Use a valid W3C date or datetime in lastmod."),
        Rule("sitemap.url_limit", "Sitemap exceeds the URL protocol limit", "error", "Split the sitemap so each file contains no more than 50,000 URLs."),
        Rule("sitemap.byte_limit", "Sitemap exceeds the configured byte limit", "error", "Split or compress the sitemap within the protocol size limit."),
        Rule("sitemap.recursion_limit", "Sitemap discovery reached its configured limit", "error", "Raise the sitemap limit or remove recursive/unnecessary sitemap indexes."),
        Rule("sitemap.url_http_error", "Sitemap URL returns an HTTP error", "error", "Remove the URL from the sitemap or restore a successful canonical page."),
        Rule("sitemap.url_redirect", "Sitemap URL redirects", "warning", "Replace it with the final canonical URL."),
        Rule("sitemap.url_noindex", "Sitemap URL is noindex", "error", "Remove the URL from the sitemap or make it indexable."),
        Rule("sitemap.url_noncanonical", "Sitemap URL canonicalizes elsewhere", "warning", "List only the canonical URL in the sitemap."),
        Rule("hreflang.invalid_language", "Hreflang language tag is invalid", "error", "Use x-default or a valid language tag with optional script and region subtags."),
        Rule("hreflang.duplicate_language", "Page has duplicate hreflang language entries", "error", "Declare only one alternate URL for each language/region value."),
        Rule("hreflang.missing_self", "Hreflang cluster is missing a self-reference", "warning", "Include the current canonical page in its own hreflang set."),
        Rule("hreflang.missing_return", "Hreflang alternate has no return link", "error", "Add a reciprocal hreflang reference from the alternate page."),
        Rule("hreflang.target_http_error", "Hreflang target returns an HTTP error", "error", "Point hreflang to a successful indexable page."),
        Rule("hreflang.target_redirect", "Hreflang target redirects", "error", "Point hreflang directly to the final canonical URL."),
        Rule("hreflang.target_noindex", "Hreflang target is noindex", "error", "Use an indexable alternate target or remove it from the cluster."),
        Rule("hreflang.target_noncanonical", "Hreflang target canonicalizes elsewhere", "warning", "Point hreflang to the target's canonical URL."),
    )
}


def get_rule(rule_id: str) -> Rule:
    try:
        return RULES[rule_id]
    except KeyError as exc:
        raise ValueError(f"Unknown rule ID: {rule_id}") from exc
