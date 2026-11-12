"""Today screen and detail screens. Each detail screen follows one pattern: a hero number, a range
selector, one chart, three stats, a few plain-language notes and the underlying entries.

Notes are rules over the person's own numbers: data, not diagnosis.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Sequence

from . import charts
from .pages import BP_URGENT, bp_over_text, HOME_BP_HIGH, goals, notes_for, nutrition_goals, when_label
from .store import Store
from .tools import bp_stats
from .ui import chips, esc, icon, legend, stats, tile

RANGES = (7, 30, 90)
MINUS = "\u2212"


def hm(seconds: float) -> str:
    m = round(seconds / 60)
    return f"{m // 60} h {m % 60:02d} m"


def hm_short(seconds: float) -> str:
    m = round(seconds / 60)
    return f"{m // 60}h {m % 60:02d}m"


def signed(v: float, unit: str = "", digits: int = 1) -> str:
    if abs(v) < 0.05 * (10 ** (1 - digits)):
        return f"no change"
    return f'{"+" if v > 0 else MINUS}{abs(v):,.{digits}f}{unit}'


def _day(d: date) -> str:
    return d.strftime("%a %-d %b")


def _when(store: Store, t: str) -> str:
    return when_label(store, t)


def _axis(dates: Sequence[date], d: int) -> tuple[list[str], int]:
    if d <= 7:
        return [x.strftime("%a") for x in dates], 1
    return [x.strftime("%-d %b") for x in dates], (5 if d <= 30 else 14)


def _xs(times: Sequence[str], dates: Sequence[date]) -> list[float]:
    n = len(dates)
    out = []
    for t in times:
        dd = date.fromisoformat(t[:10])
        frac = (int(t[11:13]) * 60 + int(t[14:16])) / 1440
        out.append(max(0.0, min(((dd - dates[0]).days + frac) / n, 1.0)))
    return out


def _axis_ticks(dates: Sequence[date], d: int) -> list[tuple[float, str]]:
    labs, every = _axis(dates, d)
    n = len(dates)
    return [((i + 0.5) / n, labs[i]) for i in range(n - 1, -1, -every) if (i + 0.5) / n > 0.07][::-1]


def _avg(vals: Sequence[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def _delete(csrf: str, kind: str, rid: int) -> str:
    """Two taps: Delete opens a confirmation, so a stray touch cannot remove an entry."""
    return ('<details class="confirm"><summary role="button">Delete</summary>'
            '<div class="ask">Delete this entry?</div>'
            f'<form method="post" action="/log/delete" style="margin:0;text-align:right"><input type="hidden" name="csrf" value="{esc(csrf)}">'
            f'<input type="hidden" name="kind" value="{esc(kind)}"><input type="hidden" name="id" value="{rid}">'
            '<button class="link" aria-label="Confirm delete this entry">Yes, delete</button></form></details>')


def bp_sessions(rs: Sequence[dict[str, Any]], gap_min: int = 30) -> list[dict[str, Any]]:
    """Readings taken within 30 minutes of each other are one sitting, shown as their average."""
    out: list[dict[str, Any]] = []
    for r in rs:
        mins = int(r["time"][11:13]) * 60 + int(r["time"][14:16])
        if out and out[-1]["time"][:10] == r["time"][:10] and mins - out[-1]["_last"] <= gap_min:
            out[-1]["readings"].append(r)
            out[-1]["_last"] = mins
        else:
            out.append({"time": r["time"], "readings": [r], "_last": mins})
    for s in out:
        n = len(s["readings"])
        s["systolic"] = round(sum(x["systolic"] for x in s["readings"]) / n)
        s["diastolic"] = round(sum(x["diastolic"] for x in s["readings"]) / n)
        pulses = [x["pulse"] for x in s["readings"] if x["pulse"]]
        s["pulse"] = round(sum(pulses) / len(pulses)) if pulses else None
    return out


def _night(v: dict[str, Any]) -> float:
    """Seconds slept overnight, leaving daytime naps out."""
    return v.get("night_seconds", v["asleep_seconds"])


def _meal(hour: int) -> str:
    return ("Breakfast" if 4 <= hour < 11 else "Lunch" if 11 <= hour < 15 else "Snack" if 15 <= hour < 17
            else "Dinner" if 17 <= hour < 22 else "Snack")


def _meter(label: str, value: float, goal: float, unit: str, *, tint: str, limit: bool = False, digits: int = 0) -> str:
    frac = max(0.0, min(value / goal, 1.0)) if goal else 0.0
    over = value > goal
    left = goal - value
    sub = (f'{abs(left):,.{digits}f} {unit} {"over" if over else "left"}' if not limit or over
           else f'{left:,.{digits}f} {unit} under your limit')
    return (f'<div class="meter{" over" if over else ""}" style="--tc:var({tint})">'
            f'<div class="mh"><b>{esc(label)}</b><span>{value:,.{digits}f} of {goal:,.{digits}f} {esc(unit)}</span></div>'
            f'<div class="tr" role="progressbar" aria-label="{esc(label)}" aria-valuemin="0" aria-valuemax="{goal:g}" '
            f'aria-valuenow="{value:.{digits}f}"><i style="width:{frac * 100:.0f}%"></i></div>'
            f'<div class="ms">{esc(sub)}</div></div>')


def _rows(items: Sequence[tuple[str, str, str, str]]) -> str:
    return '<ul class="rows">' + "".join(
        f'<li><div class="l"><b>{esc(a)}</b><span>{esc(b)}</span></div>'
        f'<div><div class="r">{esc(c)}</div>{x}</div></li>' for a, b, c, x in items) + "</ul>"


def _notes(lines: Sequence[tuple[str, str]]) -> str:
    if not lines:
        return ""
    return '<div class="card"><h2>Notes</h2>' + "".join(
        f'<div class="note {lvl}">{esc(t)}</div>' for lvl, t in lines) + "</div>"


def _head(name: str, ic: str, title: str) -> str:
    return f'<div class="dhead">{icon(ic)}<span>{esc(title)}</span></div>'


def _empty(msg: str, action: str = "") -> str:
    return f'<div class="card"><p class="m" style="margin:0">{esc(msg)}</p>{action}</div>'


ADD = '<p style="margin:12px 0 0"><a class="cta" href="/log">Add an entry</a></p>'


# ------------------------------------------------------------------------------------------ today
def _greeting(hour: int) -> str:
    return "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"


def skipped_ids(store: Store, day: date) -> set[str]:
    return {x for x in (store.get_setting(f"skip:{day.isoformat()}") or "").split(",") if x}


def meals_card(store: Store, menu: list[dict[str, Any]] | None, error: bool, csrf: str) -> str:
    """Today's planned Mealie meals with one tap to log one as eaten (or skip it)."""
    if error:
        return '<div class="card"><h2>Today\'s meals</h2><p class="m" style="margin:0">Meal plan unavailable right now.</p></div>'
    if menu is None:
        return ""
    today = store.local(store.now()).date()
    if not menu:
        return ('<div class="card"><h2>Today\'s meals</h2><p class="m" style="margin:0">Nothing planned for today. '
                'Ask Claude to plan your meals, or log one yourself.</p><p style="margin:12px 0 0"><a class="cta" href="/log">Log a meal</a></p></div>')
    skipped = skipped_ids(store, today)
    eaten_names = [e["name"] for e in store.food_entries(1) if e["_day"] == today]
    fasting = today in store.fasting_days(1)
    ng = nutrition_goals(store)
    rows, planned, eaten = [], 0.0, 0.0
    for m in menu:
        n = m.get("per_serving") or {}
        kc = n.get("kcal")
        name = m.get("recipe_name") or m.get("recipe") or m.get("note") or "Meal"
        was_eaten = any(x.startswith(f"{name} x") and x.endswith("(from Mealie)") for x in eaten_names)
        was_skipped = str(m["id"]) in skipped
        if was_eaten:
            eaten += kc or 0
        if not was_skipped:
            planned += kc or 0
        bits = [(m["meal"] or "").capitalize()]
        if kc:
            bits.append(f"{kc:,.0f} kcal")
        if n.get("protein_g"):
            bits.append(f'{n["protein_g"]:.0f} g protein')
        sub = " · ".join(b for b in bits if b)
        eid = esc(m["id"])
        if was_eaten:
            act, cls = '<span class="pill">Logged</span>', ""
        elif was_skipped:
            act = (f'<form method="post" action="/meals/skip"><input type="hidden" name="csrf" value="{esc(csrf)}">'
                   f'<input type="hidden" name="entry_id" value="{eid}"><input type="hidden" name="on" value="0">'
                   '<button class="mini ghost">Undo skip</button></form>')
            cls = " done"
        elif not m.get("slug") or not kc:
            act, cls = '<span class="pill">No calories</span>', ""
        else:
            def f(path: str, label: str, cls_: str = "mini") -> str:
                return (f'<form method="post" action="{path}"><input type="hidden" name="csrf" value="{esc(csrf)}">'
                        f'<input type="hidden" name="entry_id" value="{eid}"><input type="hidden" name="on" value="1">'
                        f'<button class="{cls_}" aria-label="{esc(label)}: {esc(name)}">{esc(label)}</button></form>')
            act, cls = f'<div class="acts">{f("/meals/eat", "I ate this")}<a class="mini ghost" style="display:inline-flex;align-items:center" href="/meals/swap?entry_id={eid}" aria-label="Swap: {esc(name)}">Swap</a>{f("/meals/skip", "Skip", "mini ghost")}</div>', ""
        rows.append(f'<li class="{cls.strip()}"><div class="l"><b>{esc(name)}</b><span>{esc(sub)}</span></div>{act}</li>')
    foot = f"Eaten {eaten:,.0f} kcal of {planned:,.0f} planned"
    if fasting:
        foot += " · fasting day"
    elif ng["kcal_goal"]:
        foot += f' · goal {ng["kcal_goal"]:,.0f}'
    return (f'<div class="card"><h2>Today\'s meals</h2><ul class="rows">{"".join(rows)}</ul>'
            f'<p class="m" style="margin:10px 0 0">{esc(foot)}</p></div>')


