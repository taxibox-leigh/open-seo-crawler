from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup


@dataclass
class PageContent:
    title: str = ""
    meta_description: str = ""
    h1s: list[str] = field(default_factory=list)
    word_count: int = 0


def extract_page_content(html: str) -> PageContent:
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    description = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    meta_description = str(description.get("content", "")).strip() if description else ""
    h1s = [item.get_text(" ", strip=True) for item in soup.find_all("h1")]
    h1s = [item for item in h1s if item]
    for element in soup(["script", "style", "noscript", "template", "svg"]):
        element.decompose()
    text = soup.get_text(" ", strip=True)
    words = re.findall(r"[^\W_]+(?:[’'-][^\W_]+)*", text, re.UNICODE)
    return PageContent(title, meta_description, h1s, len(words))
