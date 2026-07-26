"""
Oura MCP Server
----------------
Exposes your Oura recovery data as tools an MCP-compatible AI client
(like Claude) can call remotely.

ENVIRONMENT VARIABLES (set these in Render, not in this file):
  OURA_CLIENT_ID       - from the Oura developer portal
  OURA_CLIENT_SECRET   - from the Oura developer portal
  OURA_REFRESH_TOKEN   - obtained once via oura_recovery.py (see README)
  MCP_API_KEY           - a secret you make up; Claude must send this to
                          prove it's allowed to call your server
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

TOKEN_URL = "https://api.ouraring.com/oauth/token"
API_BASE = "https://api.ouraring.com/v2/usercollection"

_token_cache = {
    "access_token": None,
    "refresh_token": os.environ["OURA_REFRESH_TOKEN"],
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
    resp.raise_for_status()
    tokens = resp.json()
    _token_cache["access_token"] = tokens["access_token"]
    _token_cache["refresh_token"] = tokens.get("refresh_token", _token_cache["refresh_token"])
    _token_cache["expires_at"] = time.time() + tokens.get("expires_in", 3600)
    return _token_cache["access_token"]


def api_get(endpoint, params):
    token = get_access_token()
    resp = requests.get(
        f"{API_BASE}/{endpoint}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )
    resp.raise_for_status()
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


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Requires every request (except /healthz) to include a matching
    X-Api-Key header, so random internet traffic can't hit your Oura data."""
    async def dispatch(self, request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        if API_KEY and request.headers.get("x-api-key") != API_KEY:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


app = mcp.streamable_http_app()
app.add_middleware(ApiKeyMiddleware)


async def healthz(request):
    return PlainTextResponse("ok")


app.add_route("/healthz", healthz)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
