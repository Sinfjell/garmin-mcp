"""Ping/Push notification intake: spool, acknowledge, then process.

Garmin requires HTTP 200 within 30 seconds for payloads up to 100 MB, and
expects processing to happen *after* the acknowledgement. The HTTP handler
therefore only streams the body to a spool file on disk; a single background
worker parses and applies it. Spool files survive a restart and are drained
when the process starts, so an acknowledged delivery is never lost silently.

Every notification names a Garmin ``userId``. It is mapped to a local user
through the token store's Garmin index; unknown users are skipped. Webhooks
carry no signature, so nothing destructive is done on the payload's word:

- **Deregistration** deletes a user only once Garmin itself rejects that
  user's tokens.
- **Permission changes** read the current permissions back from Garmin and
  act on those, not on the list in the payload.
- **Ping** callback URLs are only followed on Garmin's API host.
"""
from __future__ import annotations

import functools
import json
import logging
import secrets
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from garmin_mcp.oauth.client import OfficialGarminClient
from garmin_mcp.oauth.config import OAuthConfig
from garmin_mcp.oauth.datastore import (
    ACTIVITY_PERMISSION,
    ACTIVITY_TYPES,
    HEALTH_PERMISSION,
    HEALTH_TYPES,
    SUMMARY_TYPES,
    permission_for,
)
from garmin_mcp.oauth.tokens import TokenStore

log = logging.getLogger(__name__)

# Garmin's floor is 10 MB (100 MB for activity data); anything past this cap is refused.
MAX_BODY_BYTES = 128 * 1024 * 1024

# FIT files arrive as a Ping to a binary download. The tools do not use them,
# so they are acknowledged and not fetched.
_SKIPPED_TYPES = frozenset({"activityFiles"})

# History requested right after consent, so a new user's assistant has
# something to read before their next device sync.
BACKFILL_DAYS = 30
BACKFILL_TYPES = ("activities", "dailies", "sleeps", "stressDetails", "hrv", "userMetrics")

ClientFactory = Callable[[str], OfficialGarminClient]


class WebhookInbox:
    """Spool directory for acknowledged-but-unprocessed notifications."""

    def __init__(self, token_root: Path):
        self.dir = Path(token_root) / ".inbox"
        self.failed_dir = self.dir / "failed"

    def ensure(self) -> None:
        for d in (self.dir, self.failed_dir):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)

    def new_partial(self) -> Path:
        self.ensure()
        return self.dir / f"{time.time_ns()}-{secrets.token_hex(4)}.partial"

    def commit(self, partial: Path) -> Path:
        final = partial.with_suffix(".json")
        partial.replace(final)
        return final

    def pending(self) -> list[Path]:
        if not self.dir.is_dir():
            return []
        return sorted(self.dir.glob("*.json"))

    def discard_partials(self) -> None:
        if self.dir.is_dir():
            for p in self.dir.glob("*.partial"):
                p.unlink(missing_ok=True)

    def fail(self, path: Path) -> None:
        self.ensure()
        path.replace(self.failed_dir / path.name)


