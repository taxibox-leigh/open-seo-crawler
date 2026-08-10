# Roadmap: from page crawler to complete technical SEO scanner

## Purpose

Evolve open-seo-crawler into a reproducible, automation-friendly technical SEO
scanner while keeping it FOSS, locally runnable, and useful through the existing
web UI. The scanner should cover HTML pages and every resource those pages depend
on, expose stable machine-readable results, and remain safe to run in unattended
containers.

## Current baseline

The project already has unusually broad page-level coverage for a small FOSS
crawler:

- concurrent and JavaScript-rendered crawling;
- robots.txt, sitemap, hreflang, canonical and indexability analysis;
- titles, descriptions, headings, schema presence, social metadata and content;
- redirects, response codes, security headers, mixed content and URL hygiene;
- duplicate/near-duplicate reports, crawl depth, soft-404 probes and exports.

The main gaps are architectural and resource-oriented:

- non-HTML extensions are deliberately excluded from crawl results;
- images are parsed for alt text but are not fetched for status, redirects,
  content type, byte size, dimensions or compression efficiency;
- CSS, JavaScript, fonts, media, manifests and documents have no resource graph;
- external links are extracted but not comprehensively validated;
- performance is response-time based rather than navigation/resource based;
- issue labels are display strings rather than stable rule IDs and schemas;
- `app.py` is about 4,600 lines and `static/script.js` about 6,000 lines;
- regression coverage is currently a single browser/data-correctness script.

## Product principles

1. **One crawl, two inventories.** HTML documents and dependent resources are
   different entity types, but share discovery, fetch and reporting primitives.
2. **Evidence before advice.** Every issue contains a stable rule ID, severity,
   observed value, source URL(s), target URL and remediation text.
3. **Bounded unattended execution.** Page, resource, byte, host and wall-clock
   limits are explicit; reaching one produces a completed partial report.
4. **Deterministic by default.** Raw-HTML scanning is the baseline. Rendering,
   Lighthouse and accessibility scans are optional, separately budgeted stages.
5. **Library first, UI second.** The Flask UI, CLI and API consume the same core
   scanner package and result schema.
6. **Safe scope.** Same-origin resources are checked by default. External hosts
   use configurable concurrency, delay and allow/deny rules.

## Gap and target matrix

| Area | Current behaviour | Target behaviour | Priority |
|---|---|---|---|
| Images | Count and alt-text analysis | Status, redirects, MIME, bytes, dimensions, format, loading hints and duplicate payloads | P1 |
| CSS/JS/fonts | Excluded as crawl entities | Resource graph, status/MIME/size/cache/compression checks | P1 |
| Documents/media | Excluded | Configurable inventory and response validation | P1 |
| Links | Page discovery and malformed href checks | Optional internal and external destination validation | P1 |
| Directives | Core robots/noindex/canonical | Conflicts, invalid values, canonical chains/loops and robots consistency | P2 |
| Structured data | Presence and type extraction | Parse errors, duplicate blocks and optional vocabulary validation | P2 |
| Rendering | Optional Playwright HTML comparison | Per-page budgets, diagnostics, failed-resource capture and deterministic teardown | P2 |
| Performance | Server response time | Local Lighthouse plus page/resource weight and cache/compression findings | P3 |
| Accessibility | Image-alt heuristics | Optional axe-core scan and accessibility issue export | P3 |
| Automation | UI-first SSE endpoints | Supported CLI, versioned JSON schema, exit codes, progress and resumability | P0 |
| Tests | One browser correctness script | Unit, fixture, integration and end-to-end suites in CI | P0 |

## Target architecture

Split scanner logic from Flask without a flag-day rewrite:

```text
seo_scanner/
  config.py             validated crawl configuration
  models.py             Page, Resource, Edge, FetchResult, Issue, CrawlResult
  rules.py              stable rule registry and severity metadata
  scope.py              URL normalisation and scope decisions
  fetch.py              HTTP client, retries, throttling and byte limits
  render.py             optional Playwright worker and diagnostics
  discovery.py          HTML/CSS/sitemap resource and page discovery
  analyzers/
    page.py
    resource.py
    links.py
    directives.py
    structured_data.py
    duplicates.py
  reports/
    json_report.py
    xlsx_report.py
  runner.py             queues, budgets, progress, pause/finalise/resume
  cli.py                unattended entry point
app.py                   thin Flask adapter
```

The core result model should distinguish:

- `Page`: an HTML document eligible for page-level SEO rules;
- `Resource`: image, stylesheet, script, font, document, media or other fetch;
- `Edge`: page/resource relationship and discovery context (`img.src`,
  `link.stylesheet`, `css.url`, `a.href`, etc.);
- `Issue`: stable ID, entity, severity, evidence and remediation;
- `CrawlResult`: inventories, aggregates, limits, errors and source status.

## Phased delivery

### Phase 0 — scanner contract and regression harness (medium)

- Introduce stable rule IDs while retaining current display labels.
- Define and version a JSON result schema.
- Add CLI flags/config-file support and meaningful exit codes.
- Emit progress snapshots with fetched/queued/in-flight/error counts and latest
  activity timestamps.
- Make page, resource and wall-clock limits finalise automatically.
- Extract URL normalisation, scope and issue classification into testable modules.
- Add CI for supported Python versions and fixture-based offline tests.

