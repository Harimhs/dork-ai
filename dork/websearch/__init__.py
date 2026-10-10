"""Dork Search — Dork's own web search pipeline (engine.py) and GitHub reader (browse.py)."""

from .browse import github
from .engine import age_text, read_page, search

__all__ = ["age_text", "github", "read_page", "search"]
