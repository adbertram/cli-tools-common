"""PlaywrightService - unified browser automation service.

Single interface for all playwright-cli operations. Replaces both CLIPage
(subgroup commands) and PlaywrightClient (flat commands) with one class
that calls playwright-cli directly using flat commands.

Consumers:
- BrowserAutomation uses the CLIPage-compatible convenience methods
  (goto, evaluate, query_selector, locator, wait_for_selector, etc.)
- PlaywrightClient delegates all its methods to the low-level methods
  (page_goto, page_eval, browser_open, etc.)
"""

import base64
import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._debug_logging import configure_debug_logger

logger = logging.getLogger("cli_tools.playwright_service")
configure_debug_logger(logger)


class PlaywrightServiceError(Exception):
    """Error from PlaywrightService operations."""


# ============================================================
# Shared JS fragments for fill / select_option / check
# ============================================================

def _fill_js(text: str) -> str:
    """JS body to set value and dispatch input+change events on `el`."""
    return (
        f"el.value = {json.dumps(text)};"
        f" el.dispatchEvent(new Event('input', {{bubbles: true}}));"
        f" el.dispatchEvent(new Event('change', {{bubbles: true}}));"
    )


def _select_by_label_js(label: str) -> str:
    """JS body to select option by label text on `el`."""
    return (
        f"for (const o of el.options) {{"
        f" if (o.textContent.trim() === {json.dumps(label)}) {{"
        f" el.value = o.value;"
        f" el.dispatchEvent(new Event('change', {{bubbles: true}}));"
        f" break; }} }}"
    )


def _select_by_value_js(value: str) -> str:
    """JS body to select option by value on `el`."""
    return (
        f"el.value = {json.dumps(value)};"
        f" el.dispatchEvent(new Event('change', {{bubbles: true}}));"
    )


_CHECK_JS = (
    "if (!el.checked) {"
    " el.checked = true;"
    " el.dispatchEvent(new Event('change', {bubbles: true}));"
    " }"
)

_UNCHECK_JS = (
    "if (el.checked) {"
    " el.checked = false;"
    " el.dispatchEvent(new Event('change', {bubbles: true}));"
    " }"
)

_VISIBILITY_JS = "return el.offsetParent !== null || el.getClientRects().length > 0;"


# ============================================================
# Private Parsers (from playwright_cli/parsers.py)
# ============================================================


def _parse_markdown_sections(output: str) -> Dict[str, str]:
    sections: Dict[str, str] = {}
    current_section = None
    current_lines: List[str] = []
    for line in output.split('\n'):
        if line.startswith('### '):
            if current_section is not None:
                sections[current_section] = '\n'.join(current_lines).strip()
            current_section = line[4:].strip()
            current_lines = []
        elif current_section is not None:
            current_lines.append(line)
    if current_section is not None:
        sections[current_section] = '\n'.join(current_lines).strip()
    return sections


def _parse_page_section(content: str) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        'url': '', 'title': '', 'console_errors': 0, 'console_warnings': 0,
    }
    for line in content.split('\n'):
        line = line.strip().lstrip('- ')
        if line.startswith('Page URL:'):
            info['url'] = line[9:].strip()
        elif line.startswith('Page Title:'):
            info['title'] = line[11:].strip()
        elif line.startswith('Console'):
            m = re.search(r'(\d+)\s+error', line)
            if m:
                info['console_errors'] = int(m.group(1))
            m = re.search(r'(\d+)\s+warning', line)
            if m:
                info['console_warnings'] = int(m.group(1))
    return info


def _parse_file_path_section(content: str) -> Optional[str]:
    """Parse a markdown section to extract a file path (snapshot, screenshot, pdf, etc.)."""
    for line in content.split('\n'):
        line = line.strip().lstrip('- ')
        if not line or line.startswith('#'):
            continue
        m = re.match(r'\[.*?\]\((.+?)\)', line)
        if m:
            return m.group(1)
        path = line.strip('`').strip()
        if path:
            return path
    return None


