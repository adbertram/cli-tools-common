"""Base configuration with profile-aware env loading."""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv, set_key

from .credentials import CredentialType, combined_all_fields, combined_required_fields
from .exceptions import ConfigError


# ==================== Cache Utilities ====================

_CACHE_TRUTHY = ("true", "1", "yes")
DEFAULT_CACHE_TTL = 3600


def is_cache_enabled() -> bool:
    """Check CACHE_ENABLED env var (default: true)."""
    return os.environ.get("CACHE_ENABLED", "true").lower() in _CACHE_TRUTHY


def get_cache_ttl() -> int:
    """Read CACHE_TTL env var (default: 3600)."""
    return int(os.environ.get("CACHE_TTL", str(DEFAULT_CACHE_TTL)))


def _read_is_default_profile(env_path: Path) -> Optional[bool]:
    """Read IS_DEFAULT_PROFILE from an env file without loading into os.environ.

    Returns True if IS_DEFAULT_PROFILE=1, False if =0, None if not found.
    """
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("IS_DEFAULT_PROFILE="):
                    value = line.split("=", 1)[1].strip().strip("\"'")
                    return value == "1"
    except (OSError, UnicodeDecodeError):
        pass
    return None


def _profile_name_from_path(env_path: Path) -> str:
    """Extract profile name from env file path.

    .env → 'default', .env.staging → 'staging'
    """
    name = env_path.name
    if name == ".env":
        return "default"
    return name[5:]  # Remove ".env." prefix


def _env_path_for_profile(tool_dir: Path, profile_name: str) -> Path:
    """Get env file path for a profile name.

    'default' → .env, 'staging' → .env.staging
    """
    if profile_name == "default":
        return tool_dir / ".env"
    return tool_dir / f".env.{profile_name}"


def list_env_files(tool_dir: Path) -> list:
    """List all .env profile files in a tool directory.

    Returns .env and .env.* files, excluding .env.example.
    """
    files = []
    bare = tool_dir / ".env"
    if bare.exists():
        files.append(bare)
    for f in sorted(tool_dir.glob(".env.*")):
        if f.name != ".env.example":
            files.append(f)
    return files


