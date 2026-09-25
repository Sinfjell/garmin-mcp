"""Ping/Push intake, deregistration and permission changes, driven over HTTP.

Garmin's side is an ``httpx.MockTransport``: every outbound call the server
makes (Ping callbacks, token checks, permission reads, backfill) is answered
here, so the tests see exactly which Garmin endpoints were contacted.
"""
from __future__ import annotations

import json
import os
import time

import pytest
from conftest import BOTH, PING, PUSH, USER_A, USER_B, bearer_for, register
from starlette.testclient import TestClient

from garmin_mcp import server
from garmin_mcp.oauth import client as client_module
from garmin_mcp.oauth import config as oauth_config
from garmin_mcp.oauth import webhooks
from garmin_mcp.oauth.app import build_oauth_app
from garmin_mcp.oauth.datastore import SummaryStore


def _post(app, payload, path=PUSH):
    # No `with`: webhook routes need no lifespan, and the MCP session manager
    # may only be started once per app.
    response = TestClient(app).post(path, content=json.dumps(payload))
    app.webhook_worker.wait()
    return response


def _daily(garmin_id, day="2026-09-20", steps=8000):
    return {"userId": garmin_id, "summaryId": f"d-{garmin_id}-{day}", "calendarDate": day, "steps": steps}


def test_push_lands_only_in_the_named_users_store(oauth_env, garmin):
    register(oauth_env, USER_A, "garmin-a")
    register(oauth_env, USER_B, "garmin-b")
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
    register(oauth_env, USER_A, "garmin-a")
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
        headers["Authorization"] = bearer_for(app, USER_A)
        r = http.post("/garmin-oauth/mcp", json=body, headers=headers)
    assert r.status_code == 200
    assert "12345" in r.text


def test_ping_follows_callback_with_the_users_token(oauth_env, garmin):
    register(oauth_env, USER_A, "garmin-a")
    garmin.callback_payload = [_daily("garmin-a", steps=4321)]
    app = build_oauth_app(server.mcp, oauth_env)

    callback = "https://apis.garmin.com/wellness-api/rest/dailies?uploadStartTimeInSeconds=1&token=x"
    _post(app, {"dailies": [{"userId": "garmin-a", "callbackURL": callback}]}, PING)

    assert len(garmin.requests) == 1
    assert garmin.requests[0].headers["Authorization"] == "Bearer access-garmin-a"
    stored = SummaryStore(oauth_env.token_root / USER_A).by_date("dailies", "2026-09-20")
    assert [d["steps"] for d in stored] == [4321]


def test_ping_to_a_foreign_host_is_not_followed(oauth_env, garmin):
    register(oauth_env, USER_A, "garmin-a")
    app = build_oauth_app(server.mcp, oauth_env)

    _post(app, {"dailies": [{"userId": "garmin-a", "callbackURL": "https://evil.example/steal"}]})

    assert garmin.requests == []
    # Kept and retried, not silently dropped.
    retries = list((oauth_env.token_root / ".inbox" / "retry").glob("1-*.json"))
    assert len(retries) == 1


def test_push_without_permission_is_not_stored(oauth_env, garmin):
    register(oauth_env, USER_A, "garmin-a", permissions=["ACTIVITY_EXPORT"])
    app = build_oauth_app(server.mcp, oauth_env)
    _post(app, {"dailies": [_daily("garmin-a")]})
    assert SummaryStore(oauth_env.token_root / USER_A).by_date("dailies", "2026-09-20") == []


def test_deregistration_deletes_user_once_garmin_rejects_tokens(oauth_env, garmin):
    store = register(oauth_env, USER_A, "garmin-a")
    register(oauth_env, USER_B, "garmin-b")
    app = build_oauth_app(server.mcp, oauth_env)
    _post(app, {"dailies": [_daily("garmin-a")]})
    token = bearer_for(app, USER_A)

    garmin.user_id_status = 401
    _post(app, {"deregistrations": [{"userId": "garmin-a"}]}, PING)

    assert not (oauth_env.token_root / USER_A).exists()
    assert store.lookup_by_garmin_user_id("garmin-a") is None
    assert store.resolve_existing(USER_B) is not None
    # The MCP token dies with the user.
    assert TestClient(app).post("/garmin-oauth/mcp", json={}, headers={"Authorization": token}).status_code == 401