def _ai_card(store: Store) -> str:
    """The latest AI analysis saved through the connector, or nothing if none yet."""
    import json
    raw = store.get_setting("ai_analysis")
    if not raw:
        return ""
    try:
        a = json.loads(raw)
        when = datetime.fromisoformat(a["at"])
        head, pts = str(a["headline"]), [str(x) for x in a["points"]]
    except (ValueError, KeyError, TypeError):
        return ""
    age_h = (store.now() - when).total_seconds() / 3600
    lbl = store.local(when).strftime("%a %H:%M")
    stale = ' <span class="pill">Older</span>' if age_h > 36 else ""
    items = "".join(f"<li>{esc(x)}</li>" for x in pts)
    return (f'<div class="card ai"><h2>AI analysis{stale}</h2><p class="aih">{esc(head)}</p>'
            f'<ul class="aip">{items}</ul>'
            f'<p class="m">Written by Claude from your own data, {esc(lbl)}. Not a diagnosis. '
            'Ask Claude to refresh it.</p></div>')


def today_page(store: Store, name: str, menu: list[dict[str, Any]] | None = None, menu_error: bool = False,
               csrf: str = "") -> str:
    now = store.local(store.now())
    today = now.date()
    g = goals(store)
    steps = store.steps_by_day(7)
    sleep7 = store.sleep_by_day(7)
    s_today = steps[today]["steps"] if steps[today]["records"] else None
    night = next((sleep7[d] for d in sorted(sleep7, reverse=True) if sleep7[d]), None)

    steps_ring = charts.ring((s_today or 0) / g["steps_goal"], "--c-steps",
                             big=f"{s_today:,}" if s_today is not None else "–", small=f'of {g["steps_goal"]:,.0f}',
                             summary=f"Steps today: {s_today if s_today is not None else 'no data'} of {g['steps_goal']:.0f}")
    goal_s = g["sleep_goal"] * 3600
    sleep_ring = charts.ring(_night(night) / goal_s if night else 0, "--c-sleep",
                             big=hm_short(_night(night)) if night else "–", small=f'of {g["sleep_goal"]:g} h',
                             summary=f"Sleep last night: {hm(_night(night)) if night else 'no data'}")
    hero = ('<div class="card hero">'
            f'<a href="/steps">{steps_ring}<div class="cap">Steps</div><div class="sub">today</div></a>'
            f'<a href="/sleep">{sleep_ring}<div class="cap">Sleep</div><div class="sub">last night</div></a></div>')

    notes = "".join(f'<div class="note {lvl}">{esc(t)}</div>' for lvl, t in notes_for(store))
    coach = (f'<div class="card coach"><div class="face" aria-hidden="true">C</div><div class="notes">'
             f'<h2>Coach notes</h2>{notes}</div></div>')

    coach = _ai_card(store) + coach
    meals = meals_card(store, menu, menu_error, csrf)
    tiles = []
    hb = store.heart_by_day(7)
    hd = [(d, v) for d, v in sorted(hb.items()) if v]
    if hd:
        d0, v = hd[-1]
        tiles.append(tile("/heart", "Heart rate", "heart", "--c-heart", f'{v["avg"]:.0f}', "bpm",
                          f'{v["min"]}–{v["max"]} · {"today" if d0 == today else _day(d0)}',
                          charts.spark([hb[d].get("avg") if hb[d] else None for d in sorted(hb)], "--c-heart")))
    else:
        tiles.append(tile("/heart", "Heart rate", "heart", "--c-heart", "–", "", "No data yet"))

    bps = bp_sessions(store.bp_readings(14))
    if bps:
        b = bps[-1]
        n = len(b["readings"])
        tiles.append(tile("/bp", "Blood pressure", "bp", "--c-heart", f'{b["systolic"]}/{b["diastolic"]}', "mmHg",
                          (f"{n} readings, average · " if n > 1 else "") + _when(store, b["time"]),
                          charts.spark([float(r["systolic"]) for r in bps], "--c-heart")))
    else:
        tiles.append(tile("/bp", "Blood pressure", "bp", "--c-heart", "–", "", "Log a reading"))

    ws = store.weight_readings(60)
    if ws:
        w = ws[-1]
        old = next((x for x in ws if x["time"][:10] >= (today - timedelta(days=30)).isoformat()), ws[0])
        sub = f'{signed(w["kg"] - old["kg"], " kg")} in 30 days' if old is not w else _when(store, w["time"])
        tiles.append(tile("/weight", "Weight", "weight", "--c-weight", f'{w["kg"]:.1f}', "kg", sub,
                          charts.spark([x["kg"] for x in ws], "--c-weight")))
    else:
        tiles.append(tile("/weight", "Weight", "weight", "--c-weight", "–", "", "Log your weight"))

    food = store.food_entries(7)
    kcal_by = {d: 0 for d in sorted(steps)}
    for e in food:
        if e["_day"] in kcal_by:
            kcal_by[e["_day"]] += e["kcal"]
    fasting = today in store.fasting_days(1)
    today_food = [e for e in food if e["_day"] == today]
    kg = nutrition_goals(store)["kcal_goal"]
    left = f'{abs(kg - kcal_by[today]):,.0f} kcal {"left" if kg >= kcal_by[today] else "over"}' if kg and today_food else None
    tiles.append(tile("/food", "Food", "food", "--c-food", f'{kcal_by[today]:,}' if (today_food or fasting) else "–",
                      "kcal" if today_food else "",
                      "Fasting day" if fasting else (left or (f'{len(today_food)} entries today' if today_food else "Nothing logged today")),
                      charts.spark([float(v) if v else None for v in kcal_by.values()], "--c-food")))

    wk = store.workouts(7)
    mins = sum(w["minutes"] for w in wk)
    per_day = {d: 0 for d in sorted(steps)}
    for w in wk:
        k = date.fromisoformat(w["date"])
        if k in per_day:
            per_day[k] += w["minutes"]
    tiles.append(tile("/activity", "Workouts", "work", "--c-work", str(len(wk)), "this week" if wk else "",
                      f'{hm(mins * 60)} active · last: {wk[-1]["type"].capitalize()}' if wk else "No workouts in the last 7 days",
                      charts.spark([float(v) if v else None for v in per_day.values()], "--c-work"), wide=True))

    st = store.status()
    sync = (f'<p class="m" style="text-align:center">Watch data synced {esc(st["last_sync"])}</p>'
            if st.get("last_sync") else "")
    return (f'<div class="greet"><h1>{_greeting(now.hour)}, {esc(name)}</h1><p>{esc(now.strftime("%A %-d %B"))}</p></div>'
            f'{hero}{coach}{meals}<div class="sect"><h2>Your numbers</h2></div><div class="tiles">{"".join(tiles)}</div>{sync}'
            '<p class="m" style="text-align:center">Notes are based on your own numbers. They are not a diagnosis.</p>')


