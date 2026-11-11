"""Today screen and detail screens. Each detail screen follows one pattern: a hero number, a range
selector, one chart, three stats, a few plain-language notes and the underlying entries.

Notes are rules over the person's own numbers: data, not diagnosis.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Sequence

from . import charts
from .pages import BP_URGENT, HOME_BP_HIGH, goals, notes_for, nutrition_goals
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
    today = store.local(store.now()).date()
    d = date.fromisoformat(t[:10])
    if d == today:
        return f"Today {t[11:16]}"
    if d == today - timedelta(days=1):
        return f"Yesterday {t[11:16]}"
    return f"{_day(d)} {t[11:16]}"


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
    return (f'<form method="post" action="/log/delete" style="margin:0"><input type="hidden" name="csrf" value="{esc(csrf)}">'
            f'<input type="hidden" name="kind" value="{esc(kind)}"><input type="hidden" name="id" value="{rid}">'
            '<button class="link" aria-label="Delete this entry">Delete</button></form>')


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


def today_page(store: Store, name: str) -> str:
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
    sleep_ring = charts.ring(night["asleep_seconds"] / goal_s if night else 0, "--c-sleep",
                             big=hm_short(night["asleep_seconds"]) if night else "–", small=f'of {g["sleep_goal"]:g} h',
                             summary=f"Sleep last night: {hm(night['asleep_seconds']) if night else 'no data'}")
    hero = ('<div class="card hero">'
            f'<a href="/steps">{steps_ring}<div class="cap">Steps</div><div class="sub">today</div></a>'
            f'<a href="/sleep">{sleep_ring}<div class="cap">Sleep</div><div class="sub">last night</div></a></div>')

    notes = "".join(f'<div class="note {lvl}">{esc(t)}</div>' for lvl, t in notes_for(store))
    coach = (f'<div class="card coach"><div class="face" aria-hidden="true">C</div><div class="notes">'
             f'<h2>Coach notes</h2>{notes}</div></div>')

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

    bps = store.bp_readings(14)
    if bps:
        b = bps[-1]
        tiles.append(tile("/bp", "Blood pressure", "bp", "--c-heart", f'{b["systolic"]}/{b["diastolic"]}', "mmHg",
                          _when(store, b["time"]), charts.spark([float(r["systolic"]) for r in bps], "--c-heart")))
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
            f'{hero}{coach}<div class="sect"><h2>Your numbers</h2></div><div class="tiles">{"".join(tiles)}</div>{sync}'
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
    avg = _avg([v["asleep_seconds"] for _, v in have]) or 0
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
    best = max(have, key=lambda p: p[1]["asleep_seconds"])
    worst = min(have, key=lambda p: p[1]["asleep_seconds"])
    stage_txt = " · ".join(f'{k} {hm_short(v)}' for k, v in last["stages"].items() if k not in ("unknown",) and v)
    notes: list[tuple[str, str]] = []
    gap = avg - goal_s
    notes.append(("" if gap >= 0 else "warn",
                  f'Average {hm(avg)} a night, {hm(abs(gap))} {"above" if gap >= 0 else "below"} your {g["sleep_goal"]:g} hour goal.'))
    short = sum(1 for _, v in have if v["asleep_seconds"] < 6 * 3600)
    if short:
        notes.append(("warn", f'{short} of {len(have)} nights were under 6 hours.'))
    rows = [(_day(x), f'{v.get("bed", "")[11:16]} to {v.get("wake", "")[11:16]}'
             + (f' · {round(v["awake_seconds"] / 60)} min awake' if v["awake_seconds"] else ""), hm_short(v["asleep_seconds"]), "")
            for x, v in reversed(have[-14:])]
    return (head + f'<div class="hero1">{esc(hm_short(last["asleep_seconds"]))}</div>'
            f'<div class="delta">{"Last night" if last_d == store.local(store.now()).date() else _day(last_d)}'
            f'{" · " + esc(stage_txt) if stage_txt else ""}</div>'
            + chips("/sleep", d)
            + f'<div class="card">{chart}{legend([("--st-deep", "Deep"), ("--st-rem", "REM"), ("--st-light", "Light"), ("--st-awake", "Awake")])}</div>'
            + stats([("Average", hm_short(avg)), ("Longest", hm_short(best[1]["asleep_seconds"])),
                     ("Shortest", hm_short(worst[1]["asleep_seconds"]))])
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
    rs = store.bp_readings(d)
    dates, _ = store.window(d)
    head = _head("", "bp", "Blood pressure")
    if not rs:
        return head + '<h1>Blood pressure</h1>' + chips("/bp", d) + _empty("No readings in this period.", ADD)
    last = rs[-1]
    xs = _xs([r["time"] for r in rs], dates)
    titles = [f'{r["time"]} {r["systolic"]}/{r["diastolic"]}' for r in rs]
    chart = charts.line([("Top (systolic)", "--c-heart", [float(r["systolic"]) for r in rs]),
                         ("Bottom (diastolic)", "--c-weight", [float(r["diastolic"]) for r in rs])],
                        titles, xs=xs, thresholds=[(HOME_BP_HIGH[0], "135"), (HOME_BP_HIGH[1], "85")],
                        axis=_axis_ticks(dates, d), fmt=lambda v: f"{v:.0f}", pad=8, direct_labels=False,
                        summary=f"Blood pressure readings over {d} days. Dashed lines mark 135 and 85, the home reading threshold.")
    first_day = rs[0]["time"][:10]
    st = bp_stats(rs)
    avg = st["average_all"]
    above = sum(1 for r in rs if r["systolic"] >= HOME_BP_HIGH[0] or r["diastolic"] >= HOME_BP_HIGH[1])
    notes: list[tuple[str, str]] = []
    if last["systolic"] >= BP_URGENT[0] or last["diastolic"] >= BP_URGENT[1]:
        notes.append(("bad", "Your last reading was very high. Contact your GP or NHS 111 today. With chest pain, breathlessness, "
                      "confusion or a severe headache, call 999."))
    elif last["systolic"] >= HOME_BP_HIGH[0] or last["diastolic"] >= HOME_BP_HIGH[1]:
        notes.append(("warn", f'Your last reading is above the {HOME_BP_HIGH[0]}/{HOME_BP_HIGH[1]} home threshold. '
                      'Your GP looks at the pattern over several days, so keep logging morning and evening.'))
    notes.append(("", f'{above} of {len(rs)} readings were at or above {HOME_BP_HIGH[0]}/{HOME_BP_HIGH[1]}.'))
    mor, eve = st["morning_before_noon"], st["evening_noon_onwards"]
    cells = [("Average", f'{avg["systolic"]:.0f}/{avg["diastolic"]:.0f}' if avg else "–"),
             ("Mornings", f'{mor["systolic"]:.0f}/{mor["diastolic"]:.0f}' if mor else "–"),
             ("Evenings", f'{eve["systolic"]:.0f}/{eve["diastolic"]:.0f}' if eve else "–")]
    rows = [(_when(store, r["time"]),
             (f'pulse {r["pulse"]}' if r["pulse"] else "") + (f' · {r["note"]}' if r["note"] else ""),
             f'{r["systolic"]}/{r["diastolic"]}',
             _delete(csrf, "blood_pressure", r["id"]) if r["source"] == "manual" else "") for r in reversed(rs[-30:])]
    return (head + f'<div class="hero1">{last["systolic"]}/{last["diastolic"]}<small>mmHg</small></div>'
            f'<div class="delta">{esc(_when(store, last["time"]))}{" · pulse " + str(last["pulse"]) if last["pulse"] else ""}</div>'
            + chips("/bp", d) + f'<div class="card">{chart}{legend([("--c-heart", "Top number (systolic)"), ("--c-weight", "Bottom number (diastolic)")])}</div>' + stats(cells) + _notes(notes)
            + '<div class="card"><h2>Week summary for your GP</h2><p class="m" style="margin:0 0 10px">Averages, morning and evening '
              f'splits and a printable record.</p><a class="cta" href="/gp">Open GP report</a></div>'
            + f'<div class="card"><h2>Readings</h2>{_rows(rows)}</div>'
            + f'<p style="margin:0 0 14px"><a class="cta" href="/log">Add a reading</a></p>')


def weight_page(store: Store, csrf: str, d: int) -> str:
    rs = store.weight_readings(d)
    dates, _ = store.window(d)
    head = _head("", "weight", "Weight")
    if not rs:
        return head + '<h1>Weight</h1>' + chips("/weight", d) + _empty("No weight readings in this period.", ADD)
    last, first = rs[-1], rs[0]
    kgs = [r["kg"] for r in rs]
    chart = charts.line([("Weight", "--c-weight", kgs)], [f'{r["time"]} {r["kg"]:.1f} kg' for r in rs],
                        xs=_xs([r["time"] for r in rs], dates), axis=_axis_ticks(dates, d),
                        fmt=lambda v: f"{v:.1f}", direct_labels=False, pad=0.5,
                        summary=f"Weight over {d} days, from {first['kg']:.1f} to {last['kg']:.1f} kilograms")
    change = last["kg"] - first["kg"]
    notes = [("", f'{signed(change, " kg")} over this period ({first["kg"]:.1f} to {last["kg"]:.1f} kg).'
              if len(rs) > 1 else "Log your weight a few times a week to see a trend.")]
    rows = [(_when(store, r["time"]), "from your scale or watch" if r["source"] != "manual" else "logged by you",
             f'{r["kg"]:.1f} kg', _delete(csrf, "weight", r["id"]) if r["source"] == "manual" else "")
            for r in reversed(rs[-30:])]
    return (head + f'<div class="hero1">{last["kg"]:.1f}<small>kg</small></div>'
            f'<div class="delta">{esc(_when(store, last["time"]))} · {signed(change, " kg")} in this period</div>'
            + chips("/weight", d) + f'<div class="card">{chart}</div>'
            + stats([("Change", signed(change, " kg")), ("Lowest", f"{min(kgs):.1f} kg"), ("Highest", f"{max(kgs):.1f} kg")])
            + _notes(notes) + f'<div class="card"><h2>Weigh-ins</h2>{_rows(rows)}</div>'
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
    chart = charts.bars([float(by[x]) if by[x] else None for x in dates], labs, "--c-food", unit=" kcal",
                        label_every=every, faded=[x in fasting for x in dates],
                        summary=f"Calories logged per day over {d} days")
    avg = _avg([by[x] for x in logged]) or 0
    ng = nutrition_goals(store)
    notes = []
    today_k = by.get(today, 0)
    if ng["kcal_goal"] and today not in fasting:
        gap = ng["kcal_goal"] - today_k
        notes.append(("", f'Calories today: {today_k:,.0f} of {ng["kcal_goal"]:,.0f} kcal, '
                          f'{abs(gap):,.0f} {"left" if gap >= 0 else "over"}.'))
    if ng["protein_goal"]:
        notes.append(("", f'Protein today: {prot.get(today, 0):.0f} of {ng["protein_goal"]:.0f} g.'))
    if ng["salt_limit"]:
        notes.append(("", f'Salt today: {salt.get(today, 0):.1f} g (your limit is {ng["salt_limit"]:g} g).'))
    if fasting:
        notes.append(("", f'{len(fasting)} fasting day{"s" if len(fasting) != 1 else ""} marked in this period.'))
    saltavg = _avg([salt[x] for x in logged if x in salt])
    if saltavg:
        notes.append(("", f'Average salt about {saltavg:.1f} g a day in what you logged. The usual adult guideline is under 6 g.'))
    notes.append(("", "Days with nothing logged are left out of the averages."))
    rows = [(_when(store, e["time"]), e["name"], f'{e["kcal"]} kcal',
             _delete(csrf, "nutrition", e["id"]) if e["source"] == "manual" else "") for e in reversed(es[-20:])]
    if today in fasting:
        delta = "Fasting day"
    elif ng["kcal_goal"]:
        delta = f'Goal {ng["kcal_goal"]:,.0f} kcal · average {avg:,.0f}'
    else:
        delta = f"Daily average {avg:,.0f} kcal"
    return (head + f'<div class="hero1">{today_k:,}<small>kcal today</small></div>'
            f'<div class="delta">{delta}</div>'
            + chips("/food", d) + f'<div class="card">{chart}</div>'
            + stats([("Average", f"{avg:,.0f}"), ("Protein", f"{_avg([prot[x] for x in logged if x in prot]) or 0:.0f} g"),
                     ("Entries", str(len(es)))])
            + _notes(notes) + f'<div class="card"><h2>Recent entries</h2>{_rows(rows) if rows else "<p class=m>No entries.</p>"}</div>'
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
    km = sum(w["distance_km"] or 0 for w in ws)
    active_days = sum(1 for v in per.values() if v)
    rows = []
    for w in reversed(ws[-20:]):
        bits = [f'{w["minutes"]} min']
        if w.get("distance_km"):
            bits.append(f'{w["distance_km"]:.1f} km')
        if w.get("avg_hr"):
            bits.append(f'avg {w["avg_hr"]} bpm')
        kc = w.get("kcal_above_resting_estimate") or w.get("active_kcal_estimate")
        rows.append((w["type"].capitalize(), f'{_day(date.fromisoformat(w["date"]))} {w["start"]}', " · ".join(bits[:1]),
                     f'<div class="m" style="text-align:right">{esc(" · ".join(bits[1:] + ([f"~{kc} kcal"] if kc else [])))}</div>'))
    return (head + f'<div class="hero1">{len(ws)}<small>workouts</small></div>'
            f'<div class="delta">{hm(total * 60)} in the last {d} days</div>'
            + chips("/activity", d) + f'<div class="card">{chart}</div>'
            + stats([("Active days", f"{active_days}/{d}"), ("Total time", hm_short(total * 60)),
                     ("Distance", f"{km:.1f} km")])
            + _notes([("", f'About {total / max(d / 7, 1):.0f} minutes of workouts a week. The UK guideline for adults is 150 minutes of moderate activity a week.')])
            + f'<div class="card"><h2>Sessions</h2>{_rows(rows)}</div>')