def _parse_action_output(output: str) -> Dict[str, Any]:
    sections = _parse_markdown_sections(output)
    result: Dict[str, Any] = {}
    if 'Page' in sections:
        result['page'] = _parse_page_section(sections['Page'])
    if 'Snapshot' in sections:
        result['snapshot_file'] = _parse_file_path_section(sections['Snapshot'])
    if 'Result' in sections:
        result['result'] = _parse_file_path_section(sections['Result'])
    return result


def _parse_session_list(output: str) -> List[Dict[str, Any]]:
    output = output.strip()
    if not output:
        return []
    sessions = []
    current_session: Optional[Dict[str, Any]] = None
    for line in output.split('\n'):
        if line.startswith('#'):
            continue
        m = re.match(r'^- (\S+):$', line)
        if m:
            if current_session:
                sessions.append(current_session)
            current_session = {'name': m.group(1)}
            continue
        if current_session is not None:
            m = re.match(r'^\s+- ([\w-]+):\s*(.*)$', line)
            if m:
                key = m.group(1).replace('-', '_')
                value = m.group(2).strip()
                if key == 'browser_type':
                    current_session['browser_type'] = value
                elif key == 'user_data_dir':
                    current_session['user_data_dir'] = value
                elif key == 'headed':
                    current_session['headed'] = value.lower() == 'true'
                elif key == 'pid':
                    current_session['pid'] = int(value) if value.isdigit() else None
                elif key == 'status':
                    current_session['status'] = value
    if current_session:
        sessions.append(current_session)
    return sessions


def _parse_tab_list(output: str) -> List[Dict[str, Any]]:
    tabs = []
    for line in output.strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        m = re.match(r'^(\d+):\s+(.+?)(?:\s+-\s+(.+))?$', line)
        if m:
            tab = {'index': int(m.group(1)), 'url': m.group(2).strip()}
            if m.group(3):
                tab['title'] = m.group(3).strip()
            tabs.append(tab)
        else:
            tabs.append({'index': len(tabs), 'url': line})
    return tabs


def _parse_cookie_list(output: str) -> List[Dict[str, Any]]:
    output = output.strip()
    if not output:
        return []
    try:
        data = json.loads(output)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    cookies = []
    sections = output.split('\n\n')
    for section in sections:
        section = section.strip()
        if not section:
            continue
        cookie: Dict[str, Any] = {}
        for line in section.split('\n'):
            line = line.strip().lstrip('- ')
            if ':' in line:
                key, _, val = line.partition(':')
                key = key.strip().lower().replace(' ', '')
                val = val.strip()
                if key == 'name':
                    cookie['name'] = val
                elif key == 'value':
                    cookie['value'] = val
                elif key == 'domain':
                    cookie['domain'] = val
                elif key == 'path':
                    cookie['path'] = val
                elif key == 'expires':
                    cookie['expires'] = val
                elif key == 'httponly':
                    cookie['httpOnly'] = val.lower() in ('true', 'yes', '1')
                elif key == 'secure':
                    cookie['secure'] = val.lower() in ('true', 'yes', '1')
                elif key == 'samesite':
                    cookie['sameSite'] = val
            elif not cookie and line and '=' in line:
                name, _, value = line.partition('=')
                cookie['name'] = name.strip()
                cookie['value'] = value.strip()
        if cookie and cookie.get('name'):
            cookies.append(cookie)
    if not cookies:
        for line in output.split('\n'):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                name, _, value = line.partition('=')
                cookies.append({'name': name.strip(), 'value': value.strip()})
            elif ':' in line:
                name, _, value = line.partition(':')
                cookies.append({'name': name.strip(), 'value': value.strip()})
    return cookies


def _parse_storage_list(output: str) -> List[Dict[str, str]]:
    output = output.strip()
    if not output:
        return []
    try:
        data = json.loads(output)
        if isinstance(data, dict):
            return [{'key': k, 'value': str(v)} for k, v in data.items()]
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    items = []
    for line in output.split('\n'):
        line = line.strip().lstrip('- ')
        if not line or line.startswith('#'):
            continue
        if ':' in line:
            key, _, value = line.partition(':')
            items.append({'key': key.strip(), 'value': value.strip()})
        elif '=' in line:
            key, _, value = line.partition('=')
            items.append({'key': key.strip(), 'value': value.strip()})
    return items


