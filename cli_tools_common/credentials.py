"""Credential type definitions, validation, and masking."""

from enum import Enum


class CredentialType(Enum):
    """Types of authentication credentials supported by CLI tools."""

    API_KEY = "api_key"
    PERSONAL_ACCESS_TOKEN = "personal_access_token"
    OAUTH = "oauth"
    OAUTH_AUTHORIZATION_CODE = "oauth_authorization_code"
    USERNAME_PASSWORD = "username_password"
    BROWSER_SESSION = "browser_session"

    @property
    def required_fields(self) -> list:
        """Fields that must be set for credentials to be valid."""
        return {
            CredentialType.API_KEY: ["API_KEY"],
            CredentialType.PERSONAL_ACCESS_TOKEN: ["PERSONAL_ACCESS_TOKEN"],
            CredentialType.OAUTH: ["CLIENT_ID", "CLIENT_SECRET", "ACCESS_TOKEN"],
            CredentialType.OAUTH_AUTHORIZATION_CODE: ["CLIENT_ID", "CLIENT_SECRET", "ACCESS_TOKEN"],
            CredentialType.USERNAME_PASSWORD: ["USERNAME", "PASSWORD"],
            CredentialType.BROWSER_SESSION: ["USERNAME", "PASSWORD"],
        }[self]

    @property
    def all_fields(self) -> list:
        """All fields associated with this credential type (for clearing)."""
        return {
            CredentialType.API_KEY: ["API_KEY", "BASE_URL"],
            CredentialType.PERSONAL_ACCESS_TOKEN: ["PERSONAL_ACCESS_TOKEN", "BASE_URL"],
            CredentialType.OAUTH: [
                "CLIENT_ID", "CLIENT_SECRET", "ACCESS_TOKEN",
                "REFRESH_TOKEN", "TOKEN_EXPIRES_AT", "BASE_URL",
            ],
            CredentialType.OAUTH_AUTHORIZATION_CODE: [
                "CLIENT_ID", "CLIENT_SECRET", "ACCESS_TOKEN",
                "REFRESH_TOKEN", "TOKEN_EXPIRES_AT", "REDIRECT_URI", "BASE_URL",
            ],
            CredentialType.USERNAME_PASSWORD: ["USERNAME", "PASSWORD", "BASE_URL"],
            CredentialType.BROWSER_SESSION: ["USERNAME", "PASSWORD", "BASE_URL"],
        }[self]

    @property
    def login_prompts(self) -> list:
        """Return (field_name, prompt_text, hide_input) tuples for interactive login.

        For OAUTH_AUTHORIZATION_CODE, these are the setup prompts for app credentials.
        The actual token acquisition happens via a login_handler callback (browser flow).
        """
        return {
            CredentialType.API_KEY: [
                ("API_KEY", "API key", True),
            ],
            CredentialType.PERSONAL_ACCESS_TOKEN: [
                ("PERSONAL_ACCESS_TOKEN", "Personal access token", True),
            ],
            CredentialType.OAUTH: [
                ("CLIENT_ID", "Client ID", False),
                ("CLIENT_SECRET", "Client secret", True),
            ],
            CredentialType.OAUTH_AUTHORIZATION_CODE: [
                ("CLIENT_ID", "Client ID", False),
                ("CLIENT_SECRET", "Client secret", True),
                ("REDIRECT_URI", "Redirect URI", False),
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
    def ephemeral_fields(self) -> list:
        """Fields cleared on --force (tokens, transient auth state).

        Static credentials (API keys, PATs, client IDs, passwords) are never
        cleared by --force since they don't expire or change.
        """
        return {
            CredentialType.API_KEY: [],
            CredentialType.PERSONAL_ACCESS_TOKEN: [],
            CredentialType.OAUTH: ["ACCESS_TOKEN", "REFRESH_TOKEN", "TOKEN_EXPIRES_AT"],
            CredentialType.OAUTH_AUTHORIZATION_CODE: ["ACCESS_TOKEN", "REFRESH_TOKEN", "TOKEN_EXPIRES_AT"],
            CredentialType.USERNAME_PASSWORD: [],
            CredentialType.BROWSER_SESSION: [],
        }[self]

    @property
    def sensitive_fields(self) -> list:
        """Fields that should be masked in status output."""
        return {
            CredentialType.API_KEY: ["API_KEY"],
            CredentialType.PERSONAL_ACCESS_TOKEN: ["PERSONAL_ACCESS_TOKEN"],
            CredentialType.OAUTH: ["CLIENT_SECRET", "ACCESS_TOKEN", "REFRESH_TOKEN"],
            CredentialType.OAUTH_AUTHORIZATION_CODE: ["CLIENT_SECRET", "ACCESS_TOKEN", "REFRESH_TOKEN"],
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


def combined_required_fields(cred_types: list) -> list:
    """Deduplicated required fields across multiple credential types."""
    seen = set()
    result = []
    for ct in cred_types:
        for field in ct.required_fields:
            if field not in seen:
                seen.add(field)
                result.append(field)
    return result


def combined_all_fields(cred_types: list) -> list:
    """Deduplicated all fields across multiple credential types."""
    seen = set()
    result = []
    for ct in cred_types:
        for field in ct.all_fields:
            if field not in seen:
                seen.add(field)
                result.append(field)
    return result


def combined_login_prompts(cred_types: list) -> list:
    """Deduplicated login prompts across multiple credential types (by field_name)."""
    seen = set()
    result = []
    for ct in cred_types:
        for prompt in ct.login_prompts:
            if prompt[0] not in seen:
                seen.add(prompt[0])
                result.append(prompt)
    return result


def combined_ephemeral_fields(cred_types: list) -> list:
    """Deduplicated ephemeral fields across multiple credential types."""
    seen = set()
    result = []
    for ct in cred_types:
        for field in ct.ephemeral_fields:
            if field not in seen:
                seen.add(field)
                result.append(field)
    return result


def combined_sensitive_fields(cred_types: list) -> list:
    """Deduplicated sensitive fields across multiple credential types."""
    seen = set()
    result = []
    for ct in cred_types:
        for field in ct.sensitive_fields:
            if field not in seen:
                seen.add(field)
                result.append(field)
    return result
