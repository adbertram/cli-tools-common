"""Context, Request, and Response wrappers for browser automation."""

from __future__ import annotations

import base64
import json
import re
from typing import TYPE_CHECKING, Any, Dict, List

from ._parsers import _parse_cookie_list

if TYPE_CHECKING:
    from .service import PlaywrightService


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
    def request(self) -> _ServiceRequest:
        return _ServiceRequest(self._svc)


class _ServiceRequest:
    """HTTP request interface using the browser's cookies via fetch()."""

    def __init__(self, svc: PlaywrightService):
        self._svc = svc

    def get(self, url: str) -> _ServiceResponse:
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
