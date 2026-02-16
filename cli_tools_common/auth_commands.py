"""Standard auth Typer app: login, logout, status, refresh with --profile support."""

import typer
from typing import Callable, Optional

from .credentials import CredentialType, mask_value, combined_login_prompts, combined_required_fields
from .output import print_json, print_table, print_success, print_error, print_info, handle_error


def _prompt_extra_fields(config, force: bool):
    """Prompt for AUTH_EXTRA_PROMPTS fields on config."""
    for field_name, prompt_text, hide in config.AUTH_EXTRA_PROMPTS:
        current = config._get(field_name)
        if current and not force:
            continue
        value = typer.prompt(f"Enter {prompt_text}", hide_input=hide)
        if not value or not value.strip():
            print_error(f"{prompt_text} cannot be empty")
            raise typer.Exit(1)
        config._set(field_name, value.strip())


def _handle_browser_login(config, tool_name: str, force: bool):
    """Handle browser session login if config.get_browser() is configured."""
    browser = config.get_browser()
    if browser is None:
        return
    try:
        if not force and browser.is_authenticated():
            print_success(f"Already authenticated ({tool_name} API + browser)")
        else:
            print_info("Opening browser for login...")
            result = browser.login(force=force)
            if result.get("success"):
                print_success("Browser session authenticated")
            else:
                print_error(f"Browser auth failed: {result.get('message', 'Unknown error')}")
    finally:
        browser.close()


def _check_browser_status(config) -> Optional[bool]:
    """Check browser session status. Returns None if no browser configured."""
    browser = config.get_browser()
    if browser is None:
        return None
    try:
        return browser.is_authenticated()
    except Exception:
        return False
    finally:
        try:
            browser.close()
        except Exception:
            pass


def _resolve_credential_type(config, credential_type_str: str):
    """Resolve a credential type string to a CredentialType enum, validating it's configured."""
    cred_types = config._resolved_credential_types
    if len(cred_types) < 2:
        print_error("--credential-type is only valid for CLIs with multiple credential types")
        raise typer.Exit(1)
    for ct in cred_types:
        if ct.value == credential_type_str:
            return ct
    valid = ", ".join(ct.value for ct in cred_types)
    print_error(f"Unknown credential type '{credential_type_str}'. Valid types: {valid}")
    raise typer.Exit(1)


