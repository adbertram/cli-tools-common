"""HAR-based HTTP response caching for browser automation.

Uses Playwright's built-in HAR recording/replay to cache HTTP responses.
Cache stored at {browser_data_dir}/cache.har per profile.

After recording, redirect entries (3xx) are stripped so replay serves final
200 responses directly without replaying auth redirect chains.
"""
import json
import time
from pathlib import Path

from .config import is_cache_enabled, get_cache_ttl


class HarCache:
    """Manages HAR file lifecycle for Playwright response caching.

    Args:
        cache_dir: Directory to store cache.har (typically browser data dir).
        ttl: Time-to-live in seconds. Stale HAR files are deleted.
    """

    def __init__(self, cache_dir: Path, ttl: int = 3600):
        self._cache_dir = Path(cache_dir)
        self._ttl = ttl

    @property
    def path(self) -> Path:
        """Path to the HAR cache file."""
        return self._cache_dir / "cache.har"

    def is_valid(self) -> bool:
        """Check if a non-stale HAR cache exists."""
        p = self.path
        return p.exists() and (time.time() - p.stat().st_mtime) < self._ttl

    def invalidate(self) -> None:
        """Delete the HAR cache file."""
        p = self.path
        if p.exists():
            p.unlink()

    def prepare_for_recording(self) -> Path:
        """Ensure directory exists, delete stale HAR, return path for recording."""
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and not self.is_valid():
            self.path.unlink()
        return self.path

    def clean(self) -> None:
        """Remove redirect (3xx) and failed (negative status) entries from HAR.

        Redirects poison replay by re-triggering auth flows.
        Failed entries (-1) represent cancelled/timed-out requests that
        should fall through to the network on replay.
        """
        p = self.path
        if not p.exists():
            return
        with open(p) as f:
            har = json.load(f)
        entries = har.get("log", {}).get("entries", [])
        before = len(entries)
        har["log"]["entries"] = [
            e for e in entries
            if e["response"]["status"] >= 200 and e["response"]["status"] < 300
            or e["response"]["status"] >= 400
        ]
        after = len(har["log"]["entries"])
        if after < before:
            with open(p, "w") as f:
                json.dump(har, f)

    @staticmethod
    def is_enabled() -> bool:
        """Check CACHE_ENABLED env var (default: true)."""
        return is_cache_enabled()

    @staticmethod
    def get_ttl_from_env() -> int:
        """Read CACHE_TTL env var (default: 3600)."""
        return get_cache_ttl()
