"""Health MCP server: phone ingest endpoint + OAuth-protected MCP endpoint, one process."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from collections import defaultdict
from typing import Mapping
from urllib.parse import urlparse

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import mealie, tools
from .person import CURRENT, PersonalTools, personal
from .auth import OIDCVerifier
from .store import Store

log = logging.getLogger("health-mcp")
MAX_BODY = 25 * 1024 * 1024
FAIL_LIMIT = 20          # failed ingest attempts per client per window
FAIL_WINDOW = 300.0
KNOWN_KEYS = {"timestamp", "app_version", "steps", "heart_rate", "sleep", "weight", "blood_pressure",
              "nutrition", "exercise", "active_calories", "total_calories"}


class ConfigError(RuntimeError):
    pass


def _req(env: Mapping[str, str], key: str) -> str:
    v = (env.get(key) or "").strip()
    if not v:
        raise ConfigError(f"{key} is required")
    return v


class PersonRequired(PermissionError):
    """The signed-in person is not linked to anyone in MCP_USERS. The message is safe to show them."""


def parse_mcp_users(raw: str) -> dict[str, str]:
    """'khushal:abc|khushal@x.com,shivani:def' -> {'abc': 'khushal', 'khushal@x.com': 'khushal', 'def': 'shivani'}."""
    out: dict[str, str] = {}
    for part in raw.split(","):
        name, _, ids = part.strip().partition(":")
        name = name.strip().lower()
        if not name or not ids.strip():
            if part.strip():
                raise ConfigError("MCP_USERS entries look like username:sign-in-id (separate several ids with |)")
            continue
        for i in ids.split("|"):
            if i.strip():
                out[i.strip().lower()] = name
    return out


_STORES: dict[int, "object"] = {}


def shared_stores(env: Mapping[str, str], primary: Store):
    """One Stores object (a database per person) shared by the web app and the MCP tools."""
    from . import web
    if id(primary) not in _STORES:
        users = web.parse_users(env.get("WEB_USERS") or "default:Default")
        default = (env.get("WEB_DEFAULT_USER") or next(iter(users))).strip().lower()
        _STORES[id(primary)] = web.Stores(env.get("DATA_DIR", "/data"), default, env.get("TZ_NAME", "Europe/London"), primary)
    return _STORES[id(primary)]


class PersonStore:
    """Stands in for a Store in the MCP tools: each call goes to the database of whoever is signed in.
    With no MCP_USERS set, everyone gets the default person's data (the old behaviour)."""

    def __init__(self, stores, links: dict[str, str], known: set[str] | None = None) -> None:
        self._stores, self._links = stores, links
        self._known = known or {stores.default_user}

    def _person(self) -> str:
        asked = CURRENT.get()  # the `person` argument of the tool being called
        if asked:
            if asked not in self._known:
                raise PersonRequired(f"Unknown person {asked!r}. Use one of: {', '.join(sorted(self._known))}.")
            return asked
        if not self._links:
            return self._stores.default_user
        tok = get_access_token()
        claims = (tok.claims if tok else None) or {}
        ids = [str(v).lower() for v in (tok.subject if tok else None, *claims.values()) if v]
        for i in ids:
            if i in self._links:
                return self._links[i]
        shown = ", ".join(sorted(set(ids))[:3]) or "unknown"
        raise PersonRequired(f"Your sign-in ({shown}) is not linked to a person. Ask the owner to add it to MCP_USERS.")

    def __getattr__(self, name: str):
        return getattr(self._stores.get(self._person()), name)


def _web_default(env: Mapping[str, str]) -> str:
    from . import web
    users = web.parse_users(env.get("WEB_USERS") or "default:Default")
    return (env.get("WEB_DEFAULT_USER") or next(iter(users))).strip().lower()