# ---------------------------------------------------------------------------------------- details
def sleep_page(store: Store, d: int) -> str:
    g = goals(store)
    data = store.sleep_by_day(d)
    dates = sorted(data)
    nights = [data[x] for x in dates]
    have = [(x, v) for x, v in zip(dates, nights) if v]
    head = _head("", "sleep", "Sleep")
    if not have:
        return head + '<h1>Sleep</h1>' + chips("/sleep", d) + _empty("No sleep recorded in this period.")
    last_d, last = have[-1]
    goal_s = g["sleep_goal"] * 3600
    avg = _avg([_night(v) for _, v in have]) or 0
    per = []
    for v in nights:
        if not v:
            per.append(None)
            continue
        st = v["stages"]
        per.append({"deep": st.get("deep", 0) / 3600, "rem": st.get("rem", 0) / 3600,
                    "light": (st.get("light", 0) + st.get("asleep", 0)) / 3600, "awake": st.get("awake", 0) / 3600})
    labs, every = _axis(dates, d)
    chart = charts.stacked_sleep(per, labs, label_every=every,
                                 summary=f"Hours in bed per night over {d} days, split by sleep stage")
    best = max(have, key=lambda p: _night(p[1]))
    worst = min(have, key=lambda p: _night(p[1]))
    stage_txt = " · ".join(f'{k} {hm_short(v)}' for k, v in last["stages"].items() if k not in ("unknown",) and v)
    notes: list[tuple[str, str]] = []
    gap = avg - goal_s
    notes.append(("" if gap >= 0 else "warn",
                  f'Average {hm(avg)} a night, {hm(abs(gap))} {"above" if gap >= 0 else "below"} your {g["sleep_goal"]:g} hour goal.'))
    naps = [(x, v["nap_seconds"]) for x, v in have if v.get("nap_seconds")]
    if naps:
        notes.append(("", f'{len(naps)} nap{"s" if len(naps) != 1 else ""} not counted in the nightly figures '
                          f'(latest {hm_short(naps[-1][1])} on {_day(naps[-1][0])}).'))
    short = sum(1 for _, v in have if _night(v) < 6 * 3600)
    if short:
        notes.append(("warn", f'{short} of {len(have)} nights were under 6 hours.'))
    rows = [(_day(x), f'{v.get("bed", "")[11:16]} to {v.get("wake", "")[11:16]}'
             + (f' · {round(v["awake_seconds"] / 60)} min awake' if v["awake_seconds"] else ""), hm_short(_night(v)), "")
            for x, v in reversed(have[-14:])]
    return (head + f'<div class="hero1">{esc(hm_short(_night(last)))}</div>'
            f'<div class="delta">{"Last night" if last_d == store.local(store.now()).date() else _day(last_d)}'
            f'{" · " + esc(stage_txt) if stage_txt else ""}</div>'
            + chips("/sleep", d)
            + f'<div class="card">{chart}{legend([("--st-deep", "Deep"), ("--st-rem", "REM"), ("--st-light", "Light"), ("--st-awake", "Awake")])}</div>'
            + stats([("Average", hm_short(avg)), ("Longest", hm_short(_night(best[1]))),
                     ("Shortest", hm_short(_night(worst[1])))])
            + _notes(notes) + f'<div class="card"><h2>Nights</h2>{_rows(rows)}</div>')


