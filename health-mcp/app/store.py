"""SQLite storage, ingest normalisation and read-side aggregation.

All timestamps are stored as UTC ISO strings (YYYY-MM-DDTHH:MM:SSZ). Reads group
data into local calendar days using the configured timezone.

The sync app (HC Webhook) re-sends a rolling 48 hour window and its JSON carries no
record ids, so every insert is idempotent: duplicates are ignored by UNIQUE keys.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

UTC = timezone.utc
FMT = "%Y-%m-%dT%H:%M:%SZ"

SCHEMA = """
CREATE TABLE IF NOT EXISTS steps(
  id INTEGER PRIMARY KEY, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
  count INTEGER NOT NULL, UNIQUE(start_time, end_time, count));
CREATE TABLE IF NOT EXISTS heart_rate(
  id INTEGER PRIMARY KEY, time TEXT NOT NULL, bpm INTEGER NOT NULL, UNIQUE(time, bpm));
CREATE TABLE IF NOT EXISTS sleep(
  id INTEGER PRIMARY KEY, start_time TEXT NOT NULL, end_time TEXT NOT NULL UNIQUE,
  duration_seconds INTEGER NOT NULL, stages TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS weight(
  id INTEGER PRIMARY KEY, time TEXT NOT NULL, kg REAL NOT NULL, UNIQUE(time, kg));
CREATE TABLE IF NOT EXISTS blood_pressure(
  id INTEGER PRIMARY KEY, time TEXT NOT NULL, systolic INTEGER NOT NULL,
  diastolic INTEGER NOT NULL, pulse INTEGER, note TEXT,
  source TEXT NOT NULL DEFAULT 'sync', UNIQUE(time, systolic, diastolic));
CREATE TABLE IF NOT EXISTS nutrition(
  id INTEGER PRIMARY KEY, time TEXT NOT NULL, end_time TEXT, name TEXT NOT NULL DEFAULT '',
  kcal REAL NOT NULL DEFAULT 0, protein_g REAL, carbs_g REAL, fat_g REAL, sugar_g REAL,
  fibre_g REAL, sodium_mg REAL, source TEXT NOT NULL DEFAULT 'sync',
  UNIQUE(time, name, kcal, source));
CREATE TABLE IF NOT EXISTS exercise(
  id INTEGER PRIMARY KEY, start_time TEXT NOT NULL, end_time TEXT NOT NULL, type TEXT NOT NULL,
  duration_seconds INTEGER NOT NULL, distance_m REAL, steps INTEGER,
  extra TEXT NOT NULL DEFAULT '{}', UNIQUE(start_time, end_time, type));
CREATE TABLE IF NOT EXISTS active_calories(
  id INTEGER PRIMARY KEY, start_time TEXT NOT NULL, end_time TEXT NOT NULL, kcal REAL NOT NULL,
  UNIQUE(start_time, end_time, kcal));
CREATE TABLE IF NOT EXISTS total_calories(
  id INTEGER PRIMARY KEY, start_time TEXT NOT NULL, end_time TEXT NOT NULL, kcal REAL NOT NULL,
  UNIQUE(start_time, end_time, kcal));
CREATE TABLE IF NOT EXISTS sync_log(
  id INTEGER PRIMARY KEY, received_at TEXT NOT NULL, app_version TEXT, counts TEXT NOT NULL);
"""

TABLES = ("steps", "heart_rate", "sleep", "weight", "blood_pressure", "nutrition",
          "exercise", "active_calories", "total_calories")
TIME_COLUMN = {
    "steps": "start_time", "heart_rate": "time", "sleep": "end_time",
    "weight": "time", "blood_pressure": "time", "nutrition": "time",
    "exercise": "start_time", "active_calories": "start_time", "total_calories": "start_time",
}
CALORIE_TABLES = ("active_calories", "total_calories")  # fixed names, safe to put in SQL
EXERCISE_CORE = {"type", "start_time", "end_time", "duration_seconds", "distance_meters", "steps"}
MANUAL_KINDS = {"blood_pressure", "nutrition", "weight"}
SALT_G_PER_SODIUM_MG = 1 / 400.0  # 1 g salt = 400 mg sodium


def parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 string or epoch number to an aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e11:  # milliseconds
            v /= 1000.0
        try:
            return datetime.fromtimestamp(v, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(FMT)


def _num(value: Any, lo: float, hi: float) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


def _opt(value: Any, hi: float = 1e6) -> float | None:
    return _num(value, 0, hi)


def normalise_stage(name: Any) -> str:
    s = str(name or "").lower()
    if "deep" in s:
        return "deep"
    if "rem" in s:
        return "rem"
    if "light" in s:
        return "light"
    if "awake" in s or "out_of_bed" in s or "out of bed" in s:
        return "awake"
    if "sleeping" in s:
        return "asleep"
    return "unknown"


def normalise_exercise_type(name: Any) -> str:
    s = str(name or "").strip().upper()
    for prefix in ("EXERCISE_TYPE_", "EXERCISE_SESSION_TYPE_"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s.replace("_", " ").lower()[:60] or "workout"


# Health Connect ExerciseSessionRecord type codes. The sync app sends the raw number.
EXERCISE_NAMES = {
    0: "other workout", 2: "badminton", 4: "baseball", 5: "basketball", 8: "cycling",
    9: "stationary cycling", 10: "boot camp", 11: "boxing", 13: "calisthenics", 14: "cricket",
    16: "dancing", 25: "elliptical", 26: "exercise class", 27: "fencing", 28: "american football",
    29: "australian football", 31: "frisbee", 32: "golf", 33: "guided breathing", 34: "gymnastics",
    35: "handball", 36: "HIIT", 37: "hiking", 38: "ice hockey", 39: "ice skating",
    44: "martial arts", 46: "paddling", 47: "paragliding", 48: "pilates", 50: "racquetball",
    51: "rock climbing", 52: "roller hockey", 53: "rowing", 54: "rowing machine", 55: "rugby",
    56: "running", 57: "treadmill running", 58: "sailing", 59: "scuba diving", 60: "skating",
    61: "skiing", 62: "snowboarding", 63: "snowshoeing", 64: "football", 65: "softball",
    66: "squash", 68: "stair climbing", 69: "stair climbing machine", 70: "strength training",
    71: "stretching", 72: "surfing", 73: "open-water swimming", 74: "pool swimming",
    75: "table tennis", 76: "tennis", 77: "volleyball", 79: "walking", 80: "water polo",
    81: "weightlifting", 82: "wheelchair", 83: "yoga",
}


def exercise_label(stored: str) -> str:
    """Readable name for a stored type: numeric Health Connect codes are translated."""
    if str(stored).strip().isdigit():
        code = int(stored)
        return EXERCISE_NAMES.get(code, f"workout (type {code})")
    return stored


def dedupe_intervals(records: list[tuple[datetime, datetime, float]]) -> list[tuple[datetime, datetime, float]]:
    """Drop records that mostly overlap one already kept (the same activity reported by phone
    and watch). Earlier start wins; for equal starts the larger value wins."""
    kept: list[tuple[datetime, datetime, float]] = []
    for st, en, val in sorted(records, key=lambda r: (r[0], -r[2])):
        dur = max((en - st).total_seconds(), 1.0)
        overlap = 0.0
        for ks, ke, _ in kept[-50:]:
            o = (min(en, ke) - max(st, ks)).total_seconds()
            if o > 0:
                overlap += o
        if overlap / dur > 0.5:
            continue
        kept.append((st, en, val))
    return kept


class Store:
    def __init__(self, path: str, tz: str = "Europe/London",
                 now: Callable[[], datetime] | None = None) -> None:
        self.path = path
        self.tz = ZoneInfo(tz)
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            cols = [r["name"] for r in self._conn.execute("PRAGMA table_info(weight)")]
            if "source" not in cols:  # databases created before manual weight logging
                self._conn.execute("ALTER TABLE weight ADD COLUMN source TEXT NOT NULL DEFAULT 'sync'")

    # ------------------------------------------------------------------ helpers
    def now(self) -> datetime:
        return self._now()

    def local(self, dt: datetime) -> datetime:
        return dt.astimezone(self.tz)

    def day_of(self, ts: str) -> date:
        dt = parse_ts(ts)
        assert dt is not None
        return self.local(dt).date()

    def local_str(self, ts: str) -> str:
        dt = parse_ts(ts)
        assert dt is not None
        return self.local(dt).strftime("%Y-%m-%d %H:%M")

    def window(self, days: int) -> tuple[list[date], str]:
        """Local dates for the last `days` days (oldest first) and the UTC start bound."""
        days = max(1, min(int(days), 365))
        today = self.local(self.now()).date()
        dates = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
        start_local = datetime.combine(dates[0], dtime.min, self.tz)
        return dates, iso(start_local)

    def _rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------------- ingest
    def ingest(self, payload: dict[str, Any]) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}

        def bucket(name: str) -> dict[str, int]:
            return counts.setdefault(name, {"received": 0, "stored": 0, "rejected": 0})

        def rows(name: str) -> list[Any]:
            v = payload.get(name)
            return v if isinstance(v, list) else []

        with self._lock, self._conn:
            for r in rows("steps"):
                b = bucket("steps"); b["received"] += 1
                st, en = parse_ts(r.get("start_time")), parse_ts(r.get("end_time"))
                n = _num(r.get("count"), 0, 200000)
                if st is None or en is None or n is None or en < st:
                    b["rejected"] += 1; continue
                b["stored"] += self._conn.execute(
                    "INSERT OR IGNORE INTO steps(start_time,end_time,count) VALUES(?,?,?)",
                    (iso(st), iso(en), int(n))).rowcount

            for r in rows("heart_rate"):
                b = bucket("heart_rate"); b["received"] += 1
                t, bpm = parse_ts(r.get("time")), _num(r.get("bpm"), 20, 260)
                if t is None or bpm is None:
                    b["rejected"] += 1; continue
                b["stored"] += self._conn.execute(
                    "INSERT OR IGNORE INTO heart_rate(time,bpm) VALUES(?,?)",
                    (iso(t), int(bpm))).rowcount

            for r in rows("sleep"):
                b = bucket("sleep"); b["received"] += 1
                end = parse_ts(r.get("session_end_time"))
                dur = _num(r.get("duration_seconds"), 60, 3 * 86400)
                if end is None or dur is None:
                    b["rejected"] += 1; continue
                stages = []
                for s in r.get("stages") or []:
                    if not isinstance(s, dict):
                        continue
                    s0, s1 = parse_ts(s.get("start_time")), parse_ts(s.get("end_time"))
                    secs = _num(s.get("duration_seconds"), 0, 86400)
                    if secs is None and s0 and s1:
                        secs = (s1 - s0).total_seconds()
                    if secs is None:
                        continue
                    stages.append({"stage": normalise_stage(s.get("stage")),
                                   "seconds": int(secs),
                                   "start": iso(s0) if s0 else None})
                start = end - timedelta(seconds=int(dur))
                b["stored"] += self._conn.execute(
                    "INSERT INTO sleep(start_time,end_time,duration_seconds,stages) VALUES(?,?,?,?) "
                    "ON CONFLICT(end_time) DO UPDATE SET start_time=excluded.start_time, "
                    "duration_seconds=excluded.duration_seconds, stages=excluded.stages "
                    "WHERE excluded.duration_seconds<>sleep.duration_seconds "
                    "OR excluded.stages<>sleep.stages",
                    (iso(start), iso(end), int(dur), json.dumps(stages))).rowcount

            for r in rows("weight"):
                b = bucket("weight"); b["received"] += 1
                t, kg = parse_ts(r.get("time")), _num(r.get("kilograms"), 20, 400)
                if t is None or kg is None:
                    b["rejected"] += 1; continue
                b["stored"] += self._conn.execute(
                    "INSERT OR IGNORE INTO weight(time,kg) VALUES(?,?)",
                    (iso(t), round(kg, 2))).rowcount

            for r in rows("blood_pressure"):
                b = bucket("blood_pressure"); b["received"] += 1
                t = parse_ts(r.get("time"))
                sys_, dia = _num(r.get("systolic"), 50, 300), _num(r.get("diastolic"), 30, 200)
                if t is None or sys_ is None or dia is None or dia >= sys_:
                    b["rejected"] += 1; continue
                b["stored"] += self._conn.execute(
                    "INSERT OR IGNORE INTO blood_pressure(time,systolic,diastolic,source) "
                    "VALUES(?,?,?,'sync')", (iso(t), int(sys_), int(dia))).rowcount

            for r in rows("nutrition"):
                b = bucket("nutrition"); b["received"] += 1
                st = parse_ts(r.get("start_time"))
                en = parse_ts(r.get("end_time"))
                if st is None:
                    b["rejected"] += 1; continue
                sodium_g = _opt(r.get("sodium_grams"), 100)
                b["stored"] += self._conn.execute(
                    "INSERT OR IGNORE INTO nutrition(time,end_time,name,kcal,protein_g,carbs_g,"
                    "fat_g,sugar_g,fibre_g,sodium_mg,source) VALUES(?,?,?,?,?,?,?,?,?,?,'sync')",
                    (iso(st), iso(en) if en else None, str(r.get("name") or "")[:200],
                     _opt(r.get("calories"), 20000) or 0, _opt(r.get("protein_grams")),
                     _opt(r.get("carbs_grams")), _opt(r.get("fat_grams")),
                     _opt(r.get("sugar_grams")), _opt(r.get("dietary_fiber_grams")),
                     sodium_g * 1000 if sodium_g is not None else None)).rowcount

            for r in rows("exercise"):
                b = bucket("exercise"); b["received"] += 1
                st, en = parse_ts(r.get("start_time")), parse_ts(r.get("end_time"))
                if st is None or en is None or en <= st:
                    b["rejected"] += 1; continue
                dur = _num(r.get("duration_seconds"), 30, 2 * 86400)
                if dur is None:
                    dur = _num((en - st).total_seconds(), 30, 2 * 86400)
                if dur is None:
                    b["rejected"] += 1; continue
                extra = {k: v for k, v in r.items()
                         if k not in EXERCISE_CORE and isinstance(v, (int, float))
                         and not isinstance(v, bool)}
                extra = dict(list(extra.items())[:20])
                ex_steps = _opt(r.get("steps"), 200000)
                b["stored"] += self._conn.execute(
                    "INSERT INTO exercise(start_time,end_time,type,duration_seconds,distance_m,steps,extra) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(start_time,end_time,type) DO UPDATE SET "
                    "duration_seconds=excluded.duration_seconds, distance_m=excluded.distance_m, "
                    "steps=excluded.steps, extra=excluded.extra "
                    "WHERE exercise.duration_seconds<>excluded.duration_seconds "
                    "OR exercise.distance_m IS NOT excluded.distance_m "
                    "OR exercise.steps IS NOT excluded.steps OR exercise.extra<>excluded.extra",
                    (iso(st), iso(en), normalise_exercise_type(r.get("type")), int(dur),
                     _opt(r.get("distance_meters"), 1e6),
                     int(ex_steps) if ex_steps is not None else None,
                     json.dumps(extra))).rowcount

            for table in CALORIE_TABLES:
                for r in rows(table):
                    b = bucket(table); b["received"] += 1
                    st, en = parse_ts(r.get("start_time")), parse_ts(r.get("end_time"))
                    kcal = _num(r.get("calories"), 0, 20000)
                    if st is None or en is None or en < st or kcal is None:
                        b["rejected"] += 1; continue
                    b["stored"] += self._conn.execute(
                        f"INSERT OR IGNORE INTO {table}(start_time,end_time,kcal) VALUES(?,?,?)",
                        (iso(st), iso(en), round(kcal, 1))).rowcount

            self._conn.execute(
                "INSERT INTO sync_log(received_at,app_version,counts) VALUES(?,?,?)",
                (iso(self.now()), str(payload.get("app_version") or "")[:40], json.dumps(counts)))
            self._conn.execute(
                "DELETE FROM sync_log WHERE id NOT IN (SELECT id FROM sync_log ORDER BY id DESC LIMIT 500)")
        return counts

    # ------------------------------------------------------------- manual logs
    def log_blood_pressure(self, systolic: int, diastolic: int, pulse: int | None,
                           when: datetime | None, note: str | None) -> dict[str, Any]:
        if not (50 <= systolic <= 300 and 30 <= diastolic <= 200 and diastolic < systolic):
            raise ValueError("Implausible blood pressure values")
        if pulse is not None and not (20 <= pulse <= 260):
            raise ValueError("Implausible pulse")
        ts = iso(when or self.now())
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO blood_pressure(time,systolic,diastolic,pulse,note,source) "
                "VALUES(?,?,?,?,?,'manual')", (ts, systolic, diastolic, pulse, (note or "")[:500] or None))
            if cur.rowcount == 0:
                raise ValueError("An identical reading at this time already exists")
            rid = cur.lastrowid
        return {"id": rid, "time": self.local_str(ts), "systolic": systolic,
                "diastolic": diastolic, "pulse": pulse, "note": note}

    def log_meal(self, name: str, kcal: float, protein_g: float | None, carbs_g: float | None,
                 fat_g: float | None, fibre_g: float | None, sugar_g: float | None,
                 salt_g: float | None, when: datetime | None) -> dict[str, Any]:
        if not name.strip():
            raise ValueError("Meal name is required")
        if not (0 <= kcal <= 20000):
            raise ValueError("Implausible calories")
        ts = iso(when or self.now())
        sodium_mg = salt_g / SALT_G_PER_SODIUM_MG if salt_g is not None else None
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO nutrition(time,name,kcal,protein_g,carbs_g,fat_g,sugar_g,"
                "fibre_g,sodium_mg,source) VALUES(?,?,?,?,?,?,?,?,?,'manual')",
                (ts, name.strip()[:200], kcal, protein_g, carbs_g, fat_g, sugar_g, fibre_g, sodium_mg))
            if cur.rowcount == 0:
                raise ValueError("An identical meal at this time already exists")
            rid = cur.lastrowid
        return {"id": rid, "time": self.local_str(ts), "name": name.strip(), "kcal": kcal}

    def log_weight(self, kg: float, when: datetime | None) -> dict[str, Any]:
        if not (20 <= kg <= 400):
            raise ValueError("Implausible weight")
        ts = iso(when or self.now())
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO weight(time,kg,source) VALUES(?,?,'manual')", (ts, round(kg, 2)))
            if cur.rowcount == 0:
                raise ValueError("An identical weigh-in at this time already exists")
            rid = cur.lastrowid
        return {"id": rid, "time": self.local_str(ts), "kg": round(kg, 2)}

    def delete_manual(self, kind: str, entry_id: int) -> bool:
        if kind not in MANUAL_KINDS:
            raise ValueError(f"kind must be one of {sorted(MANUAL_KINDS)}")
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"DELETE FROM {kind} WHERE id=? AND source='manual'", (entry_id,))
            return cur.rowcount > 0

    # --------------------------------------------------------------- aggregates
    def steps_by_day(self, days: int) -> dict[date, dict[str, Any]]:
        dates, start = self.window(days)
        rows = self._rows("SELECT start_time,end_time,count FROM steps WHERE start_time>=? "
                          "ORDER BY start_time ASC, count DESC", (start,))
        kept: list[tuple[datetime, datetime]] = []
        out: dict[date, dict[str, Any]] = {d: {"steps": 0, "raw_sum": 0, "records": 0} for d in dates}
        for r in rows:
            st, en = parse_ts(r["start_time"]), parse_ts(r["end_time"])
            assert st and en
            d = self.local(st).date()
            if d not in out:
                continue
            out[d]["raw_sum"] += r["count"]
            dur = max((en - st).total_seconds(), 1.0)
            overlap = 0.0
            for ks, ke in kept[-50:]:
                o = (min(en, ke) - max(st, ks)).total_seconds()
                if o > 0:
                    overlap += o
            if overlap / dur > 0.5:
                continue  # mostly overlaps a record we already counted (duplicate source)
            kept.append((st, en))
            out[d]["steps"] += r["count"]
            out[d]["records"] += 1
        return out

    def energy_by_day(self, days: int) -> dict[date, dict[str, float]]:
        """Calories burned per local day: active (movement) and total (incl. resting)."""
        dates, start = self.window(days)
        out: dict[date, dict[str, float]] = {d: {} for d in dates}
        for table, label in (("active_calories", "active_kcal"), ("total_calories", "total_kcal")):
            recs = []
            for r in self._rows(f"SELECT start_time,end_time,kcal FROM {table} WHERE start_time>=?", (start,)):
                st, en = parse_ts(r["start_time"]), parse_ts(r["end_time"])
                assert st and en
                recs.append((st, en, r["kcal"]))
            for st, _, kcal in dedupe_intervals(recs):
                d = self.local(st).date()
                if d in out:
                    out[d][label] = round(out[d].get(label, 0.0) + kcal)
        return out

    def workouts(self, days: int) -> list[dict[str, Any]]:
        _, start = self.window(days)
        sessions = []
        # Total calories arrive in short (<= 1 h) records. Resting burn per minute is taken as
        # the 25th percentile of those rates, so "above resting" approximates the workout's cost.
        short: list[tuple[datetime, datetime, float]] = []
        for c in self._rows("SELECT start_time,end_time,kcal FROM total_calories WHERE start_time>=?",
                            (start,)):
            cs, ce = parse_ts(c["start_time"]), parse_ts(c["end_time"])
            assert cs and ce
            if 0 < (ce - cs).total_seconds() <= 3600:
                short.append((cs, ce, c["kcal"]))
        short = dedupe_intervals(short)
        rates = sorted(v / ((e - s_).total_seconds() / 60) for s_, e, v in short)
        resting = rates[len(rates) // 4] if len(rates) >= 20 else None
        for r in self._rows("SELECT * FROM exercise WHERE start_time>=? ORDER BY start_time", (start,)):
            st, en = parse_ts(r["start_time"]), parse_ts(r["end_time"])
            assert st and en
            bpms = [x["bpm"] for x in self._rows(
                "SELECT bpm FROM heart_rate WHERE time>=? AND time<=?", (r["start_time"], r["end_time"]))]
            # Active calories recorded during the session: prorate records up to 6 h long
            # (longer ones are whole-day totals and say nothing about one session).
            recs = []
            for c in self._rows("SELECT start_time,end_time,kcal FROM active_calories "
                                "WHERE end_time>? AND start_time<?", (r["start_time"], r["end_time"])):
                cs, ce = parse_ts(c["start_time"]), parse_ts(c["end_time"])
                assert cs and ce
                if (ce - cs).total_seconds() <= 6 * 3600:
                    recs.append((cs, ce, c["kcal"]))
            kcal = 0.0
            for cs, ce, v in dedupe_intervals(recs):
                span = max((ce - cs).total_seconds(), 1.0)
                kcal += v * max(0.0, (min(en, ce) - max(st, cs)).total_seconds()) / span
            during = 0.0
            covered = 0.0
            for cs, ce, v in short:
                o = (min(en, ce) - max(st, cs)).total_seconds()
                if o > 0:
                    during += v * o / (ce - cs).total_seconds()
                    covered += o
            full = covered >= 0.8 * (en - st).total_seconds()
            item: dict[str, Any] = {
                "id": r["id"], "date": self.local(st).date().isoformat(),
                "start": self.local(st).strftime("%H:%M"), "type": exercise_label(r["type"]),
                "minutes": round(r["duration_seconds"] / 60),
                "distance_km": round(r["distance_m"] / 1000, 2) if r["distance_m"] else None,
                "steps": r["steps"],
                "avg_hr": round(sum(bpms) / len(bpms)) if bpms else None,
                "max_hr": max(bpms) if bpms else None,
                "active_kcal_estimate": round(kcal) if kcal > 0 else None,
                "kcal_total_during": round(during) if full else None,
                "kcal_above_resting_estimate": (
                    max(0, round(during - resting * (en - st).total_seconds() / 60))
                    if full and resting is not None else None),
            }
            item.update(json.loads(r["extra"] or "{}"))
            sessions.append(item)
        return sessions

    def heart_by_day(self, days: int) -> dict[date, dict[str, Any]]:
        dates, start = self.window(days)
        by: dict[date, list[int]] = defaultdict(list)
        for r in self._rows("SELECT time,bpm FROM heart_rate WHERE time>=?", (start,)):
            by[self.day_of(r["time"])].append(r["bpm"])
        out: dict[date, dict[str, Any]] = {}
        for d in dates:
            v = sorted(by.get(d, []))
            if not v:
                out[d] = {}
                continue
            out[d] = {"samples": len(v), "min": v[0], "avg": round(sum(v) / len(v), 1),
                      "max": v[-1], "p10": v[int(0.1 * (len(v) - 1))]}
        return out

    def sleep_by_day(self, days: int) -> dict[date, dict[str, Any]]:
        dates, start = self.window(days)
        out: dict[date, dict[str, Any]] = {d: {} for d in dates}
        rows = self._rows("SELECT start_time,end_time,duration_seconds,stages FROM sleep "
                          "WHERE end_time>=? ORDER BY end_time", (start,))
        # The watch re-sends a session as it grows (same start, later end), and each version is
        # stored. Keep the longest version of a session and drop any that mostly overlaps it.
        kept: list[Any] = []
        spans: list[tuple[datetime, datetime]] = []
        for r in sorted(rows, key=lambda x: -x["duration_seconds"]):
            st, en = parse_ts(r["start_time"]), parse_ts(r["end_time"])
            assert st and en
            dur = max((en - st).total_seconds(), 1.0)
            overlap = sum(max(0.0, (min(en, ke) - max(st, ks)).total_seconds()) for ks, ke in spans)
            if overlap / dur > 0.5:
                continue
            kept.append(r)
            spans.append((st, en))
        for r in sorted(kept, key=lambda x: x["end_time"]):
            d = self.day_of(r["end_time"])
            if d not in out:
                continue
            stages = json.loads(r["stages"])
            tot: dict[str, int] = defaultdict(int)
            for s in stages:
                tot[s["stage"]] += s["seconds"]
            awake = tot.get("awake", 0)
            asleep = (sum(tot.values()) - awake) if stages else r["duration_seconds"]
            cur = out[d] or {"sessions": 0, "asleep_seconds": 0, "awake_seconds": 0,
                             "stages": defaultdict(int)}
            cur["sessions"] += 1
            cur["asleep_seconds"] += asleep
            cur["awake_seconds"] += awake
            for k, v in tot.items():
                cur["stages"][k] += v
            cur["bed"] = self.local_str(r["start_time"]) if "bed" not in cur else cur["bed"]
            cur["wake"] = self.local_str(r["end_time"])
            out[d] = cur
        return out

    def weight_readings(self, days: int) -> list[dict[str, Any]]:
        _, start = self.window(days)
        out: list[tuple[datetime, float, int, str]] = []
        for r in self._rows("SELECT id,time,kg,source FROM weight WHERE time>=? ORDER BY time", (start,)):
            t = parse_ts(r["time"])
            assert t
            if out and abs((t - out[-1][0]).total_seconds()) <= 600 and abs(r["kg"] - out[-1][1]) < 0.05:
                continue  # same weigh-in arriving via two apps
            out.append((t, r["kg"], r["id"], r["source"]))
        return [{"id": i, "time": self.local(t).strftime("%Y-%m-%d %H:%M"), "kg": kg, "source": src}
                for t, kg, i, src in out]

    def bp_readings(self, days: int) -> list[dict[str, Any]]:
        _, start = self.window(days)
        return [{"id": r["id"], "time": self.local_str(r["time"]), "systolic": r["systolic"],
                 "diastolic": r["diastolic"], "pulse": r["pulse"], "note": r["note"],
                 "source": r["source"]}
                for r in self._rows("SELECT * FROM blood_pressure WHERE time>=? ORDER BY time", (start,))]

    def food_entries(self, days: int) -> list[dict[str, Any]]:
        _, start = self.window(days)
        out = []
        for r in self._rows("SELECT * FROM nutrition WHERE time>=? ORDER BY time", (start,)):
            out.append({
                "id": r["id"], "time": self.local_str(r["time"]), "name": r["name"],
                "kcal": round(r["kcal"]), "protein_g": r["protein_g"], "carbs_g": r["carbs_g"],
                "fat_g": r["fat_g"], "sugar_g": r["sugar_g"], "fibre_g": r["fibre_g"],
                "salt_g": round(r["sodium_mg"] * SALT_G_PER_SODIUM_MG, 2) if r["sodium_mg"] is not None else None,
                "source": r["source"], "_day": self.day_of(r["time"])})
        return out

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        last = self._rows("SELECT received_at,app_version FROM sync_log ORDER BY id DESC LIMIT 1")
        info: dict[str, Any] = {"timezone": str(self.tz), "tables": {}}
        if last:
            t = parse_ts(last[0]["received_at"])
            assert t
            info["last_sync"] = self.local_str(last[0]["received_at"])
            info["hours_since_last_sync"] = round((self.now() - t).total_seconds() / 3600, 1)
            info["sync_app_version"] = last[0]["app_version"]
            info["stale"] = info["hours_since_last_sync"] > 6
        else:
            info["last_sync"] = None
            info["stale"] = True
        for t in TABLES:
            row = self._rows(f"SELECT COUNT(*) c, MAX({TIME_COLUMN[t]}) m FROM {t}")[0]
            info["tables"][t] = {"rows": row["c"],
                                 "latest": self.local_str(row["m"]) if row["m"] else None}
        return info
