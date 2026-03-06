"""Browser automation sub-package.

Public API:
- PlaywrightServiceError  — exception for all service errors
- _DAEMON_PROFILES_DIR    — path to persistent browser profiles
- PlaywrightService       — unified browser automation service (lazy-loaded)
"""

from pathlib import Path


class PlaywrightServiceError(Exception):
    """Error from PlaywrightService operations."""


_DAEMON_PROFILES_DIR = Path.home() / "Library" / "Caches" / "ms-playwright" / "daemon"


def __getattr__(name):
    if name == "PlaywrightService":
        from .service import PlaywrightService
        return PlaywrightService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
