"""Page chrome and shared components for the wellness app: styles, header, bottom tab bar, tiles.

Design notes. Phone first (safe-area aware, one thumb-reachable tab bar, 44px touch targets). One colour
per kind of measurement, chosen from a palette checked for colour-blind separation and contrast in both
light and dark mode. Big rounded numerals carry the page; charts are quiet.
"""
from __future__ import annotations

from html import escape as _esc
from typing import Any


def esc(v: Any) -> str:
    return _esc("" if v is None else str(v), quote=True)


CSS = """
:root{color-scheme:light dark;
--bg:#F1F5F8;--surface:#FFFFFF;--surface2:#E9EFF4;--ink:#10202B;--ink2:#4B5C69;--muted:#66777F;--line:#DFE7ED;--track:#E3EAF0;
--accent:#0B7A75;--on-accent:#FFFFFF;--warn:#B4530A;--bad:#B42318;--soft:#E3F3F1;
--c-sleep:#6E56CF;--c-steps:#0E8F80;--c-heart:#E5484D;--c-weight:#3E63DD;--c-food:#C77700;--c-work:#D6409F;
--st-deep:#3A2E94;--st-rem:#7E63E8;--st-light:#CFC5FA;--st-awake:#C8D0D7;
--shadow:0 1px 2px rgba(16,32,43,.06),0 6px 20px rgba(16,32,43,.05)}
@media (prefers-color-scheme:dark){:root{
--bg:#0C1318;--surface:#151F26;--surface2:#1D2A33;--ink:#EAF1F5;--ink2:#B4C2CC;--muted:#8DA0AB;--line:#25333D;--track:#243540;
--accent:#3CC4B7;--on-accent:#06201E;--warn:#F0A04B;--bad:#FF8A80;--soft:#14302D;
--c-sleep:#8A78E0;--c-steps:#17A594;--c-heart:#F0545A;--c-weight:#5B7EEB;--c-food:#C97B0A;--c-work:#E04BA5;
--st-deep:#4D3FB0;--st-rem:#9B88F2;--st-light:#D0C7FA;--st-awake:#4A5A66;--shadow:none}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 ui-rounded,"SF Pro Rounded","Google Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
font-variant-numeric:tabular-nums;padding-bottom:calc(84px + env(safe-area-inset-bottom))}
a{color:var(--accent);text-decoration:none}
.top{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:10px;min-height:52px;
padding:env(safe-area-inset-top) 16px 0;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
.top .brand{font-weight:700;font-size:17px;letter-spacing:-.01em}
.top .back{display:inline-flex;align-items:center;gap:2px;min-height:44px;font-weight:600;color:var(--ink)}
.top .back svg{width:22px;height:22px}
.top .sp{flex:1}
.who{display:inline-flex;align-items:center;gap:8px;font-size:14px;color:var(--ink2)}
.avatar{width:30px;height:30px;border-radius:50%;display:grid;place-items:center;background:var(--accent);color:var(--on-accent);font-weight:700;font-size:14px}
main.page{max-width:560px;margin:0 auto;padding:6px 16px 8px}
h1{font-size:28px;line-height:1.15;letter-spacing:-.02em;margin:10px 0 4px}
h2{font-size:18px;letter-spacing:-.01em;margin:0 0 8px}
.lede{color:var(--ink2);margin:0 0 14px}
.card{background:var(--surface);border-radius:20px;padding:16px;margin:0 0 14px;box-shadow:var(--shadow)}
.card.flat{box-shadow:none;border:1px solid var(--line)}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.k{font-size:30px;font-weight:700;letter-spacing:-.02em}.u{font-size:13px;color:var(--muted)}.m{color:var(--muted);font-size:14px}
.note{border-left:4px solid var(--accent);background:var(--soft);padding:10px 12px;border-radius:10px;margin:8px 0;color:var(--ink)}
.note.warn{border-color:var(--warn)}.note.bad{border-color:var(--bad)}
.flash{padding:12px 14px;border-radius:14px;background:var(--soft);border:2px solid var(--accent);font-weight:600;margin:6px 0 14px}
.flash.err{background:#FBE3E0;color:#7A1D16;border-color:#B42318}
/* home */
.greet{margin:8px 0 14px}.greet h1{margin:0}.greet p{margin:2px 0 0;color:var(--ink2)}
.hero{display:flex;justify-content:space-around;align-items:flex-start;gap:8px;padding:18px 10px 14px;
background:linear-gradient(165deg,color-mix(in srgb,var(--c-steps) 13%,var(--surface)),var(--surface) 62%)}
.hero a{color:inherit;display:block;text-align:center;flex:1;min-width:0}
.ring{width:min(36vw,150px);height:auto;display:block;margin:0 auto}
.ring-big{font-size:23px;font-weight:700;fill:var(--ink)}.ring-small{font-size:11.5px;fill:var(--muted)}
.hero .cap{font-weight:650;margin-top:6px}.hero .sub{font-size:13px;color:var(--muted)}
.coach{display:flex;gap:12px;align-items:flex-start}
.coach .face{flex:none;width:38px;height:38px;border-radius:12px;background:var(--accent);color:var(--on-accent);display:grid;place-items:center;font-weight:800}
.coach .notes{flex:1;min-width:0}.coach .notes .note:first-child{margin-top:0}.coach h2{margin-bottom:2px}
.sect{display:flex;justify-content:space-between;align-items:baseline;margin:18px 2px 8px}.sect h2{margin:0}
.tiles{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px}
a.tile{display:flex;flex-direction:column;gap:2px;min-height:136px;padding:14px;border-radius:20px;background:var(--surface);color:var(--ink);box-shadow:var(--shadow);position:relative;overflow:hidden}
a.tile:active{transform:scale(.985)}
.tile .lab{display:flex;align-items:center;gap:7px;font-weight:650;font-size:14px;color:var(--ink2)}
.tile .lab svg{width:18px;height:18px;color:var(--tc);flex:none}
.tile .val{font-size:30px;font-weight:750;letter-spacing:-.025em;line-height:1.1;margin-top:6px}
.tile .val small{font-size:14px;font-weight:600;color:var(--muted);margin-left:3px;letter-spacing:0}
.tile .sub{font-size:13px;color:var(--muted);line-height:1.3}
.tile .spark{position:absolute;right:10px;bottom:10px;width:46%;height:32px;opacity:.95}
.tile.wide{grid-column:1/-1}
.tile .go{margin-top:6px;font-weight:650;color:var(--tc)}
/* detail */
.dhead{display:flex;align-items:center;gap:8px;margin:6px 0 2px;color:var(--tc);font-weight:700}
.dhead svg{width:22px;height:22px}
.hero1{font-size:44px;font-weight:780;letter-spacing:-.03em;line-height:1.05;margin:2px 0}
.hero1 small{font-size:18px;font-weight:600;color:var(--muted);margin-left:4px;letter-spacing:0}
.delta{font-size:14px;color:var(--ink2)}
.chips{display:flex;gap:6px;margin:12px 0}
.chips a{padding:7px 14px;min-height:36px;border-radius:999px;background:var(--surface2);color:var(--ink2);font-weight:600;font-size:14px;display:inline-flex;align-items:center}
.chips a[aria-current=true]{background:var(--ink);color:var(--bg)}
.chart{width:100%;height:auto;display:block}
.chart .grid{stroke:var(--line);stroke-width:1}.chart .goal{stroke:var(--ink2);stroke-width:1.2;stroke-dasharray:4 4}
.chart .ax{font-size:10.5px;fill:var(--muted)}.chart .ser{font-size:11px;font-weight:650;fill:var(--ink2)}
.chart .dot{stroke:var(--surface);stroke-width:1.6}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:8px;font-size:13px;color:var(--ink2)}
.legend i{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:0 0 14px}
.stat{background:var(--surface);border-radius:16px;padding:12px;box-shadow:var(--shadow)}
.stat b{display:block;font-size:20px;letter-spacing:-.02em}.stat span{font-size:12.5px;color:var(--muted)}
.rows{list-style:none;margin:0;padding:0}
.rows li{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:11px 0;border-top:1px solid var(--line)}
.rows li:first-child{border-top:0}.rows .l{min-width:0}.rows .l b{display:block}.rows .l span{font-size:13px;color:var(--muted)}
.rows .r{font-weight:700;white-space:nowrap}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;background:var(--surface2);font-size:12px;color:var(--ink2);font-weight:600}
.cta{display:flex;justify-content:center;align-items:center;min-height:48px;border-radius:14px;background:var(--accent);color:var(--on-accent);font-weight:700}
/* meters and confirm */
.meter{margin:0 0 14px}.meter:last-child{margin-bottom:0}
.meter .mh{display:flex;justify-content:space-between;gap:8px;font-size:14px;margin-bottom:5px}.meter .mh b{font-weight:650}.meter .mh span{color:var(--ink2)}
.meter .tr{height:10px;border-radius:99px;background:var(--track);overflow:hidden}
.meter .tr i{display:block;height:100%;border-radius:99px;background:var(--tc,var(--accent))}
.meter.over .tr i{background:var(--warn)}.meter .ms{font-size:12.5px;color:var(--muted);margin-top:4px}
.dayh{display:flex;justify-content:space-between;font-weight:650;font-size:14px;color:var(--ink2);margin:14px 0 2px}.dayh:first-of-type{margin-top:0}
details.confirm summary{list-style:none;cursor:pointer;color:var(--bad);padding:6px 0;text-align:right}
details.confirm summary::-webkit-details-marker{display:none}
details.confirm[open] summary{display:none}
details.confirm .ask{font-size:13px;color:var(--ink2);text-align:right}
.more summary{color:var(--accent);cursor:pointer;text-align:right;font-size:13px;padding:4px 0}
.more .rows{margin-top:6px}
.mini{min-height:40px;padding:0 14px;border:0;border-radius:12px;background:var(--accent);color:var(--on-accent);font:inherit;font-weight:700;cursor:pointer}
.mini.ghost{background:var(--surface2);color:var(--ink2)}
.acts{display:flex;gap:6px;align-items:center}.acts form{margin:0}
.rows li.done .l b{color:var(--muted);text-decoration:line-through}
/* tabs */
nav.tabs{position:fixed;left:0;right:0;bottom:0;z-index:6;display:flex;justify-content:center;
background:color-mix(in srgb,var(--surface) 92%,transparent);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);
border-top:1px solid var(--line);padding:6px 8px calc(6px + env(safe-area-inset-bottom))}
nav.tabs .in{display:flex;width:100%;max-width:560px}
nav.tabs a{flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;min-height:50px;justify-content:center;color:var(--muted);font-size:11.5px;font-weight:650}
nav.tabs svg{width:25px;height:25px}
nav.tabs a[aria-current=page]{color:var(--accent)}
nav.tabs a.plus span.b{display:grid;place-items:center;width:46px;height:34px;border-radius:14px;background:var(--accent);color:var(--on-accent)}
nav.tabs a.plus svg{width:22px;height:22px}
/* forms */
label{display:block;font-size:14px;color:var(--ink2);margin:10px 0 4px;font-weight:600}
input,textarea{width:100%;padding:12px;min-height:46px;border:1px solid var(--line);border-radius:12px;background:var(--bg);color:var(--ink);font:inherit}
input:focus,textarea:focus{outline:2px solid var(--accent);outline-offset:1px}
input[type=checkbox]{width:auto;min-height:0;margin-right:8px}
.row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
button.go{margin-top:14px;min-height:48px;padding:0 20px;border:0;border-radius:14px;background:var(--accent);color:var(--on-accent);font:inherit;font-weight:700;cursor:pointer}
button.link{border:0;background:none;color:var(--bad);cursor:pointer;font:inherit;padding:6px 0}
button.ghost{min-height:44px;padding:0 16px;border:1px solid var(--line);border-radius:12px;background:none;color:var(--ink);font:inherit;font-weight:600}
table{width:100%;border-collapse:collapse;font-size:15px}td,th{text-align:left;padding:8px 4px;border-bottom:1px solid var(--line)}
@media (min-width:700px){.tiles{grid-template-columns:1fr 1fr 1fr}.ring{width:150px}}
@media print{.top,nav.tabs,.noprint{display:none}body{background:#fff;color:#000;padding:0}.card{box-shadow:none;border:1px solid #999}}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""

ICONS = {
    "home": "M3 11.5 12 4l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z",
    "plus": "M12 5v14M5 12h14",
    "report": "M7 3h8l4 4v14H7zM15 3v4h4M10 12h6M10 16h6",
    "settings": "M4 7h10M18 7h2M4 17h2M10 17h10M14 5v4M6 15v4",
    "back": "M15 5l-7 7 7 7",
    "sleep": "M20 14.5A8 8 0 1 1 9.5 4 6.5 6.5 0 0 0 20 14.5z",
    "steps": "M7 20c-2 0-3-2-3-4 0-3 2-6 4-6s3 2 3 5-2 5-4 5zM15 14c-1.6 0-2.4-1.6-2.4-3.2C12.6 8.5 14 6 15.6 6S18 7.8 18 10s-1.4 4-3 4z",
    "heart": "M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.4A4 4 0 0 1 19 10c0 5.6-7 10-7 10z",
    "bp": "M12 3s6 6.5 6 11a6 6 0 0 1-12 0c0-4.5 6-11 6-11z",
    "weight": "M5 4h14l2 16H3zM9 10a3 3 0 0 1 6 0",
    "food": "M4 11h16a8 8 0 0 1-16 0zM8 4c0 2 2 2 2 4M13 4c0 2 2 2 2 4",
    "work": "M3 9v6M6 7v10M18 7v10M21 9v6M6 12h12",
}


def icon(name: str, size: int | None = None) -> str:
    s = f' width="{size}" height="{size}"' if size else ""
    return (f'<svg viewBox="0 0 24 24"{s} fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true"><path d="{ICONS[name]}"/></svg>')


TABS = (("/", "Today", "home"), ("/log", "Log", "plus"), ("/gp", "Report", "report"), ("/settings", "Settings", "settings"))
HEAD = ('<link rel="manifest" href="/manifest.webmanifest"><link rel="apple-touch-icon" href="/apple-touch-icon.png">'
        '<link rel="icon" href="/icon-192.png"><meta name="theme-color" content="#F1F5F8" media="(prefers-color-scheme: light)">'
        '<meta name="theme-color" content="#0C1318" media="(prefers-color-scheme: dark)">'
        '<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-title" content="Wellness">')


def layout(title: str, body: str, *, name: str, active: str, csrf: str = "", flash: str = "",
           flash_err: bool = False, back: tuple[str, str] | None = None, tint: str | None = None) -> str:
    tabs = "".join(
        f'<a href="{h}"{" aria-current=page" if h == active else ""}{" class=plus" if i == "plus" else ""}>'
        + (f'<span class="b">{icon(i)}</span>' if i == "plus" else icon(i)) + f'<span>{esc(t)}</span></a>'
        for h, t, i in TABS)
    left = (f'<a class="back" href="{esc(back[0])}">{icon("back")}{esc(back[1])}</a>' if back
            else '<span class="brand">Wellness</span>')
    fl = (f'<div class="flash{" err" if flash_err else ""}" role="status">'
          f'{"Problem: " if flash_err else "Done: "}{esc(flash)}</div>' if flash else "")
    style = f' style="--tc:var({tint})"' if tint else ""
    return (f'<!doctype html><html lang="en-GB"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">{HEAD}'
            f'<title>{esc(title)} · Wellness</title><style>{CSS}</style></head><body{style}>'
            f'<header class="top">{left}<span class="sp"></span><span class="who"><span>{esc(name)}</span>'
            f'<span class="avatar" aria-hidden="true">{esc(name[:1].upper())}</span></span></header>'
            f'<main class="page">{fl}{body}</main>'
            f'<nav class="tabs" aria-label="Main"><div class="in">{tabs}</div></nav></body></html>')


def chips(path: str, current: int, options: tuple[int, ...] = (7, 30, 90)) -> str:
    return '<div class="chips" role="group" aria-label="Time range">' + "".join(
        f'<a href="{path}?d={d}"{" aria-current=true" if d == current else ""}>{d} days</a>' for d in options) + "</div>"


def stats(items: list[tuple[str, str]]) -> str:
    return '<div class="stats">' + "".join(f'<div class="stat"><b>{esc(v)}</b><span>{esc(k)}</span></div>' for k, v in items) + "</div>"


def tile(href: str, name: str, ic: str, tint: str, value: str, unit: str, sub: str, spark: str = "",
         wide: bool = False) -> str:
    return (f'<a class="tile{" wide" if wide else ""}" href="{esc(href)}" style="--tc:var({tint})">'
            f'<span class="lab">{icon(ic)}{esc(name)}</span>'
            f'<span class="val">{esc(value)}{f"<small>{esc(unit)}</small>" if unit else ""}</span>'
            f'<span class="sub">{esc(sub)}</span>{spark}</a>')


def legend(items: list[tuple[str, str]]) -> str:
    return '<div class="legend">' + "".join(f'<span><i style="background:var({c})"></i>{esc(t)}</span>' for c, t in items) + "</div>"