def create(env: Mapping[str, str] = os.environ) -> tuple[FastMCP, Store, OIDCVerifier]:
    public_url = _req(env, "PUBLIC_URL").rstrip("/")
    ingest_token = _req(env, "INGEST_TOKEN")
    if len(ingest_token) < 24:
        raise ConfigError("INGEST_TOKEN must be at least 24 characters")
    issuer = _req(env, "OIDC_ISSUER")
    audience = _req(env, "OIDC_AUDIENCE")
    host_name = urlparse(public_url).netloc
    if not host_name:
        raise ConfigError("PUBLIC_URL must look like https://health.example.com")
    origin = f"{urlparse(public_url).scheme}://{host_name}"

    store = Store(os.path.join(env.get("DATA_DIR", "/data"), "health.db"),
                  tz=env.get("TZ_NAME", "Europe/London"))
    verifier = OIDCVerifier(
        issuer, audience,
        client_id=(env.get("OIDC_CLIENT_ID") or audience) or None,
        client_secret=env.get("OIDC_CLIENT_SECRET") or None,
        jwks_uri=env.get("OIDC_JWKS_URI") or None,
        introspection_endpoint=env.get("OIDC_INTROSPECTION_URL") or None,
        allowed_subjects={s.strip() for s in (env.get("OIDC_ALLOWED_SUBJECTS") or "").split(",") if s.strip()},
    )
    mcp = FastMCP(
        "health",
        instructions=("Personal health data from the user's Pixel Watch, Health Connect and manual logs. "
                      "Start with health_summary. If data looks missing, call data_status. "
                      "Report data and trends; do not diagnose. If Mealie tools are present, prefer a recipe's own nutrition over estimates. "
                      "Several people share this connector. Data tools take an optional `person` (for example khushal or shivani); "
                      "leave it empty only for the default person, and pass it on every call when someone else is asking."),
        host=env.get("HOST", "0.0.0.0"), port=int(env.get("PORT", "8000")),
        log_level="WARNING",  # also silences uvicorn's access log, which would print request URLs
        auth=AuthSettings(issuer_url=issuer, resource_server_url=f"{public_url}/mcp"),
        token_verifier=verifier,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host_name, "localhost:*", "127.0.0.1:*"],
            allowed_origins=[origin, "https://claude.ai", "https://claude.com"]),
    )
    links = parse_mcp_users(env.get("MCP_USERS") or "")
    stores = shared_stores(env, store)
    if links:
        unknown = set(links.values()) - set(__import__("app.web", fromlist=["parse_users"]).parse_users(env.get("WEB_USERS") or "default:Default"))
        if unknown:
            raise ConfigError(f"MCP_USERS names someone who is not in WEB_USERS: {sorted(unknown)}")
    else:
        log.info("MCP_USERS is not set: tools use the default person unless the caller passes `person`")
    from . import web as _web
    known = set(_web.parse_users(env.get("WEB_USERS") or "default:Default"))
    person_store = PersonStore(stores, links, known)
    tools.register(PersonalTools(mcp), person_store)  # type: ignore[arg-type]
    mealie_url = (env.get("MEALIE_URL") or "").strip()
    if mealie_url or env.get("MEALIE_API_TOKEN") or any(k.startswith("MEALIE_TOKEN_") for k in env):
        if urlparse(mealie_url).scheme not in ("http", "https") or not urlparse(mealie_url).netloc:
            raise ConfigError("MEALIE_URL must be an http or https address")
        try:
            clients = mealie.clients_from_env(env, known, _web_default(env))
        except ValueError as e:
            raise ConfigError(str(e)) from e
        mealie.register(PersonalTools(mcp), person_store, mealie.ByPerson(clients, person_store._person),  # type: ignore[arg-type]
                        tools._parse_when, tools.READ, tools.WRITE, tools.DELETE, personal)

    # A token in the URL ends up in proxy and access logs, so this is off unless asked for.
    allow_query_key = (env.get("ALLOW_QUERY_KEY") or "").lower() in ("1", "true", "yes")
    failures: dict[str, list[float]] = defaultdict(list)

    def client_key(request: Request) -> str:
        fwd = request.headers.get("x-forwarded-for", "")
        return fwd.split(",")[0].strip() or (request.client.host if request.client else "?")

    def authorised(request: Request) -> bool:
        supplied = request.headers.get("x-api-key", "")
        if not supplied:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                supplied = auth[7:].strip()
        if not supplied and allow_query_key:
            supplied = request.query_params.get("key", "")
        return hmac.compare_digest(supplied.encode(), ingest_token.encode())

    @mcp.custom_route("/ingest", methods=["POST"])
    async def ingest(request: Request) -> Response:
        who, now = client_key(request), time.monotonic()
        failures[who] = [t for t in failures[who] if now - t < FAIL_WINDOW]
        if len(failures[who]) >= FAIL_LIMIT:
            return JSONResponse({"error": "too many failed attempts"}, status_code=429)
        if not authorised(request):
            failures[who].append(now)
            log.warning("ingest: rejected unauthenticated request from %s", who)
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            if int(request.headers.get("content-length") or 0) > MAX_BODY:
                return JSONResponse({"error": "payload too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"error": "bad content-length"}, status_code=400)
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({"error": "payload too large"}, status_code=413)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "expected a JSON object"}, status_code=400)
        counts = await asyncio.to_thread(store.ingest, payload)
        # stored/received per type, plus any top-level key we do not recognise, so a renamed
        # or new data type in the sync app shows up in the logs instead of vanishing silently
        unknown = sorted(set(payload) - KNOWN_KEYS)
        log.info("ingest: %s%s", {k: f'{v["stored"]}/{v["received"]}' for k, v in counts.items()},
                 f" unrecognised keys: {unknown}" if unknown else "")
        return JSONResponse({"ok": True, "counts": counts})

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True})

    async def as_metadata(request: Request) -> Response:
        """Some MCP clients look for authorization-server metadata on the MCP host itself."""
        try:
            return JSONResponse(await verifier.discovery())
        except Exception:
            log.exception("could not fetch OIDC discovery")
            return JSONResponse({"error": "issuer unavailable"}, status_code=502)

    mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])(as_metadata)
    mcp.custom_route("/.well-known/openid-configuration", methods=["GET"])(as_metadata)
    return mcp, store, verifier


def start_web(env: Mapping[str, str], store: Store) -> None:
    """Run the wellness web app on its own port in a background thread (opt-in)."""
    import threading

    import uvicorn

    from . import web

    app = web.build(env, primary=store, stores=shared_stores(env, store))
    if app is None:
        return
    cfg = uvicorn.Config(app, host=env.get("HOST", "0.0.0.0"), port=int(env.get("WEB_PORT", "8001")),
                         log_level="warning", proxy_headers=True, forwarded_allow_ips="*")
    threading.Thread(target=uvicorn.Server(cfg).run, name="web", daemon=True).start()
    from . import reminders
    stores, users = app.state.stores, app.state.users
    reminders.start(lambda: {u: stores.get(u) for u in users}, reminders.make_notifier(app.state.ntfy_base))
    log.info("web app listening on port %s", cfg.port)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        mcp, store, _ = create()
        start_web(os.environ, store)
    except (ConfigError, RuntimeError) as e:
        raise SystemExit(f"Configuration error: {e}") from e
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
