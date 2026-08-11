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
        Rule("structured_data.duplicate_jsonld", "Page repeats an identical JSON-LD block", "warning", "Remove duplicate structured-data script blocks while retaining one complete declaration."),
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
        Rule("external_link.fetch_failed", "External link could not be checked", "warning", "Verify the destination manually or update/remove the link."),
        Rule("external_link.http_error", "External link target returns an HTTP error", "warning", "Update or remove the external link."),
        Rule("external_link.redirect", "External link target redirects", "info", "Link directly to the final destination where practical."),
        Rule("render.unavailable", "Rendered diagnostics are unavailable", "warning", "Install the optional browser dependency and its Chromium runtime, or disable rendered diagnostics."),
        Rule("render.navigation_failed", "Page failed during browser rendering", "error", "Restore the page and its browser execution path, then rerun rendered diagnostics."),
        Rule("render.failed_requests", "Browser rendering encountered failed requests", "warning", "Restore or update failed requests that affect the rendered page."),
        Rule("render.console_errors", "Browser rendering emitted console errors", "warning", "Resolve actionable browser console errors and verify the page still renders correctly."),
        Rule("architecture.deep_page", "Page exceeds the configured click depth", "warning", "Add useful internal links that bring important pages closer to the site entry point."),
        Rule("architecture.sitemap_orphan", "Sitemap page has no incoming internal links", "warning", "Link to the page from relevant indexable content, or remove it from the sitemap if it should not be discovered."),
        Rule("content.title_missing", "Page title is missing", "error", "Add a unique, descriptive title element."),
        Rule("content.title_too_long", "Page title exceeds the configured length", "warning", "Shorten the title while retaining its primary topic and intent."),
        Rule("content.duplicate_title", "Multiple pages share the same title", "warning", "Give each indexable page a distinct title that describes its unique purpose."),
        Rule("content.meta_description_missing", "Meta description is missing", "warning", "Add a useful description for the page's search snippet."),
        Rule("content.meta_description_too_long", "Meta description exceeds the configured length", "warning", "Shorten the description so its key message is less likely to be truncated."),
        Rule("content.duplicate_meta_description", "Multiple pages share the same meta description", "warning", "Write a distinct description for each indexable page."),
        Rule("content.h1_missing", "Page has no H1 heading", "warning", "Add one clear primary heading that describes the page."),
        Rule("content.multiple_h1", "Page has multiple H1 headings", "warning", "Use one primary H1 and structure subordinate headings with lower levels."),
        Rule("content.thin", "Page has little visible text content", "warning", "Add useful original content that satisfies the page's search intent."),
        Rule("robots.unavailable", "robots.txt is temporarily unavailable", "error", "Restore a successful robots.txt response so crawlers can reliably determine access policy."),
        Rule("robots.byte_limit", "robots.txt exceeds the configured byte limit", "warning", "Reduce robots.txt below the supported size limit and keep directives concise."),
        Rule("robots.invalid_syntax", "robots.txt contains malformed directives", "warning", "Correct malformed robots.txt lines so crawlers interpret the intended policy."),
        Rule("robots.blocked_page", "Page is blocked by robots.txt", "error", "Allow crawling for the configured search crawler or remove the page from internal discovery and sitemaps."),
        Rule("robots.blocked_resource", "Page resource is blocked by robots.txt", "warning", "Allow crawling of resources required to render and understand indexable pages."),
        Rule("page.oversized", "HTML page exceeds the configured size threshold", "warning", "Reduce generated HTML and remove unnecessary inline markup or data."),
        Rule("page.slow_response", "Page response exceeds the configured duration", "warning", "Reduce server response time and investigate slow application or origin work."),
        Rule("page.response_truncated", "Page exceeded the download byte limit", "error", "Reduce the HTML response or raise the safe per-page byte limit."),
        Rule("page.redirect_chain", "Page uses a multi-hop redirect chain", "warning", "Redirect directly to the final canonical destination in one hop."),
        Rule("content.viewport_missing", "Page has no viewport declaration", "warning", "Add a responsive viewport meta tag for mobile rendering."),
        Rule("content.image_alt_missing", "Page images are missing alt attributes", "warning", "Add descriptive alt text, or an explicit empty alt attribute for decorative images."),
        Rule("social.og_title_missing", "Open Graph title is missing", "info", "Add an og:title value for shared-page previews."),
        Rule("social.og_description_missing", "Open Graph description is missing", "info", "Add an og:description value for shared-page previews."),
        Rule("social.og_image_missing", "Open Graph image is missing", "warning", "Add an absolute og:image URL for shared-page previews."),
        Rule("social.twitter_card_missing", "Twitter card declaration is missing", "info", "Add a twitter:card declaration and appropriate card metadata."),
    )
}


def get_rule(rule_id: str) -> Rule:
    try:
        return RULES[rule_id]
    except KeyError as exc:
        raise ValueError(f"Unknown rule ID: {rule_id}") from exc
