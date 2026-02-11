"""Credential type definitions, validation, and masking."""

from enum import Enum


class CredentialType(Enum):
    """Types of authentication credentials supported by CLI tools."""

    API_KEY = "api_key"
    OAUTH = "oauth"
    USERNAME_PASSWORD = "username_password"
    BROWSER_SESSION = "browser_session"

    @property
    def required_fields(self) -> list:
        """Fields that must be set for credentials to be valid."""
        return {
            CredentialType.API_KEY: ["API_KEY"],
            CredentialType.OAUTH: ["CLIENT_ID", "CLIENT_SECRET", "ACCESS_TOKEN"],
            CredentialType.USERNAME_PASSWORD: ["USERNAME", "PASSWORD"],
            CredentialType.BROWSER_SESSION: ["USERNAME", "PASSWORD"],
        }[self]

    @property
    def all_fields(self) -> list:
        """All fields associated with this credential type (for clearing)."""
        return {
            CredentialType.API_KEY: ["API_KEY", "BASE_URL"],
            CredentialType.OAUTH: [
                "CLIENT_ID", "CLIENT_SECRET", "ACCESS_TOKEN",
                "REFRESH_TOKEN", "TOKEN_EXPIRES_AT", "BASE_URL",
            ],
            CredentialType.USERNAME_PASSWORD: ["USERNAME", "PASSWORD", "BASE_URL"],
            CredentialType.BROWSER_SESSION: ["USERNAME", "PASSWORD", "BASE_URL"],
        }[self]

    @property
    def login_prompts(self) -> list:
        """Return (field_name, prompt_text, hide_input) tuples for interactive login."""
        return {
            CredentialType.API_KEY: [
                ("API_KEY", "API key", True),
            ],
            CredentialType.OAUTH: [
                ("CLIENT_ID", "Client ID", False),
                ("CLIENT_SECRET", "Client secret", True),
            ],
            CredentialType.USERNAME_PASSWORD: [
                ("USERNAME", "Username", False),
                ("PASSWORD", "Password", True),
            ],
            CredentialType.BROWSER_SESSION: [
                ("USERNAME", "Username", False),
                ("PASSWORD", "Password", True),
            ],
        }[self]

    @property
    def sensitive_fields(self) -> list:
        """Fields that should be masked in status output."""
        return {
            CredentialType.API_KEY: ["API_KEY"],
            CredentialType.OAUTH: ["CLIENT_SECRET", "ACCESS_TOKEN", "REFRESH_TOKEN"],
            CredentialType.USERNAME_PASSWORD: ["PASSWORD"],
            CredentialType.BROWSER_SESSION: ["PASSWORD"],
        }[self]


def mask_value(value: str) -> str:
    """Mask a credential value for display.

    Shows first 4 and last 4 characters for long values.

    Args:
        value: The credential value to mask.

    Returns:
        Masked string like 'abc1...xyz4' or '***' for short values.
    """
    if not value or len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"
