# Running the OAuth instance in Docker

The standard way to host the official OAuth instance (`GARMIN_AUTH_MODE=oauth`) is the
`Dockerfile` and `compose.yaml` in this repository, on one host behind nginx. It replaces the
`uvx --from git+...@main` systemd unit for this instance only. Sindre's single-tenant
`garmin-mcp.service` (port 8765) and the multi-tenant units are not part of this.

Why Docker on a host and not serverless: the instance keeps state between requests (OAuth
grants in `.mcp-auth.sqlite3`, per-user tokens and `summaries.sqlite3`, the webhook spool in
`.inbox/`) and runs a background worker. All of it lives under one directory,
`GARMIN_OAUTH_TOKEN_ROOT`, which the image mounts as its one volume, `/data`.

## What the image guarantees

- **Locked dependencies.** The image installs from `uv.lock` with `uv sync --locked`, which fails
  the build when the lockfile is out of date. A rebuild of the same commit gets the same Python
  packages, and nothing re-resolves on restart. The base images are tags, so OS packages and the
  Python patch level can move between builds; pin them by digest if you need byte-identical images.
- **Health.** `GET` or `HEAD /garmin-oauth/healthz` answers `200 {"status": "ok"}` when the
  process runs and `/data` is writable, otherwise `503`. It does not yet notice a stuck webhook worker. It reveals nothing about users. Docker's `HEALTHCHECK`
  and an external uptime monitor both use it.
- **Least privilege.** Runs as uid 10001, read-only root filesystem, all capabilities dropped,
  `no-new-privileges`, bound to `127.0.0.1` only.
- **No access log.** The webhook URLs carry `GARMIN_OAUTH_WEBHOOK_SECRET` in the path, so the
  server keeps uvicorn's access log off. Docker keeps three 10 MB log files.

## First deploy on a host

Needs Docker Engine with the compose plugin.

```bash
git clone https://github.com/Sinfjell/garmin-mcp.git /opt/garmin-mcp-oauth
cd /opt/garmin-mcp-oauth
git checkout <release tag>

# Secrets: the GARMIN_OAUTH_* variables from .env.example. Never committed.
install -m 0600 /dev/null .env
$EDITOR .env

# Data volume, owned by the image's user.
mkdir -p data && chown 10001 data && chmod 0700 data   # uid matters; the image group is not 10001

GARMIN_MCP_VERSION=<release tag> docker compose up -d --build
docker compose ps        # STATUS must read (healthy)
curl -s http://127.0.0.1:8771/garmin-oauth/healthz
```

`.env` needs at least `GARMIN_OAUTH_CLIENT_ID`, `GARMIN_OAUTH_CLIENT_SECRET`,
`GARMIN_OAUTH_REDIRECT_URI`, `GARMIN_OAUTH_PUBLIC_BASE_URL` and `GARMIN_OAUTH_WEBHOOK_SECRET`.
`GARMIN_AUTH_MODE` and `GARMIN_OAUTH_TOKEN_ROOT` are set by the image and compose file.
`GARMIN_MCP_PORT` changes the loopback port (default 8771). Keep the image's `GARMIN_OAUTH_PATH_PREFIX` (`/garmin-oauth`): the nginx blocks
below and the Garmin portal registrations use it.

Moving an existing instance: stop the old unit, copy its token root into `data/` (keep the
file modes), `chown -R 10001 data`, then start the container. Users keep their grants.

## nginx

Proxy the prefix and the two well-known paths to the loopback port. The app allows the public
host from `GARMIN_OAUTH_PUBLIC_BASE_URL`, so nginx forwards the real `Host` and does not need
the `Host`/`Origin` rewrite the session units use.

```nginx
location /garmin-oauth/ {
    proxy_pass http://127.0.0.1:8771;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_read_timeout 300s;
    client_max_body_size 128m;   # Push bodies
}
location /garmin-oauth/webhooks/ {
    proxy_pass http://127.0.0.1:8771;
    proxy_set_header Host $host;
    access_log off;              # the path holds the webhook secret
    error_log /dev/null crit;    # error lines carry the request line too
    client_max_body_size 128m;
}
location = /.well-known/oauth-authorization-server/garmin-oauth {
    proxy_pass http://127.0.0.1:8771;
    proxy_set_header Host $host;
}
location = /.well-known/oauth-protected-resource/garmin-oauth/mcp {
    proxy_pass http://127.0.0.1:8771;
    proxy_set_header Host $host;
}
```

## Upgrade and rollback

```bash
cd /opt/garmin-mcp-oauth
git fetch --tags && git checkout <new tag>
GARMIN_MCP_VERSION=<new tag> docker compose up -d --build
docker compose ps        # wait for (healthy)
```

Rollback is the same with the previous tag. Images are tagged `garmin-mcp:<tag>`, so the old
one is still on the host until pruned.

## Backup

`data/` is the whole state. Back it up daily while the container runs:

```bash
cd /opt/garmin-mcp-oauth
docker compose exec -T garmin-mcp-oauth garmin-mcp-backup > "garmin-oauth-$(date +%F).tar.gz"
```

`garmin-mcp-backup` streams a gzipped tar of `/data` to stdout. SQLite files are copied with
SQLite's backup API, so a write in progress is never captured half-done, and nothing is written
inside `data/`.

Ship the archive off the host, encrypted: it holds OAuth tokens. Keep backups no longer than the
data retention the privacy policy promises, since a deregistered user's data stays in older
archives until they expire.

A backup is only proven by a restore: unpack an archive into a scratch `data/` (`chown -R 10001`),
start the image against it on another port, and check that `healthz` answers and a known client
can still authorize.