def create_auth_app(
    get_config_fn,
    tool_name: str = "tool",
    login_handler: Optional[Callable] = None,
    test_handler: Optional[Callable] = None,
):
    """Create a standard auth Typer app for a CLI tool.

    Args:
        get_config_fn: Callable that accepts (profile=None) and returns a BaseConfig.
        tool_name: CLI tool name for help text (e.g., 'cloudflare').
        login_handler: Optional callable(config, force) for custom login flows.
            Used by CLIs that need a custom OAuth flow (e.g., OAuth 1.0a,
            dual-auth). When provided, replaces the default interactive
            prompt login AND the built-in OAuth auto-detection. The handler
            is responsible for the entire login flow including obtaining
            and saving tokens.

            Handler priority (3-way resolution):
            1. Explicit login_handler param -> always wins
            2. Config has OAUTH_AUTH_URL + OAUTH_TOKEN_URL -> built-in oauth_login
            3. Neither -> default prompt-based login

        test_handler: Optional callable(config) -> dict for auth testing.
            Returns dict with at minimum {"api_test": "passed"|"failed: reason"}.
            Only provides the API test — credential checks and browser session
            checks are handled automatically by the common package.

    Returns:
        typer.Typer app with login, logout, status commands (+ refresh for OAuth,
        + test if test_handler provided).
    """
    app = typer.Typer(help=f"Manage {tool_name} authentication", no_args_is_help=True)

    @app.command("login")
    def auth_login(
        profile: Optional[str] = typer.Option(
            None, "--profile", "-p", help="Profile name to save credentials to"
        ),
        force: bool = typer.Option(
            False, "--force", "-F", help="Clear existing credentials and re-authenticate"
        ),
        credential_type: Optional[str] = typer.Option(
            None, "--credential-type", "-c",
            help="Authenticate only this credential type (e.g., 'oauth', 'browser_session')"
        ),
    ):
        """Configure authentication credentials.

        Prompts for required credentials based on the tool's authentication type.
        For OAuth authorization code flows, opens a browser for user consent.
        """
        try:
            config = get_config_fn(profile=profile)

            # Resolve scoped credential type if specified
            resolved_type = None
            if credential_type:
                resolved_type = _resolve_credential_type(config, credential_type)

            # Determine which credential types to process
            active_types = [resolved_type] if resolved_type else config._resolved_credential_types

            # Resolve effective handler (3-way)
            effective_handler = login_handler
            if effective_handler is None and config.OAUTH_AUTH_URL and config.OAUTH_TOKEN_URL:
                from .oauth import oauth_login
                effective_handler = oauth_login

            # Force clears ephemeral state (tokens + browser session), not static creds
            if force:
                if resolved_type:
                    config.clear_ephemeral_for_type(resolved_type)
                else:
                    config.clear_ephemeral()
                print_info("Existing sessions cleared")

            # Browser session only — skip all prompts, go directly to browser login
            if resolved_type == CredentialType.BROWSER_SESSION:
                _handle_browser_login(config, tool_name, force)
                return

            if effective_handler is not None:
                # Custom or built-in OAuth login flow
                # Ensure setup fields (CLIENT_ID, etc.) are configured first
                # Always skip if already set - handler controls force behavior
                for field_name, prompt_text, hide in combined_login_prompts(active_types):
                    current = config._get(field_name)
                    if current:
                        continue
                    value = typer.prompt(f"Enter {prompt_text}", hide_input=hide)
                    if not value or not value.strip():
                        print_error(f"{prompt_text} cannot be empty")
                        raise typer.Exit(1)
                    config._set(field_name, value.strip())

                # Prompt for extra fields before calling handler
                _prompt_extra_fields(config, force)

                # Delegate to handler for token acquisition
                effective_handler(config, force)
            else:
                # Default prompt-based login — skip fields that already have values
                # (force only clears ephemeral fields, so static creds remain)
                prompted = False
                for field_name, prompt_text, hide in combined_login_prompts(active_types):
                    current = config._get(field_name)
                    if current:
                        continue
                    prompted = True
                    value = typer.prompt(f"Enter {prompt_text}", hide_input=hide)
                    if not value or not value.strip():
                        print_error(f"{prompt_text} cannot be empty")
                        raise typer.Exit(1)
                    config._set(field_name, value.strip())

                # Prompt for extra fields after standard prompts
                _prompt_extra_fields(config, force)

                if prompted:
                    print_success("Credentials saved successfully")

            # Browser session login (if configured and no custom handler)
            # Custom handlers manage their own browser flow
            # Skip if --credential-type is set and it's not BROWSER_SESSION
            if effective_handler is None or effective_handler is not login_handler:
                if not resolved_type or resolved_type == CredentialType.BROWSER_SESSION:
                    _handle_browser_login(config, tool_name, force)

        except typer.Exit:
            raise
        except Exception as e:
            raise typer.Exit(handle_error(e))

    @app.command("logout")
    def auth_logout(
        profile: Optional[str] = typer.Option(
            None, "--profile", "-p", help="Profile name to clear credentials from"
        ),
    ):
        """Clear stored credentials."""
        try:
            config = get_config_fn(profile=profile)
            config.clear_credentials()
            print_success("Credentials cleared")
        except Exception as e:
            raise typer.Exit(handle_error(e))

    @app.command("status")
    def auth_status(
        profile: Optional[str] = typer.Option(
            None, "--profile", "-p", help="Profile name to check"
        ),
        table: bool = typer.Option(
            False, "--table", "-t", help="Display as table"
        ),
    ):
        """Check authentication status."""
        try:
            config = get_config_fn(profile=profile)

            if config.has_credentials():
                status_data = {
                    "authenticated": True,
                    "profile": config.get_active_profile_name(),
                    "base_url": config.base_url,
                }

                # Add masked credential fields
                for field in combined_required_fields(config._resolved_credential_types):
                    value = config._get(field)
                    if value:
                        status_data[field.lower()] = mask_value(value)

                # Auto-detect browser session status
                browser_status = _check_browser_status(config)
                if browser_status is not None:
                    status_data["browser_session"] = browser_status

                if table:
                    cols = list(status_data.keys())
                    hdrs = [c.replace("_", " ").title() for c in cols]
                    print_table([status_data], cols, hdrs)
                else:
                    print_json(status_data)
            else:
                missing = config.get_missing_credentials()
                status_data = {
                    "authenticated": False,
                    "profile": config.get_active_profile_name(),
                    "missing": missing,
                    "message": f"Not authenticated. Run '{tool_name} auth login' to configure.",
                }

                if table:
                    print_table(
                        [status_data],
                        ["authenticated", "profile", "message"],
                        ["Authenticated", "Profile", "Message"],
                    )
                else:
                    print_json(status_data)

        except typer.Exit:
            raise
        except Exception as e:
            raise typer.Exit(handle_error(e))

    # Add refresh command only if config has OAuth token URL
    # We check lazily via a probe config to avoid requiring profile at import time
    @app.command("refresh")
    def auth_refresh(
        profile: Optional[str] = typer.Option(
            None, "--profile", "-p", help="Profile name"
        ),
        table: bool = typer.Option(
            False, "--table", "-t", help="Display as table"
        ),
    ):
        """Refresh OAuth access token using stored refresh token."""
        try:
            config = get_config_fn(profile=profile)
            if not config.OAUTH_TOKEN_URL:
                print_error("Token refresh not supported (no OAUTH_TOKEN_URL configured)")
                raise typer.Exit(1)
            from .token_manager import TokenManager
            tm = TokenManager(config)
            tm.force_refresh()
            print_success("Access token refreshed")
        except typer.Exit:
            raise
        except Exception as e:
            raise typer.Exit(handle_error(e))

    # Auto-detect test_connection on config if no explicit test_handler
    effective_test_handler = test_handler

    if effective_test_handler is None:
        try:
            from .config import BaseConfig
            probe_config = get_config_fn()
            if type(probe_config).test_connection is not BaseConfig.test_connection:
                def _auto_test_handler(config):
                    result = config.test_connection()
                    if result is not None:
                        return result
                    return {"api_test": "skipped: no test_connection implemented"}
                effective_test_handler = _auto_test_handler
        except Exception:
            pass

    # Add test command if test_handler is provided or auto-detected
    if effective_test_handler is not None:
        @app.command("test")
        def auth_test(
            table: bool = typer.Option(False, "--table", "-t", help="Display as table"),
            verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detailed checks"),
            profile: Optional[str] = typer.Option(None, "--profile", "-p", help="Profile name"),
        ):
            """Test authentication by verifying credentials work."""
            try:
                config = get_config_fn(profile=profile)

                # 1. Check credentials exist (automatic)
                result = {"credentials_configured": config.has_credentials()}

                # 2. API test via CLI-provided callback
                if result["credentials_configured"]:
                    try:
                        api_result = effective_test_handler(config)
                        result.update(api_result)
                    except Exception as e:
                        result["api_test"] = f"failed: {e}"

                # 3. Browser session check (automatic if get_browser configured)
                browser_status = _check_browser_status(config)
                if browser_status is not None:
                    result["browser_session"] = browser_status

                # 4. Overall authenticated status
                api_ok = result.get("api_test") == "passed"
                browser_ok = result.get("browser_session", True)  # True if no browser configured
                result["authenticated"] = api_ok and browser_ok

                if verbose:
                    result["profile"] = config.get_active_profile_name()
                    result["base_url"] = config.base_url

                if table:
                    cols = list(result.keys())
                    hdrs = [c.replace("_", " ").title() for c in cols]
                    print_table([result], cols, hdrs)
                else:
                    print_json(result)
            except typer.Exit:
                raise
            except Exception as e:
                raise typer.Exit(handle_error(e))

    return app
