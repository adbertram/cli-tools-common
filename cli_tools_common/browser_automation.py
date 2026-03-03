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
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .cli_page import CLIPage

logger = logging.getLogger("cli_tools.browser_automation")


def _setup_debug_logging():
    """Configure debug logging to stderr when DEBUG=1 or CLI_TOOLS_DEBUG=1."""
    if os.environ.get("DEBUG") == "1" or os.environ.get("CLI_TOOLS_DEBUG") == "1":
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            "[%(name)s] %(levelname)s: %(message)s"
        ))
        logger.setLevel(logging.DEBUG)
        if not logger.handlers:
            logger.addHandler(handler)


_setup_debug_logging()


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

    def _run_cli(self, args: list, timeout: int = 30, check: bool = True) -> "subprocess.CompletedProcess":
        """Run a playwright CLI command via the shared CLIPage subprocess runner."""
        import subprocess  # noqa: F811
        from .cli_page import CLIPageError
        session = self._session_name()
        logger.debug("_run_cli: session=%s args=%s timeout=%d check=%s", session, args, timeout, check)
        try:
            result = CLIPage(session)._run(args, timeout=timeout, check=check)
            logger.debug("_run_cli: returncode=%d stdout=%r stderr=%r",
                         result.returncode,
                         (result.stdout or "")[:500],
                         (result.stderr or "")[:500])
            return result
        except CLIPageError as e:
            logger.debug("_run_cli: CLIPageError: %s", e)
            raise BrowserAutomationError(str(e)) from e

    def _marker_path(self) -> Path:
        p = self._get_browser_data_dir() / "profile.json"
        logger.debug("_marker_path: %s (exists=%s)", p, p.exists())
        return p

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
        marker = self._marker_path()
        exists = marker.exists()
        logger.debug("is_authenticated: marker=%s exists=%s", marker, exists)
        if exists:
            try:
                content = marker.read_text()
                logger.debug("is_authenticated: marker content=%s", content[:500])
            except Exception as e:
                logger.debug("is_authenticated: could not read marker: %s", e)
        # Log profile directory contents
        try:
            data_dir = self._get_browser_data_dir()
            if data_dir.exists():
                files = list(data_dir.iterdir())
                logger.debug("is_authenticated: browser data dir=%s files=%s",
                             data_dir, [f.name for f in files])
            else:
                logger.debug("is_authenticated: browser data dir=%s does not exist", data_dir)
        except Exception as e:
            logger.debug("is_authenticated: could not list browser data dir: %s", e)
        return exists

    def authenticate(self, force: bool = False):
        """Interactive login via headed persistent browser.

        Opens a visible browser window for the user to log in manually.
        Polls the page URL until login is detected (URL no longer matches
        the login page pattern), then closes the browser and writes a
        session marker file.
        """
        logger.debug("authenticate: force=%s session=%s", force, self._session_name())
        logger.debug("authenticate: LOGIN_URL=%s AUTH_CHECK_URL=%s AUTH_URL_PATTERN=%s",
                     self.LOGIN_URL, self.AUTH_CHECK_URL, self.AUTH_URL_PATTERN)

        if self.has_session() and not force:
            logger.debug("authenticate: session already exists, skipping (force=False)")
            return

        if force:
            logger.debug("authenticate: force=True, clearing existing session")
            self.clear_session()

        print(f"Opening browser for login at: {self.LOGIN_URL}", file=sys.stderr)
        print("Please log in in the browser window...", file=sys.stderr)

        # browser open is non-blocking — it launches the browser and returns
        logger.debug("authenticate: launching headed persistent browser -> %s", self.LOGIN_URL)
        result = self._run_cli(
            ["browser", "open", "--headed", "--persistent", self.LOGIN_URL],
            timeout=30,
            check=False,
        )

        # Verify the browser actually launched — if the underlying CLI is
        # missing or crashed, ``result`` will have a non-zero return code.
        if result.returncode != 0:
            error_detail = (result.stderr or result.stdout or "").strip()
            logger.debug("authenticate: browser open FAILED rc=%d detail=%r",
                         result.returncode, error_detail)
            raise BrowserAutomationError(
                f"Failed to open browser: {error_detail or 'unknown error'}"
            )
        logger.debug("authenticate: browser open succeeded")

        # Poll page URL until login is detected
        page = CLIPage(self._session_name())
        deadline = time.time() + self.LOGIN_TIMEOUT
        poll_count = 0
        while time.time() < deadline:
            time.sleep(2)
            poll_count += 1
            try:
                is_login = self._is_login_page(page)
                logger.debug("authenticate: poll #%d url=%r is_login_page=%s",
                             poll_count, page.url, is_login)
                if not is_login:
                    logger.debug("authenticate: login detected (no longer on login page)")
                    break
            except Exception as e:
                # Browser may have been closed by user — check if session is gone
                logger.debug("authenticate: poll exception (browser closed?): %s", e)
                break
        else:
            logger.debug("authenticate: login timed out after %ds", self.LOGIN_TIMEOUT)
            self.close()
            raise BrowserAutomationError(
                f"Login timed out after {self.LOGIN_TIMEOUT}s"
            )

        # Give cookies a moment to persist
        logger.debug("authenticate: waiting 2s for cookies to persist")
        time.sleep(2)

        # Close the headed browser
        logger.debug("authenticate: closing headed browser")
        self.close()

        # Write marker
        marker = self._marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker_data = json.dumps({
            "session": self._session_name(),
            "authenticated": True,
        })
        marker.write_text(marker_data)
        logger.debug("authenticate: wrote marker file %s: %s", marker, marker_data)

        # Post-auth hook — open headless page so subclass can extract tokens etc.
        logger.debug("authenticate: running post-auth hook (_on_authenticated) with AUTH_CHECK_URL=%s",
                     self.AUTH_CHECK_URL)
        try:
            page = self.get_page(self.AUTH_CHECK_URL)
            page.wait_for_timeout(2000)
            current_url = page.url
            logger.debug("authenticate: post-auth page url=%s", current_url)
            self._on_authenticated(page)
            logger.debug("authenticate: _on_authenticated completed successfully")
        except Exception as e:
            logger.debug("authenticate: post-auth hook exception (swallowed): %s", e)
        finally:
            self.close()

        logger.debug("authenticate: complete")
        print("Authentication complete.", file=sys.stderr)

    def get_page(self, url: str = None) -> CLIPage:
        """Get a CLIPage backed by a persistent browser session.

        On first call, opens a headless browser with the persistent profile
        and navigates to *url* (or ``AUTH_CHECK_URL``).  If the daemon is
        already running, reuses it.  Subsequent calls return the same page;
        if *url* is given it navigates there first.
        """
        logger.debug("get_page: url=%s has_existing_page=%s", url, self._page is not None)

        if self._page is not None:
            if url:
                logger.debug("get_page: reusing existing page, navigating to %s", url)
                self._page.goto(url)
            return self._page

        if not self.has_session():
            logger.debug("get_page: no session exists, raising error")
            raise BrowserAutomationError(
                "No session exists. Call authenticate() first."
            )

        target_url = url or self.AUTH_CHECK_URL
        logger.debug("get_page: target_url=%s", target_url)

        # Check if daemon is already running by trying a simple eval.
        # If it works, the session is live — just reuse it.
        page = CLIPage(self._session_name())
        try:
            logger.debug("get_page: probing for existing daemon (page eval 1)")
            page._run(["page", "eval", "1"], timeout=5)
            logger.debug("get_page: daemon is running, reusing session")
            # Daemon is running — navigate if needed
            if url:
                page.goto(url)
            self._page = page
            return self._page
        except Exception as e:
            logger.debug("get_page: no running daemon (%s), opening new one", e)

        # No running daemon — open a new one.
        # Clear stale Chrome singleton lock if present (left behind by
        # ungraceful daemon shutdown) to avoid "Browser is already in use".
        self._clear_stale_lock()

        logger.debug("get_page: opening headless persistent browser -> %s", target_url)
        self._run_cli(
            ["browser", "open", "--persistent", target_url],
            timeout=30,
        )

        self._page = CLIPage(self._session_name())
        logger.debug("get_page: headless browser opened successfully")
        return self._page

    def has_session(self) -> bool:
        result = self._marker_path().exists()
        logger.debug("has_session: %s", result)
        return result

    def clear_session(self) -> None:
        marker = self._marker_path()
        logger.debug("clear_session: removing marker %s (exists=%s)", marker, marker.exists())
        if marker.exists():
            marker.unlink()
        logger.debug("clear_session: deleting playwright session data")
        self._run_cli(["data", "delete"], check=False, timeout=10)

    def login(self, force: bool = False) -> Dict[str, Any]:
        """Interactive login returning the dict expected by ``create_auth_app``."""
        logger.debug("login: force=%s", force)
        try:
            self.authenticate(force=force)
            logger.debug("login: authenticate succeeded")
            return {"success": True, "message": "Session saved. Browser closed."}
        except Exception as e:
            logger.debug("login: authenticate failed: %s", e)
            return {"success": False, "message": str(e)}

    def close(self) -> None:
        logger.debug("close: closing browser session")
        self._run_cli(["browser", "close"], check=False, timeout=10)
        self._page = None

    def test_session(self) -> Dict[str, Any]:
        """Headless verification — navigate to AUTH_CHECK_URL and check auth."""
        logger.debug("test_session: AUTH_CHECK_URL=%s", self.AUTH_CHECK_URL)
        if not self.has_session():
            logger.debug("test_session: no session file, returning unauthenticated")
            return {"authenticated": False, "error": "No session file exists"}

        try:
            page = self.get_page(self.AUTH_CHECK_URL)
            page.wait_for_timeout(2000)
            current_url = page.url
            logger.debug("test_session: page loaded, url=%s", current_url)
            authenticated = self._is_authenticated_page(page)
            logger.debug("test_session: _is_authenticated_page=%s", authenticated)
            result = {"authenticated": authenticated, "url": current_url}
            self.close()
            return result
        except Exception as e:
            logger.debug("test_session: exception: %s", e)
            return {"authenticated": False, "error": str(e)}

    # ==================== Overridable Hooks ====================

    def _is_login_page(self, page) -> bool:
        url = page.url
        if self.AUTH_URL_PATTERN:
            result = bool(re.search(self.AUTH_URL_PATTERN, url))
            logger.debug("_is_login_page: url=%s pattern=%r match=%s", url, self.AUTH_URL_PATTERN, result)
            return result
        logger.debug("_is_login_page: no AUTH_URL_PATTERN, returning False")
        return False

    def _is_authenticated_page(self, page) -> bool:
        url = page.url
        if self.AUTH_SUCCESS_SELECTOR:
            try:
                visible = page.locator(self.AUTH_SUCCESS_SELECTOR).first.is_visible(timeout=500)
                logger.debug("_is_authenticated_page: url=%s selector=%r visible=%s",
                             url, self.AUTH_SUCCESS_SELECTOR, visible)
                return visible
            except Exception as e:
                logger.debug("_is_authenticated_page: selector check failed: %s", e)
                return False
        if self.AUTH_SUCCESS_URL:
            result = bool(re.search(self.AUTH_SUCCESS_URL, url))
            logger.debug("_is_authenticated_page: url=%s success_url_pattern=%r match=%s",
                         url, self.AUTH_SUCCESS_URL, result)
            return result
        result = not self._is_login_page(page)
        logger.debug("_is_authenticated_page: fallback (not login page) = %s", result)
        return result

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
