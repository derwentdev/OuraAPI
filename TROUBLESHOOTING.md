# Troubleshooting guide

Organized by symptom. Most of these were real issues hit while building
this system — not hypothetical.

## "Set OURA_CLIENT_ID and OURA_CLIENT_SECRET environment variables first"
The local script can't see your environment variables. Either they were
set in a different terminal window/tab, or the terminal was closed and
reopened since. Environment variables only last for the session they were
set in — re-run the `export` commands in the same window right before
running the script.

## 401 Unauthorized on a specific Oura endpoint (e.g. daily_spo2)
This means your token is fine but doesn't have access to that particular
data type. Common causes:
- Your ring generation doesn't support that data type (SpO2 needs Gen 3+)
- That scope wasn't granted during authorization — Oura's consent screen
  only shows scopes your account is eligible for
- An active Oura Membership subscription may be required for some data
  types on newer rings

The server code already handles this gracefully — a failed field shows
as `null`/"n/a" instead of crashing the whole request.

## `{"error": "invalid_request", "error_description": "Invalid request"}` from Oura's token endpoint
This is Oura's generic "something about this request is malformed" error
— note it's different from `invalid_grant` (bad/expired token) or
`invalid_client` (bad credentials). In practice, the most common cause we
found was **stale credentials**: the client ID, secret, or refresh token
being sent didn't actually match each other, usually because a refresh
token had already rotated elsewhere (see next entry).

To isolate this from any deployment issue, test the exact request
directly with curl:
```
curl -s -X POST https://api.ouraring.com/oauth/token \
  -d "grant_type=refresh_token" \
  -d "refresh_token=YOUR_REFRESH_TOKEN" \
  -d "client_id=YOUR_CLIENT_ID" \
  -d "client_secret=YOUR_CLIENT_SECRET"
```
If this also fails with fresh, hand-verified values, the issue is with
Oura's records for that credential set, not your code or deployment.

## "My refresh token stopped working" / repeated token errors
**Root cause: Oura rotates refresh tokens.** Every successful refresh
(whether triggered by the local script or the server) invalidates the
previous refresh token and issues a new one. If both the local script
and the server have been refreshing independently, whichever refreshed
most recently "wins" — the other's copy is now stale.

With Upstash persistence in place, only the server should ever refresh
the token going forward. **Do not run `oura_recovery.py` after initial
setup** — every run risks invalidating the token the server is using.
If you need to force a truly fresh authorization (not just reuse a
cached one), delete the local cache first:
```
rm ~/.oura_tokens.json
python oura_recovery.py
```

## Claude connector shows "Couldn't connect to the server"
Check, in order:
1. Is `https://yourapp.onrender.com/healthz` returning "ok" in a browser?
   If not, the server itself is down — check Render's logs for a crash.
2. Is the connector URL set to the `/mcp` path specifically, not just the
   bare domain? (`https://yourapp.onrender.com/mcp`, not just
   `https://yourapp.onrender.com/`)
3. Check Render logs for the request path Claude is actually hitting —
   if you see `GET /` or `POST /` instead of `/mcp`, the connector URL is
   wrong.

## Password page never appears / connector fails silently before login
Check that your server's 401 response on `/mcp` includes a
`WWW-Authenticate` header pointing to
`/.well-known/oauth-protected-resource` — without it, Claude doesn't
know where to send the user to log in and may just report a generic
connection failure instead.

## Changes don't seem to take effect after redeploying
The most common cause is the file upload not actually overwriting the
existing file on GitHub (creates a duplicate instead if the filename
doesn't match exactly), so Render is still building the old code. Verify
by opening the file directly on github.com and confirming your latest
change is actually there, and checking the deploy timestamp in Render's
Events tab.

## First request after a while is very slow (~30-50 seconds)
Expected behavior on Render's free tier — the service spins down after
~15 minutes idle and takes time to cold-start on the next request. Not a
bug. If this is disruptive, it only happens on the first request after a
gap; subsequent ones are fast until it goes idle again.

## General debugging approach that worked well
When something fails deep in a chain (Claude → server → Oura), add a
`print()` statement right before the failing call showing exactly what's
being sent, and log the *full* error response body on failure (not just
the status code) — generic status codes rarely explain enough on their
own. Check Render's Logs tab immediately after reproducing the issue.
