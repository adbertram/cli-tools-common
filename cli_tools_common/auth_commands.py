"""Standard auth Typer app: login, logout, status with --profile support."""

import typer
from typing import Callable, Optional

from .credentials import CredentialType, mask_value
from .output import print_json, print_table, print_success, print_error, print_info, handle_error


def create_auth_app(
    get_config_fn,
    tool_name: str = "tool",
    login_handler: Optional[Callable] = None,
):
    """Create a standard auth Typer app for a CLI tool.

    Args:
        get_config_fn: Callable that accepts (profile=None) and returns a BaseConfig.
        tool_name: CLI tool name for help text (e.g., 'cloudflare').
        login_handler: Optional callable(config, force) for custom login flows.
            Used by OAUTH_AUTHORIZATION_CODE CLIs that need a browser-based
            OAuth flow (open browser → user consents → paste code → exchange
            for tokens). When provided, replaces the default interactive
            prompt login. The handler is responsible for the entire login
            flow including obtaining and saving tokens.

    Returns:
        typer.Typer app with login, logout, status commands.
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
    ):
        """Configure authentication credentials.

        Prompts for required credentials based on the tool's authentication type.
        For OAuth authorization code flows, opens a browser for user consent.
        """
        try:
            config = get_config_fn(profile=profile)

            if force:
                config.clear_credentials()
                print_info("Existing credentials cleared")

            if login_handler is not None:
                # Custom login flow (e.g., browser-based OAuth)
                # Ensure setup fields (CLIENT_ID, etc.) are configured first
                cred_type = config.CREDENTIAL_TYPE
                for field_name, prompt_text, hide in cred_type.login_prompts:
                    current = config._get(field_name)
                    if current and not force:
                        continue
                    value = typer.prompt(f"Enter {prompt_text}", hide_input=hide)
                    if not value or not value.strip():
                        print_error(f"{prompt_text} cannot be empty")
                        raise typer.Exit(1)
                    config._set(field_name, value.strip())

                # Delegate to custom handler for token acquisition
                login_handler(config, force)
            else:
                # Default prompt-based login
                cred_type = config.CREDENTIAL_TYPE
                for field_name, prompt_text, hide in cred_type.login_prompts:
                    current = config._get(field_name)
                    if current and not force:
                        continue
                    value = typer.prompt(f"Enter {prompt_text}", hide_input=hide)
                    if not value or not value.strip():
                        print_error(f"{prompt_text} cannot be empty")
                        raise typer.Exit(1)
                    config._set(field_name, value.strip())

                print_success("Credentials saved successfully")

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
                cred_type = config.CREDENTIAL_TYPE
                for field in cred_type.required_fields:
                    value = config._get(field)
                    if value:
                        status_data[field.lower()] = mask_value(value)

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

    return app
