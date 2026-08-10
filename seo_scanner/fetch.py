from __future__ import annotations

import time
from dataclasses import dataclass

import requests


@dataclass
class FetchResponse:
    requested_url: str
    final_url: str
    status: int
    content_type: str
    body: bytes
    declared_bytes: int | None
    duration_ms: int
    redirect_hops: list[str]
    truncated: bool
    headers: dict[str, str]


class Fetcher:
    def __init__(self, user_agent: str, timeout_seconds: float) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate, br"})
        self.timeout_seconds = timeout_seconds

    def get(self, url: str, max_bytes: int) -> FetchResponse:
        started = time.monotonic()
        with self.session.get(url, timeout=self.timeout_seconds, stream=True, allow_redirects=True) as response:
            declared_header = response.headers.get("Content-Length")
            declared = int(declared_header) if declared_header and declared_header.isdigit() else None
            chunks: list[bytes] = []
            size = 0
            truncated = False
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                remaining = max_bytes - size
                if len(chunk) > remaining:
                    chunks.append(chunk[:remaining])
                    size = max_bytes
                    truncated = True
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size >= max_bytes:
                    truncated = declared is None or declared > max_bytes
                    break
            return FetchResponse(
                requested_url=url,
                final_url=response.url,
                status=response.status_code,
                content_type=response.headers.get("Content-Type", "").split(";", 1)[0].lower(),
                body=b"".join(chunks),
                declared_bytes=declared,
                duration_ms=round((time.monotonic() - started) * 1000),
                redirect_hops=[item.url for item in response.history] + ([response.url] if response.history else []),
                truncated=truncated,
                headers={key.lower(): value for key, value in response.headers.items()},
            )

    def head(self, url: str) -> FetchResponse:
        started = time.monotonic()
        with self.session.head(url, timeout=self.timeout_seconds, allow_redirects=True) as response:
            declared = response.headers.get("Content-Length")
            return FetchResponse(
                requested_url=url, final_url=response.url, status=response.status_code,
                content_type=response.headers.get("Content-Type", "").split(";", 1)[0].lower(), body=b"",
                declared_bytes=int(declared) if declared and declared.isdigit() else None,
                duration_ms=round((time.monotonic() - started) * 1000),
                redirect_hops=[item.url for item in response.history] + ([response.url] if response.history else []),
                truncated=False, headers={key.lower(): value for key, value in response.headers.items()},
            )

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
