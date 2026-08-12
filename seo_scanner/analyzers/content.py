from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from ..models import ImageReference, LinkReference
from ..scope import normalize_url
from .alt_text import classify_alt


@dataclass
class PageContent:
    title: str = ""
    meta_description: str = ""
    h1s: list[str] = field(default_factory=list)
    word_count: int = 0
    viewport: bool = False
    og_title: str = ""
    og_description: str = ""
    og_image: str = ""
    og_url: str = ""
    twitter_card: str = ""
    twitter_image: str = ""
    images: list[ImageReference] = field(default_factory=list)
    visible_text_hash: str = ""
    visible_text_fingerprint: str = ""
    links: list[LinkReference] = field(default_factory=list)
    heading_levels: list[int] = field(default_factory=list)


def extract_page_content(html: str, base_url: str = "") -> PageContent:
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    description = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    meta_description = str(description.get("content", "")).strip() if description else ""
    h1s = [item.get_text(" ", strip=True) for item in soup.find_all("h1")]
    h1s = [item for item in h1s if item]
    viewport = soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)}) is not None
    og_title = _meta(soup, "property", "og:title")
    og_description = _meta(soup, "property", "og:description")
    og_image = normalize_url(base_url, _meta(soup, "property", "og:image")) or ""
    og_url = normalize_url(base_url, _meta(soup, "property", "og:url")) or ""
    twitter_card = _meta(soup, "name", "twitter:card")
    twitter_image = normalize_url(base_url, _meta(soup, "name", "twitter:image")) or ""
    images: list[ImageReference] = []
    for image in soup.find_all("img"):
        raw_url = image.get("src") or image.get("data-src") or image.get("data-lazy-src") or _first_srcset(image.get("srcset") or image.get("data-srcset") or "")
        image_url = normalize_url(base_url, str(raw_url)) if raw_url else None
        if image_url:
            picture = image.find_parent("picture")
            images.append(ImageReference(
                image_url,
                str(image.get("alt", "")) if image.has_attr("alt") else None,
                _positive_int(image.get("width")), _positive_int(image.get("height")),
                bool(image.get("srcset") or image.get("data-srcset") or (picture and picture.select_one("source[srcset], source[data-srcset]"))),
                classify_alt(image, image_url),
            ))
    links: list[LinkReference] = []
    for anchor in soup.select("a[href]"):
        target = normalize_url(base_url, str(anchor.get("href", "")))
        if not target:
            continue
        text = anchor.get_text(" ", strip=True)
        if not text:
            image = anchor.find("img")
            text = str(image.get("alt", "")).strip() if image else ""
        rel = {str(value).lower() for value in anchor.get("rel", [])}
        links.append(LinkReference(target, " ".join(text.split()), "nofollow" in rel))
    heading_levels = [int(item.name[1]) for item in soup.find_all(re.compile(r"^h[1-6]$", re.I))]
    for element in soup(["script", "style", "noscript", "template", "svg"]):
        element.decompose()
    text = soup.get_text(" ", strip=True)
    words = re.findall(r"[^\W_]+(?:[’'-][^\W_]+)*", text, re.UNICODE)
    normalized_words = [word.casefold() for word in words]
    normalized_text = " ".join(normalized_words)
    return PageContent(
        title, meta_description, h1s, len(words), viewport, og_title,
        og_description, og_image, og_url, twitter_card, twitter_image, images,
        hashlib.sha256(normalized_text.encode("utf-8")).hexdigest() if normalized_text else "",
        _simhash(normalized_words), links, heading_levels,
    )


def _meta(soup: BeautifulSoup, attribute: str, value: str) -> str:
    element = soup.find("meta", attrs={attribute: re.compile(f"^{re.escape(value)}$", re.I)})
    if element is None:
        fallback = "name" if attribute == "property" else "property"
        element = soup.find("meta", attrs={fallback: re.compile(f"^{re.escape(value)}$", re.I)})
    return str(element.get("content", "")).strip() if element else ""


def _first_srcset(value: str) -> str:
    return value.split(",", 1)[0].strip().split(" ", 1)[0]


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(str(value))
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _simhash(words: list[str]) -> str:
    if not words:
        return ""
    tokens = [" ".join(words[index:index + 3]) for index in range(max(1, len(words) - 2))]
    weights = [0] * 64
    for token in tokens:
        digest = int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:8], "big")
        for bit in range(64):
            weights[bit] += 1 if digest & (1 << bit) else -1
    return f"{sum(1 << bit for bit, weight in enumerate(weights) if weight >= 0):016x}"
