"""
Oura MCP Server — ANNOTATED REFERENCE COPY
--------------------------------------------
This is the same code that's deployed on Render, with an explanation
above every section. Use this to understand the logic; deploy the plain
oura_mcp_server.py file (without the extra commentary) to keep things
readable in production.

ENVIRONMENT VARIABLES (set these in Render, not in this file):
  OURA_CLIENT_ID       - from the Oura developer portal
  OURA_CLIENT_SECRET   - from the Oura developer portal
  OURA_REFRESH_TOKEN   - obtained once via oura_recovery.py (see README);
                          only used the very first time, before Upstash has
                          a saved token
  MCP_API_KEY           - a secret you make up; Claude must send this to
                          prove it's allowed to call your server
  UPSTASH_REDIS_REST_URL   - from your Upstash database's REST API section
  UPSTASH_REDIS_REST_TOKEN - from your Upstash database's REST API section
"""

import os
import time
import threading
import json
from datetime import date, timedelta

import requests
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse

# ============================================================
# STEP 1: Load configuration from environment variables.
# Nothing secret is ever hardcoded in this file — every credential
# comes from Render's environment variables, so the code itself is
# safe to keep in a public GitHub repo.
# ============================================================
CLIENT_ID = os.environ["OURA_CLIENT_ID"]
CLIENT_SECRET = os.environ["OURA_CLIENT_SECRET"]
API_KEY = os.environ.get("MCP_API_KEY")

# Upstash is optional at the code level (falls back gracefully if unset),
# even though in practice you should always configure it — see Step 3.
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
REFRESH_TOKEN_KEY = "oura_refresh_token"  # the key we store the token under in Upstash

TOKEN_URL = "https://api.ouraring.com/oauth/token"
API_BASE = "https://api.ouraring.com/v2/usercollection"


# ============================================================
# STEP 2: Tiny helper functions to talk to Upstash (a hosted
# key-value store, accessed over plain HTTP). We only ever store
# one value here — the current Oura refresh token — so these
# helpers are intentionally minimal, not a general database layer.
# ============================================================
def upstash_get(key):
    if not UPSTASH_URL:
        return None
    try:
        resp = requests.get(
            f"{UPSTASH_URL}/get/{key}",
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
        )
        resp.raise_for_status()
        return resp.json().get("result")
    except Exception as e:
        print(f"[upstash] get failed: {e}")
        return None


def upstash_set(key, value):
    """Stores `value` (must already be a plain string, e.g. from
    json.dumps() if it's structured data) as the raw request body — this
    is Upstash's documented convention. IMPORTANT: don't pass json=value
    to requests.post here; that wraps the value in an extra layer of
    JSON encoding, which silently corrupts anything read back later
    (a real bug hit while building this: saving a dict via json.dumps()
    and then ALSO passing json=value double-encoded it, so loading it
    back produced a string instead of a dict)."""
    if not UPSTASH_URL:
        return
    try:
        resp = requests.post(
            f"{UPSTASH_URL}/set/{key}",
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
            data=value,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"[upstash] set failed: {e}")


# ============================================================
# STEP 3: On startup, try to load a previously-saved refresh token
# from Upstash. This is what makes the server survive restarts:
# if Oura has already rotated the token since the last deploy, the
# OURA_REFRESH_TOKEN env var is stale, but Upstash has the current one.
# Only on the very first-ever run (Upstash empty) do we fall back to
# the env var.
# ============================================================
_saved_refresh_token = upstash_get(REFRESH_TOKEN_KEY)
if _saved_refresh_token:
    print("[oura] loaded refresh token from Upstash (persisted from a previous run)")
else:
    print("[oura] no saved refresh token found in Upstash, using OURA_REFRESH_TOKEN env var")

# In-memory cache for the current access token, so we don't hit Oura's
# token endpoint on every single request — only when it's actually expired.
_token_cache = {
    "access_token": None,
    "refresh_token": _saved_refresh_token or os.environ["OURA_REFRESH_TOKEN"],
    "expires_at": 0,
}


