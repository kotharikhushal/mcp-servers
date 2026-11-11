from datetime import datetime, timezone

import pytest

from app.store import Store, parse_ts
from app.tools import bp_stats

UTC = timezone.utc
NOW = datetime(2026, 11, 10, 14, 0, tzinfo=UTC)


def make(tz="Europe/London", now=NOW):
    return Store(":memory:", tz=tz, now=lambda: now)


PAYLOAD = {
    "timestamp": "2026-11-10T13:59:00Z",
    "app_version": "1.2.3",
    "steps": [
        {"count": 500, "start_time": "2026-11-10T10:00:00Z", "end_time": "2026-11-10T10:30:00Z"},
        {"count": 480, "start_time": "2026-11-10T10:00:00Z", "end_time": "2026-11-10T10:30:00Z"},
        {"count": 600, "start_time": "2026-11-10T11:00:00Z", "end_time": "2026-11-10T11:30:00Z"},
    ],
    "heart_rate": [{"bpm": 60, "time": "2026-11-10T03:00:00Z"}, {"bpm": 80, "time": "2026-11-10T09:00:00Z"},
                   {"bpm": 100, "time": "2026-11-10T12:00:00Z"}],
    "sleep": [{
        "session_end_time": "2026-11-10T06:30:00Z", "duration_seconds": 28800,
        "stages": [
            {"stage": "STAGE_TYPE_LIGHT", "start_time": "2026-11-09T22:30:00Z", "end_time": "2026-11-10T01:30:00Z", "duration_seconds": 10800},
            {"stage": "STAGE_TYPE_DEEP", "start_time": "2026-11-10T01:30:00Z", "end_time": "2026-11-10T03:30:00Z", "duration_seconds": 7200},
            {"stage": "STAGE_TYPE_REM", "start_time": "2026-11-10T03:30:00Z", "end_time": "2026-11-10T05:30:00Z", "duration_seconds": 7200},
            {"stage": "STAGE_TYPE_AWAKE", "start_time": "2026-11-10T05:30:00Z", "end_time": "2026-11-10T06:30:00Z", "duration_seconds": 3600},
        ]}],
    "weight": [{"kilograms": 78.2, "time": "2026-11-10T07:00:00Z"},
               {"kilograms": 78.2, "time": "2026-11-10T07:02:00Z"}],  # same weigh-in via 2 apps
    "blood_pressure": [{"systolic": 150, "diastolic": 100, "time": "2026-11-09T08:00:00Z"},
                       {"systolic": 140, "diastolic": 90, "time": "2026-11-10T08:00:00Z"},
                       {"systolic": 130, "diastolic": 84, "time": "2026-11-10T19:00:00Z"}],
    "nutrition": [{"start_time": "2026-11-10T08:00:00Z", "calories": 410, "protein_grams": 10,
                   "sodium_grams": 0.48, "name": "Poha"}],
}


def test_ingest_is_idempotent():
    s = make()
    c1 = s.ingest(PAYLOAD)
    assert c1["steps"]["stored"] == 3 and c1["weight"]["stored"] == 2
    c2 = s.ingest(PAYLOAD)
    assert all(v["stored"] == 0 for v in c2.values())
    assert s.status()["tables"]["steps"]["rows"] == 3


def test_steps_dedupe_overlapping_sources():
    s = make()
    s.ingest(PAYLOAD)
    d = list(s.steps_by_day(1).values())[0]
    assert d["steps"] == 1100          # 500 (watch) + 600; the 480 phone duplicate is dropped
    assert d["raw_sum"] == 1580


def test_rejects_bad_values():
    s = make()
    c = s.ingest({
        "heart_rate": [{"bpm": 300, "time": "2026-11-10T03:00:00Z"}, {"bpm": 70, "time": "nope"}],
        "weight": [{"kilograms": 5, "time": "2026-11-10T07:00:00Z"}],
        "blood_pressure": [{"systolic": 80, "diastolic": 120, "time": "2026-11-10T07:00:00Z"}],
        "steps": [{"count": -4, "start_time": "2026-11-10T07:00:00Z", "end_time": "2026-11-10T07:10:00Z"}],
        "sleep": "not a list",
    })
    assert all(v["stored"] == 0 and v["rejected"] == v["received"] for v in c.values())
    assert s.status()["tables"]["heart_rate"]["rows"] == 0


