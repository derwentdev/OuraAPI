"""
Oura MCP Server
----------------
Exposes your Oura recovery data as tools an MCP-compatible AI client
(like Claude) can call remotely.

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
from datetime import date, timedelta

import requests
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse

CLIENT_ID = os.environ["OURA_CLIENT_ID"]
CLIENT_SECRET = os.environ["OURA_CLIENT_SECRET"]
API_KEY = os.environ.get("MCP_API_KEY")

UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
REFRESH_TOKEN_KEY = "oura_refresh_token"

TOKEN_URL = "https://api.ouraring.com/oauth/token"
API_BASE = "https://api.ouraring.com/v2/usercollection"


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
    if not UPSTASH_URL:
        return
    try:
        resp = requests.post(
            f"{UPSTASH_URL}/set/{key}",
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
            json=value,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"[upstash] set failed: {e}")


_saved_refresh_token = upstash_get(REFRESH_TOKEN_KEY)
if _saved_refresh_token:
    print("[oura] loaded refresh token from Upstash (persisted from a previous run)")
else:
    print("[oura] no saved refresh token found in Upstash, using OURA_REFRESH_TOKEN env var")

_token_cache = {
    "access_token": None,
    "refresh_token": _saved_refresh_token or os.environ["OURA_REFRESH_TOKEN"],
    "expires_at": 0,
}


def get_access_token():
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": _token_cache["refresh_token"],
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })
    if not resp.ok:
        print(f"[oura] refresh_token exchange failed ({resp.status_code}): {resp.text}")
    resp.raise_for_status()
    tokens = resp.json()
    _token_cache["access_token"] = tokens["access_token"]
    new_refresh_token = tokens.get("refresh_token", _token_cache["refresh_token"])
    if new_refresh_token != _token_cache["refresh_token"]:
        _token_cache["refresh_token"] = new_refresh_token
        upstash_set(REFRESH_TOKEN_KEY, new_refresh_token)
        print("[oura] refresh token rotated, saved new one to Upstash")
    _token_cache["expires_at"] = time.time() + tokens.get("expires_in", 3600)
    return _token_cache["access_token"]


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


mcp = FastMCP("oura-recovery", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))


@mcp.tool()
def get_recovery_summary(target_date: str = "") -> dict:
    """Get sleep and recovery metrics for a given night: HRV, resting heart
    rate, respiratory rate, blood oxygen, wrist temperature deviation from
    baseline, and readiness score. target_date is YYYY-MM-DD; if omitted,
    returns the most recently completed night."""
    d = date.fromisoformat(target_date) if target_date else date.today() - timedelta(days=1)
    start, end = d.isoformat(), (d + timedelta(days=1)).isoformat()

    sleep_periods = api_get("sleep", {"start_date": start, "end_date": end})
    spo2 = api_get("daily_spo2", {"start_date": start, "end_date": end})
    readiness = api_get("daily_readiness", {"start_date": start, "end_date": end})

    main_sleep = next((s for s in sleep_periods if s.get("type") == "long_sleep"), None)
    spo2_record = spo2[0] if spo2 else None
    readiness_record = readiness[0] if readiness else None

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


## ---------------------------------------------------------------------
## Minimal built-in OAuth server
##
## Claude's custom connector UI currently only offers OAuth for auth, not
## raw headers. Rather than run a separate proxy, this server plays the
## role of a tiny OAuth authorization server itself:
##   - Claude auto-registers as a client (dynamic client registration)
##   - Claude opens /authorize in a browser -> we show a password page
##     (the password is your MCP_API_KEY) -> only a correct password
##     issues an auth code
##   - Claude exchanges that code at /token for a bearer token (PKCE
##     verified)
##   - Every /mcp request must carry that bearer token
##
## Tokens are stored in memory only. If the Render service restarts, any
## issued tokens are lost and you'll need to reconnect the connector in
## Claude (click Connect again) - a minor inconvenience, not a bug.
## ---------------------------------------------------------------------

import base64
import hashlib
import html
import secrets
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

_clients = {}        # client_id -> {redirect_uris}
_auth_codes = {}      # code -> {client_id, redirect_uri, code_challenge, expires_at}
_access_tokens = {}   # token -> {expires_at}

AUTH_CODE_TTL = 300        # 5 minutes to complete the flow
ACCESS_TOKEN_TTL = 60 * 60 * 24 * 30  # 30 days


def _issuer(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


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
    return "\n".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in params.items() if v is not None
    )


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
    record = _auth_codes.pop(code, None)
    if not record or record["expires_at"] < time.time():
        print(f"[oauth] rejecting: invalid or expired code (known codes: {list(_auth_codes.keys())})")
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    challenge = record.get("code_challenge")
    if challenge:
        computed = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        if computed != challenge:
            print(f"[oauth] rejecting: PKCE mismatch. computed={computed} expected={challenge}")
            return JSONResponse({"error": "invalid_grant", "error_description": "PKCE mismatch"}, status_code=400)

    access_token = secrets.token_urlsafe(32)
    _access_tokens[access_token] = {"expires_at": time.time() + ACCESS_TOKEN_TTL}
    print(f"[oauth] issued access token successfully")

    return JSONResponse({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL,
    })


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


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
