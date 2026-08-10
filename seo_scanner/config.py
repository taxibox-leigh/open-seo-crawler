from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any


@dataclass(frozen=True)
class ScannerConfig:
    max_pages: int = 2000
    max_resources: int = 10000
    max_total_bytes: int = 1_000_000_000
    max_resource_bytes: int = 25_000_000
    max_resource_size: int = 2_000_000
    timeout_seconds: float = 20.0
    max_duration_seconds: float = 3600.0
    user_agent: str = "open-seo-crawler/0.1 (+https://github.com/puneetindersingh/open-seo-crawler)"
    follow_external_resources: bool = False

    def __post_init__(self) -> None:
        positive = ("max_pages", "max_resources", "max_total_bytes", "max_resource_bytes", "max_resource_size", "timeout_seconds", "max_duration_seconds")
        for name in positive:
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be greater than zero")

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "ScannerConfig":
        allowed = {item.name for item in fields(cls)}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
        return cls(**values)
