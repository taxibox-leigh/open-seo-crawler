"""Classify img alt attributes.

`alt=""` is valid HTML and the correct markup for a decorative image, so it
cannot be reported as a defect on its own. It is also what a template emits
when someone forgets to fill the field in, which is a real and common defect —
commercial auditors report those as missing alt text.

The split here follows the rendered crawler's heuristic: an empty alt is
treated as deliberate when the image is a third-party widget asset, when its
filename looks like a shape/icon/background export, or when it sits inside a
link or button that already carries its own accessible name. Everything else
with an empty alt is treated as content imagery whose alt text was forgotten.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

ALT_PRESENT = "present"
ALT_MISSING = "missing"
ALT_EMPTY_DECORATIVE = "empty_decorative"
ALT_EMPTY_CONTENT = "empty_content"

# Hosts whose images the site owner cannot write alt text for: consent banners,
# analytics beacons, chat widgets, captcha badges.
THIRD_PARTY_IMAGE_HOSTS = (
    "gstatic.com",
    "googletagmanager.com",
    "google-analytics.com",
    "googleadservices.com",
    "doubleclick.net",
    "hotjar.com",
    "intercomcdn.com",
    "intercom.io",
    "crisp.chat",
    "facebook.com",
    "facebook.net",
    "linkedin.com",
    "licdn.com",
    "bing.com",
    "clarity.ms",
    "hs-scripts.com",
    "hubspot.com",
    "zdassets.com",
    "zendesk.com",
)

_DECORATIVE_FILENAME_RE = re.compile(
    r"(?:"
    r"^(?:layer|group|mask[-_]?group|path|vector|rectangle|ellipse|frame|union|subtract|clip|component|line|polygon|oval|artboard)[-_ ]?\d*"
    r"|quotation|quote[-_]?mark"
    r"|(?:^|[-_/])(?:bg|background|backdrop|hero[-_]?bg|pattern|texture|noise|gradient|overlay|stripe|grid|mesh)(?:[-_]|$)"
    r"|(?:^|[-_/])(?:icon|ico|sprite|emoji|emote|bullet|chevron|caret|burger|hamburger|loader|spinner|placeholder|divider|separator|ornament|accent|swirl|squiggle|ribbon)(?:[-_]|$)"
    r"|(?:^|[-_/])(?:spacer|shim|blank|transparent|pixel|1x1)(?:[-_]|$)"
    r"|(?:^|[-_/])(?:star|sparkle|shape|blob|leaf|petal|circle|square|triangle)(?:[-_]|$)"
    r")",
    re.I,
)


def is_third_party_image(url: str) -> bool:
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    return any(host == known or host.endswith("." + known) for known in THIRD_PARTY_IMAGE_HOSTS)


def filename_looks_decorative(url: str) -> bool:
    if not url:
        return True
    path = urlparse(url).path or url
    name = path.rsplit("/", 1)[-1]
    if not name:
        return True
    name = re.sub(r"\.(svg|png|jpe?g|gif|webp|avif|ico|bmp)$", "", name, flags=re.I)
    name = re.sub(r"[-_]\d{2,4}x\d{2,4}$", "", name)
    name = re.sub(r"@\d+x$", "", name)
    name = re.sub(r"[-_]scaled$", "", name, flags=re.I)
    return bool(_DECORATIVE_FILENAME_RE.search(name))


def has_accessible_name(node) -> bool:
    """Whether a link/button carries its own name, per the W3C algorithm."""
    if node is None:
        return False
    for attribute in ("aria-label", "aria-labelledby", "title"):
        if str(node.get(attribute) or "").strip():
            return True
    if node.get_text(" ", strip=True):
        return True
    for descendant in node.find_all("img"):
        if str(descendant.get("alt") or "").strip():
            return True
        if str(descendant.get("title") or "").strip():
            return True
        if str(descendant.get("aria-label") or "").strip():
            return True
    return False


def classify_alt(image, image_url: str) -> str:
    """Return one of the ALT_* constants for a BeautifulSoup <img> tag."""
    if not image.has_attr("alt"):
        return ALT_MISSING
    if str(image.get("alt") or "").strip():
        return ALT_PRESENT
    # Empty alt from here on.
    if is_third_party_image(image_url):
        return ALT_EMPTY_DECORATIVE
    for attribute in ("title", "aria-label"):
        if str(image.get(attribute) or "").strip():
            return ALT_EMPTY_DECORATIVE
    if str(image.get("role") or "").strip().lower() == "presentation":
        return ALT_EMPTY_DECORATIVE
    if str(image.get("aria-hidden") or "").strip().lower() == "true":
        return ALT_EMPTY_DECORATIVE
    parent = image.find_parent(["a", "button"])
    if parent is not None and has_accessible_name(parent):
        return ALT_EMPTY_DECORATIVE
    if filename_looks_decorative(image_url):
        return ALT_EMPTY_DECORATIVE
    return ALT_EMPTY_CONTENT
