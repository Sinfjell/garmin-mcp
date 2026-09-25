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
import os
import secrets
import threading
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


# Items that fail (Garmin 5xx, a timeout) are retried after these delays, then parked.
RETRY_DELAYS_SECONDS = (60, 10 * 60, 60 * 60)
# Parked files hold raw health data for several users; they are not kept for long.
FAILED_RETENTION_SECONDS = 7 * 24 * 60 * 60


class WebhookInbox:
    """Spool for acknowledged-but-unprocessed notifications.

    ``.inbox/*.json`` waits for the worker, ``.inbox/retry/<attempt>-*.json``
    holds items that failed and will be tried again, ``.inbox/failed/`` holds
    items that failed every retry, for a human, for at most seven days.
    """

    def __init__(self, token_root: Path):
        self.dir = Path(token_root) / ".inbox"
        self.retry_dir = self.dir / "retry"
        self.failed_dir = self.dir / "failed"

    def ensure(self) -> None:
        for d in (self.dir, self.retry_dir, self.failed_dir):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)

    def new_partial(self) -> Path:
        self.ensure()
        return self.dir / f"{time.time_ns()}-{secrets.token_hex(4)}.partial"

    def commit(self, partial: Path) -> Path:
        final = partial.with_suffix(".json")
        partial.replace(final)
        return final

    def pending(self) -> list[Path]:
        """Queued files, plus retries left over from before a restart."""
        found: list[Path] = []
        for d in (self.dir, self.retry_dir):
            if d.is_dir():
                found += sorted(d.glob("*.json"))
        return found

    def discard_partials(self) -> None:
        if self.dir.is_dir():
            for p in self.dir.glob("*.partial"):
                p.unlink(missing_ok=True)

    def write(self, directory: Path, payload: dict, prefix: str) -> Path:
        self.ensure()
        path = directory / f"{prefix}-{time.time_ns()}-{secrets.token_hex(4)}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def expire_failed(self, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if self.failed_dir.is_dir():
            for p in self.failed_dir.glob("*.json"):
                if now - p.stat().st_mtime > FAILED_RETENTION_SECONDS:
                    p.unlink(missing_ok=True)

    def scrub_garmin_user(self, garmin_user_id: str, *, skip: Path | None = None) -> None:
        """Remove a deregistered user's items from every file still on disk."""
        for d in (self.dir, self.retry_dir, self.failed_dir):
            for path in d.glob("*.json") if d.is_dir() else []:
                if path == skip:
                    continue
                _drop_user_items(path, garmin_user_id)


def _drop_user_items(path: Path, garmin_user_id: str) -> None:
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    kept = {
        key: [i for i in items if not (isinstance(i, dict) and str(i.get("userId")) == garmin_user_id)]
        for key, items in payload.items()
        if isinstance(items, list)
    }
    kept = {k: v for k, v in kept.items() if v}
    if not kept:
        path.unlink(missing_ok=True)
    elif kept != payload:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(kept), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)


