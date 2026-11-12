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


# Colour themes. Each sets only the accent (light and dark variants); every neutral is mixed from it, so a
# theme tints the whole app. Measurement colours (steps, sleep, heart ...) stay the same in every theme.
PALETTES = {
    "tide": ("Tide", "#0B7A75", "#FFFFFF", "#3CC4B7", "#06201E"),
    "dusk": ("Dusk", "#5646CF", "#FFFFFF", "#A99DFF", "#16113F"),
    "ember": ("Ember", "#BF3F0A", "#FFFFFF", "#FF9D66", "#2B1103"),
    "moss": ("Moss", "#3B7A24", "#FFFFFF", "#8FD672", "#0F2008"),
    "graphite": ("Graphite", "#2A3640", "#FFFFFF", "#D6DFE5", "#0C1216"),
}
MODES = {"auto": "Match my phone", "light": "Light", "dark": "Dark"}
DEFAULT_PALETTE, DEFAULT_MODE = "tide", "auto"

_LIGHT = ("--base:#F4F6F8;--surface:#FFFFFF;--surface2:#EBEFF3;--ink:#0E1B23;--ink2:#46575F;--muted:#5F707A;"
          "--line:rgba(14,27,35,.10);--track:rgba(14,27,35,.09);--warn:#A24A06;--bad:#B42318;--err-bg:#FDE8E5;"
          "--c-sleep:#6E56CF;--c-steps:#0E8F80;--c-heart:#E5484D;--c-weight:#3E63DD;--c-food:#C77700;--c-work:#D6409F;"
          "--st-deep:#3A2E94;--st-rem:#7E63E8;--st-light:#CFC5FA;--st-awake:#C8D0D7;"
          "--shadow:0 1px 2px rgba(14,27,35,.05),0 10px 28px -12px rgba(14,27,35,.14)")
_DARK = ("--base:#0A1015;--surface:#131C23;--surface2:#1C2830;--ink:#EBF2F6;--ink2:#B5C4CD;--muted:#8DA1AC;"
         "--line:rgba(235,242,246,.10);--track:rgba(235,242,246,.11);--warn:#F0A04B;--bad:#FF8A80;--err-bg:#3A1714;"
         "--c-sleep:#8A78E0;--c-steps:#17A594;--c-heart:#F0545A;--c-weight:#5B7EEB;--c-food:#D88A14;--c-work:#E04BA5;"
         "--st-deep:#4D3FB0;--st-rem:#9B88F2;--st-light:#D0C7FA;--st-awake:#4A5A66;--shadow:0 0 0 rgba(0,0,0,0)")


def _theme_css() -> str:
    dark_sel = ('html[data-mode="dark"]', '@media (prefers-color-scheme:dark){html:not([data-mode="light"])')
    out = [f":root{{color-scheme:light dark;{_LIGHT}}}",
           f'html[data-mode="light"]{{color-scheme:light}}',
           f'html[data-mode="dark"]{{color-scheme:dark;{_DARK}}}',
           f'@media (prefers-color-scheme:dark){{html:not([data-mode="light"]){{{_DARK}}}}}']
    for key, (_, la, lo, da, do) in PALETTES.items():
        out.append(f':root[data-palette="{key}"]{{--accent:{la};--on-accent:{lo}}}')
        out.append(f'html[data-palette="{key}"][data-mode="dark"]{{--accent:{da};--on-accent:{do}}}')
        out.append(f'@media (prefers-color-scheme:dark){{html[data-palette="{key}"]:not([data-mode="light"]){{--accent:{da};--on-accent:{do}}}}}')
    del dark_sel
    return "".join(out)


