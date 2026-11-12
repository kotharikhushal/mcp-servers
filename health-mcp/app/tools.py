"""MCP tools. Read tools are read-only; the only writes are blood pressure and meal logs."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .store import Store, UTC

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
DELETE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)


def _h(seconds: float) -> float:
    return round(seconds / 3600, 2)


def _parse_when(store: Store, when: str | None) -> datetime | None:
    """ISO time from the user. A time without a zone means local time."""
    if not when:
        return None
    try:
        dt = datetime.fromisoformat(when.strip().replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError("'when' must be an ISO time such as 2026-11-10T07:30") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=store.tz)
    return dt.astimezone(UTC)


def _avg(vals: list[float]) -> float | None:
    return round(sum(vals) / len(vals), 1) if vals else None


def bp_stats(readings: list[dict[str, Any]]) -> dict[str, Any]:
    def block(rs: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not rs:
            return None
        return {"readings": len(rs), "systolic": _avg([r["systolic"] for r in rs]),
                "diastolic": _avg([r["diastolic"] for r in rs])}

    days = sorted({r["time"][:10] for r in readings})
    after_first = [r for r in readings if r["time"][:10] != days[0]] if len(days) >= 2 else []
    return {
        "average_all": block(readings),
        "average_excluding_first_day": block(after_first),
        "morning_before_noon": block([r for r in readings if r["time"][11:13] < "12"]),
        "evening_noon_onwards": block([r for r in readings if r["time"][11:13] >= "12"]),
        "days_with_readings": len(days),
        "note": "Home averages for clinical review usually discard the first day and use "
                "several days of morning and evening readings. This is data, not a diagnosis.",
    }


def register(mcp: FastMCP, store: Store) -> None:
    @mcp.tool(annotations=READ)
    def data_status() -> dict[str, Any]:
        """Check that the health data pipeline is alive: when the phone last synced and the
        newest record per data type. Call this first if data looks missing or old."""
        return store.status()

    @mcp.tool(annotations=READ)
    def health_summary(days: int = 7) -> dict[str, Any]:
        """One-call overview per local day for the last `days` days (1-365): steps, heart
        rate, sleep, weight, blood pressure averages and logged food (kcal, salt). Start
        here for questions about progress, then drill into a specific metric tool."""
        dates, _ = store.window(days)
        steps, hr, sl = store.steps_by_day(days), store.heart_by_day(days), store.sleep_by_day(days)
        energy = store.energy_by_day(days)
        wk: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for w in store.workouts(days):
            wk[w["date"]].append(w)
        weights: dict[str, float] = {}
        for w in store.weight_readings(days):
            weights[w["time"][:10]] = w["kg"]
        bp: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in store.bp_readings(days):
            bp[r["time"][:10]].append(r)
        food: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        for e in store.food_entries(days):
            food[e["_day"]].append(e)
        rows = []
        for d in dates:
            key = d.isoformat()
            row: dict[str, Any] = {"date": key, "weekday": d.strftime("%a")}
            s = steps[d]
            row["steps"] = s["steps"] if s["records"] else None
            row["heart_rate_avg"] = hr[d].get("avg")
            row["heart_rate_min"] = hr[d].get("min")
            row["sleep_hours"] = _h(sl[d]["asleep_seconds"]) if sl[d] else None
            row["weight_kg"] = weights.get(key)
            row["active_kcal_burned"] = energy[d].get("active_kcal")
            row["total_kcal_burned"] = energy[d].get("total_kcal")
            if wk.get(key):
                row["workouts"] = [f'{w["type"]} {w["minutes"]} min' for w in wk[key]]
            if bp.get(key):
                row["bp_avg"] = f'{round(sum(r["systolic"] for r in bp[key]) / len(bp[key]))}/' \
                                f'{round(sum(r["diastolic"] for r in bp[key]) / len(bp[key]))}'
                row["bp_readings"] = len(bp[key])
            if food.get(d):
                row["kcal_logged"] = sum(e["kcal"] for e in food[d])
                salts = [e["salt_g"] for e in food[d] if e["salt_g"] is not None]
                row["salt_g_logged"] = round(sum(salts), 1) if salts else None
            rows.append(row)
        out: dict[str, Any] = {"timezone": str(store.tz), "days": rows}
        st = store.status()
        if st.get("stale"):
            out["warning"] = f"Phone data last synced: {st.get('last_sync')}. Recent days may be incomplete."
        return out

    @mcp.tool(annotations=READ)
    def get_steps(days: int = 7) -> dict[str, Any]:
        """Daily step totals. `steps` removes records that mostly overlap another record (the
        same steps from phone and watch); `raw_sum` is the plain total. A large gap between
        them means duplicate sources, so compare `steps` with the Google Health app."""
        s = store.steps_by_day(days)
        return {"days": [{"date": d.isoformat(), "steps": v["steps"], "raw_sum": v["raw_sum"],
                          "records": v["records"]} for d, v in s.items()]}

    @mcp.tool(annotations=READ)
    def get_heart_rate(days: int = 7) -> dict[str, Any]:
        """Daily heart rate stats from watch samples: samples, min, avg, max and p10 (the
        10th percentile, a good proxy for resting heart rate)."""
        h = store.heart_by_day(days)
        return {"days": [{"date": d.isoformat(), **v} for d, v in h.items()]}

    @mcp.tool(annotations=READ)
    def get_sleep(days: int = 7) -> dict[str, Any]:
        """Sleep per local day (the day you woke up): hours asleep, awake time, bed and wake
        times and stage hours (deep, rem, light) when the watch provides them."""
        s = store.sleep_by_day(days)
        rows = []
        for d, v in s.items():
            if not v:
                rows.append({"date": d.isoformat(), "asleep_hours": None})
                continue
            rows.append({"date": d.isoformat(), "asleep_hours": _h(v["asleep_seconds"]),
                         "awake_minutes": round(v["awake_seconds"] / 60), "sessions": v["sessions"],
                         "bed": v.get("bed"), "wake": v.get("wake"),
                         "stage_hours": {k: _h(x) for k, x in v["stages"].items() if k != "awake"}})
        return {"days": rows}

    @mcp.tool(annotations=READ)
    def get_workouts(days: int = 14) -> dict[str, Any]:
        """Workout sessions from the watch or phone (running, walking, gym, treadmill...):
        type, minutes, distance, average and max heart rate, and an estimate of active calories
        burned. Also weekly totals and daily calories burned (active = movement, total =
        including resting metabolism). Use with get_food_log to compare intake and burn."""
        sessions = store.workouts(days)
        energy = store.energy_by_day(days)
        by_type: dict[str, dict[str, float]] = defaultdict(lambda: {"sessions": 0, "minutes": 0})
        for s in sessions:
            by_type[s["type"]]["sessions"] += 1
            by_type[s["type"]]["minutes"] += s["minutes"]
        return {
            "workouts": sessions,
            "totals": {"sessions": len(sessions), "minutes": sum(s["minutes"] for s in sessions),
                       "by_type": by_type},
            "calories_burned_by_day": [{"date": d.isoformat(), **v} for d, v in energy.items() if v],
            "note": "kcal_total_during is the watch's total energy over the session (includes resting burn); "
                    "kcal_above_resting_estimate subtracts a resting rate taken from the quietest periods. "
                    "Both are approximate. The watch records active calories only once a day.",
        }

    @mcp.tool(annotations=READ)
    def get_weight(days: int = 30) -> dict[str, Any]:
        """Weigh-ins (scale readings, duplicates from two apps merged) with the change over
        the period and the average weekly rate."""
        r = store.weight_readings(days)
        out: dict[str, Any] = {"readings": r}
        if len(r) >= 2:
            first, last = r[0], r[-1]
            change = round(last["kg"] - first["kg"], 2)
            out["change_kg"] = change
            t0 = datetime.strptime(first["time"], "%Y-%m-%d %H:%M")
            t1 = datetime.strptime(last["time"], "%Y-%m-%d %H:%M")
            span = (t1 - t0).total_seconds() / 86400
            if span >= 3:
                out["kg_per_week"] = round(change / span * 7, 2)
        return out

    @mcp.tool(annotations=READ)
    def get_blood_pressure(days: int = 14) -> dict[str, Any]:
        """Blood pressure readings with averages: overall, excluding the first day (the
        usual convention for home monitoring), and morning versus evening. Includes manual
        entries and any synced from a cuff."""
        readings = store.bp_readings(days)
        return {"readings": readings, "stats": bp_stats(readings)}

    @mcp.tool(annotations=READ)
    def get_food_log(days: int = 1) -> dict[str, Any]:
        """Logged meals grouped by local day with totals (kcal, protein, carbs, fat, fibre,
        sugar, salt in grams). Entries marked source 'manual' were logged through chat."""
        by: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        for e in store.food_entries(days):
            by[e.pop("_day")].append(e)
        out = []
        for d in sorted(by):
            es = by[d]

            def tot(k: str) -> float | None:
                v = [e[k] for e in es if e[k] is not None]
                return round(sum(v), 1) if v else None

            out.append({"date": d.isoformat(), "meals": es,
                        "totals": {k: tot(k) for k in ("kcal", "protein_g", "carbs_g", "fat_g",
                                                       "fibre_g", "sugar_g", "salt_g")}})
        return {"days": out}

    @mcp.tool(annotations=WRITE)
    def log_blood_pressure(systolic: int, diastolic: int, pulse: int | None = None,
                           when: str | None = None, note: str | None = None) -> dict[str, Any]:
        """Save a blood pressure reading the user reports (for example from a home cuff that
        does not sync). `when` is optional ISO time, local if no zone is given; it defaults
        to now. Only log values the user actually stated."""
        return store.log_blood_pressure(systolic, diastolic, pulse, _parse_when(store, when), note)

    @mcp.tool(annotations=WRITE)
    def log_meal(name: str, kcal: float, protein_g: float | None = None, carbs_g: float | None = None,
                 fat_g: float | None = None, fibre_g: float | None = None, sugar_g: float | None = None,
                 salt_g: float | None = None, when: str | None = None) -> dict[str, Any]:
        """Save a meal or snack to the food log. Estimates are fine; say so in `name`
        (for example 'Poha with peanuts (estimate)'). `when` is optional ISO time, local if
        no zone is given; it defaults to now."""
        return store.log_meal(name, kcal, protein_g, carbs_g, fat_g, fibre_g, sugar_g, salt_g,
                              _parse_when(store, when))

    @mcp.tool(annotations=WRITE)
    def log_weight(kg: float, when: str | None = None) -> dict[str, Any]:
        """Save a weigh-in the user reports (for example one the scale did not sync). `when`
        is optional ISO time, local if no zone is given; it defaults to now. Only log values
        the user actually stated, and say which time you used."""
        return store.log_weight(kg, _parse_when(store, when))

    @mcp.tool(annotations=WRITE)
    def import_apple_health(data: str, preview: bool = False) -> dict[str, Any]:
        """Add a Health Auto Export (Apple Health) export that someone has given the user in chat to THEIR OWN
        record. The `person` argument is required for this tool: the name of the person the export belongs to (for example shivani). Ask if it
        is not clear whose file it is, and never guess, because the wrong person would get the data. `data` is the
        export's JSON text. A zip must be opened first, in the sandbox, and the JSON passed here; a large export
        should be cut down first with code, keeping the same layout (data.metrics and data.workouts) and only the
        days and metrics that matter (steps, sleep_analysis, heart_rate, resting_heart_rate, weight_body_mass,
        active_energy, basal_energy_burned, workouts). Importing an overlapping export again is safe. Use
        preview=true to see what would be added without saving it, and tell the user what was added."""
        from . import apple
        from .person import CURRENT
        if not CURRENT.get():   # never fall back to the default person for an import
            raise ValueError("Say whose export this is by setting person (for example person='shivani').")
        if len(data) > apple.MAX_JSON:
            raise ValueError("That export is too large to pass here. Cut it down to the recent days first.")
        try:
            doc = apple.load_upload(data.encode("utf-8"))
            payload, heart = apple.convert(doc, store.tz)
        except apple.ImportError_ as e:
            raise ValueError(str(e)) from e
        if preview:
            n = {k: len(v) for k, v in payload.items() if isinstance(v, list) and v}
            return {"saved": False, "would_add": n, "heart_rate_days": len(heart)}
        counts = store.ingest(payload)
        days = store.import_daily_heart(heart)
        summary = apple.summarise(counts, days)
        store.set_setting("last_import", f'{store.local(store.now()).strftime("%-d %b %Y %H:%M")}: {summary} (from chat)')
        return {"saved": True, "summary": summary}

    @mcp.tool(annotations=DELETE)
    def delete_manual_entry(kind: str, entry_id: int) -> dict[str, Any]:
        """Delete a blood pressure, meal or weight entry that was logged manually
        (kind is 'blood_pressure', 'nutrition' or 'weight'; entry_id comes from the get_* tools). Synced data
        cannot be deleted. Confirm with the user first."""
        return {"deleted": store.delete_manual(kind, entry_id)}

    @mcp.tool(annotations=READ)
    def get_settings() -> dict[str, Any]:
        """The user's goals (daily steps, sleep hours), fasting days in the last 30 days and the GP note saved
        in the app."""
        from .pages import goals, nutrition_goals
        g, n = goals(store), nutrition_goals(store)
        return {"steps_goal": int(g["steps_goal"]), "sleep_goal_hours": g["sleep_goal"],
                "calorie_goal_kcal": n["kcal_goal"], "protein_goal_g": n["protein_goal"], "salt_limit_g": n["salt_limit"], "goal_weight_kg": n["goal_weight"],
                "fasting_weekdays": [("Mon","Tue","Wed","Thu","Fri","Sat","Sun")[i] for i in sorted(store.fasting_weekdays())],
                "fasting_days_last_30": sorted(d.isoformat() for d in store.fasting_days(30)),
                "gp_note": store.get_setting("gp_context") or None}

    @mcp.tool(annotations=WRITE)
    def set_goals(steps_goal: int | None = None, sleep_goal_hours: float | None = None,
                  calorie_goal_kcal: float | None = None, protein_goal_g: float | None = None,
                  salt_limit_g: float | None = None, goal_weight_kg: float | None = None) -> dict[str, Any]:
        """Change the daily goals shown in the wellness app: steps (1,000 to 50,000), sleep (4 to 12 hours),
        calories (1,000 to 4,000 kcal), protein (20 to 300 g) the salt limit (1 to 12 g) and goal weight (40 to 200 kg). Only values the user
        agreed to. The app then shows calories, protein and salt left today."""
        from .pages import NUTRITION_GOALS
        for key, v in (("kcal_goal", calorie_goal_kcal), ("protein_goal", protein_goal_g), ("salt_limit", salt_limit_g), ("goal_weight", goal_weight_kg)):
            lo, hi = NUTRITION_GOALS[key]
            if v is not None and not lo <= v <= hi:
                return {"error": f"{key} must be between {lo} and {hi}"}
        if steps_goal is not None and not 1000 <= steps_goal <= 50000:
            return {"error": "steps_goal must be between 1,000 and 50,000"}
        if sleep_goal_hours is not None and not 4 <= sleep_goal_hours <= 12:
            return {"error": "sleep_goal_hours must be between 4 and 12"}
        if steps_goal is not None:
            store.set_setting("steps_goal", str(int(steps_goal)))
        if sleep_goal_hours is not None:
            store.set_setting("sleep_goal", f"{sleep_goal_hours:g}")
        for key, v in (("kcal_goal", calorie_goal_kcal), ("protein_goal", protein_goal_g), ("salt_limit", salt_limit_g), ("goal_weight", goal_weight_kg)):
            if v is not None:
                store.set_setting(key, f"{v:g}")
        return get_settings()

    @mcp.tool(annotations=WRITE)
    def set_fasting_days(dates: list[str], fasting: bool = True) -> dict[str, Any]:
        """Mark days (ISO dates, up to 60) as fasting days, or clear them with fasting=false. The app and
        meal plan use this to explain low-calorie days."""
        from datetime import date as _date
        if not 0 < len(dates) <= 60:
            return {"error": "Send between 1 and 60 dates."}
        try:
            days = [_date.fromisoformat(d) for d in dates]
        except ValueError:
            return {"error": "Dates must be ISO dates such as 2026-11-12"}
        for d in days:
            store.set_fasting(d, fasting)
        return {"updated": len(days), "fasting": fasting}

    @mcp.tool(annotations=WRITE)
    def set_fasting_weekdays(weekdays: list[str]) -> dict[str, Any]:
        """Make fasting repeat every week on these weekdays (Mon, Tue, Wed, Thu, Fri, Sat, Sun). An empty list
        turns the weekly pattern off. Single dates are still set with set_fasting_days."""
        names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        try:
            days = {names.index(w.strip().lower()[:3]) for w in weekdays}
        except ValueError:
            return {"error": "Use weekday names such as Tue and Thu."}
        store.set_fasting_weekdays(days)
        return {"fasting_weekdays": [names[i].capitalize() for i in sorted(days)]}

    @mcp.tool(annotations=WRITE)
    def set_ai_analysis(headline: str, points: list[str]) -> dict[str, Any]:
        """Save the AI analysis the wellness app shows at the top of its home page (replaces the previous one).
        Write it from the person's own data only (call health_summary and the detail tools first): a one-line
        headline and up to 5 short points on trends, what is going well and one or two things to try. Report and
        encourage; never diagnose, and say when data is thin. Pass `person` so it lands on the right account."""
        import json
        headline = " ".join((headline or "").split())
        pts = [" ".join(str(x).split()) for x in (points or []) if str(x).strip()]
        if not headline or len(headline) > 160:
            return {"error": "Give a headline of up to 160 characters."}
        if not 1 <= len(pts) <= 5 or any(len(x) > 320 for x in pts):
            return {"error": "Give 1 to 5 points of up to 320 characters each."}
        at = store.now().isoformat()
        store.set_setting("ai_analysis", json.dumps({"headline": headline, "points": pts, "at": at}))
        return {"saved": True, "at": store.local_str(at)}

    @mcp.tool(annotations=WRITE)
    def set_gp_note(text: str | None) -> dict[str, Any]:
        """Save (or clear with null) the context note shown at the top of the GP report, for example medication
        start dates. Only text the user gave."""
        text = (text or "").strip()
        if len(text) > 600:
            return {"error": "Keep the note under 600 characters."}
        store.set_setting("gp_context", text or None)
        return {"saved": bool(text)}
