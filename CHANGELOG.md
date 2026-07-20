# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); this project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `get_personal_records()` — personal records / PBs. Labels the common running
  records (fastest 1km/1mile/5km/10km, longest run) and formats their values
  (clock string for time records, "X.XX km" for distance); any other/unmapped
  record keeps its raw `type_id` and `value` with a null label so it is never
  mislabeled.
- `get_threshold_history(start_date, end_date, aggregation="weekly")` —
  lactate-threshold heart rate + pace as a dated trend series (uses the ranged
  `get_lactate_threshold`), for tracking whether threshold is improving across a
  training block. Shape-tolerant: an unparseable range payload is returned
  verbatim under `raw` rather than dropped.
- `get_performance_metrics(date=None)` — running fitness/threshold snapshot in
  one call: lactate threshold (threshold heart rate + threshold pace, converted
  from Garmin's m/s to "M:SS/km"), VO2 max + fitness age, and 5k/10k/half/
  marathon race predictions (converted from seconds to clock strings). Each of
  the three sections is fetched independently, so one unavailable endpoint
  becomes `{"error": ...}` while the others still return, and every section
  carries its own `measured_date` so stale values are visible. Covers the
  "prestasjons-/terskelhistorikk" item from the roadmap's Phase 3.
- `get_activity_laps(activity_id, include_gps=False)` — per-lap breakdown
  (distance, elapsed/timer/moving durations, pace, avg/max HR, avg/max power,
  cadence, interval intensity, elevation). Enables interval and split
  analysis instead of only the aggregate summary. GPS coordinates are
  stripped by default and opt-in via `include_gps`.
- `get_activity_details` now includes an explicit `timing` object
  (`elapsed_time_s` / `timer_time_s` / `moving_time_s` / `stopped_time_s`) so
  callers no longer have to guess which duration is the finish time.
