# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); this project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `get_activity_laps(activity_id, include_gps=False)` — per-lap breakdown
  (distance, elapsed/timer/moving durations, pace, avg/max HR, avg/max power,
  cadence, interval intensity, elevation). Enables interval and split
  analysis instead of only the aggregate summary. GPS coordinates are
  stripped by default and opt-in via `include_gps`.
- `get_activity_details` now includes an explicit `timing` object
  (`elapsed_time_s` / `timer_time_s` / `moving_time_s` / `stopped_time_s`) so
  callers no longer have to guess which duration is the finish time.
