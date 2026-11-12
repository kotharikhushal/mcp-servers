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
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlencode, urlparse

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from pathlib import Path

from . import analysis, apple, chat as chat_mod, mealie as mealie_mod, pages, reminders, views
from .auth import OIDCVerifier
from .store import Store
from .tools import _parse_when

log = logging.getLogger("health-mcp.web")
MAX_UPLOAD = 25 * 1024 * 1024
AI_MIN_GAP_MIN = 60  # one local-model analysis per person per hour
SESSION_COOKIE = "wellness_session"
LOGIN_COOKIE = "wellness_login"
SESSION_SECONDS = 7 * 86400
LOGIN_SECONDS = 600
CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; manifest-src 'self'; "
       "font-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
STATIC = Path(__file__).parent / "static"
MANIFEST = {"name": "Wellness", "short_name": "Wellness", "start_url": "/", "scope": "/", "display": "standalone",
            "background_color": "#F1F5F8", "theme_color": "#0B7A75", "description": "Your health at a glance",
            "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                      {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                      {"src": "/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]}
PUBLIC_ICONS = {"/fonts/bricolage-grotesque.woff2": "bricolage-grotesque.woff2", "/icon-192.png": "icon-192.png", "/icon-512.png": "icon-512.png",
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
               notify: reminders.Notifier | None = None, mealie: Any = None) -> Starlette:
    ollama_url = (env.get("OLLAMA_URL") or "").strip()
    ollama_model = (env.get("OLLAMA_MODEL") or "gemma4:12b").strip()
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
                                         back=back, tint=tint, mode=stores.get(s["u"]).get_setting("mode") or "auto",
                                         palette=stores.get(s["u"]).get_setting("palette") or "tide", chat=bool(ollama_url)))

    menu_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}

    def load_menu(store: Store, user: str) -> list[dict[str, Any]]:
        """Today's Mealie plan (cached for 30 seconds so the home screen stays quick)."""
        day = store.local(store.now()).date()
        hit = menu_cache.get((user, day.isoformat()))
        if hit and time.monotonic() - hit[0] < 30:
            return hit[1]
        menu = mealie.day_menu(day)
        menu_cache[(user, day.isoformat())] = (time.monotonic(), menu)
        return menu

    async def today(request: Request, s: dict[str, Any], store: Store) -> Response:
        menu, err = None, False
        if mealie is not None:
            try:
                menu = await asyncio.to_thread(load_menu, store, s["u"])
            except mealie_mod.MealieError:
                err = True
        return page("Today", views.today_page(store, users[s["u"]], menu, err, s["csrf"], bool(ollama_url)), s, "/", request)

    async def meals_eat(request: Request, s: dict[str, Any], store: Store) -> Response:
        if mealie is None:
            return back("Meal plan is not connected", True, "/")
        eid = str(request.state.form.get("entry_id", ""))
        try:
            menu = await asyncio.to_thread(load_menu, store, s["u"])
        except mealie_mod.MealieError as e:
            return back(str(e), True, "/")
        m = next((x for x in menu if str(x["id"]) == eid), None)
        if not m or not m.get("_rec"):
            return back("That meal is no longer in today's plan", True, "/")
        name = m["_rec"]["name"]
        if any(e["name"].startswith(f"{name} x") for e in store.food_entries(1)
               if e["_day"] == store.local(store.now()).date()):
            return back(f"{name} is already logged", False, "/")
        res = await asyncio.to_thread(mealie_mod.log_recipe_nutrition, store, m["_rec"], 1.0, None)
        if not res.get("logged"):
            return back(res.get("reason", "Could not log that meal"), True, "/")
        return back(f'Logged {name}, {res["kcal"]} kcal', False, "/")

    async def swap_get(request: Request, s: dict[str, Any], store: Store) -> Response:
        eid = request.query_params.get("entry_id", "")
        q = request.query_params.get("q", "")[:60]
        try:
            menu = await asyncio.to_thread(load_menu, store, s["u"]) if mealie is not None else []
            m = next((x for x in menu if str(x["id"]) == eid), None)
            if not m:
                return back("That meal is no longer in today's plan", True, "/")
            opts = await asyncio.to_thread(mealie.swap_options, (m.get("per_serving") or {}).get("kcal"), q, m.get("slug"))
        except mealie_mod.MealieError as e:
            return back(str(e), True, "/")
        return page("Swap meal", views.swap_page(store, s["csrf"], m, opts, q), s, "/", request, back=("/", "Today"), tint="--c-food")

    async def swap_post(request: Request, s: dict[str, Any], store: Store) -> Response:
        f = request.state.form
        if mealie is None:
            return back("Meal plan is not connected", True, "/")
        try:
            res = await asyncio.to_thread(mealie.swap_plan_entry, str(f.get("entry_id", "")), str(f.get("slug", "")))
        except mealie_mod.MealieError as e:
            return back(str(e), True, "/")
        menu_cache.clear()
        return back(f'Swapped to {res["recipe"]}', False, "/")

    async def analysis_refresh(request: Request, s: dict[str, Any], store: Store) -> Response:
        if not ollama_url:
            return back("The local model is not set up", True, "/")
        now = store.now()
        last = store.get_setting("ai_analysis_try")
        try:
            wait_until = datetime.fromisoformat(last) + timedelta(minutes=AI_MIN_GAP_MIN) if last else None
        except ValueError:
            wait_until = None
        if wait_until and now < wait_until:
            return back(f"Refreshed recently. Try again after {store.local(wait_until).strftime('%H:%M')}", True, "/")
        store.set_setting("ai_analysis_try", now.isoformat())   # set first, so a double tap cannot start two runs
        facts = analysis.build_facts(store)
        try:
            async with httpx.AsyncClient(timeout=180) as c:
                res = await analysis.generate(c, ollama_url, ollama_model, facts)
        except analysis.AnalysisError as e:
            log.warning("analysis refresh failed for %s: %s", s["u"], e)
            return back(str(e), True, "/")
        store.set_setting("ai_analysis", json.dumps({**res, "at": store.now().isoformat(), "source": ollama_model}))
        return back("Analysis refreshed", False, "/")

    async def chat_get(request: Request, s: dict[str, Any], store: Store) -> Response:
        return page("Chat", views.chat_page(store, s["csrf"], chat_mod.load_history(store), chat_mod.load_pending(store),
                                            bool(ollama_url), ollama_model), s, "/chat", request)

    async def chat_send(request: Request, s: dict[str, Any], store: Store) -> Response:
        if not ollama_url:
            return back("The local model is not set up", True, "/chat")
        text = " ".join(str(request.state.form.get("text", "")).split())[:chat_mod.MSG_MAX]
        if not text:
            return back("Type a message first", True, "/chat")
        if chat_mod.load_pending(store):
            return back("Confirm or cancel the saved suggestion first", True, "/chat")
        history = chat_mod.load_history(store)
        menu = None
        if mealie is not None:
            try:
                menu = await asyncio.to_thread(load_menu, store, s["u"])
            except mealie_mod.MealieError:
                menu = None
        facts = chat_mod.chat_facts(store, menu)
        try:
            async with httpx.AsyncClient(timeout=180) as c:
                reply, action = await chat_mod.respond(c, ollama_url, ollama_model, facts, history, text)
            if action:
                action = await asyncio.to_thread(chat_mod.resolve, mealie, action)
        except (chat_mod.ChatError, mealie_mod.MealieError) as e:
            log.warning("chat failed for %s: %s", s["u"], e)
            chat_mod.add_message(store, "user", text)
            chat_mod.add_message(store, "note", str(e)[:300])
            return back(str(e)[:150], True, "/chat")
        chat_mod.add_message(store, "user", text)
        chat_mod.add_message(store, "bot", reply)
        if action:
            chat_mod.set_pending(store, action)
        return RedirectResponse("/chat", status_code=303)

    async def chat_confirm(request: Request, s: dict[str, Any], store: Store) -> Response:
        p = chat_mod.load_pending(store)
        if not p or not hmac.compare_digest(str(request.state.form.get("id", "")), str(p["id"])):
            return back("That suggestion is no longer waiting", True, "/chat")
        chat_mod.clear_pending(store)       # cleared first, so a double tap cannot save twice
        try:
            msg = await asyncio.to_thread(chat_mod.execute, store, mealie, p)
        except (mealie_mod.MealieError, ValueError) as e:
            chat_mod.add_message(store, "note", f"Not saved: {e}"[:300])
            return back(str(e)[:150], True, "/chat")
        chat_mod.add_message(store, "note", msg)
        menu_cache.clear()
        return back(msg, False, "/chat")

    async def chat_cancel(request: Request, s: dict[str, Any], store: Store) -> Response:
        p = chat_mod.load_pending(store)
        if p and hmac.compare_digest(str(request.state.form.get("id", "")), str(p["id"])):
            chat_mod.clear_pending(store)
            chat_mod.add_message(store, "note", "Cancelled, nothing saved")
        return RedirectResponse("/chat", status_code=303)

    async def chat_clear(request: Request, s: dict[str, Any], store: Store) -> Response:
        chat_mod.save_history(store, [])
        chat_mod.clear_pending(store)
        return back("Chat history cleared", False, "/chat")

    async def meals_skip(request: Request, s: dict[str, Any], store: Store) -> Response:
        f = request.state.form
        eid = str(f.get("entry_id", ""))
        if not eid.replace("-", "").replace("_", "").isalnum():
            return back("Bad meal", True, "/")
        key = f"skip:{store.local(store.now()).date().isoformat()}"
        cur = {x for x in (store.get_setting(key) or "").split(",") if x}
        if str(f.get("on")) == "0":
            cur.discard(eid)
        else:
            cur.add(eid)
        store.set_setting(key, ",".join(sorted(cur)) or None)
        return back("Skipped" if str(f.get("on")) != "0" else "Skip removed", False, "/")

    async def fasting_days_post(request: Request, s: dict[str, Any], store: Store) -> Response:
        vals = request.state.form.getlist("wd") if hasattr(request.state.form, "getlist") else []
        store.set_fasting_weekdays({int(v) for v in vals if str(v).isdigit() and 0 <= int(v) <= 6})
        return back("Fasting days saved", False, "/settings")

    async def appearance_post(request: Request, s: dict[str, Any], store: Store) -> Response:
        f = request.state.form
        mode, palette = str(f.get("mode", "")), str(f.get("palette", ""))
        if mode not in pages.MODES or palette not in pages.PALETTES:
            return back("Choose a mode and a colour theme", True, "/settings")
        store.set_setting("mode", None if mode == "auto" else mode)
        store.set_setting("palette", None if palette == "tide" else palette)
        return back("Appearance saved", False, "/settings")

    async def goal_weight(request: Request, s: dict[str, Any], store: Store) -> Response:
        raw = str(request.state.form.get("goal_weight", "")).strip()
        if not raw:
            store.set_setting("goal_weight", None)
            return back("Goal weight removed", False, "/weight")
        lo, hi = pages.NUTRITION_GOALS["goal_weight"]
        try:
            v = float(raw)
        except ValueError:
            return back("Goal weight must be a number", True, "/weight")
        if not lo <= v <= hi:
            return back(f"Goal weight must be between {lo} and {hi} kg", True, "/weight")
        store.set_setting("goal_weight", f"{v:g}")
        return back(f"Goal weight set to {v:g} kg", False, "/weight")

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
        return page("Quick log", pages.log_page(store, s["csrf"], request.query_params.get("t", "meal")), s, "/log", request)

    def back(msg: str, err: bool = False, to: str = "/log") -> Response:
        return RedirectResponse(to + ("&" if "?" in to else "?") + urlencode({"err" if err else "ok": msg}), status_code=303)

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
            return back(f"Could not save: {e}", True, "/log?t=bp")
        return back(f'Saved {r["systolic"]}/{r["diastolic"]}', False, "/log?t=bp")

    async def log_weight(request: Request, s: dict[str, Any], store: Store) -> Response:
        f = request.state.form
        try:
            r = store.log_weight(num(f, "kg"), when_of(f, store))
        except (ValueError, TypeError) as e:
            return back(f"Could not save: {e}", True, "/log?t=weight")
        return back(f'Saved {r["kg"]} kg', False, "/log?t=weight")

    async def log_meal(request: Request, s: dict[str, Any], store: Store) -> Response:
        f = request.state.form
        try:
            r = store.log_meal(str(f.get("name", "")), num(f, "kcal"), num(f, "protein"), None, None, None,
                               None, num(f, "salt"), when_of(f, store))
        except (ValueError, TypeError) as e:
            return back(f"Could not save: {e}", True, "/log?t=meal")
        return back(f'Saved {r["name"]}', False, "/log?t=meal")

    async def log_fasting(request: Request, s: dict[str, Any], store: Store) -> Response:
        on = str(request.state.form.get("on")) == "1"
        store.set_fasting(store.local(store.now()).date(), on)
        return back("Marked as a fasting day" if on else "Fasting day removed", False, "/log?t=fasting")

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
        extra: dict[str, str | None] = {}
        for k, (lo, hi) in pages.NUTRITION_GOALS.items():
            raw = str(f.get(k, "")).strip()
            if not raw:
                extra[k] = None
                continue
            try:
                v = float(raw)
            except ValueError:
                return back("Calorie, protein, salt and weight goals must be numbers", True, "/settings")
            if not lo <= v <= hi:
                return back(f"{k.replace('_', ' ').capitalize()} must be between {lo} and {hi}", True, "/settings")
            extra[k] = f"{v:g}"
        store.set_setting("steps_goal", str(steps))
        store.set_setting("sleep_goal", f"{sleep:g}")
        for k, v in extra.items():
            store.set_setting(k, v)
        return back("Goals saved", False, "/settings")

    async def public_file(request: Request) -> Response:
        if request.url.path == "/manifest.webmanifest":
            resp = JSONResponse(MANIFEST, media_type="application/manifest+json")
        else:
            f = STATIC / PUBLIC_ICONS[request.url.path]
            resp = Response(f.read_bytes(), media_type="font/woff2" if f.suffix == ".woff2" else "image/png")
        resp.headers["Cache-Control"] = "public, max-age=2592000" if request.url.path.startswith("/fonts/") else "public, max-age=86400"
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
        Route("/settings/fasting-days", authed(fasting_days_post, post=True), methods=["POST"]),
        Route("/meals/eat", authed(meals_eat, post=True), methods=["POST"]),
        Route("/meals/swap", authed(swap_get)), Route("/meals/swap", authed(swap_post, post=True), methods=["POST"]),
        Route("/meals/skip", authed(meals_skip, post=True), methods=["POST"]),
        Route("/chat", authed(chat_get)), Route("/chat/send", authed(chat_send, post=True), methods=["POST"]),
        Route("/chat/confirm", authed(chat_confirm, post=True), methods=["POST"]),
        Route("/chat/cancel", authed(chat_cancel, post=True), methods=["POST"]),
        Route("/chat/clear", authed(chat_clear, post=True), methods=["POST"]),
        Route("/analysis/refresh", authed(analysis_refresh, post=True), methods=["POST"]),
        Route("/settings/appearance", authed(appearance_post, post=True), methods=["POST"]),
        Route("/settings/goal-weight", authed(goal_weight, post=True), methods=["POST"]),
        Route("/logout", authed(logout, post=True), methods=["POST"]),
        Route("/login", login), Route("/callback", callback), Route("/healthz", healthz),
    ])


def build(env: Mapping[str, str], primary: Store | None = None, stores: Stores | None = None) -> Starlette | None:
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
    stores = stores or Stores(env.get("DATA_DIR", "/data"), default, env.get("TZ_NAME", "Europe/London"), primary)
    mealie_url, mealie_token = (env.get("MEALIE_URL") or "").strip(), (env.get("MEALIE_API_TOKEN") or "").strip()
    client = mealie_mod.Mealie(mealie_url, mealie_token) if mealie_url and mealie_token else None
    app = create_app(env, stores, users, verifier, mealie=client)
    app.state.stores, app.state.users = stores, users
    app.state.ntfy_base = (env.get("NTFY_BASE") or "https://ntfy.sh").rstrip("/")
    return app
