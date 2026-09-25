"""MCP authorization server in front of Garmin's OAuth.

Every user adds the same connector URL, ``<public-base><prefix>/mcp``, to
their AI client. The client discovers this server through the MCP
authorization spec (RFC 9728 protected-resource metadata, RFC 8414 server
metadata, RFC 7591 dynamic client registration) and runs OAuth 2.1 with PKCE
against it. Authorizing means: our consent page, then Garmin's consent page,
then back to the client with a code for *our* token. The Garmin tokens never
leave the server.

The bearer token replaces the per-user secret URL: nothing that identifies a
user appears in a URL, so nothing leaks through logs, screenshots or chat.

Only SHA-256 hashes of issued codes and tokens are stored, in
``<token-root>/.mcp-auth.sqlite3`` (0600).
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from garmin_mcp.oauth.tokens import TokenStore

ACCESS_TOKEN_TTL = 60 * 60
REFRESH_TOKEN_TTL = 90 * 24 * 60 * 60
AUTH_CODE_TTL = 5 * 60
REQUEST_TTL = 15 * 60
_DB_FILENAME = ".mcp-auth.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, info TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, params TEXT NOT NULL,
    browser_hash TEXT NOT NULL, garmin_state TEXT UNIQUE, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS codes (
    code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, user_id TEXT NOT NULL,
    data TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, client_id TEXT NOT NULL,
    user_id TEXT NOT NULL, grant_id TEXT NOT NULL, expires_at REAL NOT NULL, scopes TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tokens_by_user ON tokens (user_id);
CREATE INDEX IF NOT EXISTS tokens_by_grant ON tokens (grant_id);
"""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class PendingRequest:
    """An MCP client's authorization request waiting for the user and Garmin."""

    def __init__(self, request_id: str, client_id: str, params: AuthorizationParams, browser_hash: str):
        self.request_id = request_id
        self.client_id = client_id
        self.params = params
        self._browser_hash = browser_hash

    def same_browser(self, browser_token: str | None) -> bool:
        """True when the consent POST comes from the browser that loaded the page."""
        return bool(browser_token) and secrets.compare_digest(_hash(browser_token), self._browser_hash)

    def redirect(self, **query: str | None) -> str:
        """URL back to the MCP client, carrying its own ``state``."""
        return construct_redirect_uri(str(self.params.redirect_uri), state=self.params.state, **query)


