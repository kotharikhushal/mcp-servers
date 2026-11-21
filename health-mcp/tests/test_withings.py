import json
from datetime import datetime, timezone

import httpx
import pytest

from app import withings
from app.store import Store

NOW = 1_800_000_000          # a fixed moment, in Unix seconds
TZ = "Europe/London"


def store():
    return Store(":memory:", tz=TZ)


def grp(date, sys_, dia, pulse=None, category=1, unit=0):
    ms = [{"value": sys_, "type": 10, "unit": unit}, {"value": dia, "type": 9, "unit": unit}]
    if pulse is not None:
        ms.append({"value": pulse, "type": 11, "unit": 0})
    return {"grpid": date, "date": date, "category": category, "measures": ms}


class FakeWithings:
    """Stands in for api.withings.net: records every call and answers from queues."""
    def __init__(self, groups=None, updatetime=NOW):
        self.calls, self.groups, self.updatetime = [], groups or [], updatetime
        self.refreshes, self.bad_token_once, self.refuse_refresh = 0, False, False

    def __call__(self, request):
        data = dict(httpx.QueryParams(request.content.decode()).items())
        self.calls.append((str(request.url), data, request.headers.get("authorization")))
        if request.url.path == "/v2/oauth2":
            if data["grant_type"] == "refresh_token":
                if self.refuse_refresh:
                    return httpx.Response(200, json={"status": 293, "error": "invalid refresh token"})
                self.refreshes += 1
                return httpx.Response(200, json={"status": 0, "body": {
                    "userid": "u1", "access_token": f"access{self.refreshes}",
                    "refresh_token": f"refresh{self.refreshes}", "expires_in": 10800}})
            return httpx.Response(200, json={"status": 0, "body": {
                "userid": "u1", "access_token": "access0", "refresh_token": "refresh0", "expires_in": 10800}})
        if self.bad_token_once:
            self.bad_token_once = False
            return httpx.Response(200, json={"status": 343, "error": "invalid token"})
        return httpx.Response(200, json={"status": 0, "body": {
            "updatetime": self.updatetime, "timezone": "Europe/London", "measuregrps": self.groups}})


def client(fake):
    return withings.Withings("cid", "csecret", "https://wellness.example.com/withings/callback",
                             httpx.Client(transport=httpx.MockTransport(fake)))


def connect(s, c):
    withings.save_tokens(s, c.exchange("code"))
    s.set_setting(withings.TOKENS, json.dumps({**json.loads(s.get_setting(withings.TOKENS)), "expires_at": NOW + 7200}))


def test_parse_groups_reads_value_times_power_of_ten_and_skips_goals_and_partial_groups():
    groups = [grp(NOW, 1181, 801, 72, unit=-1),                 # 118.1 / 80.1
              grp(NOW + 60, 120, 80),                           # no pulse is fine
              grp(NOW + 120, 130, 85, category=2),              # a goal, not a measurement
              {"grpid": 4, "date": NOW, "category": 1, "measures": [{"value": 70, "type": 11, "unit": 0}]},
              {"grpid": 5, "date": NOW, "measures": [{"type": 10}]}, "junk", None]
    got = withings.parse_groups(groups)
    assert [(r["systolic"], r["diastolic"], r["pulse"]) for r in got] == [(118, 80, 72), (120, 80, None)]
    assert got[0]["time"] == datetime.fromtimestamp(NOW, timezone.utc)
    assert withings.parse_groups(None) == []


def test_authorize_url_asks_only_for_measurements_and_carries_the_state():
    url = client(FakeWithings()).authorize_url("abc")
    assert url.startswith("https://account.withings.com/oauth2_user/authorize2?")
    assert "scope=user.metrics" in url and "state=abc" in url and "client_id=cid" in url
    assert "redirect_uri=https%3A%2F%2Fwellness.example.com%2Fwithings%2Fcallback" in url


def test_state_works_once_expires_and_must_match():
    s, c = store(), client(FakeWithings())
    withings.begin(s, c, now=1000)
    state = s.get_setting(withings.STATE).split("|")[0]
    assert withings.check_state(s, "wrong", now=1001) is False
    assert withings.check_state(s, state, now=1001) is False            # the failed try used it up
    withings.begin(s, c, now=1000)
    state = s.get_setting(withings.STATE).split("|")[0]
    assert withings.check_state(s, state, now=1001) is True
    assert withings.check_state(s, state, now=1002) is False            # one use only
    withings.begin(s, c, now=1000)
    state = s.get_setting(withings.STATE).split("|")[0]
    assert withings.check_state(s, state, now=1000 + 11 * 60) is False  # too old
    assert withings.check_state(s, "", now=1000) is False


