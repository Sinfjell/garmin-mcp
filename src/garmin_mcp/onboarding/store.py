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
import threading
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


# What garminconnect's dump() leaves behind. Used to recognise a directory
# someone hands us as a token store, rather than trusting the path.
_TOKEN_FILE_SUFFIX = ".json"


def looks_like_token_store(source: Path) -> bool:
    """Whether a directory looks like something garminconnect wrote."""
    return source.is_dir() and any(
        p.is_file() and p.name.endswith(_TOKEN_FILE_SUFFIX) for p in source.iterdir()
    )


def import_token_store(source: Path, root: Path, user_id: str | None = None) -> str:
    """Adopt an existing ~/.garminconnect directory as a new tenant.

    The path that matters when Garmin refuses to let a server log in: the person
    authenticates on their own machine, where their IP is not blocked and their
    password never leaves, and only the resulting token comes here.

    Returns the new user ID. Copies rather than moves, so a mistake costs
    nothing, and clamps permissions afterwards — the token is the account.
    """
    if not looks_like_token_store(source):
        raise ValueError(f"{source} does not look like a Garmin token store (no .json files).")
    user_id = user_id or new_user_id()
    if not multitenant.is_valid_user_id(user_id):
        raise ValueError(f"Not a valid user ID: {user_id!r}")

    destination = multitenant.user_store_path(root, user_id)
    if destination.exists():
        raise ValueError(f"A token store already exists for {user_id}.")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copytree(source, destination)

    destination.chmod(0o700)
    for path in destination.rglob("*"):
        if path.is_file():
            path.chmod(0o600)
    return user_id


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
    """In-memory pending logins, expired on read. Never touches disk.

    Guarded by a lock: FastAPI runs synchronous endpoints in a worker
    threadpool, so two people finishing their MFA at the same moment really do
    hit this concurrently, and an unguarded purge-then-delete raises KeyError
    on the loser.
    """

    def __init__(self, ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS, clock=time.monotonic):
        self._ttl = ttl_seconds
        self._clock = clock
        self._sessions: dict[str, PendingLogin] = {}
        self._lock = threading.Lock()

    def _purge_locked(self) -> None:
        now = self._clock()
        for key in [k for k, s in self._sessions.items() if now - s.created_at > self._ttl]:
            self._sessions.pop(key, None)

    def add(self, client: Any, client_state: Any) -> str:
        session_id = secrets.token_urlsafe(24)
        with self._lock:
            self._purge_locked()
            self._sessions[session_id] = PendingLogin(
                client=client, client_state=client_state, created_at=self._clock()
            )
        return session_id

    def pop(self, session_id: str) -> PendingLogin | None:
        """Take a session out of the store. One code attempt per session id.

        Removing before validating is deliberate: a rejected code also clears
        Garmin's own MFA state, so the session is spent either way, and it means
        two concurrent posts of the same id cannot both proceed.
        """
        with self._lock:
            self._purge_locked()
            return self._sessions.pop(session_id, None)

    def __len__(self) -> int:
        with self._lock:
            self._purge_locked()
            return len(self._sessions)