def _parse_storage_get(output: str) -> Optional[str]:
    output = output.strip()
    if not output:
        return None
    if ':' in output:
        _, _, value = output.partition(':')
        return value.strip()
    return output


def _parse_network_requests(output: str) -> List[Dict[str, Any]]:
    output = output.strip()
    if not output:
        return []
    try:
        data = json.loads(output)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    requests = []
    for line in output.split('\n'):
        line = line.strip().lstrip('- ')
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) >= 2:
            req: Dict[str, Any] = {'method': parts[0]}
            if len(parts) >= 3 and parts[1].isdigit():
                req['status'] = int(parts[1])
                req['url'] = parts[2]
                if len(parts) >= 4:
                    req['content_type'] = parts[3]
            else:
                req['url'] = parts[1]
                if len(parts) >= 3:
                    if parts[2].isdigit():
                        req['status'] = int(parts[2])
                    else:
                        req['content_type'] = parts[2]
            requests.append(req)
    return requests


def _parse_route_list(output: str) -> List[Dict[str, str]]:
    output = output.strip()
    if not output:
        return []
    routes = []
    for line in output.split('\n'):
        line = line.strip().lstrip('- ')
        if not line or line.startswith('#'):
            continue
        routes.append({'pattern': line})
    return routes


def _parse_console_messages(output: str) -> List[Dict[str, Any]]:
    output = output.strip()
    if not output:
        return []
    try:
        data = json.loads(output)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    messages = []
    for line in output.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        msg: Dict[str, Any] = {'level': 'INFO', 'text': line}
        m = re.match(r'^\[(\w+)\]\s+(.+)$', line)
        if m:
            msg['level'] = m.group(1).upper()
            msg['text'] = m.group(2)
        url_match = re.search(r'(https?://\S+):(\d+)', msg['text'])
        if url_match:
            msg['url'] = url_match.group(1)
            msg['line'] = int(url_match.group(2))
        messages.append(msg)
    return messages


def _parse_eval_result(stdout: str) -> Any:
    """Extract the result field from playwright page eval JSON output."""
    text = stdout.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text if text not in ("undefined", "null") else None
    if isinstance(data, dict) and "result" in data:
        raw = data["result"]
    else:
        raw = data
    if raw is None or raw == "null" or raw == "undefined":
        return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            pass
    return raw


# ============================================================
# Selector Helpers (from cli_page.py)
# ============================================================


def _is_playwright_selector(selector: str) -> bool:
    return selector.startswith("text=") or ":has-text(" in selector


def _split_selector(selector: str) -> List[str]:
    parts: List[str] = []
    depth = 0
    current: List[str] = []
    in_quote: Optional[str] = None
    for ch in selector:
        if in_quote:
            current.append(ch)
            if ch == in_quote:
                in_quote = None
        elif ch in ('"', "'"):
            current.append(ch)
            in_quote = ch
        elif ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


# ============================================================
# PlaywrightService
# ============================================================


