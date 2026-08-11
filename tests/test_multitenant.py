"""Tests for multi-tenant mode: one process, one token store per path-ID.

The HTTP tests drive the real ASGI app end to end with a fake Garmin client,
because the property that matters — a request can only read the token store its
URL names — depends on request routing, not just on the resolver functions.
"""
import asyncio
import json

import pytest
from starlette.testclient import TestClient

from garmin_mcp import multitenant, server

USER_A = "a" * 40
USER_B = "b" * 40


class FakeGarmin:
    """Stands in for garminconnect.Garmin, reporting which store it logged in with."""

    def __init__(self, email=None, password=None, prompt_mfa=None):
        self.email = email
        self.password = password
        self.tokenstore = None

    def login(self, tokenstore=None):
        self.tokenstore = tokenstore
        return True

    def get_stats(self, date):
        return {"date": date, "token_store": self.tokenstore}


@pytest.fixture(autouse=True)
def _reset_client_caches(monkeypatch):
    monkeypatch.setattr(server, "Garmin", FakeGarmin)
    server._client = None
    server._tenant_clients.clear()
    # Process-global flag: reset so each test is order-independent.
    multitenant._multi_tenant_active = False
    yield
    server._client = None
    server._tenant_clients.clear()
    multitenant._multi_tenant_active = False


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    path = tmp_path_factory.mktemp("tenants")
    for user in (USER_A, USER_B):
        (path / user).mkdir()
    return path


# --- ID validation -------------------------------------------------------


@pytest.mark.parametrize("user_id", [USER_A, "a1b2-" * 8, "0123456789abcdef" * 2])
def test_valid_user_ids_accepted(user_id):
    assert multitenant.is_valid_user_id(user_id)


@pytest.mark.parametrize(
    "user_id",
    [
        "",
        "short",
        "a" * 31,  # below the entropy floor
        "a" * 129,  # above the ceiling
        "A" * 40,  # uppercase
        "../" + "a" * 37,  # path traversal
        "a" * 20 + "/" + "b" * 20,
        "a" * 20 + "." + "b" * 20,
        "a" * 20 + "_" + "b" * 20,
        "a" * 20 + "%2e" + "b" * 20,
    ],
)
def test_invalid_user_ids_rejected(user_id):
    assert not multitenant.is_valid_user_id(user_id)


def test_resolve_returns_store_for_known_user(root):
    assert multitenant.resolve_token_store(root, USER_A) == root / USER_A


def test_resolve_returns_none_for_unknown_and_invalid(root):
    assert multitenant.resolve_token_store(root, "c" * 40) is None
    assert multitenant.resolve_token_store(root, "../etc") is None


def test_traversal_cannot_escape_root(root, tmp_path):
    outside = tmp_path.parent / "outside-store"
    outside.mkdir(exist_ok=True)
    assert multitenant.resolve_token_store(root, f"..%2f{outside.name}") is None
    assert multitenant.resolve_token_store(root, "../" + outside.name) is None


# --- single-tenant default stays untouched -------------------------------


def test_single_tenant_root_unset_by_default(monkeypatch):
    monkeypatch.delenv(multitenant.MULTI_TENANT_ROOT_ENV, raising=False)
    assert multitenant.multi_tenant_root() is None
    assert multitenant.current_token_store() is None


def test_single_tenant_client_uses_process_token_store(monkeypatch, tmp_path):
    monkeypatch.setenv("GARMIN_TOKENS", str(tmp_path / "single"))
    client = server.get_client()
    assert client.tokenstore == str(tmp_path / "single")
    # Cached process-wide, exactly as before multi-tenant existed.
    assert server.get_client() is client
    assert server._tenant_clients == {}


# --- HTTP: isolation between tenants -------------------------------------


def _post(client, user_id, method, params=None, path_prefix="/u"):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(
        f"{path_prefix}/{user_id}/mcp",
        json=body,
        headers={"Accept": "application/json, text/event-stream"},
    )