def steps_page(store: Store, d: int) -> str:
    g = goals(store)
    data = store.steps_by_day(d)
    dates = sorted(data)
    today = store.local(store.now()).date()
    vals = [data[x]["steps"] if data[x]["records"] else None for x in dates]
    head = _head("", "steps", "Steps")
    have = [(x, v) for x, v in zip(dates, vals) if v is not None]
    if not have:
        return head + '<h1>Steps</h1>' + chips("/steps", d) + _empty("No step data in this period.")
    full = [v for x, v in have if x != today] or [v for _, v in have]
    avg = sum(full) / len(full)
    hit = sum(1 for v in full if v >= g["steps_goal"])
    labs, every = _axis(dates, d)
    chart = charts.bars(vals, labs, "--c-steps", goal=g["steps_goal"], label_every=every,
                        summary=f"Steps per day over {d} days. Dashed line is your goal of {g['steps_goal']:.0f}.")
    last_d, last = have[-1]
    best = max(have, key=lambda p: p[1])
    notes = [("", f'You reached {g["steps_goal"]:,.0f} steps on {hit} of {len(full)} complete days.'
              if hit else f'No day has reached {g["steps_goal"]:,.0f} steps in this period yet.')]
    if last_d == today:
        notes.append(("", f'Today is still in progress, so it is left out of the average.'))
    rows = [(_day(x), "today so far" if x == today else ("goal reached" if v >= g["steps_goal"] else ""), f"{v:,}", "")
            for x, v in reversed(have[-14:])]
    return (head + f'<div class="hero1">{last:,}<small>steps</small></div>'
            f'<div class="delta">{"Today" if last_d == today else _day(last_d)} · daily average {avg:,.0f}</div>'
            + chips("/steps", d) + f'<div class="card">{chart}</div>'
            + stats([("Average", f"{avg:,.0f}"), ("Best day", f"{best[1]:,}"), ("Goal days", f"{hit}/{len(full)}")])
            + _notes(notes) + f'<div class="card"><h2>Days</h2>{_rows(rows)}</div>')


