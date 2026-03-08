"""PlaywrightService - unified browser automation service.

Single interface for all playwright-cli operations, providing two API layers:
1. Low-level methods (page_goto, page_eval, browser_open, etc.) returning dicts
2. Convenience methods (goto, evaluate, locator, etc.) used by BrowserAutomation
"""

import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .._debug_logging import configure_debug_logger
from . import PlaywrightServiceError, _DAEMON_PROFILES_DIR
from ._context import _ServiceContext
from ._elements import _ServiceElement, _ServiceLocator
from ._parsers import (
    _parse_action_output,
    _parse_console_messages,
    _parse_cookie_list,
    _parse_eval_result,
    _parse_markdown_sections,
    _parse_network_requests,
    _parse_page_section,
    _parse_route_list,
    _parse_session_list,
    _parse_storage_get,
    _parse_storage_list,
    _parse_tab_list,
)
from ._selectors import _is_playwright_selector, _split_selector

logger = logging.getLogger("cli_tools.playwright_service")
configure_debug_logger(logger)


class PlaywrightService:
    """Unified browser automation service using playwright-cli flat commands.

    Provides two API layers:
    1. Low-level methods (page_goto, browser_open, etc.) returning dicts
       - Used by PlaywrightClient delegation
    2. Convenience methods (goto, evaluate, locator, etc.)
       - Used by BrowserAutomation and its subclasses
    """

    def __init__(
        self,
        session: str,
        binary: str = "playwright-cli",
        timeout: int = 60,
    ):
        self.session = session
        self.binary = binary
        self.default_timeout = timeout
        self._dialog_handler = None

    # --- Context Manager ---

    def __enter__(self):
        return self

    def __exit__(self, *args):
        try:
            self.browser_close()
        except PlaywrightServiceError:
            pass
        return False

    # --- Subprocess Runner ---

    def _run(
        self,
        args: list,
        timeout: int = None,
        check: bool = True,
        input_text: str = None,
    ) -> subprocess.CompletedProcess:
        timeout = timeout if timeout is not None else self.default_timeout
        cmd = [self.binary, f"-s={self.session}"] + args
        logger.debug("_run: cmd=%s timeout=%d check=%s", cmd, timeout, check)
        try:
            result = subprocess.run(
                cmd,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            logger.debug("_run: returncode=%d", result.returncode)
            if result.stdout and result.stdout.strip():
                logger.debug("_run: stdout=%s", result.stdout.strip()[:1000])
            if result.stderr and result.stderr.strip():
                logger.debug("_run: stderr=%s", result.stderr.strip()[:1000])
            if check and result.returncode != 0:
                error_msg = result.stderr.strip() or result.stdout.strip() or "Command failed"
                raise PlaywrightServiceError(f"playwright-cli error: {error_msg}")
            return result
        except subprocess.TimeoutExpired:
            raise PlaywrightServiceError(f"Command timed out after {timeout}s")
        except FileNotFoundError:
            raise PlaywrightServiceError(
                f"playwright-cli binary '{self.binary}' not found in PATH"
            )
        except PlaywrightServiceError:
            raise
        except Exception as e:
            raise PlaywrightServiceError(f"Failed to run command: {e}")

    # --- Internal Helpers ---

    def _clear_stale_lock(self, force_kill: bool = False) -> bool:
        """Remove stale Chrome singleton files from persistent profile.

        Checks for SingletonLock, SingletonCookie, and SingletonSocket
        in the user-data directory.  Only removes files when no Chromium
        process is using the directory, unless *force_kill* is True —
        in which case orphaned processes are killed first.

        Returns True if at least one stale file was removed.
        """
        cleared = False
        singleton_names = ("SingletonLock", "SingletonCookie", "SingletonSocket")
        for lock in _DAEMON_PROFILES_DIR.glob(f"*/ud-{self.session}-*/SingletonLock"):
            ud_dir = str(lock.parent)
            if self._is_browser_running(ud_dir):
                if force_kill:
                    killed = self._kill_orphaned_browsers(ud_dir)
                    logger.info(
                        "_clear_stale_lock: force_kill=True, killed %d orphaned processes for %s",
                        killed, ud_dir,
                    )
                    if self._is_browser_running(ud_dir):
                        logger.debug(
                            "_clear_stale_lock: processes still running after kill for %s", ud_dir
                        )
                        continue
                else:
                    logger.debug(
                        "_clear_stale_lock: skipping %s — browser process still running", lock
                    )
                    continue
            # Remove all singleton files from this user-data directory
            for name in singleton_names:
                singleton = lock.parent / name
                if singleton.exists() or singleton.is_symlink():
                    try:
                        singleton.unlink()
                        cleared = True
                        logger.info(
                            "_clear_stale_lock: removed stale %s (no running browser)", singleton
                        )
                    except OSError as exc:
                        logger.debug("_clear_stale_lock: failed to remove %s: %s", singleton, exc)
        return cleared

    @staticmethod
    def _is_browser_running(user_data_dir: str) -> bool:
        """Check if a Chromium process is using *user_data_dir*.

        Uses ``pgrep -f`` to look for any process whose command line
        contains the user-data directory path.  Returns False when no
        matching process is found (i.e., the lock is stale).
        """
        try:
            result = subprocess.run(
                ["pgrep", "-f", user_data_dir],
                capture_output=True,
                text=True,
                timeout=5,
            )
            # pgrep exits 0 when matches are found, 1 when none
            return result.returncode == 0
        except Exception:
            # If pgrep itself fails, err on the safe side: assume running
            return True

    @staticmethod
    def _kill_orphaned_browsers(user_data_dir: str) -> int:
        """Kill Chromium processes using *user_data_dir*.

        Finds process IDs via ``pgrep -f`` and sends SIGTERM.
        Returns the number of processes killed.
        """
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
            # SIGTERM first
            subprocess.run(
                ["kill"] + pids,
                capture_output=True,
                timeout=5,
            )
            import time as _time
            _time.sleep(1)
            # SIGKILL any survivors
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
        """Delete the .session file so the next browser_open uses fresh flags.

        The .session file stores launch settings (headed, persistent, etc.).
        Deleting it ensures the next browser_open respects the flags passed to
        it rather than reusing stale settings (e.g. headed=true from a prior
        authenticate() call).  The user data directory (cookies, localStorage)
        is NOT deleted.
        """
        for sf in _DAEMON_PROFILES_DIR.glob(f"*/{self.session}.session"):
            try:
                sf.unlink()
                logger.debug("clear_session_metadata: deleted %s", sf)
            except OSError:
                pass

    def _clear_stale_socket(self) -> bool:
        """Remove stale Unix domain socket files for this session.

        playwright-cli creates socket files at $TMPDIR/playwright-cli/<hash>/<session>.sock
        for IPC. These can become stale when browser processes are killed without
        clean shutdown, causing EADDRINUSE on the next open.

        Returns True if a socket file was removed.
        """
        import os
        import tempfile
        tmpdir = Path(tempfile.gettempdir())
        pw_dir = tmpdir / "playwright-cli"
        if not pw_dir.exists():
            return False
        cleared = False
        # Socket extension may be truncated (e.g. .soc instead of .sock)
        # due to macOS ~104 byte Unix domain socket path limit
        for sock in pw_dir.glob(f"*/{self.session}.soc*"):
            try:
                sock.unlink()
                cleared = True
                logger.info("_clear_stale_socket: removed %s", sock)
            except OSError as exc:
                logger.debug("_clear_stale_socket: failed to remove %s: %s", sock, exc)
        return cleared

    def _page_info_cmd(self, args: list, **kwargs) -> Dict[str, Any]:
        """Run command and return page info dict."""
        result = self._run(args, **kwargs)
        parsed = _parse_action_output(result.stdout)
        page = parsed.get('page', {})
        return {
            'url': page.get('url', ''),
            'title': page.get('title', ''),
            'console_errors': page.get('console_errors', 0),
            'console_warnings': page.get('console_warnings', 0),
        }

    def _success_cmd(self, args: list, default_msg: str = "", **kwargs) -> Dict[str, Any]:
        """Run command and return success dict."""
        result = self._run(args, **kwargs)
        return {"success": True, "message": result.stdout.strip() or default_msg}

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
        args = ["open"]
        if persistent:
            args.append("--persistent")
        if profile:
            args.extend(["--profile", profile])
        if headed:
            args.append("--headed")
        if browser:
            args.extend(["--browser", browser])
        if url:
            args.append(url)
        try:
            return self._page_info_cmd(args)
        except PlaywrightServiceError as e:
            if "already in use" not in str(e).lower():
                raise
            logger.info(
                "browser_open: 'already in use' error for session '%s', "
                "attempting cleanup and single retry",
                self.session,
            )
            try:
                self.browser_close()
            except PlaywrightServiceError:
                pass
            self.clear_session_metadata()
            # First try without force-killing (handles genuinely stale locks
            # where the process already exited).
            cleared = self._clear_stale_lock(force_kill=False)
            if not cleared:
                # Processes are still running but the daemon doesn't track
                # them (orphans).  Force-kill and remove locks.
                logger.info(
                    "browser_open: no stale locks removed — force-killing orphaned browsers"
                )
                cleared = self._clear_stale_lock(force_kill=True)
            if not cleared:
                logger.debug(
                    "browser_open: cleanup failed — browser may be genuinely in use"
                )
            # Always clear stale socket files — they survive process kills
            self._clear_stale_socket()
            return self._page_info_cmd(args)

    def browser_close(self) -> Dict[str, Any]:
        return self._success_cmd(["close"], "Browser closed")

    def browser_list(self) -> List[Dict[str, Any]]:
        result = self._run(["list"])
        return _parse_session_list(result.stdout)

    def browser_close_all(self) -> Dict[str, Any]:
        return self._success_cmd(["close-all"], "All sessions closed")

    def browser_kill_all(self) -> Dict[str, Any]:
        return self._success_cmd(["kill-all"], "All sessions killed")

    def browser_install(self) -> Dict[str, Any]:
        return self._success_cmd(["install"], "Workspace initialized", timeout=120)

    def browser_install_browser(self) -> Dict[str, Any]:
        return self._success_cmd(["install-browser"], "Browser installed", timeout=300)

    def browser_resize(self, width: int, height: int) -> Dict[str, Any]:
        return self._page_info_cmd(["resize", str(width), str(height)])

    # ==================== Page Methods ====================

    def page_goto(self, url: str) -> Dict[str, Any]:
        return self._page_info_cmd(["goto", url])

    def page_back(self) -> Dict[str, Any]:
        return self._page_info_cmd(["go-back"])

    def page_forward(self) -> Dict[str, Any]:
        return self._page_info_cmd(["go-forward"])

    def page_reload(self) -> Dict[str, Any]:
        return self._page_info_cmd(["reload"])

    def page_snapshot(self) -> Dict[str, Any]:
        result = self._run(["snapshot"])
        parsed = _parse_action_output(result.stdout)
        page = parsed.get('page', {})
        snapshot_file = parsed.get('snapshot_file', '')
        if not snapshot_file:
            snapshot_file = result.stdout.strip()
        return {
            'file': snapshot_file or '',
            'page_url': page.get('url', ''),
            'page_title': page.get('title', ''),
        }

    def _file_result_cmd(self, args: list) -> Dict[str, Any]:
        """Run command and return file path + page URL dict."""
        result = self._run(args)
        parsed = _parse_action_output(result.stdout)
        file_path = parsed.get('result', '')
        page = parsed.get('page', {})
        if not file_path:
            for line in result.stdout.strip().split('\n'):
                line = line.strip()
                if line and ('.' in line) and not line.startswith('#'):
                    file_path = line.strip('`')
                    break
        return {'file': file_path or '', 'page_url': page.get('url')}

    def page_screenshot(self, ref: Optional[str] = None) -> Dict[str, Any]:
        args = ["screenshot"]
        if ref:
            args.append(ref)
        return self._file_result_cmd(args)

    def page_pdf(self) -> Dict[str, Any]:
        return self._file_result_cmd(["pdf"])

    def page_eval(self, func: str, ref: Optional[str] = None, timeout: int = None) -> Dict[str, Any]:
        args = ["eval", func]
        if ref:
            args.append(ref)
        result = self._run(args, timeout=timeout)
        sections = _parse_markdown_sections(result.stdout)
        page = _parse_page_section(sections['Page']) if 'Page' in sections else {}
        eval_result = sections.get('Result', result.stdout.strip())
        return {
            'result': eval_result if eval_result else None,
            'page_url': page.get('url'),
            'page_title': page.get('title'),
        }

    # ==================== Interact Methods ====================

    def interact_click(self, ref: str, button: Optional[str] = None) -> Dict[str, Any]:
        args = ["click", ref]
        if button:
            args.append(button)
        return self._page_info_cmd(args)

    def interact_dblclick(self, ref: str, button: Optional[str] = None) -> Dict[str, Any]:
        args = ["dblclick", ref]
        if button:
            args.append(button)
        return self._page_info_cmd(args)

    def interact_fill(self, ref: str, text: str) -> Dict[str, Any]:
        return self._page_info_cmd(["fill", ref, text])

    def interact_type(self, ref: str, text: str) -> Dict[str, Any]:
        return self._page_info_cmd(["type", text])

    def interact_drag(self, start_ref: str, end_ref: str) -> Dict[str, Any]:
        return self._page_info_cmd(["drag", start_ref, end_ref])

    def interact_hover(self, ref: str) -> Dict[str, Any]:
        return self._page_info_cmd(["hover", ref])

    def interact_select(self, ref: str, value: str) -> Dict[str, Any]:
        return self._page_info_cmd(["select", ref, value])

    def interact_upload(self, file: str) -> Dict[str, Any]:
        return self._page_info_cmd(["upload", file])

    def interact_check(self, ref: str) -> Dict[str, Any]:
        return self._page_info_cmd(["check", ref])

    def interact_uncheck(self, ref: str) -> Dict[str, Any]:
        return self._page_info_cmd(["uncheck", ref])

    # ==================== Keyboard Methods ====================

    def keyboard_press(self, key: str) -> Dict[str, Any]:
        return self._page_info_cmd(["press", key])

    def keyboard_keydown(self, key: str) -> Dict[str, Any]:
        return self._page_info_cmd(["keydown", key])

    def keyboard_keyup(self, key: str) -> Dict[str, Any]:
        return self._page_info_cmd(["keyup", key])

    # ==================== Mouse Methods ====================

    def mouse_move(self, x: int, y: int) -> Dict[str, Any]:
        return self._page_info_cmd(["mousemove", str(x), str(y)])

    def mouse_down(self, button: Optional[str] = None) -> Dict[str, Any]:
        args = ["mousedown"]
        if button:
            args.append(button)
        return self._page_info_cmd(args)

    def mouse_up(self, button: Optional[str] = None) -> Dict[str, Any]:
        args = ["mouseup"]
        if button:
            args.append(button)
        return self._page_info_cmd(args)

    def mouse_wheel(self, dx: int, dy: int) -> Dict[str, Any]:
        return self._page_info_cmd(["mousewheel", str(dx), str(dy)])

    # ==================== Dialog Methods ====================

    def dialog_accept(self, prompt_text: Optional[str] = None) -> Dict[str, Any]:
        args = ["dialog-accept"]
        if prompt_text:
            args.append(prompt_text)
        return self._success_cmd(args, "Dialog accepted")

    def dialog_dismiss(self) -> Dict[str, Any]:
        return self._success_cmd(["dialog-dismiss"], "Dialog dismissed")

    # ==================== Tab Methods ====================

    def tab_list(self) -> List[Dict[str, Any]]:
        result = self._run(["tab-list"])
        return _parse_tab_list(result.stdout)

    def tab_new(self, url: Optional[str] = None) -> Dict[str, Any]:
        args = ["tab-new"]
        if url:
            args.append(url)
        return self._page_info_cmd(args)

    def tab_close(self, index: Optional[int] = None) -> Dict[str, Any]:
        args = ["tab-close"]
        if index is not None:
            args.append(str(index))
        return self._success_cmd(args, "Tab closed")

    def tab_select(self, index: int) -> Dict[str, Any]:
        return self._page_info_cmd(["tab-select", str(index)])

    # ==================== Cookie Methods ====================

    def cookie_list(self) -> List[Dict[str, Any]]:
        result = self._run(["cookie-list"])
        return _parse_cookie_list(result.stdout)

    def cookie_get(self, name: str) -> Dict[str, Any]:
        result = self._run(["cookie-get", name])
        raw = _parse_cookie_list(result.stdout)
        if raw:
            return raw[0]
        return {'name': name, 'value': result.stdout.strip()}

    def cookie_set(self, name: str, value: str) -> Dict[str, Any]:
        return self._success_cmd(["cookie-set", name, value], f"Cookie '{name}' set")

    def cookie_delete(self, name: str) -> Dict[str, Any]:
        return self._success_cmd(["cookie-delete", name], f"Cookie '{name}' deleted")

    def cookie_clear(self) -> Dict[str, Any]:
        return self._success_cmd(["cookie-clear"], "All cookies cleared")

    # ==================== Storage Methods (shared) ====================

    def _storage_list(self, prefix: str) -> List[Dict[str, str]]:
        result = self._run([f"{prefix}-list"])
        return _parse_storage_list(result.stdout)

    def _storage_get(self, prefix: str, key: str) -> Dict[str, str]:
        result = self._run([f"{prefix}-get", key])
        value = _parse_storage_get(result.stdout) or result.stdout.strip()
        return {'key': key, 'value': value}

    def _storage_set(self, prefix: str, display: str, key: str, value: str) -> Dict[str, Any]:
        return self._success_cmd([f"{prefix}-set", key, value], f"{display} '{key}' set")

    def _storage_delete(self, prefix: str, display: str, key: str) -> Dict[str, Any]:
        return self._success_cmd([f"{prefix}-delete", key], f"{display} '{key}' deleted")

    def _storage_clear(self, prefix: str, display: str) -> Dict[str, Any]:
        return self._success_cmd([f"{prefix}-clear"], f"{display} cleared")

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
        return self._success_cmd(["state-load", filename], f"State loaded from {filename}")

    def state_save(self, filename: Optional[str] = None) -> Dict[str, Any]:
        args = ["state-save"]
        if filename:
            args.append(filename)
        result = self._run(args)
        return {"success": True, "file": result.stdout.strip() or filename or "state.json"}

    # ==================== Network Methods ====================

    def network_requests(self) -> List[Dict[str, Any]]:
        result = self._run(["network"])
        return _parse_network_requests(result.stdout)

    def network_route(self, pattern: str) -> Dict[str, Any]:
        return self._success_cmd(["route", pattern], f"Route added: {pattern}")

    def network_route_list(self) -> List[Dict[str, str]]:
        result = self._run(["route-list"])
        return _parse_route_list(result.stdout)

    def network_unroute(self, pattern: Optional[str] = None) -> Dict[str, Any]:
        args = ["unroute"]
        if pattern:
            args.append(pattern)
        return self._success_cmd(args, "Routes removed")

    # ==================== DevTools Methods ====================

    def devtools_console(self, min_level: Optional[str] = None) -> List[Dict[str, Any]]:
        args = ["console"]
        if min_level:
            args.append(min_level)
        result = self._run(args)
        m = re.search(r'\[.*?\]\((.+?\.log)', result.stdout)
        if m:
            log_path = Path(m.group(1))
            if log_path.exists():
                log_content = log_path.read_text()
                return _parse_console_messages(log_content)
        return _parse_console_messages(result.stdout)

    def devtools_run_code(self, code: str) -> Dict[str, Any]:
        result = self._run(["run-code", code])
        parsed = _parse_action_output(result.stdout)
        page = parsed.get('page', {})
        return {
            'result': parsed.get('result', result.stdout.strip()) or None,
            'page_url': page.get('url'),
            'page_title': page.get('title'),
        }

    def devtools_tracing_start(self) -> Dict[str, Any]:
        return self._success_cmd(["tracing-start"], "Tracing started")

    def devtools_tracing_stop(self) -> Dict[str, Any]:
        return self._success_cmd(["tracing-stop"], "Tracing stopped")

    def devtools_video_start(self) -> Dict[str, Any]:
        return self._success_cmd(["video-start"], "Video recording started")

    def devtools_video_stop(self) -> Dict[str, Any]:
        return self._success_cmd(["video-stop"], "Video recording stopped")

    # ==================== Data Methods ====================

    def data_delete(self) -> Dict[str, Any]:
        return self._success_cmd(["delete-data"], "Session data deleted")

    # ============================================================
    # Convenience Methods (used by BrowserAutomation)
    # ============================================================

    def goto(self, url: str, wait_until: str = None) -> None:
        """Navigate to URL."""
        self.page_goto(url)
        if wait_until == "networkidle":
            self.wait_for_load_state("networkidle")

    def reload(self, wait_until: str = None) -> None:
        self.page_reload()

    @property
    def url(self) -> str:
        result = self._run(["eval", "window.location.href"])
        return _parse_eval_result(result.stdout) or ""

    def title(self) -> str:
        result = self._run(["eval", "document.title"])
        return _parse_eval_result(result.stdout) or ""

    def content(self) -> str:
        result = self._run(["eval", "document.documentElement.outerHTML"])
        return _parse_eval_result(result.stdout) or ""

    def evaluate(self, js: str, arg: Any = None) -> Any:
        """Evaluate JavaScript on the page.

        For functions that accept an argument, the arg is embedded as a JSON
        literal variable in the function body. If a dialog handler has been
        registered via once("dialog", ...), window.confirm and window.alert
        are overridden.
        """
        dialog_js = ""
        if self._dialog_handler:
            dialog_js = "window.confirm = () => true; window.alert = () => {}; "
            self._dialog_handler = None

        stripped = js.strip()
        is_function = (
            stripped.startswith("(") or
            stripped.startswith("async") or
            stripped.startswith("function")
        )

        if is_function:
            if arg is not None:
                expression = self._embed_arg_in_function(stripped, arg, dialog_js)
            elif dialog_js:
                expression = self._inject_into_function(stripped, dialog_js)
            else:
                expression = stripped
        else:
            expression = f"{dialog_js}{stripped}" if dialog_js else stripped

        result = self._run(["eval", expression], timeout=30)
        return _parse_eval_result(result.stdout)

    @staticmethod
    def _inject_into_function(func_str: str, inject: str) -> str:
        brace = func_str.find("{")
        if brace == -1:
            arrow = func_str.find("=>")
            if arrow == -1:
                return func_str
            prefix = func_str[:arrow + 2].strip()
            body = func_str[arrow + 2:].strip()
            return f"{prefix} {{ {inject} return {body}; }}"
        return func_str[:brace + 1] + " " + inject + func_str[brace + 1:]

    @staticmethod
    def _embed_arg_in_function(func_str: str, arg: Any, inject: str = "") -> str:
        arg_json = json.dumps(arg)
        paren_open = func_str.find("(")
        paren_close = func_str.find(")", paren_open + 1) if paren_open != -1 else -1
        if paren_open == -1 or paren_close == -1:
            return f"({func_str})({arg_json})"
        param = func_str[paren_open + 1:paren_close].strip()
        after_param = func_str[paren_close + 1:].strip()
        if after_param.startswith("=>"):
            after_param = after_param[2:].strip()
        prefix_part = func_str[:paren_open].strip()
        is_async = prefix_part.startswith("async") or prefix_part == "async"
        async_prefix = "async " if is_async else ""
        arg_decl = f"const {param} = {arg_json}; " if param else ""
        if after_param.startswith("{"):
            return f"{async_prefix}() => {{{inject}{arg_decl}{after_param[1:]}"
        else:
            return f"{async_prefix}() => {{ {inject}{arg_decl}return {after_param}; }}"

    # --- Element Selection ---

    def query_selector(self, selector: str) -> Optional[_ServiceElement]:
        if _is_playwright_selector(selector):
            for part in _split_selector(selector):
                part = part.strip()
                if _is_playwright_selector(part):
                    loc = self.locator(part)
                    if loc.count() > 0:
                        return loc.first
                else:
                    exists = self.evaluate(f"document.querySelector({json.dumps(part)}) !== null")
                    if exists:
                        return _ServiceElement(self, css=part)
            return None
        exists = self.evaluate(f"document.querySelector({json.dumps(selector)}) !== null")
        if exists:
            return _ServiceElement(self, css=selector)
        return None

    def query_selector_all(self, selector: str) -> List[_ServiceElement]:
        count = self.evaluate(f"document.querySelectorAll({json.dumps(selector)}).length")
        if not count:
            return []
        return [_ServiceElement(self, css=selector, index=i) for i in range(count)]

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
        timeout_s = timeout / 1000
        poll = 0.3
        elapsed = 0.0
        while elapsed < timeout_s:
            ready = self.evaluate("document.readyState")
            if ready == "complete":
                break
            time.sleep(poll)
            elapsed += poll
        if state != "networkidle":
            return
        self.evaluate("""() => {
            if (window.__pw_net_idle) return;
            window.__pw_net_idle = { pending: 0 };
            const orig = window.fetch;
            window.fetch = function() {
                window.__pw_net_idle.pending++;
                return orig.apply(this, arguments).finally(() => {
                    window.__pw_net_idle.pending--;
                });
            };
            const origXHR = XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.send = function() {
                window.__pw_net_idle.pending++;
                this.addEventListener('loadend', () => {
                    window.__pw_net_idle.pending--;
                }, {once: true});
                return origXHR.apply(this, arguments);
            };
        }""")
        stable_start = None
        while elapsed < timeout_s:
            pending = self.evaluate("(window.__pw_net_idle || {pending:0}).pending") or 0
            if pending == 0:
                if stable_start is None:
                    stable_start = time.monotonic()
                elif time.monotonic() - stable_start >= 0.5:
                    return
            else:
                stable_start = None
            time.sleep(poll)
            elapsed += poll

    def wait_for_selector(
        self, selector: str, *, state: str = None, timeout: int = None
    ) -> Optional[_ServiceElement]:
        timeout_s = (timeout or 30000) / 1000
        poll_interval = 0.5
        elapsed = 0.0
        sel_js = json.dumps(selector)
        while elapsed < timeout_s:
            if state in ("hidden", "detached"):
                gone = self.evaluate(f"document.querySelector({sel_js}) === null")
                if gone:
                    return None
            else:
                found = self.evaluate(f"""() => {{
                    const el = document.querySelector({sel_js});
                    if (!el) return false;
                    if ({json.dumps(state == 'visible')}) {{
                        return el.offsetParent !== null || el.getClientRects().length > 0;
                    }}
                    return true;
                }}""")
                if found:
                    return _ServiceElement(self, css=selector)
            time.sleep(poll_interval)
            elapsed += poll_interval
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
