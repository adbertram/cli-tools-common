"""Shared utilities for CLI tools: auth, profiles, config, output, OAuth, browser."""

from .config import BaseConfig


def __getattr__(name):
    """Lazy-load browser automation to avoid requiring playwright for API-only CLIs."""
    if name in ("BrowserAutomation", "BrowserAutomationError", "SessionData"):
        from .browser_automation import BrowserAutomation, BrowserAutomationError, SessionData
        _browser_exports = {
            "BrowserAutomation": BrowserAutomation,
            "BrowserAutomationError": BrowserAutomationError,
            "SessionData": SessionData,
        }
        return _browser_exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
from .credentials import (
    CredentialType,
    mask_value,
    combined_required_fields,
    combined_all_fields,
    combined_login_prompts,
    combined_sensitive_fields,
)
from .exceptions import ClientError, ConfigError
from .auth_commands import create_auth_app
from .profiles_commands import create_profiles_app
from .oauth import oauth_login, extract_code_from_input, generate_pkce_pair
from .token_manager import TokenManager
from .output import (
    print_json,
    print_table,
    print_output,
    print_error,
    print_warning,
    print_success,
    print_info,
    handle_error,
)

__all__ = [
    "BaseConfig",
    "BrowserAutomation",
    "BrowserAutomationError",
    "SessionData",
    "CredentialType",
    "mask_value",
    "combined_required_fields",
    "combined_all_fields",
    "combined_login_prompts",
    "combined_sensitive_fields",
    "ClientError",
    "ConfigError",
    "create_auth_app",
    "create_profiles_app",
    "oauth_login",
    "extract_code_from_input",
    "generate_pkce_pair",
    "TokenManager",
    "print_json",
    "print_table",
    "print_output",
    "print_error",
    "print_warning",
    "print_success",
    "print_info",
    "handle_error",
]
