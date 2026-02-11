"""Standard profiles Typer app: list, create, set-default, delete."""

import typer

from .profiles import list_profiles, create_profile, set_default_profile, delete_profile
from .output import print_json, print_table, print_success, print_error, handle_error


def create_profiles_app(get_config_fn):
    """Create a standard profiles Typer app for a CLI tool.

    Args:
        get_config_fn: Callable that accepts (profile=None) and returns a BaseConfig.

    Returns:
        typer.Typer app with list, create, set-default, delete commands.
    """
    app = typer.Typer(help="Manage authentication profiles", no_args_is_help=True)

    @app.command("list")
    def profiles_list(
        table: bool = typer.Option(False, "--table", "-t", help="Display as table"),
    ):
        """List all profiles and show which is the default."""
        try:
            config = get_config_fn()
            profiles = list_profiles(config.tool_dir)

            if not profiles:
                print_error("No profiles found. Run 'auth login' to create one.")
                raise typer.Exit(1)

            if table:
                print_table(
                    profiles,
                    ["name", "file", "is_default"],
                    ["Name", "File", "Default"],
                )
            else:
                print_json(profiles)

        except typer.Exit:
            raise
        except Exception as e:
            raise typer.Exit(handle_error(e))

    @app.command("create")
    def profiles_create(
        name: str = typer.Argument(..., help="Profile name (e.g., staging, production)"),
    ):
        """Create a new profile from .env.example template."""
        try:
            config = get_config_fn()
            path = create_profile(config.tool_dir, name)
            print_success(f"Profile '{name}' created at {path.name}")
            from .output import print_info
            print_info(f"Run 'auth login --profile {name}' to configure credentials.")

        except typer.Exit:
            raise
        except Exception as e:
            raise typer.Exit(handle_error(e))

    @app.command("set-default")
    def profiles_set_default(
        name: str = typer.Argument(..., help="Profile name to set as default"),
    ):
        """Set a profile as the default (IS_DEFAULT_PROFILE=1)."""
        try:
            config = get_config_fn()
            set_default_profile(config.tool_dir, name)
            print_success(f"Profile '{name}' is now the default")

        except typer.Exit:
            raise
        except Exception as e:
            raise typer.Exit(handle_error(e))

    @app.command("delete")
    def profiles_delete(
        name: str = typer.Argument(..., help="Profile name to delete"),
        force: bool = typer.Option(False, "--force", "-F", help="Skip confirmation"),
    ):
        """Delete a profile and its data."""
        try:
            if not force and not typer.confirm(
                f"Delete profile '{name}'? This removes the .env file and profile data."
            ):
                raise typer.Exit(0)

            config = get_config_fn()
            delete_profile(config.tool_dir, name)
            print_success(f"Profile '{name}' deleted")

        except typer.Exit:
            raise
        except Exception as e:
            raise typer.Exit(handle_error(e))

    return app
