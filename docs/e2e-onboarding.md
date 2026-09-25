# E2E: onboarding a real person, end to end

The command sequence for verifying that self-service onboarding actually works
with a real Garmin account. Written against the mocked flow (the automated
tests cover every step below except the browser part and Garmin's real MFA);
run it for real the first time someone new is onboarded.

Everything here is read-only against the running system except step 2, which
creates a token store, and step 6, which deletes one.

## Prerequisites

```bash
BASE=https://mcp.productivitytech.io          # the host serving the onboarding page
ONBOARD=$BASE/<onboarding-path>           # onboarding page (nginx location)

# The already-running single-tenant connector, whose behaviour must not change.
# It is a credential — read it off the host rather than pasting it around:
#   sudo grep -o -- '--path [^ ]*' /etc/systemd/system/garmin-mcp.service
EXISTING_CONNECTOR_URL=https://mcp.productivitytech.io/<existing-secret-path>/mcp

# Where the multi-tenant unit keeps per-user token stores (its
# GARMIN_MULTI_TENANT_ROOT), needed for steps 5 and 6.
GARMIN_MULTI_TENANT_ROOT=/var/www/vhosts/productivitytech.io/.garmin-tenants
```

The person being onboarded needs only their own Garmin Connect login. They do
not need a terminal, and their password never reaches anyone else.

## 1. Regression first: the existing connector must be untouched

Before anything else, prove the already-running single-tenant instance answers.
Keep the output — it is the "before" half of the regression evidence.

```bash
curl -sS -X POST "$EXISTING_CONNECTOR_URL" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

Expect a `result` with `serverInfo.name == "garmin"`. Re-run the identical
command after every change below and diff the two.

## 2. The person gets a token store

### 2a. In a browser (the intended route)

Only works if this host can reach Garmin's login. As of 2026-08-11 it cannot —
Cloudflare rejects the only working strategy from this IP — so check 2b first.

### 2b. From their own machine (the route that works today)

They run this once, on their own computer:

```bash
uvx --from git+https://github.com/Sinfjell/garmin-mcp@main garmin-mcp-auth
```

It prompts for their Garmin email, password and MFA code, and writes
`~/.garminconnect`. Their password never leaves their machine, and their home
IP is not the one Garmin is refusing. They send you that directory.

```bash
garmin-mcp-tenant import ./their-garminconnect
# -> Imported as <user-id>
# -> https://mcp.productivitytech.io/garmin-u/<user-id>/mcp
```

Continue from step 3 with that URL. Everything below is identical either way.

## 2c. If the browser route is available, the person onboards there

They open `$ONBOARD` on any device, read the consent text, and enter their
Garmin email and password. If Garmin asks for a one-time code, they get a
second page and have five minutes to enter it.

The page ends with their personal connector URL. That URL is a credential:
they should save it somewhere private and not paste it into a chat.

```bash
NEW_URL="<the URL from their success page>"
```

## 3. The new URL answers MCP

```bash
curl -sS -X POST "$NEW_URL" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'

curl -sS -X POST "$NEW_URL" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```

`tools/list` should list all twelve tools.

## 4. It returns *their* data, not someone else's

This is the check that matters. Ask for a day they know, and confirm the
numbers are theirs — step counts and resting HR from the wrong account are the
failure this whole design exists to prevent.

```bash
curl -sS -X POST "$NEW_URL" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_daily_stats","arguments":{"date":"'"$(date +%F)"'"}}}'
```

Cross-check the same date against the existing connector URL from step 1: the
two must disagree. Identical numbers mean the tenant is reading the host's
account.

## 5. No password was persisted

Run on the host, with `$TESTPW` set to the password that was just used. Both
must return nothing.

```bash
sudo grep -rI --fixed-strings "$TESTPW" /var/www/vhosts/productivitytech.io/ 2>/dev/null
sudo journalctl -u garmin-mcp-multi -u garmin-mcp-onboarding --since '30 min ago' | grep -F "$TESTPW"
```

Then confirm what *is* stored is only a token, owner-readable:

```bash
sudo ls -la $GARMIN_MULTI_TENANT_ROOT/<user-id>/     # expect 0700 dir, 0600 file
```

## 6. Deletion works

Deletion is a command, not a web endpoint: the auth model is possession-of-URL,
so a delete button would let anyone who ever saw a URL wipe that account's
access. Keeping it behind SSH makes removal deliberate and auditable.

```bash
garmin-mcp-tenant list
garmin-mcp-tenant delete <user-id>
```

Then prove the URL is dead — the same request from step 3 must now 404:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$NEW_URL" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## 7. Regression again

Re-run step 1 verbatim. Identical result to the "before" capture, or the
invariant is broken and the change must come out.
