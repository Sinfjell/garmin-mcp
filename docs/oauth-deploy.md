# Official OAuth eval instance (Garmin Connect Developer Program)

Additive path for a **new** smoke-test / prod-candidate instance using OAuth
2.0 PKCE + Health/Activity APIs. Does **not** replace or migrate the unofficial
session-auth services (`garmin-mcp.service`, multi-tenant, onboarding) on
Hetzner / mcp.productivitytech.io.

Garmin ticket 224965: use case OK in principle if the Privacy Policy outlines
Garmin data + third-party AI. Training status / lactate threshold / personal
records are **not** available via official APIs — oauth-mode tools that need
them return a clear error (they do not invent data).

## Privacy Policy (external gap)

Publish a Privacy Policy that covers Garmin data handling and third-party AI
before production partner verification. That page is **out of scope** for this
repo; call it out separately.

## Env

See `.env.example`. Minimum for oauth mode:

```bash
export GARMIN_AUTH_MODE=oauth
export GARMIN_OAUTH_CLIENT_ID=...          # from Garmin portal / 1Password
export GARMIN_OAUTH_CLIENT_SECRET=...
export GARMIN_OAUTH_REDIRECT_URI=https://mcp.productivitytech.io/garmin-oauth/callback
export GARMIN_OAUTH_PUBLIC_BASE_URL=https://mcp.productivitytech.io
# Optional: extra Host headers for MCP DNS-rebinding allowlist (www, etc.).
# PUBLIC_BASE_URL's hostname is always included; localhost stays allowed for
# direct curls to the bind. Prefer this app-side allowlist over rewriting Host
# in nginx (which also works but hides the public name from the app).
# export GARMIN_OAUTH_ALLOWED_HOSTS=www.productivitytech.io
export GARMIN_OAUTH_TOKEN_ROOT=$HOME/.garmin-oauth-tokens   # EU-local disk
export GARMIN_OAUTH_PATH_PREFIX=/garmin-oauth
```

Token layout (EU storage assumption — host filesystem in the EU, no US-only deps):

```
$GARMIN_OAUTH_TOKEN_ROOT/
  <user-id>/tokens.json     # access + refresh (0600)
  .pending/<state>.json      # PKCE verifier during authorize (short-lived)
  .by-garmin/<garmin-user-id>  # maps Garmin API user id → local user-id
```

## Suggested NEW systemd unit (do not edit live units)

Bind a **different** port and path so existing `:8765` / `:8766` stay untouched.
Suggested: port **8770**, prefix `/garmin-oauth`.

Example unit file (create as a new file, e.g. `/etc/systemd/system/garmin-mcp-oauth.service`
— never overwrite `garmin-mcp.service`):

```ini
[Unit]
Description=Garmin MCP official OAuth eval instance
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/var/www/vhosts/productivitytech.io
Environment=GARMIN_AUTH_MODE=oauth
Environment=GARMIN_OAUTH_CLIENT_ID=...
Environment=GARMIN_OAUTH_CLIENT_SECRET=...
Environment=GARMIN_OAUTH_REDIRECT_URI=https://mcp.productivitytech.io/garmin-oauth/callback
Environment=GARMIN_OAUTH_PUBLIC_BASE_URL=https://mcp.productivitytech.io
# Optional: Environment=GARMIN_OAUTH_ALLOWED_HOSTS=www.productivitytech.io
Environment=GARMIN_OAUTH_TOKEN_ROOT=/var/www/vhosts/productivitytech.io/.garmin-oauth-tokens
Environment=GARMIN_OAUTH_PATH_PREFIX=/garmin-oauth
# uvx caches builds: use --refresh when deploying a new git revision
ExecStart=/usr/local/bin/uvx --refresh --from git+https://github.com/Sinfjell/garmin-mcp@main \
  garmin-mcp --transport streamable-http --host 127.0.0.1 --port 8770 --path /garmin-oauth
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

After `daemon-reload` + `start`, check **both** `systemctl is-active` and
`NRestarts` (`Restart=always` can make a crash loop look healthy).

## Reverse proxy

Point only the oauth prefix at 127.0.0.1:8770. Leave existing locations for
the unofficial connectors alone.

```nginx
location /garmin-oauth/ {
    proxy_pass http://127.0.0.1:8770;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    # Keep the public Host. garmin-mcp allows it via
    # GARMIN_OAUTH_PUBLIC_BASE_URL (MCP DNS-rebinding allowlist). Do NOT rewrite
    # Host to 127.0.0.1:8771 — that breaks absolute callback/connector URLs.
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    # MCP streamable HTTP may use SSE
    proxy_buffering off;
}
```

FastMCP enables DNS-rebinding protection when bound to localhost and would
otherwise reject a public `Host` with **421 Invalid Host**. OAuth mode widens
the allowlist from `GARMIN_OAUTH_PUBLIC_BASE_URL` (and optional
`GARMIN_OAUTH_ALLOWED_HOSTS`) while keeping protection on. Alternative: have
nginx rewrite `Host` to `127.0.0.1` — we prefer the app-side allowlist so the
public hostname stays visible to the process.

### Smoke: public Host must not 421

After deploy, with a real connector user-id (or any path that reaches the MCP
transport — a 404 from our router is fine for host checks only if you hit
`/mcp` after rewrite; easier is an existing tenant URL):

```bash
# Expect JSON-RPC / MCP response, NOT "Invalid Host header" with status 421
curl -sS -o /tmp/mcp-out -w '%{http_code}\n' \
  -H 'Host: productivitytech.io' \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}' \
  https://mcp.productivitytech.io/garmin-oauth/<user-id>/mcp
