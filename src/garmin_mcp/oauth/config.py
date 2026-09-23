"""Env-based configuration for the official OAuth path.

Secrets never live in code. All values come from the environment (see
``.env.example``). File paths for tokens assume EU-local disk on the host —
there is no US-region cloud dependency.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

AUTH_MODE_ENV = "GARMIN_AUTH_MODE"
AUTH_MODE_SESSION = "session"
AUTH_MODE_OAUTH = "oauth"

CLIENT_ID_ENV = "GARMIN_OAUTH_CLIENT_ID"
CLIENT_SECRET_ENV = "GARMIN_OAUTH_CLIENT_SECRET"
REDIRECT_URI_ENV = "GARMIN_OAUTH_REDIRECT_URI"
TOKEN_ROOT_ENV = "GARMIN_OAUTH_TOKEN_ROOT"
PUBLIC_BASE_URL_ENV = "GARMIN_OAUTH_PUBLIC_BASE_URL"
PATH_PREFIX_ENV = "GARMIN_OAUTH_PATH_PREFIX"

# Public Garmin endpoints (not secrets).
OAUTH_AUTHORIZATION_URL = "https://connect.garmin.com/oauth2Confirm"
OAUTH_TOKEN_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"
API_BASE_URL = "https://apis.garmin.com"
WELLNESS_API_PATH = "wellness-api/rest"

DEFAULT_PATH_PREFIX = "/garmin-oauth"
DEFAULT_TOKEN_ROOT = Path.home() / ".garmin-oauth-tokens"

# Refresh this many seconds before access-token expiry (Garmin guidance: ≥600).
TOKEN_REFRESH_LEEWAY_SECONDS = 600
MAX_PULL_WINDOW_SECONDS = 24 * 60 * 60


def auth_mode() -> str:
    """Return ``session`` (default) or ``oauth`` from ``GARMIN_AUTH_MODE``."""
    raw = (os.environ.get(AUTH_MODE_ENV) or AUTH_MODE_SESSION).strip().lower()
    if raw not in (AUTH_MODE_SESSION, AUTH_MODE_OAUTH):
        raise RuntimeError(
            f"{AUTH_MODE_ENV} must be '{AUTH_MODE_SESSION}' or '{AUTH_MODE_OAUTH}', got {raw!r}"
        )
    return raw


def is_oauth_mode() -> bool:
    return auth_mode() == AUTH_MODE_OAUTH


@dataclass(frozen=True)
class OAuthConfig:
    """Resolved OAuth settings for one process."""

    client_id: str
    client_secret: str
    redirect_uri: str
    token_root: Path
    public_base_url: str
    path_prefix: str

    @property
    def wellness_base(self) -> str:
        return f"{API_BASE_URL.rstrip('/')}/{WELLNESS_API_PATH}"


def load_oauth_config() -> OAuthConfig:
    """Load OAuth config from the environment; fail closed if secrets are missing."""
    client_id = (os.environ.get(CLIENT_ID_ENV) or "").strip()
    client_secret = (os.environ.get(CLIENT_SECRET_ENV) or "").strip()
    redirect_uri = (os.environ.get(REDIRECT_URI_ENV) or "").strip()
    if not client_id or not client_secret or not redirect_uri:
        raise RuntimeError(
            f"OAuth mode requires {CLIENT_ID_ENV}, {CLIENT_SECRET_ENV}, and "
            f"{REDIRECT_URI_ENV} to be set (never commit these values)."
        )

    token_root_raw = (os.environ.get(TOKEN_ROOT_ENV) or "").strip()
    token_root = Path(token_root_raw) if token_root_raw else DEFAULT_TOKEN_ROOT

    public_base = (os.environ.get(PUBLIC_BASE_URL_ENV) or "").strip().rstrip("/")
    if not public_base:
        raise RuntimeError(
            f"OAuth mode requires {PUBLIC_BASE_URL_ENV} (e.g. https://productivitytech.io) "
            "so post-consent connector URLs can be built."
        )

    prefix = (os.environ.get(PATH_PREFIX_ENV) or DEFAULT_PATH_PREFIX).strip() or DEFAULT_PATH_PREFIX
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    prefix = "/" + prefix.strip("/")

    return OAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        token_root=token_root,
        public_base_url=public_base,
        path_prefix=prefix,
    )
