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
| `get_activity_details(activity_id)` | Full detail for one activity (stripped of bulky sample/GPS data) |
| `list_activities_by_date(start_date, end_date)` | Activity summaries in a date range |
| `get_daily_stats(date)` | Steps, calories, resting HR, stress for one day |
| `get_sleep(date)` | Sleep stages, sleep score, overnight HRV and resting HR |
| `get_heart_rate(date)` | Min/max/resting HR and 7-day average resting HR for one day |
| `get_body_battery(start_date, end_date)` | Body Battery charged/drained/highest/lowest per day |
| `get_training_status(date)` | Training status, load, VO2 max, heat/altitude acclimation |

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
