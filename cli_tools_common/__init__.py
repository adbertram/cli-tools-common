"""Shared utilities for CLI tools: auth, profiles, config, output, OAuth, browser."""

from .config import BaseConfig


def __getattr__(name):
    """Lazy-load browser modules."""
    if name in ("BrowserAutomation", "BrowserAutomationError"):
        from .browser_automation import BrowserAutomation, BrowserAutomationError
        _browser_exports = {
            "BrowserAutomation": BrowserAutomation,
            "BrowserAutomationError": BrowserAutomationError,
        }
        return _browser_exports[name]
    if name == "CLIPage":
        from .cli_page import CLIPage
        return CLIPage
    if name == "PlaywrightCLIBrowser":
        from .playwright_browser import PlaywrightCLIBrowser
        return PlaywrightCLIBrowser
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
from .filters import (
    FilterValidationError,
    apply_filters,
    apply_properties_filter,
    apply_limit,
    get_nested_value,
    validate_filters,
    parse_filter_string,
)
from .filter_map import FilterMap
from .bulk import BulkProcessor
from .models import CLIModel
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
from .activity_log import get_activity_logger
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
    # Filters
    "FilterValidationError",
    "apply_filters",
    "apply_properties_filter",
    "apply_limit",
    "get_nested_value",
    "validate_filters",
    "parse_filter_string",
    # Filter Map
    "FilterMap",
    # Bulk
    "BulkProcessor",
    # Models
    "CLIModel",
    # Config
    "BaseConfig",
    "BrowserAutomation",
    "BrowserAutomationError",
    "CLIPage",
    "PlaywrightCLIBrowser",
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
    # Activity Logging
    "get_activity_logger",
    "print_json",
    "print_table",
    "print_output",
    "print_error",
    "print_warning",
    "print_success",
    "print_info",
    "handle_error",
]
