"""Last line of defence: keep the in-flight password out of every log record.

The onboarding code itself never logs a credential — it logs exception *types*,
not messages. But it runs third-party code (garminconnect, its HTTP stack,
uvicorn) that logs on the same login path and is not bound by that rule, and a
failing HTTP client is exactly the kind of thing that echoes the request it
sent. The request carried the password.

So rather than auditing every library on every upgrade, this redacts the one
string that must not appear, wherever it appears. It is deliberately the
*second* line of defence: the first is not putting it there.
"""
import logging
from contextvars import ContextVar

REDACTED = "***"

# The password being processed by the current request, if any. Set for the
# duration of the login call and cleared immediately afterwards.
_in_flight_password: ContextVar[str | None] = ContextVar("garmin_in_flight_password", default=None)


def hold_password(password: str):
    """Bind a password as in-flight so log records mentioning it get scrubbed."""
    return _in_flight_password.set(password)


def release_password(token) -> None:
    _in_flight_password.reset(token)


class CredentialScrubbingFilter(logging.Filter):
    """Redact the in-flight password from a record's message and exception."""

    def filter(self, record: logging.LogRecord) -> bool:
        secret = _in_flight_password.get()
        if not secret:
            return True

        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format string must not break logging
            message = str(record.msg)

        exception_text = ""
        if record.exc_info and record.exc_info[1] is not None:
            exception_text = str(record.exc_info[1])

        if secret in message or secret in exception_text:
            record.msg = message.replace(secret, REDACTED)
            record.args = ()
            # The traceback would carry the same text through a different field,
            # so it goes rather than being rewritten line by line.
            record.exc_info = None
            record.exc_text = None
        return True


def install(logger: logging.Logger | None = None) -> CredentialScrubbingFilter:
    """Attach the filter to a logger's handlers (the root logger by default).

    Handlers, not the logger: a filter on a logger only sees records logged
    directly to it, while a filter on a handler sees everything that handler
    emits — including records propagating up from garminconnect and uvicorn.
    """
    logger = logger if logger is not None else logging.getLogger()
    scrubber = CredentialScrubbingFilter()
    for handler in logger.handlers:
        handler.addFilter(scrubber)
    return scrubber