```

A `421` body of `Invalid Host header` means the allowlist did not include the
Host nginx forwarded — check `GARMIN_OAUTH_PUBLIC_BASE_URL` / `ALLOWED_HOSTS`
and restart with `uvx --refresh`.

## Portal URLs to register

On the evaluation app «Garmin MCP». With `GARMIN_OAUTH_WEBHOOK_SECRET` set
(recommended — Garmin does not sign notifications), the webhook URLs carry the
secret as a path segment:

| Purpose | URL |
|---|---|
| OAuth redirect | `https://mcp.productivitytech.io/garmin-oauth/callback` |
| Ping webhook | `https://mcp.productivitytech.io/garmin-oauth/webhooks/<secret>/ping` |
| Push webhook | `https://mcp.productivitytech.io/garmin-oauth/webhooks/<secret>/push` |

Enable **Deregistration** and **User Permission** notifications and point them
at the Ping URL. Summary types may use either Ping or Push; the handler is the
same. Do not paste the secret into chat or tickets.

## How notifications are processed

1. The handler streams the body (≤ 128 MB) to `$TOKEN_ROOT/.inbox/*.json` and
   answers **200** before doing anything else.
2. One worker thread applies spool files in arrival order. On start, files left
   from before a restart are processed; half-written `.partial` files are dropped.
3. Summaries are upserted per user into `$TOKEN_ROOT/<user-id>/summaries.sqlite3`
   (0600). Ping callbacks are fetched with that user's bearer token, and only
   from `apis.garmin.com`.
4. **Deregistration**: the user's directory (tokens + data) and Garmin-ID index
   entry are deleted — only after Garmin rejects the user's token on
   `GET /user/id`. A 200 there means the notification is ignored.
5. **Permission change**: permissions are re-read from Garmin; data behind a
   withdrawn `ACTIVITY_EXPORT` / `HEALTH_EXPORT` is purged.
6. A file with any failed item moves to `.inbox/failed/`. All writes are
   idempotent, so replaying one (move it back to `.inbox/`, restart) is safe.

After consent the server requests 30 days of backfill for activities, dailies,
sleeps, stressDetails, hrv and userMetrics; the data then arrives as ordinary
Ping/Push notifications. Tools answer from the local store only.

## Endpoints this process serves

| Method | Path | Role |
|---|---|---|
| GET | `/garmin-oauth/authorize` | Start PKCE; redirect to Garmin consent |
| GET | `/garmin-oauth/callback` | Exchange code; create tenant token store; show MCP URL |
| POST | `/garmin-oauth/webhooks[/<secret>]/ping` | Spool, 200, process in background |
| POST | `/garmin-oauth/webhooks[/<secret>]/push` | Same handler |
| * | `/garmin-oauth/<user-id>/mcp` | Streamable MCP for that user only |

## Smoke checklist (human)

1. Create/confirm eval app redirect URI + Ping/Push URLs in the Garmin portal.
2. Set the env vars above on the host (secrets from 1Password — never git).
3. Start the **new** unit on port 8770; confirm existing units unchanged.
4. Open `https://mcp.productivitytech.io/garmin-oauth/authorize`, complete consent.
5. Copy the printed MCP URL (`.../garmin-oauth/<user-id>/mcp`).
6. Within a few minutes, `ls $TOKEN_ROOT/<user-id>/` shows `summaries.sqlite3`
   (backfill arriving) and `.inbox/` is empty; `.inbox/failed/` must stay empty.
7. `initialize` against that URL; call `get_daily_stats` and `list_recent_activities`.
8. In Garmin's Data Generator, send a Push and a Ping for the test user; repeat 7.
9. Run Partner Verification; it checks deregistration, permissions and the 200s.
10. Confirm `get_training_status` / `get_personal_records` / `get_performance_metrics`
    return a clear «not available via official API» error (no fake numbers).