def test_sleep_stages_and_update():
    s = make()
    s.ingest(PAYLOAD)
    day = list(s.sleep_by_day(1).values())[0]
    assert day["asleep_seconds"] == 25200          # awake time excluded
    assert day["awake_seconds"] == 3600
    assert day["stages"]["deep"] == 7200 and day["stages"]["rem"] == 7200
    # the same session arrives again with more detail: updated, not duplicated
    p2 = {"sleep": [{"session_end_time": "2026-11-10T06:30:00Z", "duration_seconds": 28800, "stages": []}]}
    assert s.ingest(p2)["sleep"]["stored"] == 1
    assert s.status()["tables"]["sleep"]["rows"] == 1


def test_weight_merges_near_duplicates():
    s = make()
    s.ingest(PAYLOAD)
    assert len(s.weight_readings(1)) == 1


def test_heart_rate_stats():
    s = make()
    s.ingest(PAYLOAD)
    h = list(s.heart_by_day(1).values())[0]
    assert (h["min"], h["max"], h["samples"]) == (60, 100, 3) and h["avg"] == 80.0


def test_manual_logs_and_delete_rules():
    s = make()
    s.ingest(PAYLOAD)
    r = s.log_blood_pressure(148, 98, 72, None, "morning, NHS cuff")
    assert r["systolic"] == 148
    with pytest.raises(ValueError):
        s.log_blood_pressure(148, 98, 72, None, "again")          # identical reading at same instant
    with pytest.raises(ValueError):
        s.log_blood_pressure(90, 120, None, None, None)
    synced_id = [x["id"] for x in s.bp_readings(7) if x["source"] == "sync"][0]
    assert s.delete_manual("blood_pressure", synced_id) is False   # synced data is protected
    assert s.delete_manual("blood_pressure", r["id"]) is True
    with pytest.raises(ValueError):
        s.delete_manual("steps", 1)


def test_meal_salt_round_trip():
    s = make()
    s.log_meal("Sausages (estimate)", 112, 6.6, 8.7, 4.6, 4.8, 0.4, 1.2, None)
    e = s.food_entries(1)[0]
    assert e["kcal"] == 112 and e["salt_g"] == 1.2 and e["source"] == "manual"
    synced = s.ingest(PAYLOAD)
    assert synced["nutrition"]["stored"] == 1
    poha = [x for x in s.food_entries(1) if x["name"] == "Poha"][0]
    assert poha["salt_g"] == 1.2                                     # 0.48 g sodium = 1.2 g salt


def test_bp_stats_excludes_first_day():
    s = make()
    s.ingest(PAYLOAD)
    st = bp_stats(s.bp_readings(7))
    assert st["average_all"]["systolic"] == 140.0
    assert st["average_excluding_first_day"]["systolic"] == 135.0   # (140+130)/2
    assert st["days_with_readings"] == 2


def test_local_day_grouping_uses_timezone():
    # 23:30 UTC on 9 Nov is already 10 Nov (12:30) in Auckland
    s = make(tz="Pacific/Auckland", now=datetime(2026, 11, 10, 2, 0, tzinfo=UTC))
    s.ingest({"steps": [{"count": 100, "start_time": "2026-11-09T23:30:00Z", "end_time": "2026-11-09T23:40:00Z"}]})
    days = s.steps_by_day(1)
    assert list(days)[0].isoformat() == "2026-11-10" and list(days.values())[0]["steps"] == 100


def test_status_stale_and_fresh():
    s = make()
    assert s.status()["stale"] is True
    s.ingest(PAYLOAD)
    st = s.status()
    assert st["stale"] is False and st["tables"]["steps"]["rows"] == 3


def test_parse_ts_variants():
    assert parse_ts("2026-11-10T10:00:00.123456789Z").hour == 10
    assert parse_ts("2026-11-10T10:00:00+01:00").hour == 9
    assert parse_ts(1_780_000_000_000).year >= 2026
    assert parse_ts("garbage") is None and parse_ts(True) is None


