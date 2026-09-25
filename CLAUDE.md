# garmin-mcp

MCP server exposing one person's Garmin Connect data to AI clients, plus
multi-tenant hosting and a self-service onboarding app.

## Invariants

Break one of these and someone loses access to their data, or gains access to
someone else's. Any diff touching them needs `review-against-plan` before merge.

1. **The running production connector must not break.** `garmin-mcp.service` on
   the Hetzner host, and the URL it serves, belong to Sindre's own instance.
   Multi-tenant and onboarding run as separate units on separate ports. Any
   change that touches the single-tenant code path — including the default
   `--path`, the default token store, or existing tool behaviour — has to be
   proved inert with a before/after MCP `initialize` call against the live URL.

2. **A request may only read the token store its URL names.** Tenant isolation
   rests on the token store bound per request. `get_client()` fails closed in
   multi-tenant mode: no bound store means an error, never a fall back to the
   host's session or to `GARMIN_EMAIL`/`GARMIN_PASSWORD`. Prove isolation by
   driving the ASGI app over HTTP for two user IDs, not by unit-testing the
   resolver.

3. **Session multi-tenant: user IDs are the whole authentication story.** Possession-of-URL: 32–128
   chars of `[a-z0-9-]`, generated with ≥128 bits of entropy. Unknown and
   malformed IDs must return the *same* 404, so probing reveals nothing.

4. **A password must never reach disk or a log.** It exists for the duration of
   one Garmin login call and is cleared immediately after — including before
   the MFA wait. Never log an exception's message or `exc_info` on a login
   path: a failing HTTP client can echo the request body back, and the request
   carried the password. Log the exception *type*.

5. **OAuth mode: the bearer token names the user, nothing else does.**
   `/garmin-oauth/mcp` binds the token store of the access token's subject and
   no other; a missing, unknown, expired or revoked token is a 401 before any
   store is touched. Garmin webhooks are unsigned, so a notification never
   destroys data on its own word: deregistration waits for Garmin to reject the
   user's token, permission changes are re-read from Garmin, and Ping callbacks
   are only followed on `apis.garmin.com`. Prove isolation over HTTP with two
   users' tokens.

6. **Dependencies carry an upper bound.** No lockfile exists and the production
   host re-resolves on restart, so an unpinned dependency means any restart can
   pull a breaking major. Lift a ceiling only together with a port to the new
   API — never to make CI green.

## Gates

```bash
ruff check .            # pinned 0.16.1; the default rule set grows every release
pytest
garmin-mcp --list-tools
```

## Deploy

`uvx --from git+...@main` runs without `--refresh`, so a plain
`systemctl restart` reuses the cached build and silently ships nothing. Follow
the refresh procedure in `personal-memory/engineering/garmin-mcp-remote.md`.
Check `systemctl is-active` **and** `NRestarts`: `Restart=always` makes a
crash-looping unit look alive.
