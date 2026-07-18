"""Tests for garmin_mcp tool formatting/stripping logic.

No network calls: the Garmin client getter is monkeypatched with a fake
client that returns canned data shaped like real garminconnect responses.
"""
import json
import subprocess
import sys

from garmin_mcp import server


class FakeClient:
    def get_activities(self, start, limit):
        return [
            {
                "activityId": 123,
                "activityName": "Morning Run",
                "activityType": {"typeKey": "running"},
                "distance": 5000.0,
                "duration": 1500.0,
                "averageHR": 150,
                "startTimeLocal": "2026-07-17 07:00:00",
            }
        ]

    def get_activity(self, activity_id):
        return {
            "activityId": int(activity_id),
            "activityName": "Morning Run",
            "summaryDTO": {
                "distance": 5000.0,
                "duration": 1500.0,
                "movingDuration": 1490.0,
                "elapsedDuration": 1525.0,
            },
        }

    def get_activity_details(self, activity_id):
        return {
            "activityId": int(activity_id),
            "detailsAvailable": True,
            "measurementCount": 42,
            "metricDescriptors": [{"key": "heartRate"}] * 12,
            "activityDetailMetrics": [[1, 2, 3]] * 1000,
            "geoPolylineDTO": {"polyline": ["a"] * 500},
        }

    def get_activity_splits(self, activity_id):
        return {
            "lapDTOs": [
                {
                    "lapIndex": 1,
                    "distance": 1000.0,
                    "duration": 240.0,
                    "movingDuration": 238.0,
                    "elapsedDuration": 250.0,
                    "averageHR": 164.0,
                    "maxHR": 170.0,
                    "averagePower": 498.0,
                    "maxPower": 520.0,
                    "averageRunCadence": 180.0,
                    "intensityType": "ACTIVE",
                    "elevationGain": 5.0,
                    "elevationLoss": 2.0,
                    "startLatitude": 58.1,
                    "startLongitude": 8.0,
                }
            ]
        }

    def get_sleep_data(self, date):
        return {
            "dailySleepDTO": {"calendarDate": date, "sleepTimeSeconds": 25000, "sleepScores": {"overall": {"value": 80}}},
            "restingHeartRate": 48,
            "sleepLevels": [{"x": 1}] * 200,
            "sleepMovement": [{"x": 1}] * 200,
        }

    def get_heart_rates(self, date):
        return {
            "calendarDate": date,
            "minHeartRate": 45,
            "maxHeartRate": 172,
            "restingHeartRate": 48,
            "lastSevenDaysAvgRestingHeartRate": 50,
            "heartRateValues": [[1, 60]] * 1440,
        }

    def get_body_battery(self, start_date, end_date):
        return [
            {
                "date": start_date,
                "charged": 69,
                "drained": 52,
                "bodyBatteryValuesArray": [[1, 14], [2, 77], [3, 20]],
            }
        ]


def test_list_recent_activities_formats_and_strips(monkeypatch):
    monkeypatch.setattr(server, "get_client", lambda: FakeClient())
    result = json.loads(server.list_recent_activities(5))
    assert result == [
        {
            "id": 123,
            "date": "2026-07-17 07:00",
            "type": "running",
            "name": "Morning Run",
            "distance_km": 5.0,
            "duration_min": 25.0,
            "avg_hr": 150,
            "pace_min_per_km": 5.0,
        }
    ]


def test_get_activity_details_strips_bulky_keys(monkeypatch):
    monkeypatch.setattr(server, "get_client", lambda: FakeClient())
    result = json.loads(server.get_activity_details("123"))
    assert result["activityId"] == 123
    assert result["activityName"] == "Morning Run"
    assert "details" in result
    assert result["details"] == {"activityId": 123, "detailsAvailable": True, "measurementCount": 42}
    for bulky_key in ("metricDescriptors", "activityDetailMetrics", "geoPolylineDTO"):
        assert bulky_key not in result["details"]
    assert result["timing"] == {
        "elapsed_time_s": 1525.0,
        "timer_time_s": 1500.0,
        "moving_time_s": 1490.0,
        "stopped_time_s": 35.0,
    }


def test_get_activity_laps_formats_and_strips_gps(monkeypatch):
    monkeypatch.setattr(server, "get_client", lambda: FakeClient())
    result = json.loads(server.get_activity_laps("123"))
    assert result["lap_count"] == 1
    lap = result["laps"][0]
    assert lap["lap"] == 1
    assert lap["distance_m"] == 1000.0
    assert lap["elapsed_time_s"] == 250.0
    assert lap["timer_time_s"] == 240.0
    assert lap["moving_time_s"] == 238.0
    assert lap["pace_min_per_km"] == 3.97  # (238/60) / (1000/1000)
    assert lap["avg_hr"] == 164.0
    assert lap["intensity"] == "ACTIVE"
    # GPS omitted by default
    for k in ("startLatitude", "startLongitude", "endLatitude", "endLongitude"):
        assert k not in lap


def test_get_activity_laps_includes_gps_on_request(monkeypatch):
    monkeypatch.setattr(server, "get_client", lambda: FakeClient())
    result = json.loads(server.get_activity_laps("123", include_gps=True))
    lap = result["laps"][0]
    assert lap["startLatitude"] == 58.1
    assert lap["startLongitude"] == 8.0


def test_get_sleep_strips_per_minute_arrays(monkeypatch):
    monkeypatch.setattr(server, "get_client", lambda: FakeClient())
    result = json.loads(server.get_sleep("2026-07-17"))
    assert result["dailySleepDTO"]["sleepTimeSeconds"] == 25000
    assert result["restingHeartRate"] == 48
    assert "sleepLevels" not in result
    assert "sleepMovement" not in result


def test_get_heart_rate_strips_per_minute_values(monkeypatch):
    monkeypatch.setattr(server, "get_client", lambda: FakeClient())
    result = json.loads(server.get_heart_rate("2026-07-17"))
    assert result["minHeartRate"] == 45
    assert result["restingHeartRate"] == 48
    assert "heartRateValues" not in result


def test_get_body_battery_derives_highest_lowest(monkeypatch):
    monkeypatch.setattr(server, "get_client", lambda: FakeClient())
    result = json.loads(server.get_body_battery("2026-07-17", "2026-07-17"))
    assert result == [{"date": "2026-07-17", "charged": 69, "drained": 52, "highest": 77, "lowest": 14}]


def test_tool_call_returns_json_error_on_failure(monkeypatch):
    def broken_client():
        raise RuntimeError("no auth configured")

    monkeypatch.setattr(server, "get_client", broken_client)
    result = json.loads(server.list_recent_activities(5))
    assert "error" in result


def test_list_tools_exits_zero_without_network():
    proc = subprocess.run(
        [sys.executable, "-m", "garmin_mcp.server", "--list-tools"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    assert "list_recent_activities" in proc.stdout
    assert "get_training_status" in proc.stdout