# ============================================================
# STEP 4: The core OAuth "refresh" logic for talking to Oura.
# Called before every Oura API request. If we already have a valid
# (non-expired) access token cached, reuse it. Otherwise, exchange
# the refresh token for a new access token — and critically, if Oura
# gives us a NEW refresh token in that response (it usually does —
# this is "refresh token rotation"), save it to Upstash immediately
# so a future restart picks up the current one, not a stale one.
#
# The lock below guards against a real race condition that showed up
# in practice: if two requests (e.g. from two different chats) both
# find the cached token expired at the same instant, both would try
# to refresh simultaneously. Oura's refresh tokens are single-use, so
# whichever request loses the race gets an "invalid_request" error
# even though nothing is actually wrong with the credentials. The lock
# makes the second request wait for the first to finish and reuse its
# result instead of firing a competing refresh.
# ============================================================
_refresh_lock = threading.Lock()


def get_access_token():
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    with _refresh_lock:
        # Re-check after acquiring the lock: another thread may have
        # already refreshed while we were waiting our turn.
        if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 60:
            return _token_cache["access_token"]

        resp = requests.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": _token_cache["refresh_token"],
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        })
        if not resp.ok:
            # Logged so Render's Logs tab shows Oura's *actual* error message,
            # not just a bare "400 Bad Request" with no context.
            print(f"[oura] refresh_token exchange failed ({resp.status_code}): {resp.text}")
        resp.raise_for_status()

        tokens = resp.json()
        _token_cache["access_token"] = tokens["access_token"]

        new_refresh_token = tokens.get("refresh_token", _token_cache["refresh_token"])
        if new_refresh_token != _token_cache["refresh_token"]:
            # Oura rotated the refresh token — persist the new one so we
            # don't lose it the next time this server restarts.
            _token_cache["refresh_token"] = new_refresh_token
            upstash_set(REFRESH_TOKEN_KEY, new_refresh_token)
            print("[oura] refresh token rotated, saved new one to Upstash")

    _token_cache["expires_at"] = time.time() + tokens.get("expires_in", 3600)
    return _token_cache["access_token"]


# ============================================================
# STEP 5: A small wrapper around every call to Oura's actual data
# endpoints (sleep, readiness, spo2, etc). It attaches the access
# token automatically, and — importantly — if one specific endpoint
# fails (e.g. blood oxygen isn't available on your ring/plan), it
# logs a warning and returns an empty list instead of crashing the
# whole tool call. The rest of the data still comes through fine.
# ============================================================
def api_get(endpoint, params):
    token = get_access_token()
    resp = requests.get(
        f"{API_BASE}/{endpoint}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError:
        print(f"[oura] could not fetch '{endpoint}' (status {resp.status_code}), skipping")
        return []
    return resp.json().get("data", [])


# ============================================================
# STEP 6: Create the MCP server object itself. "FastMCP" is the
# library that turns plain Python functions into tools an AI model
# can discover and call. host="0.0.0.0" means "listen on all network
# interfaces" (required for Render to route traffic to it); the port
# comes from Render's PORT env var.
# ============================================================
mcp = FastMCP("oura-recovery", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))


# ============================================================
# STEP 7: The actual tools Claude can call. The @mcp.tool() decorator
# is what exposes a plain Python function to the AI model — its
# docstring becomes the description the model reads to decide when
# to use it, and its type hints define the expected inputs/outputs.
# ============================================================
@mcp.tool()
def get_recovery_summary(target_date: str = "") -> dict:
    """Get sleep and recovery metrics for a given night: HRV, resting heart
    rate, respiratory rate, blood oxygen, wrist temperature deviation from
    baseline, and readiness score. target_date is YYYY-MM-DD; if omitted,
    returns the most recently completed night."""
    d = date.fromisoformat(target_date) if target_date else date.today() - timedelta(days=1)
    start, end = d.isoformat(), (d + timedelta(days=1)).isoformat()

    # Three separate Oura endpoints, each covering one part of the picture.
    sleep_periods = api_get("sleep", {"start_date": start, "end_date": end})
    spo2 = api_get("daily_spo2", {"start_date": start, "end_date": end})
    readiness = api_get("daily_readiness", {"start_date": start, "end_date": end})

    # "sleep" can return multiple periods (naps, etc) — we only want the
    # main overnight sleep, tagged "long_sleep" by Oura.
    main_sleep = next((s for s in sleep_periods if s.get("type") == "long_sleep"), None)
    spo2_record = spo2[0] if spo2 else None
    readiness_record = readiness[0] if readiness else None

    # Assemble one clean dict, with every field defaulting to None if
    # that data wasn't available (rather than raising an error).
    return {
        "date": start,
        "hrv_ms": main_sleep.get("average_hrv") if main_sleep else None,
        "resting_heart_rate_bpm": main_sleep.get("lowest_heart_rate") if main_sleep else None,
        "respiratory_rate_brpm": main_sleep.get("average_breath") if main_sleep else None,
        "blood_oxygen_pct": (spo2_record or {}).get("spo2_percentage", {}).get("average") if spo2_record else None,
        "wrist_temp_deviation_c": readiness_record.get("temperature_deviation") if readiness_record else None,
        "readiness_score": readiness_record.get("score") if readiness_record else None,
    }


