"""Standard cache management Typer app: clear."""

from pathlib import Path

import typer

from .output import print_json, print_success, print_info, handle_error


def create_cache_app(get_config_fn):
    """Create a standard cache Typer app for a CLI tool.

    Args:
        get_config_fn: Callable that accepts (profile=None) and returns a config
                       with a ``storage_dir`` property.

    Returns:
        typer.Typer app with a ``clear`` command.
    """
    app = typer.Typer(help="Manage response cache", no_args_is_help=True)

    @app.command("clear")
    def cache_clear():
        """Remove all cached responses.

        Deletes every file in the cache directory
        ({storage_dir}/cache/) and reports how many files
        and bytes were freed.
        """
        try:
            config = get_config_fn()
            cache_dir = Path(config.storage_dir) / "cache"

            if not cache_dir.exists():
                print_json({"files_removed": 0, "bytes_freed": 0})
                return

            files = list(cache_dir.iterdir())
            total_bytes = 0
            count = 0
            for f in files:
                if f.is_file():
                    total_bytes += f.stat().st_size
                    f.unlink()
                    count += 1

            print_json({"files_removed": count, "bytes_freed": total_bytes})
            print_success(f"Cleared {count} cached responses ({total_bytes:,} bytes freed)")

        except typer.Exit:
            raise
        except Exception as e:
            raise typer.Exit(handle_error(e))

    return app
