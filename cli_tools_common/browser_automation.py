"""Shared browser automation module for CLI tools.

Provides a base class that handles:
- Interactive login via CDP (Chrome DevTools Protocol)
- Session capture and restore (cookies + localStorage + sessionStorage + IndexedDB)
- Headless automation with anti-detection

CLI tools subclass BrowserAutomation and set class-level hooks for site-specific behavior.

Reference implementations:
- CDP approach: PayPal CLI auth_service.py
- State capture: BrickFreedom CLI auth.py (capture_state/restore_state)
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page, BrowserContext


def _ensure_playwright():
    """Import playwright at runtime, raising a clear error if not installed."""
    try:
        # Suppress Node.js url.parse() deprecation warning from Playwright internals
        os.environ.setdefault("NODE_OPTIONS", "--no-deprecation")
        from playwright.sync_api import sync_playwright, Page, BrowserContext
        return sync_playwright, Page, BrowserContext
    except ImportError:
        raise ImportError(
            "playwright is required for browser automation. "
            "Install it with: pip install playwright && playwright install chromium"
        )


class BrowserAutomationError(Exception):
    """Browser automation error."""

    def __init__(self, message: str, cause: Exception = None):
        self.message = message
        self.cause = cause
        super().__init__(message)


@dataclass
class SessionData:
    """Captured session state for persistence.

    Stores everything needed to restore a browser session:
    cookies, localStorage, sessionStorage, and IndexedDB.
    """

    cookies: List[Dict[str, Any]]
    local_storage: Dict[str, str]
    session_storage: Dict[str, str]
    indexed_db: Dict[str, Any]
    url: str
    profile: str
    created_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "cookies": self.cookies,
            "local_storage": self.local_storage,
            "session_storage": self.session_storage,
            "indexed_db": self.indexed_db,
            "url": self.url,
            "profile": self.profile,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionData":
        """Create from dict (loaded from JSON)."""
        return cls(
            cookies=data.get("cookies", []),
            local_storage=data.get("local_storage", {}),
            session_storage=data.get("session_storage", {}),
            indexed_db=data.get("indexed_db", {}),
            url=data.get("url", ""),
            profile=data.get("profile", "default"),
            created_at=(
                datetime.fromisoformat(data["created_at"])
                if "created_at" in data
                else datetime.now()
            ),
        )


class BrowserAutomation:
    """Base class for browser automation in CLI tools.

    Subclasses set class-level hooks for site-specific behavior::

        class MyBrowser(BrowserAutomation):
            LOGIN_URL = "https://example.com/login"
            AUTH_CHECK_URL = "https://example.com/dashboard"
            AUTH_URL_PATTERN = r"/login"
            AUTH_COOKIE_PATTERNS = ["session.*", "auth"]
            AUTH_SUCCESS_URL = r"/dashboard"         # URL-based detection
            AUTH_SUCCESS_SELECTOR = "text=Hello"     # DOM-based detection (takes priority)

    Two operating modes:

    1. **Interactive login** (``authenticate()``):
       Launch real Chrome via CDP → user logs in manually → capture full session state.

    2. **Headless automation** (``get_page()``):
       Playwright headless with ``channel="chrome"`` + anti-detection args.
       Restores cookies, localStorage, sessionStorage, and IndexedDB from saved session.
    """

    # --- Class-level hooks (subclasses override) ---
    LOGIN_URL = ""                    # e.g. "https://www.brickowl.com/login"
    AUTH_CHECK_URL = ""               # e.g. "https://www.brickowl.com/mystore/orders"
    AUTH_URL_PATTERN = ""             # Regex — if URL matches, user is on login page
    AUTH_COOKIE_PATTERNS = []         # Cookie name regexes indicating auth
    AUTH_SUCCESS_URL = ""             # URL pattern indicating successful login
    AUTH_SUCCESS_SELECTOR = ""        # CSS/text selector visible when authenticated
    LOGIN_TIMEOUT = 300               # Seconds to wait for manual login
    LOGIN_POLL_INTERVAL = 2           # Seconds between auth checks
    CDP_PORT = 9222                   # Chrome debugging port
    CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    CHROME_USER_DATA_DIR = "/tmp/chrome-debug"

    def __init__(self, config):
        """Initialize browser automation.

        Args:
            config: Config instance. Must provide either ``get_browser_data_dir()``
                    (BaseConfig) or ``browser_data_dir`` property for session storage.
        """
        self.config = config
        self._playwright = None
        self._browser = None
        self._context: Optional["BrowserContext"] = None
        self._page: Optional["Page"] = None
        self._chrome_process = None
        self._connected_to_existing = False

    # --- Config accessors (duck-typing for different config classes) ---

    def _get_browser_data_dir(self) -> Path:
        """Get browser data directory from config."""
        if hasattr(self.config, "get_browser_data_dir"):
            return self.config.get_browser_data_dir()
        if hasattr(self.config, "browser_data_dir"):
            d = self.config.browser_data_dir
            return d if isinstance(d, Path) else Path(d)
        raise BrowserAutomationError(
            "Config must provide get_browser_data_dir() or browser_data_dir"
        )

    def _get_profile_name(self) -> str:
        """Get active profile name from config."""
        if hasattr(self.config, "get_active_profile_name"):
            return self.config.get_active_profile_name()
        if hasattr(self.config, "profile") and self.config.profile:
            return self.config.profile
        return "default"

    @property
    def session_path(self) -> Path:
        """Path to session.json file."""
        return self._get_browser_data_dir() / "session.json"

    # ==================== Public Interface ====================

    def is_authenticated(self) -> bool:
        """Check if the browser session is authenticated.

        Uses the configured detection strategy:
        - ``AUTH_COOKIE_PATTERNS`` → fast cookie check (no browser launch)
        - ``AUTH_SUCCESS_SELECTOR`` or ``AUTH_SUCCESS_URL`` → headless browser check
        - No config → session file existence
        """
        if not self.has_session():
            return False

        try:
            session = self.load_session()
        except BrowserAutomationError:
            return False

        # Cookie-based: fast path, no browser needed
        if self.AUTH_COOKIE_PATTERNS:
            return len(self._get_auth_cookies(session.cookies)) > 0

        # DOM/URL-based: requires headless browser
        if self.AUTH_SUCCESS_SELECTOR or self.AUTH_SUCCESS_URL:
            try:
                self._init_headless_browser()
                if session.cookies:
                    self._context.add_cookies(session.cookies)
                self._page.goto(self.AUTH_CHECK_URL, wait_until="domcontentloaded")
                self._page.wait_for_timeout(2000)
                self._restore_storage(self._page, session)
                return self._is_authenticated_page(self._page)
            except Exception:
                return False
            finally:
                self.close()

        # No detection configured — trust session existence
        return True

    def authenticate(self, force: bool = False) -> SessionData:
        """Interactive login via Chrome DevTools Protocol.

        Connects to (or launches) real Chrome with remote debugging.
        User logs in manually, solving any captchas. Session is captured
        after successful login detection.

        Args:
            force: Re-authenticate even if session exists.

        Returns:
            SessionData with captured state.
        """
        if self.has_session() and not force:
            return self.load_session()

        if force and self.session_path.exists():
            self.session_path.unlink()

        # Connect to Chrome via CDP
        cdp_url = f"http://localhost:{self.CDP_PORT}"
        sync_playwright, _, _ = _ensure_playwright()
        self._playwright = sync_playwright().start()
        self._chrome_process = None
        self._connected_to_existing = False

        try:
            self._browser = self._playwright.chromium.connect_over_cdp(cdp_url)
            self._connected_to_existing = True
        except Exception:
            self._chrome_process = self._launch_chrome()
            self._connect_cdp(cdp_url)

        # Get CDP context
        if not self._browser.contexts:
            raise BrowserAutomationError("No browser context available via CDP")

        self._context = self._browser.contexts[0]

        # Find existing page or create new one
        self._page = None
        if self._context.pages:
            self._page = self._context.pages[0]
        if not self._page:
            self._page = self._context.new_page()

        # Always navigate to login page (even if Chrome was open to another site)
        try:
            self._page.goto(
                self.LOGIN_URL, wait_until="domcontentloaded", timeout=30000
            )
        except Exception:
            print(
                "Page load slow, but browser should be open...", file=sys.stderr
            )

        return self._wait_for_login_and_capture()

    def get_page(self, url: str = None) -> "Page":
        """Get headless Playwright page with full session restored.

        On first call, launches headless browser, restores cookies, navigates
        to ``url`` (or ``AUTH_CHECK_URL``), then restores localStorage,
        sessionStorage, and IndexedDB.

        Subsequent calls return the existing page. If ``url`` is provided,
        navigates to it.

        Args:
            url: URL to navigate to (defaults to AUTH_CHECK_URL).

        Returns:
            Playwright Page ready for use.
        """
        if self._page is not None:
            if url:
                self._page.goto(url, wait_until="domcontentloaded")
            return self._page

        if not self.has_session():
            raise BrowserAutomationError(
                "No session exists. Call authenticate() first."
            )

        session = self.load_session()
        target_url = url or self.AUTH_CHECK_URL

        # Launch headless browser
        self._init_headless_browser()

        # 1. Add cookies to context (origin-agnostic)
        if session.cookies:
            self._context.add_cookies(session.cookies)

        # 2. Navigate to target URL (establishes origin)
        self._page.goto(target_url, wait_until="domcontentloaded")

        # 3. Inject localStorage, sessionStorage, IndexedDB (origin-specific)
        self._restore_storage(self._page, session)

        return self._page

    def save_session(self) -> SessionData:
        """Capture full state from live browser context and save to disk.

        Captures cookies, localStorage, sessionStorage, and IndexedDB.
        """
        if not self._context or not self._page:
            raise BrowserAutomationError("No active browser session to save")

        session = SessionData(
            cookies=self._context.cookies(),
            local_storage=self._get_storage(self._page, "localStorage"),
            session_storage=self._get_storage(self._page, "sessionStorage"),
            indexed_db=self._get_indexeddb(self._page),
            url=self._page.url,
            profile=self._get_profile_name(),
        )

        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.session_path, "w") as f:
            json.dump(session.to_dict(), f, indent=2)

        return session

    def load_session(self) -> SessionData:
        """Load session from JSON file.

        Raises:
            BrowserAutomationError: If no session exists or file is corrupt.
        """
        if not self.has_session():
            raise BrowserAutomationError("No session file exists")

        try:
            with open(self.session_path) as f:
                data = json.load(f)
            return SessionData.from_dict(data)
        except (json.JSONDecodeError, KeyError) as e:
            raise BrowserAutomationError(
                f"Corrupt session file: {e}", cause=e
            )

    def restore_session(self, page: "Page") -> None:
        """Restore full state to a page (must be navigated to target origin first)."""
        if not self.has_session():
            raise BrowserAutomationError("No session to restore")

        session = self.load_session()

        if session.cookies and self._context:
            self._context.add_cookies(session.cookies)

        self._restore_storage(page, session)

    def has_session(self) -> bool:
        """Fast file check for session existence."""
        return self.session_path.exists()

    def clear_session(self) -> None:
        """Delete session.json."""
        if self.session_path.exists():
            self.session_path.unlink()

    def login(self, force: bool = False) -> Dict[str, Any]:
        """Interactive login compatible with create_auth_app interface.

        Wraps authenticate() to return the dict format expected by
        cli_tools_common.auth_commands._handle_browser_login().

        Returns:
            Dict with 'success' key (True/False) and 'message'.
        """
        try:
            self.authenticate(force=force)
            return {"success": True, "message": "Session saved. Browser closed."}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def close(self) -> None:
        """Cleanup browser and Chrome process."""
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
        if self._chrome_process:
            print("Closing browser window...", file=sys.stderr)
            self._chrome_process.terminate()
            try:
                self._chrome_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._chrome_process.kill()
                self._chrome_process.wait()
            self._chrome_process = None
        elif self._connected_to_existing:
            self._kill_chrome_debug_process()
            self._connected_to_existing = False

        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    def test_session(self) -> Dict[str, Any]:
        """Headless browser verification of auth.

        Launches headless browser, restores session, navigates to AUTH_CHECK_URL,
        and checks if we stay authenticated (not redirected to login).

        Returns:
            Dict with authenticated, url, cookies, profile, created_at.
        """
        if not self.has_session():
            return {"authenticated": False, "error": "No session file exists"}

        session = self.load_session()

        self._init_headless_browser()
        if session.cookies:
            self._context.add_cookies(session.cookies)

        self._page.goto(self.AUTH_CHECK_URL, wait_until="domcontentloaded")
        self._page.wait_for_timeout(3000)

        # Restore full state and reload
        self._restore_storage(self._page, session)
        self._page.reload(wait_until="domcontentloaded")
        self._page.wait_for_timeout(2000)

        authenticated = self._is_authenticated_page(self._page)

        result = {
            "authenticated": authenticated,
            "url": self._page.url,
            "cookies": len(session.cookies),
            "profile": session.profile,
            "created_at": session.created_at.isoformat(),
        }

        self.close()
        return result

    # ==================== Overridable Hooks (Template Method) ====================

    def _is_login_page(self, page: "Page") -> bool:
        """Check if page is on the login page.

        Default: regex ``AUTH_URL_PATTERN`` against ``page.url``.
        Override for custom logic.
        """
        if self.AUTH_URL_PATTERN:
            return bool(re.search(self.AUTH_URL_PATTERN, page.url))
        return False

    def _is_authenticated_page(self, page: "Page") -> bool:
        """Check if page shows authenticated state.

        Checks in order:
        1. ``AUTH_SUCCESS_SELECTOR`` — CSS/text selector visible on page
        2. ``AUTH_SUCCESS_URL`` — regex match against page URL
        3. Fallback — not on the login page
        """
        if self.AUTH_SUCCESS_SELECTOR:
            try:
                return page.locator(self.AUTH_SUCCESS_SELECTOR).first.is_visible(timeout=500)
            except Exception:
                return False
        if self.AUTH_SUCCESS_URL:
            return bool(re.search(self.AUTH_SUCCESS_URL, page.url))
        return not self._is_login_page(page)

    def _on_authenticated(self, page: "Page") -> None:
        """Called after successful authentication. Override to capture extra state."""
        pass

    def _get_auth_cookies(
        self, cookies: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Filter cookies by AUTH_COOKIE_PATTERNS with expiration check.

        Uses ``re.search()`` (not ``re.match()``) for partial name matching.
        """
        if not self.AUTH_COOKIE_PATTERNS:
            return cookies

        now = time.time()
        auth_cookies = []

        for cookie in cookies:
            name = cookie.get("name", "")
            expires = cookie.get("expires", -1)

            # Skip expired cookies (-1 = session cookie, 0 = expired)
            if expires > 0 and expires < now:
                continue

            for pattern in self.AUTH_COOKIE_PATTERNS:
                if re.search(pattern, name, re.IGNORECASE):
                    auth_cookies.append(cookie)
                    break

        return auth_cookies

    # ==================== Internal Methods ====================

    def _launch_chrome(self) -> subprocess.Popen:
        """Launch Chrome with remote debugging port."""
        process = subprocess.Popen(
            [
                self.CHROME_PATH,
                f"--remote-debugging-port={self.CDP_PORT}",
                f"--user-data-dir={self.CHROME_USER_DATA_DIR}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("Launching Chrome with remote debugging...", file=sys.stderr)
        time.sleep(2)
        return process

    def _connect_cdp(self, cdp_url: str) -> None:
        """Connect to Chrome via CDP with 5-retry backoff."""
        max_retries = 5
        for attempt in range(max_retries):
            try:
                self._browser = self._playwright.chromium.connect_over_cdp(cdp_url)
                return
            except Exception as e:
                if attempt == max_retries - 1:
                    self._playwright.stop()
                    self._playwright = None
                    if self._chrome_process:
                        self._chrome_process.terminate()
                        self._chrome_process = None
                    raise BrowserAutomationError(
                        f"Cannot connect to Chrome at {cdp_url} after {max_retries} attempts. "
                        "Try launching Chrome manually with:\n\n"
                        f'  "{self.CHROME_PATH}" '
                        f"--remote-debugging-port={self.CDP_PORT} "
                        f'--user-data-dir="{self.CHROME_USER_DATA_DIR}"',
                        cause=e,
                    )
                time.sleep(1)

    def _find_chrome_debug_pid(self) -> Optional[int]:
        """Find PID of Chrome process listening on CDP_PORT via lsof."""
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{self.CDP_PORT}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return int(result.stdout.strip().split()[0])
        except (subprocess.TimeoutExpired, ValueError, IndexError):
            pass
        return None

    def _kill_chrome_debug_process(self) -> None:
        """Kill Chrome process on CDP_PORT: SIGTERM, wait 1s, SIGKILL."""
        pid = self._find_chrome_debug_pid()
        if pid:
            print("Closing browser window...", file=sys.stderr)
            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(1)
                try:
                    os.kill(pid, 0)  # Check if still running
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass

    def _init_headless_browser(self) -> None:
        """Launch headless Playwright browser with anti-detection args."""
        sync_playwright, _, _ = _ensure_playwright()
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=True,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )

        self._context = self._browser.new_context()
        self._page = self._context.new_page()

    def _wait_for_login_and_capture(self) -> SessionData:
        """Wait for user to complete login, then capture full session state."""
        print(f"\n{'=' * 60}", file=sys.stderr)
        print(f"Please log in at: {self._page.url}", file=sys.stderr)
        print("Solve any captchas that appear.", file=sys.stderr)
        print(
            "The browser will close automatically once logged in.",
            file=sys.stderr,
        )
        print(f"{'=' * 60}\n", file=sys.stderr)

        waited = 0
        while waited < self.LOGIN_TIMEOUT:
            if self._is_authenticated_page(self._page):
                print(f"\nLogin detected: {self._page.url}", file=sys.stderr)
                break

            self._page.wait_for_timeout(self.LOGIN_POLL_INTERVAL * 1000)
            waited += self.LOGIN_POLL_INTERVAL

        if waited >= self.LOGIN_TIMEOUT:
            raise BrowserAutomationError(
                f"Login timeout - did not detect successful login "
                f"within {self.LOGIN_TIMEOUT}s"
            )

        # Give page a moment to fully load
        self._page.wait_for_timeout(2000)

        # Call hook for extra state capture
        self._on_authenticated(self._page)

        # Capture full session state
        session = SessionData(
            cookies=self._context.cookies(),
            local_storage=self._get_storage(self._page, "localStorage"),
            session_storage=self._get_storage(self._page, "sessionStorage"),
            indexed_db=self._get_indexeddb(self._page),
            url=self._page.url,
            profile=self._get_profile_name(),
        )

        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.session_path, "w") as f:
            json.dump(session.to_dict(), f, indent=2)

        cookie_count = len(session.cookies)
        print(f"Captured {cookie_count} cookies", file=sys.stderr)
        print(f"Session saved to: {self.session_path}", file=sys.stderr)

        # Close browser — future commands run headlessly
        self.close()
        print(
            "Authentication complete. Future commands will run in headless mode.",
            file=sys.stderr,
        )

        return session

    # ==================== State Capture & Restore ====================
    # Ported from BrickFreedom CLI auth.py (battle-tested across 6+ CLIs)

    @staticmethod
    def _get_storage(page: "Page", storage_type: str) -> Dict[str, str]:
        """Get localStorage or sessionStorage contents."""
        return page.evaluate(
            f"""() => {{
            const storage = {storage_type};
            const data = {{}};
            for (let i = 0; i < storage.length; i++) {{
                const key = storage.key(i);
                data[key] = storage.getItem(key);
            }}
            return data;
        }}"""
        )

    @staticmethod
    def _get_indexeddb(page: "Page") -> Dict[str, Any]:
        """Capture all IndexedDB databases and their contents.

        Binary data (ArrayBuffer) is converted to base64.
        """
        return page.evaluate(
            """async () => {
            const result = {};
            if (!indexedDB.databases) return result;

            const databases = await indexedDB.databases();

            for (const dbInfo of databases) {
                const dbName = dbInfo.name;
                const dbData = { version: dbInfo.version, stores: {} };

                try {
                    const db = await new Promise((resolve, reject) => {
                        const request = indexedDB.open(dbName);
                        request.onerror = () => reject(request.error);
                        request.onsuccess = () => resolve(request.result);
                    });

                    const storeNames = Array.from(db.objectStoreNames);

                    for (const storeName of storeNames) {
                        const records = [];
                        const tx = db.transaction(storeName, 'readonly');
                        const store = tx.objectStore(storeName);

                        await new Promise((resolve, reject) => {
                            const cursor = store.openCursor();
                            cursor.onerror = () => reject(cursor.error);
                            cursor.onsuccess = (event) => {
                                const cur = event.target.result;
                                if (cur) {
                                    let value = cur.value;
                                    if (value instanceof ArrayBuffer) {
                                        value = {
                                            __type: 'ArrayBuffer',
                                            data: btoa(String.fromCharCode(
                                                ...new Uint8Array(value)
                                            ))
                                        };
                                    } else if (value instanceof Blob) {
                                        value = {
                                            __type: 'Blob',
                                            size: value.size,
                                            type: value.type
                                        };
                                    }
                                    records.push({ key: cur.key, value: value });
                                    cur.continue();
                                } else {
                                    resolve();
                                }
                            };
                        });

                        dbData.stores[storeName] = {
                            keyPath: store.keyPath,
                            autoIncrement: store.autoIncrement,
                            indexNames: Array.from(store.indexNames),
                            records: records
                        };
                    }

                    db.close();
                    result[dbName] = dbData;
                } catch (e) {
                    console.warn(`Could not capture IndexedDB ${dbName}:`, e);
                }
            }

            return result;
        }"""
        )

    @staticmethod
    def _restore_indexeddb(page: "Page", data: Dict[str, Any]) -> None:
        """Restore IndexedDB data from captured state.

        Recreates databases, object stores, and records.
        Existing databases with the same name are deleted first.
        """
        if not data:
            return

        page.evaluate(
            """async (dbData) => {
            for (const [dbName, dbInfo] of Object.entries(dbData)) {
                // Delete existing database first
                await new Promise((resolve, reject) => {
                    const deleteReq = indexedDB.deleteDatabase(dbName);
                    deleteReq.onerror = () => reject(deleteReq.error);
                    deleteReq.onsuccess = () => resolve();
                    deleteReq.onblocked = () => resolve();
                });

                // Create database with correct version
                const db = await new Promise((resolve, reject) => {
                    const request = indexedDB.open(dbName, dbInfo.version);
                    request.onerror = () => reject(request.error);

                    request.onupgradeneeded = (event) => {
                        const db = event.target.result;
                        for (const [storeName, storeInfo] of
                                Object.entries(dbInfo.stores)) {
                            const storeOptions = {};
                            if (storeInfo.keyPath !== null) {
                                storeOptions.keyPath = storeInfo.keyPath;
                            }
                            if (storeInfo.autoIncrement) {
                                storeOptions.autoIncrement = true;
                            }
                            db.createObjectStore(storeName, storeOptions);
                        }
                    };

                    request.onsuccess = () => resolve(request.result);
                });

                // Populate stores with data
                for (const [storeName, storeInfo] of
                        Object.entries(dbInfo.stores)) {
                    if (storeInfo.records.length === 0) continue;

                    const tx = db.transaction(storeName, 'readwrite');
                    const store = tx.objectStore(storeName);

                    for (const record of storeInfo.records) {
                        let value = record.value;

                        // Restore ArrayBuffer from base64
                        if (value && value.__type === 'ArrayBuffer') {
                            const binary = atob(value.data);
                            const bytes = new Uint8Array(binary.length);
                            for (let i = 0; i < binary.length; i++) {
                                bytes[i] = binary.charCodeAt(i);
                            }
                            value = bytes.buffer;
                        }

                        if (storeInfo.keyPath === null) {
                            store.put(value, record.key);
                        } else {
                            store.put(value);
                        }
                    }

                    await new Promise((resolve, reject) => {
                        tx.oncomplete = () => resolve();
                        tx.onerror = () => reject(tx.error);
                    });
                }

                db.close();
            }
        }""",
            data,
        )

    def _restore_storage(self, page: "Page", session: SessionData) -> None:
        """Restore localStorage, sessionStorage, and IndexedDB to a page.

        Page must already be navigated to the target origin (these are
        origin-specific APIs).
        """
        if session.local_storage:
            page.evaluate(
                """(data) => {
                for (const [key, value] of Object.entries(data)) {
                    localStorage.setItem(key, value);
                }
            }""",
                session.local_storage,
            )

        if session.session_storage:
            page.evaluate(
                """(data) => {
                for (const [key, value] of Object.entries(data)) {
                    sessionStorage.setItem(key, value);
                }
            }""",
                session.session_storage,
            )

        if session.indexed_db:
            self._restore_indexeddb(page, session.indexed_db)

    # ==================== Context Manager ====================

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False