def test_first_sync_adds_readings_marked_withings_and_a_repeat_adds_nothing():
    s, fake = store(), FakeWithings([grp(NOW - 600, 118, 79, 70), grp(NOW - 540, 122, 81, 72)])
    c = client(fake)
    connect(s, c)
    assert withings.sync(s, c, now=NOW) == {"connected": True, "added": 2, "seen": 2}
    rows = s._rows("SELECT systolic,diastolic,pulse,note,source FROM blood_pressure ORDER BY time")
    assert [(r["systolic"], r["diastolic"], r["pulse"], r["note"], r["source"]) for r in rows] == \
        [(118, 79, 70, "Withings", "withings"), (122, 81, 72, "Withings", "withings")]
    assert withings.sync(s, c, now=NOW + 900)["added"] == 0                 # the same readings again
    first, second = fake.calls[-2], fake.calls[-1]
    assert "startdate" in first[1] and "lastupdate" not in first[1]          # first time: a window
    assert second[1]["lastupdate"] == str(NOW) and "startdate" not in second[1]   # then only what changed
    assert first[2] == "Bearer access0" and first[1]["meastypes"] == "10,9,11"
    assert s.get_setting(withings.ERROR) is None and s.get_setting(withings.LAST_SYNC)


def test_an_expiring_token_is_renewed_and_the_new_refresh_token_is_kept():
    s, fake = store(), FakeWithings([grp(NOW - 60, 118, 79, 70)])
    c = client(fake)
    connect(s, c)
    s.set_setting(withings.TOKENS, json.dumps({"access": "old", "refresh": "refresh0", "expires_at": NOW + 60}))
    assert withings.sync(s, c, now=NOW)["added"] == 1
    saved = json.loads(s.get_setting(withings.TOKENS))
    assert saved["access"] == "access1" and saved["refresh"] == "refresh1" and fake.refreshes == 1
    assert fake.calls[-1][2] == "Bearer access1"


def test_a_token_refused_early_is_renewed_once_and_retried():
    s, fake = store(), FakeWithings([grp(NOW - 60, 118, 79, 70)])
    c = client(fake)
    connect(s, c)
    fake.bad_token_once = True
    assert withings.sync(s, c, now=NOW)["added"] == 1 and fake.refreshes == 1


def test_a_refused_refresh_asks_the_person_to_connect_again_and_keeps_their_readings():
    s, fake = store(), FakeWithings([grp(NOW - 60, 118, 79, 70)])
    c = client(fake)
    connect(s, c)
    withings.sync(s, c, now=NOW)
    s.set_setting(withings.TOKENS, json.dumps({"access": "old", "refresh": "dead", "expires_at": NOW - 5}))
    fake.refuse_refresh = True
    out = withings.sync(s, c, now=NOW + 5)
    assert out["error"] == "Withings needs to be connected again" and out["added"] == 0
    assert withings.status(s)["error"] == "Withings needs to be connected again"
    assert len(s.bp_readings(10000)) == 1


def test_a_network_failure_is_reported_without_raising_and_clears_when_it_works_again():
    s, fake = store(), FakeWithings()
    c = client(fake)
    connect(s, c)
    real = c.http
    c.http = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down"))))
    assert withings.sync(s, c, now=NOW)["error"] == "Could not reach Withings"
    c.http = real
    assert withings.sync(s, c, now=NOW + 900).get("error") is None and withings.status(s)["error"] is None


def test_sync_does_nothing_when_not_connected_and_garbled_tokens_are_handled():
    s, c = store(), client(FakeWithings())
    assert withings.sync(s, c) == {"connected": False, "added": 0}
    s.set_setting(withings.TOKENS, "not json")
    assert withings.sync(s, c)["error"] == "Withings needs to be connected again"


def test_disconnect_forgets_the_tokens_but_not_the_readings():
    s, fake = store(), FakeWithings([grp(NOW - 60, 118, 79, 70)])
    c = client(fake)
    connect(s, c)
    withings.sync(s, c, now=NOW)
    withings.disconnect(s)
    assert not withings.connected(s) and s.get_setting(withings.LAST_UPDATE) is None
    assert len(s.bp_readings(10000)) == 1


def test_paging_follows_more_and_offset_and_implausible_numbers_are_skipped():
    s = store()
    pages = [{"status": 0, "body": {"updatetime": NOW, "more": 1, "offset": 5, "measuregrps": [grp(NOW - 120, 118, 79, 70)]}},
             {"status": 0, "body": {"updatetime": NOW, "more": 0, "measuregrps": [grp(NOW - 60, 400, 79, 70), grp(NOW - 30, 121, 80, 999)]}}]
    seen = []

    def handler(request):
        seen.append(request.content.decode())
        return httpx.Response(200, json=pages[len(seen) - 1])
    c = withings.Withings("cid", "csecret", "https://x/withings/callback", httpx.Client(transport=httpx.MockTransport(handler)))
    withings.save_tokens(s, {"access": "a", "refresh": "r", "expires_at": NOW + 7200})
    out = withings.sync(s, c, now=NOW)
    assert "offset=5" in seen[1] and out["seen"] == 3 and out["added"] == 2   # 400 mmHg is dropped
    rows = s._rows("SELECT systolic,pulse FROM blood_pressure ORDER BY time")
    assert [(r["systolic"], r["pulse"]) for r in rows] == [(118, 70), (121, None)]  # a silly pulse is dropped, the reading kept


def test_client_from_env_needs_both_values():
    assert withings.client_from_env({}, "https://x") is None
    assert withings.client_from_env({"WITHINGS_CLIENT_ID": "a"}, "https://x") is None
    assert withings.client_from_env({"WITHINGS_CLIENT_ID": "a", "WITHINGS_CLIENT_SECRET": "b"}, "https://x") is not None
