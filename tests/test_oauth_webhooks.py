"""Ping/Push intake, deregistration and permission changes, driven over HTTP.

Garmin's side is an ``httpx.MockTransport``: every outbound call the server
makes (Ping callbacks, token checks, permission reads, backfill) is answered
here, so the tests see exactly which Garmin endpoints were contacted.
"""
from __future__ import annotations

import json
import time

import httpx
import pytest
from starlette.testclient import TestClient

from garmin_mcp import multitenant, server
from garmin_mcp.oauth import client as client_module
from garmin_mcp.oauth import config as oauth_config
from garmin_mcp.oauth import webhooks
from garmin_mcp.oauth.app import build_oauth_app
from garmin_mcp.oauth.datastore import SummaryStore
from garmin_mcp.oauth.tokens import TokenBundle, TokenStore

USER_A = "a" * 40
USER_B = "b" * 40
BOTH = ["ACTIVITY_EXPORT", "HEALTH_EXPORT"]


class FakeGarmin:
    """Records outbound requests and answers them like the wellness API."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.callback_payload: list[dict] = []
        self.user_id_status = 200
        self.permissions = BOTH

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/user/id"):
            return httpx.Response(self.user_id_status, json={"userId": "whoever"})
        if path.endswith("/user/permissions"):
            return httpx.Response(200, json=self.permissions)
        if "/backfill/" in path:
            return httpx.Response(202)
        if path.endswith("/dailies"):
            return httpx.Response(200, json=self.callback_payload)
        return httpx.Response(404)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


@pytest.fixture
def garmin(monkeypatch):
    fake = FakeGarmin()
    real_client = httpx.Client
    monkeypatch.setattr(
        client_module.httpx,
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
    monkeypatch.delenv("GARMIN_OAUTH_WEBHOOK_SECRET", raising=False)
    yield oauth_config.load_oauth_config()
    server._oauth_clients.clear()
    multitenant._multi_tenant_active = False


def _register(config, user_id, garmin_id, permissions=BOTH):
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


def _post(app, payload, path="/garmin-oauth/webhooks/push"):
    # No `with`: webhook routes need no lifespan, and the MCP session manager
    # may only be started once per app.
    response = TestClient(app).post(path, content=json.dumps(payload))
    app.webhook_worker.wait()
    return response


def _daily(garmin_id, day="2026-09-20", steps=8000):
    return {"userId": garmin_id, "summaryId": f"d-{garmin_id}-{day}", "calendarDate": day, "steps": steps}


def test_push_lands_only_in_the_named_users_store(oauth_env, garmin):
    _register(oauth_env, USER_A, "garmin-a")
    _register(oauth_env, USER_B, "garmin-b")
    app = build_oauth_app(server.mcp, oauth_env)

    response = _post(app, {"dailies": [_daily("garmin-a"), _daily("garmin-unknown")]})

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    a = SummaryStore(oauth_env.token_root / USER_A).by_date("dailies", "2026-09-20")
    assert [d["steps"] for d in a] == [8000]
    assert SummaryStore(oauth_env.token_root / USER_B).by_date("dailies", "2026-09-20") == []
    # Push carries the data itself: no call to Garmin.
    assert garmin.requests == []
    # Processed spool files are removed.
    assert list((oauth_env.token_root / ".inbox").glob("*.json")) == []


def test_push_is_served_by_the_mcp_tool(oauth_env, garmin):
    _register(oauth_env, USER_A, "garmin-a")
    app = build_oauth_app(server.mcp, oauth_env)
    _post(app, {"dailies": [_daily("garmin-a", steps=12345)]})

    # Host must be the public one: the DNS-rebinding guard stays on.
    with TestClient(app, base_url="http://example.test") as http:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_daily_stats", "arguments": {"date": "2026-09-20"}},
        }
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        r = http.post(f"/garmin-oauth/{USER_A}/mcp", json=body, headers=headers)
    assert r.status_code == 200
    assert "12345" in r.text


def test_ping_follows_callback_with_the_users_token(oauth_env, garmin):
    _register(oauth_env, USER_A, "garmin-a")
    garmin.callback_payload = [_daily("garmin-a", steps=4321)]
    app = build_oauth_app(server.mcp, oauth_env)

    callback = "https://apis.garmin.com/wellness-api/rest/dailies?uploadStartTimeInSeconds=1&token=x"
    _post(app, {"dailies": [{"userId": "garmin-a", "callbackURL": callback}]}, "/garmin-oauth/webhooks/ping")

    assert len(garmin.requests) == 1
    assert garmin.requests[0].headers["Authorization"] == "Bearer access-garmin-a"
    stored = SummaryStore(oauth_env.token_root / USER_A).by_date("dailies", "2026-09-20")
    assert [d["steps"] for d in stored] == [4321]


def test_ping_to_a_foreign_host_is_not_followed(oauth_env, garmin):
    _register(oauth_env, USER_A, "garmin-a")
    app = build_oauth_app(server.mcp, oauth_env)

    _post(app, {"dailies": [{"userId": "garmin-a", "callbackURL": "https://evil.example/steal"}]})

    assert garmin.requests == []
    # Kept for inspection, not silently dropped.
    assert len(list((oauth_env.token_root / ".inbox" / "failed").glob("*.json"))) == 1


def test_push_without_permission_is_not_stored(oauth_env, garmin):
    _register(oauth_env, USER_A, "garmin-a", permissions=["ACTIVITY_EXPORT"])
    app = build_oauth_app(server.mcp, oauth_env)
    _post(app, {"dailies": [_daily("garmin-a")]})
    assert SummaryStore(oauth_env.token_root / USER_A).by_date("dailies", "2026-09-20") == []


def test_deregistration_deletes_user_once_garmin_rejects_tokens(oauth_env, garmin):
    store = _register(oauth_env, USER_A, "garmin-a")
    _register(oauth_env, USER_B, "garmin-b")
    app = build_oauth_app(server.mcp, oauth_env)
    _post(app, {"dailies": [_daily("garmin-a")]})

    garmin.user_id_status = 401
    _post(app, {"deregistrations": [{"userId": "garmin-a"}]}, "/garmin-oauth/webhooks/ping")

    assert not (oauth_env.token_root / USER_A).exists()
    assert store.lookup_by_garmin_user_id("garmin-a") is None
    assert store.resolve_existing(USER_B) is not None
    assert TestClient(app).post(f"/garmin-oauth/{USER_A}/mcp", json={}).status_code == 404


def test_forged_deregistration_is_ignored(oauth_env, garmin):
    store = _register(oauth_env, USER_A, "garmin-a")
    app = build_oauth_app(server.mcp, oauth_env)

    garmin.user_id_status = 200  # Garmin still honours the tokens
    _post(app, {"deregistrations": [{"userId": "garmin-a"}]})

    assert store.resolve_existing(USER_A) is not None


def test_permission_change_uses_garmins_answer_and_purges_withdrawn_data(oauth_env, garmin):
    store = _register(oauth_env, USER_A, "garmin-a")
    app = build_oauth_app(server.mcp, oauth_env)
    run = {"userId": "garmin-a", "summaryId": "r1", "activityId": 7, "startTimeInSeconds": 1_758_000_000}
    _post(app, {"dailies": [_daily("garmin-a")], "activities": [run]})

    garmin.permissions = ["ACTIVITY_EXPORT"]
    # The payload claims nothing was withdrawn; Garmin's own answer wins.
    _post(app, {"userPermissionsChange": [{"userId": "garmin-a", "permissions": BOTH}]})

    data = SummaryStore(oauth_env.token_root / USER_A)
    assert data.by_date("dailies", "2026-09-20") == []
    assert data.by_activity_id("activities", "7") is not None
    assert store.load_tokens(USER_A).permissions == ["ACTIVITY_EXPORT"]


def test_oversized_body_is_refused(oauth_env, garmin, monkeypatch):
    monkeypatch.setattr("garmin_mcp.oauth.app.MAX_BODY_BYTES", 10)
    app = build_oauth_app(server.mcp, oauth_env)
    response = _post(app, {"dailies": [_daily("garmin-a")]})
    assert response.status_code == 413
    assert list((oauth_env.token_root / ".inbox").iterdir()) == [oauth_env.token_root / ".inbox" / "failed"]


def test_webhook_secret_hides_the_bare_paths(oauth_env, garmin, monkeypatch):
    secret = "s" * 40
    monkeypatch.setenv("GARMIN_OAUTH_WEBHOOK_SECRET", secret)
    config = oauth_config.load_oauth_config()
    _register(config, USER_A, "garmin-a")
    app = build_oauth_app(server.mcp, config)

    assert _post(app, {"dailies": [_daily("garmin-a")]}).status_code == 404
    assert _post(app, {"dailies": [_daily("garmin-a")]}, f"/garmin-oauth/webhooks/{secret}/push").status_code == 200
    assert len(SummaryStore(config.token_root / USER_A).by_date("dailies", "2026-09-20")) == 1


def test_short_webhook_secret_is_rejected(oauth_env, monkeypatch):
    monkeypatch.setenv("GARMIN_OAUTH_WEBHOOK_SECRET", "short")
    with pytest.raises(RuntimeError, match="at least 32"):
        oauth_config.load_oauth_config()


def test_spooled_delivery_is_processed_after_restart(oauth_env, garmin):
    _register(oauth_env, USER_A, "garmin-a")
    inbox = webhooks.WebhookInbox(oauth_env.token_root)
    partial = inbox.new_partial()
    partial.write_text(json.dumps({"dailies": [_daily("garmin-a")]}))
    inbox.commit(partial)
    leftover = inbox.new_partial()  # an interrupted upload never counts
    leftover.write_text("{")

    app = build_oauth_app(server.mcp, oauth_env)
    app.webhook_worker.wait()

    assert len(SummaryStore(oauth_env.token_root / USER_A).by_date("dailies", "2026-09-20")) == 1
    assert not leftover.exists()


def test_initial_backfill_only_requests_permitted_types(oauth_env, garmin):
    store = _register(oauth_env, USER_A, "garmin-a", permissions=["ACTIVITY_EXPORT"])
    client = client_module.OfficialGarminClient(oauth_env, store, USER_A)
    webhooks.request_initial_backfill(client, ["ACTIVITY_EXPORT"])
    assert garmin.paths() == ["/wellness-api/rest/backfill/activities"]
    params = garmin.requests[0].url.params
    span = int(params["summaryEndTimeInSeconds"]) - int(params["summaryStartTimeInSeconds"])
    assert span == webhooks.BACKFILL_DAYS * 24 * 60 * 60
