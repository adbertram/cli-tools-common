"""Browser automation sub-package.

Public API:
- PlaywrightServiceError  — exception for all service errors
- _DAEMON_PROFILES_DIR    — path to persistent browser profiles
- PlaywrightService       — unified browser automation service (lazy-loaded)
"""

import platform
from pathlib import Path


class PlaywrightServiceError(Exception):
    """Error from PlaywrightService operations."""


def _get_profiles_dir() -> Path:
    """Return the platform-appropriate persistent browser profiles directory."""
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright" / "daemon"
    elif system == "Windows":
        local_app = Path.home() / "AppData" / "Local"
        return local_app / "ms-playwright" / "daemon"
    else:
        # Linux / WSL
        xdg = Path.home() / ".cache"
        return xdg / "ms-playwright" / "daemon"


_DAEMON_PROFILES_DIR = _get_profiles_dir()


def __getattr__(name):
    if name == "PlaywrightService":
        from .service import PlaywrightService
        return PlaywrightService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
