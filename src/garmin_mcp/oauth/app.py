"""HTTP surface for OAuth authorize/callback, webhooks, and MCP routing.

Mounted when ``GARMIN_AUTH_MODE=oauth`` and ``--transport streamable-http``.
Does not touch the live unofficial units on other ports/paths.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import parse_qs

from mcp.server.transport_security import TransportSecuritySettings

from garmin_mcp import multitenant
from garmin_mcp.oauth.config import OAuthConfig, resolve_allowed_origins
from garmin_mcp.oauth.errors import OAuthError, StateMismatchError, TokenExchangeError
from garmin_mcp.oauth.flow import build_authorization_url, exchange_code
from garmin_mcp.oauth.tokens import TokenStore

log = logging.getLogger(__name__)


def _apply_oauth_transport_security(mcp: Any, config: OAuthConfig) -> None:
    """Widen FastMCP DNS-rebinding Host allowlist for the public reverse-proxy Host.

    Binding to 127.0.0.1 makes FastMCP auto-allow only localhost Host headers.
    Nginx forwards ``Host: productivitytech.io``, which then returns 421 unless
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
        "<!doctype html><html><head><meta charset='utf-8'><title>Garmin OAuth</title></head>"
        "<body><h1>Connected</h1>"
        "<p>OAuth completed. Your personal MCP connector URL (treat as a credential):</p>"
        f"<p><code>{mcp_url}</code></p>"
        "<p>Privacy Policy outlining Garmin data use and third-party AI must be published "
        "separately before production (external gap — not part of this server).</p>"
        "</body></html>"
    )


def build_oauth_app(mcp: Any, config: OAuthConfig) -> Any:
    """ASGI app: authorize, callback, ping/push stubs, and per-user MCP."""
    store = TokenStore(config.token_root)
    store.ensure_root()
    prefix = config.path_prefix

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

        # --- OAuth authorize -------------------------------------------
        if path == f"{prefix}/authorize" and method == "GET":
            try:
                url, _pair = build_authorization_url(config, store)
            except Exception as exc:  # noqa: BLE001 - log type only, never the message
                log.error("authorize failed: %s", type(exc).__name__)
                await _html_response(send, 500, "<h1>Authorize failed</h1>")
                return
            await send({
                "type": "http.response.start",
                "status": 302,
                "headers": [(b"location", url.encode())],
            })
            await send({"type": "http.response.body", "body": b""})
            return

        # --- OAuth callback --------------------------------------------
        if path == f"{prefix}/callback" and method == "GET":
            qs = parse_qs(scope.get("query_string", b"").decode())
            code = (qs.get("code") or [None])[0]
            state = (qs.get("state") or [None])[0]
            if not code or not state:
                await _html_response(send, 400, "<h1>Missing code or state</h1>")
                return
            try:
                user_id, _bundle = exchange_code(config, store, code=code, state=state)
            except StateMismatchError:
                await _html_response(send, 400, "<h1>Invalid or expired OAuth state</h1>")
                return
            except TokenExchangeError as exc:
                log.error("token exchange failed: %s", type(exc).__name__)
                await _html_response(send, 502, "<h1>Token exchange failed</h1>")
                return
            except OAuthError as exc:
                log.error("oauth callback failed: %s", type(exc).__name__)
                await _html_response(send, 502, "<h1>OAuth failed</h1>")
                return
            except Exception as exc:  # noqa: BLE001 - log type only
                log.error("oauth callback failed: %s", type(exc).__name__)
                await _html_response(send, 500, "<h1>OAuth failed</h1>")
                return
            mcp_url = f"{config.public_base_url}{prefix}/{user_id}/mcp"
            await _html_response(send, 200, _success_html(mcp_url))
            return

        # --- Ping / Push webhook stubs (eval program requirement) ------
        if path in (f"{prefix}/webhooks/ping", f"{prefix}/webhooks/push") and method == "POST":
            # Read and discard body; return 200 quickly so Garmin does not disable the endpoint.
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    break
                if not message.get("more_body"):
                    break
            log.info("webhook stub accepted path=%s", path)
            await _json_response(
                send,
                200,
                {
                    "status": "accepted",
                    "note": (
                        "Stub: Ping/Push payloads are acknowledged but not ingested yet. "
                        "Configure these URLs in the Garmin developer portal for the eval app."
                    ),
                },
            )
            return

        # --- Per-user MCP ----------------------------------------------
        user_id, token_dir = None, None
        if path.startswith(prefix + "/"):
            rest = path[len(prefix) + 1 :]
            head, _, tail = rest.partition("/")
            if "/" + tail == "/mcp":
                user_id = head
                token_dir = store.resolve_existing(user_id) if user_id else None

        if user_id is not None:
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
            return

        await _json_response(send, 404, {"error": "Not found."})

    return app
