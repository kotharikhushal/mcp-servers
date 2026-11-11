"""Small server-rendered SVG charts (no JavaScript). Colours come from CSS variables, so the same
markup follows light and dark mode. Every mark carries a <title> so it can be read on hover or by a
screen reader, and every chart has a text summary (aria-label) and a values list beside it on the page.
"""
from __future__ import annotations

import math
from html import escape as _esc
from typing import Callable, Sequence

W = 360
PAD_L, PAD_R, PAD_T = 6, 34, 12
Num = float | int | None


def esc(v: object) -> str:
    return _esc(str(v), quote=True)


def _commas(v: float) -> str:
    return f"{v:,.0f}"


def nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    p = 10 ** math.floor(math.log10(v))
    for m in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if v <= m * p:
            return m * p
    return 10 * p


def _tick_label(v: float) -> str:
    if abs(v) >= 1000:
        return f"{v / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"{v:.0f}" if abs(v - round(v)) < 1e-6 else f"{v:.1f}"


def _x_labels(labels: Sequence[str], xs: Sequence[float], base_y: float, every: int) -> str:
    out = []
    n = len(labels)
    for i, (lab, x) in enumerate(zip(labels, xs)):
        if lab and (n - 1 - i) % every == 0 and x > 24:
            out.append(f'<text x="{min(max(x, 20), W - PAD_R - 12):.1f}" y="{base_y + 15:.1f}" text-anchor="middle" class="ax">{esc(lab)}</text>')
    return "".join(out)


