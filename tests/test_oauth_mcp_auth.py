"""The MCP authorization flow end to end, the way claude.ai or ChatGPT runs it.

Discovery → dynamic client registration → authorize → our consent page →
Garmin consent (faked) → callback → code → token → MCP call with the bearer.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from conftest import USER_A, USER_B, bearer_for, register
from starlette.testclient import TestClient

from garmin_mcp import server
from garmin_mcp.oauth.app import build_oauth_app
from garmin_mcp.oauth.datastore import SummaryStore

BASE = "https://example.test"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _register_client(http: TestClient, name: str = "Claude") -> str:
    r = http.post("/garmin-oauth/register", json={
        "client_name": name,
        "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    })
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def _authorize(http: TestClient, client_id: str, challenge: str) -> str:
    """Start authorization; returns our consent page URL."""
    r = http.get("/garmin-oauth/authorize", params={
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "client-state",
    }, follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers["location"].startswith(f"{BASE}/garmin-oauth/consent?request=")
    return r.headers["location"]


def _approve(http: TestClient, consent_url: str) -> str:
    """Load the consent page and approve; returns Garmin's consent URL."""
    page = http.get(consent_url)
    assert page.status_code == 200
    request_id = parse_qs(urlparse(consent_url).query)["request"][0]
    r = http.post("/garmin-oauth/consent", data={"request": request_id, "consent": "yes", "decision": "approve"},
                  follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers["location"].startswith("https://connect.garmin.com/oauth2Confirm")
    return r.headers["location"]


def _code(http: TestClient) -> tuple[str, str, str]:
    """Run the flow up to the redirect back to the client: (client_id, verifier, code)."""
    verifier, challenge = _pkce()
    client_id = _register_client(http)
    garmin_url = _approve(http, _authorize(http, client_id, challenge))
    garmin_state = parse_qs(urlparse(garmin_url).query)["state"][0]
    back = http.get("/garmin-oauth/callback", params={"code": "garmin-code", "state": garmin_state},
                    follow_redirects=False)
    assert back.status_code == 302
    location = urlparse(back.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == REDIRECT
    query = parse_qs(location.query)
    assert query["state"] == ["client-state"]
    return client_id, verifier, query["code"][0]


def _connect(http: TestClient) -> dict:
    """Run the whole flow; returns the token response."""
    client_id, verifier, code = _code(http)
    r = http.post("/garmin-oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT,
        "client_id": client_id,
        "code_verifier": verifier,
    })
    assert r.status_code == 200, r.text
    return {**r.json(), "client_id": client_id, "code": code, "verifier": verifier}


def _daily_steps(http: TestClient, token: str) -> str:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_daily_stats", "arguments": {"date": "2026-09-20"}},
    }
    r = http.post("/garmin-oauth/mcp", json=body, headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.text


@pytest.fixture
def app(oauth_env, garmin):
    return build_oauth_app(server.mcp, oauth_env)


def test_unauthenticated_mcp_points_to_discovery(app):
    with TestClient(app, base_url=BASE) as http:
        r = http.post("/garmin-oauth/mcp", json={}, headers=MCP_HEADERS)
        assert r.status_code == 401
        challenge = r.headers["www-authenticate"]
        assert 'resource_metadata="https://example.test/.well-known/oauth-protected-resource/garmin-oauth/mcp"' in (
            challenge
        )
        prm = http.get("/.well-known/oauth-protected-resource/garmin-oauth/mcp").json()
        assert prm["resource"] == f"{BASE}/garmin-oauth/mcp"
        assert prm["authorization_servers"] == [f"{BASE}/garmin-oauth"]
        for path in ("/.well-known/oauth-authorization-server/garmin-oauth",
                     "/garmin-oauth/.well-known/oauth-authorization-server"):
            meta = http.get(path).json()
            assert meta["authorization_endpoint"] == f"{BASE}/garmin-oauth/authorize"
            assert meta["token_endpoint"] == f"{BASE}/garmin-oauth/token"
            assert meta["registration_endpoint"] == f"{BASE}/garmin-oauth/register"


def test_full_flow_connects_a_user_and_serves_their_data(app, oauth_env, garmin):
    garmin.garmin_user_id = "garmin-new"
    with TestClient(app, base_url=BASE) as http:
        tokens = _connect(http)
        app.webhook_worker.wait()

        # Garmin's code was exchanged with our PKCE verifier, never the client's.
        assert garmin.token_forms[0]["code"] == "garmin-code"
        assert garmin.token_forms[0]["code_verifier"] != tokens["verifier"]
        user_id = oauth_env.token_root.iterdir()
        user_dirs = [p for p in user_id if not p.name.startswith(".")]
        assert len(user_dirs) == 1
        assert any("/backfill/" in p for p in garmin.paths())

        SummaryStore(user_dirs[0]).put("dailies", [{"summaryId": "d", "calendarDate": "2026-09-20", "steps": 777}])
        assert "777" in _daily_steps(http, tokens["access_token"])


def test_each_token_reads_only_its_own_user(app, oauth_env):
    register(oauth_env, USER_A, "garmin-a")
    register(oauth_env, USER_B, "garmin-b")
    SummaryStore(oauth_env.token_root / USER_A).put("dailies", [{"summaryId": "a", "calendarDate": "2026-09-20",
                                                                 "steps": 1111}])
    SummaryStore(oauth_env.token_root / USER_B).put("dailies", [{"summaryId": "b", "calendarDate": "2026-09-20",
                                                                 "steps": 2222}])
    with TestClient(app, base_url=BASE) as http:
        a = _daily_steps(http, bearer_for(app, USER_A).split()[1])
        b = _daily_steps(http, bearer_for(app, USER_B).split()[1])
    assert "1111" in a and "2222" not in a
    assert "2222" in b and "1111" not in b


def test_bad_or_missing_token_never_reaches_a_user(app, oauth_env):
    register(oauth_env, USER_A, "garmin-a")
    with TestClient(app, base_url=BASE) as http:
        for header in ({}, {"Authorization": "Bearer nope"}, {"Authorization": f"Basic {USER_A}"}):
            assert http.post("/garmin-oauth/mcp", json={}, headers={**MCP_HEADERS, **header}).status_code == 401
        # The old per-user secret URLs are gone.
        assert http.post(f"/garmin-oauth/{USER_A}/mcp", json={}, headers=MCP_HEADERS).status_code == 404


def test_consent_post_without_the_page_cookie_is_refused(app):
    """Consent CSRF: a forged POST knows the request ID but not the browser cookie."""
    victim = TestClient(app, base_url=BASE)  # no lifespan: the MCP manager starts once per app
    with TestClient(app, base_url=BASE) as attacker:
        consent_url = _authorize(attacker, _register_client(attacker), _pkce()[1])
        attacker.get(consent_url)
        request_id = parse_qs(urlparse(consent_url).query)["request"][0]
        r = victim.post("/garmin-oauth/consent", data={"request": request_id, "consent": "yes",
                                                        "decision": "approve"}, follow_redirects=False)
        assert r.status_code == 400
        assert "connect.garmin.com" not in r.headers.get("location", "")


def test_consent_requires_the_checkbox(app):
    with TestClient(app, base_url=BASE) as http:
        consent_url = _authorize(http, _register_client(http), _pkce()[1])
        http.get(consent_url)
        request_id = parse_qs(urlparse(consent_url).query)["request"][0]
        r = http.post("/garmin-oauth/consent", data={"request": request_id, "decision": "approve"},
                      follow_redirects=False)
        assert r.status_code == 400
        assert "Tick the box" in r.text


def test_cancel_returns_access_denied_to_the_client(app):
    with TestClient(app, base_url=BASE) as http:
        consent_url = _authorize(http, _register_client(http), _pkce()[1])
        http.get(consent_url)
        request_id = parse_qs(urlparse(consent_url).query)["request"][0]
        r = http.post("/garmin-oauth/consent", data={"request": request_id, "decision": "deny"},
                      follow_redirects=False)
        query = parse_qs(urlparse(r.headers["location"]).query)
        assert query["error"] == ["access_denied"]
        assert query["state"] == ["client-state"]


def test_consent_page_escapes_the_client_name_and_shows_ai_statement(app):
    with TestClient(app, base_url=BASE) as http:
        consent_url = _authorize(http, _register_client(http, "<script>alert(1)</script>"), _pkce()[1])
        page = http.get(consent_url).text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    assert "never use it to train AI models" in page
    assert "https://productivitytech.io/privacy-policy/#garmin-data" in page
    assert "Fjellestad AS" in page


def test_code_is_single_use_and_refresh_rotates(app):
    with TestClient(app, base_url=BASE) as http:
        tokens = _connect(http)
        replay = http.post("/garmin-oauth/token", data={
            "grant_type": "authorization_code", "code": tokens["code"], "redirect_uri": REDIRECT,
            "client_id": tokens["client_id"], "code_verifier": tokens["verifier"],
        })
        assert replay.status_code == 400

        refreshed = http.post("/garmin-oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": tokens["client_id"],
        })
        assert refreshed.status_code == 200, refreshed.text
        again = http.post("/garmin-oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": tokens["client_id"],
        })
        assert again.status_code == 400
        old = http.post("/garmin-oauth/mcp", json={}, headers={**MCP_HEADERS,
                                                              "Authorization": f"Bearer {tokens['access_token']}"})
        assert old.status_code == 401
        _daily_steps(http, refreshed.json()["access_token"])


