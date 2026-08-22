"""Garmin MCP server — exposes Garmin Connect data to MCP clients over stdio.

Tools are read-only: this server never writes to, modifies, or deletes
anything in Garmin Connect. All responses are compact JSON strings so they
stay cheap for an LLM to read.
"""
import argparse
import asyncio
import json
import os
from collections.abc import Callable
from datetime import date as _date
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from garminconnect import Garmin, GarminConnectAuthenticationError
from mcp.server.fastmcp import FastMCP

from garmin_mcp import multitenant

mcp = FastMCP("garmin")

_client: Garmin | None = None

# Multi-tenant clients, keyed by token store. Separate from `_client` so the
# single-tenant cache can never be handed to a tenant request, or vice versa.
_tenant_clients: dict[str, Garmin] = {}

# "Today" always means the user's local calendar day, but this server may run on
# a UTC host. Resolving in UTC would push a late-evening session onto the next
# day's date, so anchor date defaults to Europe/Oslo.
_LOCAL_TZ_NAME = "Europe/Oslo"

_AUTH_HELP = (
    "No Garmin session available. Run `garmin-mcp-auth` once to log in "
    "(supports MFA), or set GARMIN_EMAIL and GARMIN_PASSWORD environment "
    "variables for non-interactive login."
)


def _today_local() -> _date:
    """Today's date in Europe/Oslo, regardless of the host clock's timezone.

    Falls back to the host's own local date if the system has no tz database.
    """
    try:
        return datetime.now(ZoneInfo(_LOCAL_TZ_NAME)).date()
    except ZoneInfoNotFoundError:
        return datetime.now(timezone.utc).astimezone().date()


def _token_store() -> str:
    return os.environ.get("GARMIN_TOKENS", str(Path.home() / ".garminconnect"))


def _tenant_client(tokenstore: str) -> Garmin:
    """Client for one multi-tenant user, from their token store only.

    No env-credential fallback here: `GARMIN_EMAIL`/`GARMIN_PASSWORD` belong to
    whoever runs the host, so falling back to them would serve the host's data
    to a tenant whose own session has expired.

    Clients are cached for the life of the process, one per store: re-logging in
    per request is what trips Garmin's rate limiting. A tenant whose cached
    session expires therefore gets errors until the process restarts — accepted
    for a household-scale deployment, and the reason expiry is worth watching.
    """
    cached = _tenant_clients.get(tokenstore)
    if cached is not None:
        return cached
    client = Garmin()
    client.login(tokenstore)
    _tenant_clients[tokenstore] = client
    return client


def get_client() -> Garmin:
    """Return a lazily-initialized, cached Garmin Connect client."""
    global _client

    tenant_store = multitenant.current_token_store()
    if tenant_store is not None:
        return _tenant_client(tenant_store)

    # Fail closed. Once the process serves tenants, an unbound token store means
    # the request was not routed as we expect — answering it from the host's own
    # session would hand the host's Garmin data to whoever asked.
    if multitenant.multi_tenant_active():
        raise RuntimeError("No tenant token store bound for this request.")

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


def _fmt_timing(summary: dict) -> dict:
    """Extract the explicit timing breakdown from an activity summary.

    Garmin distinguishes three clocks that are easy to confuse: elapsed
    (wall time start→save, includes post-finish standing), timer (what the
    watch counted), and moving (auto-pause removed). Surfacing them
    separately stops a consumer guessing which one is the "race time".
    """
    sd = summary.get("summaryDTO") or summary
    elapsed = sd.get("elapsedDuration")
    moving = sd.get("movingDuration")
    timer = sd.get("duration")
    stopped = round(elapsed - moving, 1) if elapsed is not None and moving is not None else None
    return {
        "elapsed_time_s": round(elapsed, 1) if elapsed is not None else None,
        "timer_time_s": round(timer, 1) if timer is not None else None,
        "moving_time_s": round(moving, 1) if moving is not None else None,
        "stopped_time_s": stopped,
    }


# GPS lat/long are stripped from laps by default: they leak location (home
# address for runs that start at home) and add noise. Opt in with include_gps.
_LAP_GPS_KEYS = ("startLatitude", "startLongitude", "endLatitude", "endLongitude")


