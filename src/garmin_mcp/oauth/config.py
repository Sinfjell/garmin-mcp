"""Env-based configuration for the official OAuth path.

Secrets never live in code. All values come from the environment (see
``.env.example``). File paths for tokens assume EU-local disk on the host —
there is no US-region cloud dependency.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

AUTH_MODE_ENV = "GARMIN_AUTH_MODE"
AUTH_MODE_SESSION = "session"
AUTH_MODE_OAUTH = "oauth"

CLIENT_ID_ENV = "GARMIN_OAUTH_CLIENT_ID"
CLIENT_SECRET_ENV = "GARMIN_OAUTH_CLIENT_SECRET"
REDIRECT_URI_ENV = "GARMIN_OAUTH_REDIRECT_URI"
TOKEN_ROOT_ENV = "GARMIN_OAUTH_TOKEN_ROOT"
PUBLIC_BASE_URL_ENV = "GARMIN_OAUTH_PUBLIC_BASE_URL"
PATH_PREFIX_ENV = "GARMIN_OAUTH_PATH_PREFIX"
# Optional comma-separated Host header values for MCP DNS-rebinding protection
# (in addition to the hostname from GARMIN_OAUTH_PUBLIC_BASE_URL + localhost).
ALLOWED_HOSTS_ENV = "GARMIN_OAUTH_ALLOWED_HOSTS"

# FastMCP auto-enables DNS rebinding protection when bound to localhost and
# only allows these Host patterns unless we widen the list for a public proxy.
_LOCALHOST_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")
_LOCALHOST_ORIGINS = ("http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*")

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
    allowed_hosts: tuple[str, ...]

    @property
    def wellness_base(self) -> str:
        return f"{API_BASE_URL.rstrip('/')}/{WELLNESS_API_PATH}"


def _hostname_from_public_base(public_base_url: str) -> str:
    """Extract the Host-header hostname from a public origin URL."""
    parsed = urlparse(public_base_url)
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise RuntimeError(
            f"{PUBLIC_BASE_URL_ENV} must include a hostname "
            f"(e.g. https://mcp.productivitytech.io), got {public_base_url!r}"
        )
    return host


def _parse_extra_allowed_hosts(raw: str) -> list[str]:
    """Split ``GARMIN_OAUTH_ALLOWED_HOSTS`` into Host patterns (no empties)."""
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def resolve_allowed_hosts(public_base_url: str, extra_hosts_raw: str | None = None) -> tuple[str, ...]:
    """Host patterns FastMCP DNS-rebinding protection should accept in oauth mode.

    Always includes localhost patterns (so a direct curl to the bind still
    works) plus the hostname from ``GARMIN_OAUTH_PUBLIC_BASE_URL``. Optional
    ``GARMIN_OAUTH_ALLOWED_HOSTS`` adds more (e.g. ``www.productivitytech.io``).

    Each public hostname is listed both bare and with a ``:*`` port wildcard
    so nginx ``Host: example.com`` and ``Host: example.com:443`` both pass.
    """
    hosts: list[str] = list(_LOCALHOST_HOSTS)
    public_host = _hostname_from_public_base(public_base_url)
    for host in (public_host, *(_parse_extra_allowed_hosts(extra_hosts_raw or ""))):
        if host not in hosts:
            hosts.append(host)
        wildcard = f"{host}:*"
        if wildcard not in hosts:
            hosts.append(wildcard)
    return tuple(hosts)


def resolve_allowed_origins(public_base_url: str, allowed_hosts: tuple[str, ...]) -> tuple[str, ...]:
    """Origin patterns matching the public base URL scheme + allowed hosts."""
    scheme = urlparse(public_base_url).scheme or "https"
    origins: list[str] = list(_LOCALHOST_ORIGINS)
    for host in allowed_hosts:
        if host in _LOCALHOST_HOSTS or host.endswith(":*") and host[:-2] in ("127.0.0.1", "localhost", "[::1]"):
            continue
        bare = host.removesuffix(":*")
        for pattern in (f"{scheme}://{bare}", f"{scheme}://{bare}:*"):
            if pattern not in origins:
                origins.append(pattern)
    return tuple(origins)


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
            f"OAuth mode requires {PUBLIC_BASE_URL_ENV} (e.g. https://mcp.productivitytech.io) "
            "so post-consent connector URLs can be built."
        )

    prefix = (os.environ.get(PATH_PREFIX_ENV) or DEFAULT_PATH_PREFIX).strip() or DEFAULT_PATH_PREFIX
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    prefix = "/" + prefix.strip("/")

    allowed_hosts = resolve_allowed_hosts(
        public_base,
        os.environ.get(ALLOWED_HOSTS_ENV),
    )

    return OAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        token_root=token_root,
        public_base_url=public_base,
        path_prefix=prefix,
        allowed_hosts=allowed_hosts,
    )
