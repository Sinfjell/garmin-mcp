# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); this project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- Docs and `.env.example` point hosted endpoints at `mcp.productivitytech.io`;
  productivitytech.io itself moves to Vercel and no longer serves MCP routes.

### Fixed
- OAuth streamable-http no longer returns **421 Invalid Host** when nginx
  forwards `Host: productivitytech.io` (or whatever
  `GARMIN_OAUTH_PUBLIC_BASE_URL` names). FastMCP's localhost DNS-rebinding
  allowlist is widened from that hostname plus optional
  `GARMIN_OAUTH_ALLOWED_HOSTS`; protection stays on. Localhost Hosts remain
  allowed for direct bind curls. See `docs/oauth-deploy.md`.

### Added
- Official Garmin Connect Developer Program OAuth 2.0 PKCE path behind
  `GARMIN_AUTH_MODE=oauth` (default remains `session`). Separate HTTP entry
  for authorize/callback, Ping/Push webhook stubs, and per-user MCP under
  `/garmin-oauth/<user-id>/mcp`. Health + Activity pull only; tools that need
  training status / lactate threshold / personal records return a clear
  «not available via official API» error. Deploy notes for a **new** systemd
  unit on port 8770: `docs/oauth-deploy.md`. Does not migrate or touch live
  unofficial connectors.
- `get_running_threshold()` — lactate-threshold pace and LTHR with the date
  Garmin measured them, plus the five heart-rate zones. Garmin returns only a
  floor per zone, so the ceiling is derived (next floor minus one, zone 5 up to
  max HR) rather than left for the caller to work out.
- `get_activity_intervals(activity_id)` — the work reps of an interval session,
  with warm-up, rests and cool-down excluded. Classification prefers Garmin's
  recorded workout structure (`get_activity_typed_splits`) over the lap
  `intensity` field, which mislabels in both directions: a 6x1000m session came
  back with all thirteen laps tagged `INTERVAL`, and a track session tagged its
  cool-down jog `ACTIVE`. `classified_by` reports which source was used, and
  `excluded` lists what was dropped and why, so a thin classification is visible
  rather than implied. An unstructured run returns zero reps rather than one rep
  covering the whole activity — the activity's average pace is never a rep pace,
  which is the failure this tool exists to make impossible.
- `find_comparable_intervals(target_distance_m, ...)` — work reps of a given
  distance across recent running activities, replacing list-activities →
  laps-per-activity → filter-by-hand. Matches every Garmin running surface
  (`track_running`, `trail_running`, `virtual_run`, …), not just `running`;
  a prefix match drops exactly the structured track sessions worth comparing.
- `garmin-mcp-tenant import <dir>` — adopt an existing `~/.garminconnect`
  directory as a new tenant, printing the finished connector URL. This is the
  route when Garmin refuses to let a server log in at all: Cloudflare blocks the
  only working login strategy from datacenter IPs, so the person authenticates
  on their own machine — where their IP is fine and their password never leaves
  — and only the token travels. Copies rather than moves, and clamps the
  directory to 0700 and the token to 0600.

### Changed
- Onboarding no longer retries a rate-limited Garmin login by default.
  garminconnect already tries five strategies with its own Cloudflare backoff
  before raising — a single attempt was measured at ~1m45s from the production
  host — so an outer retry only spent more attempts against an IP Garmin was
  already refusing. The mechanism stays configurable (`login_attempts`).

### Added
- Self-service onboarding app (`garmin-mcp-onboarding`, FastAPI): a person logs
  in with their own Garmin account in a browser — including Garmin's one-time
  code — and gets their personal connector URL, without anyone touching the
  host. The password is passed to Garmin and dropped from memory in the same
  call; it is never written to disk, never logged, and specifically is not held
  across the MFA wait. What lands on disk is Garmin's own token, at 0600 in a
  0700 directory. The consent page (Norwegian) states what is stored and how to
  remove it.
- `garmin-mcp-tenant list|delete <user-id>` — admin CLI for token stores.
  Deleting is a command rather than an endpoint on purpose: with
  possession-of-URL auth, a delete endpoint would let anyone who ever saw a URL
  wipe that person's access.
- `docs/e2e-onboarding.md` — the command sequence for verifying a real
  onboarding end to end, including the before/after regression call against an
  existing connector and the password-persistence check.
- Multi-tenant hosting: set `GARMIN_MULTI_TENANT_ROOT=<dir>` and one
  streamable-http process serves several Garmin accounts, one per URL path-ID
  (`<prefix>/<user-id>/mcp` → `<dir>/<user-id>/` as that request's token
  store). User IDs are validated strictly (32–128 chars of `[a-z0-9-]`, which
  rules out path traversal by construction); an unknown or malformed ID gets a
  404 and never falls back to another user's tokens or to the host's
  `GARMIN_EMAIL`/`GARMIN_PASSWORD`. Requests are served statelessly so a
  session cannot outlive the URL that created it. With the variable unset the
  server behaves exactly as before.
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
  from Garmin's scaled speed field to "M:SS/km"), VO2 max + fitness age, and 5k/10k/half/
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

### Fixed
- `get_performance_metrics()` resolved its default date from the *host's*
  timezone, so on a UTC server every session logged after 22:00 Norwegian time
  (23:00 in winter) was attributed to the previous day. "Today" is now resolved
  in `Europe/Oslo`, with a fallback to the host's local date if the system has
  no tz database.
- CI was red on every branch, including untouched `main`: `ruff` was declared
  without a version, and 0.16.1 widened the default rule set to flag five
  pre-existing violations. `ruff` is now pinned to `==0.16.1` and the five
  violations are fixed (`UP035` `Callable` import, two redundant `int(round(…))`
  casts, `DTZ011` above, `PLW1510` explicit `check=False` in a test). Same root
  cause as the `mcp[cli]` incident: an unpinned tool breaking on its own release
  schedule.
- Server failed to start with `ModuleNotFoundError: No module named
  'mcp.server.fastmcp'` after the MCP SDK released 2.0.0, which removed that
  module. The `mcp[cli]` dependency was declared without a version ceiling, so
  any fresh dependency resolution silently pulled the incompatible major
  version — taking down long-running installs on their next restart. Now pinned
  to `>=1.0,<2`. Lift the ceiling only together with a port to the 2.x API.
- Threshold pace was reported ~10× too slow (e.g. `40:00/km` instead of
  `4:00/km`) in `get_performance_metrics` and `get_threshold_history`. Garmin's
  lactate-threshold endpoints report speed in units of 10 m/s (true m/s ÷ 10),
  unlike activity `averageSpeed` which is plain m/s; the raw value is now scaled
  by `LT_SPEED_SCALE` before pace conversion. Threshold HR and power are
  unaffected. Verified against a same-day track session whose activity
  `averageSpeed` matched the raw threshold speed × 10.
- `get_threshold_history` returned an empty `points` list (falling back to
  `raw`) because the ranged payload keys each point's date under `from`, which
  `_extract_stat_series` did not recognize. `from` is now an accepted date key,
  so the trend series populates correctly.
