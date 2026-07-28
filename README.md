# Oura + Claude connector — how it all works

This document explains the complete system: what each piece does, why it
exists, and how they fit together.

## What this solution does

Lets you ask Claude questions like "what was my HRV last night?" from
anywhere (phone, desktop), with Claude pulling live data from your Oura
ring in the background — no manual copy-pasting.

## The components

| File / Service | What it is |
|---|---|
| `oura_recovery.py` | A local script, run once to bootstrap authorization with Oura. Not used day-to-day once the server is running. |
| `oura_mcp_server.py` | The always-on server. Hosts the tools Claude calls, handles Oura's OAuth token refresh, and runs its own mini OAuth server so Claude can authenticate to it. |
| `requirements.txt` | Python dependencies for the server. |
| `privacy-policy.md` / `terms-of-service.md` | Required by Oura's developer portal when registering an app. |
| **Render** | Hosts `oura_mcp_server.py` so it's reachable over the internet 24/7. |
| **Upstash** | A small external key-value store. Holds the current Oura refresh token so it survives server restarts. |
| **Claude custom connector** | The link between Claude and your server, set up in Settings → Connectors. |

## Why it's built this way

**Why a server at all, not just the local script?** The local script only
runs when your computer is on and you manually run it. For Claude to
answer questions from your phone anytime, something needs to be running
continuously, reachable over the internet — that's the server on Render.

**Why does the server implement its own OAuth login?** Claude's custom
connector UI currently only supports OAuth-style authentication, not
simple API key headers. Rather than run a separate proxy, the server
plays double duty: it's both the thing serving your Oura tools *and* a
minimal OAuth "front door" that Claude logs into. The real security is a
password (your `MCP_API_KEY`) shown on a page during that login — only
someone who knows it can get in.

**Why Upstash?** Two separate things need to survive a server restart, and
both are stored in Upstash for the same underlying reason — Render's free
tier wipes the server's memory every time it spins down from inactivity:
- **Oura's refresh token** — Oura issues a brand-new one every time the
  server refreshes its access token, invalidating the old one. Without
  persistence, a restart would fall back to a stale value.
- **Claude's login token** — once you enter your `MCP_API_KEY` password
  and Claude gets a bearer token, that token needs to survive restarts
  too, or you'd be asked to log in again every time the server sleeps
  and wakes back up.

Upstash gives the server somewhere durable to save both, so a restart
reloads the current values instead of falling back to stale ones.

## Setup, start to finish

### 1. Register an app with Oura
At `cloud.ouraring.com`, create an app to get a Client ID and Client
Secret. Set the redirect URI to `http://localhost:8080/callback` for
local use.

### 2. Bootstrap authorization locally
Run `oura_recovery.py` once with your Client ID/Secret set as environment
variables. This opens a browser to authorize, then saves a token file
locally — proving the credentials work and producing your first refresh
token.

### 3. Push server files to GitHub
`oura_mcp_server.py` and `requirements.txt` go into a public repo (Oura's
app registration form needs a public Website, Privacy Policy, and Terms
of Service URL — the two markdown files cover the last two).

### 4. Deploy to Render
Connect the GitHub repo as a Web Service. Set these environment
variables:
- `OURA_CLIENT_ID`, `OURA_CLIENT_SECRET` — from Oura
- `OURA_REFRESH_TOKEN` — from step 2 (only used the very first time)
- `MCP_API_KEY` — a password you make up, used in the connector login
- `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN` — from Upstash

### 5. Set up Upstash
Create a free Redis database at upstash.com. Copy its REST URL and REST
token into Render (step 4).

### 6. Add the connector in Claude
Settings → Connectors → Add custom connector → paste your server's `/mcp`
URL (e.g. `https://yourapp.onrender.com/mcp`). Claude will prompt an
OAuth login — this opens your server's password page. Enter your
`MCP_API_KEY`. From then on, enable the connector per conversation via
the "+" button and ask away.

## Operational notes

- **Free tier spin-down**: after ~15 minutes idle, Render's free tier
  sleeps your server. The next request takes ~30-50 seconds to wake it
  back up, then responds normally.
- **Token rotation is fully automatic** with Upstash persistence in
  place — both the Oura refresh token and Claude's login token survive
  restarts. You should never need to manually run `oura_recovery.py`
  again after initial setup, and you shouldn't be asked to re-enter your
  `MCP_API_KEY` unless the token genuinely expires (~30 days) or you
  remove/re-add the connector.
- **Never run `oura_recovery.py` after the server is live.** Once the
  server owns the token chain, any outside refresh — the local script,
  a curl test, anything — invalidates the token the server is using and
  breaks it. The script has been renamed
  `oura_recovery.DEPRECATED_DO_NOT_RUN.py` as a reminder.
- **Blood oxygen (SpO2)** may show as unavailable depending on your ring
  generation, membership status, or granted scopes — this is expected
  and doesn't indicate a problem with the rest of the setup.
- **Concurrent requests are handled safely.** A lock around the Oura
  token refresh prevents two simultaneous requests (e.g. from two
  different chats) from racing to refresh at the same moment, which
  would otherwise invalidate one of them.
- **Corrupted stored data self-heals.** If a saved value in Upstash is
  ever unreadable (e.g. from an old bug or manual edit), the server logs
  a warning and starts fresh for that value instead of crashing.
