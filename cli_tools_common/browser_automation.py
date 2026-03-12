"""Shared browser automation module for CLI tools.

Provides a base class that handles:
- Interactive login via persistent browser profiles (playwright CLI)
- Session management via named sessions (--session flag)
- Headless automation via PlaywrightService

CLI tools subclass BrowserAutomation and set class-level hooks::

    class MyBrowser(BrowserAutomation):
        LOGIN_URL = "https://example.com/login"
        AUTH_CHECK_URL = "https://example.com/dashboard"
        AUTH_URL_PATTERN = r"/login"
        SESSION_NAME = "mysite"
"""

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._debug_logging import configure_debug_logger
from .output import print_info, print_success
from .playwright_service import PlaywrightService, PlaywrightServiceError

logger = logging.getLogger("cli_tools.browser_automation")
configure_debug_logger(logger)


class BrowserAutomationError(Exception):
    """Browser automation error."""

    def __init__(self, message: str, cause: Exception = None):
        self.message = message
        self.cause = cause
        super().__init__(message)


class AuthResult:
    """Result of an authentication check. Truthy when authenticated."""
    __slots__ = ('authenticated', 'available', 'live_check')

    def __init__(self, authenticated: bool, live_check: bool, available: bool = None):
        self.authenticated = authenticated
        self.live_check = live_check
        # available defaults to same as authenticated when not explicitly set
        self.available = available if available is not None else authenticated

    def __bool__(self):
        return self.authenticated

    def __repr__(self):
        return (f"AuthResult(authenticated={self.authenticated}, "
                f"available={self.available}, live_check={self.live_check})")


