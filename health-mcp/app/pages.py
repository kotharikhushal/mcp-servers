"""Server-rendered pages for the wellness web app. No JavaScript, so a strict CSP is possible.

Everything that comes from the database or the user goes through esc(). The coaching notes
are plain rules over the person's own numbers: data, not diagnosis.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .store import Store
from .tools import bp_stats
from .ui import CSS, esc, layout  # noqa: F401  (layout is re-exported for the web app)

DEFAULT_GOALS = {"steps_goal": 8000, "sleep_goal": 7.5}
HOME_BP_HIGH = (135, 85)       # NHS home-reading threshold
BP_URGENT = (180, 120)


NUTRITION_GOALS = {"kcal_goal": (1000, 4000), "protein_goal": (20, 300), "salt_limit": (1, 12)}


def nutrition_goals(store: Store) -> dict[str, float | None]:
    """Optional daily calorie and protein targets and a salt limit; None when not set."""
    out: dict[str, float | None] = {}
    for k in NUTRITION_GOALS:
        try:
            out[k] = float(store.get_setting(k) or "") or None
        except ValueError:
            out[k] = None
    return out


def goals(store: Store) -> dict[str, float]:
    out: dict[str, float] = {}
    for k, default in DEFAULT_GOALS.items():
        try:
            out[k] = float(store.get_setting(k) or default)
        except ValueError:
            out[k] = float(default)
    return out


def _h(seconds: float) -> str:
    m = round(seconds / 60)
    return f"{m // 60} h {m % 60:02d} m"


def notes_for(store: Store) -> list[tuple[str, str]]:
    """(level, text) coaching notes from the person's own recent data."""
    notes: list[tuple[str, str]] = []
    now = store.local(store.now())
    today = now.date()
    bp = store.bp_readings(3)
    if bp:
        last = bp[-1]
        if last["systolic"] >= BP_URGENT[0] or last["diastolic"] >= BP_URGENT[1]:
            notes.append(("bad", f'Your last reading was {last["systolic"]}/{last["diastolic"]}. A reading this '
                          'high should be checked with your GP or NHS 111 today. With chest pain, '
                          'breathlessness, confusion or a severe headache, call 999.'))
        elif last["systolic"] >= HOME_BP_HIGH[0] or last["diastolic"] >= HOME_BP_HIGH[1]:
            notes.append(("warn", f'Last reading {last["systolic"]}/{last["diastolic"]} is above the '
                          f'{HOME_BP_HIGH[0]}/{HOME_BP_HIGH[1]} home threshold. Keep logging morning and '
                          'evening: your GP will look at the pattern across several days.'))
    todays = [r for r in bp if r["time"][:10] == today.isoformat()]
    if not todays and now.hour >= 9:
        notes.append(("", "No blood pressure logged today yet. Sit quietly for 5 minutes, arm supported, "
                      "and take two readings a minute or so apart."))
    sleep = store.sleep_by_day(2).get(today)
    if sleep and sleep["asleep_seconds"] < 6 * 3600:
        notes.append(("warn", f'You slept {_h(sleep["asleep_seconds"])} last night, under 6 hours. '
                      'An earlier, steady bedtime tonight would help.'))
    st = store.status()
    if st.get("stale") and st.get("last_sync"):
        notes.append(("warn", f'Watch data last synced {st["last_sync"]}, so today may be incomplete.'))
    if not notes:
        notes.append(("", "Nothing needs attention right now."))
    return notes