def test_forged_deregistration_is_ignored(oauth_env, garmin):
    store = register(oauth_env, USER_A, "garmin-a")
    app = build_oauth_app(server.mcp, oauth_env)

    garmin.user_id_status = 200  # Garmin still honours the tokens
    _post(app, {"deregistrations": [{"userId": "garmin-a"}]})

    assert store.resolve_existing(USER_A) is not None


def test_permission_change_uses_garmins_answer_and_purges_withdrawn_data(oauth_env, garmin):
    store = register(oauth_env, USER_A, "garmin-a")
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
    assert list((oauth_env.token_root / ".inbox").rglob("*.json")) == []
    assert list((oauth_env.token_root / ".inbox").rglob("*.partial")) == []


def test_webhook_needs_the_secret_segment(oauth_env, garmin):
    register(oauth_env, USER_A, "garmin-a")
    app = build_oauth_app(server.mcp, oauth_env)

    assert _post(app, {"dailies": [_daily("garmin-a")]}, "/garmin-oauth/webhooks/push").status_code == 404
    assert _post(app, {"dailies": [_daily("garmin-a")]}, "/garmin-oauth/webhooks/" + "x" * 40 + "/push").status_code == 404
    assert SummaryStore(oauth_env.token_root / USER_A).by_date("dailies", "2026-09-20") == []
    assert _post(app, {"dailies": [_daily("garmin-a")]}).status_code == 200
    assert len(SummaryStore(oauth_env.token_root / USER_A).by_date("dailies", "2026-09-20")) == 1


@pytest.mark.parametrize("value", ["", "short", "a/" + "b" * 40])
def test_oauth_mode_refuses_to_start_without_a_proper_secret(oauth_env, monkeypatch, value):
    monkeypatch.setenv("GARMIN_OAUTH_WEBHOOK_SECRET", value)
    with pytest.raises(RuntimeError, match="GARMIN_OAUTH_WEBHOOK_SECRET"):
        oauth_config.load_oauth_config()


def test_spooled_delivery_is_processed_after_restart(oauth_env, garmin):
    register(oauth_env, USER_A, "garmin-a")
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
    store = register(oauth_env, USER_A, "garmin-a", permissions=["ACTIVITY_EXPORT"])
    client = client_module.OfficialGarminClient(oauth_env, store, USER_A)
    webhooks.request_initial_backfill(client, ["ACTIVITY_EXPORT"])
    assert garmin.paths() == ["/wellness-api/rest/backfill/activities"]
    params = garmin.requests[0].url.params
    span = int(params["summaryEndTimeInSeconds"]) - int(params["summaryStartTimeInSeconds"])
    assert span == webhooks.BACKFILL_DAYS * 24 * 60 * 60


# --- Review round 1: transient failures, retention, forgery ---------------


def _expired(store, user_id):
    bundle = store.load_tokens(user_id)
    bundle.expires_at = 0
    store.save_tokens(user_id, bundle)


@pytest.mark.parametrize("expired_access", [True, False])
def test_deregistration_survives_a_token_endpoint_outage(oauth_env, garmin, expired_access):
    """A 5xx from Garmin says nothing about the registration: nobody is deleted."""
    store = register(oauth_env, USER_A, "garmin-a")
    if expired_access:
        _expired(store, USER_A)
    else:
        garmin.user_id_status = 401  # access token refused, then the refresh hits the outage
    garmin.token_status = 503
    app = build_oauth_app(server.mcp, oauth_env)

    _post(app, {"deregistrations": [{"userId": "garmin-a"}]}, PING)

    assert store.resolve_existing(USER_A) is not None
    assert len(list((oauth_env.token_root / ".inbox" / "retry").glob("1-*.json"))) == 1


def test_refused_refresh_token_counts_as_deregistered(oauth_env, garmin):
    store = register(oauth_env, USER_A, "garmin-a")
    _expired(store, USER_A)
    garmin.token_status = 400  # invalid_grant: Garmin revoked the refresh token
    app = build_oauth_app(server.mcp, oauth_env)
    _post(app, {"deregistrations": [{"userId": "garmin-a"}]}, PING)
    assert store.resolve_existing(USER_A) is None


def test_failed_ping_is_retried_and_then_stored(oauth_env, garmin):
    register(oauth_env, USER_A, "garmin-a")
    garmin.callback_payload = [_daily("garmin-a", steps=999)]
    garmin.callback_status = 503
    app = build_oauth_app(server.mcp, oauth_env)
    callback = "https://apis.garmin.com/wellness-api/rest/dailies?token=x"
    _post(app, {"dailies": [{"userId": "garmin-a", "callbackURL": callback}]}, PING)

    [retry] = (oauth_env.token_root / ".inbox" / "retry").glob("1-*.json")
    garmin.callback_status = 200
    app.webhook_worker.submit(retry)
    app.webhook_worker.wait()

    stored = SummaryStore(oauth_env.token_root / USER_A).by_date("dailies", "2026-09-20")
    assert [d["steps"] for d in stored] == [999]
    assert not retry.exists()