@mcp.tool()
def get_recent_trend(days: int = 7) -> list:
    """Get HRV, resting heart rate, and readiness score for each of the last
    N nights (default 7), useful for spotting trends over time."""
    end_date = date.today()
    start_date = end_date - timedelta(days=days)
    sleep_periods = api_get("sleep", {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()})
    readiness = api_get("daily_readiness", {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()})

    # Build a lookup so we can match each night's sleep record to its
    # same-day readiness record by date.
    readiness_by_day = {r["day"]: r for r in readiness}

    results = []
    for s in sleep_periods:
        if s.get("type") != "long_sleep":
            continue
        day = s.get("day")
        r = readiness_by_day.get(day, {})
        results.append({
            "date": day,
            "hrv_ms": s.get("average_hrv"),
            "resting_heart_rate_bpm": s.get("lowest_heart_rate"),
            "readiness_score": r.get("score"),
        })
    results.sort(key=lambda x: x["date"])
    return results


## ============================================================
## STEP 8: Minimal built-in OAuth server.
##
## Why this exists: Claude's custom connector UI currently only offers
## OAuth as an authentication method, not simple API key headers.
## Rather than run a separate proxy service, this server plays double
## duty — it's both the thing serving Oura tools AND a tiny OAuth
## "front door" that Claude logs into.
##
## The flow, in order:
##   1. Claude auto-registers itself as a client (POST /register)
##   2. Claude opens /authorize in a browser popup
##   3. We show a password page — the password is your MCP_API_KEY.
##      This password check is the ENTIRE real security of the system.
##   4. Correct password -> we issue a one-time code, redirect back to
##      Claude with it
##   5. Claude exchanges that code at /token (with PKCE verification)
##      for a bearer token
##   6. Every future /mcp request must carry that bearer token
##
## Note: bearer tokens ARE persisted to Upstash now (see the
## _load_access_tokens / _save_access_tokens pair below) — a Render
## restart no longer forces you to redo the password login. This wasn't
## true in an earlier version of this file. Getting it right took two
## tries: the first attempt double-JSON-encoded the saved data (passed
## an already-json.dumps()'d string into a function that ALSO
## JSON-encoded it), which silently corrupted it and crashed the server
## on every startup with `AttributeError: 'str' object has no attribute
## 'items'` until the encoding was fixed and the corrupted Upstash key
## was manually cleared once.
## ============================================================

import base64
import hashlib
import html
import secrets
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

_clients = {}         # client_id -> {redirect_uris} (in-memory only; low
                       # stakes if lost, Claude just re-registers)
_auth_codes = {}       # one-time codes -> who they belong to + when they
                       # expire (in-memory only; these live for 5 minutes
                       # by design, no need to persist)

ACCESS_TOKENS_KEY = "mcp_access_tokens"   # Upstash key for issued bearer tokens
AUTH_CODE_TTL = 300                    # 5 minutes to complete the login flow
ACCESS_TOKEN_TTL = 60 * 60 * 24 * 30   # 30 days before Claude needs to log in again


def _load_access_tokens():
    """Loads previously-issued bearer tokens from Upstash on startup.
    Defensive by design: if the saved data is missing, unparseable, or
    not the shape we expect (e.g. from a bug, or manual tampering), log
    a warning and start with an empty dict instead of crashing the
    entire server — a corrupted cache entry shouldn't take down every
    tool call."""
    saved_raw = upstash_get(ACCESS_TOKENS_KEY)
    if not saved_raw:
        print("[oauth] no saved access tokens found in Upstash")
        return {}
    try:
        saved = json.loads(saved_raw) if isinstance(saved_raw, str) else saved_raw
    except (TypeError, ValueError):
        print("[oauth] could not parse saved access tokens, starting fresh")
        return {}
    if not isinstance(saved, dict):
        print(f"[oauth] saved access tokens were not a dict (got {type(saved).__name__}), starting fresh")
        return {}
    now = time.time()
    # Drop anything already expired instead of carrying it forward forever.
    valid = {tok: rec for tok, rec in saved.items() if rec.get("expires_at", 0) > now}
    print(f"[oauth] loaded {len(valid)} valid access token(s) from Upstash")
    return valid


