# Garmin MCP Server

Connect Claude (or any MCP client) to your Garmin Connect data — activities, sleep, heart rate, body battery, and training status.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![MIT License](https://img.shields.io/badge/license-MIT-green)
![CI](https://github.com/Sinfjell/garmin-mcp/actions/workflows/ci.yml/badge.svg)

## What you can ask

- "How did I sleep this week?"
- "What was my average pace on my last 5 runs?"
- "Is my resting heart rate trending down?"
- "Should I train hard today based on my body battery?"
- "Summarize my training status and VO2 max trend."
- "What's my lactate threshold heart rate and pace right now?"
- "What 10k time does Garmin predict for me?"
- "What are my running personal records?"
- "Has my threshold pace improved over the last three months?"

## Quick Start

### 1. One-time login

```bash
uvx --from git+https://github.com/Sinfjell/garmin-mcp garmin-mcp-auth
```

Prompts for your Garmin Connect email and password (or reads `GARMIN_EMAIL` /
`GARMIN_PASSWORD` from the environment), handles MFA if your account uses it,
and caches a session token locally so you won't be prompted again.

### 2. Add it to your MCP client

**Claude Desktop** — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Sinfjell/garmin-mcp", "garmin-mcp"]
    }
  }
}
```

**Claude Code**:

```bash
claude mcp add garmin -- uvx --from git+https://github.com/Sinfjell/garmin-mcp garmin-mcp
```

**Cursor** — add to `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Sinfjell/garmin-mcp", "garmin-mcp"]
    }
  }
}
```

### 3. Ask a question

Restart your client and ask something like "How did I sleep last night?"

## Tools

| Tool | Returns |
|---|---|
| `list_recent_activities(limit=10)` | Recent activities: date, type, name, distance, duration, avg HR, pace |
| `get_activity_details(activity_id)` | Full detail for one activity, incl. explicit elapsed/timer/moving/stopped timing (stripped of bulky sample/GPS data) |
| `get_activity_laps(activity_id, include_gps=False)` | Per-lap breakdown: distance, durations, pace, HR, power, cadence, interval intensity (WARMUP/ACTIVE/REST) |
| `list_activities_by_date(start_date, end_date)` | Activity summaries in a date range |
| `get_daily_stats(date)` | Steps, calories, resting HR, stress for one day |
| `get_sleep(date)` | Sleep stages, sleep score, overnight HRV and resting HR |
| `get_heart_rate(date)` | Min/max/resting HR and 7-day average resting HR for one day |
| `get_body_battery(start_date, end_date)` | Body Battery charged/drained/highest/lowest per day |
| `get_training_status(date)` | Training status, load, VO2 max, heat/altitude acclimation |
| `get_performance_metrics(date=None)` | Running fitness/threshold snapshot: lactate threshold (LTHR + pace), VO2 max + fitness age, and 5k/10k/half/marathon race predictions |
| `get_personal_records()` | Personal records / PBs: fastest 1km/1mile/5km/10km, longest run (labeled), plus raw type/value for any other record |
| `get_threshold_history(start_date, end_date, aggregation="weekly")` | Lactate-threshold HR + pace as a dated trend series over a range |

All dates are `"YYYY-MM-DD"`. All tools return compact JSON.

## Authentication

`garmin-mcp-auth` is the recommended way to log in — it supports MFA and
caches a token so the server doesn't need your password on every run.

**Environment variable alternative:** set `GARMIN_EMAIL` and `GARMIN_PASSWORD`
(see `.env.example`) and the server will log in non-interactively if no
cached token is found. This does not support MFA — accounts with MFA enabled
should use `garmin-mcp-auth` instead.

**Token cache location:** `~/.garminconnect` by default, or the path in
`GARMIN_TOKENS` if set.

## Remote / hosted mode (use from Claude mobile & web)

By default this server speaks stdio and is meant to run as a local subprocess
of your MCP client. You can instead run it as a long-lived HTTP service and
add it to claude.ai as a **custom connector**, which also makes it reachable
from Claude on mobile.

### 1. Log in once, on the host

```bash
garmin-mcp-auth
```

Run this on the machine that will host the server (interactively, so it can
handle MFA). The cached token in `~/.garminconnect` (or `GARMIN_TOKENS`) is
reused on every request — the server does not log in again per request.
Avoid triggering fresh logins often: Garmin rate-limits and sometimes blocks
sign-ins from datacenter IPs, so a stable, long-lived cached session is the
goal, not routine re-auth.

### 2. Run the server in streamable-http mode

```bash
garmin-mcp --transport streamable-http --host 127.0.0.1 --port 8765 --path /<random-secret>/mcp
```

- `--transport streamable-http` switches from stdio to an HTTP endpoint.
- `--host` / `--port` control what the server binds to (default
  `127.0.0.1:8765` — bind to `127.0.0.1` and put a reverse proxy in front
  rather than exposing the process directly).
- `--path` sets the URL path the MCP endpoint mounts at (default `/mcp`).
  Set it to a long random value (e.g. `/8f2c1a9e7b.../mcp`) and treat it as a
  secret — see below.

### 3. Put TLS and a reverse proxy in front

This mode has no built-in authentication. The security model is:

- **The path is the secret.** Anyone with the full URL — including the
  random path segment — can call every tool and read your Garmin data.
  Anyone without it gets a 404. This is possession-of-URL security, not real
  authentication — good enough for a personal deployment you control, not
  for anything shared or high-stakes.
- **TLS is required in practice.** Run the server behind a reverse proxy
  (Caddy, nginx, Cloudflare Tunnel, etc.) that terminates HTTPS, so the
  secret path isn't sent in the clear. Point the proxy at
  `127.0.0.1:<port>` and expose only the proxy's HTTPS URL.
- Don't log the URL, commit it, or paste it anywhere public — it's
  effectively a credential.

### 4. Add it to claude.ai as a custom connector

In claude.ai: **Settings → Connectors → Add custom connector**, then paste
your full HTTPS URL (e.g. `https://mcp.example.com/8f2c1a9e7b.../mcp`). Once
added, it's available from Claude on web and mobile, not just Claude Code or
Desktop.

## Security & privacy

- Your credentials never leave your machine and are never stored by this
  server — only a session token is cached locally.
- This server is **read-only**: it never writes to, modifies, or deletes
  anything in Garmin Connect.
- Nothing is sent anywhere except Garmin's own API — no third-party
  telemetry, analytics, or logging.

## Troubleshooting

**"Garmin authentication expired" / auth errors** — run `garmin-mcp-auth`
again to refresh your session.

**Stuck in an MFA loop** — make sure you're running `garmin-mcp-auth`
interactively (not through a client that swallows stdin); the MFA code
prompt needs a real terminal.

**Wrong Python version** — this package requires Python 3.10+. Check with
`python3 --version`; `uvx` will otherwise fail to build the environment.

## Disclaimer

This project uses the unofficial, community-maintained
[`garminconnect`](https://github.com/cyberjunky/python-garminconnect)
library to talk to Garmin Connect. It is not affiliated with, endorsed by,
or supported by Garmin. Garmin Connect's API is not public, and it can
change or break without notice.

## License

MIT — see [LICENSE](LICENSE).