def heart_page(store: Store, d: int) -> str:
    data = store.heart_by_day(d)
    dates = sorted(data)
    head = _head("", "heart", "Heart rate")
    have = [(x, data[x]) for x in dates if data[x]]
    if not have:
        return head + '<h1>Heart rate</h1>' + chips("/heart", d) + _empty("No heart rate data in this period.")
    labs, every = _axis(dates, d)
    avgs = [data[x].get("avg") if data[x] else None for x in dates]
    band = [(data[x]["min"], data[x]["max"]) if data[x] else (None, None) for x in dates]
    chart = charts.line([("Average", "--c-heart", avgs)], labs, band=band, band_color="--c-heart", label_every=every,
                        summary=f"Average heart rate per day over {d} days, with the daily minimum to maximum shaded",
                        direct_labels=False, fmt=lambda v: f"{v:.0f}")
    last_d, last = have[-1]
    overall = _avg([v["avg"] for _, v in have]) or 0
    lows = [v.get("resting") or v.get("p10") for _, v in have if v.get("resting") or v.get("p10")]
    hrv = [v["hrv"] for _, v in have if v.get("hrv")]
    st = [("Average", f"{overall:.0f} bpm"), ("Lowest", f'{min(v["min"] for _, v in have)} bpm'),
          ("Highest", f'{max(v["max"] for _, v in have)} bpm')]
    notes = []
    if lows:
        notes.append(("", f'Your typical resting-range heart rate is about {_avg(lows):.0f} bpm. A steady or falling figure over weeks usually reflects fitness and good recovery.'))
    if hrv:
        notes.append(("", f'Average heart rate variability {_avg(hrv):.0f} ms.'))
    rows = [(_day(x), f'{v["min"]}–{v["max"]} bpm' + (f' · resting {v["resting"]:.0f}' if v.get("resting") else ""),
             f'{v["avg"]:.0f} bpm', "") for x, v in reversed(have[-14:])]
    return (head + f'<div class="hero1">{last["avg"]:.0f}<small>bpm average</small></div>'
            f'<div class="delta">{_day(last_d)} · range {last["min"]}–{last["max"]}</div>'
            + chips("/heart", d) + f'<div class="card">{chart}<div class="legend"><span><i style="background:var(--c-heart)"></i>Daily average, shaded to minimum and maximum</span></div></div>'
            + stats(st) + _notes(notes) + f'<div class="card"><h2>Days</h2>{_rows(rows)}</div>')


def bp_page(store: Store, csrf: str, d: int) -> str:
    raw = store.bp_readings(d)
    dates, _ = store.window(d)
    head = _head("", "bp", "Blood pressure")
    if not raw:
        return head + '<h1>Blood pressure</h1>' + chips("/bp", d) + _empty("No readings in this period.", ADD)
    rs = bp_sessions(raw)
    last = rs[-1]
    xs = _xs([r["time"] for r in rs], dates)
    titles = [f'{r["time"]} {r["systolic"]}/{r["diastolic"]}' + (f' (average of {len(r["readings"])})' if len(r["readings"]) > 1 else "")
              for r in rs]
    chart = charts.line([("Top (systolic)", "--c-heart", [float(r["systolic"]) for r in rs]),
                         ("Bottom (diastolic)", "--c-weight", [float(r["diastolic"]) for r in rs])],
                        titles, xs=xs, thresholds=[(HOME_BP_HIGH[0], "135"), (HOME_BP_HIGH[1], "85")],
                        axis=_axis_ticks(dates, d), fmt=lambda v: f"{v:.0f}", pad=8, direct_labels=False,
                        summary=f"Blood pressure over {d} days, one point per sitting. Dashed lines mark 135 and 85, the home reading threshold.")
    st = bp_stats(rs)
    skip_first = st["average_excluding_first_day"] is not None
    avg = st["average_excluding_first_day"] or st["average_all"]
    above = sum(1 for r in rs if r["systolic"] >= HOME_BP_HIGH[0] or r["diastolic"] >= HOME_BP_HIGH[1])
    notes: list[tuple[str, str]] = []
    if last["systolic"] >= BP_URGENT[0] or last["diastolic"] >= BP_URGENT[1]:
        notes.append(("bad", "Your last reading was very high. Contact your GP or NHS 111 today. With chest pain, breathlessness, "
                      "confusion or a severe headache, call 999."))
    elif last["systolic"] >= HOME_BP_HIGH[0] or last["diastolic"] >= HOME_BP_HIGH[1]:
        notes.append(("warn", bp_over_text(last) + " Your GP looks at the pattern over several days, so keep logging morning and evening."))
    notes.append(("", f'{above} of {len(rs)} sittings were at or above {HOME_BP_HIGH[0]}/{HOME_BP_HIGH[1]}.'
                  + (" Two readings a minute apart count as one sitting." if len(rs) < len(raw) else "")))
    if skip_first:
        notes.append(("", "The average leaves out your first day of readings, as GPs do for home monitoring."))
    mor, eve = st["morning_before_noon"], st["evening_noon_onwards"]
    cells = [("Average" + (" (from day 2)" if skip_first else ""), f'{avg["systolic"]:.0f}/{avg["diastolic"]:.0f}' if avg else "–"),
             ("Mornings", f'{mor["systolic"]:.0f}/{mor["diastolic"]:.0f}' if mor else "–"),
             ("Evenings", f'{eve["systolic"]:.0f}/{eve["diastolic"]:.0f}' if eve else "–")]
    rows = []
    for r in reversed(rs[-30:]):
        n = len(r["readings"])
        if n == 1:
            one = r["readings"][0]
            rows.append((_when(store, r["time"]), (f'pulse {one["pulse"]}' if one["pulse"] else "") + (f' · {one["note"]}' if one["note"] else ""),
                         f'{r["systolic"]}/{r["diastolic"]}', _delete(csrf, "blood_pressure", one["id"]) if one["source"] == "manual" else ""))
            continue
        inner = "".join(
            f'<li><div class="l"><b>{x["systolic"]}/{x["diastolic"]}</b><span>{esc(x["time"][11:16])}</span></div>'
            f'<div>{_delete(csrf, "blood_pressure", x["id"]) if x["source"] == "manual" else ""}</div></li>' for x in r["readings"])
        rows.append((_when(store, r["time"]), f'{n} readings, average' + (f' · pulse {r["pulse"]}' if r["pulse"] else ""),
                     f'{r["systolic"]}/{r["diastolic"]}',
                     f'<details class="more"><summary role="button">Show readings</summary><ul class="rows">{inner}</ul></details>'))
    return (head + f'<div class="hero1">{last["systolic"]}/{last["diastolic"]}<small>mmHg</small></div>'
            f'<div class="delta">{esc(_when(store, last["time"]))}'
            f'{" · average of " + str(len(last["readings"])) + " readings" if len(last["readings"]) > 1 else ""}'
            f'{" · pulse " + str(last["pulse"]) if last["pulse"] else ""}</div>'
            + chips("/bp", d) + f'<div class="card">{chart}{legend([("--c-heart", "Top number (systolic)"), ("--c-weight", "Bottom number (diastolic)")])}</div>' + stats(cells) + _notes(notes)
            + '<div class="card"><h2>Week summary for your GP</h2><p class="m" style="margin:0 0 10px">Averages, morning and evening '
              f'splits and a printable record.</p><a class="cta" href="/gp">Open GP report</a></div>'
            + f'<div class="card"><h2>Sittings</h2>{_rows(rows)}</div>'
            + f'<p style="margin:0 0 14px"><a class="cta" href="/log">Add a reading</a></p>')


