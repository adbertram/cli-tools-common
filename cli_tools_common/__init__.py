"""Shared utilities for CLI tools: auth, profiles, config, output, OAuth, browser."""

from .config import BaseConfig


def __getattr__(name):
    """Lazy-load browser modules."""
    if name in ("BrowserAutomation", "BrowserAutomationError", "AuthResult"):
        from .browser_automation import BrowserAutomation, BrowserAutomationError, AuthResult
        _browser_exports = {
            "BrowserAutomation": BrowserAutomation,
            "BrowserAutomationError": BrowserAutomationError,
            "AuthResult": AuthResult,
        }
        return _browser_exports[name]
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
from .command_registry import register_commands
from .oauth import oauth_login, extract_code_from_input, generate_pkce_pair, build_token_auth_headers, parse_and_save_tokens
from .token_manager import TokenManager
from .app_factory import create_app, run_app
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
    # App factory
    "create_app",
    "run_app",
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
    "AuthResult",
    "BrowserAutomation",
    "BrowserAutomationError",
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
    "register_commands",
    "oauth_login",
    "extract_code_from_input",
    "generate_pkce_pair",
    "build_token_auth_headers",
    "parse_and_save_tokens",
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
