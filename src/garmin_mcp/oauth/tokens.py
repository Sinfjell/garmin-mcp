"""File-based OAuth token store (EU-local disk; no cloud DB dependency).

Layout under ``GARMIN_OAUTH_TOKEN_ROOT``::

    <root>/<user-id>/tokens.json     # access + refresh + garmin user id
    <root>/.pending/<state>.json     # PKCE verifier awaiting callback
    <root>/.by-garmin/<garmin-id>    # symlink or marker file → user-id
    <root>/<user-id>/summaries.sqlite3  # Ping/Push data (see datastore.py)

Permissions: directories 0o700, token files 0o600. A password never appears here —
only OAuth tokens issued by Garmin after consent.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from garmin_mcp import multitenant
from garmin_mcp.oauth.config import TOKEN_REFRESH_LEEWAY_SECONDS

_USER_ID_BYTES = 20
_PENDING_TTL_SECONDS = 900  # 15 minutes for the authorize → callback round-trip
_TOKENS_FILENAME = "tokens.json"


def new_user_id() -> str:
    """Unguessable local tenant ID (same entropy floor as session multi-tenant)."""
    return secrets.token_hex(_USER_ID_BYTES)


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str
    expires_at: float  # unix epoch when access_token expires
    refresh_expires_at: float | None
    garmin_user_id: str
    scope: str | None = None
    permissions: list[str] | None = None

    def access_expired(self, *, now: float | None = None, leeway: int = TOKEN_REFRESH_LEEWAY_SECONDS) -> bool:
        now = time.time() if now is None else now
        return now >= (self.expires_at - leeway)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TokenBundle:
        return cls(
            access_token=str(data["access_token"]),
            refresh_token=str(data["refresh_token"]),
            expires_at=float(data["expires_at"]),
            refresh_expires_at=(float(data["refresh_expires_at"]) if data.get("refresh_expires_at") is not None else None),
            garmin_user_id=str(data.get("garmin_user_id") or ""),
            scope=data.get("scope"),
            permissions=list(data["permissions"]) if data.get("permissions") else None,
        )


@dataclass
class PendingAuth:
    state: str
    code_verifier: str
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingAuth:
        return cls(
            state=str(data["state"]),
            code_verifier=str(data["code_verifier"]),
            created_at=float(data["created_at"]),
        )


class TokenStore:
    """Per-tenant OAuth token files under a root directory."""

    def __init__(self, root: Path):
        self.root = root
        self.pending_dir = root / ".pending"
        self.by_garmin_dir = root / ".by-garmin"

    def ensure_root(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def user_dir(self, user_id: str) -> Path:
        if not multitenant.is_valid_user_id(user_id):
            raise ValueError(f"Not a valid user ID: {user_id!r}")
        return self.root / user_id

    def tokens_path(self, user_id: str) -> Path:
        return self.user_dir(user_id) / _TOKENS_FILENAME

    def resolve_existing(self, user_id: str) -> Path | None:
        """Return the user token directory if it exists and looks populated."""
        if not multitenant.is_valid_user_id(user_id):
            return None
        path = self.user_dir(user_id)
        if path.is_dir() and self.tokens_path(user_id).is_file():
            return path
        return None

    def save_tokens(self, user_id: str, bundle: TokenBundle) -> Path:
        self.ensure_root()
        directory = self.user_dir(user_id)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        path = self.tokens_path(user_id)
        tmp = path.with_suffix(".tmp")
        data = json.dumps(bundle.to_dict(), separators=(",", ":"))
        tmp.write_text(data, encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        if bundle.garmin_user_id:
            self._index_garmin_user(bundle.garmin_user_id, user_id)
        return path

    def load_tokens(self, user_id: str) -> TokenBundle:
        path = self.tokens_path(user_id)
        raw = json.loads(path.read_text(encoding="utf-8"))
        return TokenBundle.from_dict(raw)

    def save_pending(self, pending: PendingAuth) -> None:
        self.ensure_root()
        self.pending_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.pending_dir, 0o700)
        path = self.pending_dir / f"{pending.state}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(pending.to_dict()), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    def take_pending(self, state: str, *, now: float | None = None) -> PendingAuth | None:
        """Load and delete a pending PKCE state, or None if missing/expired/invalid."""
        if not state or "/" in state or ".." in state or len(state) > 200:
            return None
        path = self.pending_dir / f"{state}.json"
        if not path.is_file():
            return None
        try:
            pending = PendingAuth.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            path.unlink(missing_ok=True)
            return None
        path.unlink(missing_ok=True)
        now = time.time() if now is None else now
        if now - pending.created_at > _PENDING_TTL_SECONDS:
            return None
        if pending.state != state:
            return None
        return pending

    def lookup_by_garmin_user_id(self, garmin_user_id: str) -> str | None:
        if not garmin_user_id or "/" in garmin_user_id or ".." in garmin_user_id:
            return None
        marker = self.by_garmin_dir / garmin_user_id
        if not marker.is_file():
            return None
        try:
            user_id = marker.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return user_id if self.resolve_existing(user_id) else None

    def _index_garmin_user(self, garmin_user_id: str, user_id: str) -> None:
        if "/" in garmin_user_id or ".." in garmin_user_id:
            return
        self.by_garmin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.by_garmin_dir, 0o700)
        marker = self.by_garmin_dir / garmin_user_id
        tmp = marker.with_suffix(".tmp")
        tmp.write_text(user_id, encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(marker)

    def delete_user(self, user_id: str) -> bool:
        """Remove a user's tokens, stored summaries, and Garmin-ID index entry.

        Used on Garmin deregistration: nothing about the user may remain.
        Returns False when no such user exists.
        """
        directory = self.resolve_existing(user_id)
        if directory is None:
            return False
        try:
            garmin_user_id = self.load_tokens(user_id).garmin_user_id
        except (OSError, ValueError, KeyError):
            garmin_user_id = ""
        shutil.rmtree(directory)
        if garmin_user_id and "/" not in garmin_user_id and ".." not in garmin_user_id:
            (self.by_garmin_dir / garmin_user_id).unlink(missing_ok=True)
        return True
