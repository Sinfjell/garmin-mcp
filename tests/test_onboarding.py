"""Tests for the self-service onboarding flow.

garminconnect is faked, but the flow is driven for real: HTTP form posts
through the actual FastAPI app, a token store written to a real directory, and
the resulting user ID checked against the server's own routing rules.
"""
import json
import logging
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

from garmin_mcp import multitenant
from garmin_mcp.onboarding import cli as tenants
from garmin_mcp.onboarding import store as onboarding_store
from garmin_mcp.onboarding.app import create_app

PASSWORD = "korrekt-hest-batteri-stift"
EMAIL = "svigersoster@example.com"
MFA_CODE = "123456"


class FakeGarthClient:
    """Stands in for garminconnect's inner client — only dump() matters here."""

    def __init__(self):
        self.dumped_to = None

    def dump(self, path):
        self.dumped_to = path
        from pathlib import Path

        target = Path(path)
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Shape mirrors garminconnect: a token, never a password.
        (target / "oauth2_token.json").write_text(json.dumps({"refresh_token": "tok-abc"}))


class FakeGarmin:
    """Configurable fake: MFA or not, wrong password, rate limiting."""

    needs_mfa = False
    rate_limit_times = 0
    bad_credentials = False
    instances: ClassVar[list] = []

    def __init__(self, email=None, password=None, return_on_mfa=False, **kwargs):
        self.username = email
        self.password = password
        self.return_on_mfa = return_on_mfa
        self.client = FakeGarthClient()
        self.resumed_with = None
        FakeGarmin.instances.append(self)

    def login(self, tokenstore=None):
        if FakeGarmin.rate_limit_times > 0:
            FakeGarmin.rate_limit_times -= 1
            raise GarminConnectTooManyRequestsError("429")
        if FakeGarmin.bad_credentials:
            raise GarminConnectAuthenticationError("bad credentials")
        if FakeGarmin.needs_mfa:
            return "needs_mfa", None
        return None, None

    def resume_login(self, client_state, mfa_code):
        self.resumed_with = mfa_code
        if mfa_code != MFA_CODE:
            raise GarminConnectAuthenticationError("wrong code")
        return None, None


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeGarmin.needs_mfa = False
    FakeGarmin.rate_limit_times = 0
    FakeGarmin.bad_credentials = False
    FakeGarmin.instances = []
    yield
    FakeGarmin.instances = []


