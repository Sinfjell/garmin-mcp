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

    def get_max_metrics(self, cdate):
        return [
            {
                "generic": {
                    "calendarDate": cdate,
                    "vo2MaxValue": 59.0,
                    "vo2MaxPreciseValue": 59.2,
                    "fitnessAge": 25,
                }
            }
        ]

    def get_race_predictions(self):
        return {
            "calendarDate": "2026-07-20",
            "time5K": 1155,  # 19:15
            "time10K": 2360,  # 39:20
            "timeHalfMarathon": 5250,  # 1:27:30
            "timeMarathon": 11100,  # 3:05:00
        }

    def get_personal_record(self):
        return [
            {"typeId": 4, "value": 2338.0, "activityId": 999, "prStartTimeGmtFormatted": "2026-07-11T18:00:00.0"},  # 10k 38:58
            {"typeId": 3, "value": 1155.0, "activityId": 998, "prStartTimeGmt": "2026-06-01T10:00:00.0"},  # 5k 19:15
            {"typeId": 7, "value": 17120.0, "activityId": 997, "prStartTimeGmtFormatted": "2026-06-20T09:00:00.0"},  # longest run 17.12 km
            {"typeId": 99, "value": 12345.0, "activityId": 996},  # unknown type -> unlabeled
        ]

    def get_lactate_threshold(self, latest=True, start_date=None, end_date=None, aggregation="weekly"):
        if latest:
            return {
                "speed_and_heart_rate": {"calendarDate": "2026-07-15", "speed": 4.31, "heartRate": 173},
                "power": {},
            }
        return {
            "speed": [
                {"calendarDate": "2026-06-01", "value": 4.10},
                {"calendarDate": "2026-07-01", "value": 4.31},
            ],
            "heart_rate": [
                {"calendarDate": "2026-06-01", "value": 170},
                {"calendarDate": "2026-07-01", "value": 173},
            ],
            "power": [],
        }


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


def test_get_performance_metrics_reshapes_all_sections(monkeypatch):
    monkeypatch.setattr(server, "get_client", lambda: FakeClient())
    result = json.loads(server.get_performance_metrics("2026-07-20"))

    lt = result["lactate_threshold"]
    assert lt["heart_rate_bpm"] == 173
    assert lt["pace_per_km"] == "3:52"  # 1000/4.31 = 232.0s = 3:52
    assert lt["pace_min_per_km"] == 3.87
    assert lt["speed_m_s"] == 4.31
    assert lt["measured_date"] == "2026-07-15"

    vo2 = result["vo2max"]
    assert vo2["value"] == 59.2
    assert vo2["rounded_value"] == 59.0
    assert vo2["fitness_age"] == 25

    rp = result["race_predictions"]
    assert rp["5k"] == "19:15"
    assert rp["10k"] == "39:20"
    assert rp["half_marathon"] == "1:27:30"
    assert rp["marathon"] == "3:05:00"


def test_get_performance_metrics_defaults_date_and_isolates_failures(monkeypatch):
    class PartialClient(FakeClient):
        def get_race_predictions(self):
            raise RuntimeError("endpoint 500")

    monkeypatch.setattr(server, "get_client", lambda: PartialClient())
    result = json.loads(server.get_performance_metrics())  # no date -> today
    # A dead endpoint is isolated; the healthy sections still resolve.
    assert "error" in result["race_predictions"]
    assert result["lactate_threshold"]["heart_rate_bpm"] == 173
    assert result["vo2max"]["value"] == 59.2
    assert result["as_of"]  # defaulted, non-empty


def test_get_personal_records_labels_known_and_preserves_unknown(monkeypatch):
    monkeypatch.setattr(server, "get_client", lambda: FakeClient())
    result = json.loads(server.get_personal_records())
    by_type = {r["type_id"]: r for r in result}

    tenk = by_type[4]
    assert tenk["record"] == "fastest_10km"
    assert tenk["value_formatted"] == "38:58"  # 2338s
    assert tenk["date"] == "2026-07-11"
    assert tenk["activity_id"] == 999

    longest = by_type[7]
    assert longest["record"] == "longest_run"
    assert longest["value_formatted"] == "17.12 km"

    # Unknown type is preserved with raw value, never mislabeled
    unknown = by_type[99]
    assert unknown["record"] is None
    assert unknown["value"] == 12345.0
    assert unknown["value_formatted"] is None


def test_get_threshold_history_merges_series(monkeypatch):
    monkeypatch.setattr(server, "get_client", lambda: FakeClient())
    result = json.loads(server.get_threshold_history("2026-06-01", "2026-07-01"))
    pts = result["points"]
    assert len(pts) == 2
    assert pts[0]["date"] == "2026-06-01"
    assert pts[0]["lthr_bpm"] == 170
    assert pts[1]["date"] == "2026-07-01"
    assert pts[1]["lthr_bpm"] == 173
    assert pts[1]["pace_per_km"] == "3:52"  # speed 4.31 m/s


def test_threshold_history_falls_back_to_raw_on_unknown_shape():
    weird = {"speed": "nope", "heart_rate": None, "power": []}
    out = server._fmt_threshold_history(weird)
    assert out["points"] == []
    assert out["raw"] == weird


def test_speed_to_pace_edge_cases():
    assert server._speed_to_pace(0) == (None, None)
    assert server._speed_to_pace(None) == (None, None)
    # rounding carry: 3.3333 m/s -> 300.0s -> 5:00 exactly
    pace_str, _ = server._speed_to_pace(1000 / 300)
    assert pace_str == "5:00"


def test_seconds_to_clock_formats():
    assert server._seconds_to_clock(1155) == "19:15"
    assert server._seconds_to_clock(11100) == "3:05:00"
    assert server._seconds_to_clock(0) is None
    assert server._seconds_to_clock(None) is None


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
    assert "get_performance_metrics" in proc.stdout
    assert "get_personal_records" in proc.stdout
    assert "get_threshold_history" in proc.stdout