def _save_access_tokens():
    # json.dumps() here is deliberate and necessary — Upstash values are
    # always strings, so a dict must be explicitly serialized. Compare
    # this to upstash_set()'s own docstring: passing a dict directly
    # into requests.post(json=value) would double-encode it.
    upstash_set(ACCESS_TOKENS_KEY, json.dumps(_access_tokens))


_access_tokens = _load_access_tokens()   # token -> {expires_at}


def _issuer(request: Request) -> str:
    """Returns this server's own base URL, e.g. https://ouramcp.onrender.com
    — used to build the URLs we advertise in our OAuth metadata."""
    return f"{request.url.scheme}://{request.url.netloc}"


# STEP 8a: Discovery endpoints. These are standard, well-known URLs
# that OAuth clients (like Claude) check automatically to find out
# where to send registration/login/token requests — you never call
# these yourself, Claude does, behind the scenes.
async def oauth_metadata(request: Request):
    issuer = _issuer(request)
    return JSONResponse({
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "registration_endpoint": f"{issuer}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    })


async def protected_resource_metadata(request: Request):
    issuer = _issuer(request)
    return JSONResponse({
        "resource": f"{issuer}/mcp",
        "authorization_servers": [issuer],
    })


# STEP 8b: Dynamic client registration. Claude calls this once to get
# a client_id before starting the login flow. We don't actually need
# to validate anything meaningful here — we just hand out an ID so the
# rest of the standard OAuth flow has something to reference.
async def register_client(request: Request):
    body = await request.json()
    print(f"[oauth] /register called with body: {body}")
    client_id = secrets.token_urlsafe(16)
    _clients[client_id] = {
        "redirect_uris": body.get("redirect_uris", []),
    }
    return JSONResponse({
        "client_id": client_id,
        "redirect_uris": body.get("redirect_uris", []),
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
    })


# A tiny inline HTML template for the password page — no separate
# templating library needed for something this small.
_AUTHORIZE_FORM = """
<!doctype html>
<html><body style="font-family: sans-serif; max-width: 400px; margin: 80px auto;">
<h2>Authorize access to your Oura data</h2>
<form method="POST">
  {hidden_fields}
  <label>Password (your MCP_API_KEY):</label><br>
  <input type="password" name="password" style="width:100%; padding:8px; margin:8px 0;" autofocus>
  <br>
  <button type="submit" style="padding:8px 16px;">Approve</button>
</form>
{error}
</body></html>
"""


def _hidden_fields(params: dict) -> str:
    """Re-embeds all the OAuth query params (client_id, redirect_uri,
    state, etc) as hidden form fields, so they survive the round trip
    from the GET (show the form) to the POST (submit the password)."""
    return "\n".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in params.items() if v is not None
    )


# STEP 8c: The authorize endpoint — this is the actual password gate.
# GET request -> show the password form.
# POST request -> check the password; if correct, issue a one-time
# code and redirect back to Claude with it (standard OAuth "authorization
# code" step). If wrong, redisplay the form with an error.
async def authorize(request: Request):
    if request.method == "GET":
        params = dict(request.query_params)
    else:
        form = await request.form()
        params = dict(form)

    required = ["client_id", "redirect_uri", "response_type", "state"]
    for key in required:
        if not params.get(key):
            return PlainTextResponse(f"Missing required parameter: {key}", status_code=400)

    if request.method == "POST":
        password = params.pop("password", "")
        if not API_KEY or password != API_KEY:
            return HTMLResponse(
                _AUTHORIZE_FORM.format(
                    hidden_fields=_hidden_fields(params),
                    error='<p style="color:red;">Incorrect password, try again.</p>',
                ),
                status_code=401,
            )
        # Password correct: mint a one-time code tied to this specific
        # login attempt (client, redirect target, and PKCE challenge),
        # then send the browser back to Claude with it.
        code = secrets.token_urlsafe(24)
        _auth_codes[code] = {
            "client_id": params.get("client_id"),
            "redirect_uri": params.get("redirect_uri"),
            "code_challenge": params.get("code_challenge"),
            "expires_at": time.time() + AUTH_CODE_TTL,
        }
        redirect_to = f"{params['redirect_uri']}?{urlencode({'code': code, 'state': params['state']})}"
        print(f"[oauth] password accepted, redirecting to: {redirect_to}")
        return RedirectResponse(redirect_to, status_code=302)

    return HTMLResponse(_AUTHORIZE_FORM.format(hidden_fields=_hidden_fields(params), error=""))


# STEP 8d: The token endpoint. Claude calls this right after the
# redirect above, trading the one-time code for a real bearer token
# it will use on every subsequent request. PKCE verification here
# proves the same client that started the flow is the one finishing
# it (protects against the code being intercepted in transit).
async def token(request: Request):
    form = await request.form()
    print(f"[oauth] /token called with grant_type={form.get('grant_type')} "
          f"code={form.get('code')} has_verifier={bool(form.get('code_verifier'))}")
    grant_type = form.get("grant_type")
    if grant_type != "authorization_code":
        print(f"[oauth] rejecting: unsupported grant_type {grant_type}")
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    code = form.get("code")
    verifier = form.get("code_verifier", "")
    record = _auth_codes.pop(code, None)  # one-time use: pop, don't just read
    if not record or record["expires_at"] < time.time():
        print(f"[oauth] rejecting: invalid or expired code (known codes: {list(_auth_codes.keys())})")
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    # PKCE check: does hashing the verifier Claude just sent match the
    # challenge it originally sent back in step /authorize?
    challenge = record.get("code_challenge")
    if challenge:
        computed = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        if computed != challenge:
            print(f"[oauth] rejecting: PKCE mismatch. computed={computed} expected={challenge}")
            return JSONResponse({"error": "invalid_grant", "error_description": "PKCE mismatch"}, status_code=400)

    # Everything checks out: issue the real bearer token Claude will
    # use going forward, and persist it immediately so a restart
    # doesn't force Claude to log in again.
    access_token = secrets.token_urlsafe(32)
    _access_tokens[access_token] = {"expires_at": time.time() + ACCESS_TOKEN_TTL}
    _save_access_tokens()
    print(f"[oauth] issued access token successfully, saved to Upstash")

    return JSONResponse({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL,
    })


# STEP 8e: The gatekeeper. This middleware runs on EVERY incoming
# request (except the OAuth/health routes themselves) and rejects
# anything that doesn't carry a bearer token we issued in step 8d.
# This is what actually protects your Oura data from random internet
# traffic hitting the server.
class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Requires a valid bearer token (issued by our own /token endpoint)
    on every request except the OAuth discovery/auth routes and /healthz."""
    OPEN_PATHS = {"/healthz", "/authorize", "/token", "/register",
                  "/.well-known/oauth-authorization-server",
                  "/.well-known/oauth-protected-resource"}

    async def dispatch(self, request, call_next):
        if request.url.path in self.OPEN_PATHS:
            return await call_next(request)
        auth_header = request.headers.get("authorization", "")
        supplied = auth_header.removeprefix("Bearer ").strip()
        record = _access_tokens.get(supplied)
        if not record or record["expires_at"] < time.time():
            # The WWW-Authenticate header here is what tells Claude
            # "you need to log in, and here's where" — without it,
            # Claude just sees a bare 401 and gives up instead of
            # starting the OAuth flow.
            issuer = _issuer(request)
            resource_metadata_url = f"{issuer}/.well-known/oauth-protected-resource"
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={
                    "WWW-Authenticate": f'Bearer resource_metadata="{resource_metadata_url}"'
                },
            )
        return await call_next(request)


# ============================================================
# STEP 9: Wire everything together into one Starlette app — the
# MCP tool-serving app (from FastMCP) plus all the OAuth routes plus
# the security middleware plus a plain health-check endpoint Render
# and you can poll to confirm the server is alive.
# ============================================================
app = mcp.streamable_http_app()
app.add_middleware(BearerAuthMiddleware)

app.add_route("/.well-known/oauth-authorization-server", oauth_metadata)
app.add_route("/.well-known/oauth-protected-resource", protected_resource_metadata)
app.add_route("/register", register_client, methods=["POST"])
app.add_route("/authorize", authorize, methods=["GET", "POST"])
app.add_route("/token", token, methods=["POST"])


async def healthz(request):
    return PlainTextResponse("ok")


app.add_route("/healthz", healthz)


# ============================================================
# STEP 10: Start the actual web server. Render runs this file directly
# (python oura_mcp_server.py), which triggers this block and starts
# uvicorn listening on the port Render assigns via the PORT env var.
# ============================================================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
