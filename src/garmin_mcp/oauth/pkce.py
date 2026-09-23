"""PKCE (RFC 7636 S256) helpers for Garmin's OAuth 2.0 flow."""
from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass


def generate_code_verifier(nbytes: int = 64) -> str:
    """Return a high-entropy code_verifier (urlsafe, within 43–128 chars)."""
    # token_urlsafe(64) → ~86 chars, comfortably inside the RFC range.
    return secrets.token_urlsafe(nbytes)


def code_challenge_s256(code_verifier: str) -> str:
    """base64url(sha256(verifier)) without padding."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_state() -> str:
    """Opaque CSRF state for the authorize → callback round-trip."""
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class PkcePair:
    code_verifier: str
    code_challenge: str
    state: str


def new_pkce_pair() -> PkcePair:
    verifier = generate_code_verifier()
    return PkcePair(
        code_verifier=verifier,
        code_challenge=code_challenge_s256(verifier),
        state=generate_state(),
    )
