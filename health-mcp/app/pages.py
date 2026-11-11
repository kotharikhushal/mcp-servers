"""Server-rendered pages for the wellness web app. No JavaScript, so a strict CSP is possible.

Everything that comes from the database or the user goes through esc(). The coaching notes
are plain rules over the person's own numbers: data, not diagnosis.
"""
from __future__ import annotations

from datetime import timedelta
from html import escape as _esc
from typing import Any

from .store import Store
from .tools import bp_stats

HOME_BP_HIGH = (135, 85)       # NHS home-reading threshold
BP_URGENT = (180, 120)


def esc(v: Any) -> str:
    return _esc("" if v is None else str(v), quote=True)


CSS = """
:root{--bg:#f6f5f1;--panel:#fff;--ink:#1d2a2a;--muted:#667;--line:#e3e1d8;--accent:#1f7a6d;--warn:#b4531a;--bad:#a82020;--soft:#e8f3f0}
@media (prefers-color-scheme:dark){:root{--bg:#121816;--panel:#1b2421;--ink:#e8efec;--muted:#9fb0aa;--line:#2c3935;--accent:#4cc2af;--warn:#e69a5a;--bad:#ff7b72;--soft:#21332e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:16px}
nav{display:flex;gap:4px;align-items:center;flex-wrap:wrap;padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--line)}
nav a,nav button{color:var(--ink);text-decoration:none;padding:6px 12px;border-radius:999px;border:0;background:none;font:inherit;cursor:pointer}
nav a.on{background:var(--accent);color:#fff}nav .sp{flex:1}nav span{color:var(--muted);font-size:14px}
h1{font-size:22px;margin:8px 0 12px}h2{font-size:17px;margin:0 0 8px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin:0 0 12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.k{font-size:26px;font-weight:650}.u{font-size:13px;color:var(--muted)}.m{color:var(--muted);font-size:14px}
.note{border-left:4px solid var(--accent);background:var(--soft);padding:8px 12px;border-radius:8px;margin:6px 0}
.note.warn{border-color:var(--warn)}.note.bad{border-color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:15px}td,th{text-align:left;padding:6px 4px;border-bottom:1px solid var(--line)}
label{display:block;font-size:14px;color:var(--muted);margin:8px 0 2px}
input{width:100%;padding:9px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font:inherit}
.row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
button.go{margin-top:12px;padding:10px 18px;border:0;border-radius:10px;background:var(--accent);color:#fff;font:inherit;cursor:pointer}
button.link{border:0;background:none;color:var(--bad);cursor:pointer;font:inherit;padding:0}
.flash{padding:8px 12px;border-radius:8px;background:var(--soft);margin-bottom:12px}.flash.err{background:#fbe3e0;color:#7a1d16}
svg{width:100%;height:auto}
@media print{nav,.noprint{display:none}body{background:#fff;color:#000}.card{border:1px solid #999}}
"""

NAV = (("/", "Today"), ("/bp", "Blood pressure"), ("/log", "Quick log"))


def layout(title: str, body: str, *, name: str, active: str, csrf: str, flash: str = "",
           flash_err: bool = False) -> str:
    links = "".join(f'<a href="{h}"{" class=on" if h == active else ""}>{esc(t)}</a>' for h, t in NAV)
    fl = f'<div class="flash{" err" if flash_err else ""}">{esc(flash)}</div>' if flash else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)} · Wellness</title><style>{CSS}</style></head><body>'
            f'<nav>{links}<span class="sp"></span><span>{esc(name)}</span>'
            f'<form method="post" action="/logout" style="margin:0">'
            f'<input type="hidden" name="csrf" value="{esc(csrf)}"><button>Sign out</button></form></nav>'
            f'<main>{fl}{body}</main></body></html>')


def _h(seconds: float) -> str:
    m = round(seconds / 60)
    return f"{m // 60} h {m % 60:02d} m"


def _spark(series: list[tuple[str, list[float]]], width: int = 640, height: int = 140) -> str:
    """Tiny inline SVG line chart. series: (css colour var, values)."""
    vals = [v for _, s in series for v in s]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals) - 5, max(vals) + 5
    n = max(len(s) for _, s in series)
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="trend">']
    for colour, s in series:
        if len(s) < 2:
            continue
        pts = " ".join(f"{10 + i * (width - 20) / max(n - 1, 1):.1f},"
                       f"{height - 10 - (v - lo) / (hi - lo) * (height - 20):.1f}" for i, v in enumerate(s))
        out.append(f'<polyline fill="none" stroke="var({colour})" stroke-width="3" points="{pts}"/>')
    out.append("</svg>")
    return "".join(out)


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


