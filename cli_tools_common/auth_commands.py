"""Standard auth Typer app: login, logout, status, refresh with --profile support."""

import logging
import typer
from typing import Callable, Optional

from ._debug_logging import configure_debug_logger
from .auth_verifier import AuthVerifier
from .credentials import CredentialType, mask_value, combined_login_prompts, combined_required_fields
from .output import print_json, print_table, print_success, print_error, print_info, handle_error

logger = logging.getLogger("cli_tools.auth_commands")
configure_debug_logger(logger)


def _prompt_and_save(config, prompts, skip_if_set: bool = True) -> bool:
    """Prompt for credential fields and save values to config.

    Args:
        config: BaseConfig instance.
        prompts: Iterable of (field_name, prompt_text, hide_input) tuples.
        skip_if_set: If True, skip fields that already have a value.

    Returns:
        True if any field was prompted.
    """
    instructions_shown = False
    prompted = False
    for field_name, prompt_text, hide in prompts:
        current = config._get(field_name)
        if current and skip_if_set:
            continue
        # Show LOGIN_INSTRUCTIONS once before the first prompt
        if not instructions_shown:
            instructions = getattr(config, "LOGIN_INSTRUCTIONS", None)
            if instructions:
                print_info(instructions)
            instructions_shown = True
        prompted = True
        value = typer.prompt(f"Enter {prompt_text}", hide_input=hide)
        if not value or not value.strip():
            print_error(f"{prompt_text} cannot be empty")
            raise typer.Exit(1)
        config._set(field_name, value.strip())
    return prompted


def _handle_browser_login(config, tool_name: str, force: bool):
    """Handle browser session login if config.get_browser() is configured."""
    logger.debug("_handle_browser_login: tool=%s force=%s", tool_name, force)
    browser = config.get_browser()
    if browser is None:
        logger.debug("_handle_browser_login: no browser configured, skipping")
        return
    try:
        is_auth = browser.is_authenticated()
        logger.debug("_handle_browser_login: is_authenticated=%s", is_auth)
        if not force and is_auth:
            print_success(f"Already authenticated ({tool_name} browser session)")
        else:
            print_info("Opening browser for login...")
            logger.debug("_handle_browser_login: calling browser.login(force=%s)", force)
            result = browser.login(force=force)
            logger.debug("_handle_browser_login: login result=%s", result)
            if result.get("success"):
                print_success("Browser session authenticated")
            else:
                print_error(f"Browser auth failed: {result.get('message', 'Unknown error')}")
    finally:
        browser.close()


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
                _prompt_and_save(config, combined_login_prompts(active_types, config=config))
                _prompt_and_save(config, config.AUTH_EXTRA_PROMPTS, skip_if_set=not force)

                # Delegate to handler for token acquisition
                effective_handler(config, force)
            else:
                # Default prompt-based login — skip fields that already have values
                # (force only clears ephemeral fields, so static creds remain)
                prompted = _prompt_and_save(config, combined_login_prompts(active_types, config=config))
                _prompt_and_save(config, config.AUTH_EXTRA_PROMPTS, skip_if_set=not force)

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
        """Clear stored credentials and browser sessions."""
        try:
            config = get_config_fn(profile=profile)
            config.clear_credentials()
            # Also clear browser session (Playwright daemon + session data)
            browser = config.get_browser()
            if browser is not None:
                try:
                    browser.clear_session()
                except Exception:
                    pass
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
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
            logger.debug("auth_status: profile=%s has_credentials=%s",
                         config.get_active_profile_name(), config.has_credentials())

            # Live verification
            verifier = AuthVerifier(config)
            auth_result = verifier.verify()

            status_data = {
                "authenticated": auth_result["authenticated"],
                "credentials_saved": auth_result["credentials_saved"],
                "profile": config.get_active_profile_name(),
                "base_url": config.base_url,
            }

            # Masked credential fields (only if creds exist)
            if auth_result["credentials_saved"]:
                for field in combined_required_fields(config._resolved_credential_types, config=config):
                    value = config._get(field)
                    if value:
                        status_data[field.lower()] = mask_value(value)

            # Conditional fields from verification
            for key in ("oauth_status", "api_test", "browser_session", "browser_available"):
                if key in auth_result:
                    status_data[key] = auth_result[key]

            if not auth_result["credentials_saved"]:
                status_data["missing"] = config.get_missing_credentials()
                status_data["message"] = f"Not authenticated. Run '{tool_name} auth login' to configure."

            logger.debug("auth_status: final status_data=%s", status_data)
            if table:
                cols = list(status_data.keys())
                hdrs = [c.replace("_", " ").title() for c in cols]
                print_table([status_data], cols, hdrs)
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

                verifier = AuthVerifier(config, api_test_handler=effective_test_handler)
                result = verifier.verify()

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