WORKOUT_PAYLOAD = {
    "exercise": [{"type": "EXERCISE_TYPE_RUNNING_TREADMILL", "start_time": "2026-11-10T13:25:00Z",
                  "end_time": "2026-11-10T13:55:00Z", "duration_seconds": 1800,
                  "distance_meters": 3500.0, "steps": 4100, "avg_cadence_spm": 160.0}],
    "heart_rate": [{"bpm": 120, "time": "2026-11-10T13:30:00Z"}, {"bpm": 160, "time": "2026-11-10T13:45:00Z"},
                   {"bpm": 70, "time": "2026-11-10T09:00:00Z"}],
    "active_calories": [
        {"calories": 250.0, "start_time": "2026-11-10T13:25:00Z", "end_time": "2026-11-10T13:55:00Z"},
        {"calories": 250.0, "start_time": "2026-11-10T13:25:00Z", "end_time": "2026-11-10T13:55:00Z"},  # dup
        {"calories": 400.0, "start_time": "2026-11-10T00:00:00Z", "end_time": "2026-11-10T14:00:00Z"}],
    "total_calories": [{"calories": 1500.0, "start_time": "2026-11-10T00:00:00Z", "end_time": "2026-11-10T13:00:00Z"},
                       {"calories": 1650.0, "start_time": "2026-11-10T00:00:00Z", "end_time": "2026-11-10T14:00:00Z"}],
}


def test_workouts_ingest_and_stats():
    s = make()
    c = s.ingest(WORKOUT_PAYLOAD)
    assert c["exercise"]["stored"] == 1
    assert c["active_calories"]["stored"] == 2          # exact duplicate ignored
    w = s.workouts(1)
    assert len(w) == 1
    w = w[0]
    assert w["type"] == "running treadmill" and w["minutes"] == 30 and w["distance_km"] == 3.5
    assert (w["avg_hr"], w["max_hr"]) == (140, 160)     # only samples inside the session
    assert w["active_kcal_estimate"] == 250             # the all-day record is not prorated into it
    assert w["avg_cadence_spm"] == 160.0
    # idempotent, and a later edit of the same session (distance added) updates it
    assert s.ingest(WORKOUT_PAYLOAD)["exercise"]["stored"] == 0
    edited = {"exercise": [dict(WORKOUT_PAYLOAD["exercise"][0], distance_meters=3600.0)]}
    assert s.ingest(edited)["exercise"]["stored"] == 1
    assert s.workouts(1)[0]["distance_km"] == 3.6
    assert s.status()["tables"]["exercise"]["rows"] == 1


def test_energy_by_day_dedupes_cumulative_records():
    s = make()
    s.ingest(WORKOUT_PAYLOAD)
    e = list(s.energy_by_day(1).values())[0]
    assert e["total_kcal"] == 1650                      # the later, larger cumulative record only
    assert e["active_kcal"] == 400                       # the 250 record sits inside the 400 one


def test_bad_exercise_rows_rejected():
    s = make()
    c = s.ingest({"exercise": [{"type": "RUNNING", "start_time": "2026-11-10T10:00:00Z",
                                "end_time": "2026-11-10T09:00:00Z", "duration_seconds": 60},
                               {"type": "RUNNING", "start_time": "nope", "end_time": "nope"}],
                  "active_calories": [{"calories": -5, "start_time": "2026-11-10T10:00:00Z",
                                       "end_time": "2026-11-10T11:00:00Z"}]})
    assert c["exercise"]["rejected"] == 2 and c["active_calories"]["rejected"] == 1


def test_manual_weight_logging_and_delete():
    s = make()
    r = s.log_weight(76.1, datetime(2026, 11, 10, 8, 0, tzinfo=UTC))
    assert r["kg"] == 76.1
    with pytest.raises(ValueError):
        s.log_weight(76.1, datetime(2026, 11, 10, 8, 0, tzinfo=UTC))
    with pytest.raises(ValueError):
        s.log_weight(5, None)
    s.ingest({"weight": [{"kilograms": 77.85, "time": "2026-11-10T17:37:00Z"}]})
    w = s.weight_readings(1)
    assert [(x["kg"], x["source"]) for x in w] == [(76.1, "manual"), (77.85, "sync")]
    assert s.delete_manual("weight", w[1]["id"]) is False      # synced data is protected
    assert s.delete_manual("weight", w[0]["id"]) is True