class Clock:
    """Manually advanced clock, so session expiry is tested without waiting."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def slept():
    return []


@pytest.fixture
def root(tmp_path):
    return tmp_path / "tenants"


@pytest.fixture
def app(root, clock, slept):
    return create_app(
        root=root,
        base_url="https://productivitytech.io",
        prefix="/u",
        garmin_factory=FakeGarmin,
        session_ttl=300,
        sleep=slept.append,
        clock=clock,
    )


@pytest.fixture
def client(app):
    return TestClient(app)


def _start(client, password=PASSWORD):
    return client.post("/start", data={"email": EMAIL, "password": password})


def _user_id_from(response):
    """Pull the user ID out of the connector URL shown on the success page."""
    marker = "https://productivitytech.io/u/"
    assert marker in response.text, response.text[:400]
    return response.text.split(marker, 1)[1].split("/mcp", 1)[0]


# --- the happy paths -----------------------------------------------------


def test_login_page_shows_consent_before_the_form(client):
    body = client.get("/").text
    assert body.count("<form") == 1
    for phrase in ("Dette lagres om deg", "lagres aldri", "slette", "Passordet"):
        assert phrase in body
    # The consent block must come before the password field, not after it.
    assert body.index("Dette lagres om deg") < body.index('type="password"')


def test_onboarding_without_mfa_writes_a_store_and_returns_a_url(client, root):
    response = _start(client)
    assert response.status_code == 200

    user_id = _user_id_from(response)
    assert multitenant.is_valid_user_id(user_id)
    # The ID the page handed out is one the MCP server will actually route.
    assert multitenant.resolve_token_store(root, user_id) == root / user_id
    assert (root / user_id / "oauth2_token.json").is_file()


def test_onboarding_with_mfa_completes_on_the_second_step(client, root):
    FakeGarmin.needs_mfa = True

    first = _start(client)
    assert first.status_code == 200
    assert "Engangskode" in first.text
    session_id = first.text.split('name="session_id" value="', 1)[1].split('"', 1)[0]

    second = client.post("/mfa", data={"session_id": session_id, "code": MFA_CODE})
    assert second.status_code == 200
    user_id = _user_id_from(second)
    assert multitenant.resolve_token_store(root, user_id) == root / user_id
    assert FakeGarmin.instances[-1].resumed_with == MFA_CODE


def test_each_onboarding_gets_a_distinct_id(client, root):
    ids = {_user_id_from(_start(client)) for _ in range(3)}
    assert len(ids) == 3
    assert sorted(onboarding_store.list_user_ids(root)) == sorted(ids)


def test_health_endpoint(client):
    assert client.get("/health").json() == {"status": "ok"}


# --- the failure paths ---------------------------------------------------


def test_wrong_mfa_code_is_rejected_and_burns_the_session(client, app):
    FakeGarmin.needs_mfa = True
    session_id = _start(client).text.split('name="session_id" value="', 1)[1].split('"', 1)[0]

    bad = client.post("/mfa", data={"session_id": session_id, "code": "000000"})
    assert bad.status_code == 401
    # Garmin clears its MFA state on a rejected code, so the session is spent.
    assert len(app.state.sessions) == 0
    retry = client.post("/mfa", data={"session_id": session_id, "code": MFA_CODE})
    assert retry.status_code == 410


def test_expired_session_cannot_be_completed(client, app, clock):
    FakeGarmin.needs_mfa = True
    session_id = _start(client).text.split('name="session_id" value="', 1)[1].split('"', 1)[0]

    clock.now += 301  # TTL is 300s
    late = client.post("/mfa", data={"session_id": session_id, "code": MFA_CODE})
    assert late.status_code == 410
    assert len(app.state.sessions) == 0


def test_unknown_session_id_is_rejected(client):
    assert client.post("/mfa", data={"session_id": "nope", "code": MFA_CODE}).status_code == 410


def test_bad_credentials_are_reported_without_a_store(client, root):
    FakeGarmin.bad_credentials = True
    response = _start(client)
    assert response.status_code == 401
    assert "Feil e-post eller passord" in response.text
    assert onboarding_store.list_user_ids(root) == []


def test_a_rate_limit_is_reported_without_hammering_garmin(client, slept, root):
    """One attempt by default.

    garminconnect has already tried five strategies, with its own Cloudflare
    backoff, by the time it raises — measured at ~1m45s from the production
    host. A second attempt would add five more SSO hits against an IP Garmin is
    already refusing.
    """
    FakeGarmin.rate_limit_times = 99
    response = _start(client)
    assert response.status_code == 429
    assert "for mange forsøk" in response.text
    assert len(FakeGarmin.instances) == 1
    assert slept == []
    assert onboarding_store.list_user_ids(root) == []


def test_the_retry_mechanism_still_works_when_turned_on(root, slept, clock):
    """Kept configurable, so re-enabling it is a config change, not a rewrite."""
    app = create_app(
        root=root,
        base_url="https://productivitytech.io",
        garmin_factory=FakeGarmin,
        login_attempts=3,
        sleep=slept.append,
        clock=clock,
    )
    FakeGarmin.rate_limit_times = 2
    response = TestClient(app).post("/start", data={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200
    assert slept == [5.0, 10.0]  # exponential
    assert len(onboarding_store.list_user_ids(root)) == 1


# --- the password must not survive the request ---------------------------


def test_password_is_never_written_to_disk(client, root, tmp_path):
    _start(client)
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written, "expected a token store on disk"
    for path in written:
        assert PASSWORD not in path.read_text()
        assert PASSWORD not in str(path)


def test_password_is_never_logged(client, caplog):
    with caplog.at_level(logging.DEBUG):
        _start(client)
        FakeGarmin.bad_credentials = True
        _start(client)
    assert PASSWORD not in caplog.text
    assert EMAIL not in caplog.text


def test_a_failing_client_cannot_leak_the_password_into_the_log(client, caplog, monkeypatch):
    """The unexpected-failure branch is the one that logs — so break it on purpose.

    An HTTP client that echoes the request it sent puts the password inside the
    exception message. If that branch ever logs the exception itself, this test
    fails; asserting only on the tidy paths would let the leak through.
    """
    leaky = RuntimeError(f"POST /sso failed: email={EMAIL}&password={PASSWORD}")

    def explode(self, tokenstore=None):
        raise leaky

    monkeypatch.setattr(FakeGarmin, "login", explode)

    with caplog.at_level(logging.DEBUG):
        response = _start(client)

    assert response.status_code == 502
    assert caplog.records, "the failure must still be recorded, just not verbatim"
    assert PASSWORD not in caplog.text
    assert EMAIL not in caplog.text
    assert "RuntimeError" in caplog.text  # the type survives; the message does not
    # And the user is not shown the raw failure either.
    assert PASSWORD not in response.text


def test_password_is_dropped_from_client_state_before_the_mfa_wait(client, app):
    FakeGarmin.needs_mfa = True
    _start(client)

    pending_client = FakeGarmin.instances[-1]
    assert pending_client.password is None
    # And it is not hiding in the session record either.
    session = next(iter(app.state.sessions._sessions.values()))
    assert PASSWORD not in repr(vars(session))


def test_password_is_dropped_after_a_failed_login(client):
    FakeGarmin.bad_credentials = True
    _start(client)
    assert FakeGarmin.instances[-1].password is None


# --- deletion ------------------------------------------------------------


def test_delete_removes_the_store_and_unroutes_the_url(client, root):
    user_id = _user_id_from(_start(client))
    assert multitenant.resolve_token_store(root, user_id) is not None

    assert onboarding_store.delete_token_store(root, user_id) is True
    # Now indistinguishable from an ID that never existed -> the server 404s it.
    assert multitenant.resolve_token_store(root, user_id) is None
    assert onboarding_store.delete_token_store(root, user_id) is False


def test_concurrent_mfa_posts_do_not_crash_the_store(root, clock):
    """Two people finishing at once, with expired sessions in the way.

    FastAPI runs these endpoints in a worker threadpool, so this is real
    concurrency, not a hypothetical. An unguarded purge-then-delete raises
    KeyError on the loser and returns a 500.
    """
    import threading

    store = onboarding_store.SessionStore(ttl_seconds=300, clock=clock)
    live = [store.add(object(), None) for _ in range(50)]
    clock.now += 301  # every session is now expired
    fresh = [store.add(object(), None) for _ in range(50)]

    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(ids):
        barrier.wait()
        try:
            for session_id in ids:
                store.pop(session_id)
                len(store)
        except BaseException as exc:  # noqa: BLE001 - the point is to catch anything
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(live + fresh,)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(store) == 0


def test_missing_base_url_fails_at_startup(root):
    """Better to refuse to boot than to hand someone a relative path."""
    with pytest.raises(RuntimeError, match="GARMIN_CONNECTOR_BASE_URL"):
        create_app(root=root, base_url="", garmin_factory=FakeGarmin)
    with pytest.raises(RuntimeError):
        create_app(root=root, base_url="productivitytech.io", garmin_factory=FakeGarmin)


def test_delete_refuses_an_invalid_id(root):
    root.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError):
        onboarding_store.delete_token_store(root, "../etc")


def test_tenant_cli_lists_and_deletes(client, root, capsys):
    user_id = _user_id_from(_start(client))

    tenants.main(["--root", str(root), "list"])
    assert user_id in capsys.readouterr().out

    tenants.main(["--root", str(root), "delete", user_id])
    assert "now 404s" in capsys.readouterr().out
    assert multitenant.resolve_token_store(root, user_id) is None

    with pytest.raises(SystemExit):
        tenants.main(["--root", str(root), "delete", user_id])
