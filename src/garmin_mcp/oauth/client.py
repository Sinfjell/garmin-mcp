"""Official Health/Activity API client with a garminconnect-shaped surface.

Reads come from the user's local summary store (``datastore.py``), filled by
Ping/Push deliveries — Garmin does not allow pull-only integrations. The
network methods here serve the webhook processor: following a Ping callback,
confirming a deregistration, reading current permissions, requesting backfill.

Anything the official APIs do not expose raises
:class:`OfficialApiUnavailableError`, so MCP tools return a clear error instead
of inventing data.
"""
from __future__ import annotations

import contextvars
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from garmin_mcp.oauth.config import API_BASE_URL, OAuthConfig
from garmin_mcp.oauth.datastore import SummaryStore
from garmin_mcp.oauth.errors import GarminApiError, OfficialApiUnavailableError, TokenExchangeError
from garmin_mcp.oauth.flow import refresh_tokens
from garmin_mcp.oauth.tokens import TokenBundle, TokenStore

_API_HOST = urlparse(API_BASE_URL).hostname

# Garmin device models whose data went into the current tool call. The MCP layer
# turns them into the "Garmin [device model]" attribution Garmin's API brand
# guidelines require on every downstream use, including AI. One set per call
# (context-local), so concurrent requests never mix their attributions.
_devices_seen: contextvars.ContextVar[set[str] | None] = contextvars.ContextVar("garmin_devices", default=None)


def begin_attribution() -> contextvars.Token:
    return _devices_seen.set(set())


def end_attribution(token: contextvars.Token) -> str:
    """``Garmin <model>[, Garmin <model>]``, or plain ``Garmin`` when no model is known."""
    devices = _devices_seen.get() or set()
    _devices_seen.reset(token)
    return ", ".join(f"Garmin {d}" for d in sorted(devices)) or "Garmin"


def _note_device(summary: dict) -> None:
    seen = _devices_seen.get()
    nested = summary.get("summary") if isinstance(summary.get("summary"), dict) else {}
    name = summary.get("deviceName") or nested.get("deviceName")
    if seen is not None and name:
        seen.add(str(name).removeprefix("Garmin ").strip())
_NOT_SYNCED = "No data stored for this date yet. Garmin delivers it after the device syncs."