Acceptance:

- existing page findings match golden fixtures;
- a capped crawl always emits a valid partial result and limit reason;
- raw and rendered modes terminate without leaked browser/server processes;
- CLI and UI produce the same schema for the same fixture site.

### Phase 1 — resource graph and asset health (large)

- Discover resources from `src`, `srcset`, `<picture>`, scripts, stylesheets,
  icons, preload/prefetch/modulepreload, manifests, media and CSS `url()`/`@import`.
- Deduplicate canonicalised resource URLs while retaining every referring edge.
- Fetch with streamed GETs, bounded bytes, retry/backoff and per-host throttling.
- Record status, final URL, redirect hops, MIME type, declared/observed bytes,
  cache headers, content encoding, ETag/Last-Modified and fetch duration.
- For images, record intrinsic dimensions and format where safely decodable.
- Add stable findings for broken resources, redirecting resources, redirect
  chains, MIME mismatch, empty responses, oversized assets, oversized images,
  missing compression, weak caching and mixed content.
- Add resource inventory, broken assets and largest assets reports.

Acceptance:

- fixtures cover broken/redirecting images, CSS, JS and fonts;
- `srcset` and nested CSS resources retain correct referring pages;
- a misleading 200 HTML response for an image becomes a MIME mismatch;
- resource caps and byte caps finalise with explicit coverage metadata.

### Phase 2 — deeper SEO correctness (large)

- Validate internal and optionally external link destinations independently of
  whether they are HTML crawl candidates.
- Detect canonical chains, loops, non-200 targets, blocked targets and directive
  conflicts (`noindex` plus canonical elsewhere, robots versus meta directives).
- Expand robots meta/X-Robots-Tag parsing to token-level validation.
- Add sitemap validation for malformed XML, duplicate URLs, invalid dates,
  index recursion, size/count limits and canonical/indexability conflicts.
- Validate hreflang syntax, canonical alignment, region/language codes and x-default.
- Report JSON-LD parse failures and malformed/duplicate structured-data blocks;
  keep full vocabulary validation optional and separately versioned.
- Add pagination, internationalisation and duplicate-cluster fixture suites.

### Phase 3 — rendered quality, performance and accessibility (large)

- Capture failed browser requests, console errors, final DOM URL and resource
  waterfall during rendered scans.
- Enforce navigation, idle, per-page and overall rendering budgets.
- Integrate local Lighthouse as an optional subprocess with pinned output schema.
- Report transfer weight, request count, render-blocking resources, cacheability
  and Core Web Vitals where Lighthouse can measure them reliably.
- Integrate optional axe-core scanning and map results into the common issue schema.
- Keep these tools optional so the core crawler remains lightweight.

### Phase 4 — operational scanner platform (medium)

- Add authenticated API mode and disable update/restart routes by default outside
  local desktop mode.
- Support resumable state in an explicit directory/object-store adapter.
- Add baseline comparison with new/resolved/regressed issue states.
- Add SARIF and NDJSON exports alongside JSON/XLSX.
- Publish container images and a documented scheduled-job example.
- Add upstream-sync CI and release notes for rule/schema changes.

## Initial issue backlog

Recommended implementation order:

1. Versioned `Issue`/`CrawlResult` models and rule registry.
2. Offline fixture server plus golden tests for existing checks.
3. Supported CLI and deterministic cap/finalise behaviour.
4. Resource/edge models and HTML resource discovery.
5. CSS resource discovery.
6. Bounded resource fetcher and HTTP/MIME findings.
7. Image metadata, size and caching findings.
8. Resource reports and JSON/XLSX exports.
9. Link-target validation and canonical/directive checks.
10. Render diagnostics, Lighthouse and axe-core adapters.
11. Baselines, SARIF and production container hardening.

## Testing strategy

Use a local fixture server; CI must not depend on arbitrary public websites.
Fixtures should model redirects, loops, slow responses, range requests, invalid
MIME types, compressed/uncompressed assets, cache headers, robots directives,
sitemaps, SPA rendering and malformed markup. Keep a small optional live-smoke
suite for release validation only.

Required layers:

- unit tests for URL/scope/directive/rule logic;
- extractor tests using saved HTML/CSS fixtures;
- HTTP integration tests against the fixture server;
- rendered tests against a deterministic local SPA;
- schema and golden-report compatibility tests;
- one browser UI smoke test consuming the same core runner.

## Compatibility and migration

- Preserve existing UI labels and XLSX columns during the first phases.
- Add stable IDs beside labels before changing any labels.
- Version JSON output from the start (`schema_version`).
- Treat thresholds as configuration, not embedded rule identity.
- Keep upstream changes easy to merge by moving new logic into modules and
  reducing changes to `app.py` incrementally.

## Definition of “complete enough” for routine use

The scanner is ready for unattended daily audits when it can:

- finish or explicitly finalise every bounded run;
- report page and resource coverage independently;
- validate all same-origin page dependencies and internal link targets;
- emit stable, versioned JSON with evidence and rule IDs;
- distinguish scanner/source failures from a clean site;
- compare with a prior baseline;
- pass deterministic fixture tests in CI;
- run in a pinned container without UI interaction.