def log_page(store: Store, csrf: str) -> str:
    today = store.local(store.now()).date()
    fasting = today in store.fasting_days(1)
    tok = f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
    when = ('<label>Time (leave empty for now)</label><input type="datetime-local" name="when">')
    meals = [e for e in store.food_entries(2) if e["source"] == "manual"][-5:]
    recent = "".join(
        f'<tr><td>{esc(e["time"])}</td><td>{esc(e["name"])}</td><td>{e["kcal"]} kcal</td>'
        f'<td><form method="post" action="/log/delete" style="margin:0">{tok}'
        f'<input type="hidden" name="kind" value="nutrition"><input type="hidden" name="id" value="{e["id"]}">'
        f'<button class="link">Delete</button></form></td></tr>' for e in reversed(meals))
    return (
        '<h1>Quick log</h1>'
        f'<div class="card"><h2>Blood pressure</h2><form method="post" action="/log/bp">{tok}'
        '<div class="row"><div><label>Systolic</label><input name="systolic" type="number" inputmode="numeric" required></div>'
        '<div><label>Diastolic</label><input name="diastolic" type="number" inputmode="numeric" required></div>'
        '<div><label>Pulse</label><input name="pulse" type="number" inputmode="numeric"></div></div>'
        f'{when}<label>Note</label><input name="note" maxlength="200" placeholder="e.g. after medication">'
        '<button class="go">Save reading</button></form></div>'
        f'<div class="card"><h2>Weight</h2><form method="post" action="/log/weight">{tok}'
        '<label>Kilograms</label><input name="kg" type="number" step="0.1" inputmode="decimal" required>'
        f'{when}<button class="go">Save weight</button></form></div>'
        f'<div class="card"><h2>Meal</h2><form method="post" action="/log/meal">{tok}'
        '<label>What did you eat?</label><input name="name" maxlength="200" required>'
        '<div class="row"><div><label>kcal</label><input name="kcal" type="number" step="1" inputmode="numeric" required></div>'
        '<div><label>Protein g</label><input name="protein" type="number" step="0.1" inputmode="decimal"></div>'
        '<div><label>Salt g</label><input name="salt" type="number" step="0.1" inputmode="decimal"></div></div>'
        f'{when}<button class="go">Save meal</button></form>'
        f'{"<table>" + recent + "</table>" if recent else ""}</div>'
        f'<div class="card"><h2>Fasting day</h2><form method="post" action="/log/fasting">{tok}'
        f'<input type="hidden" name="on" value="{"0" if fasting else "1"}">'
        f'<p class="m">Today is {"marked as a fasting day" if fasting else "not marked as a fasting day"}.</p>'
        f'<button class="go">{"Unmark today" if fasting else "Mark today as fasting"}</button></form></div>')


def login_page(message: str = "") -> str:
    return (f'<!doctype html><html lang="en-GB"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Wellness</title>'
            f'<style>{CSS}</style></head><body style="padding-bottom:0"><main class="page" style="padding-top:12vh">'
            f'<h1>Wellness</h1><p class="lede">Your health at a glance, for you and Shivani.</p>'
            f'{"<div class=note>" + esc(message) + "</div>" if message else ""}'
            '<p><a class="cta" href="/login">Sign in</a></p></main></body></html>')


def _avg(vals: list[float]) -> float | None:
    return round(sum(vals) / len(vals), 1) if vals else None


def gp_page(store: Store, name: str, days: int) -> str:
    """Printable home blood pressure record for a GP appointment."""
    days = days if days in (7, 14, 28) else 7
    today = store.local(store.now()).date()
    first = today - timedelta(days=days - 1)
    rs = [r for r in store.bp_readings(days) if r["time"][:10] >= first.isoformat()]
    context = store.get_setting("gp_context") or ""
    opts = " · ".join(f'<a href="/gp?days={d}"><b>{d} days</b></a>' if d == days else f'<a href="/gp?days={d}">{d} days</a>'
                      for d in (7, 14, 28))
    head = (f'<h1>Home blood pressure record</h1>'
            f'<div class="card"><table><tr><td>Patient</td><td>{esc(name)}</td></tr>'
            f'<tr><td>Period</td><td>{first.strftime("%-d %b %Y")} to {today.strftime("%-d %b %Y")}</td></tr>'
            f'<tr><td>Prepared</td><td>{today.strftime("%-d %b %Y")}</td></tr></table>'
            f'<p class="noprint m">Show: {opts}</p></div>')
    ctx = (f'<div class="card"><h2>Context</h2><p style="white-space:pre-wrap;margin:0">{esc(context)}</p></div>'
           if context.strip() else "")
    if not rs:
        return head + ctx + '<div class="card">No readings in this period.</div>'
    st = bp_stats(rs)

    def blk(b: dict[str, Any] | None) -> str:
        return f'{b["systolic"]:.0f}/{b["diastolic"]:.0f} mmHg ({b["readings"]} readings)' if b else "not enough data"

    hi = max(rs, key=lambda r: (r["systolic"], r["diastolic"]))
    lo = min(rs, key=lambda r: (r["systolic"], r["diastolic"]))
    pulse = _avg([float(r["pulse"]) for r in rs if r["pulse"]])
    summary = (f'<div class="card"><h2>Summary</h2><table>'
               f'<tr><td>All readings</td><td>{blk(st["average_all"])}</td></tr>'
               f'<tr><td>Excluding the first day</td><td>{blk(st["average_excluding_first_day"])}</td></tr>'
               f'<tr><td>Morning readings</td><td>{blk(st["morning_before_noon"])}</td></tr>'
               f'<tr><td>Evening readings</td><td>{blk(st["evening_noon_onwards"])}</td></tr>'
               f'<tr><td>Highest</td><td>{hi["systolic"]}/{hi["diastolic"]} ({esc(hi["time"])})</td></tr>'
               f'<tr><td>Lowest</td><td>{lo["systolic"]}/{lo["diastolic"]} ({esc(lo["time"])})</td></tr>'
               f'<tr><td>Average pulse</td><td>{esc(pulse if pulse is not None else "not recorded")}</td></tr></table></div>')
    by_day: dict[str, dict[str, list[str]]] = {}
    for r in rs:
        slot = "am" if r["time"][11:13] < "12" else "pm"
        by_day.setdefault(r["time"][:10], {"am": [], "pm": []})[slot].append(
            f'{r["systolic"]}/{r["diastolic"]}' + (f' ({r["pulse"]})' if r["pulse"] else ""))
    full = sum(1 for v in by_day.values() if len(v["am"]) >= 2 and len(v["pm"]) >= 2)
    rows = "".join(f'<tr><td>{esc(date.fromisoformat(d).strftime("%a %-d %b"))}</td>'
                   f'<td>{esc(", ".join(v["am"]) or "–")}</td><td>{esc(", ".join(v["pm"]) or "–")}</td></tr>'
                   for d, v in sorted(by_day.items()))
    table = (f'<div class="card"><h2>Readings by day</h2><table><tr><th>Day</th><th>Morning (pulse)</th>'
             f'<th>Evening (pulse)</th></tr>{rows}</table>'
             f'<p class="m">{len(by_day)} days with readings; {full} with at least two morning and two evening readings. '
             'The common home-monitoring protocol asks for 4 to 7 days, two readings each morning and evening, '
             'and discards the first day.</p></div>')
    sleep = [v["asleep_seconds"] / 3600 for v in store.sleep_by_day(days).values() if v]
    steps = [v["steps"] for v in store.steps_by_day(days).values() if v["records"]]
    weights = [w for w in store.weight_readings(days)]
    life = []
    if sleep:
        life.append(f"Average sleep {_avg(sleep)} hours a night")
    if steps:
        life.append(f"Average steps {round(sum(steps) / len(steps))} a day")
    if len(weights) >= 2:
        life.append(f'Weight {weights[0]["kg"]:.1f} kg to {weights[-1]["kg"]:.1f} kg')
    lifestyle = ('<div class="card"><h2>Other measures</h2><p style="margin:0">' + esc(" · ".join(life)) + "</p></div>"
                 if life else "")
    foot = ('<p class="m">Readings were taken at home by the patient with their own monitor and recorded in the Wellness app. '
            'This record is for discussion with a clinician and is not a diagnosis. Use your browser’s Print or '
            'Save as PDF to keep a copy.</p>')
    return head + ctx + summary + table + lifestyle + foot


def _last_import(store: Store) -> str:
    v = store.get_setting("last_import")
    return f'<p><b>Last import:</b> {esc(v)}</p>' if v else '<p class="m">Nothing imported yet.</p>'


def settings_page(store: Store, csrf: str, ntfy_base: str) -> str:
    tok = f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
    on = store.get_setting("reminders_enabled") == "1"
    topic = store.get_setting("reminder_topic") or ""
    am = store.get_setting("reminder_morning") or "08:00"
    pm = store.get_setting("reminder_evening") or "21:00"
    ctx = store.get_setting("gp_context") or ""
    g = goals(store)
    ng = nutrition_goals(store)

    def nv(k: str) -> str:
        return f"{ng[k]:g}" if ng[k] else ""
    sub = (f'<p class="m">On your phone, install the free <b>ntfy</b> app and subscribe to this topic: '
           f'<b>{esc(topic)}</b> (server {esc(ntfy_base)}). Anyone who knows the topic name can read the '
           f'reminders, so the messages never include health data.</p>' if topic else
           '<p class="m">Saving with reminders on will create a private topic name for you.</p>')
    return (
        '<h1>Settings</h1>'
        f'<div class="card"><h2>Daily goals</h2><form method="post" action="/settings/goals">{tok}'
        '<div class="row" style="grid-template-columns:1fr 1fr"><div><label>Steps a day</label>'
        f'<input name="steps_goal" type="number" inputmode="numeric" min="1000" max="50000" step="100" value="{g["steps_goal"]:.0f}" required></div>'
        '<div><label>Sleep (hours)</label>'
        f'<input name="sleep_goal" type="number" inputmode="decimal" min="4" max="12" step="0.25" value="{g["sleep_goal"]:g}" required></div></div>'
        '<div class="row" style="grid-template-columns:1fr 1fr 1fr"><div><label>Calories</label>'
        f'<input name="kcal_goal" type="number" inputmode="numeric" min="1000" max="4000" step="50" value="{nv("kcal_goal")}" placeholder="optional"></div>'
        '<div><label>Protein (g)</label>'
        f'<input name="protein_goal" type="number" inputmode="numeric" min="20" max="300" step="5" value="{nv("protein_goal")}" placeholder="optional"></div>'
        '<div><label>Salt max (g)</label>'
        f'<input name="salt_limit" type="number" inputmode="decimal" min="1" max="12" step="0.5" value="{nv("salt_limit")}" placeholder="optional"></div></div>'
        '<button class="go">Save goals</button></form></div>'
        f'<div class="card"><h2>Blood pressure reminders</h2><form method="post" action="/settings/reminders">{tok}'
        f'<label><input type="checkbox" name="enabled" value="1" style="width:auto"{" checked" if on else ""}> '
        'Send me reminders</label>'
        f'<div class="row"><div><label>Morning</label><input type="time" name="morning" value="{esc(am)}"></div>'
        f'<div><label>Evening</label><input type="time" name="evening" value="{esc(pm)}"></div></div>'
        f'<button class="go">Save</button></form>{sub}'
        + (f'<form method="post" action="/settings/test">{tok}<button class="go">Send a test reminder</button></form>'
           if topic else "") + '<p class="m">A reminder is skipped when you have already logged a reading in that half of the day.</p></div>'
        f'<div class="card"><h2>Note for your GP report</h2><form method="post" action="/settings/context">{tok}'
        f'<label>Shown at the top of the GP report (for example your medication and start date)</label>'
        f'<textarea name="context" rows="4" maxlength="600" style="width:100%;padding:9px;border:1px solid var(--line);'
        f'border-radius:8px;background:var(--bg);color:var(--ink);font:inherit">{esc(ctx)}</textarea>'
        '<button class="go">Save note</button></form></div>'
        '<div class="card"><h2>Import from Apple Health</h2>'
        '<p class="m">Upload an export made by the Health Auto Export app (the .zip or the .json). '
        f'Importing the same period again is safe.</p>{_last_import(store)}<p><a class="cta" href="/import">Import a file</a></p></div>'
        f'<div class="card"><h2>Account</h2><form method="post" action="/logout">{tok}'
        '<button class="ghost">Sign out</button></form></div>')


def import_page(store: Store, csrf: str) -> str:
    tok = f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
    return ('<h1>Import from Apple Health</h1><div class="card">'
            f'<form method="post" action="/import" enctype="multipart/form-data">{tok}'
            '<label>Health Auto Export file (.zip or .json, up to 25 MB)</label>'
            '<input type="file" name="file" accept=".zip,.json" required>'
            '<button class="go">Import</button></form>'
            '<p class="m">Large files can take a few seconds. The result is shown at the top of the next page.</p>'
            f'{_last_import(store)}'
            '<p class="m">This adds steps, sleep, heart rate summaries, weight, workouts and calories to your own '
            'record. Nothing is shared with anyone else.</p></div>')
