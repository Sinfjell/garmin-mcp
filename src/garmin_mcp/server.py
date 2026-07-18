"""Garmin MCP server — exposes Garmin Connect data to MCP clients over stdio.

Tools are read-only: this server never writes to, modifies, or deletes
anything in Garmin Connect. All responses are compact JSON strings so they
stay cheap for an LLM to read.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any, Callable

from garminconnect import Garmin, GarminConnectAuthenticationError
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("garmin")

_client: Garmin | None = None

_AUTH_HELP = (
    "No Garmin session available. Run `garmin-mcp-auth` once to log in "
    "(supports MFA), or set GARMIN_EMAIL and GARMIN_PASSWORD environment "
    "variables for non-interactive login."
)


def _token_store() -> str:
    return os.environ.get("GARMIN_TOKENS", str(Path.home() / ".garminconnect"))


def get_client() -> Garmin:
    """Return a lazily-initialized, cached Garmin Connect client."""
    global _client
    if _client is not None:
        return _client

    tokenstore = _token_store()
    try:
        client = Garmin()
        client.login(tokenstore)
        _client = client
        return _client
    except (FileNotFoundError, GarminConnectAuthenticationError):
        pass

    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if email and password:
        client = Garmin(email=email, password=password)
        client.login(tokenstore)
        _client = client
        return _client

    raise RuntimeError(_AUTH_HELP)


def _tool_call(build: Callable[[Garmin], Any]) -> str:
    """Run a tool body against the Garmin client, returning compact JSON.

    Any failure (auth expired, network error, bad input) is captured and
    returned as a JSON error object instead of raising, so MCP clients
    always get a parseable response.
    """
    try:
        data = build(get_client())
        return json.dumps(data, default=str, separators=(",", ":"))
    except GarminConnectAuthenticationError:
        return json.dumps({"error": "Garmin authentication expired. Run `garmin-mcp-auth` to log in again."})
    except Exception as exc:  # noqa: BLE001 - always return JSON, never raise to the client
        return json.dumps({"error": f"Garmin request failed: {exc}"})


def _fmt_activity_summary(a: dict) -> dict:
    dist_km = round((a.get("distance") or 0) / 1000, 2)
    dur_min = round((a.get("duration") or 0) / 60, 1)
    pace_min_per_km = round(dur_min / dist_km, 2) if dist_km else None
    return {
        "id": a.get("activityId"),
        "date": (a.get("startTimeLocal") or "")[:16],
        "type": (a.get("activityType") or {}).get("typeKey"),
        "name": a.get("activityName"),
        "distance_km": dist_km,
        "duration_min": dur_min,
        "avg_hr": a.get("averageHR"),
        "pace_min_per_km": pace_min_per_km,
    }


_ACTIVITY_DETAIL_STRIP_KEYS = {"activityDetailMetrics", "metricDescriptors", "geoPolylineDTO"}
_SLEEP_STRIP_KEYS = {
    "sleepLevels",
    "sleepMovement",
    "sleepRestlessMoments",
    "sleepHeartRate",
    "sleepStress",
    "sleepBodyBattery",
    "breathingDisruptionData",
    "hrvData",
    "remSleepData",
    "wellnessEpochRespirationAveragesList",
    "wellnessEpochRespirationDataDTOList",
    "wellnessEpochSPO2DataDTOList",
}


def _fmt_body_battery_day(item: dict) -> dict:
    values = item.get("bodyBatteryValuesArray") or []
    levels = [v[1] for v in values if len(v) > 1 and isinstance(v[1], (int, float))]
    return {
        "date": item.get("date"),
        "charged": item.get("charged"),
        "drained": item.get("drained"),
        "highest": max(levels) if levels else None,
        "lowest": min(levels) if levels else None,
    }


@mcp.tool()
def list_recent_activities(limit: int = 10) -> str:
    """List the most recent Garmin activities, newest first.

    Returns a JSON array of activity summaries with: id (use with
    get_activity_details), date (YYYY-MM-DDTHH:MM local time), type
    (e.g. "running", "cycling"), name, distance_km, duration_min, avg_hr,
    and pace_min_per_km. `limit` caps how many activities come back
    (default 10).
    """
    return _tool_call(lambda c: [_fmt_activity_summary(a) for a in c.get_activities(0, limit)])


@mcp.tool()
def get_activity_details(activity_id: str) -> str:
    """Get full detail for a single activity by its id.

    `activity_id` is the numeric id string returned by list_recent_activities
    or list_activities_by_date (the "id" field). Returns a JSON object
    combining the activity summary (name, type, distance, duration, HR,
    training effect) with a nested "details" object (measurement counts).
    Bulky per-sample metric arrays and GPS polylines are stripped to keep
    the response small — use Garmin Connect directly for raw sample data.
    """

    def build(c: Garmin) -> dict:
        summary = c.get_activity(activity_id)
        detail = c.get_activity_details(activity_id)
        details = {k: v for k, v in detail.items() if k not in _ACTIVITY_DETAIL_STRIP_KEYS}
        return {**summary, "details": details}

    return _tool_call(build)


@mcp.tool()
def list_activities_by_date(start_date: str, end_date: str) -> str:
    """List activities between two dates (inclusive).

    `start_date` and `end_date` must be "YYYY-MM-DD". Returns a JSON array
    of activity summaries in the same shape as list_recent_activities.
    """
    return _tool_call(lambda c: [_fmt_activity_summary(a) for a in c.get_activities_by_date(start_date, end_date)])


@mcp.tool()
def get_daily_stats(date: str) -> str:
    """Get daily summary stats for one day: steps, calories, resting HR, stress, etc.

    `date` must be "YYYY-MM-DD". Returns a JSON object with fields such as
    totalSteps, totalKilocalories, restingHeartRate, averageStressLevel,
    bodyBatteryHighestValue/LowestValue, and floorsAscended.
    """
    return _tool_call(lambda c: c.get_stats(date))


@mcp.tool()
def get_sleep(date: str) -> str:
    """Get sleep summary for the night ending on the given date.

    `date` must be "YYYY-MM-DD" (the calendar date the sleep session ends on).
    Returns a JSON object with the daily sleep summary (dailySleepDTO: sleep
    stages in seconds, sleep scores, average HR/SpO2/respiration/stress) plus
    overnight HRV and resting HR. Per-minute sleep-stage and movement arrays
    are stripped to keep the response small.
    """

    def build(c: Garmin) -> dict:
        data = c.get_sleep_data(date)
        return {k: v for k, v in data.items() if k not in _SLEEP_STRIP_KEYS}

    return _tool_call(build)


@mcp.tool()
def get_heart_rate(date: str) -> str:
    """Get heart rate summary for one day.

    `date` must be "YYYY-MM-DD". Returns a JSON object with minHeartRate,
    maxHeartRate, restingHeartRate, and lastSevenDaysAvgRestingHeartRate.
    The per-minute heartRateValues time series is stripped to keep the
    response small.
    """

    def build(c: Garmin) -> dict:
        data = c.get_heart_rates(date)
        return {k: v for k, v in data.items() if k != "heartRateValues"}

    return _tool_call(build)


@mcp.tool()
def get_body_battery(start_date: str, end_date: str) -> str:
    """Get Body Battery (energy reserve) summary per day over a date range.

    `start_date` and `end_date` must be "YYYY-MM-DD". Returns a JSON array
    with one object per day: date, charged (points gained), drained (points
    lost), highest, and lowest Body Battery level. Per-minute level arrays
    are stripped; highest/lowest are derived from them before stripping.
    """
    return _tool_call(lambda c: [_fmt_body_battery_day(d) for d in c.get_body_battery(start_date, end_date)])


@mcp.tool()
def get_training_status(date: str) -> str:
    """Get training status for a given day: fitness trend, load, VO2 max, acclimation.

    `date` must be "YYYY-MM-DD". Returns a JSON object with
    mostRecentTrainingStatus (training status, acute/chronic training load),
    mostRecentVO2Max, and heatAltitudeAcclimationDTO.
    """
    return _tool_call(lambda c: c.get_training_status(date))


def _print_tool_list() -> None:
    for tool in asyncio.run(mcp.list_tools()):
        first_line = (tool.description or "").strip().splitlines()[0] if tool.description else ""
        print(f"{tool.name}: {first_line}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="garmin-mcp", description="Garmin Connect MCP server")
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="Print registered tool names and descriptions, then exit (no Garmin connection made)",
    )
    args = parser.parse_args()

    if args.list_tools:
        _print_tool_list()
        return

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
