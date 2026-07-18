"""Interactive one-time Garmin Connect login.

Run this once (`garmin-mcp-auth`) to authenticate and cache a session token
so the MCP server (`garmin-mcp`) never has to prompt for credentials again.
Supports Garmin's multi-factor authentication (MFA) via an interactive prompt.
"""
import getpass
import os
import sys
from pathlib import Path

from garminconnect import Garmin


def token_store() -> str:
    """Resolve the token cache path (env override or ~/.garminconnect)."""
    return os.environ.get("GARMIN_TOKENS", str(Path.home() / ".garminconnect"))


def _prompt_mfa() -> str:
    print("Garmin MFA code required.", file=sys.stderr)
    return input("Enter code: ").strip()


def _get_credentials() -> tuple[str, str]:
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if email and password:
        return email, password
    email = email or input("Garmin Connect email: ").strip()
    password = password or getpass.getpass("Garmin Connect password: ")
    return email, password


def main() -> None:
    email, password = _get_credentials()
    tokenstore = token_store()

    print("Logging in to Garmin Connect...")
    try:
        client = Garmin(email=email, password=password, prompt_mfa=_prompt_mfa)
        client.login(tokenstore)
    except Exception as exc:  # noqa: BLE001 - surface any login failure to the user
        sys.exit(f"Login failed: {exc}")

    print(f"Login successful. Tokens saved to {tokenstore}")
    print("You can now run `garmin-mcp`, or add it to Claude Desktop / Claude Code / Cursor.")


if __name__ == "__main__":
    main()
