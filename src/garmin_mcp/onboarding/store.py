"""Token stores and in-flight login sessions for the onboarding app.

Two pieces of state live here:

* the **token store** each finished onboarding writes, named by the user ID
  that will appear in that person's connector URL, and
* the **pending logins** waiting on an MFA code, held in memory only.

A pending login never holds the password. `garminconnect` consumes it during
the first login call and `resume_login()` works off MFA state on the client
object, so the password is dropped before the MFA wait begins rather than
being carried across it.
"""
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from garmin_mcp import multitenant

# 20 bytes -> 40 hex chars: 160 bits of entropy, and [0-9a-f] satisfies the
# server's [a-z0-9-]{32,128} route pattern. The ID is the only thing standing
# between a stranger and someone's Garmin data, so this must stay >= 128 bits.
_USER_ID_BYTES = 20

# How long someone has to type the code Garmin texted them before the
# half-finished login is dropped.
DEFAULT_SESSION_TTL_SECONDS = 300


def new_user_id() -> str:
    """A fresh, unguessable user ID for one person's connector URL."""
    return secrets.token_hex(_USER_ID_BYTES)


def write_token_store(client: Any, root: Path, user_id: str) -> Path:
    """Persist a logged-in client's tokens as this user's token store.

    garminconnect writes the token file itself, 0o600 inside a 0o700
    directory — it holds a refresh token, which is as good as the account.
    """
    store = multitenant.user_store_path(root, user_id)
    client.client.dump(str(store))
    return store


def delete_token_store(root: Path, user_id: str) -> bool:
    """Remove one user's token store. Returns False if there was nothing to remove.

    Validates the ID rather than trusting the caller: this deletes a directory
    tree, and an unvalidated ID is a path-traversal delete.
    """
    if not multitenant.is_valid_user_id(user_id):
        raise ValueError(f"Not a valid user ID: {user_id!r}")
    store = multitenant.user_store_path(root, user_id)
    if not store.is_dir():
        return False
    shutil.rmtree(store)
    return True


def list_user_ids(root: Path) -> list[str]:
    """Every user ID with a token store under `root`, sorted."""
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and multitenant.is_valid_user_id(p.name))


@dataclass
class PendingLogin:
    """A login that got as far as Garmin asking for an MFA code.

    Deliberately holds no password and no email — only the client object that
    already carries Garmin's MFA state.
    """

    client: Any
    client_state: Any
    created_at: float = field(default_factory=time.monotonic)


class SessionStore:
    """In-memory pending logins, expired on read. Never touches disk."""

    def __init__(self, ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS, clock=time.monotonic):
        self._ttl = ttl_seconds
        self._clock = clock
        self._sessions: dict[str, PendingLogin] = {}

    def _purge(self) -> None:
        now = self._clock()
        for key in [k for k, s in self._sessions.items() if now - s.created_at > self._ttl]:
            del self._sessions[key]

    def add(self, client: Any, client_state: Any) -> str:
        self._purge()
        session_id = secrets.token_urlsafe(24)
        self._sessions[session_id] = PendingLogin(
            client=client, client_state=client_state, created_at=self._clock()
        )
        return session_id

    def pop(self, session_id: str) -> PendingLogin | None:
        """Take a session out of the store. One code attempt per session id."""
        self._purge()
        return self._sessions.pop(session_id, None)

    def __len__(self) -> int:
        self._purge()
        return len(self._sessions)