class PlaywrightService:
    """Unified browser automation service using playwright-cli flat commands.

    Provides two API layers:
    1. Low-level methods (page_goto, browser_open, etc.) returning dicts
       - Used by PlaywrightClient delegation
    2. CLIPage-compatible convenience methods (goto, evaluate, locator, etc.)
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

    def _clear_stale_lock(self) -> None:
        """Remove stale Chrome SingletonLock files from persistent profile."""
        profiles_dir = Path.home() / "Library" / "Caches" / "ms-playwright" / "daemon"
        for lock in profiles_dir.glob(f"*/ud-{self.session}-*/SingletonLock"):
            try:
                lock.unlink()
            except OSError:
                pass

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
    ) -> Dict[str, Any]:
        self._clear_stale_lock()
        args = ["open"]
        if persistent:
            args.append("--persistent")
        if profile:
            args.extend(["--profile", profile])
        if headed:
            args.append("--headed")
        if url:
            args.append(url)
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
    # CLIPage-Compatible Convenience Methods
    # ============================================================

    def goto(self, url: str, wait_until: str = None) -> None:
        """Navigate to URL (CLIPage-compatible)."""
        self._run(["goto", url])
        if wait_until == "networkidle":
            self.wait_for_load_state("networkidle")

    def reload(self, wait_until: str = None) -> None:
        self._run(["reload"])

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

    def query_selector(self, selector: str) -> Optional["_ServiceElement"]:
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

    def query_selector_all(self, selector: str) -> List["_ServiceElement"]:
        count = self.evaluate(f"document.querySelectorAll({json.dumps(selector)}).length")
        if not count:
            return []
        return [_ServiceElement(self, css=selector, index=i) for i in range(count)]

    # --- Locator API ---

    def locator(self, selector: str) -> "_ServiceLocator":
        return _ServiceLocator(self, selector)

    def get_by_role(self, role: str, *, name=None) -> "_ServiceLocator":
        return _ServiceLocator.from_role(self, role, name)

    def get_by_placeholder(self, text: str) -> "_ServiceLocator":
        return _ServiceLocator(self, f'[placeholder="{text}"]')

    # --- Direct Page Actions ---

    def fill(self, selector: str, text: str) -> None:
        sel_js = json.dumps(selector)
        self.evaluate(
            f"() => {{ const el = document.querySelector({sel_js});"
            f" if (!el) throw new Error('Element not found: {selector}');"
            f" {_fill_js(text)} }}"
        )

    def select_option(self, selector: str, *, label: str = None, value: str = None) -> None:
        sel_js = json.dumps(selector)
        if label:
            body = _select_by_label_js(label)
        elif value:
            body = _select_by_value_js(value)
        else:
            return
        self.evaluate(
            f"() => {{ const el = document.querySelector({sel_js});"
            f" if (!el) throw new Error('Select not found');"
            f" {body} }}"
        )

    def press(self, key: str) -> None:
        """Press a key (CLIPage-compatible, no return value)."""
        self._run(["press", key])

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
    ) -> Optional["_ServiceElement"]:
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
    def context(self) -> "_ServiceContext":
        return _ServiceContext(self)


# ============================================================
# _ServiceElement
# ============================================================


class _ServiceElement:
    """Element resolved by CSS selector (optionally indexed)."""

    def __init__(
        self,
        svc: PlaywrightService,
        css: str = None,
        index: int = None,
        *,
        js_expr: str = None,
    ):
        self._svc = svc
        self._css = css
        self._index = index
        self._js_expr = js_expr

    def _resolve(self) -> str:
        if self._js_expr:
            return self._js_expr
        sel = json.dumps(self._css)
        if self._index is not None:
            return f"document.querySelectorAll({sel})[{self._index}]"
        return f"document.querySelector({sel})"

    def _eval_on_el(self, body: str) -> Any:
        return self._svc.evaluate(
            f"() => {{ const el = {self._resolve()}; {body} }}"
        )

    def click(self) -> None:
        self._eval_on_el("if (el) el.click();")

    def fill(self, text: str) -> None:
        self._eval_on_el(f"if (!el) throw new Error('Element not found'); {_fill_js(text)}")

    def check(self) -> None:
        self._eval_on_el(f"if (el) {{ {_CHECK_JS} }}")

    def uncheck(self) -> None:
        self._eval_on_el(f"if (el) {{ {_UNCHECK_JS} }}")

    def select_option(self, value: str = None, *, label: str = None) -> None:
        if label:
            self._eval_on_el(f"if (!el) return; {_select_by_label_js(label)}")
        elif value:
            self._eval_on_el(f"if (!el) return; {_select_by_value_js(value)}")

    def press(self, key: str) -> None:
        self._eval_on_el("if (el) el.focus();")
        self._svc._run(["press", key])

    def text_content(self) -> Optional[str]:
        return self._eval_on_el("return el ? el.textContent : null;")

    def is_visible(self, *, timeout: int = None) -> bool:
        return bool(self._eval_on_el(f"if (!el) return false; {_VISIBILITY_JS}"))

    def count(self) -> int:
        return 1


# ============================================================
# _ServiceLocator
# ============================================================


class _ServiceLocator:
    """Lazy element locator supporting CSS, text=, :has-text(), and role selectors."""

    def __init__(self, svc: PlaywrightService, selector: str):
        self._svc = svc
        self._selector = selector
        self._parent: Optional["_ServiceLocator"] = None

    @classmethod
    def from_role(cls, svc: PlaywrightService, role: str, name=None) -> "_ServiceLocator":
        loc = cls.__new__(cls)
        loc._svc = svc
        loc._selector = None
        loc._parent = None
        loc._role = role
        loc._role_name = name
        return loc

    def _js_find_all(self) -> str:
        if hasattr(self, "_role"):
            return self._build_role_js()
        return self._build_selector_js(self._selector)

    def _build_selector_js(self, sel: str) -> str:
        if sel.startswith("text="):
            return self._build_text_js(sel[5:])
        if ":has-text(" in sel:
            return self._build_has_text_js(sel)
        escaped = json.dumps(sel)
        if self._parent:
            return f"{self._parent._js_find_all()}.flatMap(p => Array.from(p.querySelectorAll({escaped})))"
        return f"Array.from(document.querySelectorAll({escaped}))"

    @staticmethod
    def _build_text_js(text_part: str) -> str:
        if text_part.startswith("/"):
            return (
                f"Array.from(document.querySelectorAll('*'))"
                f".filter(el => el.children.length === 0 && {text_part}.test(el.textContent))"
            )
        if text_part.startswith('"') or text_part.startswith("'"):
            exact = text_part.strip('"').strip("'")
            return (
                f"Array.from(document.querySelectorAll('*'))"
                f".filter(el => el.textContent.trim() === {json.dumps(exact)})"
            )
        return (
            f"Array.from(document.querySelectorAll('*'))"
            f".filter(el => el.textContent.toLowerCase().includes({json.dumps(text_part.lower())}))"
        )

    @staticmethod
    def _build_has_text_js(sel: str) -> str:
        m = re.match(r'^(.*?):has-text\(\s*["\'](.+?)["\']\s*\)$', sel)
        if m:
            css_part = m.group(1) or "*"
            text_part = m.group(2)
            return (
                f"Array.from(document.querySelectorAll({json.dumps(css_part)}))"
                f".filter(el => el.textContent.includes({json.dumps(text_part)}))"
            )
        return f"Array.from(document.querySelectorAll({json.dumps(sel)}))"

    def _build_role_js(self) -> str:
        role_map = {
            "button": "button, input[type='button'], input[type='submit'], [role='button']",
            "link": "a[href], [role='link']",
            "textbox": "input[type='text'], input:not([type]), textarea, [role='textbox']",
            "checkbox": "input[type='checkbox'], [role='checkbox']",
            "radio": "input[type='radio'], [role='radio']",
            "combobox": "select, [role='combobox']",
            "heading": "h1, h2, h3, h4, h5, h6, [role='heading']",
            "spinbutton": "input[type='number'], [role='spinbutton']",
            "listitem": "li, [role='listitem']",
        }
        css = role_map.get(self._role, f"[role='{self._role}']")
        escaped_css = json.dumps(css)
        # Scope to parent if set
        if self._parent:
            base = f"{self._parent._js_find_all()}.flatMap(p => Array.from(p.querySelectorAll({escaped_css})))"
        else:
            base = f"Array.from(document.querySelectorAll({escaped_css}))"
        name = self._role_name
        if name is None:
            return base
        if hasattr(name, "pattern"):
            flags = "i" if name.flags & re.IGNORECASE else ""
            js_re = f"/{name.pattern}/{flags}"
            return (
                f"{base}"
                f".filter(el => {js_re}.test(el.textContent.trim())"
                f" || {js_re}.test(el.getAttribute('aria-label') || '')"
                f" || {js_re}.test(el.value || ''))"
            )
        escaped_name = json.dumps(str(name))
        return (
            f"{base}"
            f".filter(el => el.textContent.trim().includes({escaped_name})"
            f" || (el.getAttribute('aria-label') || '').includes({escaped_name})"
            f" || (el.value || '').includes({escaped_name}))"
        )

    def _eval_on_first(self, body: str, *, require: bool = False) -> Any:
        guard = (
            'if (els.length === 0) throw new Error("No element found for locator");'
            if require else
            'if (els.length === 0) return null;'
        )
        return self._svc.evaluate(
            f"() => {{ const els = {self._js_find_all()}; {guard} const el = els[0]; {body} }}"
        )

    def click(self) -> None:
        self._eval_on_first("el.click();", require=True)

    def fill(self, text: str) -> None:
        self._eval_on_first(_fill_js(text), require=True)

    def check(self) -> None:
        self._eval_on_first(_CHECK_JS)

    def select_option(self, value: str = None, *, label: str = None) -> None:
        if label:
            self._eval_on_first(_select_by_label_js(label))
        elif value:
            self._eval_on_first(_select_by_value_js(value))

    def press(self, key: str) -> None:
        self._eval_on_first("el.focus();")
        self._svc._run(["press", key])

    def count(self) -> int:
        result = self._svc.evaluate(f"({self._js_find_all()}).length")
        return int(result) if result else 0

    @property
    def first(self) -> _ServiceElement:
        return _ServiceElement(self._svc, js_expr=f"({self._js_find_all()})[0]")

    def is_visible(self, *, timeout: int = None) -> bool:
        return bool(self._eval_on_first(_VISIBILITY_JS))

    def is_enabled(self) -> bool:
        return bool(self._eval_on_first("return !el.disabled;"))

    def text_content(self) -> Optional[str]:
        return self._eval_on_first("return el.textContent;")

    def all_text_contents(self) -> List[str]:
        result = self._svc.evaluate(f"({self._js_find_all()}).map(el => el.textContent || '')")
        return result if isinstance(result, list) else []

    def all(self) -> List["_ServiceLocator"]:
        cnt = self.count()
        return [_IndexedLocator(self._svc, self, i) for i in range(cnt)]

    def locator(self, child_selector: str) -> "_ServiceLocator":
        child = _ServiceLocator(self._svc, child_selector)
        child._parent = self
        return child

    def filter(self, *, has_text=None) -> "_ServiceLocator":
        """Return a filtered locator that matches elements containing the given text."""
        if has_text is None:
            return self
        return _FilteredLocator(self._svc, self, has_text)

    def get_by_placeholder(self, text: str) -> "_ServiceLocator":
        return self.locator(f'[placeholder="{text}"]')

    def get_by_role(self, role: str, *, name=None) -> "_ServiceLocator":
        """Get elements by role, scoped to this locator's matches."""
        role_loc = _ServiceLocator.from_role(self._svc, role, name)
        role_loc._parent = self
        return role_loc


