"""Authorize URL, code exchange, and refresh for Garmin OAuth 2.0 PKCE."""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx

from garmin_mcp.oauth.config import (
    OAUTH_AUTHORIZATION_URL,
    OAUTH_TOKEN_URL,
    OAuthConfig,
)
from garmin_mcp.oauth.errors import StateMismatchError, TokenExchangeError
from garmin_mcp.oauth.pkce import PkcePair, new_pkce_pair
from garmin_mcp.oauth.tokens import PendingAuth, TokenBundle, TokenStore, new_user_id


def build_authorization_url(config: OAuthConfig, store: TokenStore) -> tuple[str, PkcePair]:
    """Create a consent URL and persist the PKCE verifier keyed by ``state``."""
    pair = new_pkce_pair()
    store.save_pending(
        PendingAuth(state=pair.state, code_verifier=pair.code_verifier, created_at=time.time())
    )
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "code_challenge": pair.code_challenge,
        "code_challenge_method": "S256",
        "redirect_uri": config.redirect_uri,
        "state": pair.state,
    }
    return f"{OAUTH_AUTHORIZATION_URL}?{urlencode(params)}", pair


def _token_request(config: OAuthConfig, data: dict[str, str], *, http: httpx.Client | None = None) -> dict[str, Any]:
    client = http or httpx.Client(timeout=30.0)
    owns = http is None
    try:
        response = client.post(
            OAUTH_TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    finally:
        if owns:
            client.close()
    if response.status_code >= 400:
        # Capture only OAuth error *codes* (never the body — it can echo form fields).
        err_code = None
        try:
            payload = response.json()
            if isinstance(payload, dict):
                raw = payload.get("error")
                if isinstance(raw, str) and raw.isascii() and len(raw) <= 64:
                    err_code = raw
        except Exception:
            err_code = None
        raise TokenExchangeError(response.status_code, error_code=err_code)
    return response.json()


def _bundle_from_token_response(
    payload: dict[str, Any],
    *,
    garmin_user_id: str = "",
    permissions: list[str] | None = None,
    now: float | None = None,
) -> TokenBundle:
    now = time.time() if now is None else now
    expires_in = int(payload.get("expires_in") or 0)
    refresh_expires_in = payload.get("refresh_token_expires_in")
    return TokenBundle(
        access_token=str(payload["access_token"]),
        refresh_token=str(payload["refresh_token"]),
        expires_at=now + expires_in,
        refresh_expires_at=(now + int(refresh_expires_in)) if refresh_expires_in is not None else None,
        garmin_user_id=garmin_user_id,
        scope=payload.get("scope"),
        permissions=permissions,
    )


def exchange_code(
    config: OAuthConfig,
    store: TokenStore,
    *,
    code: str,
    state: str,
    http: httpx.Client | None = None,
) -> tuple[str, TokenBundle]:
    """Validate state, exchange the code, fetch Garmin user id, persist tokens.

    Returns ``(local_user_id, token_bundle)``.
    """
    pending = store.take_pending(state)
    if pending is None:
        raise StateMismatchError("OAuth state missing, expired, or mismatched")

    payload = _token_request(
        config,
        {
            "grant_type": "authorization_code",
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "code": code,
            "code_verifier": pending.code_verifier,
            "redirect_uri": config.redirect_uri,
        },
        http=http,
    )
    # Clear verifier from memory as soon as the exchange uses it.
    del pending

    access = str(payload["access_token"])
    garmin_user_id = fetch_garmin_user_id(config, access, http=http) or ""
    permissions = fetch_permissions(config, access, http=http)

    # Prefer reusing an existing local ID when the same Garmin account reconnects.
    user_id = store.lookup_by_garmin_user_id(garmin_user_id) if garmin_user_id else None
    user_id = user_id or new_user_id()
    bundle = _bundle_from_token_response(
        payload, garmin_user_id=garmin_user_id, permissions=permissions
    )
    store.save_tokens(user_id, bundle)
    return user_id, bundle


def refresh_tokens(
    config: OAuthConfig,
    store: TokenStore,
    user_id: str,
    bundle: TokenBundle,
    *,
    http: httpx.Client | None = None,
    now: float | None = None,
) -> TokenBundle:
    """Refresh access+refresh tokens. Garmin rotates the refresh token every time."""
    payload = _token_request(
        config,
        {
            "grant_type": "refresh_token",
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "refresh_token": bundle.refresh_token,
        },
        http=http,
    )
    updated = _bundle_from_token_response(
        payload,
        garmin_user_id=bundle.garmin_user_id,
        permissions=bundle.permissions,
        now=now,
    )
    if updated.scope is None:
        updated.scope = bundle.scope
    store.save_tokens(user_id, updated)
    return updated


def fetch_garmin_user_id(
    config: OAuthConfig, access_token: str, *, http: httpx.Client | None = None
) -> str | None:
    client = http or httpx.Client(timeout=15.0)
    owns = http is None
    try:
        response = client.get(
            f"{config.wellness_base}/user/id",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    finally:
        if owns:
            client.close()
    if response.status_code >= 400:
        return None
    data = response.json()
    user_id = data.get("userId") if isinstance(data, dict) else None
    return str(user_id) if user_id else None


def fetch_permissions(
    config: OAuthConfig, access_token: str, *, http: httpx.Client | None = None
) -> list[str] | None:
    client = http or httpx.Client(timeout=15.0)
    owns = http is None
    try:
        response = client.get(
            f"{config.wellness_base}/user/permissions",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    finally:
        if owns:
            client.close()
    if response.status_code >= 400:
        return None
    payload = response.json()
    if isinstance(payload, dict):
        payload = payload.get("permissions", [])
    if not isinstance(payload, list):
        return None
    return [str(p) for p in payload]
