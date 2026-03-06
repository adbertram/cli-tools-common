"""Backward-compatible re-exports from browser sub-package."""
from .browser import PlaywrightServiceError, PlaywrightService, _DAEMON_PROFILES_DIR

__all__ = ["PlaywrightService", "PlaywrightServiceError", "_DAEMON_PROFILES_DIR"]
