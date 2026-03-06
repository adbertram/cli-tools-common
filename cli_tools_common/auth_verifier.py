"""Authentication verification service for all credential types."""

import logging
from typing import Optional

from .credentials import CredentialType

logger = logging.getLogger("cli_tools.auth_verifier")


class AuthVerifier:
    """Verifies authentication across all configured credential types.

    Performs live checks per type:
      API_KEY / PAT / USERNAME_PASSWORD / CUSTOM -> config.test_connection()
      OAUTH / OAUTH_AUTHORIZATION_CODE -> TokenManager expiry check + refresh
      BROWSER_SESSION -> headless browser live check
    """

    OAUTH_TYPES = frozenset({
        CredentialType.OAUTH,
        CredentialType.OAUTH_AUTHORIZATION_CODE,
    })
    BROWSER_TYPES = frozenset({
        CredentialType.BROWSER_SESSION,
    })
    # Types verified via config.test_connection() — everything not OAuth or browser
    API_TYPES = frozenset({
        CredentialType.API_KEY,
        CredentialType.PERSONAL_ACCESS_TOKEN,
        CredentialType.USERNAME_PASSWORD,
        CredentialType.CUSTOM,
    })

    def __init__(self, config, api_test_handler=None):
        self.config = config
        self._api_test_handler = api_test_handler

    def verify(self) -> dict:
        """Run all authentication checks.

        Returns dict with:
          authenticated     - bool: can use this tool right now?
          credentials_saved - bool: are static credentials in config?
          browser_session   - Optional[bool]: browser session live? (omitted if no browser)
          oauth_status      - Optional[str]: "valid"|"refreshed"|"expired"|"no_token"
          api_test          - Optional[str]: "passed"|"failed: ..."|"skipped"

        For dual-auth tools (e.g. OAUTH + BROWSER_SESSION), uses OR logic:
        authenticated is True if any credential pathway is live-verified.
        """
        cred_types = self.config._resolved_credential_types
        result = {"credentials_saved": self.config.has_credentials()}

        cred_set = frozenset(cred_types)
        has_oauth = bool(cred_set & self.OAUTH_TYPES)
        has_browser = bool(cred_set & self.BROWSER_TYPES)
        has_api = bool(cred_set & self.API_TYPES)

        # Track per-category verification results for OR logic
        # None = not applicable, True = passed, False = failed
        non_browser_ok = None
        browser_ok_result = None

        # 1. OAuth token check (fast - no API call, just expiry + refresh attempt)
        if has_oauth:
            oauth_status = self._check_oauth()
            result["oauth_status"] = oauth_status
            non_browser_ok = oauth_status in ("valid", "refreshed")

        # 2. API connectivity test
        if has_api and result["credentials_saved"]:
            api_status = self._check_api()
            if api_status is not None:
                result["api_test"] = api_status
                api_passed = api_status == "passed"
                if non_browser_ok is None:
                    non_browser_ok = api_passed
                else:
                    non_browser_ok = non_browser_ok and api_passed

        # 3. Browser session live check
        if has_browser:
            browser_live = self._check_browser()
            if browser_live is not None:
                result["browser_session"] = browser_live
                browser_ok_result = browser_live

        # Determine authenticated: dual-auth uses OR, single-type uses AND
        is_dual_auth = has_browser and (has_oauth or has_api)

        if is_dual_auth:
            # OR: at least one credential pathway must be live
            any_ok = False
            if non_browser_ok is True:
                any_ok = True
            if browser_ok_result is True:
                any_ok = True
            result["authenticated"] = any_ok
        else:
            # Single-type: all checks must pass (original AND behavior)
            all_ok = result["credentials_saved"]
            if non_browser_ok is not None and not non_browser_ok:
                all_ok = False
            if browser_ok_result is not None and not browser_ok_result:
                all_ok = False
            result["authenticated"] = all_ok

        return result

    def _check_oauth(self) -> str:
        """Check OAuth token expiry, attempt refresh if needed."""
        if not self.config.access_token:
            return "no_token"
        from .token_manager import TokenManager
        tm = TokenManager(self.config)
        if not tm.is_expired():
            return "valid"
        try:
            tm.force_refresh()
            return "refreshed"
        except Exception:
            return "expired"

    def _check_api(self) -> Optional[str]:
        """Test API connectivity via handler or config.test_connection()."""
        # Custom handler takes priority (used by auth_test)
        if self._api_test_handler:
            try:
                api_result = self._api_test_handler(self.config)
                return api_result.get("api_test", "skipped")
            except Exception as e:
                return f"failed: {e}"

        # Auto-detect test_connection override
        from .config import BaseConfig
        if type(self.config).test_connection is BaseConfig.test_connection:
            return None  # No test implemented - can't verify
        try:
            r = self.config.test_connection()
            if r is None:
                return None
            return r.get("api_test", "skipped")
        except Exception as e:
            return f"failed: {e}"

    def _check_browser(self) -> Optional[bool]:
        """Check browser session via live headless check."""
        browser = self.config.get_browser()
        if browser is None:
            return None
        try:
            result = browser.is_authenticated()
            return bool(result)
        except Exception:
            return False
        finally:
            try:
                browser.close()
            except Exception:
                pass
