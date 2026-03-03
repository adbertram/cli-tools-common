"""Playwright CLI Page wrapper.

Provides CLIPage, CLIElement, CLILocator, and CLIContext classes that implement
enough of the Playwright Python Page API for BrowserAutomation subclasses to
work unchanged. All operations delegate to the ``playwright`` CLI via subprocess.

The ``playwright`` CLI outputs JSON from ``page eval``::

    {"result": <value>, "page_url": "...", "page_title": "..."}

This module parses that JSON and extracts the ``result`` field.
"""

import base64
import json
import logging
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("cli_tools.cli_page")


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


class CLIPageError(Exception):
    """Error from CLIPage operations."""
    pass


# ============================================================
# CLIPage
# ============================================================


class CLIPage:
    """Wraps the playwright CLI to provide a Playwright Page-like interface.

    Every method delegates to a subprocess call to the ``playwright`` CLI
    using the given named session (``--session`` flag).
    """

    def __init__(self, session_name: str):
        self.session_name = session_name
        self._dialog_handler = None

    # --- subprocess runner ---

    def _run(self, args: list, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
        """Run a playwright CLI command with the named session."""
        cmd = ["playwright", "--session", self.session_name] + args
        logger.debug("_run: cmd=%s timeout=%d check=%s", cmd, timeout, check)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            logger.debug("_run: returncode=%d", result.returncode)
            if result.stdout and result.stdout.strip():
                logger.debug("_run: stdout=%s", result.stdout.strip()[:1000])
            if result.stderr and result.stderr.strip():
                logger.debug("_run: stderr=%s", result.stderr.strip()[:1000])
            if check and result.returncode != 0:
                error_msg = result.stderr.strip() or result.stdout.strip() or "Command failed"
                logger.debug("_run: check failed, raising CLIPageError: %s", error_msg)
                raise CLIPageError(f"playwright error: {error_msg}")
            return result
        except subprocess.TimeoutExpired:
            logger.debug("_run: TIMEOUT after %ds for cmd=%s", timeout, cmd)
            raise CLIPageError(f"Command timed out after {timeout}s")
        except FileNotFoundError:
            logger.debug("_run: playwright CLI not found in PATH")
            raise CLIPageError("playwright CLI not found in PATH")
        except CLIPageError:
            raise
        except Exception as e:
            logger.debug("_run: unexpected exception: %s", e)
            raise CLIPageError(f"Failed to run command: {e}")

    def _parse_eval_result(self, stdout: str) -> Any:
        """Extract the ``result`` field from playwright page eval JSON output."""
        text = stdout.strip()
        if not text:
            return None

        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            # Non-JSON output — return raw string
            return text if text not in ("undefined", "null") else None

        if isinstance(data, dict) and "result" in data:
            raw = data["result"]
        else:
            raw = data

        if raw is None or raw == "null" or raw == "undefined":
            return None

        # If the result is a JSON-encoded string, try to parse it
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                pass

        return raw

    # --- Navigation ---

    def goto(self, url: str, wait_until: str = None) -> None:
        self._run(["page", "goto", url])
        if wait_until == "networkidle":
            self.wait_for_load_state("networkidle")

    def reload(self, wait_until: str = None) -> None:
        self._run(["page", "reload"])

    # --- Properties ---

    @property
    def url(self) -> str:
        result = self._run(["page", "eval", "window.location.href"])
        return self._parse_eval_result(result.stdout) or ""

    def title(self) -> str:
        result = self._run(["page", "eval", "document.title"])
        return self._parse_eval_result(result.stdout) or ""

    def content(self) -> str:
        result = self._run(["page", "eval", "document.documentElement.outerHTML"])
        return self._parse_eval_result(result.stdout) or ""

    # --- JavaScript Evaluation ---

    def evaluate(self, js: str, arg: Any = None) -> Any:
        """Evaluate JavaScript on the page.

        The playwright CLI ``page eval`` command auto-invokes functions, so
        arrow functions and ``function`` expressions are passed directly
        (no IIFE wrapper).  Plain expressions are also passed as-is.

        For functions that accept an argument, the arg is embedded as a JSON
        literal variable in the function body.

        If a dialog handler has been registered via ``once("dialog", ...)``,
        ``window.confirm`` and ``window.alert`` are overridden at the start
        of the function body.
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
                # Rewrite: (param) => { body } → () => { const param = <arg>; body }
                expression = self._embed_arg_in_function(stripped, arg, dialog_js)
            elif dialog_js:
                expression = self._inject_into_function(stripped, dialog_js)
            else:
                expression = stripped
        else:
            # Plain expression — prepend dialog overrides if needed
            expression = f"{dialog_js}{stripped}" if dialog_js else stripped

        result = self._run(["page", "eval", expression], timeout=30)
        return self._parse_eval_result(result.stdout)

    @staticmethod
    def _inject_into_function(func_str: str, inject: str) -> str:
        """Inject code at the start of a function body."""
        brace = func_str.find("{")
        if brace == -1:
            # Arrow without braces: () => expr → () => { inject; return expr; }
            arrow = func_str.find("=>")
            if arrow == -1:
                return func_str
            prefix = func_str[:arrow + 2].strip()
            body = func_str[arrow + 2:].strip()
            return f"{prefix} {{ {inject} return {body}; }}"
        return func_str[:brace + 1] + " " + inject + func_str[brace + 1:]

    @staticmethod
    def _embed_arg_in_function(func_str: str, arg: Any, inject: str = "") -> str:
        """Rewrite a function to embed an arg as a local variable.

        Transforms ``(param) => { body }`` into
        ``() => { const param = <arg_json>; body }``.
        """
        arg_json = json.dumps(arg)

        # Find the parameter name between parens
        paren_open = func_str.find("(")
        paren_close = func_str.find(")", paren_open + 1) if paren_open != -1 else -1
        if paren_open == -1 or paren_close == -1:
            # Can't parse — fall back to expression evaluation
            return f"({func_str})({arg_json})"

        param = func_str[paren_open + 1:paren_close].strip()
        after_param = func_str[paren_close + 1:].strip()

        # Remove leading => if arrow function
        if after_param.startswith("=>"):
            after_param = after_param[2:].strip()

        prefix_part = func_str[:paren_open].strip()
        # Reconstruct as no-arg function
        is_async = prefix_part.startswith("async") or prefix_part == "async"
        async_prefix = "async " if is_async else ""

        arg_decl = f"const {param} = {arg_json}; " if param else ""

        if after_param.startswith("{"):
            # Block body
            return f"{async_prefix}() => {{{inject}{arg_decl}{after_param[1:]}"
        else:
            # Expression body
            return f"{async_prefix}() => {{ {inject}{arg_decl}return {after_param}; }}"

    # --- Element Selection ---

    def query_selector(self, selector: str) -> Optional["CLIElement"]:
        """Find a single element. Handles both CSS and Playwright text selectors."""
        if _is_playwright_selector(selector):
            # May be comma-separated with mixed CSS and Playwright selectors
            for part in _split_selector(selector):
                part = part.strip()
                if _is_playwright_selector(part):
                    loc = self.locator(part)
                    if loc.count() > 0:
                        return loc.first
                else:
                    exists = self.evaluate(f"document.querySelector({json.dumps(part)}) !== null")
                    if exists:
                        return CLIElement(self, css=part)
            return None

        exists = self.evaluate(f"document.querySelector({json.dumps(selector)}) !== null")
        if exists:
            return CLIElement(self, css=selector)
        return None

    def query_selector_all(self, selector: str) -> List["CLIElement"]:
        count = self.evaluate(f"document.querySelectorAll({json.dumps(selector)}).length")
        if not count:
            return []
        return [CLIElement(self, css=selector, index=i) for i in range(count)]

    # --- Locator API ---

    def locator(self, selector: str) -> "CLILocator":
        return CLILocator(self, selector)

    def get_by_role(self, role: str, *, name=None) -> "CLILocator":
        return CLILocator.from_role(self, role, name)

    # --- Direct Page Actions (brickowl uses page.fill / page.select_option) ---

    def fill(self, selector: str, text: str) -> None:
        self.evaluate(f"""() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) throw new Error('Element not found: {selector}');
            el.value = {json.dumps(text)};
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
        }}""")

    def select_option(self, selector: str, *, label: str = None, value: str = None) -> None:
        sel_js = json.dumps(selector)
        if label:
            self.evaluate(f"""() => {{
                const s = document.querySelector({sel_js});
                if (!s) throw new Error('Select not found');
                for (const o of s.options) {{
                    if (o.textContent.trim() === {json.dumps(label)}) {{
                        s.value = o.value;
                        s.dispatchEvent(new Event('change', {{bubbles: true}}));
                        break;
                    }}
                }}
            }}""")
        elif value:
            self.evaluate(f"""() => {{
                const s = document.querySelector({sel_js});
                if (!s) throw new Error('Select not found');
                s.value = {json.dumps(value)};
                s.dispatchEvent(new Event('change', {{bubbles: true}}));
            }}""")

    # --- Waiting ---

    def wait_for_timeout(self, ms: int) -> None:
        time.sleep(ms / 1000)

    def wait_for_load_state(self, state: str = "load", timeout: int = 30000) -> None:
        """Wait for page load state.

        Args:
            state: ``"load"`` waits for ``document.readyState === 'complete'``.
                   ``"networkidle"`` additionally waits until no in-flight
                   fetches for 500 ms.
            timeout: Maximum wait time in milliseconds.
        """
        timeout_s = timeout / 1000
        poll = 0.3
        elapsed = 0.0

        # Phase 1: document.readyState === "complete"
        while elapsed < timeout_s:
            ready = self.evaluate("document.readyState")
            if ready == "complete":
                break
            time.sleep(poll)
            elapsed += poll

        if state != "networkidle":
            return

        # Phase 2: wait until no in-flight fetches for 500 ms
        # Inject a fetch observer that tracks pending requests
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

        # Wait for pending count to stay at 0 for 500 ms
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
    ) -> Optional["CLIElement"]:
        """Poll until an element matches the desired state."""
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
                    return CLIElement(self, css=selector)

            time.sleep(poll_interval)
            elapsed += poll_interval

        if state not in ("hidden", "detached"):
            raise CLIPageError(f"Timeout waiting for selector: {selector}")
        return None

    # --- Event Handling ---

    def once(self, event: str, callback) -> None:
        """Register a one-time event handler.

        Supports ``"dialog"`` — overrides ``window.confirm``/``window.alert``
        on the next ``evaluate()`` call so the dialog is auto-accepted.
        """
        if event == "dialog":
            self._dialog_handler = callback

    # --- Context ---

    @property
    def context(self) -> "CLIContext":
        return CLIContext(self)


# ============================================================
# CLIElement
# ============================================================


class CLIElement:
    """An element resolved by CSS selector (optionally indexed).

    Can also be backed by a raw JS expression (``js_expr``) for elements
    that can't be addressed by a simple CSS selector (e.g. role-based or
    text-based matches).
    """

    def __init__(
        self,
        page: CLIPage,
        css: str = None,
        index: int = None,
        *,
        js_expr: str = None,
    ):
        self._page = page
        self._css = css
        self._index = index
        self._js_expr = js_expr

    def _resolve(self) -> str:
        """JS expression that evaluates to this DOM element."""
        if self._js_expr:
            return self._js_expr
        sel = json.dumps(self._css)
        if self._index is not None:
            return f"document.querySelectorAll({sel})[{self._index}]"
        return f"document.querySelector({sel})"

    # --- helpers ---

    def _eval_on_el(self, body: str) -> Any:
        """Evaluate JS with ``el`` bound to this element."""
        return self._page.evaluate(
            f"() => {{ const el = {self._resolve()}; {body} }}"
        )

    # --- actions ---

    def click(self) -> None:
        self._eval_on_el("if (el) el.click();")

    def fill(self, text: str) -> None:
        self._eval_on_el(
            f"if (!el) throw new Error('Element not found');"
            f" el.value = {json.dumps(text)};"
            f" el.dispatchEvent(new Event('input', {{bubbles: true}}));"
            f" el.dispatchEvent(new Event('change', {{bubbles: true}}));"
        )

    def check(self) -> None:
        self._eval_on_el(
            "if (el && !el.checked) {"
            " el.checked = true;"
            " el.dispatchEvent(new Event('change', {bubbles: true}));"
            " }"
        )

    def uncheck(self) -> None:
        self._eval_on_el(
            "if (el && el.checked) {"
            " el.checked = false;"
            " el.dispatchEvent(new Event('change', {bubbles: true}));"
            " }"
        )

    def select_option(self, value: str = None, *, label: str = None) -> None:
        if label:
            self._eval_on_el(
                f"if (!el) return;"
                f" for (const o of el.options) {{"
                f" if (o.textContent.trim() === {json.dumps(label)}) {{"
                f" el.value = o.value;"
                f" el.dispatchEvent(new Event('change', {{bubbles: true}}));"
                f" break; }} }}"
            )
        elif value:
            self._eval_on_el(
                f"if (!el) return;"
                f" el.value = {json.dumps(value)};"
                f" el.dispatchEvent(new Event('change', {{bubbles: true}}));"
            )

    def press(self, key: str) -> None:
        self._eval_on_el("if (el) el.focus();")
        self._page._run(["keyboard", "press", key])

    # --- queries ---

    def text_content(self) -> Optional[str]:
        return self._eval_on_el("return el ? el.textContent : null;")

    def is_visible(self, *, timeout: int = None) -> bool:
        return bool(self._eval_on_el(
            "if (!el) return false;"
            " return el.offsetParent !== null || el.getClientRects().length > 0;"
        ))

    def count(self) -> int:
        return 1


# ============================================================
# CLILocator
# ============================================================


class CLILocator:
    """Lazy element locator supporting CSS, text=, :has-text(), and role selectors.

    Unlike CLIElement, locators are not resolved until an action is performed.
    They support ``.count()`` and ``.first`` for introspection.
    """

    def __init__(self, page: CLIPage, selector: str):
        self._page = page
        self._selector = selector
        self._parent: Optional["CLILocator"] = None

    @classmethod
    def from_role(cls, page: CLIPage, role: str, name=None) -> "CLILocator":
        """Create a locator from a role + optional name filter."""
        loc = cls.__new__(cls)
        loc._page = page
        loc._selector = None
        loc._parent = None
        loc._role = role
        loc._role_name = name
        return loc

    # --- JS generation ---

    def _js_find_all(self) -> str:
        """Return a JS expression evaluating to an Array of matching elements."""
        if hasattr(self, "_role"):
            return self._build_role_js()
        return self._build_selector_js(self._selector)

    def _build_selector_js(self, sel: str) -> str:
        # text=... selectors
        if sel.startswith("text="):
            return self._build_text_js(sel[5:])

        # tag:has-text("...")
        if ":has-text(" in sel:
            return self._build_has_text_js(sel)

        # Standard CSS — scope to parent if chained
        escaped = json.dumps(sel)
        if self._parent:
            return f"{self._parent._js_find_all()}.flatMap(p => Array.from(p.querySelectorAll({escaped})))"
        return f"Array.from(document.querySelectorAll({escaped}))"

    @staticmethod
    def _build_text_js(text_part: str) -> str:
        # Regex: text=/pattern/flags
        if text_part.startswith("/"):
            return (
                f"Array.from(document.querySelectorAll('*'))"
                f".filter(el => el.children.length === 0 && {text_part}.test(el.textContent))"
            )
        # Exact: text="value"
        if text_part.startswith('"') or text_part.startswith("'"):
            exact = text_part.strip('"').strip("'")
            return (
                f"Array.from(document.querySelectorAll('*'))"
                f".filter(el => el.textContent.trim() === {json.dumps(exact)})"
            )
        # Contains (case-insensitive): text=value
        return (
            f"Array.from(document.querySelectorAll('*'))"
            f".filter(el => el.textContent.toLowerCase().includes({json.dumps(text_part.lower())}))"
        )

    @staticmethod
    def _build_has_text_js(sel: str) -> str:
        # Parse: css_part:has-text("text_part")
        m = re.match(r'^(.*?):has-text\(\s*["\'](.+?)["\']\s*\)$', sel)
        if m:
            css_part = m.group(1) or "*"
            text_part = m.group(2)
            return (
                f"Array.from(document.querySelectorAll({json.dumps(css_part)}))"
                f".filter(el => el.textContent.includes({json.dumps(text_part)}))"
            )
        # Fallback — treat whole thing as CSS (will probably fail, but let the
        # browser report the error rather than silently returning nothing)
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
        }
        css = role_map.get(self._role, f"[role='{self._role}']")
        escaped_css = json.dumps(css)

        name = self._role_name
        if name is None:
            return f"Array.from(document.querySelectorAll({escaped_css}))"

        # re.compile() pattern
        if hasattr(name, "pattern"):
            flags = "i" if name.flags & re.IGNORECASE else ""
            js_re = f"/{name.pattern}/{flags}"
            return (
                f"Array.from(document.querySelectorAll({escaped_css}))"
                f".filter(el => {js_re}.test(el.textContent.trim())"
                f" || {js_re}.test(el.getAttribute('aria-label') || '')"
                f" || {js_re}.test(el.value || ''))"
            )

        # Plain string name
        escaped_name = json.dumps(str(name))
        return (
            f"Array.from(document.querySelectorAll({escaped_css}))"
            f".filter(el => el.textContent.trim().includes({escaped_name})"
            f" || (el.getAttribute('aria-label') || '').includes({escaped_name})"
            f" || (el.value || '').includes({escaped_name}))"
        )

    # --- helpers ---

    def _eval_on_first(self, body: str, *, require: bool = False) -> Any:
        """Evaluate JS with ``el`` bound to the first matching element."""
        guard = (
            'if (els.length === 0) throw new Error("No element found for locator");'
            if require else
            'if (els.length === 0) return null;'
        )
        return self._page.evaluate(
            f"() => {{ const els = {self._js_find_all()}; {guard} const el = els[0]; {body} }}"
        )

    # --- actions (resolve first match) ---

    def click(self) -> None:
        self._eval_on_first("el.click();", require=True)

    def fill(self, text: str) -> None:
        self._eval_on_first(
            f"el.value = {json.dumps(text)};"
            f" el.dispatchEvent(new Event('input', {{bubbles: true}}));"
            f" el.dispatchEvent(new Event('change', {{bubbles: true}}));",
            require=True,
        )

    def check(self) -> None:
        self._eval_on_first(
            "if (!el.checked) {"
            " el.checked = true;"
            " el.dispatchEvent(new Event('change', {bubbles: true}));"
            " }"
        )

    def select_option(self, value: str = None, *, label: str = None) -> None:
        if label:
            self._eval_on_first(
                f"for (const o of el.options) {{"
                f" if (o.textContent.trim() === {json.dumps(label)}) {{"
                f" el.value = o.value;"
                f" el.dispatchEvent(new Event('change', {{bubbles: true}}));"
                f" break; }} }}"
            )
        elif value:
            self._eval_on_first(
                f"el.value = {json.dumps(value)};"
                f" el.dispatchEvent(new Event('change', {{bubbles: true}}));"
            )

    def press(self, key: str) -> None:
        self._eval_on_first("el.focus();")
        self._page._run(["keyboard", "press", key])

    # --- queries ---

    def count(self) -> int:
        result = self._page.evaluate(f"({self._js_find_all()}).length")
        return int(result) if result else 0

    @property
    def first(self) -> CLIElement:
        return CLIElement(self._page, js_expr=f"({self._js_find_all()})[0]")

    def is_visible(self, *, timeout: int = None) -> bool:
        return bool(self._eval_on_first(
            "return el.offsetParent !== null || el.getClientRects().length > 0;"
        ))

    def is_enabled(self) -> bool:
        return bool(self._eval_on_first("return !el.disabled;"))

    def text_content(self) -> Optional[str]:
        return self._eval_on_first("return el.textContent;")

    def all_text_contents(self) -> List[str]:
        result = self._page.evaluate(f"({self._js_find_all()}).map(el => el.textContent || '')")
        return result if isinstance(result, list) else []

    def all(self) -> List["CLILocator"]:
        cnt = self.count()
        return [_IndexedLocator(self._page, self, i) for i in range(cnt)]

    def locator(self, child_selector: str) -> "CLILocator":
        child = CLILocator(self._page, child_selector)
        child._parent = self
        return child


# ============================================================
# _IndexedLocator — single item from .all()
# ============================================================


class _IndexedLocator(CLILocator):
    """Targets a specific index within a parent locator's matches."""

    def __init__(self, page: CLIPage, parent: CLILocator, index: int):
        super().__init__(page, parent._selector or "")
        self._parent_locator = parent
        self._idx = index

    def _js_find_all(self) -> str:
        return f"[({self._parent_locator._js_find_all()})[{self._idx}]].filter(Boolean)"

    def locator(self, child_selector: str) -> CLILocator:
        parent_js = self._parent_locator._js_find_all()
        return _ScopedLocator(self._page, child_selector, parent_js, self._idx)

    def all_text_contents(self) -> List[str]:
        js = self._parent_locator._js_find_all()
        result = self._page.evaluate(f"""() => {{
            const el = ({js})[{self._idx}];
            return el ? [el.textContent || ''] : [];
        }}""")
        return result if isinstance(result, list) else []


