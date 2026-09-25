"""HTTP surface for OAuth authorize/callback, webhooks, and MCP routing.

Mounted when ``GARMIN_AUTH_MODE=oauth`` and ``--transport streamable-http``.
Does not touch the live unofficial units on other ports/paths.
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import anyio.to_thread
from mcp.server.auth.routes import build_resource_metadata_url, create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.routing import Mount, Route

from garmin_mcp import multitenant
from garmin_mcp.oauth.client import OfficialGarminClient
from garmin_mcp.oauth.config import OAuthConfig, resolve_allowed_origins
from garmin_mcp.oauth.flow import build_authorization_url, exchange_code
from garmin_mcp.oauth.mcp_auth import GarminAuthProvider, PendingRequest
from garmin_mcp.oauth.pages import EXPIRED, consent_page, message_page
from garmin_mcp.oauth.tokens import TokenStore
from garmin_mcp.oauth.webhooks import (
    MAX_BODY_BYTES,
    NotificationProcessor,
    WebhookInbox,
    WebhookWorker,
    request_initial_backfill,
)

log = logging.getLogger(__name__)

_CONSENT_COOKIE_PREFIX = "garmin_mcp_consent_"


def _consent_cookie(request_id: str) -> str:
    """One cookie per parked request, so two open consent tabs never clobber each other."""
    return _CONSENT_COOKIE_PREFIX + hashlib.sha256(request_id.encode()).hexdigest()[:16]


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


def _webhook_paths(config: OAuthConfig) -> tuple[str, ...]:
    """Ping and Push URLs, behind the secret segment (Garmin does not sign notifications)."""
    base = f"{config.path_prefix}/webhooks/{config.webhook_secret}"
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
                # Client went away mid-upload: never queue a truncated body.
                fh.close()
                partial.unlink(missing_ok=True)
                return None
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                fh.close()
                partial.unlink(missing_ok=True)
                return None
            # Off the event loop: a 100 MB Push must not delay other acknowledgements.
            await anyio.to_thread.run_sync(fh.write, chunk)
            if not message.get("more_body"):
                break
    return inbox.commit(partial)


async def _handle_webhook(receive: Any, send: Any, worker: WebhookWorker) -> None:
    path = await _spool_body(receive, worker.inbox)
    if path is None:
        # Too large, or the connection dropped (then nobody reads this anyway).
        await _json_response(send, 413, {"error": "Payload too large."})
        return
    # Acknowledge first; Garmin wants 200 within 30 s and processing afterwards.
    await _json_response(send, 200, {"status": "accepted"})
    worker.submit(path)


def _redirect(send: Any, location: str, headers: list[tuple[bytes, bytes]] | None = None) -> Any:
    async def _send_all() -> None:
        await send({
            "type": "http.response.start",
            "status": 302,
            "headers": [(b"location", location.encode()), (b"cache-control", b"no-store"), *(headers or [])],
        })
        await send({"type": "http.response.body", "body": b""})

    return _send_all()


def _query(scope: dict) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(scope.get("query_string", b"").decode()).items()}


def _cookie(scope: dict, name: str) -> str | None:
    for key, value in scope.get("headers", []):
        if key == b"cookie":
            for part in value.decode("latin-1").split(";"):
                k, _, v = part.strip().partition("=")
                if k == name:
                    return v
    return None


async def _read_form(receive: Any, limit: int = 16 * 1024) -> dict[str, str] | None:
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if len(body) > limit:
            return None
        if not message.get("more_body"):
            break
    return {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}


class OAuthRoutes:
    """Browser-facing steps of the MCP authorization: consent and Garmin's callback."""

    def __init__(self, config: OAuthConfig, store: TokenStore, provider: GarminAuthProvider,
                 worker: WebhookWorker):
        self.config = config
        self.store = store
        self.provider = provider
        self.worker = worker
        self.consent_path = f"{config.path_prefix}/consent"
        secure = "; Secure" if config.public_base_url.startswith("https://") else ""
        self._cookie_attrs = f"; Path={config.path_prefix}; HttpOnly; SameSite=Lax; Max-Age=900{secure}"

    async def consent(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("method", "GET").upper() == "POST":
            await self._consent_post(scope, receive, send)
            return
        pending = self.provider.load_request(_query(scope).get("request", ""))
        if pending is None:
            await _html_response(send, 400, message_page(*EXPIRED))
            return
        browser_token = self.provider.bind_browser(pending.request_id)
        html = await self._consent_html(pending)
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/html; charset=utf-8"),
                (b"cache-control", b"no-store"),
                (b"x-frame-options", b"DENY"),
                (b"set-cookie",
                 f"{_consent_cookie(pending.request_id)}={browser_token}{self._cookie_attrs}".encode()),
            ],
        })
        await send({"type": "http.response.body", "body": html.encode()})

    async def _consent_html(self, pending: PendingRequest, error: str | None = None) -> str:
        client = await self.provider.get_client(pending.client_id)
        return consent_page(
            client_name=(client.client_name if client and client.client_name else "your AI assistant"),
            redirect_uri=str(pending.params.redirect_uri),
            request_id=pending.request_id,
            action=self.consent_path,
            privacy_url=self.config.privacy_url,
            operator=self.config.operator,
            error=error,
        )

    async def _consent_post(self, scope: dict, receive: Any, send: Any) -> None:
        form = await _read_form(receive)
        pending = self.provider.load_request((form or {}).get("request", ""))
        # The cookie proves this browser loaded the consent page for this request.
        # Checked before approve *and* deny: a forged POST may not cancel either.
        if pending is None or not pending.same_browser(_cookie(scope, _consent_cookie(pending.request_id))):
            await _html_response(send, 400, message_page(*EXPIRED))
            return
        if form.get("decision") == "deny":
            self.provider.drop_request(pending.request_id)
            await _redirect(send, pending.redirect(error="access_denied"))
            return
        if form.get("consent") != "yes":
            await _html_response(send, 400, await self._consent_html(pending, "Tick the box to continue."))
            return
        url, pair = build_authorization_url(self.config, self.store)
        self.provider.attach_garmin_state(pending.request_id, pair.state)
        await _redirect(send, url)

    async def callback(self, scope: dict, send: Any) -> None:
        qs = _query(scope)
        pending = self.provider.take_request_by_garmin_state(qs.get("state", ""))
        if pending is None:
            await _html_response(send, 400, message_page(*EXPIRED))
            return
        if not qs.get("code"):
            await _redirect(send, pending.redirect(error="access_denied"))
            return
        try:
            # Three blocking Garmin calls: keep them off the event loop, which
            # also has to acknowledge webhooks within Garmin's 30 s.
            user_id, bundle = await anyio.to_thread.run_sync(
                functools.partial(exchange_code, self.config, self.store, code=qs["code"], state=qs["state"])
            )
        except Exception as exc:  # noqa: BLE001 - log the type only, never the message
            log.error("oauth callback failed: %s", type(exc).__name__)
            await _redirect(send, pending.redirect(error="server_error"))
            return
        self.worker.submit_task(lambda: _backfill(self.config, self.store, user_id, bundle.permissions))
        await _redirect(send, pending.redirect(code=self.provider.issue_code(pending, user_id)))