CSS = _theme_css() + """
@font-face{font-family:"Bricolage Grotesque";src:url(/fonts/bricolage-grotesque.woff2) format("woff2");font-weight:200 800;font-display:swap}
:root{--r:22px;--accent:#0B7A75;--on-accent:#fff;
--bg:color-mix(in srgb,var(--accent) 5%,var(--base));--soft:color-mix(in srgb,var(--accent) 13%,var(--surface))}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.45 "Bricolage Grotesque",ui-rounded,"SF Pro Rounded","Google Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
font-variant-numeric:tabular-nums;padding-bottom:calc(104px + env(safe-area-inset-bottom))}
a{color:var(--accent);text-decoration:none}
a:focus-visible,button:focus-visible,summary:focus-visible,input:focus-visible+span{outline:3px solid color-mix(in srgb,var(--accent) 70%,transparent);outline-offset:2px;border-radius:10px}
.top{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:10px;min-height:56px;
padding:env(safe-area-inset-top) 18px 0;background:color-mix(in srgb,var(--bg) 82%,transparent);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px)}
.top .brand{display:inline-flex;align-items:center;gap:8px;font-weight:750;font-size:18px;letter-spacing:-.02em}
.top .brand::before{content:"";width:12px;height:12px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 4px color-mix(in srgb,var(--accent) 22%,transparent)}
.top .back{display:inline-flex;align-items:center;gap:2px;min-height:44px;font-weight:650;color:var(--ink)}
.top .back svg{width:22px;height:22px}
.top .sp{flex:1}
.who{display:inline-flex;align-items:center;gap:9px;font-size:14px;font-weight:600;color:var(--ink2)}
.avatar{width:32px;height:32px;border-radius:50%;display:grid;place-items:center;background:var(--accent);color:var(--on-accent);font-weight:750;font-size:14px}
main.page{max-width:560px;margin:0 auto;padding:6px 18px 8px}
h1{font-size:32px;line-height:1.1;letter-spacing:-.035em;font-weight:760;margin:10px 0 4px}
h2{font-size:19px;letter-spacing:-.02em;font-weight:700;margin:0 0 10px}
.lede{color:var(--ink2);margin:0 0 14px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:18px;margin:0 0 14px;box-shadow:var(--shadow)}
.card.flat{box-shadow:none}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.k{font-size:30px;font-weight:750;letter-spacing:-.025em}.u{font-size:13px;color:var(--muted)}.m{color:var(--muted);font-size:14px}
.note{border-radius:14px;background:var(--soft);padding:11px 14px;margin:8px 0;color:var(--ink);position:relative;padding-left:18px}
.note::before{content:"";position:absolute;left:6px;top:10px;bottom:10px;width:4px;border-radius:4px;background:var(--accent)}
.note.warn::before{background:var(--warn)}.note.bad::before{background:var(--bad)}
.flash{padding:13px 16px;border-radius:16px;background:var(--soft);border:1px solid var(--accent);font-weight:650;margin:6px 0 14px}
.flash.err{background:var(--err-bg);color:var(--ink);border-color:var(--bad)}
/* home */
.greet{margin:10px 0 16px}.greet h1{margin:0;font-size:30px}.greet p{margin:4px 0 0;color:var(--ink2);font-weight:500}
.hero{display:flex;justify-content:space-around;align-items:flex-start;gap:8px;padding:22px 10px 16px;border:0;
background:radial-gradient(120% 90% at 50% 0%,color-mix(in srgb,var(--accent) 24%,var(--surface)) 0%,var(--surface) 72%)}
.hero a{color:inherit;display:block;text-align:center;flex:1;min-width:0}
.ring{width:min(38vw,156px);height:auto;display:block;margin:0 auto}
.ring circle+circle{animation:ringfill .9s cubic-bezier(.2,.7,.2,1) both}
@keyframes ringfill{from{stroke-dasharray:0 1000}}
.ring-big{font-size:25px;font-weight:780;letter-spacing:-.03em;fill:var(--ink)}.ring-small{font-size:11.5px;fill:var(--muted)}
.hero .cap{font-weight:700;margin-top:8px;letter-spacing:-.01em}.hero .sub{font-size:13px;color:var(--muted)}
.coach{display:flex;gap:14px;align-items:flex-start}
.coach .face{flex:none;width:40px;height:40px;border-radius:14px;background:var(--accent);color:var(--on-accent);display:grid;place-items:center;font-weight:800;font-size:18px}
.coach .notes{flex:1;min-width:0}.coach .notes .note:first-child{margin-top:0}.coach h2{margin-bottom:4px}
.sect{display:flex;justify-content:space-between;align-items:baseline;margin:22px 2px 10px}.sect h2{margin:0}
.tiles{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;margin-bottom:14px}
a.tile{display:flex;flex-direction:column;gap:2px;min-height:144px;padding:16px;border-radius:var(--r);background:var(--surface);border:1px solid var(--line);color:var(--ink);box-shadow:var(--shadow);position:relative;overflow:hidden;transition:transform .15s}
a.tile:active{transform:scale(.985)}
.tile .lab{display:flex;align-items:center;gap:7px;font-weight:650;font-size:13.5px;color:var(--ink2)}
.tile .lab svg{width:30px;height:30px;padding:6px;border-radius:10px;color:var(--tc);background:color-mix(in srgb,var(--tc) 15%,transparent);flex:none}
.tile .val{display:flex;flex-wrap:wrap;align-items:baseline;column-gap:5px;font-size:34px;font-weight:780;letter-spacing:-.04em;line-height:1.05;margin-top:10px}
.tile .val small{font-size:14px;font-weight:600;color:var(--muted);margin-left:0;letter-spacing:0}
.tile .sub{font-size:13px;color:var(--muted);line-height:1.3}
.tile .spark{display:block;width:100%;height:30px;margin-top:auto;padding-top:10px;box-sizing:content-box}.tile .spark-empty{margin-top:auto}
.card.ai .aih{font-size:19px;font-weight:700;letter-spacing:-.02em;line-height:1.25;margin:2px 0 8px}.card.ai .aip{margin:0 0 8px;padding-left:20px;display:grid;gap:6px;line-height:1.4}.card.ai .aip li::marker{color:var(--accent)}
.tile.wide{grid-column:1/-1}
.tile .go{margin-top:6px;font-weight:650;color:var(--tc)}
/* detail */
.dhead{display:flex;align-items:center;gap:9px;margin:8px 0 2px;color:var(--tc);font-weight:700}
.dhead svg{width:32px;height:32px;padding:7px;border-radius:11px;background:color-mix(in srgb,var(--tc) 15%,transparent)}
.hero1{font-size:60px;font-weight:800;letter-spacing:-.05em;line-height:1;margin:6px 0 4px}
.hero1 small{font-size:19px;font-weight:600;color:var(--muted);margin-left:6px;letter-spacing:0}
.delta{font-size:14.5px;color:var(--ink2)}
.chips{display:flex;gap:6px;margin:14px 0;flex-wrap:wrap}
.chips a{padding:8px 16px;min-height:38px;border-radius:999px;background:var(--surface);border:1px solid var(--line);color:var(--ink2);font-weight:650;font-size:14px;display:inline-flex;align-items:center}
.chips a[aria-current=true]{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.chart{width:100%;height:auto;display:block}
.chart .grid{stroke:var(--line);stroke-width:1}.chart .goal{stroke:var(--ink2);stroke-width:1.2;stroke-dasharray:4 4}
.chart .ax{font-size:10.5px;fill:var(--muted)}.chart .ser{font-size:11px;font-weight:650;fill:var(--ink2)}
.chart .dot{stroke:var(--surface);stroke-width:1.6}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:10px;font-size:13px;color:var(--ink2)}
.legend i{display:inline-block;width:11px;height:11px;border-radius:4px;margin-right:6px;vertical-align:-1px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:0 0 14px}
.stat{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:13px;box-shadow:var(--shadow)}
.stat b{display:block;font-size:21px;font-weight:750;letter-spacing:-.03em}.stat span{font-size:12.5px;color:var(--muted)}
.rows{list-style:none;margin:0;padding:0}
.rows li{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:12px 0;border-top:1px solid var(--line)}
.rows li:first-child{border-top:0}.rows .l{min-width:0}.rows .l b{display:block;font-weight:650}.rows .l span{font-size:13px;color:var(--muted)}
.rows .r{font-weight:750;white-space:nowrap}
.pill{display:inline-block;padding:3px 10px;border-radius:999px;background:var(--soft);font-size:12px;color:var(--ink2);font-weight:650}
.cta{display:flex;justify-content:center;align-items:center;min-height:50px;border-radius:16px;background:var(--accent);color:var(--on-accent);font-weight:700;letter-spacing:-.01em}
/* meters and confirm */
.meter{margin:0 0 16px}.meter:last-child{margin-bottom:0}
.meter .mh{display:flex;justify-content:space-between;gap:8px;font-size:14px;margin-bottom:6px}.meter .mh b{font-weight:700}.meter .mh span{color:var(--ink2)}
.meter .tr{height:12px;border-radius:99px;background:var(--track);overflow:hidden}
.meter .tr i{display:block;height:100%;border-radius:99px;background:var(--tc,var(--accent));animation:barfill .8s cubic-bezier(.2,.7,.2,1) both;transform-origin:left}
@keyframes barfill{from{transform:scaleX(0)}}
.meter.over .tr i{background:var(--warn)}.meter .ms{font-size:12.5px;color:var(--muted);margin-top:5px}
.dayh{display:flex;justify-content:space-between;font-weight:700;font-size:14px;color:var(--ink2);margin:16px 0 2px}.dayh:first-of-type{margin-top:0}
details.confirm summary{list-style:none;cursor:pointer;color:var(--bad);padding:6px 0;text-align:right}
details.confirm summary::-webkit-details-marker{display:none}
details.confirm[open] summary{display:none}
details.confirm .ask{font-size:13px;color:var(--ink2);text-align:right}
.more summary{color:var(--accent);cursor:pointer;text-align:right;font-size:13px;padding:4px 0}
.more .rows{margin-top:6px}
.mini{white-space:nowrap;min-height:40px;padding:0 15px;border:0;border-radius:13px;background:var(--accent);color:var(--on-accent);font:inherit;font-weight:700;cursor:pointer}
.mini.ghost{background:var(--surface2);color:var(--ink2)}
.rows li:has(.acts){flex-direction:column;align-items:stretch;gap:8px}.acts{display:flex;gap:6px;align-items:center}.acts form{margin:0}
.rows li.done .l b{color:var(--muted);text-decoration:line-through}
/* appearance */
.opts{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 4px}
.opt{position:relative;display:inline-flex;margin:0}
.opt input{position:absolute;opacity:0;inset:0;width:100%;height:100%;min-height:0;margin:0;cursor:pointer}
.opt span{display:inline-flex;align-items:center;gap:8px;min-height:44px;padding:0 15px;border-radius:999px;background:var(--surface2);border:2px solid transparent;font-weight:650;color:var(--ink2)}
.opt input:checked+span{border-color:var(--accent);background:var(--soft);color:var(--ink)}
.opt i{width:18px;height:18px;border-radius:50%;background:var(--sw);box-shadow:inset 0 0 0 2px rgba(255,255,255,.35)}
/* chat */
.chat{display:grid;gap:8px;margin-bottom:12px}.chat .msg{max-width:86%;padding:9px 13px;border-radius:18px;line-height:1.4;overflow-wrap:anywhere;white-space:pre-wrap}
.chat .msg.user{justify-self:end;background:var(--accent);color:var(--on-accent);border-bottom-right-radius:6px}
.chat .msg.bot{justify-self:start;background:var(--surface);border:1px solid var(--line);border-bottom-left-radius:6px}
.chat .msg.note{justify-self:center;color:var(--muted);font-size:13px;background:none;padding:2px 6px}
.chat .when{font-size:11px;color:var(--muted);margin-top:2px}
.card.pend{border:2px solid var(--accent)}.card.pend .acts{display:flex;gap:8px;margin-top:10px;align-items:center}.card.pend button{margin:0}.card.pend form{margin:0}
.chatbox{position:sticky;bottom:calc(86px + env(safe-area-inset-bottom));display:flex;gap:8px;align-items:flex-end}.chatbox input{flex:1}

/* refresh: Google Health tonal cards and soft sky, Lifesum friendliness, Cronometer bars */
body{background:linear-gradient(180deg,color-mix(in srgb,var(--accent) 16%,var(--base)) 0,var(--bg) 420px) no-repeat,var(--bg)}
:root{--r:26px}
.card{border-color:transparent}
.hero2{display:grid;grid-template-columns:1.05fr 1fr;gap:12px;align-items:center;padding:16px;background:var(--surface)}
.hero2 .bigring{color:inherit;display:block;text-align:center}.hero2 .bigring .ring{width:100%;max-width:170px}
.hero2 .bigring span{display:block;font-weight:700;margin-top:2px;color:var(--ink2);font-size:14px}
.hero2 .stack{display:grid;gap:10px}
.hero2 .chip{display:flex;flex-direction:column;padding:10px 14px;border-radius:18px;color:var(--ink);
background:color-mix(in srgb,var(--tc) 20%,var(--surface));min-width:0}
.hero2 .chip .l{font-size:12.5px;font-weight:700;color:color-mix(in srgb,var(--tc) 55%,var(--ink))}
.hero2 .chip b{font-size:24px;font-weight:780;letter-spacing:-.03em;line-height:1.15}
.hero2 .chip small{font-size:12px;color:var(--ink2)}
.qa{display:flex;gap:10px;margin:0 0 14px}
.qb{flex:1;display:flex;align-items:center;justify-content:center;gap:7px;min-height:48px;border-radius:999px;font-weight:700;
background:color-mix(in srgb,var(--accent) 16%,var(--surface));color:var(--ink)}
.qb svg{width:20px;height:20px;color:var(--accent)}
.qb.pri{background:var(--accent);color:var(--on-accent)}.qb.pri svg{color:var(--on-accent)}
.card.ai{background:linear-gradient(180deg,color-mix(in srgb,var(--accent) 9%,var(--surface)),var(--surface))}
.aihead{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:13px;font-weight:600;margin-bottom:6px}
.aihead svg{width:20px;height:20px;color:var(--accent)}
a.tile{background:color-mix(in srgb,var(--tc) 8%,var(--surface));border-color:transparent}
.tile .st{align-self:flex-start;margin-top:8px;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:700;
background:color-mix(in srgb,var(--tc) 20%,var(--surface));color:var(--ink)}
.tile .st+.val{margin-top:4px}
.stat{border-color:transparent;background:color-mix(in srgb,var(--accent) 8%,var(--surface))}
.chat .msg.user{background:color-mix(in srgb,var(--accent) 20%,var(--surface));color:var(--ink);border-radius:20px 20px 6px 20px}
.chat .msg.bot{background:none;border:0;padding:4px 2px;max-width:100%;border-radius:0}
.chat .msg.bot::before{content:"";display:block;width:20px;height:20px;margin-bottom:4px;background:var(--accent);
-webkit-mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z'/%3E%3C/svg%3E") center/contain no-repeat;
mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z'/%3E%3C/svg%3E") center/contain no-repeat}
.chatbox{padding:8px 8px 8px 16px;border-radius:999px;background:var(--surface);box-shadow:var(--shadow);border:1px solid var(--line)}
.chatbox input{border:0!important;background:none!important;box-shadow:none!important;min-height:40px}
.chatbox button.go{margin:0;min-height:44px;border-radius:999px}
/* tabs */
nav.tabs{position:fixed;left:0;right:0;bottom:0;z-index:6;display:flex;justify-content:center;padding:0 14px calc(10px + env(safe-area-inset-bottom));pointer-events:none}
nav.tabs .in{pointer-events:auto;display:flex;width:100%;max-width:440px;gap:2px;padding:6px;border-radius:28px;
background:color-mix(in srgb,var(--surface) 88%,transparent);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
border:1px solid var(--line);box-shadow:0 12px 32px -10px rgba(0,0,0,.28)}
nav.tabs a{flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;min-height:54px;justify-content:center;color:var(--muted);font-size:11.5px;font-weight:650;border-radius:22px}
nav.tabs svg{width:24px;height:24px}
nav.tabs a[aria-current=page]{color:var(--accent);background:var(--soft)}
nav.tabs a.plus span.b{display:grid;place-items:center;width:48px;height:34px;border-radius:15px;background:var(--accent);color:var(--on-accent)}
nav.tabs a.plus[aria-current=page]{background:none}
nav.tabs a.plus svg{width:22px;height:22px}
/* forms */
label{display:block;font-size:14px;color:var(--ink2);margin:12px 0 5px;font-weight:650}
input,textarea{width:100%;padding:12px 14px;min-height:48px;border:1px solid var(--line);border-radius:14px;background:var(--surface2);color:var(--ink);font:inherit}
input:focus,textarea:focus{outline:2px solid var(--accent);outline-offset:1px}
input[type=checkbox]{width:auto;min-height:0;margin-right:8px}
.row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
button.go{margin-top:16px;min-height:50px;padding:0 22px;border:0;border-radius:16px;background:var(--accent);color:var(--on-accent);font:inherit;font-weight:700;cursor:pointer}
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
    "chat": "M4 5h16v11H9l-5 4z",
    "spark": "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z",
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


TABS = (("/", "Today", "home"), ("/chat", "Chat", "chat"), ("/log", "Log", "plus"), ("/gp", "Report", "report"),
        ("/settings", "Settings", "settings"))
HEAD = ('<link rel="manifest" href="/manifest.webmanifest"><link rel="apple-touch-icon" href="/apple-touch-icon.png">'
        '<link rel="icon" href="/icon-192.png"><meta name="theme-color" content="#F1F5F8" media="(prefers-color-scheme: light)">'
        '<meta name="theme-color" content="#0C1318" media="(prefers-color-scheme: dark)">'
        '<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-title" content="Wellness">')


def layout(title: str, body: str, *, name: str, active: str, csrf: str = "", flash: str = "",
           flash_err: bool = False, back: tuple[str, str] | None = None, tint: str | None = None,
           mode: str = DEFAULT_MODE, palette: str = DEFAULT_PALETTE, chat: bool = False) -> str:
    tabs = "".join(
        f'<a href="{h}"{" aria-current=page" if h == active else ""}{" class=plus" if i == "plus" else ""}>'
        + (f'<span class="b">{icon(i)}</span>' if i == "plus" else icon(i)) + f'<span>{esc(t)}</span></a>'
        for h, t, i in TABS if chat or h != "/chat")
    left = (f'<a class="back" href="{esc(back[0])}">{icon("back")}{esc(back[1])}</a>' if back
            else '<span class="brand">Wellness</span>')
    fl = (f'<div class="flash{" err" if flash_err else ""}" role="status">'
          f'{"Problem: " if flash_err else "Done: "}{esc(flash)}</div>' if flash else "")
    style = f' style="--tc:var({tint})"' if tint else ""
    mode = mode if mode in MODES else DEFAULT_MODE
    palette = palette if palette in PALETTES else DEFAULT_PALETTE
    return (f'<!doctype html><html lang="en-GB" data-mode="{mode}" data-palette="{palette}"><head><meta charset="utf-8">'
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
         wide: bool = False, pill: str = "") -> str:
    return (f'<a class="tile{" wide" if wide else ""}" href="{esc(href)}" style="--tc:var({tint})">'
            f'<span class="lab">{icon(ic)}{esc(name)}</span>{f"<span class=st>{esc(pill)}</span>" if pill else ""}'
            f'<span class="val">{esc(value)}{f"<small>{esc(unit)}</small>" if unit else ""}</span>'
            f'<span class="sub">{esc(sub)}</span>{spark}</a>')


def legend(items: list[tuple[str, str]]) -> str:
    return '<div class="legend">' + "".join(f'<span><i style="background:var({c})"></i>{esc(t)}</span>' for c, t in items) + "</div>"
