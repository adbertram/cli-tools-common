"""Shared utilities for CLI tools: auth, profiles, config, output."""

from .config import BaseConfig
from .credentials import CredentialType, mask_value
from .exceptions import ClientError, ConfigError
from .auth_commands import create_auth_app
from .profiles_commands import create_profiles_app
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
    "CredentialType",
    "mask_value",
    "ClientError",
    "ConfigError",
    "create_auth_app",
    "create_profiles_app",
    "print_json",
    "print_table",
    "print_output",
    "print_error",
    "print_warning",
    "print_success",
    "print_info",
    "handle_error",
]