def _rpc_result(response):
    """Pull the JSON-RPC result out of an SSE or plain-JSON MCP response."""
    text = response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        line = next(ln for ln in text.splitlines() if ln.startswith("data: "))
        text = line[len("data: ") :]
    payload = json.loads(text)
    assert "error" not in payload, payload
    return payload["result"]


def _daily_stats_store(client, user_id):
    """Call get_daily_stats as one user and report which token store answered."""
    result = _rpc_result(
        _post(client, user_id, "tools/call", {"name": "get_daily_stats", "arguments": {"date": "2026-08-11"}})
    )
    payload = json.loads(result["content"][0]["text"])
    return payload["token_store"]


@pytest.fixture(scope="module")
def http(root):
    """One app for the module: a session manager can only be run once per instance.

    base_url carries a 127.0.0.1 Host on purpose — the MCP SDK's DNS-rebinding
    guard rejects anything else, which is the same check nginx has to satisfy in
    production by overriding Host (see garmin-mcp-remote.md).
    """
    app = multitenant.build_multi_tenant_app(server.mcp, root)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        yield client


def test_each_user_id_reads_only_its_own_token_store(http, root):
    assert _daily_stats_store(http, USER_A) == str(root / USER_A)
    assert _daily_stats_store(http, USER_B) == str(root / USER_B)
    # Two distinct clients, one per store — never one shared session.
    assert set(server._tenant_clients) == {str(root / USER_A), str(root / USER_B)}


def test_tools_list_served_per_user(http):
    result = _rpc_result(_post(http, USER_A, "tools/list"))
    assert "get_daily_stats" in {tool["name"] for tool in result["tools"]}


def test_unknown_user_id_is_rejected(http):
    response = _post(http, "c" * 40, "tools/list")
    assert response.status_code == 404
    assert server._tenant_clients == {}


def test_invalid_user_id_is_rejected(http):
    for bad in ("short", "A" * 40, "a" * 20 + "_" + "b" * 20):
        assert _post(http, bad, "tools/list").status_code == 404
    assert server._tenant_clients == {}


def test_paths_outside_the_pattern_are_rejected(http):
    for path in (f"/u/{USER_A}", f"/u/{USER_A}/mcp/extra", "/mcp", f"/{USER_A}/mcp", "/"):
        assert http.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 404


def test_tenant_request_ignores_env_credentials(http, root, monkeypatch):
    """A tenant must never be logged in as the host, even with env creds set."""
    monkeypatch.setenv("GARMIN_EMAIL", "host@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")
    assert _daily_stats_store(http, USER_A) == str(root / USER_A)
    client = server._tenant_clients[str(root / USER_A)]
    assert client.email is None
    assert client.password is None


def test_token_store_unbound_after_request(http):
    _daily_stats_store(http, USER_A)
    assert multitenant.current_token_store() is None


# --- the isolation mechanism itself --------------------------------------


def test_concurrent_tasks_keep_their_own_token_store(root):
    """Interleaved requests must not see each other's store.

    Isolation rests on each task getting its own copy of the context. This
    drives that directly, with awaits forcing the two tasks to overlap, so a
    regression shows up as a failed assert rather than as one person reading
    another's Garmin data.
    """

    async def call(user):
        token = multitenant._current_token_store.set(str(root / user))
        try:
            await asyncio.sleep(0)  # yield, so the other task runs in between
            client = server.get_client()
            await asyncio.sleep(0)
            assert multitenant.current_token_store() == str(root / user)
            return client.tokenstore
        finally:
            multitenant._current_token_store.reset(token)

    async def both():
        return await asyncio.gather(call(USER_A), call(USER_B))

    stores = asyncio.run(both())
    assert stores == [str(root / USER_A), str(root / USER_B)]


def test_multi_tenant_mode_fails_closed_without_a_bound_store(monkeypatch):
    """An unrouted request must error, never fall back to the host's account."""
    monkeypatch.setenv("GARMIN_EMAIL", "host@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")
    multitenant.activate_multi_tenant()

    with pytest.raises(RuntimeError):
        server.get_client()
    assert server._client is None
    assert server._tenant_clients == {}
