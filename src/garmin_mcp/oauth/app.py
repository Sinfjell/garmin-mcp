"""HTTP surface for OAuth authorize/callback, webhooks, and MCP routing.

Mounted when ``GARMIN_AUTH_MODE=oauth`` and ``--transport streamable-http``.
Does not touch the live unofficial units on other ports/paths.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from mcp.server.transport_security import TransportSecuritySettings

from garmin_mcp import multitenant
from garmin_mcp.oauth.client import OfficialGarminClient
from garmin_mcp.oauth.config import OAuthConfig, resolve_allowed_origins
from garmin_mcp.oauth.errors import StateMismatchError
from garmin_mcp.oauth.flow import build_authorization_url, exchange_code
from garmin_mcp.oauth.tokens import TokenStore
from garmin_mcp.oauth.webhooks import (
    MAX_BODY_BYTES,
    NotificationProcessor,
    WebhookInbox,
    WebhookWorker,
    request_initial_backfill,
)

log = logging.getLogger(__name__)


def _apply_oauth_transport_security(mcp: Any, config: OAuthConfig) -> None:
    """Widen FastMCP DNS-rebinding Host allowlist for the public reverse-proxy Host.

    Binding to 127.0.0.1 makes FastMCP auto-allow only localhost Host headers.
    Nginx forwards ``Host: mcp.productivitytech.io``, which then returns 421 unless
    the public hostname (from ``GARMIN_OAUTH_PUBLIC_BASE_URL`` / optional
    ``GARMIN_OAUTH_ALLOWED_HOSTS``) is listed. Protection stays enabled — only
    the allowlist grows. Localhost patterns remain so direct bind curls work.
    """
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(config.allowed_hosts),
        allowed_origins=list(resolve_allowed_origins(config.public_base_url, config.allowed_hosts)),
    )
    mcp.settings.transport_security = security
    # streamable_http_app() lazily creates a session manager that captures
    # security_settings once and whose .run() may only start once. Drop any
    # prior manager (e.g. leftover from another test or a previous bind) so
    # oauth boots with the widened allowlist and a fresh lifespan.
    mcp._session_manager = None


def _json_response(send: Any, status: int, body: dict) -> Any:
    payload = json.dumps(body).encode()

    async def _send_all() -> None:
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": payload})

    return _send_all()


def _html_response(send: Any, status: int, html: str) -> Any:
    payload = html.encode()

    async def _send_all() -> None:
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/html; charset=utf-8")],
        })
        await send({"type": "http.response.body", "body": payload})

    return _send_all()


def _success_html(mcp_url: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Garmin connected</title></head>"
        "<body><h1>Connected</h1>"
        "<p>Your personal MCP connector URL (treat it as a password):</p>"
        f"<p><code>{mcp_url}</code></p>"
        "<p>Garmin delivers your recent history over the next minutes, and new data "
        "each time your device syncs.</p>"
        "</body></html>"
    )


def _webhook_paths(config: OAuthConfig) -> tuple[str, ...]:
    """Ping and Push URLs; behind a secret segment when one is configured."""
    base = f"{config.path_prefix}/webhooks"
    if config.webhook_secret:
        base = f"{base}/{config.webhook_secret}"
    return (f"{base}/ping", f"{base}/push")


async def _spool_body(receive: Any, inbox: WebhookInbox) -> Path | None:
    """Stream the request body to a spool file. None when it exceeds the cap."""
    partial = inbox.new_partial()
    total = 0
    with partial.open("wb") as fh:
        os.chmod(partial, 0o600)
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                fh.close()
                partial.unlink(missing_ok=True)
                return None
            fh.write(chunk)
            if not message.get("more_body"):
                break
    return inbox.commit(partial)


async def _handle_webhook(receive: Any, send: Any, worker: WebhookWorker) -> None:
    path = await _spool_body(receive, worker.inbox)
    if path is None:
        await _json_response(send, 413, {"error": "Payload too large."})
        return
    # Acknowledge first; Garmin wants 200 within 30 s and processing afterwards.
    await _json_response(send, 200, {"status": "accepted"})
    worker.submit(path)


async def _handle_callback(scope: dict, send: Any, config: OAuthConfig, store: TokenStore,
                           worker: WebhookWorker) -> None:
    qs = parse_qs(scope.get("query_string", b"").decode())
    code = (qs.get("code") or [None])[0]
    state = (qs.get("state") or [None])[0]
    if not code or not state:
        await _html_response(send, 400, "<h1>Missing code or state</h1>")
        return
    try:
        user_id, bundle = exchange_code(config, store, code=code, state=state)
    except StateMismatchError:
        await _html_response(send, 400, "<h1>Invalid or expired OAuth state</h1>")
        return
    except Exception as exc:  # noqa: BLE001 - log the type only, never the message
        log.error("oauth callback failed: %s", type(exc).__name__)
        await _html_response(send, 502, "<h1>Connecting to Garmin failed</h1>")
        return

    def backfill() -> None:
        client = OfficialGarminClient(config, store, user_id)
        try:
            request_initial_backfill(client, bundle.permissions)
        finally:
            client.close()

    worker.submit_task(backfill)
    await _html_response(send, 200, _success_html(f"{config.public_base_url}{config.path_prefix}/{user_id}/mcp"))


def build_oauth_app(
    mcp: Any,
    config: OAuthConfig,
    *,
    on_user_deleted: Callable[[str], None] | None = None,
) -> Any:
    """ASGI app: authorize, callback, Ping/Push intake, and per-user MCP."""
    store = TokenStore(config.token_root)
    store.ensure_root()
    prefix = config.path_prefix
    worker = WebhookWorker(
        WebhookInbox(config.token_root),
        NotificationProcessor(config, store, on_user_deleted=on_user_deleted),
    )
    worker.drain_on_start()
    webhook_paths = _webhook_paths(config)

    multitenant.activate_multi_tenant()
    mcp.settings.stateless_http = True
    mcp.settings.streamable_http_path = "/mcp"
    _apply_oauth_transport_security(mcp, config)
    inner_app = mcp.streamable_http_app()

    async def app(scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await inner_app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        method: str = scope.get("method", "GET").upper()

        if path == f"{prefix}/authorize" and method == "GET":
            await _handle_authorize(send, config, store)
        elif path == f"{prefix}/callback" and method == "GET":
            await _handle_callback(scope, send, config, store, worker)
        elif path in webhook_paths and method == "POST":
            await _handle_webhook(receive, send, worker)
        else:
            await _route_mcp(scope, receive, send, inner_app, store, prefix)

    app.webhook_worker = worker  # type: ignore[attr-defined]  # tests wait on it
    return app


async def _handle_authorize(send: Any, config: OAuthConfig, store: TokenStore) -> None:
    try:
        url, _pair = build_authorization_url(config, store)
    except Exception as exc:  # noqa: BLE001 - log type only, never the message
        log.error("authorize failed: %s", type(exc).__name__)
        await _html_response(send, 500, "<h1>Authorize failed</h1>")
        return
    await send({"type": "http.response.start", "status": 302, "headers": [(b"location", url.encode())]})
    await send({"type": "http.response.body", "body": b""})


async def _route_mcp(scope: dict, receive: Any, send: Any, inner_app: Any, store: TokenStore, prefix: str) -> None:
    """Serve ``<prefix>/<user-id>/mcp`` from that user's token store only."""
    path: str = scope.get("path", "")
    user_id, token_dir = None, None
    if path.startswith(prefix + "/"):
        head, _, tail = path[len(prefix) + 1 :].partition("/")
        if "/" + tail == "/mcp":
            user_id = head
            token_dir = store.resolve_existing(user_id) if user_id else None
    if user_id is None:
        await _json_response(send, 404, {"error": "Not found."})
        return
    if token_dir is None:
        await _json_response(send, 404, {"error": "Unknown connector URL."})
        return
    inner_scope = dict(scope)
    inner_scope["path"] = "/mcp"
    inner_scope["raw_path"] = b"/mcp"
    token = multitenant._current_token_store.set(str(token_dir))
    try:
        await inner_app(inner_scope, receive, send)
    finally:
        multitenant._current_token_store.reset(token)
