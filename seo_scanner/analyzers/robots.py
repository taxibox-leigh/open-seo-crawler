from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..models import RobotsDocument


@dataclass
class RobotsPolicy:
    document: RobotsDocument
    groups: list[tuple[list[str], list[tuple[bool, str]]]]

    def allows(self, url: str) -> bool:
        agent = self.document.user_agent.casefold()
        matching: list[tuple[int, list[tuple[bool, str]]]] = []
        for agents, rules in self.groups:
            specificity = max((len(item) for item in agents if item != "*" and agent.startswith(item)), default=-1)
            if specificity < 0 and "*" in agents:
                specificity = 0
            if specificity >= 0:
                matching.append((specificity, rules))
        if not matching:
            return True
        best_agent = max(item[0] for item in matching)
        split = urlsplit(url)
        path = split.path or "/"
        if split.query:
            path += "?" + split.query
        matches: list[tuple[int, bool]] = []
        for specificity, rules in matching:
            if specificity != best_agent:
                continue
            for allowed, pattern in rules:
                if pattern and _matches(pattern, path):
                    matches.append((len(pattern.rstrip("$")), allowed))
        if not matches:
            return True
        longest = max(item[0] for item in matches)
        return any(allowed for length, allowed in matches if length == longest)


def parse_robots(url: str, body: bytes, user_agent: str) -> RobotsPolicy:
    errors: list[str] = []
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        text = body.decode("utf-8-sig", errors="replace")
        errors.append(f"robots.txt is not valid UTF-8: {exc}")
    recognized = {"user-agent", "allow", "disallow", "sitemap", "crawl-delay", "request-rate"}
    for number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" in line:
            continue
        if line.casefold().split(None, 1)[0] in recognized:
            errors.append(f"Line {number} is missing a colon: {raw_line.strip()}")

    groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[bool, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        name, value = (item.strip() for item in line.split(":", 1))
        name = name.casefold()
        if name == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.casefold())
        elif name in {"allow", "disallow"} and agents:
            rules.append((name == "allow", value))
    if agents:
        groups.append((agents, rules))
    return RobotsPolicy(RobotsDocument(url=url, status=200, user_agent=user_agent, errors=errors), groups)


def _matches(pattern: str, path: str) -> bool:
    terminal = pattern.endswith("$")
    value = pattern[:-1] if terminal else pattern
    expression = "^" + re.escape(value).replace(r"\*", ".*")
    if terminal:
        expression += "$"
    return re.search(expression, path) is not None
