"""Per-user store of Health/Activity summaries delivered by Ping/Push.

Garmin does not allow pull-only integrations in production: summaries arrive
through Ping (we fetch the callback URL) or Push (the summary is in the POST
body), and the MCP tools answer from what has been stored here.

One SQLite file per user, inside that user's token directory::

    <GARMIN_OAUTH_TOKEN_ROOT>/<user-id>/summaries.sqlite3   (0600)

Keeping it next to ``tokens.json`` means a request can only ever open the file
its own token directory names, and deleting a user is deleting one directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Iterable
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DB_FILENAME = "summaries.sqlite3"

# Summary types Garmin delivers per API, keyed by the permission that gates them.
# A user who withdraws a permission loses the stored data behind it.
ACTIVITY_PERMISSION = "ACTIVITY_EXPORT"
HEALTH_PERMISSION = "HEALTH_EXPORT"
ACTIVITY_TYPES = frozenset({
    "activities",
    "activityDetails",
    "activityFiles",
    "manuallyUpdatedActivities",
    "moveiq",
})
HEALTH_TYPES = frozenset({
    "allDayRespiration",
    "bloodPressures",
    "bodyComps",
    "dailies",
    "epochs",
    "healthSnapshot",
    "hrv",
    "pulseox",
    "skinTemp",
    "sleeps",
    "stressDetails",
    "userMetrics",
})
SUMMARY_TYPES = ACTIVITY_TYPES | HEALTH_TYPES

_SCHEMA = """
CREATE TABLE IF NOT EXISTS summaries (
    summary_type  TEXT NOT NULL,
    summary_id    TEXT NOT NULL,
    calendar_date TEXT,
    start_time    INTEGER,
    activity_id   TEXT,
    received_at   REAL NOT NULL,
    payload       TEXT NOT NULL,
    PRIMARY KEY (summary_type, summary_id)
);
CREATE INDEX IF NOT EXISTS summaries_by_date ON summaries (summary_type, calendar_date);
CREATE INDEX IF NOT EXISTS summaries_by_start ON summaries (summary_type, start_time);
"""


def permission_for(summary_type: str) -> str | None:
    """The Garmin permission that gates ``summary_type``, or None if unknown."""
    if summary_type in ACTIVITY_TYPES:
        return ACTIVITY_PERMISSION
    if summary_type in HEALTH_TYPES:
        return HEALTH_PERMISSION
    return None


def withdrawn_types(permissions: list[str]) -> set[str]:
    """Summary types a user no longer shares, given their current permissions."""
    withdrawn: set[str] = set()
    if ACTIVITY_PERMISSION not in permissions:
        withdrawn |= ACTIVITY_TYPES
    if HEALTH_PERMISSION not in permissions:
        withdrawn |= HEALTH_TYPES
    return withdrawn


def calendar_date_of(summary: dict) -> str | None:
    """Local calendar date of a summary: ``calendarDate`` or start + offset."""
    if summary.get("calendarDate"):
        return str(summary["calendarDate"])[:10]
    start = summary.get("startTimeInSeconds")
    offset = summary.get("startTimeOffsetInSeconds") or 0
    if isinstance(start, (int, float)) and isinstance(offset, (int, float)):
        return datetime.fromtimestamp(start + offset, tz=timezone.utc).date().isoformat()
    return None


def _summary_id(summary: dict) -> str:
    sid = summary.get("summaryId")
    if sid not in (None, ""):
        return str(sid)
    # No summaryId: fall back to a content hash so a redelivery still dedupes.
    digest = hashlib.sha256(json.dumps(summary, sort_keys=True).encode()).hexdigest()
    return f"sha256:{digest}"


class SummaryStore:
    """Read/write one user's summaries. Callers pass the user's token directory."""

    def __init__(self, user_dir: Path):
        self.path = Path(user_dir) / _DB_FILENAME
        self._schema_ready = False

    def _connect(self) -> sqlite3.Connection:
        new = not self.path.exists()
        conn = sqlite3.connect(self.path, timeout=30)
        if new:
            os.chmod(self.path, 0o600)
        if new or not self._schema_ready:
            conn.executescript(_SCHEMA)
            self._schema_ready = True
        return conn

    def put(self, summary_type: str, summaries: Iterable[dict], *, now: float | None = None) -> int:
        """Upsert summaries; a redelivered summaryId replaces the stored row."""
        now = time.time() if now is None else now
        rows = []
        for s in summaries:
            if not isinstance(s, dict):
                continue
            start = s.get("startTimeInSeconds")
            rows.append((
                summary_type,
                _summary_id(s),
                calendar_date_of(s),
                int(start) if isinstance(start, (int, float)) else None,
                str(s["activityId"]) if s.get("activityId") is not None else None,
                now,
                json.dumps(s, separators=(",", ":")),
            ))
        if not rows:
            return 0
        with closing(self._connect()) as conn, conn:
            conn.executemany(
                "INSERT OR REPLACE INTO summaries VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def by_date(self, summary_type: str, calendar_date: str) -> list[dict]:
        """Summaries of one type for one local calendar date, newest delivery first."""
        return self._select(
            "SELECT payload FROM summaries WHERE summary_type = ? AND calendar_date = ? "
            "ORDER BY received_at DESC",
            (summary_type, calendar_date),
        )

    def by_date_range(self, summary_type: str, start_date: str, end_date: str) -> list[dict]:
        return self._select(
            "SELECT payload FROM summaries WHERE summary_type = ? AND calendar_date BETWEEN ? AND ? "
            "ORDER BY start_time DESC",
            (summary_type, start_date, end_date),
        )

    def latest(self, summary_type: str, *, offset: int = 0, limit: int = 20) -> list[dict]:
        return self._select(
            "SELECT payload FROM summaries WHERE summary_type = ? ORDER BY start_time DESC LIMIT ? OFFSET ?",
            (summary_type, limit, offset),
        )

    def by_activity_id(self, summary_type: str, activity_id: str) -> dict | None:
        rows = self._select(
            "SELECT payload FROM summaries WHERE summary_type = ? AND activity_id = ? "
            "ORDER BY received_at DESC LIMIT 1",
            (summary_type, str(activity_id)),
        )
        return rows[0] if rows else None

    def purge(self, summary_types: Iterable[str]) -> int:
        """Delete every stored summary of the given types (withdrawn permission)."""
        types = sorted(set(summary_types))
        if not types or not self.path.exists():
            return 0
        # Only "?" placeholders are interpolated; the type names are bound parameters.
        placeholders = ",".join("?" for _ in types)
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(f"DELETE FROM summaries WHERE summary_type IN ({placeholders})", types)
            return cur.rowcount

    def _select(self, sql: str, params: tuple[Any, ...]) -> list[dict]:
        if not self.path.exists():
            return []
        with closing(self._connect()) as conn:
            return [json.loads(row[0]) for row in conn.execute(sql, params)]