class NotificationProcessor:
    """Applies one notification payload to the token and summary stores."""

    def __init__(
        self,
        config: OAuthConfig,
        tokens: TokenStore,
        *,
        client_factory: ClientFactory | None = None,
        on_user_deleted: Callable[[str], None] | None = None,
    ):
        self._config = config
        self._tokens = tokens
        self._client_factory = client_factory or (lambda uid: OfficialGarminClient(config, tokens, uid))
        self._on_user_deleted = on_user_deleted

    def process_file(self, path: Path, inbox: WebhookInbox) -> None:
        """Apply one spooled notification. Anything that failed stays in ``failed/``.

        Every write is an idempotent upsert or delete, so replaying a failed
        file after a fix is safe.
        """
        try:
            failures = self.process(json.loads(path.read_bytes()))["failed"]
        except Exception as exc:  # noqa: BLE001 - log the type only
            log.error("webhook processing failed: %s", type(exc).__name__)
            failures = 1
        if failures:
            inbox.fail(path)
        else:
            path.unlink(missing_ok=True)

    def process(self, payload: Any) -> dict[str, int]:
        """Apply every section of a notification. Returns per-section counts.

        One notification can carry many users; one user's failure is counted
        in ``failed`` and does not stop the others.
        """
        if not isinstance(payload, dict):
            raise TypeError("notification body is not a JSON object")
        counts: dict[str, int] = {"failed": 0}
        for key, items in payload.items():
            if not isinstance(items, list):
                continue
            if key == "deregistrations":
                handler = self._deregister
            elif key == "userPermissionsChange":
                handler = self._permissions_changed
            elif key in SUMMARY_TYPES and key not in _SKIPPED_TYPES:
                handler = functools.partial(self._summary, key)
            else:
                continue
            counts[key] = 0
            for item in items:
                try:
                    counts[key] += handler(item)
                except Exception as exc:  # noqa: BLE001 - log the type only
                    log.error("webhook item failed in %s: %s", key, type(exc).__name__)
                    counts["failed"] += 1
        return counts

    def _local_user(self, item: Any) -> str | None:
        if not isinstance(item, dict) or item.get("userId") in (None, ""):
            return None
        return self._tokens.lookup_by_garmin_user_id(str(item["userId"]))

    def _with_client(self, user_id: str, fn: Callable[[OfficialGarminClient], int]) -> int:
        client = self._client_factory(user_id)
        try:
            return fn(client)
        finally:
            client.close()

    def _deregister(self, item: Any) -> int:
        user_id = self._local_user(item)
        if user_id is None:
            return 0
        if self._with_client(user_id, lambda c: int(c.registration_active())):
            log.warning("deregistration ignored: Garmin still accepts the user's tokens")
            return 0
        self._tokens.delete_user(user_id)
        if self._on_user_deleted is not None:
            self._on_user_deleted(user_id)
        return 1

    def _permissions_changed(self, item: Any) -> int:
        user_id = self._local_user(item)
        if user_id is None:
            return 0

        def apply(client: OfficialGarminClient) -> int:
            permissions = client.current_permissions()
            bundle = self._tokens.load_tokens(user_id)
            bundle.permissions = permissions
            self._tokens.save_tokens(user_id, bundle)
            withdrawn: set[str] = set()
            if ACTIVITY_PERMISSION not in permissions:
                withdrawn |= ACTIVITY_TYPES
            if HEALTH_PERMISSION not in permissions:
                withdrawn |= HEALTH_TYPES
            client.data.purge(withdrawn)
            return 1

        return self._with_client(user_id, apply)

    def _summary(self, summary_type: str, item: Any) -> int:
        user_id = self._local_user(item)
        if user_id is None:
            return 0
        permissions = self._tokens.load_tokens(user_id).permissions
        if permissions is not None and permission_for(summary_type) not in permissions:
            return 0
        callback = item.get("callbackURL")

        def store(client: OfficialGarminClient) -> int:
            summaries = client.fetch_callback(str(callback)) if callback else [item]
            return client.data.put(summary_type, summaries)

        return self._with_client(user_id, store)


class WebhookWorker:
    """One background thread applying spooled notifications in arrival order."""

    def __init__(self, inbox: WebhookInbox, processor: NotificationProcessor):
        self.inbox = inbox
        self.processor = processor
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="garmin-webhooks")

    def submit(self, path: Path) -> None:
        self._pool.submit(self.processor.process_file, path, self.inbox)

    def drain_on_start(self) -> None:
        """Re-queue deliveries acknowledged before the last shutdown."""
        self.inbox.discard_partials()
        for path in self.inbox.pending():
            self.submit(path)

    def submit_task(self, fn: Callable[[], None]) -> None:
        self._pool.submit(_log_failures, fn)

    def wait(self) -> None:
        """Block until everything queued so far has run (tests and shutdown)."""
        self._pool.submit(lambda: None).result()


def _log_failures(fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - background task; log the type only
        log.error("background task failed: %s", type(exc).__name__)


def request_initial_backfill(client: OfficialGarminClient, permissions: list[str] | None) -> None:
    """Ask Garmin to redeliver the last BACKFILL_DAYS of the types the user shared."""
    end = int(time.time())
    start = end - BACKFILL_DAYS * 24 * 60 * 60
    for summary_type in BACKFILL_TYPES:
        if permissions is not None and permission_for(summary_type) not in permissions:
            continue
        try:
            client.request_backfill(summary_type, start, end)
        except Exception as exc:  # noqa: BLE001 - one type failing must not stop the rest
            log.warning("backfill request failed for %s: %s", summary_type, type(exc).__name__)
