"""Blood pressure reminders, delivered as push notifications through ntfy (https://ntfy.sh).

A background thread checks once a minute. A reminder is sent once per slot per day, only inside a
three hour window after its time (so a restart at midnight does not fire yesterday's reminder), and
only when no reading has been logged in that half of the day. Messages never contain health data,
because anyone who knows the topic name can read a topic.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from datetime import datetime, time as dtime, timedelta
from typing import Callable

import httpx

from .store import Store

log = logging.getLogger("health-mcp.reminders")
SLOTS = ("morning", "evening")
DEFAULTS = {"morning": "08:00", "evening": "21:00"}
WINDOW = timedelta(hours=3)
RETRY_SECONDS = 300
MESSAGES = {
    "morning": "Time for your morning blood pressure reading. Sit quietly for 5 minutes first, arm supported.",
    "evening": "Time for your evening blood pressure reading. Sit quietly for 5 minutes first, arm supported.",
}


def parse_hhmm(value: str | None, default: str) -> dtime:
    try:
        h, m = (value or default).split(":")
        return dtime(int(h), int(m))
    except (ValueError, AttributeError):
        h, m = default.split(":")
        return dtime(int(h), int(m))


def new_topic() -> str:
    return "wellness-" + secrets.token_urlsafe(12)


def due_slots(store: Store, now: datetime | None = None) -> list[str]:
    """Slots that should be sent now. Marks a slot done (without sending) when already logged."""
    if store.get_setting("reminders_enabled") != "1" or not store.get_setting("reminder_topic"):
        return []
    loc = store.local(now or store.now())
    day = loc.date().isoformat()
    readings = [r for r in store.bp_readings(1) if r["time"][:10] == day]
    out = []
    for slot in SLOTS:
        start = datetime.combine(loc.date(), parse_hhmm(store.get_setting(f"reminder_{slot}"), DEFAULTS[slot]), store.tz)
        if not (start <= loc < start + WINDOW) or store.get_setting(f"reminder_sent:{slot}") == day:
            continue
        morning = slot == "morning"
        if any((r["time"][11:13] < "12") == morning for r in readings):
            store.set_setting(f"reminder_sent:{slot}", day)
            continue
        out.append(slot)
    return out


Notifier = Callable[[str, str, str], None]


def make_notifier(base: str, client: httpx.Client | None = None) -> Notifier:
    http = client or httpx.Client(timeout=10)

    def notify(topic: str, title: str, message: str) -> None:
        r = http.post(f"{base.rstrip('/')}/{topic}", content=message.encode(),
                      headers={"Title": title, "Tags": "heart", "Priority": "default"})
        r.raise_for_status()
    return notify


def run_once(stores: dict[str, Store], notify: Notifier, attempts: dict[tuple[str, str], float],
             now: datetime | None = None) -> int:
    sent = 0
    for user, store in stores.items():
        for slot in due_slots(store, now):
            last = attempts.get((user, slot), 0.0)
            if time.monotonic() - last < RETRY_SECONDS:
                continue
            attempts[(user, slot)] = time.monotonic()
            try:
                notify(store.get_setting("reminder_topic") or "", "Wellness", MESSAGES[slot])
            except Exception as e:  # network trouble must never stop the loop
                log.warning("reminder not sent (%s): %s", slot, type(e).__name__)
                continue
            store.set_setting(f"reminder_sent:{slot}", store.local(now or store.now()).date().isoformat())
            sent += 1
    return sent


def start(get_stores: Callable[[], dict[str, Store]], notify: Notifier) -> threading.Thread:
    attempts: dict[tuple[str, str], float] = {}

    def loop() -> None:
        while True:
            try:
                run_once(get_stores(), notify, attempts)
            except Exception:
                log.exception("reminder loop error")
            time.sleep(60)

    t = threading.Thread(target=loop, name="reminders", daemon=True)
    t.start()
    return t
