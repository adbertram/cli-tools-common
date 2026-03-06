"""Selector parsing utilities for Playwright-style selectors."""

from typing import List, Optional


def _is_playwright_selector(selector: str) -> bool:
    return selector.startswith("text=") or ":has-text(" in selector


def _split_selector(selector: str) -> List[str]:
    parts: List[str] = []
    depth = 0
    current: List[str] = []
    in_quote: Optional[str] = None
    for ch in selector:
        if in_quote:
            current.append(ch)
            if ch == in_quote:
                in_quote = None
        elif ch in ('"', "'"):
            current.append(ch)
            in_quote = ch
        elif ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts
