"""AI analysis written by a self-hosted model (Ollama) from facts the app works out itself.

The code does all the arithmetic and the blood-pressure comparison. The model only writes the wording, and
every answer is checked before it is saved: right shape, no number that is not in the facts, no diagnosis or
medication wording, no wrong or alarming blood-pressure wording, and thin data admitted as thin.
"""
from __future__ import annotations

import json
import re
import statistics
from datetime import timedelta
from typing import Any

import httpx

from .pages import HOME_BP_HIGH, goals, nutrition_goals
from .store import Store
from .views import bp_sessions

SYSTEM = """You write the short "AI analysis" card for a personal wellness app, for the person whose numbers are given.
Rules:
- Use ONLY the numbers in the facts. Never calculate new numbers and never invent any. Round only as already given or to the nearest whole number.
- For blood pressure, quote "latest_sitting_summary" and compare the top and bottom numbers separately, exactly as it states. Never say a whole reading is above or below the threshold yourself, and never call readings "high" or tell the person they "need attention".
- Do not set new targets or goals of your own; only use the goals given.
- Be factual, kind and practical. Cover what changed, what is going well and one or two things to try.
- Never diagnose, never name a medical condition, never comment on medication or doses. For blood pressure, say the GP follow-up is the place to discuss the numbers when "gp_follow_up" is given, otherwise say to discuss them with a GP.
- If data is thin (few days, few readings, no food logged), say so plainly instead of drawing conclusions.
- Write in British English, plain words, no emojis.
Return JSON only: {"headline": string up to 160 characters, "points": array of 3 to 5 strings, each up to 320 characters}."""

SCHEMA = {"type": "object",
          "properties": {"headline": {"type": "string"}, "points": {"type": "array", "items": {"type": "string"}}},
          "required": ["headline", "points"]}


def build_facts(store: Store) -> dict[str, Any]:
    """Everything the model may mention, with all comparisons already made."""
    today = store.local(store.now()).date()
    g, ng = goals(store), nutrition_goals(store)
    f: dict[str, Any] = {"today": today.strftime("%A %-d %B %Y"),
                         "goals": {"steps": int(g["steps_goal"]), "sleep_hours": g["sleep_goal"]}}
    for key, name in (("kcal_goal", "kcal"), ("protein_goal", "protein_g"), ("salt_limit", "salt_g"),
                      ("goal_weight", "weight_kg")):
        if ng.get(key):
            f["goals"][name] = ng[key]
    f["goals"]["bp_home_threshold"] = f"{HOME_BP_HIGH[0]}/{HOME_BP_HIGH[1]}"
    fasting = sorted(store.fasting_weekdays())
    if fasting:
        f["goals"]["fasting_weekdays"] = ", ".join(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][i] for i in fasting)

    steps = store.steps_by_day(15)
    whole = [steps[d]["steps"] for d in sorted(steps) if d < today and steps[d]["steps"]]
    last7, prev7 = whole[-7:], whole[-14:-7]
    if last7:
        f["steps"] = {"days_counted": len(last7), "daily_average": round(statistics.mean(last7)),
                      "days_reaching_goal": sum(1 for v in last7 if v >= g["steps_goal"])}
        if len(prev7) >= 3:
            f["steps"]["daily_average_week_before"] = round(statistics.mean(prev7))

    counts: dict[str, int] = {}
    for w in store.workouts(7):
        counts[w["type"]] = counts.get(w["type"], 0) + 1
    if counts:
        f["workouts_last_7_days"] = counts

    nights = [(v.get("night_seconds", v.get("asleep_seconds", 0)) / 3600, v.get("bed"))
              for d, v in sorted(store.sleep_by_day(7).items()) if v]
    if nights:
        beds = sorted({b[11:16] for _, b in nights if b})
        f["sleep"] = {"nights_counted": len(nights), "average_hours": round(statistics.mean(h for h, _ in nights), 1),
                      "last_night_hours": round(nights[-1][0], 2), "goal_hours": g["sleep_goal"], "bed_times": beds}

    sits = bp_sessions(store.bp_readings(7))
    if sits:
        ts, td = HOME_BP_HIGH
        last = sits[-1]
        ls, ld = last["systolic"], last["diastolic"]

        def cmp(v: int, t: int, name: str) -> str:
            return f"{name} number {v} is {abs(v - t)} {'under' if v < t else 'over' if v > t else 'level with'} {t}"

        f["blood_pressure"] = {
            "latest_sitting_summary": f"Latest sitting averaged {ls}/{ld}: {cmp(ls, ts, 'top')}; {cmp(ld, td, 'bottom')}.",
            "latest_sitting_mixed": (ls >= ts) != (ld >= td),
            "sittings_oldest_first": [{"when": s["time"], "readings": len(s["readings"]),
                                      "average": f"{s['systolic']}/{s['diastolic']}"} for s in sits[-6:]],
            "home_threshold": f"{ts}/{td}",
            "days_with_readings": len({s["time"][:10] for s in sits}),
        }
        if (store.get_setting("gp_context") or "").strip():
            f["blood_pressure"]["gp_follow_up"] = "in the app's GP note"

    w = store.weight_readings(30)
    if w:
        f["weight"] = {"latest_kg": w[-1]["kg"], "readings_counted": len(w),
                       "note": "readings at different times of day can differ by about 1 kg"}
        if ng.get("goal_weight"):
            f["weight"]["goal_kg"] = ng["goal_weight"]
            f["weight"]["kg_above_goal"] = round(w[-1]["kg"] - ng["goal_weight"], 2)

    by_day: dict[Any, dict[str, float]] = {}
    for e in store.food_entries(7):
        d = by_day.setdefault(e["_day"], {"kcal": 0.0, "protein_g": 0.0, "salt_g": 0.0})
        d["kcal"] += e["kcal"] or 0
        d["protein_g"] += e["protein_g"] or 0
        d["salt_g"] += e["salt_g"] or 0
    f["food"] = {"days_logged_of_last_7": len(by_day)}
    if by_day:
        day = sorted(by_day)[-1]
        t = by_day[day]
        f["food"]["latest_logged_day"] = {"date": day.isoformat(), "kcal": round(t["kcal"]),
                                          "protein_g": round(t["protein_g"], 1),
                                          **({"salt_g": round(t["salt_g"], 1)} if t["salt_g"] else {})}
    f["thin"] = bool(len(whole) < 5 or not sits or len(by_day) < 3 or len(w) < 3)
    return f


# ---------------------------------------------------------------- checks on what the model returns
NUM = re.compile(r"(?<![\w.])\d[\d,]*\.?\d*")
BANNED = re.compile(r"hypertensi|diagnos|you have (high|hypertension|diabetes)|stage [12]|stop taking|skip (your|the)|"
                    r"(increase|reduce|change|double|adjust)\b.{0,25}\b(dose|dosage|medication|tablet|pill|losartan)|"
                    r"\bdosage\b|medically|clinically", re.I)
THIN_WORDS = re.compile(r"\b(only|just|too (few|early|short)|limited|not enough|early|few|single|one reading|"
                        r"one sitting|hard to|difficult|can't|cannot|insufficient|thin|more (days|data|readings))\b", re.I)
ALARM = re.compile(r"\bhigh\b|need(s)? attention|concerning|worrying|alarming|dangerous", re.I)
DIR = re.compile(r"\b(above|over|below|under)\b", re.I)


def _numbers(x: Any, acc: set[float]) -> set[float]:
    if isinstance(x, bool):
        return acc
    if isinstance(x, (int, float)):
        acc.add(float(x))
    elif isinstance(x, str):
        for m in NUM.findall(re.sub(r"\d{1,2}:\d{2}", " ", x)):
            try:
                acc.add(float(m.replace(",", "")))
            except ValueError:
                pass
    elif isinstance(x, dict):
        for v in x.values():
            _numbers(v, acc)
    elif isinstance(x, list):
        for v in x:
            _numbers(v, acc)
    return acc


def problems(out: str, facts: dict[str, Any]) -> list[str]:
    """Empty list means the answer is safe to save."""
    try:
        d = json.loads(out)
        head, pts = d["headline"], d["points"]
        assert isinstance(head, str) and isinstance(pts, list) and all(isinstance(p, str) for p in pts)
    except Exception:
        return ["not valid JSON in the agreed shape"]
    head, pts = " ".join(head.split()), [" ".join(p.split()) for p in pts if p.strip()]
    errs: list[str] = []
    if not head or len(head) > 160:
        errs.append("headline length")
    if not 3 <= len(pts) <= 5 or any(len(p) > 320 for p in pts):
        errs.append("points count or length")
    text = head + " " + " ".join(pts)
    allowed = _numbers(facts, set())
    clean = re.sub(r"\d{1,2}:\d{2}", " ", text)
    clean = re.sub(r"\b\d{1,2}(st|nd|rd|th)?\s+(Nov|November|Oct|October|Dec|December|Jan|January)\b|"
                   r"\b(Nov|November|Oct|October|Dec|December|Jan|January)\s+\d{1,2}\b", " ", clean)
    for m in NUM.findall(clean):
        raw = m.replace(",", "").rstrip(".")
        if not raw:
            continue
        n = float(raw)
        dec = len(raw.split(".")[1]) if "." in raw else 0
        ok = any(round(a, dec) == n for a in allowed) or any(abs(a - n) <= 0.003 * a for a in allowed if a >= 1000)
        if not ok and dec == 0 and n <= 7:
            ok = True
        if not ok:
            errs.append(f"number not in the facts: {raw}")
    if BANNED.search(text):
        errs.append("diagnosis or medication wording")
    bp = facts.get("blood_pressure")
    if bp:
        mentions = re.search(r"blood pressure|\bBP\b|\d{2,3}/\d{2,3}", text, re.I)
        if mentions and not re.search(r"\bGP\b|doctor", text, re.I):
            errs.append("blood pressure without the GP")
        if bp["latest_sitting_mixed"]:
            for sent in re.split(r"(?<=[.!?])\s+", text):
                if re.search(r"threshold|135/85", sent, re.I) and DIR.search(sent) and \
                        not re.search(r"\b(top|bottom|systolic|diastolic|upper|lower)\b", sent, re.I):
                    errs.append("compares a mixed reading to the threshold as a whole")
                if re.search(r"blood pressure|\bBP\b|readings?", sent, re.I) and ALARM.search(sent):
                    errs.append("alarming blood-pressure wording")
    if facts.get("thin") and not THIN_WORDS.search(text):
        errs.append("does not say the data is thin")
    return sorted(set(errs))


class AnalysisError(Exception):
    pass


async def generate(client: httpx.AsyncClient, url: str, model: str, facts: dict[str, Any],
                   attempts: int = 2) -> dict[str, Any]:
    """Ask the local model, check the answer, retry once with another seed. Raises AnalysisError."""
    last: list[str] = ["no answer"]
    for i in range(attempts):
        body = {"model": model, "stream": False, "format": SCHEMA, "think": False,
                "options": {"temperature": 0.2, "num_ctx": 8192, "seed": i + 1},
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": "Facts:\n" + json.dumps(facts, indent=1)}]}
        try:
            r = await client.post(url.rstrip("/") + "/api/chat", json=body)
            if r.status_code == 400 and "think" in r.text.lower():
                body.pop("think")
                r = await client.post(url.rstrip("/") + "/api/chat", json=body)
            r.raise_for_status()
            out = r.json()["message"]["content"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise AnalysisError(f"Could not reach the model ({type(e).__name__})") from e
        last = problems(out, facts)
        if not last:
            d = json.loads(out)
            return {"headline": " ".join(d["headline"].split()),
                    "points": [" ".join(p.split()) for p in d["points"] if p.strip()]}
    raise AnalysisError("The model's answer failed the safety checks: " + "; ".join(last))
