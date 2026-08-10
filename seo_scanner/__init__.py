"""Automation-friendly technical SEO scanner core."""

from .config import ScannerConfig
from .runner import Scanner

__all__ = ["Scanner", "ScannerConfig"]
__version__ = "0.1.0"
