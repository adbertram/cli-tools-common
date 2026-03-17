"""App factory: standardised Typer app creation with global flags."""

import os
from typing import Optional

import typer


def create_app(
    name: str,
    help: str,
    version: str,
    *,
    cache_support: bool = True,
) -> typer.Typer:
    """Create a standardised Typer app with --version and optional --no-cache.

    Args:
        name: CLI tool name (e.g. "slack").
        help: Help text shown in the root ``--help``.
        version: Version string printed by ``--version``.
        cache_support: Add a ``--no-cache`` global flag (default True).

    Returns:
        Configured :class:`typer.Typer` instance.
    """
    app = typer.Typer(name=name, help=help, add_completion=True)

    @app.callback(invoke_without_command=True)
    def _callback(
        ctx: typer.Context,
        version_flag: Optional[bool] = typer.Option(
            None, "--version", "-v", help="Show version and exit", is_eager=True,
        ),
        no_cache: bool = typer.Option(
            False, "--no-cache", help="Bypass response cache",
            hidden=not cache_support,
        ),
    ):
        if no_cache and cache_support:
            os.environ["CACHE_ENABLED"] = "false"
        if version_flag:
            typer.echo(f"{name}-cli version {version}")
            raise typer.Exit()
        if ctx.invoked_subcommand is None:
            typer.echo(ctx.get_help())
            raise typer.Exit()

    return app


def run_app(app: typer.Typer, *, error_types=None) -> None:
    """Run *app* with standard error handling.

    Args:
        app: The Typer application to run.
        error_types: Exception class or tuple of classes treated as client
            errors (printed to stderr, exit 2).  Defaults to
            :class:`cli_tools_common.exceptions.ClientError`.
    """
    if error_types is None:
        from .exceptions import ClientError
        error_types = (ClientError,)
    elif isinstance(error_types, type):
        error_types = (error_types,)

    try:
        app()
    except error_types as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(2)
    except KeyboardInterrupt:
        typer.echo("\nAborted!", err=True)
        raise typer.Exit(130)