def _fmt_lap(lap: dict, include_gps: bool = False) -> dict:
    distance_m = round(lap.get("distance") or 0, 1)
    moving_s = lap.get("movingDuration")
    # Pace from moving time (the standard convention). Meaningless for
    # non-distance sports, so only computed when the lap covered ground.
    pace = round((moving_s / 60) / (distance_m / 1000), 2) if distance_m and moving_s else None
    out = {
        "lap": lap.get("lapIndex"),
        "distance_m": distance_m,
        "elapsed_time_s": round(lap["elapsedDuration"], 1) if lap.get("elapsedDuration") is not None else None,
        "timer_time_s": round(lap["duration"], 1) if lap.get("duration") is not None else None,
        "moving_time_s": round(moving_s, 1) if moving_s is not None else None,
        "pace_min_per_km": pace,
        "avg_hr": lap.get("averageHR"),
        "max_hr": lap.get("maxHR"),
        "avg_power": lap.get("averagePower"),
        "max_power": lap.get("maxPower"),
        "avg_run_cadence": lap.get("averageRunCadence"),
        "intensity": lap.get("intensityType"),
        "elevation_gain_m": lap.get("elevationGain"),
        "elevation_loss_m": lap.get("elevationLoss"),
    }
    if include_gps:
        for k in _LAP_GPS_KEYS:
            if lap.get(k) is not None:
                out[k] = lap[k]
    return out


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


