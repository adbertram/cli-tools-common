"""JavaScript snippet constants and generators for browser element interaction."""

import json


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

_CLICK_JS = (
    "if (typeof el.click === 'function') el.click();"
    " else el.dispatchEvent(new MouseEvent('click', {bubbles: true}));"
)
