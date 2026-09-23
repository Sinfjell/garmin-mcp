# Official OAuth eval instance (Garmin Connect Developer Program)

Additive path for a **new** smoke-test / prod-candidate instance using OAuth
2.0 PKCE + Health/Activity APIs. Does **not** replace or migrate the unofficial
session-auth services (`garmin-mcp.service`, multi-tenant, onboarding) on
Hetzner / productivitytech.io.

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
export GARMIN_OAUTH_REDIRECT_URI=https://productivitytech.io/garmin-oauth/callback
export GARMIN_OAUTH_PUBLIC_BASE_URL=https://productivitytech.io
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
Environment=GARMIN_OAUTH_REDIRECT_URI=https://productivitytech.io/garmin-oauth/callback
Environment=GARMIN_OAUTH_PUBLIC_BASE_URL=https://productivitytech.io
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

## Portal URLs to register

On the evaluation app «Garmin MCP»:

| Purpose | URL |
|---|---|
| OAuth redirect | `https://productivitytech.io/garmin-oauth/callback` |
| Ping webhook | `https://productivitytech.io/garmin-oauth/webhooks/ping` |
| Push webhook | `https://productivitytech.io/garmin-oauth/webhooks/push` |

Ping/Push handlers currently **acknowledge with HTTP 200** and do not ingest
payloads (documented stubs for the eval program). Pull is used for smoke tests.

## Endpoints this process serves

| Method | Path | Role |
|---|---|---|
| GET | `/garmin-oauth/authorize` | Start PKCE; redirect to Garmin consent |
| GET | `/garmin-oauth/callback` | Exchange code; create tenant token store; show MCP URL |
| POST | `/garmin-oauth/webhooks/ping` | Stub 200 |
| POST | `/garmin-oauth/webhooks/push` | Stub 200 |
| * | `/garmin-oauth/<user-id>/mcp` | Streamable MCP for that user only |

## Smoke checklist (human)

1. Create/confirm eval app redirect URI + Ping/Push URLs in the Garmin portal.
2. Set the env vars above on the host (secrets from 1Password — never git).
3. Start the **new** unit on port 8770; confirm existing units unchanged.
4. Open `https://productivitytech.io/garmin-oauth/authorize`, complete consent.
5. Copy the printed MCP URL (`.../garmin-oauth/<user-id>/mcp`).
6. `initialize` against that URL; call `get_daily_stats` and `list_recent_activities`.
7. Confirm `get_training_status` / `get_personal_records` / `get_performance_metrics`
   return a clear «not available via official API» error (no fake numbers).
