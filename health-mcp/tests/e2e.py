"""End-to-end check: real server process, mock OIDC issuer, real MCP client over HTTP.

Run from the project root:  python tests/e2e.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

import httpx
import uvicorn
from joserfc import jwt
from joserfc.jwk import RSAKey
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

try:
    from mcp.client.streamable_http import streamablehttp_client as http_client
except ImportError:  # newer 1.x name
    from mcp.client.streamable_http import streamable_http_client as http_client
from mcp import ClientSession

ISSUER_PORT, APP_PORT = 9100, 8765
ISSUER = f"http://127.0.0.1:{ISSUER_PORT}"
APP = f"http://127.0.0.1:{APP_PORT}"
AUD = "health-mcp"
INGEST_TOKEN = "t" * 40
KEY = RSAKey.generate_key(2048, parameters={"kid": "e2e", "use": "sig", "alg": "RS256"})
OPAQUE = "opaque-access-token-123"

checks: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def mint(**over) -> str:
    claims = {"iss": ISSUER, "aud": [AUD], "sub": "khushal", "exp": int(time.time()) + 600, "scope": "openid"}
    claims.update(over)
    return jwt.encode({"alg": "RS256", "kid": "e2e"}, claims, KEY)


async def discovery(_: Request):
    return JSONResponse({"issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks", "authorization_endpoint": f"{ISSUER}/authorize",
                         "token_endpoint": f"{ISSUER}/token", "introspection_endpoint": f"{ISSUER}/introspect"})


async def jwks(_: Request):
    return JSONResponse({"keys": [KEY.as_dict(private=False)]})


async def introspect(request: Request):
    form = await request.form()
    if form.get("token") == OPAQUE:
        return JSONResponse({"active": True, "sub": "khushal", "client_id": AUD, "exp": int(time.time()) + 300,
                             "iss": ISSUER, "scope": "openid"})
    return JSONResponse({"active": False})


def start_issuer() -> uvicorn.Server:
    app = Starlette(routes=[Route("/.well-known/openid-configuration", discovery), Route("/jwks", jwks),
                            Route("/introspect", introspect, methods=["POST"])])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=ISSUER_PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    return server


PAYLOAD = {
    "timestamp": "2026-11-10T13:59:00Z", "app_version": "e2e",
    "steps": [{"count": 4200, "start_time": "NOW-6h", "end_time": "NOW-5h"}],
    "heart_rate": [{"bpm": 64, "time": "NOW-3h"}, {"bpm": 92, "time": "NOW-2h"}],
    "weight": [{"kilograms": 78.4, "time": "NOW-4h"}],
    "exercise": [{"type": "RUNNING_TREADMILL", "start_time": "NOW-3h", "end_time": "NOW-2h", "duration_seconds": 3600}],
    "active_calories": [{"calories": 300.0, "start_time": "NOW-3h", "end_time": "NOW-2h"}],
    "sleep": [{"session_end_time": "NOW-8h", "duration_seconds": 27000, "stages": [
        {"stage": "STAGE_TYPE_DEEP", "duration_seconds": 5400}, {"stage": "STAGE_TYPE_LIGHT", "duration_seconds": 18000},
        {"stage": "STAGE_TYPE_AWAKE", "duration_seconds": 3600}]}],
}


def materialise(payload: dict) -> dict:
    """Replace NOW-Nh markers with real timestamps so data lands in 'today'."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    text = json.dumps(payload)
    for h in range(1, 12):
        text = text.replace(f'"NOW-{h}h"', json.dumps((now - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")))
    return json.loads(text)


def payload_text(r) -> dict:
    try:
        return json.loads(r.content[0].text)
    except ValueError:
        raise RuntimeError(f"tool returned non-JSON (isError={r.isError}): {r.content[0].text[:300]}")


async def mcp_session(token: str, label: str):
    async with http_client(f"{APP}/mcp", headers={"Authorization": f"Bearer {token}"}) as (rd, wr, _):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            tools = await s.list_tools()
            out = {"tools": [t.name for t in tools.tools]}
            out["status"] = payload_text(await s.call_tool("data_status", {}))
            out["summary"] = payload_text(await s.call_tool("health_summary", {"days": 2}))
            out["sleep"] = payload_text(await s.call_tool("get_sleep", {"days": 1}))
            out["bp_log"] = payload_text(await s.call_tool("log_blood_pressure",
                                                           {"systolic": 148, "diastolic": 98, "pulse": 71, "note": "NHS cuff"}))
            out["bp"] = payload_text(await s.call_tool("get_blood_pressure", {"days": 7}))
            out["meal"] = payload_text(await s.call_tool("log_meal", {"name": f"Poha with peanuts (estimate, {label})", "kcal": 450,
                                                                        "protein_g": 10, "salt_g": 1.0}))
            out["food"] = payload_text(await s.call_tool("get_food_log", {"days": 1}))
            out["weight"] = payload_text(await s.call_tool("get_weight", {"days": 7}))
            out["workouts"] = payload_text(await s.call_tool("get_workouts", {"days": 2}))
            out["deleted"] = payload_text(await s.call_tool("delete_manual_entry",
                                                            {"kind": "blood_pressure", "entry_id": out["bp_log"]["id"]}))
            bad = await s.call_tool("log_blood_pressure", {"systolic": 80, "diastolic": 120})
            out["bad_bp_is_error"] = bool(bad.isError)
            return out


async def main() -> int:
    start_issuer()
    data_dir = tempfile.mkdtemp()
    env = dict(os.environ, PUBLIC_URL=APP, INGEST_TOKEN=INGEST_TOKEN, OIDC_ISSUER=ISSUER, OIDC_AUDIENCE=AUD,
               OIDC_CLIENT_SECRET="secret", DATA_DIR=data_dir, PORT=str(APP_PORT), HOST="127.0.0.1", LOG_LEVEL="INFO")
    proc = subprocess.Popen([sys.executable, "-m", "app.server"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            for _ in range(60):
                try:
                    if (await c.get(f"{APP}/healthz")).status_code == 200:
                        break
                except httpx.HTTPError:
                    await asyncio.sleep(0.25)
            else:
                print("server did not start"); return 1

            r = await c.post(f"{APP}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            check("MCP endpoint rejects no token with 401", r.status_code == 401, str(r.status_code))
            check("401 points to resource metadata", "resource_metadata" in r.headers.get("www-authenticate", ""),
                  r.headers.get("www-authenticate", ""))
            r = await c.get(f"{APP}/.well-known/oauth-protected-resource/mcp")
            check("protected-resource metadata names the issuer", r.status_code == 200 and ISSUER in json.dumps(r.json()),
                  r.text[:200])
            r = await c.get(f"{APP}/.well-known/oauth-authorization-server")
            check("authorization-server metadata proxied", r.status_code == 200 and r.json().get("token_endpoint") == f"{ISSUER}/token")
            for name, tok in {"expired JWT": mint(exp=int(time.time()) - 5), "wrong audience": mint(aud=["x"]),
                              "wrong issuer": mint(iss="http://evil"), "garbage": "not-a-token",
                              "unknown opaque": "opaque-unknown"}.items():
                r = await c.post(f"{APP}/mcp", headers={"Authorization": f"Bearer {tok}"},
                                 json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
                check(f"MCP rejects {name}", r.status_code == 401, str(r.status_code))

            body = materialise(PAYLOAD)
            r = await c.post(f"{APP}/ingest", json=body)
            check("ingest rejects missing key", r.status_code == 401)
            r = await c.post(f"{APP}/ingest", json=body, headers={"x-api-key": "wrong" * 10})
            check("ingest rejects wrong key", r.status_code == 401)
            r = await c.post(f"{APP}/ingest", json=body, headers={"x-api-key": INGEST_TOKEN})
            check("ingest accepts x-api-key", r.status_code == 200 and r.json()["counts"]["steps"]["stored"] == 1, r.text)
            r = await c.post(f"{APP}/ingest", json=body, headers={"Authorization": f"Bearer {INGEST_TOKEN}"})
            check("ingest idempotent via bearer", r.status_code == 200 and r.json()["counts"]["steps"]["stored"] == 0, r.text)
            r = await c.post(f"{APP}/ingest?key={INGEST_TOKEN}", json=body)
            check("?key= in the URL is refused by default", r.status_code == 401, str(r.status_code))
            r = await c.post(f"{APP}/ingest", content=b"{bad json", headers={"x-api-key": INGEST_TOKEN})
            check("ingest rejects invalid JSON", r.status_code == 400)
            r = await c.post(f"{APP}/ingest", json=[1, 2], headers={"x-api-key": INGEST_TOKEN})
            check("ingest rejects non-object", r.status_code == 400)
            r = await c.post(f"{APP}/ingest", json={"steps": [{"count": "x"}]}, headers={"x-api-key": INGEST_TOKEN})
            check("ingest survives junk rows", r.status_code == 200 and r.json()["counts"]["steps"]["rejected"] == 1)

        for label, token in (("JWT", mint()), ("opaque token", OPAQUE)):
            out = await mcp_session(token, label)
            check(f"[{label}] 18 tools listed", len(out["tools"]) == 18, str(out["tools"]))
            today = out["summary"]["days"][-1]
            check(f"[{label}] summary has synced steps, HR, weight",
                  today["steps"] == 4200 and today["heart_rate_min"] == 64 and today["weight_kg"] == 78.4, str(today))
            sl = out["sleep"]["days"][-1]
            check(f"[{label}] sleep excludes awake time", sl["asleep_hours"] == 6.5 and sl["awake_minutes"] == 60, str(sl))
            check(f"[{label}] BP logged, averaged and deleted",
                  out["bp_log"]["systolic"] == 148 and out["bp"]["stats"]["average_all"]["diastolic"] == 98.0
                  and out["deleted"]["deleted"] is True, str(out["bp"]))
            mine = [m for m in out["food"]["days"][0]["meals"] if m["name"].endswith(f"{label})")]
            check(f"[{label}] meal salt round-trips",
                  len(mine) == 1 and mine[0]["salt_g"] == 1.0 and mine[0]["kcal"] == 450, str(out["food"]))
            wk = out["workouts"]["workouts"]
            check(f"[{label}] workout stored with heart rate and calories",
                  len(wk) == 1 and wk[0]["minutes"] == 60 and wk[0]["avg_hr"] == 78
                  and wk[0]["active_kcal_estimate"] == 300, str(out["workouts"]))
            check(f"[{label}] implausible BP returns a tool error", out["bad_bp_is_error"])
            check(f"[{label}] status reports fresh sync", out["status"]["stale"] is False, str(out["status"]))
    finally:
        proc.terminate()
        try:
            logs = proc.communicate(timeout=5)[0].decode()
        except subprocess.TimeoutExpired:
            proc.kill(); logs = ""
        leaked = [w for w in (INGEST_TOKEN, "secret") if w in logs]
        check("server logs contain no secrets", not leaked, str(leaked))
    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
