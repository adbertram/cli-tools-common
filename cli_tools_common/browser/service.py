"""PlaywrightService - unified browser automation service.

Uses the Playwright Python API directly (playwright.sync_api) to drive
Chromium.  Persistent sessions are backed by
``BrowserType.launch_persistent_context(user_data_dir)`` so that cookies,
localStorage, and other state survive across runs.
"""

import json
import logging
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .._debug_logging import get_debug_logger
from . import PlaywrightServiceError, _DAEMON_PROFILES_DIR
from ._context import _ServiceContext
from ._elements import _ServiceElement, _ServiceLocator
logger = get_debug_logger("cli_tools.playwright_service")


class PlaywrightService:
    """Unified browser automation service using the Playwright Python API."""

    def __init__(self, session: str, timeout: int = 60):
        self.session = session
        self.default_timeout = timeout
        self._dialog_handler = None
        self._playwright = None
        self._browser_context = None
        self._page = None

    # --- Context Manager ---

    def __enter__(self):
        return self

    def __exit__(self, *args):
        try:
            self.browser_close()
        except PlaywrightServiceError:
            pass
        return False

    # --- Internal: Playwright lifecycle ---

    def _ensure_playwright(self):
        """Lazily start the Playwright driver."""
        if self._playwright is None:
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
        return self._playwright

    def _user_data_dir(self) -> Path:
        """Return the persistent user-data directory for this session."""
        d = _DAEMON_PROFILES_DIR / f"ud-{self.session}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _get_page(self):
        """Return the active page, raising if browser is not open.

        Tracks the most recently opened page in the browser context.
        This is critical for SAML/SSO flows where the identity provider
        postback may create a new page/tab (the original page stays at
        the IdP URL while the authenticated session lands on a new page).
        """
        if self._page is None or self._browser_context is None:
            raise PlaywrightServiceError(
                f"No browser open for session '{self.session}'. Call browser_open() first."
            )
        pages = self._browser_context.pages
        if not pages:
            raise PlaywrightServiceError("All pages have been closed.")
        # If the page was closed, switch to the last open one
        if self._page.is_closed():
            self._page = pages[-1]
            return self._page
        # If new pages were created (e.g. SAML postback opened a new tab),
        # switch to the newest page so callers see the post-redirect URL
        if len(pages) > 1 and pages[-1] is not self._page:
            logger.debug(
                "_get_page: new page detected (total=%d), switching from %s to %s",
                len(pages),
                getattr(self._page, 'url', '?'),
                getattr(pages[-1], 'url', '?'),
            )
            self._page = pages[-1]
        return self._page

    def _page_info(self, page=None) -> Dict[str, Any]:
        """Return standard page info dict from the current page."""
        if page is None:
            try:
                page = self._get_page()
            except PlaywrightServiceError:
                return {'url': '', 'title': '', 'console_errors': 0, 'console_warnings': 0}
        try:
            url = page.url
        except Exception:
            url = ''
        try:
            title = page.title()
        except Exception:
            title = ''
        return {
            'url': url,
            'title': title,
            'console_errors': 0,
            'console_warnings': 0,
        }

    # --- Internal Helpers ---

    def _clear_stale_lock(self, force_kill: bool = False) -> bool:
        """Remove stale Chrome singleton files from persistent profile.

        Checks for SingletonLock, SingletonCookie, and SingletonSocket
        in the user-data directory.  Only removes files when no Chromium
        process is using the directory, unless *force_kill* is True --
        in which case orphaned processes are killed first.

        Returns True if at least one stale file was removed.
        """
        cleared = False
        singleton_names = ("SingletonLock", "SingletonCookie", "SingletonSocket")
        ud_dir = self._user_data_dir()
        for name in singleton_names:
            lock = ud_dir / name
            if not lock.exists() and not lock.is_symlink():
                continue
            ud_str = str(ud_dir)
            if self._is_browser_running(ud_str):
                if force_kill:
                    killed = self._kill_orphaned_browsers(ud_str)
                    logger.info(
                        "_clear_stale_lock: force_kill=True, killed %d orphaned processes for %s",
                        killed, ud_str,
                    )
                    if self._is_browser_running(ud_str):
                        logger.debug(
                            "_clear_stale_lock: processes still running after kill for %s", ud_str
                        )
                        continue
                else:
                    logger.debug(
                        "_clear_stale_lock: skipping %s -- browser process still running", lock
                    )
                    continue
            try:
                lock.unlink()
                cleared = True
                logger.info(
                    "_clear_stale_lock: removed stale %s (no running browser)", lock
                )
            except OSError as exc:
                logger.debug("_clear_stale_lock: failed to remove %s: %s", lock, exc)
        return cleared

    @staticmethod
    def _is_browser_running(user_data_dir: str) -> bool:
        """Check if a Chromium process is using *user_data_dir*."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", user_data_dir],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return True

    @staticmethod
    def _kill_orphaned_browsers(user_data_dir: str) -> int:
        """Kill Chromium processes using *user_data_dir*."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", user_data_dir],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return 0
            pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
            if not pids:
                return 0
            logger.info(
                "_kill_orphaned_browsers: killing %d processes for %s: %s",
                len(pids), user_data_dir, pids,
            )
            subprocess.run(["kill"] + pids, capture_output=True, timeout=5)
            import time as _time
            _time.sleep(1)
            result2 = subprocess.run(
                ["pgrep", "-f", user_data_dir],
                capture_output=True, text=True, timeout=5,
            )
            if result2.returncode == 0 and result2.stdout.strip():
                survivors = [p.strip() for p in result2.stdout.strip().split("\n") if p.strip()]
                if survivors:
                    logger.info("_kill_orphaned_browsers: SIGKILL %d survivors", len(survivors))
                    subprocess.run(
                        ["kill", "-9"] + survivors,
                        capture_output=True,
                        timeout=5,
                    )
                    _time.sleep(0.5)
            return len(pids)
        except Exception as exc:
            logger.debug("_kill_orphaned_browsers: failed: %s", exc)
            return 0

    def clear_session_metadata(self) -> None:
        """Delete the .session marker file.

        The .session file stores launch settings (headed, persistent, etc.).
        Deleting it ensures the next browser_open respects the flags passed to
        it rather than reusing stale settings.
        """
        session_file = _DAEMON_PROFILES_DIR / f"{self.session}.session"
        if session_file.exists():
            try:
                session_file.unlink()
                logger.debug("clear_session_metadata: deleted %s", session_file)
            except OSError:
                pass

    def _clear_stale_socket(self) -> bool:
        """Remove stale Unix domain socket files for this session.

        Returns True if a socket file was removed.
        """
        tmpdir = Path(tempfile.gettempdir())
        pw_dir = tmpdir / "playwright-cli"
        if not pw_dir.exists():
            return False
        cleared = False
        for sock in pw_dir.glob(f"*/{self.session}.soc*"):
            try:
                sock.unlink()
                cleared = True
                logger.info("_clear_stale_socket: removed %s", sock)
            except OSError as exc:
                logger.debug("_clear_stale_socket: failed to remove %s: %s", sock, exc)
        return cleared

    # ==================== Browser Methods ====================

    def browser_open(
        self,
        url: Optional[str] = None,
        persistent: bool = False,
        profile: Optional[str] = None,
        headed: bool = False,
        browser: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._clear_stale_lock()
        self._clear_stale_socket()

        # Close any existing context first
        if self._browser_context is not None:
            try:
                self._browser_context.close()
            except Exception:
                pass
            self._browser_context = None
            self._page = None

        pw = self._ensure_playwright()
        browser_type = pw.chromium

        timeout_ms = self.default_timeout * 1000

        try:
            if persistent:
                user_data_dir = str(self._user_data_dir())
                logger.debug(
                    "browser_open: launching persistent context session=%s "
                    "user_data_dir=%s headed=%s url=%s",
                    self.session, user_data_dir, headed, url,
                )
                launch_args = {
                    "user_data_dir": user_data_dir,
                    "headless": not headed,
                    "timeout": timeout_ms,
                    "args": ["--disable-blink-features=AutomationControlled"],
                }
                # Chrome channel if requested
                if browser and browser.lower() in ("chrome", "chrome-stable", "msedge"):
                    launch_args["channel"] = browser.lower()
                self._browser_context = browser_type.launch_persistent_context(
                    **launch_args
                )
            else:
                logger.debug(
                    "browser_open: launching non-persistent browser headed=%s url=%s",
                    headed, url,
                )
                browser_instance = browser_type.launch(
                    headless=not headed,
                    timeout=timeout_ms,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                self._browser_context = browser_instance.new_context()

            # Get or create a page
            pages = self._browser_context.pages
            if pages:
                self._page = pages[0]
            else:
                self._page = self._browser_context.new_page()

            # Track new pages/tabs as they open (critical for SAML/SSO
            # flows where the IdP postback may create a new page).
            def _on_new_page(new_page):
                logger.debug(
                    "browser_open: new page opened -> %s (was %s)",
                    getattr(new_page, 'url', '?'),
                    getattr(self._page, 'url', '?'),
                )
                self._page = new_page
            self._browser_context.on("page", _on_new_page)

            if url:
                self._page.goto(url, timeout=timeout_ms)

            # Write session metadata
            _DAEMON_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
            session_file = _DAEMON_PROFILES_DIR / f"{self.session}.session"
            session_file.write_text(json.dumps({
                "headed": headed,
                "persistent": persistent,
                "browser": browser or "chromium",
            }))

            return self._page_info()

        except PlaywrightServiceError:
            raise
        except Exception as e:
            error_msg = str(e)
            if "already in use" in error_msg.lower() or "Target closed" in error_msg:
                logger.info(
                    "browser_open: error for session '%s', attempting cleanup and retry: %s",
                    self.session, error_msg,
                )
                self._browser_context = None
                self._page = None
                self.clear_session_metadata()
                if not self._clear_stale_lock(force_kill=False):
                    self._clear_stale_lock(force_kill=True)
                self._clear_stale_socket()
                # Retry once
                try:
                    return self.browser_open(
                        url=url, persistent=persistent, profile=profile,
                        headed=headed, browser=browser,
                    )
                except Exception:
                    pass
            raise PlaywrightServiceError(f"Failed to open browser: {e}")

    def browser_close(self) -> Dict[str, Any]:
        if self._browser_context is not None:
            try:
                self._browser_context.close()
            except Exception as e:
                logger.debug("browser_close: context close error: %s", e)
            self._browser_context = None
            self._page = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception as e:
                logger.debug("browser_close: playwright stop error: %s", e)
            self._playwright = None
        return {"success": True, "message": "Browser closed"}

    # ==================== Page Methods ====================

    def page_goto(self, url: str) -> Dict[str, Any]:
        page = self._get_page()
        try:
            page.goto(url, timeout=self.default_timeout * 1000)
        except Exception as e:
            raise PlaywrightServiceError(f"Failed to navigate to {url}: {e}")
        return self._page_info()

    def page_reload(self) -> Dict[str, Any]:
        page = self._get_page()
        page.reload(timeout=self.default_timeout * 1000)
        return self._page_info()

    def page_screenshot(self, ref: Optional[str] = None) -> Dict[str, Any]:
        page = self._get_page()
        screenshot_dir = Path(tempfile.gettempdir()) / "playwright-screenshots"
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        screenshot_file = screenshot_dir / f"{self.session}-{int(time.time())}.png"
        if ref:
            element = page.locator(ref)
            element.screenshot(path=str(screenshot_file))
        else:
            page.screenshot(path=str(screenshot_file))
        return {'file': str(screenshot_file), 'page_url': page.url}

    def page_eval(self, func: str, ref: Optional[str] = None, timeout: int = None) -> Dict[str, Any]:
        page = self._get_page()
        try:
            if ref:
                element = page.locator(ref)
                result = element.evaluate(func)
            else:
                result = page.evaluate(func)
        except Exception as e:
            raise PlaywrightServiceError(f"Eval failed: {e}")
        return {
            'result': result,
            'page_url': page.url,
            'page_title': page.title(),
        }

    # ==================== Keyboard Methods ====================

    def keyboard_press(self, key: str) -> Dict[str, Any]:
        page = self._get_page()
        page.keyboard.press(key)
        return self._page_info()

    # ==================== Cookie Methods ====================

    def cookie_list(self) -> List[Dict[str, Any]]:
        if self._browser_context is None:
            return []
        try:
            return self._browser_context.cookies()
        except Exception:
            return []

    def cookie_set(self, name: str, value: str) -> Dict[str, Any]:
        if self._browser_context is None:
            raise PlaywrightServiceError("No browser open")
        page = self._get_page()
        url = page.url
        # Extract domain from current URL
        domain = ""
        m = re.match(r"https?://([^/]+)", url)
        if m:
            domain = m.group(1)
        self._browser_context.add_cookies([{
            'name': name,
            'value': value,
            'domain': domain,
            'path': '/',
        }])
        return {"success": True, "message": f"Cookie '{name}' set"}

    # ==================== Storage Methods ====================

    def _storage_list(self, prefix: str) -> List[Dict[str, str]]:
        page = self._get_page()
        storage_type = "localStorage" if prefix == "localstorage" else "sessionStorage"
        try:
            result = page.evaluate(f"""() => {{
                const items = [];
                for (let i = 0; i < {storage_type}.length; i++) {{
                    const key = {storage_type}.key(i);
                    items.push({{ key: key, value: {storage_type}.getItem(key) }});
                }}
                return items;
            }}""")
            return result or []
        except Exception:
            return []

    def localstorage_list(self) -> List[Dict[str, str]]:
        return self._storage_list("localstorage")

    # ==================== State Methods ====================

    def state_load(self, filename: str) -> Dict[str, Any]:
        """Load browser state (cookies, localStorage) from a JSON file."""
        path = Path(filename)
        if not path.exists():
            raise PlaywrightServiceError(f"State file not found: {filename}")
        try:
            state = json.loads(path.read_text())
        except Exception as e:
            raise PlaywrightServiceError(f"Failed to parse state file: {e}")

        if self._browser_context is None:
            raise PlaywrightServiceError("No browser open")

        # Restore cookies
        cookies = state.get("cookies", [])
        if cookies:
            self._browser_context.add_cookies(cookies)

        # Restore localStorage
        origins = state.get("origins", [])
        for origin_data in origins:
            origin = origin_data.get("origin", "")
            ls = origin_data.get("localStorage", [])
            if ls and origin:
                page = self._get_page()
                current_url = page.url
                # Navigate to origin to set localStorage, then navigate back
                try:
                    page.goto(origin, timeout=10000)
                    for item in ls:
                        page.evaluate(
                            f"localStorage.setItem({json.dumps(item['name'])}, {json.dumps(item['value'])})"
                        )
                    if current_url and current_url != "about:blank":
                        page.goto(current_url, timeout=10000)
                except Exception as e:
                    logger.debug("state_load: localStorage restore failed for %s: %s", origin, e)

        return {"success": True, "message": f"State loaded from {filename}"}

    def state_save(self, filename: Optional[str] = None) -> Dict[str, Any]:
        """Save browser state (cookies, localStorage) to a JSON file."""
        if self._browser_context is None:
            raise PlaywrightServiceError("No browser open")

        if not filename:
            filename = str(Path(tempfile.gettempdir()) / f"playwright-state-{self.session}.json")

        try:
            state = self._browser_context.storage_state()
            Path(filename).parent.mkdir(parents=True, exist_ok=True)
            Path(filename).write_text(json.dumps(state, indent=2))
        except Exception as e:
            raise PlaywrightServiceError(f"Failed to save state: {e}")

        return {"success": True, "file": filename}

    # ==================== Data Methods ====================

    def data_delete(self) -> Dict[str, Any]:
        """Delete all session data (user data directory)."""
        import shutil
        ud = self._user_data_dir()
        if ud.exists():
            try:
                shutil.rmtree(ud)
                logger.debug("data_delete: removed %s", ud)
            except OSError as e:
                logger.debug("data_delete: failed to remove %s: %s", ud, e)
        # Also remove session metadata
        self.clear_session_metadata()
        return {"success": True, "message": "Session data deleted"}

    # ============================================================
    # Convenience Methods (used by BrowserAutomation)
    # ============================================================

    def goto(self, url: str, wait_until: str = None) -> None:
        """Navigate to URL."""
        page = self._get_page()
        kwargs = {"timeout": self.default_timeout * 1000}
        if wait_until:
            kwargs["wait_until"] = wait_until
        page.goto(url, **kwargs)

    def reload(self, wait_until: str = None) -> None:
        page = self._get_page()
        kwargs = {"timeout": self.default_timeout * 1000}
        if wait_until:
            kwargs["wait_until"] = wait_until
        page.reload(**kwargs)

    @property
    def url(self) -> str:
        try:
            return self._get_page().url
        except PlaywrightServiceError:
            return ""

    def title(self) -> str:
        try:
            return self._get_page().title()
        except PlaywrightServiceError:
            return ""

    def content(self) -> str:
        try:
            return self._get_page().content()
        except PlaywrightServiceError:
            return ""

    def evaluate(self, js: str, arg: Any = None) -> Any:
        """Evaluate JavaScript on the page.

        For functions that accept an argument, the arg is passed directly
        to Playwright's evaluate(). If a dialog handler has been registered
        via once("dialog", ...), window.confirm and window.alert are
        overridden.
        """
        page = self._get_page()

        # Handle dialog override
        if self._dialog_handler:
            try:
                page.evaluate(
                    "window.confirm = () => true; window.alert = () => {};"
                )
            except Exception:
                pass
            self._dialog_handler = None

        try:
            if arg is not None:
                return page.evaluate(js, arg)
            return page.evaluate(js)
        except Exception as e:
            error_msg = str(e)
            # Don't raise for common benign errors
            if "undefined" in error_msg.lower() or "null" in error_msg.lower():
                return None
            raise PlaywrightServiceError(f"Eval error: {e}")

    # --- Element Selection ---

    def query_selector(self, selector: str) -> Optional[_ServiceElement]:
        from ._selectors import _is_playwright_selector, _split_selector
        page = self._get_page()
        if _is_playwright_selector(selector):
            for part in _split_selector(selector):
                part = part.strip()
                if _is_playwright_selector(part):
                    loc = self.locator(part)
                    if loc.count() > 0:
                        return loc.first
                else:
                    el = page.query_selector(part)
                    if el:
                        return _ServiceElement(self, css=part)
            return None
        el = page.query_selector(selector)
        if el:
            return _ServiceElement(self, css=selector)
        return None

    def query_selector_all(self, selector: str) -> List[_ServiceElement]:
        page = self._get_page()
        elements = page.query_selector_all(selector)
        return [_ServiceElement(self, css=selector, index=i) for i in range(len(elements))]

    # --- Locator API ---

    def locator(self, selector: str) -> _ServiceLocator:
        return _ServiceLocator(self, selector)

    def get_by_role(self, role: str, *, name=None) -> _ServiceLocator:
        return _ServiceLocator.from_role(self, role, name)

    def get_by_placeholder(self, text: str) -> _ServiceLocator:
        return _ServiceLocator(self, f'[placeholder="{text}"]')

    # --- Direct Page Actions ---

    def fill(self, selector: str, text: str) -> None:
        self.locator(selector).fill(text)

    def select_option(self, selector: str, *, label: str = None, value: str = None) -> None:
        self.locator(selector).select_option(value, label=label)

    # --- Waiting ---

    def wait_for_timeout(self, ms: int) -> None:
        time.sleep(ms / 1000)

    def wait_for_load_state(self, state: str = "load", timeout: int = 30000) -> None:
        page = self._get_page()
        try:
            page.wait_for_load_state(state, timeout=timeout)
        except Exception:
            # Fall back to polling readyState
            timeout_s = timeout / 1000
            poll = 0.3
            elapsed = 0.0
            while elapsed < timeout_s:
                ready = page.evaluate("document.readyState")
                if ready == "complete":
                    break
                time.sleep(poll)
                elapsed += poll

    def wait_for_selector(
        self, selector: str, *, state: str = None, timeout: int = None
    ) -> Optional[_ServiceElement]:
        page = self._get_page()
        pw_state = state or "visible"
        try:
            element = page.wait_for_selector(
                selector, state=pw_state, timeout=timeout or 30000
            )
            if element and state not in ("hidden", "detached"):
                return _ServiceElement(self, css=selector)
            return None
        except Exception:
            if state not in ("hidden", "detached"):
                raise PlaywrightServiceError(f"Timeout waiting for selector: {selector}")
            return None

    # --- Event Handling ---

    def once(self, event: str, callback) -> None:
        if event == "dialog":
            self._dialog_handler = callback

    # --- Context ---

    @property
    def context(self) -> _ServiceContext:
        return _ServiceContext(self)