def _backfill(config: OAuthConfig, store: TokenStore, user_id: str, permissions: list[str] | None) -> None:
    client = OfficialGarminClient(config, store, user_id)
    try:
        request_initial_backfill(client, permissions)
    finally:
        client.close()


def _auth_router(config: OAuthConfig, provider: GarminAuthProvider) -> Starlette:
    """OAuth metadata, dynamic registration, authorize, token and revoke endpoints."""
    issuer = AnyHttpUrl(config.public_base_url + config.path_prefix)
    auth_routes = create_auth_routes(
        provider,
        issuer_url=issuer,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )
    metadata = next(r for r in auth_routes if r.path == "/.well-known/oauth-authorization-server")
    routes = [
        Mount(config.path_prefix, routes=auth_routes),
        # RFC 8414 §3: path-bearing issuers publish metadata at /.well-known/…/<path>.
        Route(f"/.well-known/oauth-authorization-server{config.path_prefix}", metadata.endpoint,
              methods=["GET", "OPTIONS"]),
        *create_protected_resource_routes(
            resource_url=AnyHttpUrl(_resource_url(config)),
            authorization_servers=[issuer],
            resource_name="Garmin MCP",
        ),
    ]
    return Starlette(routes=routes)


def _resource_url(config: OAuthConfig) -> str:
    return f"{config.public_base_url}{config.path_prefix}/mcp"