def _mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def weight_page(store: Store, csrf: str, d: int) -> str:
    wide = store.weight_readings(d + 14)
    dates, _ = store.window(d)
    start = dates[0].isoformat()
    rs = [r for r in wide if r["time"][:10] >= start]
    head = _head("", "weight", "Weight")
    if not rs:
        return head + '<h1>Weight</h1>' + chips("/weight", d) + _empty("No weight readings in this period.", ADD)
    last = rs[-1]
    ng = nutrition_goals(store)
    goal = ng["goal_weight"]

    def day_ord(t: str) -> int:
        return date.fromisoformat(t[:10]).toordinal()

    def avg7(i: int) -> float:
        """Average of every weigh-in in the 7 days up to and including reading i."""
        o = day_ord(wide[i]["time"])
        return _mean([x["kg"] for x in wide if o - 6 <= day_ord(x["time"]) <= o and x["time"] <= wide[i]["time"]]) or wide[i]["kg"]
    off = len(wide) - len(rs)
    trend = [avg7(off + i) for i in range(len(rs))]
    kgs = [r["kg"] for r in rs]
    today_o = store.local(store.now()).date().toordinal()
    week = [x["kg"] for x in wide if today_o - 6 <= day_ord(x["time"]) <= today_o]
    prev = [x["kg"] for x in wide if today_o - 13 <= day_ord(x["time"]) <= today_o - 7]
    cur_avg = _mean(week)
    prev_avg = _mean(prev) if len(prev) >= 3 else None
    chart = charts.line([("Weigh-ins", "--c-weight", kgs), ("7-day average", "--accent", trend)],
                        [f'{r["time"]} {r["kg"]:.1f} kg' for r in rs],
                        xs=_xs([r["time"] for r in rs], dates), axis=_axis_ticks(dates, d),
                        thresholds=[(goal, "Goal")] if goal and min(kgs) - 2.5 <= goal <= max(kgs) + 2.5 else (),
                        fmt=lambda v: f"{v:.1f}", direct_labels=False, pad=0.5,
                        summary=f"Weight over {d} days with its 7-day average" + (f" and your goal of {goal:g} kilograms" if goal else ""))
    notes: list[tuple[str, str]] = []
    if len(week) < 5:
        notes.append(("warn" if len(week) < 3 else "", f'{len(week)} weigh-in{"s" if len(week) != 1 else ""} in the last 7 days. '
                      'Five or more, at the same time each morning, makes the average steady enough to trust.'))
    if goal and cur_avg:
        togo = cur_avg - goal
        if togo > 0.05:
            notes.append(("", f'{togo:.1f} kg above your {goal:g} kg goal. At about 0.5 kg a week that is roughly {max(round(togo / 0.5), 1)} weeks.'))
        else:
            notes.append(("", f'Your 7-day average is at or below your {goal:g} kg goal.'))
    elif not goal:
        notes.append(("", "Set a goal weight in Settings to see how far you have to go."))
    notes.append(("", "Daily weight moves by a kilo or more with water and salt. Read the 7-day average, not single days."))
    first_wk = [r["kg"] for r in rs if day_ord(r["time"]) < day_ord(rs[0]["time"]) + 7]
    last_wk = [r["kg"] for r in rs if day_ord(r["time"]) > day_ord(rs[-1]["time"]) - 7]
    change = (_mean(last_wk) - _mean(first_wk)) if d >= 14 and len(first_wk) >= 2 and len(last_wk) >= 2 else None
    rows = [(_when(store, r["time"]),
             ("Morning weigh-in" if int(r["time"][11:13]) < 10 else "Later in the day")
             + (" · from your scale or watch" if r["source"] != "manual" else " · logged by you"),
             f'{r["kg"]:.1f} kg', _delete(csrf, "weight", r["id"]) if r["source"] == "manual" else "")
            for r in reversed(rs[-30:])]
    if cur_avg and len(week) >= 3:
        delta = f'7-day average {cur_avg:.1f} kg' + (f' · {signed(cur_avg - prev_avg, " kg")} on the week before' if prev_avg else "")
    else:
        delta = f'{esc(_when(store, last["time"]))} · average needs 3 or more weigh-ins in a week'
    if goal and cur_avg:
        third = ("To goal", f'{max(cur_avg - goal, 0):.1f} kg')
    else:
        third = ("Lowest", f"{min(kgs):.1f} kg")
    goal_card = ('<div class="card"><h2>Goal weight</h2><form method="post" action="/settings/goal-weight">'
                 f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
                 '<label for="gw">Your target in kg. Change it any time; leave empty to remove it.</label>'
                 '<input id="gw" name="goal_weight" type="number" inputmode="decimal" min="40" max="200" step="0.5" '
                 f'value="{f"{goal:g}" if goal else ""}" placeholder="e.g. 70">'
                 '<button class="go">Save goal weight</button></form></div>')
    return (head + f'<div class="hero1">{last["kg"]:.1f}<small>kg</small></div>'
            f'<div class="delta">{delta}</div>'
            + chips("/weight", d)
            + f'<div class="card">{chart}{legend([("--c-weight", "Weigh-ins"), ("--accent", "7-day average")])}</div>'
            + stats([("7-day average", f"{cur_avg:.1f} kg" if cur_avg else "–"),
                     ("Change" + (" in period" if change is not None else ""), signed(change, " kg") if change is not None else "–"),
                     third])
            + _notes(notes)
            + goal_card
            + f'<div class="card"><h2>Weigh-ins</h2>{_rows(rows)}</div>'
            + '<p style="margin:0 0 14px"><a class="cta" href="/log">Add your weight</a></p>')