class NotificationProcessor:
    """Applies one notification payload to the token and summary stores."""

    def __init__(
        self,
        config: OAuthConfig,
        tokens: TokenStore,
        *,
        client_factory: ClientFactory | None = None,
        on_user_deleted: Callable[[str, str], None] | None = None,
    ):
        self._config = config
        self._tokens = tokens
        self._client_factory = client_factory or (lambda uid: OfficialGarminClient(config, tokens, uid))
        self._on_user_deleted = on_user_deleted

    def process(self, payload: Any) -> dict[str, list]:
        """Apply every section of a notification; return the items that failed.

        One notification can carry many users. One user's failure does not
        stop the others, and every write is an idempotent upsert or delete,
        so a failed item can simply be applied again later.
        """
        if not isinstance(payload, dict):
            raise TypeError("notification body is not a JSON object")
        failed: dict[str, list] = {}
        for key, items in payload.items():
            if not isinstance(items, list):
                continue
            if key == "deregistrations":
                groups = [[i] for i in items]
                handler: Callable[[list], None] = self._deregister
            elif key == "userPermissionsChange":
                groups = [[i] for i in items]
                handler = self._permissions_changed
            elif key in SUMMARY_TYPES and key not in _SKIPPED_TYPES:
                groups = _group_by_user(items)
                handler = functools.partial(self._summaries, key)
            else:
                continue
            for group in groups:
                try:
                    handler(group)
                except Exception as exc:  # noqa: BLE001 - log the type only
                    log.error("webhook items failed in %s: %s", key, type(exc).__name__)
                    failed.setdefault(key, []).extend(group)
        return failed

    def _local_user(self, item: Any) -> str | None:
        if not isinstance(item, dict) or item.get("userId") in (None, ""):
            return None
        return self._tokens.lookup_by_garmin_user_id(str(item["userId"]))

    def _with_client(self, user_id: str, fn: Callable[[OfficialGarminClient], Any]) -> Any:
        client = self._client_factory(user_id)
        try:
            return fn(client)
        finally:
            client.close()

    def _deregister(self, group: list) -> None:
        user_id = self._local_user(group[0])
        if user_id is None:
            return
        if self._with_client(user_id, lambda c: c.registration_active()):
            log.warning("deregistration ignored: Garmin still accepts the user's tokens")
            return
        self._tokens.delete_user(user_id)
        if self._on_user_deleted is not None:
            self._on_user_deleted(user_id, str(group[0]["userId"]))

    def _permissions_changed(self, group: list) -> None:
        user_id = self._local_user(group[0])
        if user_id is None:
            return

        def apply(client: OfficialGarminClient) -> None:
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

        self._with_client(user_id, apply)

    def _summaries(self, summary_type: str, group: list) -> None:
        """Store one user's items of one type: Push items as-is, Ping callbacks fetched."""
        user_id = self._local_user(group[0])
        if user_id is None:
            return
        permissions = self._tokens.load_tokens(user_id).permissions
        # Unknown permissions store nothing: consent fails without them, so
        # only a damaged token file gets here.
        if permissions is None or permission_for(summary_type) not in permissions:
            return

        def store(client: OfficialGarminClient) -> None:
            summaries: list[dict] = []
            for item in group:
                callback = item.get("callbackURL")
                summaries += client.fetch_callback(str(callback)) if callback else [item]
            client.data.put(summary_type, summaries)

        self._with_client(user_id, store)


def _group_by_user(items: list) -> list[list]:
    groups: dict[str, list] = {}
    for item in items:
        if isinstance(item, dict) and item.get("userId") not in (None, ""):
            groups.setdefault(str(item["userId"]), []).append(item)
    return list(groups.values())


class WebhookWorker:
    """One background thread applying spooled notifications in arrival order."""

    def __init__(self, inbox: WebhookInbox, processor: NotificationProcessor):
        self.inbox = inbox
        self.processor = processor
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="garmin-webhooks")
        self._timers: list[threading.Timer] = []

    def submit(self, path: Path) -> None:
        self._pool.submit(_log_failures, functools.partial(self._run_file, path))

    def _run_file(self, path: Path) -> None:
        attempt = _attempt_of(path)
        try:
            payload = json.loads(path.read_bytes())
            failed = self.processor.process(payload)
        except Exception as exc:  # noqa: BLE001 - unreadable file; log the type only
            log.error("webhook file unreadable: %s", type(exc).__name__)
            path.replace(self.inbox.failed_dir / path.name)
            return
        path.unlink(missing_ok=True)
        if failed:
            self._retry_later(failed, attempt)

    def _retry_later(self, failed: dict[str, list], attempt: int) -> None:
        if attempt >= len(RETRY_DELAYS_SECONDS):
            self.inbox.write(self.inbox.failed_dir, failed, "failed")
            self.inbox.expire_failed()
            return
        retry = self.inbox.write(self.inbox.retry_dir, failed, str(attempt + 1))
        timer = threading.Timer(RETRY_DELAYS_SECONDS[attempt], self.submit, args=(retry,))
        timer.daemon = True
        timer.start()
        self._timers.append(timer)

    def drain_on_start(self) -> None:
        """Re-queue deliveries and retries left from before the last shutdown."""
        self.inbox.ensure()
        self.inbox.discard_partials()
        self.inbox.expire_failed()
        for path in self.inbox.pending():
            self.submit(path)

    def submit_task(self, fn: Callable[[], None]) -> None:
        self._pool.submit(_log_failures, fn)

    def wait(self) -> None:
        """Block until everything queued so far has run (tests and shutdown)."""
        self._pool.submit(lambda: None).result()


def _attempt_of(path: Path) -> int:
    """Retry files are named ``<attempt>-…``; first deliveries count as attempt 0."""
    if path.parent.name != "retry":
        return 0
    head = path.name.split("-", 1)[0]
    return int(head) if head.isdigit() else len(RETRY_DELAYS_SECONDS)


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
        if permissions is None or permission_for(summary_type) not in permissions:
            continue
        try:
            client.request_backfill(summary_type, start, end)
        except Exception as exc:  # noqa: BLE001 - one type failing must not stop the rest
            log.warning("backfill request failed for %s: %s", summary_type, type(exc).__name__)