def test_expired_garmin_state_shows_a_page_not_a_redirect(app):
    with TestClient(app, base_url=BASE) as http:
        r = http.get("/garmin-oauth/callback", params={"code": "x", "state": "unknown"}, follow_redirects=False)
    assert r.status_code == 400
    assert "expired" in r.text


def test_forged_cancel_is_refused(app):
    """A cross-site POST that knows the request ID may not cancel it either."""
    victim = TestClient(app, base_url=BASE)
    with TestClient(app, base_url=BASE) as http:
        consent_url = _authorize(http, _register_client(http), _pkce()[1])
        http.get(consent_url)
        request_id = parse_qs(urlparse(consent_url).query)["request"][0]
        forged = victim.post("/garmin-oauth/consent", data={"request": request_id, "decision": "deny"},
                             follow_redirects=False)
        assert forged.status_code == 400
        # The real user's approval still works.
        r = http.post("/garmin-oauth/consent", data={"request": request_id, "consent": "yes",
                                                      "decision": "approve"}, follow_redirects=False)
        assert r.headers["location"].startswith("https://connect.garmin.com/oauth2Confirm")


def test_two_consent_tabs_do_not_clobber_each_other(app):
    with TestClient(app, base_url=BASE) as http:
        client_id = _register_client(http)
        first = _authorize(http, client_id, _pkce()[1])
        second = _authorize(http, client_id, _pkce()[1])
        http.get(first)
        http.get(second)
        # Submit the first tab without reloading it: its cookie must still be there.
        request_id = parse_qs(urlparse(first).query)["request"][0]
        r = http.post("/garmin-oauth/consent", data={"request": request_id, "consent": "yes",
                                                      "decision": "approve"}, follow_redirects=False)
        assert r.headers["location"].startswith("https://connect.garmin.com/oauth2Confirm")


