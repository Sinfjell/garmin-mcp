"""Unit tests for official OAuth PKCE, token store, refresh, and tool gating.

No real credentials or network calls — httpx is mocked.
"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

from garmin_mcp import multitenant, server
from garmin_mcp.oauth import config as oauth_config
from garmin_mcp.oauth.client import OfficialGarminClient
from garmin_mcp.oauth.errors import OfficialApiUnavailableError, StateMismatchError, TokenExchangeError
from garmin_mcp.oauth.flow import build_authorization_url, exchange_code, refresh_tokens
from garmin_mcp.oauth.pkce import code_challenge_s256, generate_code_verifier, new_pkce_pair
from garmin_mcp.oauth.tokens import PendingAuth, TokenBundle, TokenStore


@pytest.fixture
def oauth_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GARMIN_AUTH_MODE", "oauth")
    monkeypatch.setenv("GARMIN_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GARMIN_OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("GARMIN_OAUTH_REDIRECT_URI", "https://example.test/garmin-oauth/callback")
    monkeypatch.setenv("GARMIN_OAUTH_PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setenv("GARMIN_OAUTH_TOKEN_ROOT", str(tmp_path / "tokens"))
    monkeypatch.setenv("GARMIN_OAUTH_PATH_PREFIX", "/garmin-oauth")
    return oauth_config.load_oauth_config()


@pytest.fixture(autouse=True)
def _reset_server_state():
    server._client = None
    server._tenant_clients.clear()
    server._oauth_clients.clear()
    multitenant._multi_tenant_active = False
    yield
    server._client = None
    server._tenant_clients.clear()
    server._oauth_clients.clear()
    multitenant._multi_tenant_active = False


# --- PKCE ----------------------------------------------------------------


def test_code_challenge_s256_is_unpadded_base64url():
    # RFC 7636 appendix B example
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert code_challenge_s256(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_new_pkce_pair_lengths():
    pair = new_pkce_pair()
    assert 43 <= len(pair.code_verifier) <= 128
    assert pair.code_challenge == code_challenge_s256(pair.code_verifier)
    assert len(pair.state) >= 16
    assert generate_code_verifier() != generate_code_verifier()


# --- Token store ---------------------------------------------------------


def test_token_store_round_trip(tmp_path):
    store = TokenStore(tmp_path)
    user_id = "a" * 40
    bundle = TokenBundle(
        access_token="access",
        refresh_token="refresh",
        expires_at=time.time() + 3600,
        refresh_expires_at=time.time() + 86400,
        garmin_user_id="g-user-1",
        permissions=["HEALTH_EXPORT", "ACTIVITY_EXPORT"],
    )
    store.save_tokens(user_id, bundle)
    loaded = store.load_tokens(user_id)
    assert loaded.access_token == "access"
    assert loaded.garmin_user_id == "g-user-1"
    assert store.lookup_by_garmin_user_id("g-user-1") == user_id
    assert store.resolve_existing(user_id) is not None
    assert store.resolve_existing("b" * 40) is None


def test_pending_state_take_once(tmp_path):
    store = TokenStore(tmp_path)
    store.save_pending(PendingAuth(state="abc", code_verifier="verifier", created_at=time.time()))
    first = store.take_pending("abc")
    assert first is not None and first.code_verifier == "verifier"
    assert store.take_pending("abc") is None


def test_pending_state_expires(tmp_path):
    store = TokenStore(tmp_path)
    store.save_pending(PendingAuth(state="old", code_verifier="v", created_at=time.time() - 10_000))
    assert store.take_pending("old") is None


def test_access_expired_respects_leeway():
    bundle = TokenBundle(
        access_token="a",
        refresh_token="r",
        expires_at=time.time() + 100,
        refresh_expires_at=None,
        garmin_user_id="",
    )
    assert bundle.access_expired(leeway=600) is True
    assert bundle.access_expired(leeway=50) is False


# --- Authorize / exchange / refresh (mocked httpx) -----------------------


def test_build_authorization_url_persists_verifier(oauth_env):
    store = TokenStore(oauth_env.token_root)
    url, pair = build_authorization_url(oauth_env, store)
    assert "response_type=code" in url
    assert "code_challenge_method=S256" in url
    assert oauth_env.client_id in url
    pending = store.take_pending(pair.state)
    assert pending is not None
    assert pending.code_verifier == pair.code_verifier


def test_exchange_code_persists_tokens(oauth_env):
    store = TokenStore(oauth_env.token_root)
    url, pair = build_authorization_url(oauth_env, store)
    assert "oauth2Confirm" in url

    http = MagicMock()
    token_response = MagicMock()
    token_response.status_code = 200
    token_response.json.return_value = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "expires_in": 3600,
        "refresh_token_expires_in": 7776000,
        "scope": "CONNECT_READ",
        "token_type": "bearer",
    }
    user_response = MagicMock()
    user_response.status_code = 200
    user_response.json.return_value = {"userId": "garmin-abc"}
    perm_response = MagicMock()
    perm_response.status_code = 200
    perm_response.json.return_value = ["HEALTH_EXPORT", "ACTIVITY_EXPORT"]
    http.post.return_value = token_response
    http.get.side_effect = [user_response, perm_response]

    user_id, bundle = exchange_code(
        oauth_env, store, code="auth-code", state=pair.state, http=http
    )
    assert len(user_id) >= 32
    assert bundle.access_token == "new-access"
    assert bundle.garmin_user_id == "garmin-abc"
    assert store.load_tokens(user_id).refresh_token == "new-refresh"
    # Verifier consumed
    assert store.take_pending(pair.state) is None


def test_exchange_rejects_bad_state(oauth_env):
    store = TokenStore(oauth_env.token_root)
    with pytest.raises(StateMismatchError):
        exchange_code(oauth_env, store, code="x", state="nope")


def test_token_exchange_error_hides_body(oauth_env):
    store = TokenStore(oauth_env.token_root)
    _, pair = build_authorization_url(oauth_env, store)
    http = MagicMock()
    bad = MagicMock()
    bad.status_code = 401
    bad.text = "should-not-appear-in-exception"
    http.post.return_value = bad
    with pytest.raises(TokenExchangeError) as excinfo:
        exchange_code(oauth_env, store, code="x", state=pair.state, http=http)
    assert "should-not-appear" not in str(excinfo.value)
    assert excinfo.value.status_code == 401


def test_refresh_rotates_refresh_token(oauth_env):
    store = TokenStore(oauth_env.token_root)
    user_id = "c" * 40
    original = TokenBundle(
        access_token="old-a",
        refresh_token="old-r",
        expires_at=time.time() - 10,
        refresh_expires_at=None,
        garmin_user_id="g1",
    )
    store.save_tokens(user_id, original)

    http = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "access_token": "rotated-a",
        "refresh_token": "rotated-r",
        "expires_in": 3600,
        "refresh_token_expires_in": 1000,
    }
    http.post.return_value = resp

    updated = refresh_tokens(oauth_env, store, user_id, original, http=http, now=1_700_000_000.0)
    assert updated.access_token == "rotated-a"
    assert updated.refresh_token == "rotated-r"
    assert updated.expires_at == 1_700_000_000.0 + 3600
    assert store.load_tokens(user_id).refresh_token == "rotated-r"


# --- Official client + tool gating ---------------------------------------


def test_official_client_maps_dailies(oauth_env):
    store = TokenStore(oauth_env.token_root)
    user_id = "d" * 40
    store.save_tokens(
        user_id,
        TokenBundle(
            access_token="a",
            refresh_token="r",
            expires_at=time.time() + 10_000,
            refresh_expires_at=None,
            garmin_user_id="g",
        ),
    )
    http = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"[]"
    resp.json.return_value = [
        {
            "calendarDate": "2026-09-20",
            "steps": 8000,
            "activeKilocalories": 400,
            "bmrKilocalories": 1500,
            "restingHeartRateInBeatsPerMinute": 48,
            "averageStressLevel": 25,
            "floorsClimbed": 10,
            "minHeartRateInBeatsPerMinute": 42,
            "maxHeartRateInBeatsPerMinute": 160,
            "distanceInMeters": 6000,
        }
    ]
    http.request.return_value = resp

    client = OfficialGarminClient(oauth_env, store, user_id, http=http)
    stats = client.get_stats("2026-09-20")
    assert stats["totalSteps"] == 8000
    assert stats["totalKilocalories"] == 1900
    assert stats["restingHeartRate"] == 48


def test_official_client_lists_activities(oauth_env):
    store = TokenStore(oauth_env.token_root)
    user_id = "e" * 40
    store.save_tokens(
        user_id,
        TokenBundle(
            access_token="a",
            refresh_token="r",
            expires_at=time.time() + 10_000,
            refresh_expires_at=None,
            garmin_user_id="g",
        ),
    )
    http = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"[]"
    resp.json.return_value = [
        {
            "activityId": 99,
            "activityName": "Easy Run",
            "activityType": "RUNNING",
            "distanceInMeters": 5000,
            "durationInSeconds": 1500,
            "averageHeartRateInBeatsPerMinute": 140,
            "startTimeInSeconds": 1_700_000_000,
            "startTimeOffsetInSeconds": 7200,
        }
    ]
    http.request.return_value = resp
    client = OfficialGarminClient(oauth_env, store, user_id, http=http)
    activities = client.get_activities(0, 5)
    assert len(activities) == 1
    assert activities[0]["activityId"] == 99
    assert activities[0]["activityType"]["typeKey"] == "running"


def test_unavailable_metrics_raise(oauth_env):
    store = TokenStore(oauth_env.token_root)
    user_id = "f" * 40
    store.save_tokens(
        user_id,
        TokenBundle(
            access_token="a",
            refresh_token="r",
            expires_at=time.time() + 10_000,
            refresh_expires_at=None,
            garmin_user_id="g",
        ),
    )
    client = OfficialGarminClient(oauth_env, store, user_id, http=MagicMock())
    with pytest.raises(OfficialApiUnavailableError, match="lactate threshold"):
        client.get_lactate_threshold(latest=True)
    with pytest.raises(OfficialApiUnavailableError, match="personal records"):
        client.get_personal_record()
    with pytest.raises(OfficialApiUnavailableError, match="training status"):
        client.get_training_status("2026-09-20")
    with pytest.raises(OfficialApiUnavailableError, match="Body Battery"):
        client.get_body_battery("2026-09-01", "2026-09-07")


def test_tool_call_surfaces_official_unavailable(oauth_env, monkeypatch):
    class FakeUnavailable:
        def get_training_status(self, date):
            raise OfficialApiUnavailableError("training status")

    monkeypatch.setattr(server, "get_client", lambda: FakeUnavailable())
    out = json.loads(server.get_training_status("2026-09-20"))
    assert "not available via the official" in out["error"]
    assert out["auth_mode"] == "oauth"


def test_session_mode_still_default(monkeypatch):
    monkeypatch.delenv("GARMIN_AUTH_MODE", raising=False)
    assert oauth_config.auth_mode() == "session"
    assert oauth_config.is_oauth_mode() is False


def test_oauth_http_authorize_and_webhook_stubs(oauth_env):
    from starlette.testclient import TestClient

    from garmin_mcp.oauth.app import build_oauth_app

    app = build_oauth_app(server.mcp, oauth_env)
    client = TestClient(app)

    r = client.get("/garmin-oauth/authorize", follow_redirects=False)
    assert r.status_code == 302
    assert "connect.garmin.com/oauth2Confirm" in r.headers["location"]

    ping = client.post("/garmin-oauth/webhooks/ping", json={"dailies": []})
    assert ping.status_code == 200
    assert ping.json()["status"] == "accepted"

    push = client.post("/garmin-oauth/webhooks/push", json={"activities": []})
    assert push.status_code == 200

    unknown = client.get("/garmin-oauth/" + ("z" * 40) + "/mcp")
    assert unknown.status_code == 404
    assert unknown.json()["error"] == "Unknown connector URL."
