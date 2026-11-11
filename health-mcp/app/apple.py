"""Convert a Health Auto Export (Apple Health) JSON export into the payload the store ingests.

The export holds daily summaries rather than individual samples, so steps become one record per
day, heart rate becomes a daily min/avg/max row, and sleep keeps its stage hours. Re-importing an
overlapping export is safe: the store ignores duplicates and keeps the larger step count per day.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import date, datetime, time as dtime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

MAX_JSON = 40 * 1024 * 1024
KJ_PER_KCAL = 4.184
_FORMATS = ("%Y-%m-%d %I:%M:%S %p %z", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z")


class ImportError_(ValueError):
    """The upload is not a usable Health Auto Export file."""


def parse_apple_time(value: Any) -> datetime | None:
    """'2026-10-12 12:00:00 am +0100' (with narrow no-break spaces) to an aware datetime."""
    if not isinstance(value, str):
        return None
    s = re.sub(r"[  ]", " ", value).strip()
    s = re.sub(r"\s+", " ", s)
    for fmt in _FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def load_upload(raw: bytes) -> dict[str, Any]:
    """The export's JSON, from either the .json itself or the .zip the app produces."""
    if raw[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                names = [i for i in z.infolist()
                         if i.filename.lower().endswith(".json") and not i.filename.startswith("__MACOSX")]
                if not names:
                    raise ImportError_("The zip has no .json file in it")
                info = max(names, key=lambda i: i.file_size)
                if info.file_size > MAX_JSON:
                    raise ImportError_("The export is too large")
                with z.open(info) as f:
                    raw = f.read(MAX_JSON + 1)
        except zipfile.BadZipFile as e:
            raise ImportError_("That file is not a valid zip") from e
    if len(raw) > MAX_JSON:
        raise ImportError_("The export is too large")
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as e:
        raise ImportError_("That file is not valid JSON") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("data"), dict):
        raise ImportError_("This does not look like a Health Auto Export file")
    return doc["data"]


def _qty(item: Any) -> float | None:
    v = item.get("qty") if isinstance(item, dict) else item
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _kcal(qty: float | None, units: str | None) -> float | None:
    if qty is None:
        return None
    return qty if (units or "").lower().startswith("kcal") else qty / KJ_PER_KCAL