def food_page(store: Store, csrf: str, d: int) -> str:
    es = store.food_entries(d)
    dates, _ = store.window(d)
    today = dates[-1]
    head = _head("", "food", "Food")
    fasting = store.fasting_days(d)
    if not es and not fasting:
        return head + '<h1>Food</h1>' + chips("/food", d) + _empty("Nothing logged in this period.", ADD)
    by = {x: 0 for x in dates}
    prot: dict[date, float] = {}
    salt: dict[date, float] = {}
    for e in es:
        by[e["_day"]] = by.get(e["_day"], 0) + e["kcal"]
        prot[e["_day"]] = prot.get(e["_day"], 0) + (e["protein_g"] or 0)
        salt[e["_day"]] = salt.get(e["_day"], 0) + (e["salt_g"] or 0)
    logged = [x for x in dates if by[x] and x != today] or [x for x in dates if by[x]]
    labs, every = _axis(dates, d)
    ng = nutrition_goals(store)
    kg = ng["kcal_goal"]
    chart = charts.bars([float(by[x]) if by[x] else None for x in dates], labs, "--c-food", unit=" kcal",
                        goal=kg, label_every=every, faded=[x in fasting for x in dates],
                        summary=f"Calories logged per day over {d} days" + (f". Dashed line is your goal of {kg:,.0f} kcal." if kg else ""))
    avg = _avg([by[x] for x in logged]) or 0
    today_k = by.get(today, 0)
    meters = []
    if today in fasting:
        meters.append(f'<p class="m" style="margin:0">Fasting day. {today_k:,.0f} kcal logged so far.</p>')
    else:
        if kg:
            meters.append(_meter("Calories", today_k, kg, "kcal", tint="--c-food"))
        if ng["protein_goal"]:
            meters.append(_meter("Protein", prot.get(today, 0), ng["protein_goal"], "g", tint="--c-steps"))
        if ng["salt_limit"]:
            meters.append(_meter("Salt", salt.get(today, 0), ng["salt_limit"], "g", tint="--c-weight", limit=True, digits=1))
    today_card = (f'<div class="card"><h2>Today</h2>{"".join(meters)}</div>' if meters else
                  '<div class="card"><h2>Today</h2><p class="m" style="margin:0">Set calorie, protein and salt goals in '
                  '<a href="/settings">Settings</a> to see how much is left today.</p></div>')
    notes = []
    if fasting:
        notes.append(("", f'{len(fasting)} fasting day{"s" if len(fasting) != 1 else ""} marked in this period (lighter bars).'))
    saltavg = _avg([salt[x] for x in logged if x in salt])
    if saltavg:
        notes.append(("", f'Salt averages about {saltavg:.1f} g on days you logged. The usual adult guideline is under 6 g.'))
    notes.append(("", "Days with nothing logged are left out of the averages."))
    # entries grouped by day, each with the meal it belongs to
    recent_days = sorted({e["_day"] for e in es}, reverse=True)[:7]
    groups = []
    for day in recent_days:
        items = [e for e in es if e["_day"] == day]
        title = "Today" if day == today else "Yesterday" if day == today - timedelta(days=1) else _day(day)
        rows = [(e["name"], f'{_meal(int(e["time"][11:13]))} · {e["time"][11:16]}'
                 + (f' · {e["protein_g"]:.0f} g protein' if e["protein_g"] else ""), f'{e["kcal"]} kcal',
                 _delete(csrf, "nutrition", e["id"]) if e["source"] == "manual" else "") for e in items]
        groups.append(f'<div class="dayh"><span>{esc(title)}</span><span>{sum(e["kcal"] for e in items):,} kcal</span></div>{_rows(rows)}')
    if today in fasting:
        delta = "Fasting day"
    elif kg:
        delta = f'Goal {kg:,.0f} kcal'
    else:
        delta = "Calories logged today"
    return (head + f'<div class="hero1">{today_k:,}<small>kcal today</small></div>'
            f'<div class="delta">{delta}</div>' + today_card
            + chips("/food", d) + f'<div class="card">{chart}</div>'
            + stats([("Avg per logged day", f"{avg:,.0f}"), ("Days logged", f"{sum(1 for x in dates if by[x])}/{d}"),
                     ("Entries", str(len(es)))])
            + _notes(notes) + f'<div class="card"><h2>Meals</h2>{"".join(groups) if groups else "<p class=m>No entries.</p>"}</div>'
            + '<p style="margin:0 0 14px"><a class="cta" href="/log">Log a meal</a></p>')


