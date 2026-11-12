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


def test_calorie_protein_and_salt_goals_show_what_is_left_today(env):
    c, stores, _ = env
    signed_in(c)
    base = {"csrf": "tok", "steps_goal": "10000", "sleep_goal": "8"}
    for bad in ({"kcal_goal": "300"}, {"protein_goal": "x"}, {"salt_limit": "50"}):
        assert "err=" in c.post("/settings/goals", data={**base, **bad}).headers["location"]
    assert stores.get("khushal").get_setting("kcal_goal") is None
    k = stores.get("khushal")
    k.log_meal("Test lunch", 1560, 87, None, None, None, None, 4.4, None)
    ok = c.post("/settings/goals", data={**base, "kcal_goal": "1850", "protein_goal": "120", "salt_limit": "6"})
    assert "ok=" in ok.headers["location"]
    food = c.get("/food").text
    assert "Goal 1,850 kcal" in food and "290 kcal left" in food and "of 120 g" in food and "of 6.0 g" in food and "<h2>Meals</h2>" in food and 'role="progressbar"' in food
    assert "290 kcal left" in c.get("/").text
    assert 'value="1850"' in c.get("/settings").text
    assert stores.get("shivani").get_setting("kcal_goal") is None
    c.post("/settings/goals", data=base)  # blank fields clear the goals
    assert k.get_setting("kcal_goal") is None and "Goal 1,850" not in c.get("/food").text


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


def test_goal_weight_is_optional_and_drives_the_weight_page(env):
    c, stores, _ = env
    signed_in(c)
    k = stores.get("khushal")
    base = {"csrf": "tok", "steps_goal": "10000", "sleep_goal": "8"}
    assert "err=" in c.post("/settings/goals", data={**base, "goal_weight": "20"}).headers["location"]
    for i, kg in enumerate((75.0, 74.8, 74.6, 74.7, 74.4)):
        k.log_weight(kg, datetime(2026, 11, 7 + i, 7, 30, tzinfo=timezone.utc))
    assert "Set a goal weight" in c.get("/weight").text
    assert "ok=" in c.post("/settings/goals", data={**base, "goal_weight": "70"}).headers["location"]
    w = c.get("/weight").text
    assert "To goal" in w and "above your 70 kg goal" in w and "7-day average" in w and "Morning weigh-in" in w
    c.post("/settings/goals", data=base)
    assert k.get_setting("goal_weight") is None


def test_blood_pressure_readings_a_minute_apart_are_one_sitting():
    from app.views import bp_sessions
    rs = [{"time": "2026-11-10 08:00", "systolic": 150, "diastolic": 90, "pulse": 70, "note": "", "source": "manual", "id": 1},
          {"time": "2026-11-10 08:02", "systolic": 140, "diastolic": 86, "pulse": 68, "note": "", "source": "manual", "id": 2},
          {"time": "2026-11-10 21:00", "systolic": 130, "diastolic": 80, "pulse": None, "note": "", "source": "manual", "id": 3}]
    s = bp_sessions(rs)
    assert len(s) == 2 and (s[0]["systolic"], s[0]["diastolic"]) == (145, 88) and len(s[0]["readings"]) == 2


def test_delete_needs_a_second_tap(env):
    c, stores, _ = env
    signed_in(c)
    stores.get("khushal").log_meal("Toast", 200, 6, None, None, None, None, 0.2, None)
    page = c.get("/food").text
    assert "Delete this entry?" in page and "Yes, delete" in page


class StubMealie:
    def __init__(self, fail=False):
        self.fail = fail

    def day_menu(self, day):
        from app.mealie import MealieError
        if self.fail:
            raise MealieError("down")
        rec = {"name": "Dal and rice", "nutrition_per_serving": {"kcal": 620, "protein_g": 24}}
        return [{"id": "e1", "meal": "dinner", "recipe": "Dal and rice", "recipe_name": "Dal and rice", "slug": "dal",
                 "per_serving": rec["nutrition_per_serving"], "_rec": rec, "note": None}]


