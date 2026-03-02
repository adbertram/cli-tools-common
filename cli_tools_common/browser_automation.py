"""Shared browser automation module for CLI tools.

Provides a base class that handles:
- Interactive login via persistent browser profiles (playwright CLI)
- Session management via named sessions (--session flag)
- Headless automation via CLIPage wrapper

CLI tools subclass BrowserAutomation and set class-level hooks::

    class MyBrowser(BrowserAutomation):
        LOGIN_URL = "https://example.com/login"
        AUTH_CHECK_URL = "https://example.com/dashboard"
        AUTH_URL_PATTERN = r"/login"
        SESSION_NAME = "mysite"
"""

import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .cli_page import CLIPage


class BrowserAutomationError(Exception):
    """Browser automation error."""

    def __init__(self, message: str, cause: Exception = None):
        self.message = message
        self.cause = cause
        super().__init__(message)


class BrowserAutomation:
    """Base class for browser automation in CLI tools.

    Uses the ``playwright`` CLI with named sessions for per-tool isolation.
    Persistent browser profiles handle session persistence automatically —
    no need for cookie/storage capture and restore.
    """

    # --- Class-level hooks (subclasses override) ---
    LOGIN_URL = ""
    AUTH_CHECK_URL = ""
    AUTH_URL_PATTERN = ""        # Regex — URL matches → user is on login page
    AUTH_COOKIE_PATTERNS = []    # Cookie name regexes indicating auth
    AUTH_SUCCESS_URL = ""        # URL pattern indicating successful login
    AUTH_SUCCESS_SELECTOR = ""   # Playwright selector visible when authenticated
    LOGIN_TIMEOUT = 300          # Seconds to wait for manual login
    SESSION_NAME = ""            # Named session for playwright --session flag

    def __init__(self, config):
        self.config = config
        self._page: Optional[CLIPage] = None

    # --- Config accessors ---

    def _get_browser_data_dir(self) -> Path:
        if hasattr(self.config, "get_browser_data_dir"):
            return self.config.get_browser_data_dir()
        if hasattr(self.config, "browser_data_dir"):
            d = self.config.browser_data_dir
            return d if isinstance(d, Path) else Path(d)
        raise BrowserAutomationError(
            "Config must provide get_browser_data_dir() or browser_data_dir"
        )

    def _session_name(self) -> str:
        if self.SESSION_NAME:
            return self.SESSION_NAME
        return self.config.__class__.__name__.lower().replace("config", "")

    def _run_cli(self, args: list, timeout: int = 30, check: bool = True) -> subprocess.CompletedProcess:
        """Run a playwright CLI command via the shared CLIPage subprocess runner."""
        from .cli_page import CLIPageError
        try:
            return CLIPage(self._session_name())._run(args, timeout=timeout, check=check)
        except CLIPageError as e:
            raise BrowserAutomationError(str(e)) from e

    def _marker_path(self) -> Path:
        return self._get_browser_data_dir() / "profile.json"

    def _clear_stale_lock(self) -> None:
        """Remove stale Chrome SingletonLock files from the persistent profile.

        Chromium writes a SingletonLock symlink into the user-data dir while
        running.  If the daemon exits ungracefully, the lock stays behind
        and prevents the next ``browser open --persistent`` from succeeding.
        """
        profiles_dir = Path.home() / "Library" / "Caches" / "ms-playwright" / "daemon"
        session = self._session_name()
        for lock in profiles_dir.glob(f"*/ud-{session}-*/SingletonLock"):
            try:
                lock.unlink()
            except OSError:
                pass

    # ==================== Public Interface ====================

    def is_authenticated(self) -> bool:
        return self._marker_path().exists()

    def authenticate(self, force: bool = False):
        """Interactive login via headed persistent browser.

        Opens a visible browser window for the user to log in manually.
        Polls the page URL until login is detected (URL no longer matches
        the login page pattern), then closes the browser and writes a
        session marker file.
        """
        if self.has_session() and not force:
            return

        if force:
            self.clear_session()

        print(f"Opening browser for login at: {self.LOGIN_URL}", file=sys.stderr)
        print("Please log in in the browser window...", file=sys.stderr)

        # browser open is non-blocking — it launches the browser and returns
        self._run_cli(
            ["browser", "open", "--headed", "--persistent", self.LOGIN_URL],
            timeout=30,
            check=False,
        )

        # Poll page URL until login is detected
        page = CLIPage(self._session_name())
        deadline = time.time() + self.LOGIN_TIMEOUT
        while time.time() < deadline:
            time.sleep(2)
            try:
                if not self._is_login_page(page):
                    break
            except Exception:
                # Browser may have been closed by user — check if session is gone
                break
        else:
            self.close()
            raise BrowserAutomationError(
                f"Login timed out after {self.LOGIN_TIMEOUT}s"
            )

        # Give cookies a moment to persist
        time.sleep(2)

        # Close the headed browser
        self.close()

        # Write marker
        marker = self._marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({
            "session": self._session_name(),
            "authenticated": True,
        }))

        # Post-auth hook — open headless page so subclass can extract tokens etc.
        try:
            page = self.get_page(self.AUTH_CHECK_URL)
            page.wait_for_timeout(2000)
            self._on_authenticated(page)
        except Exception:
            pass
        finally:
            self.close()

        print("Authentication complete.", file=sys.stderr)

    def get_page(self, url: str = None) -> CLIPage:
        """Get a CLIPage backed by a persistent browser session.

        On first call, opens a headless browser with the persistent profile
        and navigates to *url* (or ``AUTH_CHECK_URL``).  If the daemon is
        already running, reuses it.  Subsequent calls return the same page;
        if *url* is given it navigates there first.
        """
        if self._page is not None:
            if url:
                self._page.goto(url)
            return self._page

        if not self.has_session():
            raise BrowserAutomationError(
                "No session exists. Call authenticate() first."
            )

        target_url = url or self.AUTH_CHECK_URL

        # Check if daemon is already running by trying a simple eval.
        # If it works, the session is live — just reuse it.
        page = CLIPage(self._session_name())
        try:
            page._run(["page", "eval", "1"], timeout=5)
            # Daemon is running — navigate if needed
            if url:
                page.goto(url)
            self._page = page
            return self._page
        except Exception:
            pass

        # No running daemon — open a new one.
        # Clear stale Chrome singleton lock if present (left behind by
        # ungraceful daemon shutdown) to avoid "Browser is already in use".
        self._clear_stale_lock()

        self._run_cli(
            ["browser", "open", "--persistent", target_url],
            timeout=30,
        )

        self._page = CLIPage(self._session_name())
        return self._page

    def has_session(self) -> bool:
        return self._marker_path().exists()

    def clear_session(self) -> None:
        marker = self._marker_path()
        if marker.exists():
            marker.unlink()
        self._run_cli(["data", "delete"], check=False, timeout=10)

    def login(self, force: bool = False) -> Dict[str, Any]:
        """Interactive login returning the dict expected by ``create_auth_app``."""
        try:
            self.authenticate(force=force)
            return {"success": True, "message": "Session saved. Browser closed."}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def close(self) -> None:
        self._run_cli(["browser", "close"], check=False, timeout=10)
        self._page = None

    def test_session(self) -> Dict[str, Any]:
        """Headless verification — navigate to AUTH_CHECK_URL and check auth."""
        if not self.has_session():
            return {"authenticated": False, "error": "No session file exists"}

        try:
            page = self.get_page(self.AUTH_CHECK_URL)
            page.wait_for_timeout(2000)
            authenticated = self._is_authenticated_page(page)
            result = {"authenticated": authenticated, "url": page.url}
            self.close()
            return result
        except Exception as e:
            return {"authenticated": False, "error": str(e)}

    # ==================== Overridable Hooks ====================

    def _is_login_page(self, page) -> bool:
        if self.AUTH_URL_PATTERN:
            return bool(re.search(self.AUTH_URL_PATTERN, page.url))
        return False

    def _is_authenticated_page(self, page) -> bool:
        if self.AUTH_SUCCESS_SELECTOR:
            try:
                return page.locator(self.AUTH_SUCCESS_SELECTOR).first.is_visible(timeout=500)
            except Exception:
                return False
        if self.AUTH_SUCCESS_URL:
            return bool(re.search(self.AUTH_SUCCESS_URL, page.url))
        return not self._is_login_page(page)

    def _on_authenticated(self, page) -> None:
        """Called after successful authentication with a headless CLIPage.

        Override to extract tokens, cookies, or other post-login state.
        """
        pass

    def _get_auth_cookies(self, cookies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not self.AUTH_COOKIE_PATTERNS:
            return cookies
        now = time.time()
        auth_cookies = []
        for cookie in cookies:
            name = cookie.get("name", "")
            expires = cookie.get("expires", -1)
            if 0 < expires < now:
                continue
            for pattern in self.AUTH_COOKIE_PATTERNS:
                if re.search(pattern, name, re.IGNORECASE):
                    auth_cookies.append(cookie)
                    break
        return auth_cookies

    # ==================== Context Manager ====================

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False
