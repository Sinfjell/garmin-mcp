"""Multi-tenant mode: one hosted server, one token store per path-ID.

Single-tenant (the default) keeps one Garmin session for whoever owns the
host. Multi-tenant serves several people from one process: the URL carries an
unguessable user ID, and that ID selects which token store — and therefore
which Garmin account — the request may read.

Enabled by setting ``GARMIN_MULTI_TENANT_ROOT`` to a directory whose
subdirectories are per-user token stores::

    <root>/<user-id>/  ->  the same layout garminconnect writes to ~/.garminconnect

The endpoint URL is ``<prefix>/<user-id>/mcp``. Auth is possession-of-URL, as
in single-tenant hosting: the ID *is* the secret, so it must be long and
random (see USER_ID_PATTERN). An ID that fails validation, or one with no
token store on disk, is rejected with 404 — it never falls back to another
user's tokens, and never to the ``GARMIN_EMAIL``/``GARMIN_PASSWORD`` env
credentials.
"""
import json
import os
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any

MULTI_TENANT_ROOT_ENV = "GARMIN_MULTI_TENANT_ROOT"

# Possession of the ID is the entire authentication story, so it has to carry
# real entropy: 32+ chars of [a-z0-9-] is >= 128 bits when generated randomly.
# The character class also rules out path traversal ("..", "/") by construction,
# rather than relying on a separate sanitising step.
USER_ID_PATTERN = re.compile(r"^[a-z0-9-]{32,128}$")

# The token store for the request currently being served. Unset (None) means
# single-tenant: the process-wide token store is used instead.
#
# This binding reaches the tool call because anyio copies the current context
# when the MCP session task is spawned from the request task. That is load-
# bearing, so it is pinned by a test that drives two tenants over real HTTP
# (tests/test_multitenant.py) rather than assumed. If the assumption ever
# breaks, `multi_tenant_active` below makes it fail loudly instead of quietly
# serving the host's account.
_current_token_store: ContextVar[str | None] = ContextVar("garmin_token_store", default=None)

# Set once the process is serving multi-tenant traffic. Nothing may then be
# answered from the host's own token store or env credentials.
_multi_tenant_active = False


def activate_multi_tenant() -> None:
    """Mark this process as multi-tenant: no request may fall back to the host."""
    global _multi_tenant_active
    _multi_tenant_active = True


def multi_tenant_active() -> bool:
    return _multi_tenant_active


def multi_tenant_root() -> Path | None:
    """The configured multi-tenant root, or None when running single-tenant."""
    raw = os.environ.get(MULTI_TENANT_ROOT_ENV)
    return Path(raw) if raw else None


def is_valid_user_id(user_id: str) -> bool:
    """Whether a path segment is shaped like a user ID we will look up."""
    return bool(USER_ID_PATTERN.fullmatch(user_id))


def user_store_path(root: Path, user_id: str) -> Path:
    """The token-store directory a valid user ID maps to (may not exist)."""
    return root / user_id


def resolve_token_store(root: Path, user_id: str) -> Path | None:
    """Resolve a user ID to an existing token store, or None if it has none.

    Returns None for an ID that fails validation and for one whose store is
    missing — the caller cannot tell the two apart, which is deliberate: a
    probe learns nothing about which IDs exist.
    """
    if not is_valid_user_id(user_id):
        return None
    store = user_store_path(root, user_id)
    return store if store.is_dir() else None


def current_token_store() -> str | None:
    """Token store bound to the in-flight request, or None in single-tenant mode."""
    return _current_token_store.get()


def _not_found(message: str) -> bytes:
    return json.dumps({"error": message}).encode()


def build_multi_tenant_app(mcp: Any, root: Path, prefix: str = "/u") -> Any:
    """Wrap a FastMCP streamable-http app with per-user-ID token-store routing.

    Requests to ``<prefix>/<user-id>/mcp`` are rewritten to the inner app's own
    mount path with the user's token store bound for the duration of the
    request; everything else gets a 404.

    The inner app is put in stateless mode on purpose. Each request then gets
    its own transport and session, so a request can only ever read the token
    store bound by the URL it arrived on — there is no long-lived session whose
    identity could outlive the request that created it.
    """
    inner_path = "/mcp"
    activate_multi_tenant()
    mcp.settings.stateless_http = True
    mcp.settings.streamable_http_path = inner_path
    inner_app = mcp.streamable_http_app()

    prefix = "/" + prefix.strip("/") if prefix.strip("/") else ""

    async def app(scope: dict, receive: Any, send: Any) -> None:
        # Lifespan drives the inner session manager's task group; it must pass
        # through untouched or no request can be served at all.
        if scope["type"] != "http":
            await inner_app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        user_id, store = None, None
        if path.startswith(prefix + "/"):
            rest = path[len(prefix) + 1 :]
            head, _, tail = rest.partition("/")
            if "/" + tail == inner_path:
                user_id = head
        if user_id is not None:
            store = resolve_token_store(root, user_id)

        if store is None:
            await send({
                "type": "http.response.start",
                "status": 404,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({"type": "http.response.body", "body": _not_found("Unknown connector URL.")})
            return

        inner_scope = dict(scope)
        inner_scope["path"] = inner_path
        inner_scope["raw_path"] = inner_path.encode()
        token = _current_token_store.set(str(store))
        try:
            await inner_app(inner_scope, receive, send)
        finally:
            _current_token_store.reset(token)

    return app
