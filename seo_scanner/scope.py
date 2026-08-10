from __future__ import annotations

from posixpath import normpath
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit


def normalize_url(base_url: str, value: str) -> str | None:
    value = value.strip()
    if not value or value.startswith(("data:", "javascript:", "mailto:", "tel:", "blob:")):
        return None
    absolute, _ = urldefrag(urljoin(base_url, value))
    parts = urlsplit(absolute)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    port = parts.port
    netloc = host if not port or (parts.scheme == "http" and port == 80) or (parts.scheme == "https" and port == 443) else f"{host}:{port}"
    path = normpath(parts.path or "/")
    if parts.path.endswith("/") and not path.endswith("/"):
        path += "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def same_origin(left: str, right: str) -> bool:
    a, b = urlsplit(left), urlsplit(right)
    return (a.scheme.lower(), a.hostname, a.port or (443 if a.scheme == "https" else 80)) == (b.scheme.lower(), b.hostname, b.port or (443 if b.scheme == "https" else 80))
