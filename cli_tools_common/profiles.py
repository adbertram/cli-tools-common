"""Profile CRUD operations for CLI tools."""

import shutil
from pathlib import Path

from .config import (
    env_path_for_profile,
    get_profiles_base_dir,
    profile_name_from_path,
    read_is_default_profile,
    list_env_files,
)
from .exceptions import ConfigError


def list_profiles(tool_dir: Path) -> list:
    """List all profiles in a tool directory.

    Returns list of dicts with 'name', 'file', and 'is_default' keys.
    """
    env_files = list_env_files(tool_dir)
    profiles = []
    for f in env_files:
        is_default = read_is_default_profile(f)
        profiles.append({
            "name": profile_name_from_path(f),
            "file": f.name,
            "is_default": bool(is_default),
        })
    return profiles


def create_profile(tool_dir: Path, name: str) -> Path:
    """Create a new profile by copying .env.example.

    Args:
        tool_dir: Root directory of the CLI tool.
        name: Profile name (e.g., 'staging').

    Returns:
        Path to the created .env.{name} file.

    Raises:
        ConfigError: If profile already exists or .env.example not found.
    """
    target = env_path_for_profile(tool_dir, name)
    if target.exists():
        raise ConfigError(f"Profile '{name}' already exists at {target}")

    example = tool_dir / ".env.example"
    if example.exists():
        # Copy from .env.example and ensure IS_DEFAULT_PROFILE=0
        shutil.copy2(example, target)
        _set_is_default_in_file(target, False)
    else:
        # No .env.example - create minimal file
        target.write_text("IS_DEFAULT_PROFILE=0\n")

    return target


def set_default_profile(tool_dir: Path, name: str):
    """Set a profile as the default (IS_DEFAULT_PROFILE=1).

    Sets IS_DEFAULT_PROFILE=0 in all other .env files.

    Args:
        tool_dir: Root directory of the CLI tool.
        name: Profile name to make default.

    Raises:
        ConfigError: If profile not found.
    """
    target = env_path_for_profile(tool_dir, name)
    if not target.exists():
        raise ConfigError(f"Profile '{name}' not found at {target}")

    env_files = list_env_files(tool_dir)
    for f in env_files:
        _set_is_default_in_file(f, f == target)


def delete_profile(tool_dir: Path, name: str):
    """Delete a profile and its data directory.

    Args:
        tool_dir: Root directory of the CLI tool.
        name: Profile name to delete.

    Raises:
        ConfigError: If profile not found or is the default.
    """
    target = env_path_for_profile(tool_dir, name)
    if not target.exists():
        raise ConfigError(f"Profile '{name}' not found at {target}")

    if read_is_default_profile(target) is True:
        raise ConfigError(
            f"Cannot delete default profile '{name}'. "
            "Set another profile as default first with 'profiles set-default <name>'."
        )

    target.unlink()

    # Clean up profile data directory (new XDG location)
    profile_data_dir = get_profiles_base_dir(tool_dir.name) / name
    if profile_data_dir.exists():
        shutil.rmtree(profile_data_dir)
    # Also clean up legacy location if it exists
    legacy_data_dir = tool_dir / ".profiles" / name
    if legacy_data_dir.exists():
        shutil.rmtree(legacy_data_dir)


def _set_is_default_in_file(env_path: Path, is_default: bool):
    """Set IS_DEFAULT_PROFILE value in an env file.

    Updates in-place if the line exists, prepends if not.
    """
    value = "1" if is_default else "0"
    try:
        content = env_path.read_text()
    except OSError:
        return

    lines = content.splitlines()
    new_lines = []
    found = False
    for line in lines:
        if line.strip().startswith("IS_DEFAULT_PROFILE="):
            new_lines.append(f"IS_DEFAULT_PROFILE={value}")
            found = True
        else:
            new_lines.append(line)

    if not found:
        new_lines.insert(0, f"IS_DEFAULT_PROFILE={value}")

    env_path.write_text("\n".join(new_lines) + "\n")
