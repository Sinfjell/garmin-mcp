"""Errors specific to the official OAuth / Health+Activity path."""


class OAuthError(Exception):
    """Base for OAuth flow failures (never include secrets in the message)."""


class StateMismatchError(OAuthError):
    """CSRF ``state`` from the callback did not match a pending verifier."""


class TokenExchangeError(OAuthError):
    """Token endpoint returned a non-success status."""

    def __init__(self, status_code: int, error_code: str | None = None):
        # Do not embed the response body: it can echo form fields. Only the
        # RFC 6749 error code (e.g. "invalid_grant") is kept.
        super().__init__(f"token endpoint returned HTTP {status_code}")
        self.status_code = status_code
        self.error_code = error_code

    @property
    def grant_refused(self) -> bool:
        """Garmin refused the grant itself (revoked/expired), not our client credentials."""
        if self.error_code == "invalid_grant":
            return True
        return self.status_code == 400 and self.error_code not in ("invalid_client", "unauthorized_client")


class OfficialApiUnavailableError(Exception):
    """Raised when a tool needs data the official APIs do not expose.

    Training status, lactate threshold, and personal records are confirmed
    unavailable on Health/Activity (Garmin ticket 224965). Callers must surface
    this as a clear JSON error — never invent substitute numbers.
    """

    def __init__(self, feature: str):
        self.feature = feature
        super().__init__(
            f"{feature} is not available via the official Garmin Connect "
            "Developer API (Health/Activity only). Do not claim this metric "
            "in oauth mode; use GARMIN_AUTH_MODE=session for unofficial "
            "Connect session data, or omit the claim."
        )


class GarminApiError(Exception):
    """Wellness API returned a non-success status (body deliberately not kept)."""

    def __init__(self, status_code: int):
        super().__init__(f"Garmin wellness API HTTP {status_code}")
        self.status_code = status_code
