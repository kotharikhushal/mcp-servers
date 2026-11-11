import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from starlette.testclient import TestClient

from app import web

BASE = "https://wellness.example.com"
ENV = {"WEB_PUBLIC_URL": BASE, "WEB_OIDC_CLIENT_ID": "wellness-web", "WEB_OIDC_CLIENT_SECRET": "s3cret",
       "WEB_SESSION_SECRET": "x" * 40}
USERS = {"khushal": "Khushal", "shivani": "Shivani"}
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


class FakeVerifier:
    def __init__(self):
        self.claims = {}
        self.userinfo = {}

    async def discovery(self):
        return {"authorization_endpoint": "https://auth.example.com/authorize",
                "token_endpoint": "https://auth.example.com/token",
                "userinfo_endpoint": "https://auth.example.com/userinfo"}

    async def claims_from_jwt(self, token):
        return dict(self.claims) if token == "good-id-token" else None


@pytest.fixture
def env(tmp_path):
    ver = FakeVerifier()
    def handler(req):
        if req.url.path == "/userinfo":
            return httpx.Response(200, json=ver.userinfo)
        return httpx.Response(200, json={"id_token": "good-id-token", "access_token": "at"})
    transport = httpx.MockTransport(handler)
    stores = web.Stores(str(tmp_path), "khushal", "Europe/London")
    app = web.create_app(ENV, stores, USERS, ver, http=httpx.AsyncClient(transport=transport))
    client = TestClient(app, base_url=BASE, follow_redirects=False)
    return client, stores, ver


def session_cookie(user, csrf="tok", exp=None):
    return web.Signer(ENV["WEB_SESSION_SECRET"]).dumps(
        {"u": user, "csrf": csrf, "exp": exp or time.time() + 3600})


def signed_in(client, user="khushal"):
    client.cookies.set(web.SESSION_COOKIE, session_cookie(user), domain="wellness.example.com")
    return client


def test_unauthenticated_requests_are_refused(env):
    c, _, _ = env
    r = c.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.post("/log/bp", data={"systolic": 120, "diastolic": 80}).status_code == 401


def test_forged_unknown_and_expired_sessions_are_refused(env):
    c, _, _ = env
    good = session_cookie("khushal")
    for bad in (good[:-2] + "xx", session_cookie("mallory"), session_cookie("khushal", exp=time.time() - 5), "junk"):
        c.cookies.clear()
        c.cookies.set(web.SESSION_COOKIE, bad, domain="wellness.example.com")
        assert c.get("/").status_code == 303


def test_signed_in_user_sees_pages_with_security_headers(env):
    c, _, _ = env
    signed_in(c)
    for path in ("/", "/bp", "/log"):
        r = c.get(path)
        assert r.status_code == 200 and "Khushal" in r.text
        assert "default-src 'none'" in r.headers["content-security-policy"]


def test_bad_host_is_rejected(env):
    c, _, _ = env
    signed_in(c)
    assert c.get("/", headers={"host": "evil.example.org"}).status_code == 400


def test_post_needs_csrf_token_and_right_origin(env):
    c, stores, _ = env
    signed_in(c)
    form = {"systolic": "150", "diastolic": "100", "pulse": "70"}
    assert c.post("/log/bp", data=form).status_code == 403
    assert c.post("/log/bp", data={**form, "csrf": "wrong"}).status_code == 403
    assert c.post("/log/bp", data={**form, "csrf": "tok"}, headers={"origin": "https://evil.example.org"}).status_code == 403
    assert stores.get("khushal").bp_readings(1) == []
    r = c.post("/log/bp", data={**form, "csrf": "tok", "note": "after medication"})
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    saved = stores.get("khushal").bp_readings(1)
    assert saved[0]["systolic"] == 150 and saved[0]["note"] == "after medication"


def test_each_person_only_sees_their_own_data(env, tmp_path):
    c, stores, _ = env
    signed_in(c, "khushal")
    c.post("/log/bp", data={"systolic": "151", "diastolic": "101", "csrf": "tok"})
    assert "151/101" in c.get("/bp").text
    c.cookies.clear()
    signed_in(c, "shivani")
    assert "151/101" not in c.get("/bp").text
    c.post("/log/weight", data={"kg": "58.9", "csrf": "tok"})
    assert stores.get("shivani").weight_readings(1)[0]["kg"] == 58.9
    assert stores.get("khushal").weight_readings(1) == []
    assert (tmp_path / "health.db").exists() and (tmp_path / "users" / "shivani.db").exists()


