"""Tests for threshold + work-rep classification, against real Garmin payloads.

The fixtures in tests/fixtures/ are unedited responses from Garmin, captured
2026-08-22, with GPS coordinates and body weight stripped (this repo is public
and the runs start at a home address). They are here because every guard in the
classifier exists for one specific real session — a synthetic payload would let
the guard pass while the session it was written for still fails.

No network calls: the Garmin client getter is monkeypatched with a fake client
that serves the fixtures.
"""
import json
import pathlib

import pytest

from garmin_mcp import server

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

# The sessions each guard was written against.
CLEAN_1000 = "23823428717"  # 02.08 6x1000m — every lap tagged INTERVAL, which is why laps alone fail
CLEAN_2000 = "23885353686"  # 07.08 3x10min — the one activity whose lap intensity is correct
EASY_RUN = "23935979150"  # 11.08 unstructured — one INTERVAL_WARMUP covering the whole run
COOLDOWN_TAGGED_ACTIVE = "23996955436"  # 16.08 3x2000m + a 7:29/km cool-down tagged ACTIVE
FRAGMENTED = "24048644063"  # 20.08 6x1000m + four 1-metre ACTIVE fragments
UNSTRUCTURED = "24060441521"  # 21.08 unstructured — one INTERVAL_ACTIVE over all 9437 m

# Well below the classifier's own lone-rep threshold: any real rep is a fraction of the run.
_LONE_REP_SHARE = 0.9


def load(name):
    return json.loads((FIXTURES / name).read_text())


class FixtureClient:
    """Serves captured Garmin payloads, and records what was asked for."""

    def __init__(self):
        self.typed_split_calls = []

    def get_activity_typed_splits(self, activity_id):
        self.typed_split_calls.append(activity_id)
        return load(f"typed_splits_{activity_id}.json")

    def get_activity_splits(self, activity_id):
        return load(f"laps_{activity_id}.json")

    def get_lactate_threshold(self, latest=True, **kwargs):
        return load("lactate_threshold.json")

    def get_heart_rate_zones(self):
        return load("hr_zones.json")

    def get_activities(self, start, limit):
        return load("recent_activities.json")[:limit]


@pytest.fixture
def client(monkeypatch):
    fake = FixtureClient()
    monkeypatch.setattr(server, "get_client", lambda: fake)
    return fake


def work_paces(result):
    return [r["pace_per_km"] for r in result["reps"] if r["is_work_rep"]]


def excluded_reasons(result):
    return sorted({e["reason"] for e in result["excluded"]})


def test_running_threshold_returns_lt_pace_lthr_and_zones(client):
    result = json.loads(server.get_running_threshold())
    lt = result["lactate_threshold"]
    assert lt["pace_per_km"] == "4:00"
    assert lt["heart_rate_bpm"] == 172
    assert lt["measured_date"].startswith("2026-08-11")

    zones = result["heart_rate_zones"]
    assert zones["lthr_bpm"] == 172
    assert zones["max_hr_bpm"] == 188
    assert zones["resting_hr_bpm"] == 56
    assert [z["floor_bpm"] for z in zones["zones"]] == [94, 135, 154, 164, 173]
    # No ceiling field exists in Garmin's payload: each zone ends one below the
    # next zone's floor, and zone 5 runs to max HR.
    assert [z["ceiling_bpm"] for z in zones["zones"]] == [134, 153, 163, 172, 188]


def test_typed_splits_classify_the_session_that_breaks_lap_intensity(client):
    """02.08: all 13 laps are tagged INTERVAL, so only typed splits can read it."""
    result = json.loads(server.get_activity_intervals(CLEAN_1000))
    assert result["classified_by"] == "typed_splits"
    assert work_paces(result) == ["3:45", "3:49", "3:53", "3:52", "3:56", "3:55"]
    assert result["work_summary"]["count"] == 6
    assert result["work_summary"]["pace_spread_s_per_km"] == 10


