"""Element and Locator classes for browser automation."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, List, Optional

from . import PlaywrightServiceError
from ._js_fragments import (
    _CHECK_JS,
    _CLICK_JS,
    _UNCHECK_JS,
    _VISIBILITY_JS,
    _fill_js,
    _select_by_label_js,
    _select_by_value_js,
)

if TYPE_CHECKING:
    from .service import PlaywrightService


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
        self._eval_on_el(f"if (el) {{ {_CLICK_JS} }}")

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


class _ServiceLocator:
    """Lazy element locator supporting CSS, text=, :has-text(), and role selectors."""

    def __init__(self, svc: PlaywrightService, selector: str):
        self._svc = svc
        self._selector = selector
        self._parent: Optional[_ServiceLocator] = None

    @classmethod
    def from_role(cls, svc: PlaywrightService, role: str, name=None) -> _ServiceLocator:
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
        self._eval_on_first(_CLICK_JS, require=True)

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
        result = self._svc.evaluate(f"() => ({self._js_find_all()}).length")
        if not result:
            return 0
        try:
            return int(result)
        except (ValueError, TypeError):
            raise PlaywrightServiceError(f"count() expected integer, got: {str(result)[:200]}")

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
        result = self._svc.evaluate(f"() => ({self._js_find_all()}).map(el => el.textContent || '')")
        return result if isinstance(result, list) else []

    def all(self) -> List[_ServiceLocator]:
        cnt = self.count()
        return [_IndexedLocator(self._svc, self, i) for i in range(cnt)]

    def locator(self, child_selector: str) -> _ServiceLocator:
        child = _ServiceLocator(self._svc, child_selector)
        child._parent = self
        return child

    def filter(self, *, has_text=None) -> _ServiceLocator:
        """Return a filtered locator that matches elements containing the given text."""
        if has_text is None:
            return self
        return _FilteredLocator(self._svc, self, has_text)

    def get_by_placeholder(self, text: str) -> _ServiceLocator:
        return self.locator(f'[placeholder="{text}"]')

    def get_by_role(self, role: str, *, name=None) -> _ServiceLocator:
        """Get elements by role, scoped to this locator's matches."""
        role_loc = _ServiceLocator.from_role(self._svc, role, name)
        role_loc._parent = self
        return role_loc


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