def test_items_are_parked_after_the_last_retry(oauth_env, garmin):
    register(oauth_env, USER_A, "garmin-a")
    app = build_oauth_app(server.mcp, oauth_env)
    last = len(webhooks.RETRY_DELAYS_SECONDS)
    item = {"dailies": [{"userId": "garmin-a", "callbackURL": "https://evil.example/x"}]}
    path = app.webhook_worker.inbox.write(app.webhook_worker.inbox.retry_dir, item, str(last))
    app.webhook_worker.submit(path)
    app.webhook_worker.wait()
    assert len(list((oauth_env.token_root / ".inbox" / "failed").glob("*.json"))) == 1


def test_parked_files_expire(oauth_env):
    inbox = webhooks.WebhookInbox(oauth_env.token_root)
    old = inbox.write(inbox.failed_dir, {"dailies": []}, "failed")
    fresh = inbox.write(inbox.failed_dir, {"dailies": []}, "failed")
    week_ago = time.time() - webhooks.FAILED_RETENTION_SECONDS - 60
    os.utime(old, (week_ago, week_ago))
    inbox.expire_failed()
    assert not old.exists()
    assert fresh.exists()


def test_deregistration_scrubs_the_user_from_spooled_files(oauth_env, garmin):
    register(oauth_env, USER_A, "garmin-a")
    register(oauth_env, USER_B, "garmin-b")
    app = build_oauth_app(server.mcp, oauth_env)
    inbox = app.webhook_worker.inbox
    parked = inbox.write(inbox.failed_dir, {"dailies": [_daily("garmin-a"), _daily("garmin-b")]}, "failed")
    only_a = inbox.write(inbox.failed_dir, {"sleeps": [{"userId": "garmin-a"}]}, "failed")

    garmin.user_id_status = 401
    _post(app, {"deregistrations": [{"userId": "garmin-a"}]}, PING)

    assert not only_a.exists()
    left = json.loads(parked.read_text())
    assert [i["userId"] for i in left["dailies"]] == ["garmin-b"]


def test_unexpected_permissions_payload_purges_nothing(oauth_env, garmin):
    store = register(oauth_env, USER_A, "garmin-a")
    app = build_oauth_app(server.mcp, oauth_env)
    _post(app, {"dailies": [_daily("garmin-a")]})

    garmin.permissions = {"somethingElse": []}
    _post(app, {"userPermissionsChange": [{"userId": "garmin-a"}]})

    assert len(SummaryStore(oauth_env.token_root / USER_A).by_date("dailies", "2026-09-20")) == 1
    assert store.load_tokens(USER_A).permissions == BOTH


def test_unknown_permissions_store_nothing(oauth_env, garmin):
    register(oauth_env, USER_A, "garmin-a", permissions=None)
    app = build_oauth_app(server.mcp, oauth_env)
    _post(app, {"dailies": [_daily("garmin-a")]})
    assert SummaryStore(oauth_env.token_root / USER_A).by_date("dailies", "2026-09-20") == []


def test_one_push_with_many_items_is_one_write_per_user(oauth_env, garmin, monkeypatch):
    register(oauth_env, USER_A, "garmin-a")
    calls = []
    real_put = SummaryStore.put
    monkeypatch.setattr(SummaryStore, "put", lambda self, t, s: calls.append(t) or real_put(self, t, s))
    app = build_oauth_app(server.mcp, oauth_env)
    _post(app, {"dailies": [_daily("garmin-a", day=f"2026-09-{d:02d}") for d in range(1, 21)]})
    assert calls == ["dailies"]


def test_disconnect_mid_upload_queues_nothing(oauth_env):
    import anyio

    from garmin_mcp.oauth.app import _spool_body

    inbox = webhooks.WebhookInbox(oauth_env.token_root)
    messages = iter([{"type": "http.request", "body": b'{"dailies":', "more_body": True}, {"type": "http.disconnect"}])

    async def receive():
        return next(messages)

    assert anyio.run(_spool_body, receive, inbox) is None
    assert list(inbox.dir.glob("*.json")) == []
    assert list(inbox.dir.glob("*.partial")) == []