class OfficialGarminClient:
    """Duck-typed stand-in for ``garminconnect.Garmin`` in oauth mode."""

    def __init__(
        self,
        config: OAuthConfig,
        store: TokenStore,
        user_id: str,
        *,
        http: httpx.Client | None = None,
    ):
        self._config = config
        self._store = store
        self._user_id = user_id
        self._data = SummaryStore(store.user_dir(user_id))
        self._http = http or httpx.Client(timeout=30.0)
        self._owns_http = http is None
        self._bundle: TokenBundle | None = None

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    @property
    def data(self) -> SummaryStore:
        return self._data

    def _tokens(self) -> TokenBundle:
        if self._bundle is None:
            self._bundle = self._store.load_tokens(self._user_id)
        if self._bundle.access_expired():
            self._bundle = refresh_tokens(
                self._config, self._store, self._user_id, self._bundle, http=self._http
            )
        return self._bundle

    def _request(self, method: str, url: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        """Authorized call; one refresh-and-retry on 401. Raises GarminApiError on >= 400."""
        tokens = self._tokens()
        headers = {"Authorization": f"Bearer {tokens.access_token}"}
        response = self._http.request(method, url, params=params, headers=headers)
        if response.status_code == 401:
            self._bundle = refresh_tokens(
                self._config, self._store, self._user_id, tokens, http=self._http
            )
            headers = {"Authorization": f"Bearer {self._bundle.access_token}"}
            response = self._http.request(method, url, params=params, headers=headers)
        if response.status_code >= 400:
            raise GarminApiError(response.status_code)
        return response

    def _wellness(self, path: str) -> str:
        return f"{self._config.wellness_base}/{path.lstrip('/')}"

    # --- Webhook support ------------------------------------------------

    def fetch_callback(self, callback_url: str) -> list[dict]:
        """Follow a Ping ``callbackURL``. Only Garmin's API host is ever contacted."""
        parsed = urlparse(callback_url)
        if parsed.scheme != "https" or parsed.hostname != _API_HOST:
            raise ValueError("callbackURL is not on the Garmin API host")
        response = self._request("GET", callback_url)
        payload = response.json() if response.content else []
        return [s for s in payload if isinstance(s, dict)] if isinstance(payload, list) else []

    def registration_active(self) -> bool:
        """True while Garmin still honours this user's tokens.

        A deregistration notification is only acted on once Garmin itself
        rejects the user's tokens, so a forged notification cannot delete data.
        Network and 5xx failures propagate: they prove nothing either way.
        """
        try:
            self._request("GET", self._wellness("user/id"))
        except (TokenExchangeError, GarminApiError) as exc:
            # 400/401 from the token endpoint is a refused refresh token; 401/403
            # from the API is a refused access token. Anything else (429, 5xx)
            # says nothing about the registration and must not delete anyone.
            refused = (400, 401) if isinstance(exc, TokenExchangeError) else (401, 403)
            if exc.status_code in refused:
                return False
            raise
        return True

    def current_permissions(self) -> list[str]:
        """The user's permissions as Garmin reports them now (not as a webhook claims)."""
        payload = self._request("GET", self._wellness("user/permissions")).json()
        if isinstance(payload, dict):
            payload = payload.get("permissions")
        if not isinstance(payload, list):
            # Never read an unexpected shape as "everything withdrawn": that purges data.
            raise TypeError("unexpected permissions payload")
        return [str(p) for p in payload]

    def request_backfill(self, summary_type: str, start_ts: int, end_ts: int) -> None:
        """Ask Garmin to redeliver history for one type; data arrives via Ping/Push."""
        self._request(
            "GET",
            self._wellness(f"backfill/{summary_type}"),
            params={"summaryStartTimeInSeconds": start_ts, "summaryEndTimeInSeconds": end_ts},
        )

    # --- Activity tools -------------------------------------------------

    def get_activities(self, start: int, limit: int) -> list[dict]:
        return [_activity_to_connect_shape(s) for s in self._data.latest("activities", offset=start, limit=limit)]

    def get_activities_by_date(self, start_date: str, end_date: str) -> list[dict]:
        rows = self._data.by_date_range("activities", start_date, end_date)
        return [_activity_to_connect_shape(s) for s in rows]

    def get_activity(self, activity_id: str) -> dict:
        summary = self._data.by_activity_id("activities", activity_id)
        if summary is None:
            raise RuntimeError(f"Activity {activity_id} has not been delivered by Garmin yet")
        return _activity_to_connect_shape(summary)

    def get_activity_details(self, activity_id: str) -> dict:
        details = self._data.by_activity_id("activityDetails", activity_id)
        if details is None:
            return {"activityId": activity_id, "detailsAvailable": False}
        _note_device(details)
        samples = details.get("samples")
        return {
            "activityId": details.get("activityId"),
            "summaryId": details.get("summaryId"),
            "detailsAvailable": True,
            "measurementCount": len(samples) if isinstance(samples, list) else None,
        }

    def get_activity_splits(self, activity_id: str) -> dict:
        raise OfficialApiUnavailableError("activity lap / split structure")

    def get_activity_typed_splits(self, activity_id: str) -> Any:
        raise OfficialApiUnavailableError("typed interval / workout structure")

    # --- Daily / sleep / HR ---------------------------------------------

    def get_stats(self, date: str) -> dict:
        daily = self._daily_for_date(date)
        if daily is None:
            return {"calendarDate": date, "note": _NOT_SYNCED}
        return {
            "calendarDate": date,
            "totalSteps": daily.get("steps"),
            "totalDistanceMeters": daily.get("distanceInMeters"),
            "totalKilocalories": _sum_calories(daily),
            "restingHeartRate": daily.get("restingHeartRateInBeatsPerMinute"),
            "minHeartRate": daily.get("minHeartRateInBeatsPerMinute"),
            "maxHeartRate": daily.get("maxHeartRateInBeatsPerMinute"),
            "averageStressLevel": daily.get("averageStressLevel"),
            "floorsAscended": daily.get("floorsClimbed"),
            "source": "official_dailies",
        }

    def get_sleep_data(self, date: str) -> dict:
        sleeps = self._data.by_date("sleeps", date)
        if not sleeps:
            return {"dailySleepDTO": {"calendarDate": date}, "note": _NOT_SYNCED}
        match = sleeps[0]
        return {
            "dailySleepDTO": {
                "calendarDate": date,
                "sleepTimeSeconds": match.get("durationInSeconds"),
                "deepSleepSeconds": match.get("deepSleepDurationInSeconds"),
                "lightSleepSeconds": match.get("lightSleepDurationInSeconds"),
                "remSleepSeconds": match.get("remSleepInSeconds") or match.get("remSleepDurationInSeconds"),
                "awakeSleepSeconds": match.get("awakeDurationInSeconds"),
                "validation": match.get("validation"),
            },
            "source": "official_sleeps",
        }

    def get_heart_rates(self, date: str) -> dict:
        daily = self._daily_for_date(date)
        if daily is None:
            return {"calendarDate": date, "note": _NOT_SYNCED}
        return {
            "calendarDate": date,
            "minHeartRate": daily.get("minHeartRateInBeatsPerMinute"),
            "maxHeartRate": daily.get("maxHeartRateInBeatsPerMinute"),
            "restingHeartRate": daily.get("restingHeartRateInBeatsPerMinute"),
            "lastSevenDaysAvgRestingHeartRate": None,
            "source": "official_dailies",
        }

    def _daily_for_date(self, date: str) -> dict | None:
        # Garmin re-sends a day's summary as it fills up; the fullest one wins.
        dailies = self._data.by_date("dailies", date)
        if not dailies:
            return None
        return max(dailies, key=lambda d: d.get("durationInSeconds") or 0)

    # --- Explicitly unavailable -----------------------------------------

    def get_body_battery(self, start_date: str, end_date: str) -> list:
        raise OfficialApiUnavailableError("Body Battery")

    def get_training_status(self, date: str) -> dict:
        raise OfficialApiUnavailableError("training status")

    def get_lactate_threshold(self, *args: Any, **kwargs: Any) -> dict:
        raise OfficialApiUnavailableError("lactate threshold")

    def get_max_metrics(self, date: str) -> Any:
        raise OfficialApiUnavailableError("VO2 max / max metrics")

    def get_race_predictions(self) -> Any:
        raise OfficialApiUnavailableError("race predictions")

    def get_personal_record(self) -> list:
        raise OfficialApiUnavailableError("personal records")

    def get_heart_rate_zones(self) -> Any:
        raise OfficialApiUnavailableError("heart-rate zones / threshold")


def _sum_calories(daily: dict) -> int | None:
    active = daily.get("activeKilocalories")
    bmr = daily.get("bmrKilocalories")
    if isinstance(active, (int, float)) or isinstance(bmr, (int, float)):
        return int((active or 0) + (bmr or 0))
    return None


def _activity_to_connect_shape(summary: dict) -> dict:
    """Map an official activities summary toward the unofficial Connect shape."""
    _note_device(summary)
    start = summary.get("startTimeInSeconds")
    offset = summary.get("startTimeOffsetInSeconds") or 0
    start_local = None
    if isinstance(start, (int, float)):
        local = datetime.fromtimestamp(start + offset, tz=timezone.utc)
        start_local = local.strftime("%Y-%m-%d %H:%M:%S")
    activity_type = summary.get("activityType")
    if isinstance(activity_type, str):
        type_key = activity_type.lower()
    elif isinstance(activity_type, dict):
        type_key = activity_type.get("typeKey") or activity_type.get("type")
    else:
        type_key = None
    return {
        "activityId": summary.get("activityId") or summary.get("summaryId"),
        "activityName": summary.get("activityName") or summary.get("activityType"),
        "activityType": {"typeKey": type_key},
        "distance": summary.get("distanceInMeters"),
        "duration": summary.get("durationInSeconds"),
        "movingDuration": summary.get("durationInSeconds"),
        "elapsedDuration": summary.get("durationInSeconds"),
        "averageHR": summary.get("averageHeartRateInBeatsPerMinute"),
        "startTimeLocal": start_local,
        "summaryDTO": {
            "distance": summary.get("distanceInMeters"),
            "duration": summary.get("durationInSeconds"),
            "movingDuration": summary.get("durationInSeconds"),
            "elapsedDuration": summary.get("durationInSeconds"),
        },
        "source": "official_activities",
    }
