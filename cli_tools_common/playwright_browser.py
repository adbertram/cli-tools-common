"""Playwright CLI browser adapter for create_auth_app() integration.

Implements the browser interface expected by auth_commands.py:
  - is_authenticated() -> bool
  - login(force) -> dict
  - close() -> None

Uses named playwright sessions (--session flag) for per-tool isolation.
"""

import json
import shutil
import subprocess
from pathlib import Path


class PlaywrightCLIBrowser:
    """Browser adapter that delegates to the playwright CLI with named sessions.

    Args:
        session_name: Named session for playwright --session flag.
        login_url: URL to open for interactive login.
        config: BaseConfig instance (provides get_profile_data_dir()).
    """

    def __init__(self, session_name: str, login_url: str, config):
        self.session_name = session_name
        self.login_url = login_url
        self.config = config

    def _run(self, args: list, timeout: int = 30, check: bool = True) -> subprocess.CompletedProcess:
        """Run a playwright CLI command with the named session."""
        cmd = ["playwright", "--session", self.session_name] + args
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if check and result.returncode != 0:
                error_msg = result.stderr.strip() or result.stdout.strip() or "Command failed"
                raise RuntimeError(f"playwright error: {error_msg}")
            return result
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Command timed out after {timeout}s")
        except FileNotFoundError:
            raise RuntimeError("playwright CLI not found in PATH")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Failed to run command: {e}")

    def _marker_path(self) -> Path:
        """Path to the profile.json marker file."""
        return self.config.get_profile_data_dir() / "profile.json"

    def is_authenticated(self) -> bool:
        """Check if a browser session marker exists."""
        return self._marker_path().exists()

    def has_session(self) -> bool:
        """Check if profile.json exists (alias for is_authenticated)."""
        return self._marker_path().exists()

    def login(self, force: bool = False) -> dict:
        """Open a headed persistent browser for interactive login.

        After the user closes the browser, writes a profile.json marker
        so has_saved_session() on BaseConfig returns True.
        """
        if force:
            self.clear_session()
        self._run(
            ["browser", "open", "--headed", "--persistent", self.login_url],
            timeout=300,
            check=False,
        )
        # Write marker so BaseConfig.has_saved_session() detects the session
        marker = self._marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"session_name": self.session_name, "authenticated": True}))
        return {"success": True, "message": "Browser session authenticated"}

    def close(self) -> None:
        """Close the browser session."""
        self._run(["browser", "close"], check=False, timeout=10)

    def clear_session(self) -> None:
        """Delete the marker and playwright session data."""
        marker = self._marker_path()
        if marker.exists():
            marker.unlink()
        self._run(["data", "delete"], check=False, timeout=10)

    def test_session(self) -> dict:
        """Return dict with authenticated status."""
        return {"authenticated": self.is_authenticated()}

    # ==================== Static Helpers ====================

    @staticmethod
    def is_cli_available() -> bool:
        """Check if the playwright CLI is available in PATH."""
        return shutil.which("playwright") is not None

    @staticmethod
    def get_cli_version() -> str | None:
        """Get the version of the playwright CLI."""
        try:
            result = subprocess.run(
                ["playwright", "--version"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None
