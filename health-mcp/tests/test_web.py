import time
from datetime import datetime, timedelta, timezone
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
    assert "Fasting day" in c.get("/").text and "Fasting day" in c.get("/food").text
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
    assert "3,456" in body and "76.1" in body and "185/125" in body and "NHS 111" in body
    assert "under 6 hours" in body or "Last night" in body
    assert "Week summary" in c.get("/bp").text


def test_every_detail_page_renders_for_every_range_with_and_without_data(env):
    c, stores, _ = env
    signed_in(c)
    paths = ["/sleep", "/steps", "/heart", "/bp", "/weight", "/food", "/activity"]
    for path in paths:                                   # empty database
        for d in (7, 30, 90):
            r = c.get(f"{path}?d={d}")
            assert r.status_code == 200 and "Khushal" in r.text, path
    s = stores.get("khushal")
    now = s.now()
    s.log_blood_pressure(150, 95, 70, None, None)
    s.log_weight(80.2, None)
    s.log_meal("Toast", 200, 6, None, None, None, None, 0.5, None)
    s.ingest({"steps": [{"start_time": (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "end_time": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"), "count": 900}],
              "heart_rate": [{"time": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "bpm": 72}],
              "sleep": [{"session_end_time": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "duration_seconds": 7 * 3600,
                         "stages": [{"stage": 5, "duration_seconds": 5000}, {"stage": 6, "duration_seconds": 6000},
                                    {"stage": 4, "duration_seconds": 14000}]}],
              "exercise": [{"start_time": (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "end_time": (now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ"), "type": 79}]})
    for path in paths:
        for d in (7, 30, 90):
            assert c.get(f"{path}?d={d}").status_code == 200, (path, d)
    assert "150/95" in c.get("/bp").text and "80.2" in c.get("/weight").text and "Toast" in c.get("/food").text
    assert c.get("/steps?d=banana").status_code == 200 and c.get("/sleep?d=9999").status_code == 200


def test_detail_pages_are_per_person_and_need_sign_in(env):
    c, stores, _ = env
    for path in ("/sleep", "/steps", "/heart", "/bp", "/weight", "/food", "/activity"):
        assert c.get(path).status_code == 303
    stores.get("khushal").log_weight(81.3, None)
    signed_in(c, "shivani")
    assert "81.3" not in c.get("/weight").text and "81.3" not in c.get("/").text


def test_goals_are_validated_saved_and_used(env):
    c, stores, _ = env
    signed_in(c)
    assert "err=" in c.post("/settings/goals", data={"csrf": "tok", "steps_goal": "5", "sleep_goal": "7"}).headers["location"]
    assert "err=" in c.post("/settings/goals", data={"csrf": "tok", "steps_goal": "abc", "sleep_goal": "7"}).headers["location"]
    r = c.post("/settings/goals", data={"csrf": "tok", "steps_goal": "10000", "sleep_goal": "8"})
    assert "ok=" in r.headers["location"] and stores.get("khushal").get_setting("steps_goal") == "10000"
    assert "of 10,000" in c.get("/").text and "of 8 h" in c.get("/").text
    assert stores.get("shivani").get_setting("steps_goal") is None


def test_phone_install_files_are_public_and_the_csp_allows_the_manifest(env):
    c, _, _ = env
    m = c.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.json()["display"] == "standalone"
    for icon in ("/icon-192.png", "/icon-512.png", "/icon-maskable-512.png", "/apple-touch-icon.png"):
        r = c.get(icon)
        assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    signed_in(c)
    page = c.get("/")
    assert "manifest-src 'self'" in page.headers["content-security-policy"]
    assert 'rel="manifest"' in page.text and "viewport-fit=cover" in page.text


def test_sign_out_lives_in_settings(env):
    c, _, _ = env
    signed_in(c)
    assert 'action="/logout"' in c.get("/settings").text


# ---------------------------------------------------------------- GP report, settings, reminders, import
def _log_week(store):
    base = store.now().replace(hour=8, minute=0, second=0, microsecond=0)
    for back, am, pm in ((5, (150, 100), (148, 98)), (4, (146, 96), (144, 95)), (3, (140, 92), (142, 94))):
        day = base - timedelta(days=back)
        for hour, minute, (sys_, dia), pulse in ((8, 0, am, 70), (8, 2, (am[0] + 1, am[1] + 1), 71),
                                                  (20, 0, pm, 68), (20, 2, (pm[0] + 1, pm[1] + 1), 69)):
            store.log_blood_pressure(sys_, dia, pulse, day.replace(hour=hour, minute=minute), "after medication")


def test_gp_report_has_summary_context_and_escapes(env):
    c, stores, _ = env
    s = stores.get("khushal")
    _log_week(s)
    signed_in(c)
    c.post("/settings/context", data={"context": "Losartan 25mg nightly <b>from 10 Nov</b>", "csrf": "tok"})
    body = c.get("/gp?days=28").text
    assert "Home blood pressure record" in body and "Khushal" in body
    assert "&lt;b&gt;from 10 Nov&lt;/b&gt;" in body and "<b>from 10 Nov</b>" not in body
    assert "Highest" in body and "Excluding the first day" in body
    assert "no diagnosis" not in body and "not a diagnosis" in body


def test_gp_report_empty_period_and_bad_days(env):
    c, _, _ = env
    signed_in(c)
    assert "No readings in this period" in c.get("/gp?days=banana").text


def test_gp_report_is_per_person(env):
    c, stores, _ = env
    stores.get("khushal").log_blood_pressure(151, 101, 70, None, None)
    signed_in(c, "shivani")
    assert "151/101" not in c.get("/gp").text


def test_reminder_settings_validation_topic_and_test_send(env):
    c, stores, _ = env
    s = stores.get("khushal")
    signed_in(c)
    r = c.post("/settings/reminders", data={"enabled": "1", "morning": "25:00", "evening": "21:00", "csrf": "tok"})
    assert "err=" in r.headers["location"] and s.get_setting("reminders_enabled") is None
    r = c.post("/settings/reminders", data={"enabled": "1", "morning": "07:30", "evening": "20:45", "csrf": "tok"})
    assert "ok=" in r.headers["location"]
    assert s.get_setting("reminders_enabled") == "1" and s.get_setting("reminder_morning") == "07:30"
    topic = s.get_setting("reminder_topic")
    assert topic.startswith("wellness-")
    assert topic in c.get("/settings").text
    # saving again keeps the same topic
    c.post("/settings/reminders", data={"enabled": "1", "morning": "07:30", "evening": "20:45", "csrf": "tok"})
    assert s.get_setting("reminder_topic") == topic
    # people have separate topics
    c.cookies.clear()
    signed_in(c, "shivani")
    c.post("/settings/reminders", data={"enabled": "1", "morning": "08:00", "evening": "21:00", "csrf": "tok"})
    assert stores.get("shivani").get_setting("reminder_topic") != topic


def test_test_reminder_uses_the_notifier(tmp_path):
    sent = []
    ver = FakeVerifier()
    stores = web.Stores(str(tmp_path), "khushal", "Europe/London")
    app = web.create_app(ENV, stores, USERS, ver, notify=lambda t, ti, m: sent.append((t, m)))
    c = TestClient(app, base_url=BASE, follow_redirects=False)
    signed_in(c)
    assert "err=" in c.post("/settings/test", data={"csrf": "tok"}).headers["location"]
    c.post("/settings/reminders", data={"enabled": "1", "morning": "08:00", "evening": "21:00", "csrf": "tok"})
    r = c.post("/settings/test", data={"csrf": "tok"})
    assert "ok=" in r.headers["location"] and sent and sent[0][0].startswith("wellness-")


def test_apple_import_via_upload_goes_to_the_signed_in_persons_database(env):
    import json
    from tests.test_apple import EXPORT
    c, stores, _ = env
    signed_in(c, "shivani")
    files = {"file": ("export.json", json.dumps(EXPORT).encode(), "application/json")}
    r = c.post("/import", data={"csrf": "tok"}, files=files)
    assert r.status_code == 303 and "ok=" in r.headers["location"] and "nights" in r.headers["location"].replace("+", " ")
    assert stores.get("shivani").status()["tables"]["sleep"]["rows"] == 1
    assert stores.get("khushal").status()["tables"]["sleep"]["rows"] == 0


def test_apple_import_rejects_bad_file_missing_file_and_missing_csrf(env):
    c, _, _ = env
    signed_in(c)
    r = c.post("/import", data={"csrf": "tok"}, files={"file": ("x.json", b"nope", "application/json")})
    assert "err=" in r.headers["location"]
    assert "err=" in c.post("/import", data={"csrf": "tok"}).headers["location"]
    r = c.post("/import", data={}, files={"file": ("x.json", b"{}", "application/json")})
    assert r.status_code == 403


def test_new_pages_need_sign_in(env):
    c, _, _ = env
    for path in ("/gp", "/settings", "/import"):
        assert c.get(path).status_code == 303
    for path in ("/settings/reminders", "/settings/test", "/settings/context", "/import"):
        assert c.post(path, data={}).status_code == 401


def test_referrer_policy_lets_browsers_send_a_real_origin_on_form_posts(env):
    c, _, _ = env
    signed_in(c)
    assert c.get("/log").headers["referrer-policy"] == "same-origin"
    # a browser form post under the policy carries the site's own origin and is accepted
    ok = c.post("/log/weight", data={"kg": "70", "csrf": "tok"}, headers={"origin": BASE})
    assert ok.status_code == 303 and "ok=" in ok.headers["location"]
    # "null" is what a browser sends under no-referrer: refused, so the header must never go back
    assert c.post("/log/weight", data={"kg": "71", "csrf": "tok"}, headers={"origin": "null"}).status_code == 403


def test_import_confirmation_is_prominent_and_remembered(env):
    import json
    from tests.test_apple import EXPORT
    c, stores, _ = env
    signed_in(c, "shivani")
    c.follow_redirects = True
    r = c.post("/import", data={"csrf": "tok"}, files={"file": ("e.json", json.dumps(EXPORT).encode(), "application/json")})
    assert 'role="status"' in r.text and "Done: Imported" in r.text
    again = c.get("/import").text
    assert "Last import:" in again and "new nights" in again          # still visible after the message is gone
    assert "Last import:" in c.get("/settings").text
    bad = c.post("/import", data={"csrf": "tok"}, files={"file": ("e.json", b"nope", "application/json")})
    assert "Problem: " in bad.text
    c.cookies.clear()
    signed_in(c, "khushal")
    assert "Nothing imported yet" in c.get("/import").text           # per person
