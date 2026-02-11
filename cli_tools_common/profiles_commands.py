"""Standard profiles Typer app: list, create, set-default, delete, get."""

from typing import Optional, List

import typer

from .profiles import list_profiles, create_profile, set_default_profile, delete_profile
from .output import print_json, print_table, print_success, print_error, handle_error


def _apply_filters(profiles, filter_strings):
    """Apply simple field:op:value filters to profile dicts."""
    if not filter_strings:
        return profiles
    result = profiles
    for f_str in filter_strings:
        parts = f_str.split(",")
        for part in parts:
            tokens = part.strip().split(":")
            if len(tokens) >= 3:
                field, op, value = tokens[0], tokens[1], ":".join(tokens[2:])
            elif len(tokens) == 2:
                field, op, value = tokens[0], "eq", tokens[1]
            else:
                continue
            filtered = []
            for p in result:
                pval = str(p.get(field, ""))
                if op == "eq" and pval == value:
                    filtered.append(p)
                elif op == "ne" and pval != value:
                    filtered.append(p)
                elif op == "contains" and value in pval:
                    filtered.append(p)
                elif op == "startswith" and pval.startswith(value):
                    filtered.append(p)
                elif op == "endswith" and pval.endswith(value):
                    filtered.append(p)
                else:
                    # Unknown op defaults to eq
                    if op == "eq" and pval == value:
                        filtered.append(p)
            result = filtered
    return result


def create_profiles_app(get_config_fn):
    """Create a standard profiles Typer app for a CLI tool.

    Args:
        get_config_fn: Callable that accepts (profile=None) and returns a BaseConfig.

    Returns:
        typer.Typer app with list, get, create, set-default, delete commands.
    """
    app = typer.Typer(help="Manage authentication profiles", no_args_is_help=True)

    @app.command("list")
    def profiles_list(
        table: bool = typer.Option(False, "--table", "-t", help="Display as table"),
        limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Maximum number of profiles to return"),
        filter: Optional[List[str]] = typer.Option(None, "--filter", "-f", help="Filter: field:op:value (e.g., is_default:eq:True)"),
        properties: Optional[str] = typer.Option(None, "--properties", "-p", help="Comma-separated list of fields to include"),
    ):
        """List all profiles and show which is the default."""
        try:
            config = get_config_fn()
            profiles = list_profiles(config.tool_dir)

            if not profiles:
                print_error("No profiles found. Run 'auth login' to create one.")
                raise typer.Exit(1)

            # Apply filters
            profiles = _apply_filters(profiles, filter)

            # Apply limit
            if limit is not None:
                profiles = profiles[:limit]

            # Apply properties selection
            if properties:
                prop_list = [p.strip() for p in properties.split(",")]
                profiles = [{k: v for k, v in p.items() if k in prop_list} for p in profiles]

            if table:
                if properties:
                    prop_list = [p.strip() for p in properties.split(",")]
                    headers = [c.replace("_", " ").title() for c in prop_list]
                    print_table(profiles, prop_list, headers)
                else:
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

    @app.command("get")
    def profiles_get(
        name: str = typer.Argument(..., help="Profile name to get details for"),
        table: bool = typer.Option(False, "--table", "-t", help="Display as table"),
    ):
        """Get details for a specific profile."""
        try:
            config = get_config_fn()
            profiles = list_profiles(config.tool_dir)

            match = [p for p in profiles if p.get("name") == name]
            if not match:
                print_error(f"Profile '{name}' not found.")
                raise typer.Exit(1)

            profile = match[0]
            if table:
                columns = list(profile.keys())
                headers = [c.replace("_", " ").title() for c in columns]
                print_table([profile], columns, headers)
            else:
                print_json(profile)

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
