"""Unit tests for AuthVerifier service.

Validates that AuthVerifier correctly verifies authentication across all
credential types and produces the expected output fields.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_tools_common.auth_verifier import AuthVerifier
from cli_tools_common.browser_automation import AuthResult
from cli_tools_common.credentials import CredentialType


# ---------------------------------------------------------------------------
# Helpers: mock config factories
# ---------------------------------------------------------------------------

def _make_config(cred_types, has_creds=True, browser=None, access_token=None):
    """Create a mock config with the given credential types.

    Uses BaseConfig as spec so type(config).test_connection resolves correctly
    for the AuthVerifier._check_api() auto-detection logic.
    """
    from cli_tools_common.config import BaseConfig

    config = MagicMock(spec=BaseConfig)
    config.CREDENTIAL_TYPES = cred_types
    config.has_credentials.return_value = has_creds
    config.get_browser.return_value = browser
    config.access_token = access_token
    config.base_url = "https://example.com"
    config.get_active_profile_name.return_value = "default"

    return config


# ---------------------------------------------------------------------------
# auth_commands.py uses AuthVerifier (source-level check)
# ---------------------------------------------------------------------------

def test_auth_commands_uses_auth_verifier():
    """auth_commands.py must import and use AuthVerifier, with no legacy functions."""
    import importlib.util
    spec = importlib.util.find_spec("cli_tools_common.auth_commands")
    assert spec is not None, "cli_tools_common.auth_commands not found"
    source = Path(spec.origin).read_text()

    assert "AuthVerifier" in source, (
        "auth_commands.py does not use AuthVerifier. "
        "Fix: Import AuthVerifier from cli_tools_common.auth_verifier and use it in "
        "auth_status and auth_test commands."
    )
    assert "_check_browser_status" not in source, (
        "auth_commands.py still contains legacy _check_browser_status function. "
        "Fix: Remove it — browser checking is handled by AuthVerifier._check_browser()."
    )


# ---------------------------------------------------------------------------
# AuthVerifier.verify() output structure
# ---------------------------------------------------------------------------

class TestVerifyOutputFields:
    """Verify that AuthVerifier.verify() returns the expected fields per credential type."""

    def test_always_includes_authenticated_and_credentials_saved(self):
        config = _make_config([CredentialType.API_KEY], has_creds=True)
        handler = lambda cfg: {"api_test": "passed"}
        result = AuthVerifier(config, api_test_handler=handler).verify()
        assert "authenticated" in result
        assert "credentials_saved" in result

    def test_browser_session_includes_browser_session_field(self):
        """Browser CLIs must get a 'browser_session' field in verify() output."""
        browser = MagicMock()
        browser.is_authenticated.return_value = AuthResult(authenticated=True, live_check=True)
        browser.close.return_value = None

        config = _make_config(
            [CredentialType.BROWSER_SESSION],
            has_creds=True,
            browser=browser,
        )
        result = AuthVerifier(config).verify()

        assert "browser_session" in result, (
            "AuthVerifier.verify() missing 'browser_session' for BROWSER_SESSION credential type. "
            "Fix: Ensure _check_browser() is called when BROWSER_SESSION is in cred_types."
        )
        assert result["browser_session"] is True

    def test_browser_session_false_when_not_authenticated(self):
        browser = MagicMock()
        browser.is_authenticated.return_value = AuthResult(authenticated=False, live_check=True)
        browser.close.return_value = None

        config = _make_config(
            [CredentialType.BROWSER_SESSION],
            has_creds=True,
            browser=browser,
        )
        result = AuthVerifier(config).verify()
        assert result["browser_session"] is False
        assert result["authenticated"] is False

    def test_browser_session_omitted_when_no_browser(self):
        """Non-browser CLIs should NOT have browser_session in output."""
        config = _make_config([CredentialType.API_KEY], has_creds=True)
        handler = lambda cfg: {"api_test": "passed"}
        result = AuthVerifier(config, api_test_handler=handler).verify()
        assert "browser_session" not in result

    def test_oauth_includes_oauth_status(self):
        config = _make_config(
            [CredentialType.OAUTH],
            has_creds=True,
            access_token="fake-token",
        )
        with patch("cli_tools_common.token_manager.TokenManager") as MockTM:
            MockTM.return_value.is_expired.return_value = False
            result = AuthVerifier(config).verify()

        assert "oauth_status" in result
        assert result["oauth_status"] == "valid"

    def test_oauth_expired_sets_authenticated_false(self):
        config = _make_config(
            [CredentialType.OAUTH],
            has_creds=True,
            access_token="fake-token",
        )
        with patch("cli_tools_common.token_manager.TokenManager") as MockTM:
            MockTM.return_value.is_expired.return_value = True
            MockTM.return_value.force_refresh.side_effect = Exception("refresh failed")
            result = AuthVerifier(config).verify()

        assert result["oauth_status"] == "expired"
        assert result["authenticated"] is False

    def test_no_credentials_sets_authenticated_false(self):
        config = _make_config([CredentialType.API_KEY], has_creds=False)
        result = AuthVerifier(config).verify()
        assert result["credentials_saved"] is False
        assert result["authenticated"] is False

    def test_api_test_with_custom_handler(self):
        config = _make_config([CredentialType.API_KEY], has_creds=True)

        def handler(cfg):
            return {"api_test": "passed"}

        result = AuthVerifier(config, api_test_handler=handler).verify()
        assert result.get("api_test") == "passed"
        assert result["authenticated"] is True

    def test_api_test_failed_sets_authenticated_false(self):
        config = _make_config([CredentialType.API_KEY], has_creds=True)

        def handler(cfg):
            return {"api_test": "failed: unauthorized"}

        result = AuthVerifier(config, api_test_handler=handler).verify()
        assert "failed" in result["api_test"]
        assert result["authenticated"] is False


# ---------------------------------------------------------------------------
# AuthVerifier credential type constants
# ---------------------------------------------------------------------------

class TestCredentialTypeConstants:
    """Verify that AuthVerifier type constants cover all CredentialTypes."""

    def test_all_types_covered(self):
        """Every CredentialType (except NO_AUTH) must be in exactly one of the type sets."""
        all_covered = AuthVerifier.OAUTH_TYPES | AuthVerifier.BROWSER_TYPES | AuthVerifier.API_TYPES
        for ct in CredentialType:
            if ct == CredentialType.NO_AUTH:
                continue
            assert ct in all_covered, (
                f"CredentialType.{ct.name} not covered by any AuthVerifier type constant"
            )

    def test_no_overlap(self):
        """Type sets must be mutually exclusive."""
        assert not (AuthVerifier.OAUTH_TYPES & AuthVerifier.BROWSER_TYPES)
        assert not (AuthVerifier.OAUTH_TYPES & AuthVerifier.API_TYPES)
        assert not (AuthVerifier.BROWSER_TYPES & AuthVerifier.API_TYPES)