def activity_page(store: Store, d: int) -> str:
    ws = store.workouts(d)
    dates, _ = store.window(d)
    head = _head("", "work", "Workouts")
    if not ws:
        return head + '<h1>Workouts</h1>' + chips("/activity", d) + _empty("No workouts in this period.")
    per = {x: 0 for x in dates}
    for w in ws:
        k = date.fromisoformat(w["date"])
        if k in per:
            per[k] += w["minutes"]
    labs, every = _axis(dates, d)
    chart = charts.bars([float(per[x]) if per[x] else None for x in dates], labs, "--c-work", unit=" min",
                        label_every=every, faded=[False] * len(dates), summary=f"Workout minutes per day over {d} days")
    total = sum(w["minutes"] for w in ws)
    def _dist(w: dict[str, Any]) -> float:
        return 0.0 if any(k in w["type"].lower() for k in ("strength", "weight", "gym")) else (w.get("distance_km") or 0.0)
    km = sum(_dist(w) for w in ws)
    active_days = sum(1 for v in per.values() if v)
    rows = []
    for w in reversed(ws[-20:]):
        bits = [f'{w["minutes"]} min']
        if _dist(w) >= 0.05:
            bits.append(f'{_dist(w):.1f} km')
        if w.get("avg_hr"):
            bits.append(f'avg {w["avg_hr"]} bpm')
        kc = w.get("kcal_above_resting_estimate") or w.get("active_kcal_estimate")
        rows.append((w["type"].capitalize(), f'{_day(date.fromisoformat(w["date"]))} {w["start"]}', " · ".join(bits[:1]),
                     f'<div class="m" style="text-align:right">{esc(" · ".join(bits[1:] + ([f"~{kc} kcal"] if kc else [])))}</div>'))
    return (head + f'<div class="hero1">{len(ws)}<small>workouts</small></div>'
            f'<div class="delta">{hm(total * 60)} in the last {d} days</div>'
            + chips("/activity", d) + f'<div class="card">{chart}</div>'
            + stats([("Active days", f"{active_days}/{d}"), ("Total time", hm_short(total * 60)),
                     ("Distance", f"{km:.1f} km" if km >= 0.05 else "Longest " + hm_short(max(w["minutes"] for w in ws) * 60))])
            + _notes([("", f'About {total / max(d / 7, 1):.0f} minutes of workouts a week. The UK guideline for adults is 150 minutes of moderate activity a week.')])
            + f'<div class="card"><h2>Sessions</h2>{_rows(rows)}</div>')


def swap_page(store: Store, csrf: str, entry: dict[str, Any], options: list[dict[str, Any]], q: str) -> str:
    cur = entry.get("per_serving") or {}
    name = entry.get("recipe_name") or entry.get("recipe") or "this meal"
    eid = esc(entry["id"])
    rows = []
    for o in options:
        n = o["per_serving"]
        sub = f'{n["kcal"]:,.0f} kcal' + (f' · {n["protein_g"]:.0f} g protein' if n.get("protein_g") else "")
        if cur.get("kcal"):
            sub += f' · {signed(n["kcal"] - cur["kcal"], " kcal", 0)} vs planned'
        rows.append(f'<li><div class="l"><b>{esc(o["name"])}</b><span>{esc(sub)}</span></div>'
                    f'<form method="post" action="/meals/swap" style="margin:0"><input type="hidden" name="csrf" value="{esc(csrf)}">'
                    f'<input type="hidden" name="entry_id" value="{eid}"><input type="hidden" name="slug" value="{esc(o["slug"])}">'
                    f'<button class="mini" aria-label="Use {esc(o["name"])}">Use this</button></form></li>')
    body = (f'<ul class="rows">{"".join(rows)}</ul>' if rows else '<p class="m" style="margin:0">No recipes with calories match.</p>')
    return ('<div class="dhead">' + icon("food") + '<span>Swap meal</span></div>'
            f'<h1>{esc(name)}</h1><p class="lede">'
            + (f'Planned: {cur["kcal"]:,.0f} kcal. ' if cur.get("kcal") else "")
            + 'Closest in calories first.</p>'
            '<form method="get" action="/meals/swap" class="card" style="padding:12px">'
            f'<input type="hidden" name="entry_id" value="{eid}"><label for="q">Search recipes</label>'
            f'<input id="q" name="q" value="{esc(q)}" placeholder="e.g. chicken"><button class="go">Search</button></form>'
            f'<div class="card">{body}</div><p style="margin:0 0 14px"><a class="cta" href="/">Back to Today</a></p>')