class GarminAuthProvider:
    """``OAuthAuthorizationServerProvider`` backed by SQLite; users are token-store IDs."""

    def __init__(self, token_root: Path, consent_url: str, tokens: TokenStore):
        self._path = Path(token_root) / _DB_FILENAME
        self._consent_url = consent_url
        self._tokens = tokens
        Path(token_root).mkdir(mode=0o700, parents=True, exist_ok=True)
        new = not self._path.exists()
        with closing(sqlite3.connect(self._path, timeout=30)) as conn:
            if new:
                os.chmod(self._path, 0o600)
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=30)

    # --- Clients (RFC 7591) --------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT info FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO clients VALUES (?, ?)",
                (client_info.client_id, client_info.model_dump_json()),
            )

    # --- Authorization ---------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Park the request and send the user to our consent page first."""
        request_id = secrets.token_urlsafe(32)
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM requests WHERE created_at < ?", (time.time() - REQUEST_TTL,))
            conn.execute("DELETE FROM codes WHERE expires_at < ?", (time.time(),))
            conn.execute(
                "INSERT INTO requests VALUES (?, ?, ?, '', NULL, ?)",
                (request_id, client.client_id, params.model_dump_json(), time.time()),
            )
        return construct_redirect_uri(self._consent_url, request=request_id)

    def bind_browser(self, request_id: str) -> str:
        """New per-browser token for the consent form; only the browser holding it may approve.

        Stops a forged POST from approving someone else's parked request
        (consent CSRF): the attacker knows the request ID, not the cookie.
        """
        browser_token = secrets.token_urlsafe(32)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE requests SET browser_hash = ? WHERE request_id = ?", (_hash(browser_token), request_id)
            )
        return browser_token

    def load_request(self, request_id: str) -> PendingRequest | None:
        return self._request_where("request_id", request_id)

    def attach_garmin_state(self, request_id: str, garmin_state: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE requests SET garmin_state = ? WHERE request_id = ?", (garmin_state, request_id))

    def take_request_by_garmin_state(self, garmin_state: str) -> PendingRequest | None:
        pending = self._request_where("garmin_state", garmin_state)
        if pending is not None:
            self.drop_request(pending.request_id)
        return pending

    def drop_request(self, request_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM requests WHERE request_id = ?", (request_id,))

    def _request_where(self, column: str, value: str) -> PendingRequest | None:
        if not value or column not in ("request_id", "garmin_state"):
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT request_id, client_id, params, created_at, browser_hash FROM requests "
                f"WHERE {column} = ?",
                (value,),
            ).fetchone()
        if row is None or time.time() - row[3] > REQUEST_TTL:
            return None
        return PendingRequest(row[0], row[1], AuthorizationParams.model_validate_json(row[2]), row[4])

    def issue_code(self, pending: PendingRequest, user_id: str) -> str:
        """Authorization code for the MCP client, bound to one local user."""
        code = secrets.token_urlsafe(32)
        auth_code = AuthorizationCode(
            code=code,
            scopes=pending.params.scopes or [],
            expires_at=time.time() + AUTH_CODE_TTL,
            client_id=pending.client_id,
            code_challenge=pending.params.code_challenge,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            resource=pending.params.resource,
            subject=user_id,
        )
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO codes VALUES (?, ?, ?, ?, ?)",
                # The code itself is not stored, only its hash (the lookup key).
                (_hash(code), pending.client_id, user_id, auth_code.model_copy(update={"code": ""}).model_dump_json(),
                 auth_code.expires_at),
            )
        return code

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT data FROM codes WHERE code_hash = ? AND client_id = ?",
                (_hash(authorization_code), client.client_id),
            ).fetchone()
        if row is None:
            return None
        code = AuthorizationCode.model_validate_json(row[0])
        return code.model_copy(update={"code": authorization_code})

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        with closing(self._connect()) as conn, conn:
            deleted = conn.execute(
                "DELETE FROM codes WHERE code_hash = ?", (_hash(authorization_code.code),)
            ).rowcount
        if not deleted:  # a code is single-use, even under a race
            raise TokenError("invalid_grant", "authorization code already used")
        return self._issue_tokens(client.client_id, str(authorization_code.subject), authorization_code.scopes)

    # --- Tokens ------------------------------------------------------------

    def _issue_tokens(self, client_id: str, user_id: str, scopes: list[str], grant_id: str | None = None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        grant_id = grant_id or secrets.token_hex(16)
        now = time.time()
        scope_text = " ".join(scopes)
        with closing(self._connect()) as conn, conn:
            conn.executemany(
                "INSERT INTO tokens VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (_hash(access), "access", client_id, user_id, grant_id, now + ACCESS_TOKEN_TTL, scope_text),
                    (_hash(refresh), "refresh", client_id, user_id, grant_id, now + REFRESH_TOKEN_TTL, scope_text),
                ],
            )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            refresh_token=refresh,
            scope=" ".join(scopes) or None,
        )

    def _token_row(self, token: str, kind: str) -> tuple[str, str, str, float, str] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT client_id, user_id, grant_id, expires_at, scopes FROM tokens "
                "WHERE token_hash = ? AND kind = ?",
                (_hash(token), kind),
            ).fetchone()
        if row is None or row[3] < time.time():
            return None
        # A user deleted by deregistration has no token directory left.
        if self._tokens.resolve_existing(row[1]) is None:
            return None
        return row

    async def load_access_token(self, token: str) -> AccessToken | None:
        return self.lookup_access_token(token)

    def lookup_access_token(self, token: str) -> AccessToken | None:
        """Synchronous form, for callers that run it in a worker thread."""
        row = self._token_row(token, "access")
        if row is None:
            return None
        return AccessToken(
            token=token, client_id=row[0], scopes=row[4].split(), expires_at=int(row[3]), subject=row[1]
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = self._token_row(refresh_token, "refresh")
        if row is None or row[0] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token, client_id=row[0], scopes=row[4].split(), expires_at=int(row[3]), subject=row[1]
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        """Rotate: the old refresh token and its grant's access tokens stop working.

        Claiming the refresh token is one DELETE, so two concurrent refreshes
        with the same token cannot both succeed.
        """
        row = self._token_row(refresh_token.token, "refresh")
        if row is None:
            raise TokenError("invalid_grant", "refresh token is not valid")
        with closing(self._connect()) as conn, conn:
            claimed = conn.execute(
                "DELETE FROM tokens WHERE token_hash = ? AND kind = 'refresh'", (_hash(refresh_token.token),)
            ).rowcount
            conn.execute("DELETE FROM tokens WHERE grant_id = ?", (row[2],))
        if not claimed:
            raise TokenError("invalid_grant", "refresh token already used")
        return self._issue_tokens(client.client_id, row[1], scopes or row[4].split())

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT grant_id FROM tokens WHERE token_hash = ?", (_hash(token.token),)).fetchone()
        if row is not None:
            self._revoke_grant(row[0])

    def _revoke_grant(self, grant_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM tokens WHERE grant_id = ?", (grant_id,))

    def revoke_user(self, user_id: str) -> None:
        """Drop every token and pending code for a user (deregistration)."""
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM tokens WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM codes WHERE user_id = ?", (user_id,))

