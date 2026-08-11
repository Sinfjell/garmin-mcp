"""Self-service onboarding: log in with your own Garmin account, get your URL.

The whole point is that nobody has to SSH into the host to add a person. A
browser flow does the Garmin login (including MFA), writes that person's token
store, and hands back their personal connector URL.

Credential handling, which is the part worth being strict about:

* The password exists only for the duration of the login call. `garminconnect`
  consumes it against Garmin's SSO, and we clear it off the client immediately
  afterwards — before the MFA wait, not after it.
* It is never written to disk and never passed to a logger. What lands on disk
  is Garmin's own token, written by garminconnect at 0o600.
* Nothing in this module logs the email either. There is no request log line
  that could identify who onboarded, only that someone did.
"""
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

from garmin_mcp.onboarding import pages
from garmin_mcp.onboarding.store import SessionStore, new_user_id, write_token_store

logger = logging.getLogger(__name__)

BASE_URL_ENV = "GARMIN_CONNECTOR_BASE_URL"
PREFIX_ENV = "GARMIN_CONNECTOR_PREFIX"

# Garmin rate-limits datacenter IPs harder than home connections, and this host
# has a history of it. garminconnect already falls through five login strategies
# internally and only raises once all five were limited — so one outer attempt
# already costs five SSO hits. Retrying hard would deepen the block rather than
# get us in, which is why this is a single retry after a real pause, not a loop.
DEFAULT_LOGIN_ATTEMPTS = 2
_BACKOFF_START_SECONDS = 5.0

_RATE_LIMITED = (
    "Garmin slipper oss ikke inn akkurat nå (for mange forsøk). "
    "Vent noen minutter og prøv igjen."
)
_BAD_CREDENTIALS = "Feil e-post eller passord. Prøv igjen."
_LOGIN_FAILED = "Innloggingen mot Garmin feilet. Prøv igjen om litt."


def _connector_url(base_url: str, prefix: str, user_id: str) -> str:
    return f"{base_url.rstrip('/')}/{prefix.strip('/')}/{user_id}/mcp"


def create_app(
    *,
    root: Path | None = None,
    base_url: str | None = None,
    prefix: str | None = None,
    garmin_factory: Callable[..., Any] = Garmin,
    session_ttl: float | None = None,
    login_attempts: int = DEFAULT_LOGIN_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """Build the onboarding app.

    Everything external — where token stores go, what URL to hand out, how to
    build a Garmin client, how time passes — is injected, so the tests drive the
    real flow rather than a rehearsal of it.
    """
    from garmin_mcp import multitenant

    root = root if root is not None else multitenant.multi_tenant_root()
    if root is None:
        raise RuntimeError(
            f"Onboarding needs a token-store root: set {multitenant.MULTI_TENANT_ROOT_ENV}."
        )
    base_url = base_url if base_url is not None else os.environ.get(BASE_URL_ENV, "")
    if not base_url.startswith(("http://", "https://")):
        # Fail at startup, not at the finish line. Without this the app boots
        # fine and only reveals the misconfiguration by handing someone a
        # relative path — after they have already typed their password and
        # burned a one-time code.
        raise RuntimeError(
            f"{BASE_URL_ENV} must be the public origin, e.g. https://example.com "
            f"(got {base_url!r}). Connector URLs are built from it."
        )
    prefix = prefix if prefix is not None else os.environ.get(PREFIX_ENV, "/u")

    sessions = SessionStore(
        ttl_seconds=session_ttl if session_ttl is not None else 300.0, clock=clock
    )
    app = FastAPI(title="Garmin connector onboarding", docs_url=None, redoc_url=None)
    app.state.sessions = sessions
    app.state.root = root

    def finish(client: Any) -> HTMLResponse:
        """Persist the logged-in session as a new user, and hand back their URL."""
        user_id = new_user_id()
        write_token_store(client, root, user_id)
        logger.info("Onboarding completed for a new user store.")
        return HTMLResponse(pages.success_page(_connector_url(base_url, prefix, user_id)))

    def start_login(email: str, password: str) -> tuple[Any, Any, Any]:
        """Run Garmin's login, retrying with backoff while it rate-limits us."""
        delay = _BACKOFF_START_SECONDS
        last: Exception | None = None
        for attempt in range(login_attempts):
            client = garmin_factory(email=email, password=password, return_on_mfa=True)
            try:
                status, state = client.login()
            except GarminConnectTooManyRequestsError as exc:
                last = exc
                if attempt < login_attempts - 1:
                    sleep(delay)
                    delay *= 2
                continue
            finally:
                # The password has done its job by now: garminconnect has either
                # exchanged it for a token or failed. resume_login() works off MFA
                # state on the client, not the password, so dropping it here keeps
                # it out of the minutes-long MFA wait entirely.
                client.password = None
            return client, status, state
        raise last if last is not None else GarminConnectTooManyRequestsError("rate limited")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return pages.login_page()

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/start", response_class=HTMLResponse)
    def start(email: str = Form(...), password: str = Form(...)) -> HTMLResponse:
        try:
            client, status, state = start_login(email, password)
        except GarminConnectTooManyRequestsError:
            logger.warning("Garmin rate-limited an onboarding login.")
            return HTMLResponse(pages.login_page(_RATE_LIMITED), status_code=429)
        except GarminConnectAuthenticationError:
            return HTMLResponse(pages.login_page(_BAD_CREDENTIALS), status_code=401)
        except Exception as exc:  # noqa: BLE001 - the user gets a page, not a traceback
            # Only the exception's *type*. Not exc_info, not str(exc): a failing
            # HTTP client can echo the request it sent, and that request carried
            # the password. Losing the message costs some debuggability; keeping
            # it risks writing a credential to the journal forever.
            logger.error("Onboarding login failed: %s", type(exc).__name__)
            return HTMLResponse(pages.login_page(_LOGIN_FAILED), status_code=502)

        if status == "needs_mfa":
            session_id = sessions.add(client, state)
            return HTMLResponse(pages.mfa_page(session_id))
        return finish(client)

    @app.post("/mfa", response_class=HTMLResponse)
    def mfa(session_id: str = Form(...), code: str = Form(...)) -> HTMLResponse:
        pending = sessions.pop(session_id)
        if pending is None:
            return HTMLResponse(pages.expired_page(), status_code=410)
        try:
            pending.client.resume_login(pending.client_state, code.strip())
        except Exception:  # noqa: BLE001 - any failure here means "start over"
            # A rejected code also clears Garmin's MFA state, so this session is
            # spent either way — the page says to start over.
            logger.info("An MFA code was rejected.")
            return HTMLResponse(pages.expired_page(), status_code=401)
        return finish(pending.client)

    return app


def main() -> None:
    """Run the onboarding app (`garmin-mcp-onboarding`)."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="garmin-mcp-onboarding", description="Self-service Garmin connector onboarding"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