def test_parallel_run_walk_typing_never_leaks_into_reps(client):
    """The same payload carries 17 RWD_* splits overlapping the reps in time."""
    raw = load(f"typed_splits_{CLEAN_1000}.json")["splits"]
    assert sum(1 for s in raw if s["type"].startswith("RWD_")) == 17

    result = json.loads(server.get_activity_intervals(CLEAN_1000))
    assert all(r["source_type"].startswith("INTERVAL_") for r in result["reps"])
    assert len(result["reps"]) == 11  # 6 work + 5 recoveries; warm-up and cool-down excluded


def test_warmup_and_rests_are_excluded_from_work_reps(client):
    result = json.loads(server.get_activity_intervals(CLEAN_2000))
    assert work_paces(result) == ["4:10", "4:18", "4:40"]
    assert [r["kind"] for r in result["reps"]] == ["work", "work", "work", "rest", "rest"]
    # The 4:40 rep is 22 s/km off the median and must survive — it is a real,
    # hilly third rep, not a cool-down.
    assert result["work_summary"]["slowest_pace_per_km"] == "4:40"


def test_cooldown_tagged_active_is_dropped_as_a_pace_outlier(client):
    """16.08: Garmin tagged the 1550 m cool-down jog INTERVAL_ACTIVE."""
    result = json.loads(server.get_activity_intervals(COOLDOWN_TAGGED_ACTIVE))
    assert work_paces(result) == ["3:51", "3:50", "3:50"]
    assert [r["distance_m"] for r in result["reps"] if r["is_work_rep"]] == [2000.0, 2000.0, 2000.0]
    assert ["7:29"] == [e["pace_per_km"] for e in result["excluded"] if e["reason"] == "pace_outlier"]


def test_metre_long_fragments_are_dropped(client):
    """20.08: four ACTIVE splits of 1 m and 14-20 minutes sit among six real reps."""
    result = json.loads(server.get_activity_intervals(FRAGMENTED))
    assert work_paces(result) == ["3:58", "3:54", "3:55", "3:59", "4:01", "4:05"]
    assert [e["distance_m"] for e in result["excluded"] if e["reason"] == "too_short"] == [1.2, 1.0, 1.0, 0.9]


def test_unstructured_run_yields_no_reps_instead_of_the_whole_activity(client):
    """21.08: one INTERVAL_ACTIVE covers all 9437 m at the activity's own pace."""
    raw = load(f"typed_splits_{UNSTRUCTURED}.json")["splits"]
    whole = [s for s in raw if s["type"] == "INTERVAL_ACTIVE"]
    assert len(whole) == 1 and round(whole[0]["distance"]) == 9437

    result = json.loads(server.get_activity_intervals(UNSTRUCTURED))
    assert result["work_summary"]["count"] == 0
    assert work_paces(result) == []
    assert excluded_reasons(result) == ["no_interval_structure"]


def test_easy_run_labelled_warmup_yields_no_reps(client):
    result = json.loads(server.get_activity_intervals(EASY_RUN))
    assert result["work_summary"]["count"] == 0


@pytest.mark.parametrize("activity_id", [CLEAN_1000, CLEAN_2000, COOLDOWN_TAGGED_ACTIVE, FRAGMENTED, UNSTRUCTURED, EASY_RUN])
def test_no_rep_is_ever_the_whole_activity(client, activity_id):
    """The invariant: a rep pace may never be the activity's average pace."""
    total_m = sum(lap.get("distance") or 0 for lap in load(f"laps_{activity_id}.json")["lapDTOs"])
    result = json.loads(server.get_activity_intervals(activity_id))
    for rep in result["reps"]:
        if rep["is_work_rep"]:
            assert rep["distance_m"] < total_m * _LONE_REP_SHARE



def test_find_comparable_intervals_matches_target_distance(client):
    result = json.loads(server.find_comparable_intervals(1000))
    assert {m["activity_id"] for m in result["matches"]} == {CLEAN_1000, FRAGMENTED}
    assert result["match_count"] == 12  # six reps in each session
    assert all(900 <= m["distance_m"] <= 1100 for m in result["matches"])
    assert all(m["is_work_rep"] for m in result["matches"])


