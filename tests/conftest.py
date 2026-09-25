"""Shared fixtures for the official-OAuth tests.

Garmin's side is an ``httpx.MockTransport``: every outbound call the server
makes (token exchange, Ping callbacks, token checks, permissions, backfill)
is answered by :class:`FakeGarmin`, so tests see exactly what was contacted.
"""
from __future__ import annotations

import time

import httpx
import pytest

from garmin_mcp import multitenant, server
from garmin_mcp.oauth import config as oauth_config
from garmin_mcp.oauth.tokens import TokenBundle, TokenStore

USER_A = "a" * 40
USER_B = "b" * 40
BOTH = ["ACTIVITY_EXPORT", "HEALTH_EXPORT"]
WEBHOOK_SECRET = "w" * 40
PUSH = f"/garmin-oauth/webhooks/{WEBHOOK_SECRET}/push"
PING = f"/garmin-oauth/webhooks/{WEBHOOK_SECRET}/ping"


class FakeGarmin:
    """Records outbound requests and answers them like the wellness API."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.callback_payload: list[dict] = []
        self.user_id_status = 200
        self.token_status = 200
        self.token_error = "server_error"
        self.callback_status = 200
        self.permissions = BOTH
        self.garmin_user_id = "garmin-new"
        self.token_forms: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/user/id"):
            return httpx.Response(self.user_id_status, json={"userId": self.garmin_user_id})
        if path.endswith("/user/permissions"):
            return httpx.Response(200, json=self.permissions)
        if "/backfill/" in path:
            return httpx.Response(202)
        if path.endswith("/dailies"):
            return httpx.Response(self.callback_status, json=self.callback_payload)
        if path.endswith("/oauth/token"):
            self.token_forms.append(dict(httpx.QueryParams(request.content.decode())))
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": self.token_error})
            return httpx.Response(200, json={
                "access_token": f"garmin-access-{len(self.token_forms)}",
                "refresh_token": "garmin-refresh",
                "expires_in": 86400,
            })
        return httpx.Response(404)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


@pytest.fixture
def garmin(monkeypatch):
    fake = FakeGarmin()
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(fake.handler), **kw),
    )
    return fake


@pytest.fixture
def oauth_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GARMIN_AUTH_MODE", "oauth")
    monkeypatch.setenv("GARMIN_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GARMIN_OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("GARMIN_OAUTH_REDIRECT_URI", "https://example.test/garmin-oauth/callback")
    monkeypatch.setenv("GARMIN_OAUTH_PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setenv("GARMIN_OAUTH_TOKEN_ROOT", str(tmp_path / "tokens"))
    monkeypatch.setenv("GARMIN_OAUTH_WEBHOOK_SECRET", WEBHOOK_SECRET)
    yield oauth_config.load_oauth_config()
    server._oauth_clients.clear()
    multitenant._multi_tenant_active = False


def register(config, user_id, garmin_id, permissions=BOTH):
    store = TokenStore(config.token_root)
    store.save_tokens(
        user_id,
        TokenBundle(
            access_token=f"access-{garmin_id}",
            refresh_token="refresh",
            expires_at=time.time() + 10_000,
            refresh_expires_at=None,
            garmin_user_id=garmin_id,
            permissions=permissions,
        ),
    )
    return store




def bearer_for(app, user_id: str) -> str:
    """Authorization header value for an MCP token issued to ``user_id``."""
    token = app.auth_provider._issue_tokens("test-client", user_id, [])
    return f"Bearer {token.access_token}"
