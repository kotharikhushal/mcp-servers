"""Blood pressure readings from a Withings cuff (for example BPM Connect) through Withings' public API.

The Withings app can only send data to other apps on its paid plan, so this reads the readings
straight from Withings instead, with the person's permission (OAuth). Only the "user.metrics"
scope is requested. Each person connects their own Withings account from the Settings page.

How it works:
  * Connect sends the person to Withings to allow access; Withings sends them back to
    /withings/callback with a one-time code, which is exchanged for tokens.
  * The tokens are kept in that person's own settings table. The access token lasts three hours
    and is renewed with the refresh token; Withings hands out a new refresh token each time, so the
    new one is saved straight away.
  * A background thread pulls new readings about every 15 minutes (Withings asks for no more than
    one request per 10 minutes). Readings are stored with source "withings", and a reading that is
    already there is ignored, so repeating a sync is harmless.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

import httpx

from .store import Store

log = logging.getLogger("health-mcp.withings")

AUTH_URL = "https://account.withings.com/oauth2_user/authorize2"
TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
MEASURE_URL = "https://wbsapi.withings.net/measure"
SCOPE = "user.metrics"
POLL_SECONDS = 15 * 60
FIRST_SYNC_DAYS = 60
RENEW_BEFORE = 5 * 60        # renew the access token when it has under five minutes left
STATE_MINUTES = 10

TOKENS = "withings_tokens"
LAST_UPDATE = "withings_last_update"
LAST_SYNC = "withings_last_sync"
ERROR = "withings_error"
STATE = "withings_state"
SYSTOLIC, DIASTOLIC, PULSE = 10, 9, 11            # Withings measure type numbers
BAD_TOKEN = 343                                   # Withings status: access token absent or invalid

_lock = threading.Lock()


class WithingsError(Exception):
    def __init__(self, message: str, status: int | None = None, reconnect: bool = False):
        super().__init__(message)
        self.status, self.reconnect = status, reconnect


class Withings:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str,
                 http: httpx.Client | None = None):
        self.client_id, self.client_secret, self.redirect_uri = client_id, client_secret, redirect_uri
        self.http = http or httpx.Client(timeout=20)

    # ------------------------------------------------------------------ talking to Withings
    def authorize_url(self, state: str) -> str:
        return AUTH_URL + "?" + urlencode({"response_type": "code", "client_id": self.client_id,
                                           "scope": SCOPE, "redirect_uri": self.redirect_uri, "state": state})

    def _call(self, url: str, data: Mapping[str, Any], token: str | None = None) -> dict[str, Any]:
        try:
            r = self.http.post(url, data=data, headers={"Authorization": f"Bearer {token}"} if token else None)
            r.raise_for_status()
            j = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise WithingsError("Could not reach Withings") from e
        status = j.get("status")
        if status not in (0, 100):                 # 100 means "worked, no data"
            raise WithingsError(f"Withings refused the request (status {status})", status,
                                reconnect=status in (BAD_TOKEN, 304, 342))
        return j.get("body") or {}

    def _tokens(self, data: Mapping[str, Any]) -> dict[str, Any]:
        body = self._call(TOKEN_URL, {"action": "requesttoken", "client_id": self.client_id,
                                      "client_secret": self.client_secret, **data})
        try:
            return {"access": body["access_token"], "refresh": body["refresh_token"],
                    "expires_at": int(time.time()) + int(body.get("expires_in") or 10800),
                    "userid": str(body.get("userid") or "")}
        except KeyError as e:
            raise WithingsError("Withings sent back an unexpected answer") from e

    def exchange(self, code: str) -> dict[str, Any]:
        return self._tokens({"grant_type": "authorization_code", "code": code,
                             "redirect_uri": self.redirect_uri})

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        try:
            return self._tokens({"grant_type": "refresh_token", "refresh_token": refresh_token})
        except WithingsError as e:
            if e.status is not None:               # Withings answered and said no: the person must reconnect
                e.reconnect = True
            raise

    def measures(self, access_token: str, **window: int) -> tuple[list[dict[str, Any]], int | None]:
        """Blood pressure readings in the window, and the update time to ask from next time."""
        readings: list[dict[str, Any]] = []
        updatetime: int | None = None
        offset = 0
        for _ in range(20):                        # a safety stop on paging
            data: dict[str, Any] = {"action": "getmeas", "meastypes": f"{SYSTOLIC},{DIASTOLIC},{PULSE}",
                                    "category": 1, **window}
            if offset:
                data["offset"] = offset
            body = self._call(MEASURE_URL, data, access_token)
            readings += parse_groups(body.get("measuregrps"))
            updatetime = body.get("updatetime") or updatetime
            if not body.get("more"):
                break
            offset = body.get("offset") or 0
            if not offset:
                break
        return readings, updatetime


def parse_groups(groups: Any) -> list[dict[str, Any]]:
    """One reading per group that has both numbers. Withings sends value and a power of ten."""
    out = []
    for g in groups or []:
        if not isinstance(g, dict) or g.get("category") == 2:      # 2 is a goal, not a measurement
            continue
        vals: dict[int, float] = {}
        for m in g.get("measures") or []:
            try:
                vals[int(m["type"])] = float(m["value"]) * 10 ** int(m.get("unit") or 0)
            except (KeyError, TypeError, ValueError):
                continue
        if SYSTOLIC in vals and DIASTOLIC in vals and isinstance(g.get("date"), (int, float)):
            out.append({"time": datetime.fromtimestamp(g["date"], timezone.utc),
                        "systolic": round(vals[SYSTOLIC]), "diastolic": round(vals[DIASTOLIC]),
                        "pulse": round(vals[PULSE]) if PULSE in vals else None})
    return out


# ---------------------------------------------------------------------- per person
def client_from_env(env: Mapping[str, str], redirect_uri: str,
                    http: httpx.Client | None = None) -> Withings | None:
    cid, secret = (env.get("WITHINGS_CLIENT_ID") or "").strip(), (env.get("WITHINGS_CLIENT_SECRET") or "").strip()
    return Withings(cid, secret, redirect_uri, http) if cid and secret else None


def connected(store: Store) -> bool:
    return bool(store.get_setting(TOKENS))


def begin(store: Store, client: Withings, now: float | None = None) -> str:
    """Remember a one-time state value for this person and return the Withings address to send them to."""
    import secrets
    state = secrets.token_urlsafe(24)
    store.set_setting(STATE, f"{state}|{int((now or time.time()) + STATE_MINUTES * 60)}")
    return client.authorize_url(state)


def check_state(store: Store, given: str, now: float | None = None) -> bool:
    import hmac
    raw = store.get_setting(STATE) or ""
    store.set_setting(STATE, None)                 # one use only
    state, _, exp = raw.partition("|")
    return bool(state and given and exp.isdigit() and int(exp) >= (now or time.time())
                and hmac.compare_digest(state, given))


def save_tokens(store: Store, tokens: Mapping[str, Any]) -> None:
    store.set_setting(TOKENS, json.dumps(dict(tokens)))


def disconnect(store: Store) -> None:
    for k in (TOKENS, LAST_UPDATE, LAST_SYNC, ERROR, STATE):
        store.set_setting(k, None)


def status(store: Store) -> dict[str, Any]:
    return {"connected": connected(store), "last_sync": store.get_setting(LAST_SYNC),
            "error": store.get_setting(ERROR)}


def sync(store: Store, client: Withings, now: float | None = None) -> dict[str, Any]:
    """Pull new readings for one person. Never raises: problems are kept in the person's settings."""
    with _lock:
        raw = store.get_setting(TOKENS)
        if not raw:
            return {"connected": False, "added": 0}
        now = now or time.time()
        try:
            tokens = json.loads(raw)
            if tokens.get("expires_at", 0) - RENEW_BEFORE < now:
                tokens = client.refresh(tokens["refresh"])
                save_tokens(store, tokens)                 # the old refresh token is replaced: keep the new one now
            last = store.get_setting(LAST_UPDATE)
            window = ({"lastupdate": int(last)} if last and last.isdigit()
                      else {"startdate": int(now) - FIRST_SYNC_DAYS * 86400, "enddate": int(now)})
            try:
                readings, updatetime = client.measures(tokens["access"], **window)
            except WithingsError as e:
                if e.status != BAD_TOKEN:
                    raise
                tokens = client.refresh(tokens["refresh"])       # the token was refused early: renew once and retry
                save_tokens(store, tokens)
                readings, updatetime = client.measures(tokens["access"], **window)
            added = sum(store.add_synced_bp(r["systolic"], r["diastolic"], r["pulse"], r["time"], "Withings")
                        for r in readings)
            if updatetime:
                store.set_setting(LAST_UPDATE, str(int(updatetime)))
            store.set_setting(LAST_SYNC, datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M"))
            store.set_setting(ERROR, None)
            return {"connected": True, "added": added, "seen": len(readings)}
        except WithingsError as e:
            log.warning("withings sync failed: %s", e)
            store.set_setting(ERROR, "Withings needs to be connected again" if e.reconnect else str(e))
            return {"connected": True, "added": 0, "error": store.get_setting(ERROR)}
        except (ValueError, KeyError, TypeError):
            log.warning("withings sync failed: unreadable saved tokens")
            store.set_setting(ERROR, "Withings needs to be connected again")
            return {"connected": True, "added": 0, "error": store.get_setting(ERROR)}


def start(get_stores: Callable[[], dict[str, Store]], client: Withings) -> threading.Thread:
    def loop() -> None:
        time.sleep(30)                             # let the app finish starting
        while True:
            try:
                for store in get_stores().values():
                    if connected(store):
                        sync(store, client)
            except Exception:
                log.exception("withings loop error")
            time.sleep(POLL_SECONDS)

    t = threading.Thread(target=loop, name="withings", daemon=True)
    t.start()
    return t
