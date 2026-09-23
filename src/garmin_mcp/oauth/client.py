"""Official Health/Activity API client with a garminconnect-shaped surface.

Only methods that map to Health/Activity pull endpoints are implemented.
Anything else raises :class:`OfficialApiUnavailableError` so MCP tools return
a clear error instead of inventing data.

Pull windows filter by **upload** time (device sync), capped at 24h per request.
For smoke tests we walk recent upload windows and filter summaries by calendar
date / activity start. Deep history belongs on Ping/Push + backfill (webhook
stubs accept those deliveries; full ingest is out of scope for this eval path).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from garmin_mcp.oauth.config import MAX_PULL_WINDOW_SECONDS, OAuthConfig
from garmin_mcp.oauth.errors import OfficialApiUnavailableError
from garmin_mcp.oauth.flow import refresh_tokens
from garmin_mcp.oauth.tokens import TokenBundle, TokenStore

# How far back (by upload time) recent-activity / daily lookups scan.
_DEFAULT_LOOKBACK_DAYS = 14


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
        self._http = http or httpx.Client(timeout=30.0)
        self._owns_http = http is None
        self._bundle: TokenBundle | None = None

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def _tokens(self) -> TokenBundle:
        if self._bundle is None:
            self._bundle = self._store.load_tokens(self._user_id)
        if self._bundle.access_expired():
            self._bundle = refresh_tokens(
                self._config, self._store, self._user_id, self._bundle, http=self._http
            )
        return self._bundle

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None) -> Any:
        tokens = self._tokens()
        url = f"{self._config.wellness_base}/{path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {tokens.access_token}"}
        response = self._http.request(method, url, params=params, headers=headers)
        if response.status_code == 401:
            self._bundle = refresh_tokens(
                self._config, self._store, self._user_id, tokens, http=self._http
            )
            headers = {"Authorization": f"Bearer {self._bundle.access_token}"}
            response = self._http.request(method, url, params=params, headers=headers)
        if response.status_code >= 400:
            raise RuntimeError(f"Garmin wellness API HTTP {response.status_code}")
        if not response.content:
            return None
        return response.json()

    def _iter_summaries(self, summary_type: str, *, start: datetime, end: datetime) -> list[dict]:
        results: list[dict] = []
        window_start = int(start.astimezone(timezone.utc).timestamp())
        final_end = int(end.astimezone(timezone.utc).timestamp())
        while window_start < final_end:
            window_end = min(window_start + MAX_PULL_WINDOW_SECONDS, final_end)
            payload = self._request(
                "GET",
                summary_type,
                params={
                    "uploadStartTimeInSeconds": window_start,
                    "uploadEndTimeInSeconds": window_end,
                },
            )
            if isinstance(payload, list):
                results.extend(s for s in payload if isinstance(s, dict))
            window_start = window_end
        return results

    def _lookback_window(self, days: int = _DEFAULT_LOOKBACK_DAYS) -> tuple[datetime, datetime]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        return start, end

    # --- Activity tools -------------------------------------------------

    def get_activities(self, start: int, limit: int) -> list[dict]:
        window_start, window_end = self._lookback_window()
        summaries = self._iter_summaries("activities", start=window_start, end=window_end)
        summaries = _dedupe_by_id(summaries, "activityId")
        summaries.sort(key=lambda s: s.get("startTimeInSeconds") or 0, reverse=True)
        sliced = summaries[start : start + limit]
        return [_activity_to_connect_shape(s) for s in sliced]

    def get_activities_by_date(self, start_date: str, end_date: str) -> list[dict]:
        # Pull a padded upload window around the calendar range, then filter.
        start_dt = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc) - timedelta(days=2)
        end_dt = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc) + timedelta(days=3)
        now = datetime.now(timezone.utc)
        end_dt = min(end_dt, now)
        if start_dt > end_dt:
            start_dt = end_dt - timedelta(days=1)
        summaries = self._iter_summaries("activities", start=start_dt, end=end_dt)
        summaries = _dedupe_by_id(summaries, "activityId")
        out = []
        for s in summaries:
            local = _activity_local_date(s)
            if local and start_date <= local <= end_date:
                out.append(_activity_to_connect_shape(s))
        out.sort(key=lambda a: a.get("startTimeLocal") or "", reverse=True)
        return out

    def get_activity(self, activity_id: str) -> dict:
        for a in self.get_activities(0, 100):
            if str(a.get("activityId")) == str(activity_id):
                return a
        # Narrower targeted pull via activityDetails if list miss.
        window_start, window_end = self._lookback_window(days=30)
        details = self._iter_summaries("activityDetails", start=window_start, end=window_end)
        for d in details:
            if str(d.get("activityId")) == str(activity_id):
                return _activity_to_connect_shape(d)
        raise RuntimeError(f"Activity {activity_id} not found in recent official API uploads")

    def get_activity_details(self, activity_id: str) -> dict:
        window_start, window_end = self._lookback_window(days=30)
        details = self._iter_summaries("activityDetails", start=window_start, end=window_end)
        for d in details:
            if str(d.get("activityId")) == str(activity_id):
                # Strip bulky samples; keep a compact details object.
                return {
                    "activityId": d.get("activityId"),
                    "summaryId": d.get("summaryId"),
                    "detailsAvailable": True,
                    "measurementCount": len(d.get("samples") or []) if isinstance(d.get("samples"), list) else None,
                }
        return {"activityId": activity_id, "detailsAvailable": False}

    def get_activity_splits(self, activity_id: str) -> dict:
        raise OfficialApiUnavailableError("activity lap / split structure")

    def get_activity_typed_splits(self, activity_id: str) -> Any:
        raise OfficialApiUnavailableError("typed interval / workout structure")

    # --- Daily / sleep / HR ---------------------------------------------

    def get_stats(self, date: str) -> dict:
        daily = self._daily_for_date(date)
        if daily is None:
            return {"calendarDate": date, "note": "No dailies summary in recent upload window"}
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
        start, end = self._lookback_window()
        sleeps = self._iter_summaries("sleeps", start=start, end=end)
        match = None
        for s in sleeps:
            if _summary_calendar_date(s) == date:
                match = s
                break
        if match is None:
            return {"dailySleepDTO": {"calendarDate": date}, "note": "No sleep summary in recent upload window"}
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
            return {"calendarDate": date, "note": "No dailies summary in recent upload window"}
        return {
            "calendarDate": date,
            "minHeartRate": daily.get("minHeartRateInBeatsPerMinute"),
            "maxHeartRate": daily.get("maxHeartRateInBeatsPerMinute"),
            "restingHeartRate": daily.get("restingHeartRateInBeatsPerMinute"),
            "lastSevenDaysAvgRestingHeartRate": None,
            "source": "official_dailies",
        }

    def _daily_for_date(self, date: str) -> dict | None:
        start, end = self._lookback_window()
        dailies = self._iter_summaries("dailies", start=start, end=end)
        for d in dailies:
            if _summary_calendar_date(d) == date:
                return d
        return None

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


def _dedupe_by_id(summaries: list[dict], key: str) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for s in summaries:
        sid = s.get(key) or s.get("summaryId")
        if sid is None:
            out.append(s)
            continue
        sid_s = str(sid)
        if sid_s in seen:
            continue
        seen.add(sid_s)
        out.append(s)
    return out


def _summary_calendar_date(summary: dict) -> str | None:
    if summary.get("calendarDate"):
        return str(summary["calendarDate"])[:10]
    start = summary.get("startTimeInSeconds")
    offset = summary.get("startTimeOffsetInSeconds") or 0
    if isinstance(start, (int, float)):
        local = datetime.fromtimestamp(start + offset, tz=timezone.utc)
        return local.date().isoformat()
    return None


def _activity_local_date(summary: dict) -> str | None:
    return _summary_calendar_date(summary)


def _activity_to_connect_shape(summary: dict) -> dict:
    """Map an official activities summary toward the unofficial Connect shape."""
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