def _iso(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def convert(data: dict[str, Any], tz: ZoneInfo) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Returns (payload for Store.ingest, daily heart rows for Store.import_daily_heart)."""
    metrics = {m.get("name"): m for m in data.get("metrics", []) if isinstance(m, dict)}

    def points(name: str) -> list[dict[str, Any]]:
        m = metrics.get(name)
        return [p for p in (m or {}).get("data", []) if isinstance(p, dict)]

    def day_bounds(p: dict[str, Any]) -> tuple[date, datetime, datetime] | None:
        t = parse_apple_time(p.get("date"))
        if t is None:
            return None
        d = t.date()  # the day as written, in the offset it was recorded in
        start = datetime.combine(d, dtime.min, tz)
        return d, start, start + timedelta(days=1) - timedelta(seconds=1)

    payload: dict[str, list[dict[str, Any]]] = {k: [] for k in (
        "steps", "sleep", "weight", "exercise", "active_calories", "total_calories")}

    for p in points("step_count"):
        b, q = day_bounds(p), _qty(p)
        if b and q is not None and q > 0:
            payload["steps"].append({"start_time": _iso(b[1]), "end_time": _iso(b[2]), "count": round(q)})

    active: dict[date, float] = {}
    m_active = metrics.get("active_energy") or {}
    for p in points("active_energy"):
        b = day_bounds(p)
        kcal = _kcal(_qty(p), m_active.get("units"))
        if b and kcal is not None:
            active[b[0]] = kcal
            payload["active_calories"].append({"start_time": _iso(b[1]), "end_time": _iso(b[2]),
                                               "calories": round(kcal, 1)})
    m_basal = metrics.get("basal_energy_burned") or {}
    for p in points("basal_energy_burned"):
        b = day_bounds(p)
        basal = _kcal(_qty(p), m_basal.get("units"))
        if b and basal is not None and b[0] in active:
            payload["total_calories"].append({"start_time": _iso(b[1]), "end_time": _iso(b[2]),
                                              "calories": round(basal + active[b[0]], 1)})

    for p in points("weight_body_mass"):
        t, q = parse_apple_time(p.get("date")), _qty(p)
        if t is not None and q is not None:
            payload["weight"].append({"time": _iso(t), "kilograms": round(q, 2)})

    stage_keys = (("core", "light"), ("deep", "deep"), ("rem", "rem"), ("awake", "awake"), ("asleep", "sleeping"))
    for p in points("sleep_analysis"):
        start = parse_apple_time(p.get("sleepStart") or p.get("inBedStart"))
        end = parse_apple_time(p.get("sleepEnd") or p.get("inBedEnd"))
        if start is None or end is None or end <= start:
            continue
        stages = [{"stage": name, "duration_seconds": round(float(p[key]) * 3600)}
                  for key, name in stage_keys if isinstance(p.get(key), (int, float)) and p[key] > 0]
        if not stages:
            continue
        payload["sleep"].append({"session_end_time": _iso(end), "duration_seconds": int((end - start).total_seconds()),
                                 "stages": stages})

    daily: dict[date, dict[str, Any]] = {}
    for p in points("heart_rate"):
        b = day_bounds(p)
        if b:
            daily.setdefault(b[0], {})["min"] = p.get("Min")
            daily[b[0]]["avg"] = p.get("Avg")
            daily[b[0]]["max"] = p.get("Max")
    for name, key in (("resting_heart_rate", "resting"), ("heart_rate_variability", "hrv")):
        for p in points(name):
            b, q = day_bounds(p), _qty(p)
            if b and q is not None:
                daily.setdefault(b[0], {})[key] = q
    heart = [{"day": d.isoformat(), **v} for d, v in sorted(daily.items())]

    for w in data.get("workouts", []):
        if not isinstance(w, dict):
            continue
        st, en = parse_apple_time(w.get("start")), parse_apple_time(w.get("end"))
        if st is None or en is None or en <= st:
            continue
        dist = w.get("distance") if isinstance(w.get("distance"), dict) else {}
        dq = _qty(dist)
        metres = None if dq is None else dq * {"km": 1000, "mi": 1609.344, "m": 1}.get(dist.get("units") or "km", 1000)
        entry: dict[str, Any] = {"type": str(w.get("name") or "workout"), "start_time": _iso(st),
                                 "end_time": _iso(en), "duration_seconds": w.get("duration") or (en - st).total_seconds()}
        if metres is not None:
            entry["distance_meters"] = round(metres)
        for key, out_key in (("avgHeartRate", "avg_heart_rate"), ("maxHeartRate", "max_heart_rate")):
            q = _qty(w.get(key))
            if q is not None:
                entry[out_key] = round(q)
        k = _kcal(_qty(w.get("activeEnergyBurned")), (w.get("activeEnergyBurned") or {}).get("units")
                  if isinstance(w.get("activeEnergyBurned"), dict) else None)
        if k is not None:
            entry["active_kcal"] = round(k)
        payload["exercise"].append(entry)

    return {k: v for k, v in payload.items() if v}, heart


def summarise(counts: dict[str, dict[str, int]], heart_days: int) -> str:
    names = {"steps": "step days", "sleep": "nights", "weight": "weigh-ins", "exercise": "workouts"}
    parts = [f'{c["stored"]} new {names[k]}' for k, c in counts.items() if k in names and c["stored"]]
    if heart_days and parts:
        parts.append(f"{heart_days} heart rate days")
    return "Imported " + (", ".join(parts) if parts else "nothing new (already up to date)")