def today_page(store: Store) -> str:
    now = store.local(store.now())
    today = now.date()
    sleep_by = store.sleep_by_day(2)
    sleep = sleep_by.get(today)
    steps = store.steps_by_day(8)
    week = [v["steps"] for d, v in steps.items() if d != today and v["records"]]
    weights = store.weight_readings(60)
    food = [e for e in store.food_entries(1) if e["_day"] == today]
    bp = store.bp_readings(2)
    fasting = today in store.fasting_days(1)

    cards = []
    if sleep:
        stages = " · ".join(f'{k} {_h(v)}' for k, v in sleep["stages"].items() if k not in ("awake", "unknown"))
        cards.append(f'<div class="card"><div class="u">Last night</div><div class="k">{_h(sleep["asleep_seconds"])}</div>'
                     f'<div class="m">{round(sleep["awake_seconds"] / 60)} min awake'
                     f'{" · " + esc(stages) if stages else ""}</div></div>')
    else:
        cards.append('<div class="card"><div class="u">Last night</div><div class="k">–</div>'
                     '<div class="m">No sleep recorded yet</div></div>')
    s_today = steps[today]["steps"] if steps[today]["records"] else None
    avg = round(sum(week) / len(week)) if week else None
    cards.append(f'<div class="card"><div class="u">Steps today</div><div class="k">{esc(s_today if s_today is not None else "–")}</div>'
                 f'<div class="m">7-day average {esc(avg if avg is not None else "–")}</div></div>')
    if weights:
        w = weights[-1]
        cards.append(f'<div class="card"><div class="u">Weight</div><div class="k">{w["kg"]:.1f} kg</div>'
                     f'<div class="m">{esc(w["time"][:10])}</div></div>')
    if bp:
        b = bp[-1]
        cards.append(f'<div class="card"><div class="u">Latest blood pressure</div>'
                     f'<div class="k">{b["systolic"]}/{b["diastolic"]}</div>'
                     f'<div class="m">{esc(b["time"])}{" · pulse " + str(b["pulse"]) if b["pulse"] else ""}</div></div>')
    kcal = sum(e["kcal"] for e in food)
    cards.append(f'<div class="card"><div class="u">Food logged today</div><div class="k">{kcal} kcal</div>'
                 f'<div class="m">{"Fasting day" if fasting else str(len(food)) + " entries"}</div></div>')

    notes = "".join(f'<div class="note {lvl}">{esc(t)}</div>' for lvl, t in notes_for(store))
    return (f'<h1>{esc(now.strftime("%A %-d %B"))}</h1><div class="card"><h2>Coach notes</h2>{notes}</div>'
            f'<div class="grid">{"".join(cards)}</div>'
            '<p class="m">These notes are based on your own numbers. They are not a diagnosis.</p>')


def bp_page(store: Store, csrf: str) -> str:
    rs = store.bp_readings(14)
    last7 = [r for r in rs if r["time"][:10] >= (store.local(store.now()).date() - timedelta(days=6)).isoformat()]
    stats = bp_stats(last7) if last7 else None

    def blk(b: dict[str, Any] | None) -> str:
        return f'{b["systolic"]:.0f}/{b["diastolic"]:.0f} ({b["readings"]})' if b else "–"

    summary = ""
    if stats:
        summary = (f'<div class="card"><h2>Week summary for your GP</h2>'
                   f'<table><tr><td>All readings, last 7 days</td><td>{blk(stats["average_all"])}</td></tr>'
                   f'<tr><td>Excluding the first day</td><td>{blk(stats["average_excluding_first_day"])}</td></tr>'
                   f'<tr><td>Mornings</td><td>{blk(stats["morning_before_noon"])}</td></tr>'
                   f'<tr><td>Evenings</td><td>{blk(stats["evening_noon_onwards"])}</td></tr>'
                   f'<tr><td>Days with readings</td><td>{stats["days_with_readings"]}</td></tr></table>'
                   '<p class="m">Home readings recorded by the patient. Averages in brackets show the number '
                   'of readings. To keep a copy, use your browser’s Print or Save as PDF.</p></div>')
    chart = _spark([("--bad", [float(r["systolic"]) for r in rs]), ("--accent", [float(r["diastolic"]) for r in rs])])
    rows = "".join(
        f'<tr><td>{esc(r["time"])}</td><td><b>{r["systolic"]}/{r["diastolic"]}</b></td>'
        f'<td>{esc(r["pulse"] or "")}</td><td class="m">{esc(r["note"] or "")}</td>'
        + (f'<td class="noprint"><form method="post" action="/log/delete" style="margin:0">'
           f'<input type="hidden" name="csrf" value="{esc(csrf)}"><input type="hidden" name="kind" value="blood_pressure">'
           f'<input type="hidden" name="id" value="{r["id"]}"><button class="link">Delete</button></form></td>'
           if r["source"] == "manual" else '<td class="noprint"></td>') + '</tr>'
        for r in reversed(rs))
    table = (f'<div class="card"><h2>Readings, last 14 days</h2>{chart}'
             f'<table><tr><th>Time</th><th>BP</th><th>Pulse</th><th>Note</th><th class="noprint"></th></tr>{rows}</table></div>'
             if rs else '<div class="card">No readings yet. Add one under Quick log.</div>')
    return f'<h1>Blood pressure</h1>{summary}{table}'


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
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>Wellness</title><style>{CSS}</style></head>'
            f'<body><main><div class="card"><h1>Wellness</h1>{"<p>" + esc(message) + "</p>" if message else ""}'
            '<p><a href="/login"><button class="go">Sign in</button></a></p></div></main></body></html>')
