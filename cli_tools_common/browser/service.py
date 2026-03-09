"""PlaywrightService - unified browser automation service.

Uses the Playwright Python API directly (playwright.sync_api) to drive
Chromium.  Persistent sessions are backed by
``BrowserType.launch_persistent_context(user_data_dir)`` so that cookies,
localStorage, and other state survive across runs.

Provides two API layers:
1. Low-level methods (page_goto, page_eval, browser_open, etc.) returning dicts
2. Convenience methods (goto, evaluate, locator, etc.) used by BrowserAutomation
"""

import json
import logging
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .._debug_logging import configure_debug_logger
from . import PlaywrightServiceError, _DAEMON_PROFILES_DIR
from ._context import _ServiceContext
from ._elements import _ServiceElement, _ServiceLocator
logger = logging.getLogger("cli_tools.playwright_service")
configure_debug_logger(logger)


class PlaywrightService:
    """Unified browser automation service using the Playwright Python API.

    Provides two API layers:
    1. Low-level methods (page_goto, browser_open, etc.) returning dicts
       - Used by PlaywrightClient delegation
    2. Convenience methods (goto, evaluate, locator, etc.)
       - Used by BrowserAutomation and its subclasses
    """

    def __init__(
        self,
        session: str,
        binary: str = "playwright-cli",  # kept for backward compat; ignored
        timeout: int = 60,
    ):
        self.session = session
        self.binary = binary  # no longer used
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
                self._clear_stale_lock(force_kill=False)
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

    def browser_list(self) -> List[Dict[str, Any]]:
        sessions = []
        if _DAEMON_PROFILES_DIR.exists():
            for sf in _DAEMON_PROFILES_DIR.glob("*.session"):
                name = sf.stem
                try:
                    data = json.loads(sf.read_text())
                except Exception:
                    data = {}
                ud = _DAEMON_PROFILES_DIR / f"ud-{name}"
                sessions.append({
                    'name': name,
                    'browser_type': data.get('browser', 'chromium'),
                    'user_data_dir': str(ud) if ud.exists() else '',
                    'headed': data.get('headed', False),
                    'status': 'running' if self._is_browser_running(str(ud)) else 'stopped',
                })
        return sessions

    def browser_close_all(self) -> Dict[str, Any]:
        self.browser_close()
        return {"success": True, "message": "All sessions closed"}

    def browser_kill_all(self) -> Dict[str, Any]:
        self.browser_close()
        return {"success": True, "message": "All sessions killed"}

    def browser_install(self) -> Dict[str, Any]:
        return {"success": True, "message": "Workspace initialized"}

    def browser_install_browser(self) -> Dict[str, Any]:
        try:
            subprocess.run(
                ["playwright", "install", "chromium"],
                capture_output=True, text=True, timeout=300,
            )
        except Exception as e:
            raise PlaywrightServiceError(f"Failed to install browser: {e}")
        return {"success": True, "message": "Browser installed"}

    def browser_resize(self, width: int, height: int) -> Dict[str, Any]:
        page = self._get_page()
        page.set_viewport_size({"width": width, "height": height})
        return self._page_info()

    # ==================== Page Methods ====================

    def page_goto(self, url: str) -> Dict[str, Any]:
        page = self._get_page()
        try:
            page.goto(url, timeout=self.default_timeout * 1000)
        except Exception as e:
            raise PlaywrightServiceError(f"Failed to navigate to {url}: {e}")
        return self._page_info()

    def page_back(self) -> Dict[str, Any]:
        page = self._get_page()
        page.go_back(timeout=self.default_timeout * 1000)
        return self._page_info()

    def page_forward(self) -> Dict[str, Any]:
        page = self._get_page()
        page.go_forward(timeout=self.default_timeout * 1000)
        return self._page_info()

    def page_reload(self) -> Dict[str, Any]:
        page = self._get_page()
        page.reload(timeout=self.default_timeout * 1000)
        return self._page_info()

    def page_snapshot(self) -> Dict[str, Any]:
        page = self._get_page()
        snapshot_dir = Path(tempfile.gettempdir()) / "playwright-snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snapshot_file = snapshot_dir / f"{self.session}-{int(time.time())}.mhtml"
        try:
            # Use CDP to get MHTML snapshot
            client = page.context.new_cdp_session(page)
            result = client.send("Page.captureSnapshot", {"format": "mhtml"})
            snapshot_file.write_text(result.get("data", ""))
            client.detach()
        except Exception:
            # Fallback: save HTML content
            snapshot_file = snapshot_file.with_suffix(".html")
            snapshot_file.write_text(page.content())
        return {
            'file': str(snapshot_file),
            'page_url': page.url,
            'page_title': page.title(),
        }

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

    def page_pdf(self) -> Dict[str, Any]:
        page = self._get_page()
        pdf_dir = Path(tempfile.gettempdir()) / "playwright-pdfs"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        pdf_file = pdf_dir / f"{self.session}-{int(time.time())}.pdf"
        page.pdf(path=str(pdf_file))
        return {'file': str(pdf_file), 'page_url': page.url}

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

    # ==================== Interact Methods ====================

    def interact_click(self, ref: str, button: Optional[str] = None) -> Dict[str, Any]:
        page = self._get_page()
        kwargs = {}
        if button:
            kwargs["button"] = button
        page.locator(ref).click(**kwargs)
        return self._page_info()

    def interact_dblclick(self, ref: str, button: Optional[str] = None) -> Dict[str, Any]:
        page = self._get_page()
        kwargs = {}
        if button:
            kwargs["button"] = button
        page.locator(ref).dblclick(**kwargs)
        return self._page_info()

    def interact_fill(self, ref: str, text: str) -> Dict[str, Any]:
        page = self._get_page()
        page.locator(ref).fill(text)
        return self._page_info()

    def interact_type(self, ref: str, text: str) -> Dict[str, Any]:
        page = self._get_page()
        page.keyboard.type(text)
        return self._page_info()

    def interact_drag(self, start_ref: str, end_ref: str) -> Dict[str, Any]:
        page = self._get_page()
        page.locator(start_ref).drag_to(page.locator(end_ref))
        return self._page_info()

    def interact_hover(self, ref: str) -> Dict[str, Any]:
        page = self._get_page()
        page.locator(ref).hover()
        return self._page_info()

    def interact_select(self, ref: str, value: str) -> Dict[str, Any]:
        page = self._get_page()
        page.locator(ref).select_option(value)
        return self._page_info()

    def interact_upload(self, file: str) -> Dict[str, Any]:
        page = self._get_page()
        page.locator('input[type="file"]').set_input_files(file)
        return self._page_info()

    def interact_check(self, ref: str) -> Dict[str, Any]:
        page = self._get_page()
        page.locator(ref).check()
        return self._page_info()

    def interact_uncheck(self, ref: str) -> Dict[str, Any]:
        page = self._get_page()
        page.locator(ref).uncheck()
        return self._page_info()

    # ==================== Keyboard Methods ====================

    def keyboard_press(self, key: str) -> Dict[str, Any]:
        page = self._get_page()
        page.keyboard.press(key)
        return self._page_info()

    def keyboard_keydown(self, key: str) -> Dict[str, Any]:
        page = self._get_page()
        page.keyboard.down(key)
        return self._page_info()

    def keyboard_keyup(self, key: str) -> Dict[str, Any]:
        page = self._get_page()
        page.keyboard.up(key)
        return self._page_info()

    # ==================== Mouse Methods ====================

    def mouse_move(self, x: int, y: int) -> Dict[str, Any]:
        page = self._get_page()
        page.mouse.move(x, y)
        return self._page_info()

    def mouse_down(self, button: Optional[str] = None) -> Dict[str, Any]:
        page = self._get_page()
        kwargs = {}
        if button:
            kwargs["button"] = button
        page.mouse.down(**kwargs)
        return self._page_info()

    def mouse_up(self, button: Optional[str] = None) -> Dict[str, Any]:
        page = self._get_page()
        kwargs = {}
        if button:
            kwargs["button"] = button
        page.mouse.up(**kwargs)
        return self._page_info()

    def mouse_wheel(self, dx: int, dy: int) -> Dict[str, Any]:
        page = self._get_page()
        page.mouse.wheel(dx, dy)
        return self._page_info()

    # ==================== Dialog Methods ====================

    def dialog_accept(self, prompt_text: Optional[str] = None) -> Dict[str, Any]:
        page = self._get_page()
        page.on("dialog", lambda d: d.accept(prompt_text) if prompt_text else d.accept())
        return {"success": True, "message": "Dialog accepted"}

    def dialog_dismiss(self) -> Dict[str, Any]:
        page = self._get_page()
        page.on("dialog", lambda d: d.dismiss())
        return {"success": True, "message": "Dialog dismissed"}

    # ==================== Tab Methods ====================

    def tab_list(self) -> List[Dict[str, Any]]:
        if self._browser_context is None:
            return []
        tabs = []
        for i, page in enumerate(self._browser_context.pages):
            tab = {'index': i, 'url': page.url}
            try:
                tab['title'] = page.title()
            except Exception:
                pass
            tabs.append(tab)
        return tabs

    def tab_new(self, url: Optional[str] = None) -> Dict[str, Any]:
        if self._browser_context is None:
            raise PlaywrightServiceError("No browser open")
        page = self._browser_context.new_page()
        if url:
            page.goto(url, timeout=self.default_timeout * 1000)
        self._page = page
        return self._page_info()

    def tab_close(self, index: Optional[int] = None) -> Dict[str, Any]:
        if self._browser_context is None:
            return {"success": True, "message": "Tab closed"}
        pages = self._browser_context.pages
        if index is not None and 0 <= index < len(pages):
            pages[index].close()
        elif self._page is not None:
            self._page.close()
        # Switch to last remaining page
        pages = self._browser_context.pages
        if pages:
            self._page = pages[-1]
        else:
            self._page = None
        return {"success": True, "message": "Tab closed"}

    def tab_select(self, index: int) -> Dict[str, Any]:
        if self._browser_context is None:
            raise PlaywrightServiceError("No browser open")
        pages = self._browser_context.pages
        if 0 <= index < len(pages):
            self._page = pages[index]
            self._page.bring_to_front()
        else:
            raise PlaywrightServiceError(f"Tab index {index} out of range (0-{len(pages)-1})")
        return self._page_info()

    # ==================== Cookie Methods ====================

    def cookie_list(self) -> List[Dict[str, Any]]:
        if self._browser_context is None:
            return []
        try:
            return self._browser_context.cookies()
        except Exception:
            return []

    def cookie_get(self, name: str) -> Dict[str, Any]:
        cookies = self.cookie_list()
        for c in cookies:
            if c.get('name') == name:
                return c
        return {'name': name, 'value': ''}

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

    def cookie_delete(self, name: str) -> Dict[str, Any]:
        if self._browser_context is None:
            return {"success": True, "message": f"Cookie '{name}' deleted"}
        # Playwright doesn't have delete-single-cookie, so clear and re-add others
        cookies = self._browser_context.cookies()
        remaining = [c for c in cookies if c.get('name') != name]
        self._browser_context.clear_cookies()
        if remaining:
            self._browser_context.add_cookies(remaining)
        return {"success": True, "message": f"Cookie '{name}' deleted"}

    def cookie_clear(self) -> Dict[str, Any]:
        if self._browser_context is not None:
            self._browser_context.clear_cookies()
        return {"success": True, "message": "All cookies cleared"}

    # ==================== Storage Methods (shared) ====================

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

    def _storage_get(self, prefix: str, key: str) -> Dict[str, str]:
        page = self._get_page()
        storage_type = "localStorage" if prefix == "localstorage" else "sessionStorage"
        try:
            value = page.evaluate(f"{storage_type}.getItem({json.dumps(key)})")
            return {'key': key, 'value': value or ''}
        except Exception:
            return {'key': key, 'value': ''}

    def _storage_set(self, prefix: str, display: str, key: str, value: str) -> Dict[str, Any]:
        page = self._get_page()
        storage_type = "localStorage" if prefix == "localstorage" else "sessionStorage"
        page.evaluate(f"{storage_type}.setItem({json.dumps(key)}, {json.dumps(value)})")
        return {"success": True, "message": f"{display} '{key}' set"}

    def _storage_delete(self, prefix: str, display: str, key: str) -> Dict[str, Any]:
        page = self._get_page()
        storage_type = "localStorage" if prefix == "localstorage" else "sessionStorage"
        page.evaluate(f"{storage_type}.removeItem({json.dumps(key)})")
        return {"success": True, "message": f"{display} '{key}' deleted"}

    def _storage_clear(self, prefix: str, display: str) -> Dict[str, Any]:
        page = self._get_page()
        storage_type = "localStorage" if prefix == "localstorage" else "sessionStorage"
        page.evaluate(f"{storage_type}.clear()")
        return {"success": True, "message": f"{display} cleared"}

    # ==================== LocalStorage Methods ====================

    def localstorage_list(self) -> List[Dict[str, str]]:
        return self._storage_list("localstorage")

    def localstorage_get(self, key: str) -> Dict[str, str]:
        return self._storage_get("localstorage", key)

    def localstorage_set(self, key: str, value: str) -> Dict[str, Any]:
        return self._storage_set("localstorage", "localStorage", key, value)

    def localstorage_delete(self, key: str) -> Dict[str, Any]:
        return self._storage_delete("localstorage", "localStorage", key)

    def localstorage_clear(self) -> Dict[str, Any]:
        return self._storage_clear("localstorage", "localStorage")

    # ==================== SessionStorage Methods ====================

    def sessionstorage_list(self) -> List[Dict[str, str]]:
        return self._storage_list("sessionstorage")

    def sessionstorage_get(self, key: str) -> Dict[str, str]:
        return self._storage_get("sessionstorage", key)

    def sessionstorage_set(self, key: str, value: str) -> Dict[str, Any]:
        return self._storage_set("sessionstorage", "sessionStorage", key, value)

    def sessionstorage_delete(self, key: str) -> Dict[str, Any]:
        return self._storage_delete("sessionstorage", "sessionStorage", key)

    def sessionstorage_clear(self) -> Dict[str, Any]:
        return self._storage_clear("sessionstorage", "sessionStorage")

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

    # ==================== Network Methods ====================

    def network_requests(self) -> List[Dict[str, Any]]:
        # Network request tracking would require setting up route handlers
        # Return empty list as baseline
        return []

    def network_route(self, pattern: str) -> Dict[str, Any]:
        page = self._get_page()
        page.route(pattern, lambda route: route.continue_())
        return {"success": True, "message": f"Route added: {pattern}"}

    def network_route_list(self) -> List[Dict[str, str]]:
        return []

    def network_unroute(self, pattern: Optional[str] = None) -> Dict[str, Any]:
        page = self._get_page()
        if pattern:
            page.unroute(pattern)
        return {"success": True, "message": "Routes removed"}

    # ==================== DevTools Methods ====================

    def devtools_console(self, min_level: Optional[str] = None) -> List[Dict[str, Any]]:
        return []

    def devtools_run_code(self, code: str) -> Dict[str, Any]:
        page = self._get_page()
        try:
            result = page.evaluate(code)
        except Exception as e:
            raise PlaywrightServiceError(f"Code execution failed: {e}")
        return {
            'result': result,
            'page_url': page.url,
            'page_title': page.title(),
        }

    def devtools_tracing_start(self) -> Dict[str, Any]:
        if self._browser_context is not None:
            self._browser_context.tracing.start(screenshots=True, snapshots=True)
        return {"success": True, "message": "Tracing started"}

    def devtools_tracing_stop(self) -> Dict[str, Any]:
        if self._browser_context is not None:
            trace_dir = Path(tempfile.gettempdir()) / "playwright-traces"
            trace_dir.mkdir(parents=True, exist_ok=True)
            trace_file = trace_dir / f"{self.session}-{int(time.time())}.zip"
            self._browser_context.tracing.stop(path=str(trace_file))
        return {"success": True, "message": "Tracing stopped"}

    def devtools_video_start(self) -> Dict[str, Any]:
        return {"success": True, "message": "Video recording started"}

    def devtools_video_stop(self) -> Dict[str, Any]:
        return {"success": True, "message": "Video recording stopped"}

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

    def press(self, key: str) -> None:
        """Press a key (no return value)."""
        self.keyboard_press(key)

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