def test_authorization_codes_are_not_stored_in_plaintext(app, oauth_env):
    with TestClient(app, base_url=BASE) as http:
        _client_id, _verifier, pending_code = _code(http)  # issued, not yet exchanged
        raw = (oauth_env.token_root / ".mcp-auth.sqlite3").read_bytes()
        assert pending_code.encode() not in raw
        tokens = _connect(http)
    raw = (oauth_env.token_root / ".mcp-auth.sqlite3").read_bytes()
    assert tokens["access_token"].encode() not in raw
    assert tokens["refresh_token"].encode() not in raw


def test_refresh_that_repeats_the_scope_keeps_working(app):
    """Clients often resend `scope` on refresh; the stored grant must still match it."""
    with TestClient(app, base_url=BASE) as http:
        r = http.post("/garmin-oauth/register", json={
            "client_name": "Claude", "redirect_uris": [REDIRECT], "scope": "garmin",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        })
        client_id = r.json()["client_id"]
        verifier, challenge = _pkce()
        consent = http.get("/garmin-oauth/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "scope": "garmin",
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "client-state",
        }, follow_redirects=False).headers["location"]
        garmin_state = parse_qs(urlparse(_approve(http, consent)).query)["state"][0]
        back = http.get("/garmin-oauth/callback", params={"code": "g", "state": garmin_state}, follow_redirects=False)
        code = parse_qs(urlparse(back.headers["location"]).query)["code"][0]
        tokens = http.post("/garmin-oauth/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
            "client_id": client_id, "code_verifier": verifier,
        }).json()
        assert tokens["scope"] == "garmin"
        refreshed = http.post("/garmin-oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
            "client_id": client_id, "scope": "garmin",
        })
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["scope"] == "garmin"


def test_reconnecting_with_less_sharing_drops_the_unshared_data(app, oauth_env, garmin):
    garmin.garmin_user_id = "garmin-same"
    with TestClient(app, base_url=BASE) as http:
        _connect(http)
        [user_dir] = [p for p in oauth_env.token_root.iterdir() if not p.name.startswith(".")]
        data = SummaryStore(user_dir)
        data.put("sleeps", [{"summaryId": "s", "calendarDate": "2026-09-20"}])
        data.put("activities", [{"summaryId": "a", "activityId": 1, "startTimeInSeconds": 1}])

        garmin.permissions = ["ACTIVITY_EXPORT"]  # health sharing unticked this time
        _connect(http)

    assert [p for p in oauth_env.token_root.iterdir() if not p.name.startswith(".")] == [user_dir]
    assert data.by_date("sleeps", "2026-09-20") == []
    assert data.by_activity_id("activities", "1") is not None