def test_old_database_gets_weight_source_column(tmp_path):
    import sqlite3
    p = str(tmp_path / "old.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE weight(id INTEGER PRIMARY KEY, time TEXT NOT NULL, kg REAL NOT NULL, UNIQUE(time, kg))")
    c.execute("INSERT INTO weight(time,kg) VALUES('2026-11-10T17:37:00Z', 77.85)")
    c.commit(); c.close()
    s = Store(p, now=lambda: NOW)
    assert s.weight_readings(2)[0]["source"] == "sync"


def test_numeric_exercise_types_get_names_without_duplicating_rows():
    s = make()
    sess = {"type": 57, "start_time": "2026-11-10T13:25:00Z", "end_time": "2026-11-10T13:55:00Z",
            "duration_seconds": 1800}
    s.ingest({"exercise": [sess]})
    assert s.ingest({"exercise": [sess]})["exercise"]["stored"] == 0   # resent window: no duplicate
    assert s.workouts(1)[0]["type"] == "treadmill running"
    from app.store import exercise_label
    assert exercise_label("79") == "walking" and exercise_label("9999") == "workout (type 9999)"
    assert exercise_label("running") == "running"


def test_workout_energy_from_fifteen_minute_records():
    s = make()
    recs = []
    for i in range(40):  # a quiet day: 1.0 kcal/min in 15-minute records
        h, m = divmod(i * 15, 60)
        recs.append({"calories": 15.0, "start_time": f"2026-11-10T{h:02d}:{m:02d}:00Z",
                     "end_time": f"2026-11-10T{(i * 15 + 15) // 60:02d}:{(i * 15 + 15) % 60:02d}:00Z"})
    # the workout hour 12:00-13:00 burns 4 kcal/min
    recs = [r for r in recs if not r["start_time"].startswith(("12:", "T12"))]
    recs = [r for r in recs if r["start_time"][11:13] != "12"]
    for k in range(4):
        recs.append({"calories": 60.0, "start_time": f"2026-11-10T12:{k * 15:02d}:00Z",
                     "end_time": f"2026-11-10T12:{k * 15 + 15:02d}:00Z" if k < 3 else "2026-11-10T13:00:00Z"})
    s.ingest({"total_calories": recs, "exercise": [{"type": 79, "start_time": "2026-11-10T12:00:00Z",
                                                     "end_time": "2026-11-10T13:00:00Z", "duration_seconds": 3600}]})
    w = s.workouts(1)[0]
    assert w["kcal_total_during"] == 240
    assert w["kcal_above_resting_estimate"] == 180       # 240 minus 60 min at ~1 kcal/min


def test_sleep_session_resent_longer_is_not_double_counted():
    # The watch first reports a night ending 07:49, then the same night extended to 09:01.
    s = make(now=datetime(2026, 11, 11, 12, 0, tzinfo=UTC))
    s.ingest({"sleep": [{"session_end_time": "2026-11-11T07:49:00Z", "duration_seconds": 31740, "stages": []}]})
    s.ingest({"sleep": [{"session_end_time": "2026-11-11T09:01:00Z", "duration_seconds": 36060, "stages": []}]})
    assert s.status()["tables"]["sleep"]["rows"] == 2
    day = s.sleep_by_day(1)[datetime(2026, 11, 11, tzinfo=UTC).date()]
    assert day["sessions"] == 1 and day["asleep_seconds"] == 36060


def test_sleep_night_and_nap_are_both_counted():
    s = make(now=datetime(2026, 11, 9, 20, 0, tzinfo=UTC))
    s.ingest({"sleep": [
        {"session_end_time": "2026-11-09T07:49:00Z", "duration_seconds": 19920, "stages": []},
        {"session_end_time": "2026-11-09T17:42:00Z", "duration_seconds": 4860, "stages": []}]})
    day = s.sleep_by_day(1)[datetime(2026, 11, 9, tzinfo=UTC).date()]
    assert day["sessions"] == 2 and day["asleep_seconds"] == 19920 + 4860