# ============================================================
# _FilteredLocator
# ============================================================


class _FilteredLocator(_ServiceLocator):
    """Locator that filters parent results by text content."""

    def __init__(self, svc: PlaywrightService, parent: _ServiceLocator, has_text):
        super().__init__(svc, parent._selector or "")
        self._filter_parent = parent
        self._has_text = has_text

    def _js_find_all(self) -> str:
        base = self._filter_parent._js_find_all()
        if hasattr(self._has_text, "pattern"):  # re.Pattern
            flags = "i" if self._has_text.flags & re.IGNORECASE else ""
            return f"{base}.filter(el => /{self._has_text.pattern}/{flags}.test(el.textContent))"
        return f"{base}.filter(el => el.textContent.includes({json.dumps(str(self._has_text))}))"


# ============================================================
# _IndexedLocator
# ============================================================


class _IndexedLocator(_ServiceLocator):
    """Targets a specific index within a parent locator's matches."""

    def __init__(self, svc: PlaywrightService, parent: _ServiceLocator, index: int):
        super().__init__(svc, parent._selector or "")
        self._parent_locator = parent
        self._idx = index

    def _js_find_all(self) -> str:
        return f"[({self._parent_locator._js_find_all()})[{self._idx}]].filter(Boolean)"

    def locator(self, child_selector: str) -> _ServiceLocator:
        parent_js = self._parent_locator._js_find_all()
        return _ScopedLocator(self._svc, child_selector, parent_js, self._idx)

    def all_text_contents(self) -> List[str]:
        js = self._parent_locator._js_find_all()
        result = self._svc.evaluate(f"""() => {{
            const el = ({js})[{self._idx}];
            return el ? [el.textContent || ''] : [];
        }}""")
        return result if isinstance(result, list) else []


