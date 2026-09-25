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
export GARMIN_OAUTH_WEBHOOK_SECRET=...     # required; python -c "import secrets; print(secrets.token_urlsafe(32))"
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
Production uses port **8771**, prefix `/garmin-oauth`.

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
Environment=GARMIN_OAUTH_WEBHOOK_SECRET=...
# uvx caches builds: use --refresh when deploying a new git revision
ExecStart=/usr/local/bin/uvx --refresh --from git+https://github.com/Sinfjell/garmin-mcp@main \
  garmin-mcp --transport streamable-http --host 127.0.0.1 --port 8771 --path /garmin-oauth
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

After `daemon-reload` + `start`, check **both** `systemctl is-active` and
`NRestarts` (`Restart=always` can make a crash loop look healthy).

## Reverse proxy

Point the oauth prefix and its two discovery documents at 127.0.0.1:8771
(8770 is `personal-context-mcp`). Leave existing locations for the unofficial
connectors alone.

```nginx
location /garmin-oauth/ {
    proxy_pass http://127.0.0.1:8771;
    proxy_http_version 1.1;
    # Garmin pushes up to 100 MB of activity data; nginx's default is 1 MB and
    # would answer 413 before the app sees the request.
    client_max_body_size 128m;
    proxy_set_header Host $host;
    # Keep the public Host. garmin-mcp allows it via
    # GARMIN_OAUTH_PUBLIC_BASE_URL (MCP DNS-rebinding allowlist). Do NOT rewrite
    # Host to 127.0.0.1:8771 — that breaks absolute callback/connector URLs.
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    # MCP streamable HTTP may use SSE
    proxy_buffering off;
}

# The webhook paths carry GARMIN_OAUTH_WEBHOOK_SECRET: keep them out of the
# access log (the app itself runs uvicorn with access_log=False).
location /garmin-oauth/webhooks/ {
    access_log off;
    proxy_pass http://127.0.0.1:8771;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    client_max_body_size 128m;
}

# MCP clients discover the authorization server from these two documents
# (RFC 9728 and RFC 8414). They live outside /garmin-oauth/ by specification.
location = /.well-known/oauth-protected-resource/garmin-oauth/mcp {
    proxy_pass http://127.0.0.1:8771;
    proxy_set_header Host $host;
}
location = /.well-known/oauth-authorization-server/garmin-oauth {
    proxy_pass http://127.0.0.1:8771;
    proxy_set_header Host $host;
}
```

FastMCP enables DNS-rebinding protection when bound to localhost and would
otherwise reject a public `Host` with **421 Invalid Host**. OAuth mode widens
the allowlist from `GARMIN_OAUTH_PUBLIC_BASE_URL` (and optional
`GARMIN_OAUTH_ALLOWED_HOSTS`) while keeping protection on. Alternative: have
nginx rewrite `Host` to `127.0.0.1` — we prefer the app-side allowlist so the
public hostname stays visible to the process.

### Smoke: public Host must not 421

Unauthenticated requests get 401 from the router before FastMCP's Host check
runs, so a bare curl cannot prove the allowlist. The connector working in
claude.ai (smoke step 5–7) does. If it fails with **421** `Invalid Host header`,
the allowlist did not include the Host nginx forwarded — check
`GARMIN_OAUTH_PUBLIC_BASE_URL` / `ALLOWED_HOSTS` and restart with `uvx --refresh`.

## Portal URLs to register

On the evaluation app «Garmin MCP». `GARMIN_OAUTH_WEBHOOK_SECRET` is required
in oauth mode (≥ 32 characters, no `/`; the process refuses to start without
it) — Garmin does not sign notifications, so the secret path segment is what
authenticates them:

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
6. Items that fail (Garmin 5xx, timeouts) are written to `.inbox/retry/` and
   retried after 1 min, 10 min and 1 h — also across a restart. After the last
   retry they are parked in `.inbox/failed/` and deleted after 7 days. All writes
   are idempotent, so replaying a parked file (move it to `.inbox/`, restart) is safe.