class BrowserAutomation:
    """Base class for browser automation in CLI tools.

    Uses the ``playwright`` CLI with named sessions for per-tool isolation.
    Persistent browser profiles handle session persistence automatically —
    no need for cookie/storage capture and restore.
    """

    # --- Class-level hooks (subclasses override) ---
    AUTH_CHECK_TTL = 300  # Seconds to cache a successful auth check (0 = always live)
    LOGIN_URL = ""
    AUTH_CHECK_URL = ""
    AUTH_URL_PATTERN = ""        # Regex — URL matches → user is on login page
    AUTH_COOKIE_PATTERNS = []    # Cookie name regexes indicating auth
    AUTH_SUCCESS_URL = ""        # URL pattern indicating successful login
    AUTH_SUCCESS_SELECTOR = ""   # Playwright selector visible when authenticated
    AUTH_UNAVAILABLE_SELECTOR = ""  # Playwright selector — if visible, authenticated but not available
    AUTH_STORAGE_KEY = ""        # localStorage key; True if key exists and has a value
    LOGIN_TIMEOUT = 300          # Seconds to wait for manual login
    SESSION_NAME = ""            # Named session for playwright --session flag

    def __init__(self, config):
        self.config = config
        self._page: Optional[PlaywrightService] = None
        self._service: Optional[PlaywrightService] = None
        self._auth_verified_at: float = 0

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
        name = self.config.__class__.__name__.lower().replace("config", "")
        return name or "default"

    def _get_service(self) -> PlaywrightService:
        """Get a cached PlaywrightService instance for this session."""
        if self._service is None:
            self._service = PlaywrightService(self._session_name())
        return self._service

    def _marker_path(self) -> Path:
        p = self._get_browser_data_dir() / "profile.json"
        logger.debug("_marker_path: %s (exists=%s)", p, p.exists())
        return p

    def _read_marker(self) -> dict:
        """Read the session marker file, returning {} if missing or invalid."""
        marker = self._marker_path()
        if not marker.exists():
            return {}
        try:
            data = json.loads(marker.read_text())
            logger.debug("_read_marker: %s -> %s", marker, data)
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("_read_marker: failed to read %s: %s", marker, e)
            return {}

    def _write_marker(self, **extra) -> None:
        """Write the session marker file.

        Any keyword arguments are merged into the marker data (e.g.
        ``browser="chrome"`` to record which browser channel was used
        during authentication).
        """
        marker = self._marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker_data_dict = {
            "session": self._session_name(),
            "authenticated": True,
            "timestamp": time.time(),
        }
        marker_data_dict.update(extra)
        marker_data = json.dumps(marker_data_dict)
        marker.write_text(marker_data)
        logger.debug("_write_marker: wrote %s: %s", marker, marker_data)

    # ==================== Public Interface ====================

    def is_authenticated(self) -> AuthResult:
        """Check auth via live browser check, with TTL caching.

        Returns AuthResult (truthy/falsy) with .live_check indicating
        whether a live browser check was performed or the result was cached.
        """
        # TTL cache: return immediately if recently verified
        if self.AUTH_CHECK_TTL and self._auth_verified_at:
            elapsed = time.time() - self._auth_verified_at
            if elapsed < self.AUTH_CHECK_TTL:
                logger.debug("is_authenticated: cached=True (%.0fs ago, ttl=%ds)",
                             elapsed, self.AUTH_CHECK_TTL)
                return AuthResult(authenticated=True, live_check=False)

        if not self.AUTH_CHECK_URL:
            result = self.has_session()
            logger.debug("is_authenticated: no AUTH_CHECK_URL, falling back to has_session=%s", result)
            if result:
                self._auth_verified_at = time.time()
            return AuthResult(authenticated=result, live_check=True)

        try:
            page = self.get_page(self.AUTH_CHECK_URL)
            page.wait_for_timeout(2000)
            result = self._check_auth(page)
            available = self._check_available(page) if result else False
            logger.debug("is_authenticated: live check result=%s available=%s", result, available)
            if result:
                self._auth_verified_at = time.time()
            return AuthResult(authenticated=result, available=available, live_check=True)
        except Exception as e:
            logger.debug("is_authenticated: live check failed: %s", e)
            return AuthResult(authenticated=False, available=False, live_check=True)

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

        if self.is_authenticated() and not force:
            logger.debug("authenticate: session already exists, skipping (force=False)")
            return

        if force:
            logger.debug("authenticate: force=True, clearing existing session")
            self.clear_session()

        print_info(f"Opening browser for login at: {self.LOGIN_URL}")
        print_info("Log in, then close the browser when done.")

        # browser open is non-blocking — it launches the browser and returns
        logger.debug("authenticate: launching headed persistent browser -> %s", self.LOGIN_URL)
        svc = self._get_service()
        try:
            svc.browser_open(self.LOGIN_URL, persistent=True, headed=True, browser="chrome")
        except PlaywrightServiceError as e:
            logger.debug("authenticate: browser open FAILED: %s", e)
            raise BrowserAutomationError(f"Failed to open browser: {e}") from e
        logger.debug("authenticate: browser open succeeded")

        # Wait for the user to close the browser.  Periodically save state
        # so session-only cookies (no expiry) are captured before Chrome
        # discards them on exit.
        state_file = self._get_browser_data_dir() / "auth-state.json"
        poll_count = 0
        while True:
            time.sleep(2)
            poll_count += 1
            try:
                # Access the page to check if browser is still open.
                # _browser_context.pages is empty or throws when closed.
                pages = svc._browser_context.pages if svc._browser_context else []
                if not pages:
                    logger.debug("authenticate: poll #%d browser closed (no pages)", poll_count)
                    break
                # Save state while browser is still open
                try:
                    svc.state_save(str(state_file))
                    logger.debug("authenticate: poll #%d saved state (url=%s)",
                                 poll_count, svc.url)
                except Exception as e:
                    logger.debug("authenticate: poll #%d state_save failed: %s",
                                 poll_count, e)
            except Exception as e:
                logger.debug("authenticate: poll #%d browser closed (exception: %s)",
                             poll_count, e)
                break

        # Clean up — browser is already closed by the user
        logger.debug("authenticate: browser closed by user, finalizing")
        svc.clear_session_metadata()
        self._page = None
        self._service = None

        self._write_marker(browser="chrome")
        self._auth_verified_at = time.time()

        logger.debug("authenticate: complete")
        print_success("Authentication complete.")

    def get_page(self, url: str = None) -> PlaywrightService:
        """Get a PlaywrightService backed by a persistent browser session.

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

        svc = self._get_service()
        target_url = url or self.AUTH_CHECK_URL
        logger.debug("get_page: target_url=%s", target_url)

        fresh_session = False
        try:
            logger.debug("get_page: probing for existing daemon (page eval 1)")
            svc.page_eval("1", timeout=5)
            logger.debug("get_page: daemon is running, reusing session")

            # If the reused daemon is on a login page, try loading saved state
            state_file = self._get_browser_data_dir() / "auth-state.json"
            if state_file.exists():
                try:
                    current_url = svc.page_eval("window.location.href")
                    if self._is_login_page(current_url):
                        logger.debug("get_page: reused daemon on login page, loading saved auth state")
                        svc.state_load(str(state_file))
                        logger.debug("get_page: restored auth state into reused daemon")
                except Exception as e:
                    logger.debug("get_page: state_load into reused daemon failed (non-fatal): %s", e)
        except PlaywrightServiceError:
            # Check if saved auth state exists BEFORE opening the browser.
            # If it does, open without a URL (about:blank) so we can load
            # state before the first navigation.  Session-only cookies are
            # lost when Chrome closes; state-load restores them so the
            # first real page load sends the correct cookies.
            state_file = self._get_browser_data_dir() / "auth-state.json"
            has_state = state_file.exists()
            open_url = None if has_state else target_url

            # Read the browser channel from the marker file so headless
            # sessions use the same browser binary as the login session.
            # This avoids User-Agent / TLS fingerprint mismatches that
            # cause sites to treat the headless session as a new device.
            marker_data = self._read_marker()
            auth_browser = marker_data.get("browser")
            logger.debug("get_page: no running daemon, opening new one -> %s (has_state=%s, browser=%s)",
                         open_url or "about:blank", has_state, auth_browser)
            try:
                svc.browser_open(open_url, persistent=True, browser=auth_browser)
                fresh_session = True
            except PlaywrightServiceError as e:
                raise BrowserAutomationError(str(e)) from e

            if has_state:
                try:
                    svc.state_load(str(state_file))
                    logger.debug("get_page: restored auth state from %s", state_file)
                except Exception as e:
                    logger.debug("get_page: state_load failed (non-fatal): %s", e)

        if url:
            svc.goto(url)
        elif fresh_session and target_url:
            # Navigate to target URL if browser was opened to about:blank
            svc.goto(target_url)
        self._page = svc
        logger.debug("get_page: ready")
        return self._page

    def has_session(self) -> bool:
        result = self._marker_path().exists()
        logger.debug("has_session: %s", result)
        return result

    def clear_session(self) -> None:
        self._auth_verified_at = 0
        marker = self._marker_path()
        logger.debug("clear_session: removing marker %s (exists=%s)", marker, marker.exists())
        if marker.exists():
            marker.unlink()
        # Remove saved auth state file (cookies/localStorage snapshot)
        state_file = self._get_browser_data_dir() / "auth-state.json"
        if state_file.exists():
            state_file.unlink()
            logger.debug("clear_session: removed auth state file %s", state_file)
        logger.debug("clear_session: deleting playwright session data")
        try:
            self._get_service().data_delete()
        except PlaywrightServiceError:
            pass

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
        # Save refreshed auth state (cookies, localStorage) before closing
        # so that server-refreshed session cookies are preserved for the
        # next headless session.  Only save if we have an active page and
        # a session marker (i.e. we were authenticated).
        if self._page is not None and self._marker_path().exists():
            state_file = self._get_browser_data_dir() / "auth-state.json"
            try:
                self._get_service().state_save(str(state_file))
                logger.debug("close: saved refreshed auth state to %s", state_file)
            except Exception as e:
                logger.debug("close: state_save before close failed (non-fatal): %s", e)
        try:
            self._get_service().browser_close()
        except PlaywrightServiceError:
            pass
        self._page = None
        self._service = None

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
            authenticated = self._check_auth(page)
            logger.debug("test_session: _check_auth=%s", authenticated)
            result = {"authenticated": authenticated, "url": current_url}
            self.close()
            return result
        except Exception as e:
            logger.debug("test_session: exception: %s", e)
            return {"authenticated": False, "error": str(e)}

    # ==================== Overridable Hooks ====================

    def _is_login_page(self, page) -> bool:
        url = page.url
        return self._is_login_page_url(url)

    def _is_login_page_url(self, url: str) -> bool:
        if self.AUTH_URL_PATTERN:
            result = bool(re.search(self.AUTH_URL_PATTERN, url))
            logger.debug("_is_login_page_url: url=%s pattern=%r match=%s", url, self.AUTH_URL_PATTERN, result)
            return result
        return False

    def _check_auth(self, page) -> bool:
        """Check if page indicates authenticated state.

        Checks in priority order (first configured wins):
        0. AUTH_URL_PATTERN — if current URL matches, user is on a login/auth
           page and is NOT authenticated (even if cookies exist)
        1. AUTH_COOKIE_PATTERNS — auth cookies present in browser
        2. AUTH_SUCCESS_SELECTOR — DOM element visible on page
        3. AUTH_STORAGE_KEY — localStorage key exists and has a value
        4. AUTH_SUCCESS_URL — URL matches success pattern
        5. Fallback — current URL is not a login page
        """
        url = page.url

        # 0. Login/auth page check — takes priority over all other checks.
        if self._is_login_page(page):
            logger.debug("_check_auth: on login/auth page (url=%s), returning False", url)
            return False

        # 1. Cookie check
        if self.AUTH_COOKIE_PATTERNS:
            try:
                cookies = page.cookie_list()
                auth_cookies = self._get_auth_cookies(cookies)
                has_cookies = len(auth_cookies) > 0
                logger.debug("_check_auth: cookie check — %d auth cookies "
                             "(patterns=%s, total_cookies=%d)",
                             len(auth_cookies), self.AUTH_COOKIE_PATTERNS, len(cookies))
                return has_cookies
            except Exception as e:
                logger.debug("_check_auth: cookie check failed: %s", e)
                return False

        # 2. DOM element check
        if self.AUTH_SUCCESS_SELECTOR:
            try:
                visible = page.locator(self.AUTH_SUCCESS_SELECTOR).first.is_visible(timeout=500)
                logger.debug("_check_auth: url=%s selector=%r visible=%s",
                             url, self.AUTH_SUCCESS_SELECTOR, visible)
                return visible
            except Exception as e:
                logger.debug("_check_auth: selector check failed: %s", e)
                return False

        # 3. localStorage check
        if self.AUTH_STORAGE_KEY:
            try:
                items = page.localstorage_list()
                result = any(i['key'] == self.AUTH_STORAGE_KEY and i['value'] for i in items)
                logger.debug("_check_auth: localStorage key=%r found=%s", self.AUTH_STORAGE_KEY, result)
                return result
            except Exception as e:
                logger.debug("_check_auth: localStorage check failed: %s", e)
                return False

        # 4. Success URL pattern
        if self.AUTH_SUCCESS_URL:
            result = bool(re.search(self.AUTH_SUCCESS_URL, url))
            logger.debug("_check_auth: url=%s success_url_pattern=%r match=%s",
                         url, self.AUTH_SUCCESS_URL, result)
            return result

        # 5. Fallback: not on login page
        result = not self._is_login_page(page)
        logger.debug("_check_auth: fallback (not login page) = %s", result)
        return result

    def _check_available(self, page) -> bool:
        """Check if the page is available (no blocking elements).

        Returns False if AUTH_UNAVAILABLE_SELECTOR is set and visible,
        meaning the session is authenticated but the page requires
        additional verification (e.g. email confirmation).
        """
        if not self.AUTH_UNAVAILABLE_SELECTOR:
            return True
        try:
            visible = page.locator(self.AUTH_UNAVAILABLE_SELECTOR).first.is_visible(timeout=500)
            logger.debug("_check_available: selector=%r visible=%s → available=%s",
                         self.AUTH_UNAVAILABLE_SELECTOR, visible, not visible)
            return not visible
        except Exception as e:
            logger.debug("_check_available: check failed: %s (assuming available)", e)
            return True

    def _on_authenticated(self, page) -> None:
        """Called after successful authentication with a headless page.

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