class BaseConfig:
    """Base configuration with profile-aware env loading.

    Subclasses set class variables:
        CREDENTIAL_TYPES: list of CredentialType values (AND — all must be satisfied)
        DEFAULT_BASE_URL: str fallback URL

    Example subclass (simple API key tool):

        class Config(BaseConfig):
            CREDENTIAL_TYPES = [CredentialType.API_KEY]
            DEFAULT_BASE_URL = "https://api.example.com/v1"

            def __init__(self, profile=None):
                super().__init__(
                    tool_dir=Path(__file__).resolve().parent.parent,
                    profile=profile,
                )
    """

    CREDENTIAL_TYPE: CredentialType = None  # Deprecated: use CREDENTIAL_TYPES
    CREDENTIAL_TYPES: list = None           # List of CredentialType values (AND)
    DEFAULT_BASE_URL: str = ""

    # OAuth 2.0 configuration (set by subclasses that use OAuth)
    OAUTH_AUTH_URL: str = ""              # Authorization endpoint
    OAUTH_TOKEN_URL: str = ""            # Token endpoint
    OAUTH_SCOPES: list = []              # Scope strings
    OAUTH_REDIRECT_URI: str = ""         # Default redirect URI (overridable in .env via REDIRECT_URI)
    OAUTH_PKCE: bool = False             # Enable PKCE (S256)
    OAUTH_STATE: bool = False            # Generate + verify state parameter
    OAUTH_TOKEN_AUTH: str = "body"       # "basic" | "body" | "none"
    OAUTH_EXTRA_AUTH_PARAMS: dict = {}   # Extra params for auth URL (e.g. audience)
    OAUTH_USE_PLAYWRIGHT: bool = False   # Use Playwright to capture redirect vs manual paste
    OAUTH_PLAYWRIGHT_TIMEOUT: int = 120  # Seconds to wait for Playwright redirect

    # Extra credential prompts (set by subclasses that need additional fields prompted during login)
    # List of (field_name, prompt_label, hide_input) tuples
    # Prompted AFTER standard credential prompts but BEFORE login_handler or browser login
    AUTH_EXTRA_PROMPTS: list = []

    # Custom credential type field definitions (only used when CREDENTIAL_TYPES includes CUSTOM)
    CUSTOM_REQUIRED_FIELDS: list = []
    CUSTOM_ALL_FIELDS: list = []
    CUSTOM_LOGIN_PROMPTS: list = []
    CUSTOM_EPHEMERAL_FIELDS: list = []
    CUSTOM_SENSITIVE_FIELDS: list = []

    @property
    def _resolved_credential_types(self) -> list:
        """Resolve credential types with backward compatibility.

        Returns CREDENTIAL_TYPES if set, falls back to [CREDENTIAL_TYPE].
        """
        if self.CREDENTIAL_TYPES is not None:
            return self.CREDENTIAL_TYPES
        if self.CREDENTIAL_TYPE is not None:
            return [self.CREDENTIAL_TYPE]
        raise ConfigError(
            "Subclass must set CREDENTIAL_TYPES (list) or CREDENTIAL_TYPE (deprecated)."
        )

    def __init__(self, tool_dir: Path, profile: str = None):
        """Initialize config by resolving the profile and loading the env file.

        Profile resolution priority:
            1. Explicit profile argument (from --profile flag)
            2. CLI_TOOLS_PROFILE environment variable
            3. Whichever .env* file has IS_DEFAULT_PROFILE=1

        Args:
            tool_dir: Root directory of the CLI tool (contains .env files).
            profile: Optional explicit profile name.
        """
        self.tool_dir = tool_dir
        self.profile = profile
        self.env_file_path = self._resolve_env_file(profile)

        if self.env_file_path.exists():
            # Clear standard credential env vars before loading to prevent
            # stale values from a previously loaded profile
            for field in combined_all_fields(self._resolved_credential_types, config=self):
                os.environ.pop(field, None)
            os.environ.pop("IS_DEFAULT_PROFILE", None)
            load_dotenv(self.env_file_path, override=True)
        # If no .env file exists, keep current env vars intact — supports
        # running with credentials injected via environment (e.g., n8n nodes)

    def _resolve_env_file(self, profile: str = None) -> Path:
        """Resolve which .env file to load."""
        # 1. Explicit profile argument
        if profile:
            return self._env_file_for_profile(profile)

        # 2. CLI_TOOLS_PROFILE env var
        env_profile = os.getenv("CLI_TOOLS_PROFILE")
        if env_profile:
            return self._env_file_for_profile(env_profile)

        # 3. Find default (IS_DEFAULT_PROFILE=1)
        return self._find_default_env_file()

    def _env_file_for_profile(self, name: str) -> Path:
        """Get .env file path for a named profile."""
        path = _env_path_for_profile(self.tool_dir, name)
        if not path.exists():
            raise ConfigError(
                f"Profile '{name}' not found. "
                f"Expected file: {path}\n"
                f"Run 'profiles create {name}' to create it."
            )
        return path

    def _find_default_env_file(self) -> Path:
        """Find the .env file with IS_DEFAULT_PROFILE=1."""
        env_files = list_env_files(self.tool_dir)

        if not env_files:
            # No env files exist yet - return bare .env (will be created)
            return self.tool_dir / ".env"

        defaults = []
        for f in env_files:
            if _read_is_default_profile(f) is True:
                defaults.append(f)

        if len(defaults) == 1:
            return defaults[0]

        if len(defaults) > 1:
            names = [_profile_name_from_path(f) for f in defaults]
            raise ConfigError(
                f"Multiple default profiles found: {', '.join(names)}. "
                "Only one .env file should have IS_DEFAULT_PROFILE=1."
            )

        # No IS_DEFAULT_PROFILE=1 found - fall back to bare .env (legacy support)
        bare = self.tool_dir / ".env"
        if bare.exists():
            return bare

        raise ConfigError(
            "No default profile found. Set IS_DEFAULT_PROFILE=1 in one .env file."
        )

    # ==================== Generic Get/Set/Clear ====================

    def _get(self, name: str) -> Optional[str]:
        """Get an env var value. Returns None for empty strings."""
        val = os.getenv(name)
        return val if val else None

    def _set(self, name: str, value: str):
        """Set an env var in both the .env file and os.environ."""
        set_key(str(self.env_file_path), name, value)
        os.environ[name] = value

    def _clear(self, name: str):
        """Clear an env var from the .env file and os.environ."""
        set_key(str(self.env_file_path), name, "")
        os.environ.pop(name, None)

    # ==================== Standard Properties ====================

    @property
    def api_key(self) -> Optional[str]:
        return self._get("API_KEY")

    @property
    def client_id(self) -> Optional[str]:
        return self._get("CLIENT_ID")

    @property
    def client_secret(self) -> Optional[str]:
        return self._get("CLIENT_SECRET")

    @property
    def personal_access_token(self) -> Optional[str]:
        return self._get("PERSONAL_ACCESS_TOKEN")

    @property
    def access_token(self) -> Optional[str]:
        return self._get("ACCESS_TOKEN")

    @property
    def refresh_token(self) -> Optional[str]:
        return self._get("REFRESH_TOKEN")

    @property
    def token_expires_at(self) -> Optional[str]:
        return self._get("TOKEN_EXPIRES_AT")

    @property
    def username(self) -> Optional[str]:
        return self._get("USERNAME")

    @property
    def password(self) -> Optional[str]:
        return self._get("PASSWORD")

    @property
    def redirect_uri(self) -> Optional[str]:
        return self._get("REDIRECT_URI")

    @property
    def base_url(self) -> str:
        return self._get("BASE_URL") or self.DEFAULT_BASE_URL

    @property
    def cache_enabled(self) -> bool:
        return is_cache_enabled()

    @property
    def cache_ttl(self) -> int:
        return get_cache_ttl()

    # ==================== Credential Management ====================

    def has_credentials(self) -> bool:
        """Check if required credentials are set."""
        cred_types = self._resolved_credential_types
        if CredentialType.BROWSER_SESSION in cred_types:
            # Browser session: require saved session profile + any non-browser credential fields
            non_browser_types = [ct for ct in cred_types if ct != CredentialType.BROWSER_SESSION]
            non_browser_ok = all(self._get(f) for f in combined_required_fields(non_browser_types, config=self))
            return non_browser_ok and self.has_saved_session()
        return all(self._get(f) for f in combined_required_fields(cred_types, config=self))

    def get_missing_credentials(self) -> list:
        """Get list of missing required credential field names."""
        return [f for f in combined_required_fields(self._resolved_credential_types, config=self) if not self._get(f)]

    def save_api_key(self, api_key: str):
        """Save API key credential."""
        self._set("API_KEY", api_key)

    def save_credentials(self, **kwargs):
        """Save arbitrary credentials. Keys are uppercased to env var names."""
        for key, value in kwargs.items():
            self._set(key.upper(), value)

    def save_tokens(self, access_token: str, refresh_token: str, expires_at: str):
        """Save OAuth tokens."""
        self._set("ACCESS_TOKEN", access_token)
        self._set("REFRESH_TOKEN", refresh_token)
        self._set("TOKEN_EXPIRES_AT", expires_at)

    def clear_credentials(self):
        """Clear all credential fields for this credential type."""
        for field in combined_all_fields(self._resolved_credential_types, config=self):
            self._clear(field)

    def clear_ephemeral(self):
        """Clear ephemeral fields (tokens) and browser session. Preserves static credentials."""
        from .credentials import combined_ephemeral_fields  # avoid circular at module level
        for field in combined_ephemeral_fields(self._resolved_credential_types, config=self):
            self._clear(field)
        self.clear_session()

    def clear_ephemeral_for_type(self, cred_type: 'CredentialType'):
        """Clear ephemeral fields for a single credential type."""
        if cred_type == CredentialType.CUSTOM:
            fields = self.CUSTOM_EPHEMERAL_FIELDS
        else:
            fields = cred_type.ephemeral_fields
        for field in fields:
            self._clear(field)
        if cred_type == CredentialType.BROWSER_SESSION:
            self.clear_session()

    # ==================== Profile Data Directories ====================

    def get_profiles_dir(self) -> Path:
        """Get .profiles/ directory for runtime data."""
        return self.tool_dir / ".profiles"

    def get_profile_data_dir(self) -> Path:
        """Get data directory for the active profile."""
        name = _profile_name_from_path(self.env_file_path)
        profile_dir = self.get_profiles_dir() / name
        profile_dir.mkdir(parents=True, exist_ok=True)
        return profile_dir

    def get_browser_data_dir(self) -> Path:
        """Get browser data directory for the active profile."""
        browser_dir = self.get_profile_data_dir() / "browser-data"
        browser_dir.mkdir(parents=True, exist_ok=True)
        return browser_dir

    def has_saved_session(self) -> bool:
        """Check if a saved browser session exists for the active profile."""
        profile_dir = self.get_profile_data_dir()
        # New browser template: profile.json in profile data dir
        if (profile_dir / "profile.json").exists():
            return True
        # Old BrowserAutomation pattern: browser-data subdirectory
        browser_dir = profile_dir / "browser-data"
        return browser_dir.exists() and any(browser_dir.iterdir())

    def clear_session(self):
        """Clear saved session data for the active profile."""
        import shutil
        profile_dir = self.get_profile_data_dir()
        if profile_dir.exists():
            shutil.rmtree(profile_dir)

    def clear_all(self):
        """Clear credentials and session data."""
        self.clear_credentials()
        self.clear_session()

    # ==================== Active Profile Info ====================

    def get_active_profile_name(self) -> str:
        """Get the name of the currently active profile."""
        return _profile_name_from_path(self.env_file_path)

    def test_connection(self) -> Optional[dict]:
        """Test API connectivity. Override in subclass to make a lightweight API call.

        Returns:
            dict with at minimum {"api_test": "passed"} or {"api_test": "failed: reason"},
            or None if no test is implemented.
        """
        return None

    def get_browser(self):
        """Return browser service instance for browser-based authentication.

        Override in CLI Config subclasses that require browser session authentication
        (in addition to or instead of API credentials).

        The returned object must implement:
        - is_authenticated() -> bool
        - login(force: bool) -> dict with 'success' key
        - close() -> None

        Returns None if browser auth is not needed.
        """
        return None