7. Deregistration also removes the user's items from every spooled file.

After consent the server requests 30 days of backfill for activities, dailies,
sleeps, stressDetails, hrv and userMetrics; the data then arrives as ordinary
Ping/Push notifications. Tools answer from the local store only.

## Endpoints this process serves

| Method | Path | Role |
|---|---|---|
| * | `/garmin-oauth/mcp` | Streamable MCP; bearer token decides whose data. 401 + discovery hint without one |
| GET | `/.well-known/oauth-protected-resource/garmin-oauth/mcp` | RFC 9728 resource metadata |
| GET | `/.well-known/oauth-authorization-server/garmin-oauth` | RFC 8414 server metadata (also under `/garmin-oauth/.well-known/…`) |
| POST | `/garmin-oauth/register` | Dynamic client registration (RFC 7591) |
| GET | `/garmin-oauth/authorize` | MCP client starts here; parks the request, sends the user to consent |
| GET/POST | `/garmin-oauth/consent` | AI-transparency statement + explicit consent, then Garmin |
| GET | `/garmin-oauth/callback` | Garmin returns here; code exchanged, user created, client gets our code |
| POST | `/garmin-oauth/token` | Code/refresh → our access token (1 h) + rotating refresh token (90 d) |
| POST | `/garmin-oauth/revoke` | Token revocation |
| POST | `/garmin-oauth/webhooks[/<secret>]/ping` | Spool, 200, process in background |
| POST | `/garmin-oauth/webhooks[/<secret>]/push` | Same handler |

Every user adds the **same** connector URL, `https://mcp.productivitytech.io/garmin-oauth/mcp`,
in claude.ai (Settings → Connectors → Add custom connector) or ChatGPT; the client
runs the login itself. The old per-user URLs (`/garmin-oauth/<user-id>/mcp`) are
gone — anyone who connected through one must add the connector again. Reconnecting
with the same Garmin account reuses the same stored data.

MCP auth state (registered clients, pending requests, SHA-256 hashes of codes and
tokens) lives in `$TOKEN_ROOT/.mcp-auth.sqlite3`. Deregistration revokes all of a
user's MCP tokens along with their data.

Lifetimes: access token 1 h, refresh token 90 days (rotated on every use, the old
pair dies), authorization code 5 min, parked authorization request 15 min.
Dynamic client registration is open, as the MCP spec expects: any client can
register, but every sign-in passes our consent page (which names the client and
the host it returns to) and Garmin's. The consent form only accepts a POST from
the browser that loaded it (per-request cookie), for approve and cancel alike.
Consent fails — and no user is created — if Garmin's user ID or permissions
cannot be read after the token exchange.

## Smoke checklist (human)

1. Create/confirm eval app redirect URI + Ping/Push URLs in the Garmin portal.
2. Set the env vars above on the host (secrets from 1Password — never git).
3. Restart the unit on port 8771 (with `uvx --refresh`); confirm the other units are unchanged.
4. `curl -si https://mcp.productivitytech.io/garmin-oauth/mcp -X POST` → 401 with a
   `resource_metadata=` hint; both `/.well-known/…` URLs return JSON (not nginx HTML).
5. In claude.ai, add the connector `https://mcp.productivitytech.io/garmin-oauth/mcp`
   and complete our consent page and Garmin's. Screenshot each step for the review.
6. Within a few minutes, `ls $TOKEN_ROOT/<user-id>/` shows `summaries.sqlite3`
   (backfill arriving) and `.inbox/` is empty; `.inbox/failed/` must stay empty.
7. Ask the assistant for yesterday's steps and recent activities.
8. In Garmin's Data Generator, send a Push and a Ping for the test user; repeat 7.
9. Run Partner Verification; it checks deregistration, permissions and the 200s.
10. Confirm `get_training_status` / `get_personal_records` / `get_performance_metrics`
    return a clear «not available via official API» error (no fake numbers).