def test_find_comparable_intervals_excludes_other_rep_distances(client):
    result = json.loads(server.find_comparable_intervals(2000))
    assert {m["activity_id"] for m in result["matches"]} == {COOLDOWN_TAGGED_ACTIVE}
    assert [m["pace_per_km"] for m in result["matches"]] == ["3:51", "3:50", "3:50"]


def test_scan_covers_every_running_surface_and_skips_other_sports(client):
    """The 16.08 session is `track_running`; a prefix match on "running" drops it."""
    window = load("recent_activities.json")
    assert {a["activityType"]["typeKey"] for a in window} >= {"running", "track_running", "strength_training"}

    result = json.loads(server.find_comparable_intervals(2000, activity_limit=25))
    assert COOLDOWN_TAGGED_ACTIVE in result["activities_scanned"]
    non_runs = {a["activityId"] for a in window if "run" not in a["activityType"]["typeKey"]}
    assert not non_runs & {int(a) for a in result["activities_scanned"]}


def test_find_comparable_intervals_caps_the_activities_it_opens(client):
    server.find_comparable_intervals(1000, activity_limit=2)
    assert len(client.typed_split_calls) <= 2


class UntypedClient(FixtureClient):
    """An activity Garmin never gave typed splits for."""

    def get_activity_typed_splits(self, activity_id):
        return {"activityId": activity_id, "splits": []}


def test_falls_back_to_lap_intensity_when_no_typed_splits(monkeypatch):
    monkeypatch.setattr(server, "get_client", lambda: UntypedClient())
    result = json.loads(server.get_activity_intervals(CLEAN_2000))
    assert result["classified_by"] == "garmin_intensity"
    assert work_paces(result) == ["4:10", "4:18", "4:40"]


def test_uniform_lap_intensity_classifies_nothing(monkeypatch):
    """02.08 tags every lap INTERVAL; the fallback must not read that as six reps."""
    monkeypatch.setattr(server, "get_client", lambda: UntypedClient())
    result = json.loads(server.get_activity_intervals(CLEAN_1000))
    assert result["classified_by"] == "garmin_intensity"
    assert result["work_summary"]["count"] == 0
    assert result["excluded"] == [
        {"source_type": "INTERVAL", "distance_m": None, "pace_per_km": None, "reason": "uniform_intensity"}
    ]


class SyntheticClient(FixtureClient):
    """A typed-split payload assembled by hand, for shapes no real session provides."""

    def __init__(self, splits):
        super().__init__()
        self._splits = splits

    def get_activity_typed_splits(self, activity_id):
        self.typed_split_calls.append(activity_id)
        return {"activityId": activity_id, "splits": self._splits}


def _split(split_type, distance_m, pace_min_per_km):
    return {"type": split_type, "distance": distance_m, "duration": distance_m / 1000 * pace_min_per_km * 60}


def test_mixed_rep_lengths_bias_towards_exclusion_and_say_so(monkeypatch):
    """A pyramid session drops its slow half — deliberately, and visibly.

    The pace guard measures against the median of all candidates, so a session
    mixing 400s at 3:15 with 2000s at 4:10 reads the 2000s as outliers. That is
    the wrong call for a pyramid, and the right bias for this tool: reporting a
    cool-down as a rep is the failure it exists to prevent, and dropping in the
    other direction is at least legible — every dropped rep is listed in
    "excluded" with its pace. Change this and the 16.08 cool-down comes back.
    """
    splits = [_split("INTERVAL_WARMUP", 1000, 5.5)]
    splits += [_split("INTERVAL_ACTIVE", 2000, 4 + 10 / 60) for _ in range(2)]
    splits += [_split("INTERVAL_ACTIVE", 400, 3 + 15 / 60) for _ in range(4)]
    monkeypatch.setattr(server, "get_client", lambda: SyntheticClient(splits))

    result = json.loads(server.get_activity_intervals("synthetic"))
    assert work_paces(result) == ["3:15", "3:15", "3:15", "3:15"]
    assert [(e["distance_m"], e["pace_per_km"], e["reason"]) for e in result["excluded"]] == [
        (2000.0, "4:10", "pace_outlier"),
        (2000.0, "4:10", "pace_outlier"),
    ]