def _start_worker(config: OAuthConfig, store: TokenStore, provider: GarminAuthProvider,
                  on_user_deleted: Callable[[str], None] | None) -> WebhookWorker:
    inbox = WebhookInbox(config.token_root)

    def user_deleted(user_id: str, garmin_user_id: str) -> None:
        provider.revoke_user(user_id)
        inbox.scrub_garmin_user(garmin_user_id)
        if on_user_deleted is not None:
            on_user_deleted(user_id)

    worker = WebhookWorker(inbox, NotificationProcessor(config, store, on_user_deleted=user_deleted))
    worker.drain_on_start()
    return worker


def _mcp_app(mcp: Any, config: OAuthConfig) -> Any:
    """The FastMCP streamable-HTTP app, stateless, served per request for one bound store."""
    multitenant.activate_multi_tenant()
    mcp.settings.stateless_http = True
    mcp.settings.streamable_http_path = "/mcp"
    _apply_oauth_transport_security(mcp, config)
    return mcp.streamable_http_app()


def build_oauth_app(
    mcp: Any,
    config: OAuthConfig,
    *,
    on_user_deleted: Callable[[str], None] | None = None,
) -> Any:
    """ASGI app: MCP authorization, consent, Garmin callback, Ping/Push, and MCP."""
    store = TokenStore(config.token_root)
    store.ensure_root()
    prefix = config.path_prefix
    provider = GarminAuthProvider(config.token_root, f"{config.public_base_url}{prefix}/consent", store)
    worker = _start_worker(config, store, provider, on_user_deleted)
    webhook_paths = _webhook_paths(config)
    routes = OAuthRoutes(config, store, provider, worker)
    auth_router = _auth_router(config, provider)
    inner_app = _mcp_app(mcp, config)

    async def app(scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await inner_app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        method: str = scope.get("method", "GET").upper()
        if path == routes.consent_path:
            await routes.consent(scope, receive, send)
        elif path == f"{prefix}/callback" and method == "GET":
            await routes.callback(scope, send)
        elif path in webhook_paths and method == "POST":
            await _handle_webhook(receive, send, worker)
        elif path == f"{prefix}/mcp":
            await _serve_mcp(scope, receive, send, inner_app, store, provider, config)
        else:
            await auth_router(scope, receive, send)

    app.webhook_worker = worker  # type: ignore[attr-defined]  # tests wait on it
    app.auth_provider = provider  # type: ignore[attr-defined]
    return app


async def _serve_mcp(scope: dict, receive: Any, send: Any, inner_app: Any, store: TokenStore,
                     provider: GarminAuthProvider, config: OAuthConfig) -> None:
    """MCP for the user the bearer token belongs to — and only that user's store."""
    header = dict(scope.get("headers", [])).get(b"authorization", b"").decode("latin-1")
    scheme, _, token = header.partition(" ")
    access = None
    if scheme.lower() == "bearer":
        # SQLite lookup (with a busy timeout) runs in a thread, not on the event loop.
        access = await anyio.to_thread.run_sync(provider.lookup_access_token, token.strip())
    token_dir = store.resolve_existing(str(access.subject)) if access and access.subject else None
    if token_dir is None:
        metadata_url = build_resource_metadata_url(AnyHttpUrl(_resource_url(config)))
        challenge = f'Bearer error="invalid_token", resource_metadata="{metadata_url}"'
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", challenge.encode())],
        })
        await send({"type": "http.response.body", "body": b'{"error": "invalid_token"}'})
        return
    inner_scope = dict(scope)
    inner_scope["path"] = "/mcp"
    inner_scope["raw_path"] = b"/mcp"
    bound = multitenant._current_token_store.set(str(token_dir))
    try:
        await inner_app(inner_scope, receive, send)
    finally:
        multitenant._current_token_store.reset(bound)