class _ScopedLocator(_ServiceLocator):
    """A locator scoped to a specific parent element by index."""

    def __init__(self, svc: PlaywrightService, selector: str, parent_js: str, parent_index: int):
        super().__init__(svc, selector)
        self._parent_js = parent_js
        self._parent_index = parent_index

    def _js_find_all(self) -> str:
        return (
            f"Array.from((({self._parent_js})[{self._parent_index}]"
            f" || document).querySelectorAll({json.dumps(self._selector)}))"
        )


# ============================================================
# _ServiceContext
# ============================================================


class _ServiceContext:
    """Minimal browser context wrapper for cookie and request operations."""

    def __init__(self, svc: PlaywrightService):
        self._svc = svc

    def cookies(self, urls: List[str] = None) -> List[Dict[str, Any]]:
        result = self._svc._run(["cookie-list"], check=False)
        if result.returncode != 0:
            return []
        cookies = _parse_cookie_list(result.stdout)
        if urls and cookies:
            domains = set()
            for url in urls:
                m = re.match(r"https?://([^/]+)", url)
                if m:
                    domain = m.group(1)
                    domains.add(domain)
                    parts = domain.split(".")
                    if len(parts) > 2:
                        domains.add("." + ".".join(parts[-2:]))
            cookies = [
                c for c in cookies
                if any(
                    c.get("domain", "").endswith(d) or d.endswith(c.get("domain", "").lstrip("."))
                    for d in domains
                )
            ]
        return cookies

    def add_cookies(self, cookies: List[Dict[str, Any]]) -> None:
        for cookie in cookies:
            name = cookie.get("name", "")
            value = cookie.get("value", "")
            if name and value:
                self._svc._run(["cookie-set", name, value], check=False)

    @property
    def pages(self) -> List[PlaywrightService]:
        return [self._svc]

    @property
    def request(self) -> "_ServiceRequest":
        return _ServiceRequest(self._svc)


class _ServiceRequest:
    """HTTP request interface using the browser's cookies via fetch()."""

    def __init__(self, svc: PlaywrightService):
        self._svc = svc

    def get(self, url: str) -> "_ServiceResponse":
        result = self._svc.evaluate(f"""async () => {{
            const resp = await fetch({json.dumps(url)}, {{credentials: 'include'}});
            const buffer = await resp.arrayBuffer();
            const bytes = new Uint8Array(buffer);
            let binary = '';
            for (let i = 0; i < bytes.length; i++) {{
                binary += String.fromCharCode(bytes[i]);
            }}
            return {{
                ok: resp.ok,
                status: resp.status,
                statusText: resp.statusText,
                body_b64: btoa(binary),
            }};
        }}""")
        return _ServiceResponse(result)


class _ServiceResponse:
    """Response from a browser-context HTTP request."""

    def __init__(self, data):
        self._data = data or {}

    @property
    def ok(self) -> bool:
        return self._data.get("ok", False)

    @property
    def status(self) -> int:
        return self._data.get("status", 0)

    @property
    def status_text(self) -> str:
        return self._data.get("statusText", "")

    def body(self) -> bytes:
        b64 = self._data.get("body_b64", "")
        return base64.b64decode(b64) if b64 else b""
