"""Wellness web app: Authelia (OIDC) login, one database per person, server-rendered pages.

Safety properties:
  * Every page and action needs a valid signed session cookie. There is no unauthenticated mode.
  * Only usernames listed in WEB_USERS can sign in. Each name maps to its own database file,
    so one person's session can never open another person's data.
  * Forms carry a per-session CSRF token and the cookie is SameSite=Lax.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlencode, urlparse

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from pathlib import Path

from . import apple, pages, reminders, views
from .auth import OIDCVerifier
from .store import Store
from .tools import _parse_when

log = logging.getLogger("health-mcp.web")
MAX_UPLOAD = 25 * 1024 * 1024
SESSION_COOKIE = "wellness_session"
LOGIN_COOKIE = "wellness_login"
SESSION_SECONDS = 7 * 86400
LOGIN_SECONDS = 600
CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; manifest-src 'self'; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
STATIC = Path(__file__).parent / "static"
MANIFEST = {"name": "Wellness", "short_name": "Wellness", "start_url": "/", "scope": "/", "display": "standalone",
            "background_color": "#F1F5F8", "theme_color": "#0B7A75", "description": "Your health at a glance",
            "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                      {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                      {"src": "/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]}
PUBLIC_ICONS = {"/icon-192.png": "icon-192.png", "/icon-512.png": "icon-512.png",
                "/icon-maskable-512.png": "icon-maskable-512.png", "/apple-touch-icon.png": "apple-touch-icon.png"}


class WebConfigError(RuntimeError):
    pass


def parse_users(raw: str) -> dict[str, str]:
    """'khushal:Khushal,shivani:Shivani' -> {'khushal': 'Khushal', 'shivani': 'Shivani'}."""
    users: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, display = part.partition(":")
        name = name.strip().lower()
        if not name or not all(c.isalnum() or c in "-_" for c in name):
            raise WebConfigError(f"WEB_USERS: bad username {name!r}")
        users[name] = display.strip() or name.title()
    if not users:
        raise WebConfigError("WEB_USERS needs at least one username")
    return users


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Signer:
    def __init__(self, secret: str) -> None:
        if len(secret) < 32:
            raise WebConfigError("WEB_SESSION_SECRET must be at least 32 characters")
        self._key = secret.encode()

    def dumps(self, data: dict[str, Any]) -> str:
        body = _b64(json.dumps(data, separators=(",", ":")).encode())
        sig = _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())
        return f"{body}.{sig}"

    def loads(self, token: str | None) -> dict[str, Any] | None:
        if not token or token.count(".") != 1:
            return None
        body, sig = token.split(".")
        good = _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        try:
            data = json.loads(_unb64(body))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict) or float(data.get("exp", 0)) < time.time():
            return None
        return data


class Stores:
    """One Store per person. The default person keeps the original database file."""

    def __init__(self, data_dir: str, default_user: str, tz: str, primary: Store | None = None) -> None:
        self.data_dir, self.default_user, self.tz = data_dir, default_user, tz
        self._stores: dict[str, Store] = {}
        if primary is not None:
            self._stores[default_user] = primary

    def get(self, user: str) -> Store:
        if user not in self._stores:
            if user == self.default_user:
                path = os.path.join(self.data_dir, "health.db")
            else:
                path = os.path.join(self.data_dir, "users", f"{user}.db")
            self._stores[user] = Store(path, tz=self.tz)
        return self._stores[user]


def create_app(env: Mapping[str, str], stores: Stores, users: dict[str, str],
               verifier: OIDCVerifier, http: httpx.AsyncClient | None = None,
               notify: reminders.Notifier | None = None) -> Starlette:
    public = (env.get("WEB_PUBLIC_URL") or "").rstrip("/")
    p = urlparse(public)
    if p.scheme != "https" or not p.netloc:
        raise WebConfigError("WEB_PUBLIC_URL must look like https://wellness.example.com")
    client_id = (env.get("WEB_OIDC_CLIENT_ID") or "").strip()
    client_secret = (env.get("WEB_OIDC_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        raise WebConfigError("WEB_OIDC_CLIENT_ID and WEB_OIDC_CLIENT_SECRET are required")
    signer = Signer(env.get("WEB_SESSION_SECRET", ""))
    redirect_uri = f"{public}/callback"
    client = http or httpx.AsyncClient(timeout=10)
    ntfy_base = (env.get("NTFY_BASE") or "https://ntfy.sh").rstrip("/")
    send = notify or reminders.make_notifier(ntfy_base)
    allowed_hosts = {p.hostname or "", "localhost", "127.0.0.1", "testserver"}

    def headers(resp: Response) -> Response:
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        # "no-referrer" makes browsers send "Origin: null" on form posts, which the Origin check refuses.
        # "same-origin" sends the real origin to this site only and nothing to other sites.
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def set_cookie(resp: Response, name: str, value: str, max_age: int) -> None:
        resp.set_cookie(name, value, max_age=max_age, httponly=True, secure=True, samesite="lax", path="/")

    def session_of(request: Request) -> dict[str, Any] | None:
        s = signer.loads(request.cookies.get(SESSION_COOKIE))
        if not s or s.get("u") not in users or not s.get("csrf"):
            return None
        return s

    def bad_host(request: Request) -> bool:
        return (request.headers.get("host") or "").rsplit(":", 1)[0] not in allowed_hosts

    Handler = Callable[[Request, dict[str, Any], Store], Awaitable[Response]]

    def authed(handler: Handler, *, post: bool = False) -> Callable[[Request], Awaitable[Response]]:
        async def wrapper(request: Request) -> Response:
            if bad_host(request):
                return headers(Response("bad host", status_code=400))
            s = session_of(request)
            if s is None:
                if post:
                    return headers(Response("sign in required", status_code=401))
                return headers(RedirectResponse("/login", status_code=303))
            if post:
                try:
                    if int(request.headers.get("content-length") or 0) > MAX_UPLOAD + 1024 * 1024:
                        return headers(Response("too large", status_code=413))
                except ValueError:
                    return headers(Response("bad content-length", status_code=400))
                origin = request.headers.get("origin")
                if origin and origin != f"{p.scheme}://{p.netloc}":
                    return headers(Response("bad origin", status_code=403))
                form = await request.form()
                if not hmac.compare_digest(str(form.get("csrf", "")), s["csrf"]):
                    return headers(Response("bad csrf token", status_code=403))
                request.state.form = form
            return headers(await handler(request, s, stores.get(s["u"])))
        return wrapper

    def page(title: str, body: str, s: dict[str, Any], active: str, request: Request,
             back: tuple[str, str] | None = None, tint: str | None = None) -> HTMLResponse:
        flash = request.query_params.get("ok") or request.query_params.get("err") or ""
        return HTMLResponse(pages.layout(title, body, name=users[s["u"]], active=active, csrf=s["csrf"],
                                         flash=flash[:200], flash_err=bool(request.query_params.get("err")),
                                         back=back, tint=tint))

    async def today(request: Request, s: dict[str, Any], store: Store) -> Response:
        return page("Today", views.today_page(store, users[s["u"]]), s, "/", request)

    def detail(title: str, path: str, tint: str, render: Callable[[Store, str, int], str], default: int = 7):
        async def handler(request: Request, s: dict[str, Any], store: Store) -> Response:
            try:
                d = int(request.query_params.get("d", default))
            except ValueError:
                d = default
            d = d if d in views.RANGES else default
            return page(title, render(store, s["csrf"], d), s, "/", request, back=("/", "Today"), tint=tint)
        return handler

    async def log_get(request: Request, s: dict[str, Any], store: Store) -> Response:
        return page("Quick log", pages.log_page(store, s["csrf"]), s, "/log", request)

    def back(msg: str, err: bool = False, to: str = "/log") -> Response:
        return RedirectResponse(to + "?" + urlencode({"err" if err else "ok": msg}), status_code=303)

    def num(form: Any, key: str, cast: Callable[[str], Any] = float) -> Any:
        v = str(form.get(key, "")).strip()
        return cast(v) if v else None

    def when_of(form: Any, store: Store) -> Any:
        w = str(form.get("when", "")).strip()
        return _parse_when(store, w) if w else None

    async def log_bp(request: Request, s: dict[str, Any], store: Store) -> Response:
        f = request.state.form
        try:
            r = store.log_blood_pressure(num(f, "systolic", int), num(f, "diastolic", int),
                                         num(f, "pulse", int), when_of(f, store), str(f.get("note", "")).strip() or None)
        except (ValueError, TypeError) as e:
            return back(f"Could not save: {e}", True)
        return back(f'Saved {r["systolic"]}/{r["diastolic"]}')

    async def log_weight(request: Request, s: dict[str, Any], store: Store) -> Response:
        f = request.state.form
        try:
            r = store.log_weight(num(f, "kg"), when_of(f, store))
        except (ValueError, TypeError) as e:
            return back(f"Could not save: {e}", True)
        return back(f'Saved {r["kg"]} kg')

    async def log_meal(request: Request, s: dict[str, Any], store: Store) -> Response:
        f = request.state.form
        try:
            r = store.log_meal(str(f.get("name", "")), num(f, "kcal"), num(f, "protein"), None, None, None,
                               None, num(f, "salt"), when_of(f, store))
        except (ValueError, TypeError) as e:
            return back(f"Could not save: {e}", True)
        return back(f'Saved {r["name"]}')

    async def log_fasting(request: Request, s: dict[str, Any], store: Store) -> Response:
        on = str(request.state.form.get("on")) == "1"
        store.set_fasting(store.local(store.now()).date(), on)
        return back("Marked as a fasting day" if on else "Fasting day removed")

    async def log_delete(request: Request, s: dict[str, Any], store: Store) -> Response:
        f = request.state.form
        try:
            ok = store.delete_manual(str(f.get("kind", "")), int(str(f.get("id", ""))))
        except ValueError as e:
            return back(f"Could not delete: {e}", True)
        return RedirectResponse({"blood_pressure": "/bp", "weight": "/weight", "nutrition": "/food"}.get(str(f.get("kind")), "/log") +
                                "?" + urlencode({"ok": "Deleted"} if ok else {"err": "Nothing deleted"}),
                                status_code=303)


    async def gp(request: Request, s: dict[str, Any], store: Store) -> Response:
        try:
            days = int(request.query_params.get("days", "7"))
        except ValueError:
            days = 7
        return page("GP report", pages.gp_page(store, users[s["u"]], days), s, "/gp", request)

    async def settings_get(request: Request, s: dict[str, Any], store: Store) -> Response:
        return page("Settings", pages.settings_page(store, s["csrf"], ntfy_base), s, "/settings", request)

    async def settings_reminders(request: Request, s: dict[str, Any], store: Store) -> Response:
        f = request.state.form
        times = {}
        for slot in reminders.SLOTS:
            v = str(f.get(slot, "")).strip()
            try:
                h, m = v.split(":")
                if not (0 <= int(h) < 24 and 0 <= int(m) < 60):
                    raise ValueError
            except ValueError:
                return back(f"The {slot} time is not valid", True, "/settings")
            times[slot] = f"{int(h):02d}:{int(m):02d}"
        on = str(f.get("enabled")) == "1"
        store.set_setting("reminders_enabled", "1" if on else "0")
        for slot, v in times.items():
            store.set_setting(f"reminder_{slot}", v)
        if on and not store.get_setting("reminder_topic"):
            store.set_setting("reminder_topic", reminders.new_topic())
        return back("Reminders saved", False, "/settings")

    async def settings_test(request: Request, s: dict[str, Any], store: Store) -> Response:
        topic = store.get_setting("reminder_topic")
        if not topic:
            return back("Turn reminders on first", True, "/settings")
        try:
            await asyncio.to_thread(send, topic, "Wellness", "This is a test reminder. It worked.")
        except Exception:
            log.warning("test reminder failed")
            return back("Could not reach the notification server", True, "/settings")
        return back("Test sent. Check your phone.", False, "/settings")

    async def settings_context(request: Request, s: dict[str, Any], store: Store) -> Response:
        text = str(request.state.form.get("context", "")).strip()[:600]
        store.set_setting("gp_context", text or None)
        return back("Note saved", False, "/settings")

    async def settings_goals(request: Request, s: dict[str, Any], store: Store) -> Response:
        f = request.state.form
        try:
            steps, sleep = int(str(f.get("steps_goal", "")).strip()), float(str(f.get("sleep_goal", "")).strip())
        except ValueError:
            return back("Goals must be numbers", True, "/settings")
        if not (1000 <= steps <= 50000) or not (4 <= sleep <= 12):
            return back("Steps must be 1,000 to 50,000 and sleep 4 to 12 hours", True, "/settings")
        store.set_setting("steps_goal", str(steps))
        store.set_setting("sleep_goal", f"{sleep:g}")
        return back("Goals saved", False, "/settings")

    async def public_file(request: Request) -> Response:
        if request.url.path == "/manifest.webmanifest":
            resp = JSONResponse(MANIFEST, media_type="application/manifest+json")
        else:
            f = STATIC / PUBLIC_ICONS[request.url.path]
            resp = Response(f.read_bytes(), media_type="image/png")
        resp.headers["Cache-Control"] = "public, max-age=86400"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    async def import_get(request: Request, s: dict[str, Any], store: Store) -> Response:
        return page("Import", pages.import_page(store, s["csrf"]), s, "/settings", request)

    async def import_post(request: Request, s: dict[str, Any], store: Store) -> Response:
        up = request.state.form.get("file")
        if up is None or not hasattr(up, "read"):
            return back("Choose a file first", True, "/import")
        raw = await up.read(MAX_UPLOAD + 1)
        if len(raw) > MAX_UPLOAD:
            return back("That file is too large", True, "/import")
        try:
            data = apple.load_upload(raw)
            payload, heart = apple.convert(data, store.tz)
            counts = await asyncio.to_thread(store.ingest, payload)
            days = await asyncio.to_thread(store.import_daily_heart, heart)
        except apple.ImportError_ as e:
            return back(str(e), True, "/import")
        summary = apple.summarise(counts, days)
        store.set_setting("last_import", f'{store.local(store.now()).strftime("%-d %b %Y %H:%M")}: {summary}')
        return back(summary, False, "/settings")

    # ------------------------------------------------------------------ login
    async def login(request: Request) -> Response:
        if bad_host(request):
            return headers(Response("bad host", status_code=400))
        try:
            disc = await verifier.discovery()
        except Exception:
            log.exception("OIDC discovery failed")
            return headers(HTMLResponse(pages.login_page("Sign-in is unavailable right now."), status_code=502))
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        verifier_code = secrets.token_urlsafe(48)  # 64 chars; RFC 7636 requires 43 to 128
        challenge = _b64(hashlib.sha256(verifier_code.encode()).digest())
        url = disc["authorization_endpoint"] + "?" + urlencode({
            "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
            "scope": "openid profile", "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256"})
        resp = RedirectResponse(url, status_code=303)
        set_cookie(resp, LOGIN_COOKIE, signer.dumps({"s": state, "n": nonce, "v": verifier_code,
                                                     "exp": time.time() + LOGIN_SECONDS}), LOGIN_SECONDS)
        return headers(resp)

    async def callback(request: Request) -> Response:
        if bad_host(request):
            return headers(Response("bad host", status_code=400))
        pending = signer.loads(request.cookies.get(LOGIN_COOKIE))
        code, state = request.query_params.get("code"), request.query_params.get("state", "")
        if not pending or not code or not hmac.compare_digest(state, pending["s"]):
            return headers(HTMLResponse(pages.login_page("Sign-in expired. Please try again."), status_code=400))
        try:
            disc = await verifier.discovery()
            r = await client.post(disc["token_endpoint"], data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "client_id": client_id, "client_secret": client_secret, "code_verifier": pending["v"]})
            if r.status_code >= 400:
                log.error("token endpoint refused the code: %s %s", r.status_code, r.text[:300])
            r.raise_for_status()
            tokens = r.json()
            claims = await verifier.claims_from_jwt(tokens.get("id_token", ""))
            if claims and not claims.get("preferred_username") and disc.get("userinfo_endpoint"):
                # Authelia 4.39+ keeps profile claims out of the ID token unless a claims policy asks
                # for them, so read them from the userinfo endpoint and check they are the same person.
                ui = await client.get(disc["userinfo_endpoint"],
                                      headers={"Authorization": f"Bearer {tokens.get('access_token', '')}"})
                ui.raise_for_status()
                info = ui.json()
                if str(info.get("sub", "")) == str(claims.get("sub", "?")):
                    claims = {**claims, "preferred_username": info.get("preferred_username")}
        except Exception:
            log.exception("OIDC code exchange failed")
            return headers(HTMLResponse(pages.login_page("Sign-in failed."), status_code=502))
        if not claims or not hmac.compare_digest(str(claims.get("nonce", "")), pending["n"]):
            return headers(HTMLResponse(pages.login_page("Sign-in failed."), status_code=401))
        user = str(claims.get("preferred_username") or "").lower()
        if user not in users:
            log.warning("sign-in refused for an account that is not in WEB_USERS")
            return headers(HTMLResponse(pages.login_page("This account does not have access."), status_code=403))
        resp = RedirectResponse("/", status_code=303)
        set_cookie(resp, SESSION_COOKIE, signer.dumps({"u": user, "csrf": secrets.token_urlsafe(24),
                                                       "exp": time.time() + SESSION_SECONDS}), SESSION_SECONDS)
        resp.delete_cookie(LOGIN_COOKIE, path="/")
        return headers(resp)

    async def logout(request: Request, s: dict[str, Any], store: Store) -> Response:
        resp = HTMLResponse(pages.login_page("You are signed out."))
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True})

    return Starlette(routes=[
        Route("/", authed(today)), Route("/log", authed(log_get)),
        Route("/sleep", authed(detail("Sleep", "/sleep", "--c-sleep", lambda st, c, d: views.sleep_page(st, d)))),
        Route("/steps", authed(detail("Steps", "/steps", "--c-steps", lambda st, c, d: views.steps_page(st, d)))),
        Route("/heart", authed(detail("Heart rate", "/heart", "--c-heart", lambda st, c, d: views.heart_page(st, d)))),
        Route("/bp", authed(detail("Blood pressure", "/bp", "--c-heart", views.bp_page))),
        Route("/weight", authed(detail("Weight", "/weight", "--c-weight", views.weight_page, 30))),
        Route("/food", authed(detail("Food", "/food", "--c-food", views.food_page))),
        Route("/activity", authed(detail("Workouts", "/activity", "--c-work", lambda st, c, d: views.activity_page(st, d)))),
        Route("/manifest.webmanifest", public_file), *[Route(p_, public_file) for p_ in PUBLIC_ICONS],
        Route("/settings/goals", authed(settings_goals, post=True), methods=["POST"]),
        Route("/gp", authed(gp)), Route("/settings", authed(settings_get)), Route("/import", authed(import_get)),
        Route("/settings/reminders", authed(settings_reminders, post=True), methods=["POST"]),
        Route("/settings/test", authed(settings_test, post=True), methods=["POST"]),
        Route("/settings/context", authed(settings_context, post=True), methods=["POST"]),
        Route("/import", authed(import_post, post=True), methods=["POST"]),
        Route("/log/bp", authed(log_bp, post=True), methods=["POST"]),
        Route("/log/weight", authed(log_weight, post=True), methods=["POST"]),
        Route("/log/meal", authed(log_meal, post=True), methods=["POST"]),
        Route("/log/fasting", authed(log_fasting, post=True), methods=["POST"]),
        Route("/log/delete", authed(log_delete, post=True), methods=["POST"]),
        Route("/logout", authed(logout, post=True), methods=["POST"]),
        Route("/login", login), Route("/callback", callback), Route("/healthz", healthz),
    ])


def build(env: Mapping[str, str], primary: Store | None = None) -> Starlette | None:
    """The web app, or None when WEB_PUBLIC_URL is not set (the feature is opt-in)."""
    if not (env.get("WEB_PUBLIC_URL") or "").strip():
        return None
    users = parse_users(env.get("WEB_USERS", ""))
    default = (env.get("WEB_DEFAULT_USER") or next(iter(users))).strip().lower()
    if default not in users:
        raise WebConfigError("WEB_DEFAULT_USER must be one of WEB_USERS")
    issuer = (env.get("OIDC_ISSUER") or "").strip()
    client_id = (env.get("WEB_OIDC_CLIENT_ID") or "").strip()
    if not issuer or not client_id:
        raise WebConfigError("OIDC_ISSUER and WEB_OIDC_CLIENT_ID are required for the web app")
    verifier = OIDCVerifier(issuer, client_id, client_id=client_id,
                            client_secret=env.get("WEB_OIDC_CLIENT_SECRET") or None)
    stores = Stores(env.get("DATA_DIR", "/data"), default, env.get("TZ_NAME", "Europe/London"), primary)
    app = create_app(env, stores, users, verifier)
    app.state.stores, app.state.users = stores, users
    app.state.ntfy_base = (env.get("NTFY_BASE") or "https://ntfy.sh").rstrip("/")
    return app
