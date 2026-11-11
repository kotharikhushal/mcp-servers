from datetime import datetime, timezone

from app import reminders
from app.store import Store


def make(now):
    s = Store(":memory:", now=lambda: now)
    s.set_setting("reminders_enabled", "1")
    s.set_setting("reminder_topic", "wellness-abc")
    return s


def at(h, m=0, d=11):
    return datetime(2026, 11, d, h, m, tzinfo=timezone.utc)  # London is UTC in November


def test_nothing_due_when_disabled_or_no_topic():
    s = Store(":memory:", now=lambda: at(8, 5))
    assert reminders.due_slots(s) == []
    s.set_setting("reminders_enabled", "1")
    assert reminders.due_slots(s) == []


def test_slot_due_inside_window_only():
    assert reminders.due_slots(make(at(7, 59))) == []
    assert reminders.due_slots(make(at(8, 0))) == ["morning"]
    assert reminders.due_slots(make(at(10, 59))) == ["morning"]
    assert reminders.due_slots(make(at(11, 0))) == []          # window closed
    assert reminders.due_slots(make(at(21, 30))) == ["evening"]


def test_custom_times_and_bad_values_fall_back():
    s = make(at(7, 30))
    s.set_setting("reminder_morning", "07:15")
    assert reminders.due_slots(s) == ["morning"]
    s.set_setting("reminder_morning", "garbage")
    assert reminders.due_slots(s) == []                          # falls back to 08:00


def test_skipped_when_reading_already_logged_in_that_half_of_day():
    s = make(at(8, 30))
    s.log_blood_pressure(150, 100, 70, at(7, 40), None)
    assert reminders.due_slots(s) == []
    s2 = make(at(21, 30))
    s2.log_blood_pressure(150, 100, 70, at(7, 40), None)         # a morning reading does not cover the evening
    assert reminders.due_slots(s2) == ["evening"]


def test_run_once_sends_once_and_retries_after_failure():
    s = make(at(8, 5))
    sent = []
    attempts = {}
    assert reminders.run_once({"u": s}, lambda t, ti, m: sent.append((t, ti, m)), attempts, at(8, 5)) == 1
    assert sent[0][0] == "wellness-abc" and "morning blood pressure" in sent[0][2]
    assert "mmHg" not in sent[0][2]
    assert reminders.run_once({"u": s}, lambda *a: sent.append(a), attempts, at(8, 6)) == 0   # already sent today

    s2 = make(at(8, 5))
    calls = []

    def flaky(t, ti, m):
        calls.append(1)
        raise RuntimeError("network down")
    attempts2 = {}
    assert reminders.run_once({"u": s2}, flaky, attempts2, at(8, 5)) == 0
    assert reminders.run_once({"u": s2}, flaky, attempts2, at(8, 6)) == 0
    assert len(calls) == 1                                       # throttled: one try per five minutes
    assert reminders.due_slots(s2, at(8, 6)) == ["morning"]      # still owed


def test_new_topic_is_unguessable_and_url_safe():
    a, b = reminders.new_topic(), reminders.new_topic()
    assert a != b and a.startswith("wellness-") and len(a) > 20


def test_first_attempt_is_not_throttled_even_just_after_boot(monkeypatch):
    # time.monotonic() counts from boot, so it can be tiny on a freshly started host
    monkeypatch.setattr(reminders.time, "monotonic", lambda: 12.0)
    s = make(at(8, 5))
    sent = []
    assert reminders.run_once({"u": s}, lambda t, ti, m: sent.append(m), {}, at(8, 5)) == 1