def _speed_to_pace(speed_m_s: Any) -> tuple[str | None, float | None]:
    """Convert a running speed in m/s to (formatted "M:SS/km", decimal min/km).

    Garmin reports threshold and activity speed in metres per second. Runners
    think in pace, so we surface a human "3:52" string (primary) alongside the
    decimal min/km used elsewhere in this server. Returns (None, None) for a
    zero/missing/non-numeric speed.
    """
    if not isinstance(speed_m_s, (int, float)) or speed_m_s <= 0:
        return None, None
    secs_per_km = 1000.0 / speed_m_s
    minutes = int(secs_per_km // 60)
    seconds = round(secs_per_km - minutes * 60)
    if seconds == 60:  # rounding carry, e.g. 3:60 -> 4:00
        minutes += 1
        seconds = 0
    return f"{minutes}:{seconds:02d}", round(secs_per_km / 60, 2)


# Garmin's lactate-threshold endpoints report speed in units of 10 m/s (i.e.
# true m/s divided by 10), unlike activity `averageSpeed` which is plain m/s.
# A raw 0.417 is really 4.17 m/s (~4:00/km), not 40:00/km. Verified 2026-07-20
# against a same-day track session whose activity averageSpeed matched raw * 10.
LT_SPEED_SCALE = 10.0


def _lt_speed_to_m_s(raw_speed: Any) -> float | None:
    """Scale Garmin's lactate-threshold speed field to true m/s.

    Returns None for missing/non-numeric input. See LT_SPEED_SCALE for why the
    threshold endpoints need this while activity speed does not.
    """
    if not isinstance(raw_speed, (int, float)):
        return None
    return raw_speed * LT_SPEED_SCALE


def _seconds_to_clock(secs: Any) -> str | None:
    """Format a duration in seconds as "H:MM:SS" (or "M:SS" under an hour)."""
    if not isinstance(secs, (int, float)) or secs <= 0:
        return None
    secs = round(secs)
    hours, rem = divmod(secs, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _fmt_lactate_threshold(raw: dict) -> dict:
    """Reshape get_lactate_threshold(latest=True) into HR + pace.

    The library returns {"speed_and_heart_rate": {...}, "power": {...}}. The
    threshold heart rate (LTHR) and threshold speed are what a runner uses to
    set zones, so we surface those plus a formatted pace. Garmin's raw threshold
    speed is scaled (see LT_SPEED_SCALE); we normalize it to true m/s here.
    """
    shr = raw.get("speed_and_heart_rate") or {}
    speed_m_s = _lt_speed_to_m_s(shr.get("speed"))
    pace_str, pace_dec = _speed_to_pace(speed_m_s)
    return {
        "heart_rate_bpm": shr.get("heartRate"),
        "pace_per_km": pace_str,
        "pace_min_per_km": pace_dec,
        "speed_m_s": round(speed_m_s, 3) if speed_m_s is not None else None,
        "measured_date": shr.get("calendarDate"),
    }


def _fmt_vo2max(raw: Any) -> dict:
    """Extract VO2max + fitness age from get_max_metrics.

    The endpoint returns a list of daily entries (or a single dict); the
    running value lives under the "generic" block. preciseValue keeps the
    decimal (59.2) that the rounded value (59) throws away.
    """
    entry = raw[0] if isinstance(raw, list) and raw else raw
    generic = (entry or {}).get("generic") if isinstance(entry, dict) else None
    generic = generic or {}
    precise = generic.get("vo2MaxPreciseValue")
    value = generic.get("vo2MaxValue")
    return {
        "value": round(precise, 1) if isinstance(precise, (int, float)) else value,
        "rounded_value": value,
        "fitness_age": generic.get("fitnessAge"),
        "measured_date": generic.get("calendarDate"),
    }


# Garmin race-predictor times are seconds; field names are stable across the
# community API. Order preserved for readable output (short → long).
_RACE_FIELDS = (("5k", "time5K"), ("10k", "time10K"), ("half_marathon", "timeHalfMarathon"), ("marathon", "timeMarathon"))


def _fmt_race_predictions(raw: Any) -> dict:
    """Reshape race predictions into 5k/10k/HM/marathon as clock strings."""
    entry = raw[-1] if isinstance(raw, list) and raw else raw
    if not isinstance(entry, dict):
        return {"error": "unexpected race-prediction shape"}
    out: dict[str, Any] = {}
    for label, field in _RACE_FIELDS:
        out[label] = _seconds_to_clock(entry.get(field))
    out["measured_date"] = entry.get("calendarDate")
    return out


# Best-effort map of Garmin personal-record typeIds → (label, unit) for the
# common *running* records. Garmin's typeId scheme is internal and undocumented;
# only the widely-confirmed running set is mapped here. Any typeId not listed
# keeps label=None so an unknown record is never mislabeled — the raw type_id
# and value are always preserved so the truth survives a wrong/missing label.
_PR_RUNNING_TYPES = {
    1: ("fastest_1km", "time"),
    2: ("fastest_1mile", "time"),
    3: ("fastest_5km", "time"),
    4: ("fastest_10km", "time"),
    7: ("longest_run", "distance"),
}

_PR_DATE_KEYS = ("prStartTimeGmtFormatted", "prStartTimeGmt", "prStartTimeLocalFormatted", "prStartTimeLocal")


def _fmt_personal_record(rec: dict) -> dict:
    """Reshape one personal-record entry, preserving raw type_id + value.

    value is seconds for time records and metres for distance records. We
    format it only for typeIds in the known running map; everything else keeps
    the raw value and a null label rather than a guessed one.
    """
    type_id = rec.get("typeId")
    value = rec.get("value")
    label, unit = _PR_RUNNING_TYPES.get(type_id, (None, None))
    if unit == "time":
        formatted = _seconds_to_clock(value)
    elif unit == "distance" and isinstance(value, (int, float)):
        formatted = f"{round(value / 1000, 2)} km"
    else:
        formatted = None
    date = next((rec[k] for k in _PR_DATE_KEYS if rec.get(k)), None)
    return {
        "record": label,
        "type_id": type_id,
        "value": value,
        "value_formatted": formatted,
        "activity_id": rec.get("activityId"),
        "date": (str(date)[:10] if date else None),
    }


def _extract_stat_series(obj: Any, value_keys: tuple[str, ...]) -> list[dict]:
    """Normalize a Garmin biometric-stats range payload to [{date, value}].

    These range endpoints are undocumented and have been seen as either a bare
    list of point dicts or a dict wrapping such a list. We look for a date-ish
    and a value-ish key on each point and skip anything unrecognizable, so a
    shape we didn't anticipate degrades to fewer/zero points rather than raising.
    """
    if isinstance(obj, dict):
        # Unwrap the first list-valued field (e.g. {"lactateThresholdSpeed": [...]})
        for v in obj.values():
            if isinstance(v, list):
                obj = v
                break
    if not isinstance(obj, list):
        return []
    date_keys = ("calendarDate", "date", "startDate", "from", "timestamp")
    points = []
    for pt in obj:
        if not isinstance(pt, dict):
            continue
        d = next((pt[k] for k in date_keys if pt.get(k)), None)
        val = next((pt[k] for k in value_keys if pt.get(k) is not None), None)
        if d is not None and val is not None:
            points.append({"date": str(d)[:10], "value": val})
    return points


def _fmt_threshold_history(raw: dict) -> dict:
    """Merge ranged lactate-threshold speed + HR into one dated trend series.

    get_lactate_threshold(latest=False, ...) returns
    {"speed": <series>, "heart_rate": <series>, "power": <series>}. We join
    speed and HR by date into [{date, lthr_bpm, pace_per_km, ...}] so the
    threshold trend reads as one timeline. If neither series parses, the raw
    payload is returned under "raw" so nothing is silently dropped.
    """
    speed_pts = _extract_stat_series(raw.get("speed"), ("value", "speed"))
    hr_pts = _extract_stat_series(raw.get("heart_rate"), ("value", "heartRate"))
    if not speed_pts and not hr_pts:
        return {"points": [], "raw": raw}
    hr_by_date = {p["date"]: p["value"] for p in hr_pts}
    speed_by_date = {p["date"]: p["value"] for p in speed_pts}
    points = []
    for d in sorted(set(hr_by_date) | set(speed_by_date)):
        speed_m_s = _lt_speed_to_m_s(speed_by_date.get(d))
        pace_str, pace_dec = _speed_to_pace(speed_m_s)
        points.append({
            "date": d,
            "lthr_bpm": hr_by_date.get(d),
            "pace_per_km": pace_str,
            "pace_min_per_km": pace_dec,
            "speed_m_s": round(speed_m_s, 3) if speed_m_s is not None else None,
        })
    return {"points": points}


# --- Heart-rate zones -------------------------------------------------------

# get_heart_rate_zones() returns a *list*, one entry per sport; the "DEFAULT"
# entry is the one the watch applies to running. Each entry carries only the
# floor of each zone — there is no ceiling field, so a zone's ceiling is the
# next zone's floor minus one, and zone 5 runs to max HR.
_HR_ZONE_COUNT = 5


def _fmt_hr_zones(raw: Any) -> dict:
    """Reshape get_heart_rate_zones() into zones with explicit floor and ceiling."""
    entries = [e for e in (raw if isinstance(raw, list) else [raw]) if isinstance(e, dict)]
    if not entries:
        return {"error": "unexpected heart-rate-zone shape", "raw": raw}
    entry = next((e for e in entries if e.get("sport") == "DEFAULT"), entries[0])
    floors = [entry.get(f"zone{i}Floor") for i in range(1, _HR_ZONE_COUNT + 1)]
    max_hr = entry.get("maxHeartRateUsed")
    zones = []
    for i, floor in enumerate(floors):
        if floor is None:
            continue
        nxt = next((f for f in floors[i + 1:] if f is not None), None)
        zones.append({"zone": i + 1, "floor_bpm": floor, "ceiling_bpm": (nxt - 1) if nxt is not None else max_hr})
    return {
        "sport": entry.get("sport"),
        "method": entry.get("trainingMethod"),
        "zones": zones,
        "lthr_bpm": entry.get("lactateThresholdHeartRateUsed"),
        "max_hr_bpm": max_hr,
        "resting_hr_bpm": entry.get("restingHeartRateUsed"),
    }


# --- Work-rep classification ------------------------------------------------

# Garmin exposes two independent typings on the same activity. `intensityType`
# on laps is the one that lies: on 2026-08-02 every lap of a 6x1000m session was
# tagged INTERVAL (warm-up, reps, recoveries and cool-down alike), and on
# 2026-08-16 the cool-down jog was tagged ACTIVE. get_activity_typed_splits()
# carries the workout structure Garmin actually recorded and is used first;
# `intensityType` is only a fallback for activities that have no typed structure.
_TYPED_SPLIT_PREFIX = "INTERVAL_"
_TYPED_WORK_TYPES = {"INTERVAL_ACTIVE"}
_TYPED_REST_TYPES = {"INTERVAL_REST", "INTERVAL_RECOVERY"}
_LAP_WORK_TYPES = {"ACTIVE"}
_LAP_REST_TYPES = {"REST", "RECOVERY"}

# Guards. Every one of these exists because a real activity fails without it —
# see the tests, which pin the activity behind each.
_MIN_REP_DISTANCE_M = 200.0  # 2026-08-20 had four ACTIVE splits of 1 m / 14-20 min
_MIN_REP_DURATION_S = 30.0
_MAX_REP_PACE_DEVIATION_S = 45.0  # 2026-08-16 tagged a 7:29/km cool-down ACTIVE among 3:50 reps
_LONE_REP_COVERAGE = 0.9  # a single "rep" that is the whole session is not a rep


def _pace_from(distance_m: Any, duration_s: Any) -> tuple[str | None, float | None]:
    """Pace for one split/lap, via the same m/s -> "M:SS" path as everything else."""
    if not isinstance(distance_m, (int, float)) or not isinstance(duration_s, (int, float)):
        return None, None
    if distance_m <= 0 or duration_s <= 0:
        return None, None
    return _speed_to_pace(distance_m / duration_s)


def _fmt_rep(item: dict, kind: str, type_key: str) -> dict:
    distance_m = round(item.get("distance") or 0, 1)
    duration_s = round(item.get("duration") or 0, 1)
    pace_str, pace_dec = _pace_from(distance_m, duration_s)
    return {
        "is_work_rep": kind == "work",
        "kind": kind,
        "distance_m": distance_m,
        "duration_s": duration_s,
        "pace_per_km": pace_str,
        "pace_min_per_km": pace_dec,
        "avg_hr": item.get("averageHR"),
        "max_hr": item.get("maxHR"),
        "source_type": item.get(type_key),
    }


def _excluded(rep: dict, reason: str) -> dict:
    return {
        "source_type": rep["source_type"],
        "distance_m": rep["distance_m"],
        "pace_per_km": rep["pace_per_km"],
        "reason": reason,
    }


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _guard_work_reps(work: list[dict], structured_distance_m: float) -> tuple[list[dict], list[dict]]:
    """Drop split/lap entries that carry a work label but are not work reps.

    Three ways a labelled rep is not one: it is a fragment (metres long), it is
    a cool-down or warm-up jog that kept the label, or it is the whole session
    under one label — the last being how an unstructured run turns into a "rep"
    at the activity's own average pace, which is exactly the number no consumer
    may ever be handed.
    """
    kept, dropped = [], []
    for rep in work:
        if rep["distance_m"] < _MIN_REP_DISTANCE_M or rep["duration_s"] < _MIN_REP_DURATION_S:
            dropped.append(_excluded(rep, "too_short"))
        else:
            kept.append(rep)

    lone = kept[0] if len(kept) == 1 else None
    if lone and structured_distance_m > 0 and lone["distance_m"] / structured_distance_m >= _LONE_REP_COVERAGE:
        return [], dropped + [_excluded(lone, "covers_whole_activity")]

    paced = [r for r in kept if r["pace_min_per_km"] is not None]
    if len(paced) > 1:
        limit = _median([r["pace_min_per_km"] for r in paced]) + _MAX_REP_PACE_DEVIATION_S / 60
        outliers = [r for r in paced if r["pace_min_per_km"] > limit]
        if outliers:
            kept = [r for r in kept if not any(r is o for o in outliers)]
            dropped += [_excluded(r, "pace_outlier") for r in outliers]
    return kept, dropped


def _reps_from_typed_splits(splits: list[dict]) -> tuple[list[dict], list[dict]] | None:
    """Work + rest reps from Garmin's typed splits, or None if none are typed.

    The response interleaves a second, independent run/walk typing (RWD_RUN,
    RWD_WALK, RWD_STAND) that overlaps the interval splits in time — counting
    those would double every rep, so only INTERVAL_* entries are considered.
    """
    interval = [s for s in splits if str(s.get("type") or "").startswith(_TYPED_SPLIT_PREFIX)]
    if not interval:
        return None
    work = [_fmt_rep(s, "work", "type") for s in interval if s.get("type") in _TYPED_WORK_TYPES]
    rest = [_fmt_rep(s, "rest", "type") for s in interval if s.get("type") in _TYPED_REST_TYPES]
    # A lone INTERVAL_* split is an unstructured run that Garmin labelled in one
    # piece (an ordinary easy run comes back as a single INTERVAL_ACTIVE or
    # INTERVAL_WARMUP covering the whole distance). There is no structure to read.
    if len(interval) == 1:
        return [], [_excluded(r, "no_interval_structure") for r in work]
    structured = sum(s.get("distance") or 0 for s in interval)
    kept, dropped = _guard_work_reps(work, structured)
    return kept + rest, dropped


def _reps_from_lap_intensity(laps: list[dict]) -> tuple[list[dict], list[dict]]:
    """Fallback classification from lap `intensityType`, for untyped activities.

    Uniform intensity across every lap carries no information (2026-08-02 tagged
    all 13 laps INTERVAL). Returning nothing is correct there: the alternative is
    handing back warm-up and cool-down as reps.
    """
    labels = {lap.get("intensityType") for lap in laps}
    if len(labels) < 2:
        return [], [{"source_type": next(iter(labels), None), "reason": "uniform_intensity"}]
    work = [_fmt_rep(lap, "work", "intensityType") for lap in laps if lap.get("intensityType") in _LAP_WORK_TYPES]
    rest = [_fmt_rep(lap, "rest", "intensityType") for lap in laps if lap.get("intensityType") in _LAP_REST_TYPES]
    kept, dropped = _guard_work_reps(work, sum(lap.get("distance") or 0 for lap in laps))
    return kept + rest, dropped


def _summarize_work(reps: list[dict]) -> dict:
    work = [r for r in reps if r["is_work_rep"]]
    paces = [r["pace_min_per_km"] for r in work if r["pace_min_per_km"] is not None]
    hrs = [r["avg_hr"] for r in work if isinstance(r["avg_hr"], (int, float))]
    max_hrs = [r["max_hr"] for r in work if isinstance(r["max_hr"], (int, float))]
    avg_pace_str, avg_pace_dec = (None, None)
    if paces:
        avg_pace_str, avg_pace_dec = _speed_to_pace(1000 / (sum(paces) / len(paces) * 60))
    return {
        "count": len(work),
        "total_distance_m": round(sum(r["distance_m"] for r in work), 1),
        "avg_pace_per_km": avg_pace_str,
        "avg_pace_min_per_km": avg_pace_dec,
        "fastest_pace_per_km": _speed_to_pace(1000 / (min(paces) * 60))[0] if paces else None,
        "slowest_pace_per_km": _speed_to_pace(1000 / (max(paces) * 60))[0] if paces else None,
        "pace_spread_s_per_km": round((max(paces) - min(paces)) * 60) if paces else None,
        "avg_hr": round(sum(hrs) / len(hrs), 1) if hrs else None,
        "max_hr": max(max_hrs) if max_hrs else None,
    }


# How many recent activities to look through for running ones. Runs are a
# minority of Garmin activity types (strength, tennis, hiking all interleave),
# so a window sized to the rep-scan limit would come back near-empty.
_ACTIVITY_SCAN_WINDOW = 50

# Garmin has a running type per surface — running, track_running, trail_running,
# treadmill_running, virtual_run. Matching the whole family matters: the
# 2026-08-16 track session is `track_running`, and a prefix match on "running"
# silently drops exactly the structured track sessions worth comparing against.
_RUNNING_TYPE_MARKER = "run"


def _is_running(activity: dict) -> bool:
    return _RUNNING_TYPE_MARKER in str((activity.get("activityType") or {}).get("typeKey") or "")


def _classify_intervals(client: Garmin, activity_id: str) -> dict:
    """Work reps for one activity, preferring typed splits over lap intensity."""
    try:
        splits = (client.get_activity_typed_splits(activity_id) or {}).get("splits") or []
    except Exception:  # noqa: BLE001 - an activity without typed splits falls back below
        splits = []
    classified = _reps_from_typed_splits(splits) if splits else None
    if classified is not None:
        reps, dropped = classified
        source = "typed_splits"
    else:
        laps = (client.get_activity_splits(activity_id) or {}).get("lapDTOs") or []
        reps, dropped = _reps_from_lap_intensity(laps)
        source = "garmin_intensity"
    return {
        "activity_id": activity_id,
        "classified_by": source,
        "reps": reps,
        "work_summary": _summarize_work(reps),
        "excluded": dropped,
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
    A "timing" object gives the explicit elapsed / timer / moving / stopped
    breakdown so the caller never has to guess which duration is the
    finish time. For a per-lap breakdown (interval or km splits), call
    get_activity_laps. Bulky per-sample metric arrays and GPS polylines are
    stripped to keep the response small — use Garmin Connect directly for
    raw sample data.
    """

    def build(c: Garmin) -> dict:
        summary = c.get_activity(activity_id)
        detail = c.get_activity_details(activity_id)
        details = {k: v for k, v in detail.items() if k not in _ACTIVITY_DETAIL_STRIP_KEYS}
        return {**summary, "timing": _fmt_timing(summary), "details": details}

    return _tool_call(build)


@mcp.tool()
def get_activity_laps(activity_id: str, include_gps: bool = False) -> str:
    """Get the individual laps of an activity (intervals or auto-km splits).

    `activity_id` is the numeric id from list_recent_activities. Returns a
    JSON object with "lap_count" and a "laps" array, one entry per real
    Garmin lap: lap number, distance_m, the three durations (elapsed_time_s,
    timer_time_s, moving_time_s), pace_min_per_km, avg/max HR, avg/max power,
    avg_run_cadence, "intensity" (e.g. WARMUP/ACTIVE/REST — the interval
    structure), and elevation gain/loss. Use this instead of
    get_activity_details when you need per-lap threshold or split analysis.
    GPS start/end coordinates are omitted by default; pass include_gps=true
    to include them.
    """

    def build(c: Garmin) -> dict:
        laps = c.get_activity_splits(activity_id).get("lapDTOs") or []
        return {
            "activity_id": activity_id,
            "lap_count": len(laps),
            "laps": [_fmt_lap(lap, include_gps) for lap in laps],
        }

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


@mcp.tool()
def get_performance_metrics(date: str | None = None) -> str:
    """Get current running fitness/threshold snapshot: lactate threshold, VO2 max, race predictions.

    Answers "where is my fitness right now?" in one call. `date` is optional
    ("YYYY-MM-DD", defaults to today) and only affects which day's VO2 max is
    read; lactate threshold and race predictions always return Garmin's latest
    available values. Returns a JSON object with three independent sections:

    - "lactate_threshold": threshold heart rate (LTHR, bpm) and threshold pace
      ("M:SS/km" plus decimal min/km and raw m/s) — the numbers used to set
      training zones. (Garmin's "heart rate threshold" and "lactate threshold"
      are the same value.)
    - "vo2max": VO2 max (decimal precise value + rounded) and fitness age.
    - "race_predictions": predicted 5k / 10k / half-marathon / marathon finish
      times as clock strings.

    Each section is fetched independently: if one Garmin endpoint is
    unavailable it becomes {"error": ...} while the others still return. Every
    section carries its own "measured_date" so stale values are visible.
    """
    cdate = date or _today_local().isoformat()

    def build(c: Garmin) -> dict:
        def section(fetch: Callable[[], Any], fmt: Callable[[Any], dict]) -> dict:
            try:
                return fmt(fetch())
            except Exception as exc:  # noqa: BLE001 - one dead endpoint shouldn't sink the others
                return {"error": f"unavailable: {exc}"}

        return {
            "as_of": cdate,
            "lactate_threshold": section(lambda: c.get_lactate_threshold(latest=True), _fmt_lactate_threshold),
            "vo2max": section(lambda: c.get_max_metrics(cdate), _fmt_vo2max),
            "race_predictions": section(c.get_race_predictions, _fmt_race_predictions),
        }

    return _tool_call(build)


@mcp.tool()
def get_personal_records() -> str:
    """Get the user's Garmin personal records (running PBs and bests).

    Returns a JSON array, one object per record: `record` (human label for the
    common running types — fastest 1km/1mile/5km/10km, longest run — or null
    for other/unmapped types), `type_id` (Garmin's raw record type), `value`
    (raw: seconds for time records, metres for distance), `value_formatted`
    (clock string or "X.XX km" for mapped running types, else null),
    `activity_id`, and `date`. The raw `type_id` and `value` are always kept so
    an unmapped or non-running record is still usable, just unlabeled.
    """
    return _tool_call(lambda c: [_fmt_personal_record(r) for r in (c.get_personal_record() or []) if isinstance(r, dict)])


@mcp.tool()
def get_threshold_history(start_date: str, end_date: str, aggregation: str = "weekly") -> str:
    """Get lactate-threshold heart rate + pace over time (the threshold trend).

    `start_date` and `end_date` are "YYYY-MM-DD" (start no more than a year
    before end). `aggregation` is one of "daily", "weekly" (default), "monthly",
    "yearly". Returns a JSON object with "points": a dated series, each entry
    giving `lthr_bpm` (threshold heart rate) and threshold pace (`pace_per_km`
    "M:SS", decimal `pace_min_per_km`, raw `speed_m_s`). Use this to see whether
    threshold HR/pace is trending up over a training block — for the single
    latest value use get_performance_metrics instead. If Garmin's range payload
    can't be parsed into a series it is returned verbatim under "raw".
    """

    def build(c: Garmin) -> dict:
        raw = c.get_lactate_threshold(
            latest=False, start_date=start_date, end_date=end_date, aggregation=aggregation
        )
        return _fmt_threshold_history(raw)

    return _tool_call(build)


@mcp.tool()
def get_running_threshold() -> str:
    """Get the running threshold anchor: lactate threshold pace, LTHR, and HR zones.

    This is the number a threshold or interval session should be prescribed
    from — never an activity's average pace. Returns a JSON object with:

    - "lactate_threshold": threshold heart rate (LTHR, bpm) and threshold pace
      ("M:SS/km" plus decimal min/km and raw m/s), with the date Garmin last
      measured it. Check that date: a threshold from months ago is stale.
    - "heart_rate_zones": the five zones with explicit floor_bpm and
      ceiling_bpm, plus max and resting HR and the method Garmin used.

    Each section is fetched independently, so one dead endpoint leaves the
    other intact. For VO2 max and race predictions use get_performance_metrics.
    """

    def build(c: Garmin) -> dict:
        def section(fetch: Callable[[], Any], fmt: Callable[[Any], dict]) -> dict:
            try:
                return fmt(fetch())
            except Exception as exc:  # noqa: BLE001 - one dead endpoint shouldn't sink the other
                return {"error": f"unavailable: {exc}"}

        return {
            "lactate_threshold": section(lambda: c.get_lactate_threshold(latest=True), _fmt_lactate_threshold),
            "heart_rate_zones": section(c.get_heart_rate_zones, _fmt_hr_zones),
        }

    return _tool_call(build)


@mcp.tool()
def get_activity_intervals(activity_id: str) -> str:
    """Get the work reps of an interval session, with warm-up, rests and cool-down excluded.

    `activity_id` is the numeric id from list_recent_activities. Use this
    instead of get_activity_laps when you need what the reps were actually run
    at: it answers "how fast were the intervals", not "how fast was the run".

    Returns a JSON object with "classified_by" — `typed_splits` (Garmin's
    recorded workout structure, preferred) or `garmin_intensity` (the lap
    intensity field, used only when an activity has no typed structure) — so
    the caller can see how certain the classification is. "reps" holds one
    entry per work rep and rest, each with `is_work_rep`, distance_m,
    duration_s, pace_per_km, avg/max HR. "work_summary" aggregates the work
    reps only (count, total distance, average/fastest/slowest pace, pace
    spread, HR). "excluded" lists what was dropped and why.

    An unstructured run returns **zero** work reps rather than one rep covering
    the whole activity: the activity's own average pace is never a rep pace.
    """
    return _tool_call(lambda c: _classify_intervals(c, activity_id))


@mcp.tool()
def find_comparable_intervals(target_distance_m: float, tolerance_pct: float = 10.0, activity_limit: int = 10) -> str:
    """Find recent work reps of a given distance, across your last running activities.

    Answers "what have I been running 1000m reps at lately?" in one call,
    replacing list-activities → laps-per-activity → filter-by-hand. Every rep
    returned has been through the same classification as
    get_activity_intervals, so warm-ups, rests and cool-downs never appear.

    `target_distance_m` is the rep distance to match (e.g. 1000). Reps within
    `tolerance_pct` of it are returned (default 10%, so 900-1100 m).
    `activity_limit` (default 10) caps how many running activities are opened —
    each one costs a Garmin call — and they are taken from the most recent
    activities of any type, so a long stretch without running returns fewer.

    Returns a JSON object with "matches" (each rep plus its activity_id, date
    and name, newest first), "match_count", and "activities_scanned".
    """

    def build(c: Garmin) -> dict:
        recent = c.get_activities(0, _ACTIVITY_SCAN_WINDOW)
        activities = [a for a in recent if _is_running(a)][:activity_limit]
        low = target_distance_m * (1 - tolerance_pct / 100)
        high = target_distance_m * (1 + tolerance_pct / 100)
        matches = []
        for a in activities:
            activity_id = str(a.get("activityId"))
            result = _classify_intervals(c, activity_id)
            for rep in result["reps"]:
                if rep["is_work_rep"] and low <= rep["distance_m"] <= high:
                    matches.append({
                        "activity_id": activity_id,
                        "date": (a.get("startTimeLocal") or "")[:10],
                        "name": a.get("activityName"),
                        "classified_by": result["classified_by"],
                        **rep,
                    })
        return {
            "target_distance_m": target_distance_m,
            "tolerance_pct": tolerance_pct,
            "distance_range_m": [round(low, 1), round(high, 1)],
            "activities_scanned": [str(a.get("activityId")) for a in activities],
            "match_count": len(matches),
            "matches": matches,
        }

    return _tool_call(build)


def _run_multi_tenant(root: Path, host: str, port: int, prefix: str) -> None:
    """Serve every user's endpoint from one process under <prefix>/<user-id>/mcp."""
    import uvicorn

    app = multitenant.build_multi_tenant_app(mcp, root, prefix)
    uvicorn.run(app, host=host, port=port, log_level="info")


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
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport to serve over (default: stdio). Use streamable-http to host this "
        "server remotely, e.g. as a claude.ai custom connector.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to when --transport=streamable-http (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port to bind to when --transport=streamable-http (default: 8765)",
    )
    parser.add_argument(
        "--path",
        default=None,
        help="URL path the streamable-http endpoint mounts at (default: /mcp). Give it an "
        "unguessable value to use as a lightweight secret when hosting remotely. In "
        f"multi-tenant mode ({multitenant.MULTI_TENANT_ROOT_ENV} set) this is instead the "
        "prefix each user's endpoint hangs off (default: /u), giving <prefix>/<user-id>/mcp "
        "— there the user ID carries the secrecy, so the prefix need not.",
    )
    args = parser.parse_args()

    if args.list_tools:
        _print_tool_list()
        return

    if args.transport == "streamable-http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        root = multitenant.multi_tenant_root()
        if root is not None:
            _run_multi_tenant(root, args.host, args.port, args.path or "/u")
            return
        mcp.settings.streamable_http_path = args.path or "/mcp"

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
