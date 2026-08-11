from __future__ import annotations

import time
from pathlib import Path
from datetime import datetime, timezone
from collections.abc import Callable, Iterable
from typing import Any

from .config import ScannerConfig
from .models import RenderedPage


BrowserFactory = Callable[[], Any]


def select_render_urls(urls: Iterable[str], config: ScannerConfig, day_of_year: int | None = None) -> tuple[list[str], int, int, int]:
    eligible = sorted(set(urls))
    if not eligible:
        return [], 0, 0, 0
    slices = (len(eligible) + config.max_rendered_pages - 1) // config.max_rendered_pages
    selected_slice = 0
    if config.render_sample_strategy == "daily_rotation":
        day = day_of_year or datetime.now(timezone.utc).timetuple().tm_yday
        selected_slice = (day - 1) % slices
    start = selected_slice * config.max_rendered_pages
    return eligible[start:start + config.max_rendered_pages], len(eligible), selected_slice + 1, slices


def render_pages(urls: Iterable[str], config: ScannerConfig, browser_factory: BrowserFactory | None = None) -> tuple[list[RenderedPage], str]:
    """Render a bounded URL sample. Returns results and an optional setup error."""
    if browser_factory is None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return [], "Playwright is not installed"

        def browser_factory() -> Any:
            return sync_playwright()

    rendered: list[RenderedPage] = []
    try:
        with browser_factory() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for url in list(urls)[: config.max_rendered_pages]:
                    rendered.append(_render_page(browser, url, config))
            finally:
                browser.close()
    except Exception as exc:
        return rendered, str(exc)
    return rendered, ""


def _render_page(browser: Any, url: str, config: ScannerConfig) -> RenderedPage:
    started = time.monotonic()
    console_errors: list[str] = []
    failed_requests: list[dict[str, Any]] = []
    responses: list[Any] = []
    request_count = 0
    network_requests_truncated = False
    page = browser.new_page()

    def on_console(message: Any) -> None:
        if message.type == "error" and len(console_errors) < config.max_render_events_per_page:
            console_errors.append(message.text)

    def on_request_failed(request: Any) -> None:
        if len(failed_requests) < config.max_render_events_per_page:
            failure = request.failure
            error_text = failure.get("errorText", "") if isinstance(failure, dict) else str(failure or "")
            failed_requests.append({"url": request.url, "error": error_text})

    def on_request(_: Any) -> None:
        nonlocal request_count
        request_count += 1

    def on_response(response: Any) -> None:
        nonlocal network_requests_truncated
        if len(responses) < config.max_render_network_requests_per_page:
            responses.append(response)
        else:
            network_requests_truncated = True
        if response.status >= 400 and len(failed_requests) < config.max_render_events_per_page:
            failed_requests.append({"url": response.url, "status": response.status})

    page.on("console", on_console)
    page.on("request", on_request)
    page.on("requestfailed", on_request_failed)
    page.on("response", on_response)
    error = ""
    final_url = ""
    network_requests: list[dict[str, Any]] = []
    transfer_bytes = 0
    accessibility_violations: list[dict[str, Any]] = []
    accessibility_violations_total = 0
    accessibility_truncated = False
    accessibility_error = ""
    seo_signals: dict[str, Any] = {}
    seo_signals_error = ""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=config.render_navigation_timeout_ms)
        if config.render_settle_ms:
            page.wait_for_timeout(config.render_settle_ms)
        final_url = page.url
        try:
            seo_signals = page.evaluate("""() => ({
                title: document.title.trim(),
                meta_description: (document.querySelector('meta[name="description" i]')?.content || '').trim(),
                canonical_url: document.querySelector('link[rel~="canonical"]')?.href || '',
                robots_directives: Array.from(document.querySelectorAll('meta[name="robots" i], meta[name="googlebot" i], meta[name="bingbot" i]'))
                    .flatMap(item => (item.content || '').split(',').map(value => value.trim().toLowerCase()).filter(Boolean)),
                h1s: Array.from(document.querySelectorAll('h1')).map(item => (item.textContent || '').trim()).filter(Boolean),
                html_language: (document.documentElement.lang || '').trim()
            })""")
            if not isinstance(seo_signals, dict):
                raise TypeError("Rendered SEO signal evaluation returned an invalid result")
        except Exception as exc:
            seo_signals_error = str(exc)
        if config.accessibility_enabled:
            try:
                axe_path = Path(config.axe_script_path).resolve()
                if not axe_path.is_file():
                    raise FileNotFoundError(f"axe-core script not found: {axe_path}")
                page.add_script_tag(path=str(axe_path))
                axe_result = page.evaluate("""async (limits) => {
                    const result = await axe.run(document, {resultTypes: ['violations']});
                    return {
                        total: result.violations.length,
                        violations: result.violations.slice(0, limits.violations).map(item => ({
                            id: item.id, impact: item.impact, description: item.description,
                            help: item.help, help_url: item.helpUrl, tags: item.tags,
                            nodes: item.nodes.slice(0, limits.nodes).map(node => ({
                                target: node.target, html: node.html,
                                failure_summary: node.failureSummary
                            })),
                            nodes_total: item.nodes.length
                        }))
                    };
                }""", {"violations": config.max_accessibility_violations_per_page, "nodes": config.max_accessibility_nodes_per_violation})
                accessibility_violations = axe_result.get("violations", [])
                accessibility_violations_total = int(axe_result.get("total", len(accessibility_violations)))
                accessibility_truncated = accessibility_violations_total > len(accessibility_violations) or any(
                    item.get("nodes_total", 0) > len(item.get("nodes", [])) for item in accessibility_violations
                )
            except Exception as exc:
                accessibility_error = str(exc)
    except Exception as exc:
        error = str(exc)
        final_url = getattr(page, "url", "")
    finally:
        for response in responses:
            sizes: dict[str, int] = {}
            try:
                sizes = response.request.sizes()
            except Exception:
                pass
            transferred = max(0, sizes.get("responseBodySize", 0)) + max(0, sizes.get("responseHeadersSize", 0))
            transfer_bytes += transferred
            network_requests.append({
                "url": response.url,
                "status": response.status,
                "resource_type": getattr(response.request, "resource_type", ""),
                "transfer_bytes": transferred,
            })
        page.close()
    return RenderedPage(
        url=url,
        final_url=final_url,
        duration_ms=round((time.monotonic() - started) * 1000),
        console_errors=console_errors,
        failed_requests=failed_requests,
        network_requests=network_requests,
        request_count=request_count,
        transfer_bytes=transfer_bytes,
        network_requests_truncated=network_requests_truncated,
        accessibility_violations=accessibility_violations,
        accessibility_violations_total=accessibility_violations_total,
        accessibility_truncated=accessibility_truncated,
        accessibility_error=accessibility_error,
        title=str(seo_signals.get("title", "")),
        meta_description=str(seo_signals.get("meta_description", "")),
        canonical_url=str(seo_signals.get("canonical_url", "")),
        robots_directives=sorted(set(seo_signals.get("robots_directives", []))),
        h1s=[str(item) for item in seo_signals.get("h1s", [])],
        html_language=str(seo_signals.get("html_language", "")),
        seo_signals_error=seo_signals_error,
        error=error,
    )