def mealie_env(tmp_path, stub):
    ver = FakeVerifier()
    stores = web.Stores(str(tmp_path), "khushal", "Europe/London")
    app = web.create_app(ENV, stores, USERS, ver, mealie=stub,
                         http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    return signed_in(TestClient(app, base_url=BASE, follow_redirects=False)), stores


def test_todays_planned_meals_can_be_logged_in_one_tap_and_skipped(tmp_path):
    c, stores = mealie_env(tmp_path, StubMealie())
    home = c.get("/").text
    assert "Today's meals" in home and "Dal and rice" in home and "620 kcal" in home and "I ate this" in home
    r = c.post("/meals/eat", data={"csrf": "tok", "entry_id": "e1"})
    assert "ok=" in r.headers["location"] and r.headers["location"].endswith("#meals")   # stays on the meals card
    assert 'id="meals"' in home
    k = stores.get("khushal")
    assert [e["name"] for e in k.food_entries(1)] == ["Dal and rice x1 (from Mealie)"] and k.food_entries(1)[0]["kcal"] == 620
    c.post("/meals/eat", data={"csrf": "tok", "entry_id": "e1"})   # a second tap must not log it twice
    assert len(k.food_entries(1)) == 1 and "Logged" in c.get("/").text
    assert "err=" in c.post("/meals/eat", data={"csrf": "tok", "entry_id": "nope"}).headers["location"]
    c.post("/meals/skip", data={"csrf": "tok", "entry_id": "e2", "on": "1"})
    assert k.get_setting(f"skip:{k.local(k.now()).date().isoformat()}") == "e2"
    c.post("/meals/skip", data={"csrf": "tok", "entry_id": "e2", "on": "0"})
    assert k.get_setting(f"skip:{k.local(k.now()).date().isoformat()}") is None
    assert c.post("/meals/eat", data={"entry_id": "e1"}).status_code in (400, 403)  # no CSRF token


def test_home_still_loads_when_the_meal_plan_is_unreachable(tmp_path):
    c, _ = mealie_env(tmp_path, StubMealie(fail=True))
    r = c.get("/")
    assert r.status_code == 200 and "Meal plan unavailable" in r.text


def test_goal_weight_can_be_changed_from_the_weight_page(env):
    c, stores, _ = env
    signed_in(c)
    k = stores.get("khushal")
    k.log_weight(74.5, datetime.now(timezone.utc))
    assert 'action="/settings/goal-weight"' in c.get("/weight").text
    assert "ok=" in c.post("/settings/goal-weight", data={"csrf": "tok", "goal_weight": "69.5"}).headers["location"]
    assert k.get_setting("goal_weight") == "69.5" and 'value="69.5"' in c.get("/weight").text
    assert "err=" in c.post("/settings/goal-weight", data={"csrf": "tok", "goal_weight": "5"}).headers["location"]
    c.post("/settings/goal-weight", data={"csrf": "tok", "goal_weight": ""})
    assert k.get_setting("goal_weight") is None


def test_weekly_fasting_days_and_log_again(env):
    c, stores, _ = env
    signed_in(c)
    k = stores.get("khushal")
    today = k.local(k.now()).date()
    assert "ok=" in c.post("/settings/fasting-days", data={"csrf": "tok", "wd": [str(today.weekday())]}).headers["location"]
    assert today in k.fasting_days(1)
    c.post("/log/fasting", data={"csrf": "tok", "on": "0"})          # one-off exception
    assert today not in k.fasting_days(1)
    c.post("/log/fasting", data={"csrf": "tok", "on": "1"})
    assert today in k.fasting_days(1)
    c.post("/settings/fasting-days", data={"csrf": "tok"})
    c.post("/log/fasting", data={"csrf": "tok", "on": "0"})
    assert k.fasting_weekdays() == set() and today not in k.fasting_days(1)
    k.log_meal("Greek yoghurt", 150, 15, None, None, None, None, 0.2, None)
    log = c.get("/log").text
    assert "Recent meals" in log and "Log again" in log


class SwapStub(StubMealie):
    def __init__(self):
        super().__init__()
        self.swapped = None

    def swap_options(self, kcal, q, slug):
        return [{"slug": "soup", "name": "Lentil soup", "per_serving": {"kcal": 450, "protein_g": 20}}]

    def swap_plan_entry(self, eid, slug):
        self.swapped = (eid, slug)
        return {"recipe": "Lentil soup"}


def test_a_planned_meal_can_be_swapped_for_another_recipe(tmp_path):
    stub = SwapStub()
    c, _ = mealie_env(tmp_path, stub)
    assert "/meals/swap?entry_id=e1" in c.get("/").text
    page = c.get("/meals/swap?entry_id=e1").text
    assert "Lentil soup" in page and "170 kcal less" not in page and "−170 kcal vs planned" in page
    r = c.post("/meals/swap", data={"csrf": "tok", "entry_id": "e1", "slug": "soup"})
    assert "ok=" in r.headers["location"] and stub.swapped == ("e1", "soup")
    assert c.get("/meals/swap?entry_id=zzz").headers["location"].startswith("/?err=")
    assert c.post("/meals/swap", data={"entry_id": "e1", "slug": "soup"}).status_code in (400, 403)


def test_log_page_is_split_into_tabs(env):
    c, _, _ = env
    signed_in(c)
    meal, bp = c.get("/log").text, c.get("/log?t=bp").text
    assert 'action="/log/meal"' in meal and 'action="/log/bp"' not in meal
    assert 'action="/log/bp"' in bp and 'action="/log/meal"' not in bp
    assert 'action="/log/weight"' in c.get("/log?t=weight").text and 'action="/log/meal"' in c.get("/log?t=nonsense").text
    r = c.post("/log/bp", data={"csrf": "tok", "systolic": "120", "diastolic": "80"})
    assert r.headers["location"].startswith("/log?t=bp&ok=")


def test_each_person_picks_a_mode_and_colour_theme_and_the_font_is_served(env):
    c, stores, _ = env
    signed_in(c)
    assert 'data-mode="auto" data-palette="tide"' in c.get("/").text
    r = c.post("/settings/appearance", data={"csrf": "tok", "mode": "dark", "palette": "dusk"})
    assert "ok=" in r.headers["location"]
    home = c.get("/").text
    assert 'data-mode="dark" data-palette="dusk"' in home and 'name="palette" value="dusk" checked' in c.get("/settings").text
    assert stores.get("shivani").get_setting("palette") is None            # per person
    assert "err=" in c.post("/settings/appearance", data={"csrf": "tok", "mode": "neon", "palette": "dusk"}).headers["location"]
    c.post("/settings/appearance", data={"csrf": "tok", "mode": "auto", "palette": "tide"})
    assert stores.get("khushal").get_setting("palette") is None and stores.get("khushal").get_setting("mode") is None
    f = c.get("/fonts/bricolage-grotesque.woff2")
    assert f.status_code == 200 and f.headers["content-type"] == "font/woff2" and f.content[:4] == b"wOF2"
    assert "font-src 'self'" in c.get("/").headers["content-security-policy"]


def test_bp_coach_note_names_the_number_that_is_high_and_uses_the_sitting_average():
    from app.pages import bp_over_text
    dia = bp_over_text({"systolic": 128, "diastolic": 102, "readings": [1, 2]})
    assert "averaged 128/102" in dia and "bottom number is above the 85" in dia and "top number is under 135" in dia
    sysh = bp_over_text({"systolic": 150, "diastolic": 80, "readings": [1]})
    assert "last reading was 150/80" in sysh and "top number is above the 135" in sysh
    both = bp_over_text({"systolic": 150, "diastolic": 95, "readings": [1]})
    assert "Both numbers" in both


# ---------------------------------------------------------------- local-model analysis
GOOD_ANSWER = {"headline": "Blood pressure sittings are being logged; keep going",
               "points": ["Data is limited so far, with only a few days of readings.",
                          "Your latest sitting averaged {avg}: {summary}. Discuss these numbers with your GP.",
                          "Try logging a weigh-in and your meals so the picture fills in."]}


def ai_env(tmp_path, monkeypatch, answers):
    """App with OLLAMA_URL set and a fake Ollama that returns the given answers in turn."""
    calls = []
    real = httpx.AsyncClient

    def fake(request):
        calls.append(request)
        a = answers[min(len(calls) - 1, len(answers) - 1)]
        return httpx.Response(200, json={"message": {"content": a if isinstance(a, str) else __import__("json").dumps(a)}})

    monkeypatch.setattr(web.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(fake), **kw))
    stores = web.Stores(str(tmp_path), "khushal", "Europe/London")
    app = web.create_app(dict(ENV, OLLAMA_URL="http://ollama.test:9068", OLLAMA_MODEL="gemma4:12b"), stores, USERS, FakeVerifier(),
                         http=real(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    return signed_in(TestClient(app, base_url=BASE, follow_redirects=False)), stores, calls


def test_refresh_button_runs_the_local_model_checks_the_answer_and_saves_it(tmp_path, monkeypatch):
    import json
    from app import analysis
    summary = "Latest sitting averaged 128/104: top number 128 is 7 under 135; bottom number 104 is 19 over 85."
    good = {"headline": GOOD_ANSWER["headline"],
            "points": [GOOD_ANSWER["points"][0], f"{summary} Discuss these numbers with your GP.", GOOD_ANSWER["points"][2]]}
    c, stores, calls = ai_env(tmp_path, monkeypatch, [good])
    k = stores.get("khushal")
    k.log_blood_pressure(128, 104, 70, None, None)
    assert analysis.build_facts(k)["blood_pressure"]["latest_sitting_summary"] == summary
    home = c.get("/").text
    assert "Refresh analysis" in home and "No analysis yet" in home
    r = c.post("/analysis/refresh", data={"csrf": "tok"})
    assert "ok=" in r.headers["location"] and len(calls) == 1
    body = json.loads(calls[0].content)
    assert body["model"] == "gemma4:12b" and "latest_sitting_summary" in body["messages"][1]["content"]
    page = c.get("/").text
    assert "Blood pressure sittings are being logged" in page and "gemma4:12b" in page and "Not a diagnosis" in page
    again = c.post("/analysis/refresh", data={"csrf": "tok"})            # inside the hour: refused, model not called
    assert "err=" in again.headers["location"] and len(calls) == 1
    assert c.post("/analysis/refresh", data={}).status_code in (400, 403)   # no CSRF token


def test_a_wrong_or_alarming_answer_is_rejected_and_the_old_analysis_is_kept(tmp_path, monkeypatch):
    bad = {"headline": "Your blood pressure is high and needs attention",
           "points": ["Latest reading 128/104 is below the home threshold of 135/85. See your GP.",
                      "You have hypertension.", "Aim for 9,500 steps."]}
    c, stores, calls = ai_env(tmp_path, monkeypatch, [bad])
    k = stores.get("khushal")
    k.log_blood_pressure(128, 104, 70, None, None)
    k.set_setting("ai_analysis", '{"headline": "Earlier note", "points": ["kept"], "at": "%s", "source": "Claude"}' % k.now().isoformat())
    r = c.post("/analysis/refresh", data={"csrf": "tok"})
    assert "err=" in r.headers["location"] and len(calls) == 2           # one retry, then give up
    assert "Earlier note" in c.get("/").text


def test_no_refresh_button_when_no_local_model_is_set_up(env):
    c, stores, _ = env
    signed_in(c)
    stores.get("khushal").set_setting("ai_analysis", '{"headline": "H", "points": ["p"], "at": "%s"}' % stores.get("khushal").now().isoformat())
    page = c.get("/").text
    assert "Ask Claude to refresh it" in page and "Refresh analysis" not in page
    assert "err=" in c.post("/analysis/refresh", data={"csrf": "tok"}).headers["location"]


def test_the_checks_catch_each_kind_of_bad_answer():
    from app.analysis import problems
    f = {"goals": {"steps": 10000}, "steps": {"daily_average": 7909},
         "blood_pressure": {"latest_sitting_summary": "Latest sitting averaged 120/93: top number 120 is 15 under 135; bottom number 93 is 8 over 85.",
                            "latest_sitting_mixed": True, "home_threshold": "135/85"}, "thin": False}
    ok = {"headline": "Steps are up", "points": ["Steps averaged 7909 a day.", "Latest sitting averaged 120/93: top number 120 is 15 under 135; bottom number 93 is 8 over 85. Discuss with your GP.", "Keep going."]}
    import json
    assert problems(json.dumps(ok), f) == []
    def bad(**kw):
        d = json.loads(json.dumps(ok)); d.update(kw); return json.dumps(d)
    assert "not valid JSON" in problems("nope", f)[0]
    assert any("not in the facts" in p for p in problems(bad(points=["Aim for 8,000 steps.", "GP", "x"]), f))
    assert "diagnosis or medication wording" in problems(bad(headline="You may have hypertension"), f)
    assert "compares a mixed reading to the threshold as a whole" in problems(bad(points=["Your 120/93 is below the 135/85 threshold. GP.", "a", "b"]), f)
    assert "alarming blood-pressure wording" in problems(bad(points=["Your blood pressure readings are high. GP.", "a", "b"]), f)
    assert "blood pressure without the GP" in problems(bad(points=["Your blood pressure is changing.", "a", "b"]), f)
    thin = dict(f, thin=True)
    assert "does not say the data is thin" in problems(json.dumps(ok), thin)


# ---------------------------------------------------------------- chat
def chat_answer(reply, **action):
    return {"reply": reply, "action": action or {"kind": "none"}}


def test_chat_logs_only_after_confirm_and_keeps_history(tmp_path, monkeypatch):
    import json
    c, stores, calls = ai_env(tmp_path, monkeypatch, [
        chat_answer("I will save 78.4 kg as your weight. Press Confirm to save it.", kind="log_weight", kg=78.4)])
    k = stores.get("khushal")
    assert 'href="/chat"' in c.get("/").text
    r = c.post("/chat/send", data={"csrf": "tok", "text": "log my weight 78.4"})
    assert r.headers["location"] == "/chat" and not k.weight_readings(1)          # proposed, not saved
    page = c.get("/chat").text
    assert "Confirm to save" in page and "Save weight 78.4 kg" in page and "log my weight 78.4" in page
    body = json.loads(calls[0].content)
    assert body["messages"][-1]["content"] == "log my weight 78.4" and "Facts:" in body["messages"][1]["content"]
    assert "err=" in c.post("/chat/send", data={"csrf": "tok", "text": "another"}).headers["location"]   # must decide first
    pid = k.get_setting("chat_pending")
    assert c.post("/chat/confirm", data={"csrf": "tok", "id": "wrong"}).headers["location"].find("err=") > 0
    assert not k.weight_readings(1)
    pid = json.loads(pid)["id"]
    ok = c.post("/chat/confirm", data={"csrf": "tok", "id": pid})
    assert "ok=" in ok.headers["location"] and k.weight_readings(1)[-1]["kg"] == 78.4
    assert "err=" in c.post("/chat/confirm", data={"csrf": "tok", "id": pid}).headers["location"]   # no double save
    assert len(k.weight_readings(1)) == 1
    assert c.post("/chat/confirm", data={"id": pid}).status_code in (400, 403)                        # CSRF
    # cancel path, then Shivani's chat is separate
    c.post("/chat/send", data={"csrf": "tok", "text": "log my weight 78.4"})
    pid2 = json.loads(k.get_setting("chat_pending"))["id"]
    c.post("/chat/cancel", data={"csrf": "tok", "id": pid2})
    assert k.get_setting("chat_pending") is None and len(k.weight_readings(1)) == 1
    sh = signed_in(c, "shivani").get("/chat").text
    assert "Clear chat history" not in sh and "Saved weight" not in sh
    signed_in(c, "khushal")
    c.post("/chat/clear", data={"csrf": "tok"})
    assert "Clear chat history" not in c.get("/chat").text


def test_chat_rejects_an_action_with_numbers_the_person_did_not_type(tmp_path, monkeypatch):
    c, stores, calls = ai_env(tmp_path, monkeypatch, [
        chat_answer("I will save 80 kg. Press Confirm.", kind="log_weight", kg=80)])
    k = stores.get("khushal")
    r = c.post("/chat/send", data={"csrf": "tok", "text": "log my weight 78.4"})
    assert "err=" in r.headers["location"] and len(calls) == 2 and k.get_setting("chat_pending") is None


def test_chat_history_is_capped_and_bad_replies_are_blocked(tmp_path, monkeypatch):
    answers = [chat_answer("Your steps look fine.")]
    c, stores, calls = ai_env(tmp_path, monkeypatch, answers)
    k = stores.get("khushal")
    for i in range(12):
        c.post("/chat/send", data={"csrf": "tok", "text": f"question {i}"})
    from app import chat
    assert len(chat.load_history(k)) == chat.HISTORY_MAX
    answers[0] = chat_answer("You have hypertension.")
    r = c.post("/chat/send", data={"csrf": "tok", "text": "what is wrong with me"})
    assert "err=" in r.headers["location"]


def test_chat_needs_the_local_model(env):
    c, _, _ = env
    signed_in(c)
    assert 'href="/chat"' not in c.get("/").text
    assert "OLLAMA_URL" in c.get("/chat").text
    assert "err=" in c.post("/chat/send", data={"csrf": "tok", "text": "hi"}).headers["location"]


def test_chat_action_checks():
    from app import chat
    assert chat.check_action({"kind": "none"}, "hi") is None
    a = chat.check_action({"kind": "log_bp", "systolic": 128, "diastolic": 84}, "bp 128/84")
    assert a["summary"] == "Save blood pressure 128/84, now"
    for bad, text in [({"kind": "log_bp", "systolic": 84, "diastolic": 128}, "bp 84 128"),
                      ({"kind": "log_weight", "kg": 9}, "weight 9"),
                      ({"kind": "log_meal", "name": "x", "kcal": 5}, "I ate x"),
                      ({"kind": "log_meal", "name": "x", "kcal": 9000}, "I ate x"),
                      ({"kind": "delete_everything"}, "hi"), ({"kind": "add_shopping", "items": []}, "add")]:
        with pytest.raises(ValueError):
            chat.check_action(bad, text)
    f = {"steps": {"daily_average": 7909}, "blood_pressure": {"latest_sitting_summary": "x", "latest_sitting_mixed": True}}
    assert chat.reply_problems("Steps averaged 7909 a day.", f, "steps?", None) == []
    assert any("not in the facts" in p for p in chat.reply_problems("You did 9,999 steps.", f, "steps?", None))
    assert "claims to have saved" in chat.reply_problems("I have saved it.", f, "log", None)
    assert "alarming blood-pressure wording" in chat.reply_problems("Your blood pressure readings are high. GP.", f, "bp", None)


def test_chat_mealie_actions_resolve_first_then_run_on_confirm(tmp_path):
    from app import chat
    from app.store import Store

    class FakeMealie:
        added = None
        def search(self, q, n): return [{"slug": "lentil-curry"}] if "lentil" in q else []
        def recipe(self, slug):
            return {"slug": slug, "name": "Lentil curry", "servings": 2,
                    "nutrition_per_serving": {"kcal": 410, "protein_g": 20, "carbs_g": 50, "fat_g": 9, "salt_g": 1.1}}
        def shopping_lists(self): return [{"id": "abc123", "name": "Weekly"}]
        def add_shopping_items(self, lid, items):
            FakeMealie.added = (lid, items)
            return {"added": len(items)}

    st = web.Stores(str(tmp_path), "khushal", "Europe/London").get("khushal")
    m = FakeMealie()
    a = chat.resolve(m, chat.check_action({"kind": "log_recipe", "recipe": "lentil curry", "servings": 1}, "log lentil curry"))
    assert a["summary"] == "Log 1 serving(s) of Lentil curry, about 410 kcal" and not st.food_entries(1)
    assert "Logged Lentil curry" in chat.execute(st, m, a) and st.food_entries(1)
    s = chat.resolve(m, chat.check_action({"kind": "add_shopping", "items": ["milk", "eggs"]}, "add milk and eggs"))
    assert 'milk, eggs' in s["summary"] and FakeMealie.added is None
    assert chat.execute(st, m, s) == "Added 2 item(s) to Weekly" and FakeMealie.added == ("abc123", ["milk", "eggs"])
    with pytest.raises(Exception):
        chat.resolve(m, chat.check_action({"kind": "log_recipe", "recipe": "pizza"}, "log pizza"))
    with pytest.raises(Exception):
        chat.resolve(None, s)


def test_refreshed_today_page_has_summary_chips_actions_and_plain_bp_status(env):
    c, stores, _ = env
    signed_in(c)
    k = stores.get("khushal")
    k.log_blood_pressure(128, 104, 70, None, None)
    page = c.get("/").text
    assert 'class="card hero2"' in page and 'href="/sleep"' in page and 'href="/food"' in page
    assert "Bottom number over" in page                       # top 128 is under 135, bottom 104 is over 85
    assert 'href="/log"' in page and "Ask coach" not in page   # no local model, so no coach button


def test_chat_only_demands_the_gp_when_the_reply_talks_about_readings():
    from app import chat
    f = {"blood_pressure": {"latest_sitting_summary": "x", "latest_sitting_mixed": False}}
    assert chat.reply_problems("I can answer questions about your numbers, including blood pressure, and log things after you confirm.", f, "what can you do", None) == []
    assert "blood pressure without the GP" in chat.reply_problems("Your blood pressure trend is improving.", f, "bp?", None)
    assert "blood pressure without the GP" in chat.reply_problems("Your last sitting was 120/93.", f, "bp?", None)
    assert chat.reply_problems("Your last sitting was 120/93. Discuss it with your GP.", f, "120/93", None) == []


def test_chat_estimated_meal_is_editable_marked_and_reuses_your_earlier_entry(tmp_path, monkeypatch):
    import json
    from app import chat
    ans = chat_answer("That is about 290 kcal, an estimate. Edit it if you like, then press Confirm.",
                      kind="log_meal", name="2 bananas and tea with milk", kcal=290, estimate=True)
    c, stores, calls = ai_env(tmp_path, monkeypatch, [ans])
    k = stores.get("khushal")
    a = chat.check_action(ans["action"], "I ate 2 bananas and tea with 200 ml milk")
    assert a["estimate"] and a["name"].endswith("(estimate)") and a["kcal"] == 290
    c.post("/chat/send", data={"csrf": "tok", "text": "I ate 2 bananas and tea with 200 ml milk"})
    page = c.get("/chat").text
    assert 'name="kcal"' in page and 'value="290"' in page and "estimate" in page and not k.food_entries(1)
    pid = json.loads(k.get_setting("chat_pending"))["id"]
    bad = c.post("/chat/confirm", data={"csrf": "tok", "id": pid, "kcal": "abc"})
    assert "err=" in bad.headers["location"] and not k.food_entries(1) and k.get_setting("chat_pending")
    ok = c.post("/chat/confirm", data={"csrf": "tok", "id": pid, "kcal": "275"})
    assert "ok=" in ok.headers["location"]
    e = k.food_entries(1)[-1]
    assert e["kcal"] == 275 and e["name"] == "2 bananas and tea with milk (estimate)"
    again = chat.reuse_history(k, chat.check_action(ans["action"], "same again"))
    assert again["kcal"] == 275 and again["from_history"] and "before" in again["summary"]
    typed = chat.check_action({"kind": "log_meal", "name": "Toast", "kcal": 180}, "toast 180 kcal")
    assert typed["estimate"] is False and typed["name"] == "Toast"


def test_chat_facts_hold_the_calories_left_so_the_model_need_not_calculate(tmp_path):
    from app import chat
    from app.store import Store
    st = Store(str(tmp_path / "c.db"), tz="Europe/London")
    st.set_setting("kcal_goal", "1500")
    st.set_setting("protein_goal", "80")
    st.log_meal("Bananas and tea", 300, 4.0, None, None, None, None, None, None)
    f = chat.chat_facts(st, None)
    assert f["food_today_total_kcal"] == 300 and f["kcal_left_today"] == 1200 and f["protein_left_today_g"] == 76
    assert chat.reply_problems("You have 1200 kcal left today.", f, "Should I eat more today?", None) == []
    assert any("not in the facts" in p for p in chat.reply_problems("You have 1001 kcal left.", f, "x", None))
    st.set_fasting(st.local(st.now()).date(), True)
    g = chat.chat_facts(st, None)
    assert "today_is_fasting_day" in g and "kcal_left_today" not in g

def test_chat_allows_food_arithmetic_but_not_for_blood_pressure(tmp_path):
    from app import chat
    from app.store import Store
    st = Store(str(tmp_path / "d.db"), tz="Europe/London")
    st.set_setting("kcal_goal", "1500")
    st.log_meal("Toast", 300, 8.0, None, None, None, None, None, None)
    menu = [{"id": "p1", "meal": "dinner", "recipe_name": "Dal", "_rec": {"name": "Dal"}, "per_serving": {"kcal": 620}}]
    f = chat.chat_facts(st, menu)
    assert f["planned_meals_not_yet_eaten_kcal"] == 620 and f["kcal_left_after_planned_meals"] == 580
    ok = lambda t, u="x": chat.reply_problems(t, f, u, None)
    assert ok("After the dal you would have 580 kcal left.") == []
    assert ok("A 250 kcal snack would leave you about 330.", "I might have a 250 kcal snack") == []   # 580 - 250
    assert ok("You have 920 kcal planned and eaten.") == []                                           # 300 + 620
    assert any("not in the facts" in p for p in ok("You have 777 kcal left."))
    f["blood_pressure"] = {"latest_sitting_summary": "x", "latest_sitting_mixed": False}
    assert any("not in the facts" in p for p in ok("Your blood pressure is 920 over the GP threshold."))
