import io
import json
import zipfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app import apple
from app.store import Store

TZ = ZoneInfo("Europe/London")
S = " "  # narrow no-break space, as the app writes it
NOW = datetime(2026, 11, 11, 13, 0, tzinfo=timezone.utc)

EXPORT = {"data": {
    "metrics": [
        {"name": "step_count", "units": "count", "data": [
            {"date": f"2026-11-10 12:00:00{S}am +0000", "qty": 9185, "source": "Watch"},
            {"date": f"2026-11-11 12:00:00{S}am +0000", "qty": 991, "source": "Watch"}]},
        {"name": "active_energy", "units": "kJ", "data": [{"date": f"2026-11-10 12:00:00{S}am +0000", "qty": 2092}]},
        {"name": "basal_energy_burned", "units": "kJ", "data": [{"date": f"2026-11-10 12:00:00{S}am +0000", "qty": 6276}]},
        {"name": "heart_rate", "units": "count/min", "data": [
            {"date": f"2026-11-10 12:00:00{S}am +0000", "Min": 52, "Avg": 65, "Max": 89}]},
        {"name": "resting_heart_rate", "units": "count/min", "data": [{"date": f"2026-11-10 12:00:00{S}am +0000", "qty": 60}]},
        {"name": "weight_body_mass", "units": "kg", "data": [{"date": f"2026-11-10 12:00:00{S}am +0000", "qty": 58.9}]},
        {"name": "sleep_analysis", "units": "hr", "data": [{
            "sleepStart": f"2026-11-10 11:00:00{S}pm +0000", "sleepEnd": f"2026-11-11 09:00:00{S}am +0000",
            "core": 6.0, "deep": 1.5, "rem": 2.0, "awake": 0.5, "asleep": 0, "inBed": 0,
            "date": f"2026-11-11 12:00:00{S}am +0000"}]},
    ],
    "workouts": [{"name": "Outdoor Walk", "start": f"2026-11-10 1:00:00{S}pm +0000", "end": f"2026-11-10 2:00:00{S}pm +0000",
                  "duration": 3600, "distance": {"qty": 4.0, "units": "km"}, "avgHeartRate": {"qty": 95, "units": "count/min"},
                  "activeEnergyBurned": {"qty": 836.8, "units": "kJ"}}],
}}


def convert(doc=EXPORT):
    return apple.convert(doc["data"], TZ)


def test_time_parsing_handles_narrow_spaces_and_pm():
    t = apple.parse_apple_time(f"2026-10-17 1:34:45{S}pm +0100")
    assert (t.hour, t.minute, t.utcoffset().total_seconds()) == (13, 34, 3600)
    assert apple.parse_apple_time("nonsense") is None and apple.parse_apple_time(None) is None


def test_convert_maps_metrics_and_units():
    payload, heart = convert()
    assert [r["count"] for r in payload["steps"]] == [9185, 991]
    assert payload["active_calories"][0]["calories"] == 500.0          # 2092 kJ
    assert payload["total_calories"][0]["calories"] == 2000.0          # 500 active + 1500 basal
    assert payload["weight"][0]["kilograms"] == 58.9
    sl = payload["sleep"][0]
    assert sl["duration_seconds"] == 10 * 3600 and {s["stage"] for s in sl["stages"]} == {"light", "deep", "rem", "awake"}
    w = payload["exercise"][0]
    assert w["distance_meters"] == 4000 and w["avg_heart_rate"] == 95 and w["active_kcal"] == 200
    assert heart == [{"day": "2026-11-10", "min": 52, "avg": 65, "max": 89, "resting": 60}]


def test_import_into_store_gives_correct_sleep_steps_and_heart():
    s = Store(":memory:", now=lambda: NOW)
    payload, heart = convert()
    counts = s.ingest(payload)
    s.import_daily_heart(heart)
    day = s.sleep_by_day(2)[NOW.date()]
    assert day["asleep_seconds"] == 9.5 * 3600 and day["awake_seconds"] == 1800
    assert day["stages"]["light"] == 6 * 3600
    assert [v["steps"] for v in s.steps_by_day(2).values()] == [9185, 991]
    assert s.heart_by_day(2)[datetime(2026, 11, 10).date()]["resting"] == 60
    assert "new nights" in apple.summarise(counts, 1)


def test_reimport_is_idempotent_and_keeps_larger_step_count():
    s = Store(":memory:", now=lambda: NOW)
    payload, heart = convert()
    s.ingest(payload)
    again = s.ingest(payload)
    assert all(c["stored"] == 0 for c in again.values())
    bigger = json.loads(json.dumps(EXPORT))
    bigger["data"]["metrics"][0]["data"][1]["qty"] = 2500
    s.ingest(apple.convert(bigger["data"], TZ)[0])
    assert s.steps_by_day(1)[NOW.date()]["steps"] == 2500
    assert apple.summarise(again, 1) == "Imported nothing new (already up to date)"


def test_load_upload_accepts_json_and_zip_and_rejects_rubbish():
    raw = json.dumps(EXPORT).encode()
    assert apple.load_upload(raw)["metrics"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Route.gpx", "<gpx/>")
        z.writestr("HealthAutoExport-2026.json", raw)
    assert apple.load_upload(buf.getvalue())["workouts"]
    for bad in (b"not json", b'{"data": 5}', b"PK\x03\x04broken", b"[]"):
        with pytest.raises(apple.ImportError_):
            apple.load_upload(bad)
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("a.txt", "x")
    with pytest.raises(apple.ImportError_):
        apple.load_upload(empty.getvalue())