def bars(values: Sequence[Num], labels: Sequence[str], color: str, *, goal: float | None = None,
         unit: str = "", height: int = 170, label_every: int = 1, fmt: Callable[[float], str] = _commas,
         summary: str = "chart", faded: Sequence[bool] | None = None) -> str:
    """Vertical bars, one per day. `goal` draws a dashed line; bars below the goal are lighter."""
    n = max(len(values), 1)
    vals = [v for v in values if v is not None]
    top = nice_max(max([*vals, goal or 0, 1]) * 1.05)
    base = height - 24
    ph = base - PAD_T
    pw = W - PAD_L - PAD_R
    slot = pw / n
    bw = max(2.0, min(slot * 0.66, 26))
    out = [f'<svg viewBox="0 0 {W} {height}" class="chart" role="img" aria-label="{esc(summary)}">']
    for frac in (0, 0.5, 1):
        y = base - ph * frac
        out.append(f'<line x1="{PAD_L}" x2="{W - PAD_R + 4}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        out.append(f'<text x="{W - PAD_R + 8}" y="{y + 3.5:.1f}" class="ax">{_tick_label(top * frac)}</text>')
    xs = []
    for i, v in enumerate(values):
        x = PAD_L + slot * i + (slot - bw) / 2
        xs.append(x + bw / 2)
        if v is None:
            continue
        h = max(1.5, ph * v / top)
        y = base - h
        r = min(4.0, bw / 2, h)
        dim = (faded[i] if faded else (goal is not None and v < goal))
        out.append(f'<path d="M{x:.1f},{base} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} H{x + bw - r:.1f} '
                   f'Q{x + bw:.1f},{y:.1f} {x + bw:.1f},{y + r:.1f} V{base} Z" fill="var({color})" '
                   f'fill-opacity="{0.5 if dim else 1}"><title>{esc(labels[i])}: {esc(fmt(v))}{esc(unit)}</title></path>')
    if goal:
        gy = base - ph * goal / top
        out.append(f'<line x1="{PAD_L}" x2="{W - PAD_R + 4}" y1="{gy:.1f}" y2="{gy:.1f}" class="goal"/>')
    out.append(_x_labels(labels, xs, base, label_every))
    out.append("</svg>")
    return "".join(out)


STAGE_ORDER = ("deep", "rem", "light", "awake")


def stacked_sleep(nights: Sequence[dict[str, float] | None], labels: Sequence[str], *, height: int = 190,
                  label_every: int = 1, summary: str = "sleep stages") -> str:
    """One stacked bar per night: deep at the bottom, then REM, light and awake."""
    n = max(len(nights), 1)
    totals = [sum(d.values()) if d else 0 for d in nights]
    top = nice_max(max([*totals, 6]) * 1.05)
    base = height - 24
    ph = base - PAD_T
    slot = (W - PAD_L - PAD_R) / n
    bw = max(2.0, min(slot * 0.66, 26))
    out = [f'<svg viewBox="0 0 {W} {height}" class="chart" role="img" aria-label="{esc(summary)}">']
    for frac in (0, 0.5, 1):
        y = base - ph * frac
        out.append(f'<line x1="{PAD_L}" x2="{W - PAD_R + 4}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        out.append(f'<text x="{W - PAD_R + 8}" y="{y + 3.5:.1f}" class="ax">{_tick_label(top * frac)}h</text>')
    xs = []
    for i, d in enumerate(nights):
        x = PAD_L + slot * i + (slot - bw) / 2
        xs.append(x + bw / 2)
        if not d:
            continue
        y = base
        parts = [s for s in STAGE_ORDER if d.get(s, 0) > 0]
        for j, s in enumerate(parts):
            h = ph * d[s] / top
            if h < 0.8:
                continue
            gap = 1.5 if j < len(parts) - 1 else 0
            out.append(f'<rect x="{x:.1f}" y="{y - h:.1f}" width="{bw:.1f}" height="{max(h - gap, 0.8):.1f}" '
                       f'rx="{min(3, bw / 2):.1f}" fill="var(--st-{s})"><title>{esc(labels[i])}: {s} '
                       f'{d[s]:.1f} h</title></rect>')
            y -= h
    out.append(_x_labels(labels, xs, base, label_every))
    out.append("</svg>")
    return "".join(out)


def line(series: Sequence[tuple[str, str, Sequence[Num]]], labels: Sequence[str], *, xs: Sequence[float] | None = None,
         height: int = 190, thresholds: Sequence[tuple[float, str]] = (), band: Sequence[tuple[Num, Num]] | None = None,
         band_color: str | None = None, label_every: int = 1, fmt: Callable[[float], str] = _tick_label,
         summary: str = "trend", direct_labels: bool = True, pad: float | None = None,
         axis: Sequence[tuple[float, str]] | None = None) -> str:
    """Lines over time. series: (name, css colour variable, values). xs are 0..1 positions."""
    allv = [v for _, _, vs in series for v in vs if v is not None]
    if band:
        allv += [b for pair in band for b in pair if b is not None]
    allv += [t for t, _ in thresholds]
    if not allv:
        return ""
    lo, hi = min(allv), max(allv)
    span = max(hi - lo, 1.0)
    p = pad if pad is not None else span * 0.18
    lo, hi = lo - p, hi + p
    n = max(len(labels), 1)
    pw = W - PAD_L - PAD_R
    base = height - 24
    ph = base - PAD_T
    pos = list(xs) if xs is not None else [(i + 0.5) / n for i in range(n)]
    px = [PAD_L + pw * x for x in pos]

    def py(v: float) -> float:
        return base - ph * (v - lo) / (hi - lo)

    out = [f'<svg viewBox="0 0 {W} {height}" class="chart" role="img" aria-label="{esc(summary)}">']
    for k in range(4):
        v = lo + (hi - lo) * k / 3
        out.append(f'<line x1="{PAD_L}" x2="{W - PAD_R + 4}" y1="{py(v):.1f}" y2="{py(v):.1f}" class="grid"/>')
        out.append(f'<text x="{W - PAD_R + 8}" y="{py(v) + 3.5:.1f}" class="ax">{esc(fmt(v))}</text>')
    for t, name in thresholds:
        out.append(f'<line x1="{PAD_L}" x2="{W - PAD_R + 4}" y1="{py(t):.1f}" y2="{py(t):.1f}" class="goal"/>')
        out.append(f'<text x="{PAD_L + 2}" y="{py(t) - 4:.1f}" class="ax">{esc(name)}</text>')
    if band and band_color:
        top_pts, bot_pts = [], []
        for x, (a, b) in zip(px, band):
            if a is not None and b is not None:
                top_pts.append(f"{x:.1f},{py(b):.1f}")
                bot_pts.append(f"{x:.1f},{py(a):.1f}")
        if len(top_pts) >= 2:
            out.append(f'<polygon points="{" ".join(top_pts + bot_pts[::-1])}" fill="var({band_color})" '
                       f'fill-opacity="0.16"/>')
    for name, colour, vs in series:
        seg: list[str] = []
        segs: list[list[str]] = []
        for x, v in zip(px, vs):
            if v is None:
                if seg:
                    segs.append(seg)
                    seg = []
            else:
                seg.append(f"{x:.1f},{py(v):.1f}")
        if seg:
            segs.append(seg)
        for s in segs:
            if len(s) >= 2:
                out.append(f'<polyline points="{" ".join(s)}" fill="none" stroke="var({colour})" stroke-width="2.5" '
                           f'stroke-linecap="round" stroke-linejoin="round"/>')
        show_dots = len(px) <= 40
        last = None
        for i, (x, v) in enumerate(zip(px, vs)):
            if v is None:
                continue
            last = (x, v)
            if show_dots:
                out.append(f'<circle cx="{x:.1f}" cy="{py(v):.1f}" r="3.2" fill="var({colour})" class="dot">'
                           f'<title>{esc(labels[i] if i < len(labels) else "")}: {esc(fmt(v))}</title></circle>')
        if last and direct_labels and len(series) > 1:
            out.append(f'<text x="{last[0] - 4:.1f}" y="{py(last[1]) - 9:.1f}" text-anchor="end" class="ser">{esc(name)}</text>')
    if axis is not None:
        out.extend(f'<text x="{min(max(PAD_L + pw * x, 20), W - PAD_R - 12):.1f}" y="{base + 15:.1f}" '
                   f'text-anchor="middle" class="ax">{esc(t)}</text>' for x, t in axis)
    else:
        out.append(_x_labels(labels, px, base, label_every))
    out.append("</svg>")
    return "".join(out)


def ring(frac: float, color: str, *, size: int = 124, stroke: int = 13, big: str = "", small: str = "",
         summary: str = "progress") -> str:
    r = (size - stroke) / 2
    c = 2 * math.pi * r
    f = max(0.0, min(frac, 1.0))
    mid = size / 2
    return (f'<svg viewBox="0 0 {size} {size}" class="ring" role="img" aria-label="{esc(summary)}">'
            f'<circle cx="{mid}" cy="{mid}" r="{r:.1f}" fill="none" stroke="var(--track)" stroke-width="{stroke}"/>'
            f'<circle cx="{mid}" cy="{mid}" r="{r:.1f}" fill="none" stroke="var({color})" stroke-width="{stroke}" '
            f'stroke-linecap="round" stroke-dasharray="{c * f:.1f} {c:.1f}" transform="rotate(-90 {mid} {mid})"/>'
            f'<text x="{mid}" y="{mid + 2}" text-anchor="middle" class="ring-big">{esc(big)}</text>'
            f'<text x="{mid}" y="{mid + 18}" text-anchor="middle" class="ring-small">{esc(small)}</text></svg>')


def spark(values: Sequence[Num], color: str, *, w: int = 120, h: int = 34) -> str:
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(pts) < 2:
        return '<div class="spark-empty"></div>'
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    span = max(hi - lo, 1e-9)
    n = max(len(values) - 1, 1)
    xy = [(3 + (w - 6) * i / n, h - 4 - (h - 8) * (v - lo) / span if hi > lo else h / 2) for i, v in pts]
    line_pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    area = f"{xy[0][0]:.1f},{h} " + line_pts + f" {xy[-1][0]:.1f},{h}"
    return (f'<svg viewBox="0 0 {w} {h}" class="spark" aria-hidden="true"><polygon points="{area}" fill="var({color})" '
            f'fill-opacity="0.14"/><polyline points="{line_pts}" fill="none" stroke="var({color})" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round"/><circle cx="{xy[-1][0]:.1f}" cy="{xy[-1][1]:.1f}" r="2.8" '
            f'fill="var({color})"/></svg>')
