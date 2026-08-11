from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from ..models import ImageReference
from ..scope import normalize_url


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
    twitter_card: str = ""
    twitter_image: str = ""
    images: list[ImageReference] = field(default_factory=list)


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
    twitter_card = _meta(soup, "name", "twitter:card")
    twitter_image = normalize_url(base_url, _meta(soup, "name", "twitter:image")) or ""
    images: list[ImageReference] = []
    for image in soup.find_all("img"):
        raw_url = image.get("src") or image.get("data-src") or image.get("data-lazy-src") or _first_srcset(image.get("srcset") or image.get("data-srcset") or "")
        image_url = normalize_url(base_url, str(raw_url)) if raw_url else None
        if image_url:
            images.append(ImageReference(image_url, str(image.get("alt", "")) if image.has_attr("alt") else None))
    for element in soup(["script", "style", "noscript", "template", "svg"]):
        element.decompose()
    text = soup.get_text(" ", strip=True)
    words = re.findall(r"[^\W_]+(?:[’'-][^\W_]+)*", text, re.UNICODE)
    return PageContent(title, meta_description, h1s, len(words), viewport, og_title, og_description, og_image, twitter_card, twitter_image, images)


def _meta(soup: BeautifulSoup, attribute: str, value: str) -> str:
    element = soup.find("meta", attrs={attribute: re.compile(f"^{re.escape(value)}$", re.I)})
    if element is None:
        fallback = "name" if attribute == "property" else "property"
        element = soup.find("meta", attrs={fallback: re.compile(f"^{re.escape(value)}$", re.I)})
    return str(element.get("content", "")).strip() if element else ""


def _first_srcset(value: str) -> str:
    return value.split(",", 1)[0].strip().split(" ", 1)[0]