class _ScopedLocator(CLILocator):
    """A locator scoped to a specific parent element by index."""

    def __init__(self, page: CLIPage, selector: str, parent_js: str, parent_index: int):
        super().__init__(page, selector)
        self._parent_js = parent_js
        self._parent_index = parent_index

    def _js_find_all(self) -> str:
        return (
            f"Array.from((({self._parent_js})[{self._parent_index}]"
            f" || document).querySelectorAll({json.dumps(self._selector)}))"
        )


# ============================================================
# CLIContext
# ============================================================


class CLIContext:
    """Minimal browser context wrapper for cookie and request operations."""

    def __init__(self, page: CLIPage):
        self._page = page

    def cookies(self, urls: List[str] = None) -> List[Dict[str, Any]]:
        result = self._page._run(["cookie", "list"], check=False)
        if result.returncode != 0:
            return []

        text = result.stdout.strip()
        cookies: List[Dict[str, Any]] = []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                cookies = parsed
            elif isinstance(parsed, dict):
                cookies = [parsed]
        except (json.JSONDecodeError, ValueError):
            pass

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
                c
                for c in cookies
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
                self._page._run(["cookie", "set", name, value], check=False)

    @property
    def pages(self) -> List[CLIPage]:
        """Only the current page — CLI sessions don't expose multiple tabs."""
        return [self._page]

    @property
    def request(self) -> "_CLIRequest":
        return _CLIRequest(self._page)


class _CLIRequest:
    """HTTP request interface using the browser's cookies via fetch()."""

    def __init__(self, page: CLIPage):
        self._page = page

    def get(self, url: str) -> "_CLIResponse":
        result = self._page.evaluate(f"""async () => {{
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
        return _CLIResponse(result)


class _CLIResponse:
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


# ============================================================
# Helpers
# ============================================================


def _is_playwright_selector(selector: str) -> bool:
    """True if the selector uses Playwright-specific syntax."""
    return selector.startswith("text=") or ":has-text(" in selector


def _split_selector(selector: str) -> List[str]:
    """Split a comma-separated selector while respecting quotes and parens."""
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