def test_bad_values_show_an_error_not_a_crash(env):
    c, stores, _ = env
    signed_in(c)
    r = c.post("/log/bp", data={"systolic": "90", "diastolic": "120", "csrf": "tok"})
    assert r.status_code == 303 and "err=" in r.headers["location"]
    r = c.post("/log/meal", data={"name": "", "kcal": "100", "csrf": "tok"})
    assert "err=" in r.headers["location"]


def test_meal_fasting_and_delete(env):
    c, stores, _ = env
    signed_in(c)
    c.post("/log/meal", data={"name": "Bhurji", "kcal": "414", "salt": "1.2", "csrf": "tok"})
    meal = stores.get("khushal").food_entries(1)[0]
    assert meal["name"] == "Bhurji" and meal["salt_g"] == 1.2
    c.post("/log/fasting", data={"on": "1", "csrf": "tok"})
    assert stores.get("khushal").fasting_days(1)
    assert "Fasting day" in c.get("/").text
    c.post("/log/delete", data={"kind": "nutrition", "id": str(meal["id"]), "csrf": "tok"})
    assert stores.get("khushal").food_entries(1) == []


def test_page_escapes_user_text(env):
    c, _, _ = env
    signed_in(c)
    c.post("/log/bp", data={"systolic": "120", "diastolic": "80", "note": "<script>alert(1)</script>", "csrf": "tok"})
    assert "<script>alert(1)</script>" not in c.get("/bp").text
    assert "&lt;script&gt;" in c.get("/bp").text


def _login_flow(c, ver, username, nonce_override=None):
    r = c.get("/login")
    assert r.status_code == 303
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["client_id"] == ["wellness-web"]
    sent = web.Signer(ENV["WEB_SESSION_SECRET"]).loads(c.cookies.get(web.LOGIN_COOKIE))
    assert 43 <= len(sent["v"]) <= 128  # RFC 7636 code_verifier length
    ver.claims = {"preferred_username": username, "nonce": nonce_override or q["nonce"][0]}
    return c.get("/callback", params={"code": "abc", "state": q["state"][0]})


def test_login_flow_creates_session_for_allowed_user(env):
    c, _, ver = env
    r = _login_flow(c, ver, "Shivani")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "Shivani" in c.get("/").text


def test_username_is_read_from_userinfo_when_id_token_lacks_it(env):
    c, _, ver = env
    r = c.get("/login")
    q = parse_qs(urlparse(r.headers["location"]).query)
    ver.claims = {"sub": "u-1", "nonce": q["nonce"][0]}
    ver.userinfo = {"sub": "u-1", "preferred_username": "shivani"}
    r = c.get("/callback", params={"code": "abc", "state": q["state"][0]})
    assert r.status_code == 303 and "Shivani" in c.get("/").text


def test_userinfo_for_a_different_subject_is_refused(env):
    c, _, ver = env
    q = parse_qs(urlparse(c.get("/login").headers["location"]).query)
    ver.claims = {"sub": "u-1", "nonce": q["nonce"][0]}
    ver.userinfo = {"sub": "someone-else", "preferred_username": "khushal"}
    assert c.get("/callback", params={"code": "abc", "state": q["state"][0]}).status_code == 403


def test_login_refuses_unlisted_account_and_wrong_nonce(env):
    c, _, ver = env
    assert _login_flow(c, ver, "mallory").status_code == 403
    c.cookies.clear()
    assert _login_flow(c, ver, "khushal", nonce_override="other").status_code == 401
    assert c.get("/").status_code == 303


def test_callback_without_login_cookie_or_bad_state_fails(env):
    c, _, _ = env
    assert c.get("/callback", params={"code": "abc", "state": "x"}).status_code == 400
    c.get("/login")
    assert c.get("/callback", params={"code": "abc", "state": "wrong"}).status_code == 400


def test_config_validation():
    with pytest.raises(web.WebConfigError):
        web.parse_users("")
    with pytest.raises(web.WebConfigError):
        web.Signer("short")
    assert web.build({}) is None


def test_today_page_renders_with_real_data(env):
    c, stores, _ = env
    s = stores.get("khushal")
    now = s.now()
    s.ingest({"sleep": [{"session_end_time": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "duration_seconds": 5 * 3600,
                         "stages": [{"stage": 4, "duration_seconds": 12000}, {"stage": 1, "duration_seconds": 1200}]}],
              "steps": [{"start_time": (now.replace(hour=0, minute=0, second=0)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "end_time": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "count": 3456}]})
    s.log_blood_pressure(185, 125, 80, None, None)
    s.log_weight(76.1, None)
    s.log_meal("Tea", 40, None, None, None, None, None, None, None)
    signed_in(c)
    body = c.get("/").text
    assert "3456" in body and "76.1 kg" in body and "185/125" in body and "NHS 111" in body
    assert "under 6 hours" in body or "Last night" in body
    assert "Week summary" in c.get("/bp").text
