"""Errors specific to the official OAuth / Health+Activity path."""


class OAuthError(Exception):
    """Base for OAuth flow failures (never include secrets in the message)."""


class StateMismatchError(OAuthError):
    """CSRF ``state`` from the callback did not match a pending verifier."""


class TokenExchangeError(OAuthError):
    """Token endpoint returned a non-success status."""

    def __init__(self, status_code: int, error_code: str | None = None):
        # Do not embed the response body: it can echo form fields.
        self.status_code = status_code
        self.error_code = error_code
        if error_code:
            super().__init__(
                f"token endpoint returned HTTP {status_code} error={error_code}"
            )
        else:
            super().__init__(f"token endpoint returned HTTP {status_code}")


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
