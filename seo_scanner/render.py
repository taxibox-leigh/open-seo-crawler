from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any

from .config import ScannerConfig
from .models import RenderedPage


BrowserFactory = Callable[[], Any]


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
                for url in list(dict.fromkeys(urls))[: config.max_rendered_pages]:
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
    page = browser.new_page()

    def on_console(message: Any) -> None:
        if message.type == "error" and len(console_errors) < config.max_render_events_per_page:
            console_errors.append(message.text)

    def on_request_failed(request: Any) -> None:
        if len(failed_requests) < config.max_render_events_per_page:
            failure = request.failure
            error_text = failure.get("errorText", "") if isinstance(failure, dict) else str(failure or "")
            failed_requests.append({"url": request.url, "error": error_text})

    def on_response(response: Any) -> None:
        if response.status >= 400 and len(failed_requests) < config.max_render_events_per_page:
            failed_requests.append({"url": response.url, "status": response.status})

    page.on("console", on_console)
    page.on("requestfailed", on_request_failed)
    page.on("response", on_response)
    error = ""
    final_url = ""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=config.render_navigation_timeout_ms)
        if config.render_settle_ms:
            page.wait_for_timeout(config.render_settle_ms)
        final_url = page.url
    except Exception as exc:
        error = str(exc)
        final_url = getattr(page, "url", "")
    finally:
        page.close()
    return RenderedPage(
        url=url,
        final_url=final_url,
        duration_ms=round((time.monotonic() - started) * 1000),
        console_errors=console_errors,
        failed_requests=failed_requests,
        error=error,
    )
