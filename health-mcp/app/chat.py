"""Chat with a self-hosted model (Ollama) about your own numbers, and log things after you confirm.

How it stays safe:
  * The model never touches data. It returns a reply plus, at most, one proposed action from a fixed list.
  * Code checks the action: known kind, sane ranges, and every number must be one you typed in your message.
  * A proposed action is only saved as "pending". Nothing is written until you press Confirm.
  * The reply gets the same checks as the AI analysis: no invented numbers, no diagnosis or medication wording,
    blood pressure only with the GP, no alarming wording about a mixed reading.
  * The model is never trained by chatting. Saved history is only sent back to it as context.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import timedelta
from typing import Any

import httpx

from . import analysis
from .store import Store

HISTORY_MAX = 20          # messages kept per person
HISTORY_SENT = 10         # of those, how many go back to the model
MSG_MAX = 500
PENDING_MINUTES = 30
KINDS = ("none", "log_weight", "log_bp", "log_meal", "log_recipe", "add_shopping")

SYSTEM = """You are the chat helper in a personal wellness app, talking to the person whose numbers are in the facts.
Rules:
- Answer only from the facts and the conversation. Never calculate new numbers and never invent any. If the facts do not hold the answer, say so.
- Keep replies short, kind and practical, in British English and plain words, no emojis.
- For blood pressure, quote "latest_sitting_summary" and compare top and bottom separately as it states. Never call readings "high" or say they "need attention". Say the GP follow-up is the place to discuss them.
- Never diagnose, never name a medical condition, never comment on medication or doses. Do not set new health targets.
- To save something, propose ONE action and say in the reply what you will save and that they must press Confirm. Only propose an action when the person clearly asks to log or add something, and only with numbers they typed. Never claim it is saved yet.
- Actions: log_weight (kg), log_bp (systolic, diastolic, optional pulse), log_meal (name, kcal, optional protein_g, salt_g; only when they gave the kcal), log_recipe (recipe = words to find it in their recipe book, servings), add_shopping (items = list of short item names). Otherwise use "none".
Return JSON only: {"reply": string up to 500 characters, "action": {"kind": one of none|log_weight|log_bp|log_meal|log_recipe|add_shopping, plus the fields that action needs}}."""

SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "action": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "kg": {"type": "number"}, "systolic": {"type": "number"}, "diastolic": {"type": "number"},
            "pulse": {"type": "number"}, "name": {"type": "string"}, "kcal": {"type": "number"},
            "protein_g": {"type": "number"}, "salt_g": {"type": "number"},
            "recipe": {"type": "string"}, "servings": {"type": "number"},
            "items": {"type": "array", "items": {"type": "string"}}},
            "required": ["kind"]}},
    "required": ["reply", "action"]}


class ChatError(Exception):
    pass


# ---------------------------------------------------------------- history and pending action
def load_history(store: Store) -> list[dict[str, Any]]:
    try:
        h = json.loads(store.get_setting("chat_history") or "[]")
        return [m for m in h if isinstance(m, dict) and m.get("r") in ("user", "bot", "note") and isinstance(m.get("t"), str)]
    except ValueError:
        return []


def save_history(store: Store, h: list[dict[str, Any]]) -> None:
    store.set_setting("chat_history", json.dumps(h[-HISTORY_MAX:]) if h else None)


def add_message(store: Store, role: str, text: str) -> None:
    h = load_history(store)
    h.append({"r": role, "t": text[:700], "at": store.now().isoformat()})
    save_history(store, h)


def load_pending(store: Store) -> dict[str, Any] | None:
    try:
        p = json.loads(store.get_setting("chat_pending") or "null")
        if not isinstance(p, dict):
            return None
        from datetime import datetime
        if store.now() > datetime.fromisoformat(p["at"]) + timedelta(minutes=PENDING_MINUTES):
            store.set_setting("chat_pending", None)
            return None
        return p
    except (ValueError, KeyError):
        return None


def set_pending(store: Store, action: dict[str, Any]) -> None:
    store.set_setting("chat_pending", json.dumps({**action, "id": secrets.token_urlsafe(8), "at": store.now().isoformat()}))


def clear_pending(store: Store) -> None:
    store.set_setting("chat_pending", None)


# ---------------------------------------------------------------- facts
def chat_facts(store: Store, menu: list[dict[str, Any]] | None) -> dict[str, Any]:
    f = analysis.build_facts(store)
    today = store.local(store.now()).date()
    f["weights_last_14_days"] = [{"when": w["time"], "kg": w["kg"]} for w in store.weight_readings(14)][-10:]
    eaten = [e for e in store.food_entries(1) if e["_day"] == today]
    f["food_logged_today"] = [{"name": e["name"][:60], "kcal": e["kcal"], "protein_g": e["protein_g"]} for e in eaten][-12:]
    f["food_today_total_kcal"] = sum(e["kcal"] for e in eaten)
    if menu is not None:
        f["meal_plan_today"] = [{"meal": m.get("meal"), "recipe": m.get("recipe_name") or m.get("recipe"),
                                 "kcal_per_serving": (m.get("per_serving") or {}).get("kcal")} for m in menu][:8]
    f.pop("thin", None)
    return f


# ---------------------------------------------------------------- checking an action
NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?")


def user_numbers(text: str) -> set[float]:
    out: set[float] = set()
    for m in NUMBER.findall(text):
        try:
            out.add(float(m.replace(",", ".")))
        except ValueError:
            pass
    return out


def _num(a: dict[str, Any], key: str, lo: float, hi: float, typed: set[float], required: bool = True) -> float | None:
    v = a.get(key)
    if v is None or v == "":
        if required:
            raise ValueError(f"missing {key}")
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{key} is not a number")
    if not lo <= v <= hi:
        raise ValueError(f"{key} out of range")
    if not any(abs(v - t) < 1e-9 for t in typed):
        raise ValueError(f"{key} was not typed by the person")
    return float(v)


def check_action(a: Any, user_text: str) -> dict[str, Any] | None:
    """Return a clean action (with a plain-words summary) or None for 'no action'. Raises ValueError if unusable."""
    if not isinstance(a, dict) or a.get("kind") in (None, "none", ""):
        return None
    kind = a["kind"]
    if kind not in KINDS:
        raise ValueError("unknown action")
    typed = user_numbers(user_text)
    if kind == "log_weight":
        kg = _num(a, "kg", 30, 300, typed)
        return {"kind": kind, "kg": kg, "summary": f"Save weight {kg:g} kg, now"}
    if kind == "log_bp":
        s, d = _num(a, "systolic", 70, 260, typed), _num(a, "diastolic", 40, 160, typed)
        if s <= d:
            raise ValueError("top number must be higher than bottom")
        p = _num(a, "pulse", 30, 220, typed, required=False)
        return {"kind": kind, "systolic": int(s), "diastolic": int(d), "pulse": int(p) if p else None,
                "summary": f"Save blood pressure {int(s)}/{int(d)}" + (f", pulse {int(p)}" if p else "") + ", now"}
    if kind == "log_meal":
        name = " ".join(str(a.get("name", "")).split())[:100]
        if not name:
            raise ValueError("missing meal name")
        kcal = _num(a, "kcal", 0, 5000, typed)
        prot = _num(a, "protein_g", 0, 500, typed, required=False)
        salt = _num(a, "salt_g", 0, 50, typed, required=False)
        return {"kind": kind, "name": name, "kcal": kcal, "protein_g": prot, "salt_g": salt,
                "summary": f"Save meal \"{name}\", {kcal:g} kcal"}
    if kind == "log_recipe":
        q = " ".join(str(a.get("recipe", "")).split())[:80]
        if not q:
            raise ValueError("missing recipe words")
        sv = a.get("servings") or 1
        if isinstance(sv, bool) or not isinstance(sv, (int, float)) or not 0.25 <= sv <= 10:
            raise ValueError("servings out of range")
        if a.get("servings") and not any(abs(sv - t) < 1e-9 for t in typed) and sv != 1:
            raise ValueError("servings was not typed by the person")
        return {"kind": kind, "recipe": q, "servings": float(sv), "summary": ""}
    if kind == "add_shopping":
        items = a.get("items")
        if not isinstance(items, list):
            raise ValueError("items missing")
        clean = [" ".join(str(i).split())[:80] for i in items if str(i).strip()][:15]
        if not clean:
            raise ValueError("items missing")
        return {"kind": kind, "items": clean, "summary": ""}
    raise ValueError("unknown action")


# ---------------------------------------------------------------- checking the reply
def reply_problems(reply: Any, facts: dict[str, Any], user_text: str, action: dict[str, Any] | None) -> list[str]:
    if not isinstance(reply, str) or not reply.strip():
        return ["empty reply"]
    text = " ".join(reply.split())
    errs: list[str] = []
    if len(text) > 600:
        errs.append("reply too long")
    allowed = analysis._numbers(facts, set()) | user_numbers(user_text)
    if action:
        allowed |= analysis._numbers({k: v for k, v in action.items() if k != "summary"}, set())
    clean = re.sub(r"\d{1,2}:\d{2}", " ", text)
    clean = re.sub(r"\b\d{1,2}(st|nd|rd|th)?\s+(Nov|November|Oct|October|Dec|December|Jan|January)\b", " ", clean)
    for m in analysis.NUM.findall(clean):
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
    if analysis.BANNED.search(text):
        errs.append("diagnosis or medication wording")
    bp = facts.get("blood_pressure")
    if bp:
        if re.search(r"blood pressure|\bBP\b|\d{2,3}/\d{2,3}", text, re.I) and not re.search(r"\bGP\b|doctor", text, re.I) \
                and not (action and action["kind"] == "log_bp"):
            errs.append("blood pressure without the GP")
        if bp["latest_sitting_mixed"]:
            for sent in re.split(r"(?<=[.!?])\s+", text):
                if re.search(r"blood pressure|\bBP\b|readings?", sent, re.I) and analysis.ALARM.search(sent):
                    errs.append("alarming blood-pressure wording")
    if action is None and re.search(r"\b(i('| ha)ve (saved|logged|added)|has been (saved|logged|added)|is saved|done, saved)\b", text, re.I):
        errs.append("claims to have saved")
    return sorted(set(errs))


# ---------------------------------------------------------------- asking the model
async def respond(client: httpx.AsyncClient, url: str, model: str, facts: dict[str, Any],
                  history: list[dict[str, Any]], user_text: str, attempts: int = 2) -> tuple[str, dict[str, Any] | None]:
    msgs: list[dict[str, str]] = [{"role": "system", "content": SYSTEM},
                                  {"role": "system", "content": "Facts:\n" + json.dumps(facts, indent=1)}]
    for m in history[-HISTORY_SENT:]:
        if m["r"] in ("user", "bot"):
            msgs.append({"role": "user" if m["r"] == "user" else "assistant", "content": m["t"]})
    msgs.append({"role": "user", "content": user_text})
    last = ["no answer"]
    for i in range(attempts):
        body = {"model": model, "stream": False, "format": SCHEMA, "think": False,
                "options": {"temperature": 0.2, "num_ctx": 8192, "seed": i + 1}, "messages": msgs}
        try:
            r = await client.post(url.rstrip("/") + "/api/chat", json=body)
            if r.status_code == 400 and "think" in r.text.lower():
                body.pop("think")
                r = await client.post(url.rstrip("/") + "/api/chat", json=body)
            r.raise_for_status()
            d = json.loads(r.json()["message"]["content"])
            reply, raw_action = d["reply"], d.get("action")
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
            raise ChatError(f"Could not reach the model ({type(e).__name__})") from e
        try:
            action = check_action(raw_action, user_text)
        except ValueError as e:
            last = [f"action rejected: {e}"]
            continue
        last = reply_problems(reply, facts, user_text, action)
        if not last:
            return " ".join(reply.split()), action
    raise ChatError("The model's answer failed the safety checks (" + "; ".join(last) + "). Try rewording.")


# ---------------------------------------------------------------- running a confirmed action
def execute(store: Store, mealie: Any, p: dict[str, Any]) -> str:
    """Do the saved action. Returns a short message. Raises on failure."""
    from . import mealie as mealie_mod
    k = p["kind"]
    if k == "log_weight":
        store.log_weight(p["kg"], None)
        return f"Saved weight {p['kg']:g} kg"
    if k == "log_bp":
        store.log_blood_pressure(p["systolic"], p["diastolic"], p.get("pulse"), None, None)
        return f"Saved blood pressure {p['systolic']}/{p['diastolic']}"
    if k == "log_meal":
        store.log_meal(p["name"], p["kcal"], p.get("protein_g"), None, None, None, None, p.get("salt_g"), None)
        return f"Saved {p['name']}, {p['kcal']:g} kcal"
    if mealie is None:
        raise mealie_mod.MealieError("Mealie is not connected")
    if k == "log_recipe":
        rec = mealie.recipe(p["slug"])
        res = mealie_mod.log_recipe_nutrition(store, rec, p["servings"], None)
        if not res.get("logged"):
            raise mealie_mod.MealieError(res.get("reason", "Could not log that recipe"))
        return f"Logged {rec['name']}, {res['kcal']} kcal"
    if k == "add_shopping":
        res = mealie.add_shopping_items(p["list_id"], p["items"])
        return f"Added {res['added']} item(s) to {p['list_name']}"
    raise ValueError("unknown action")


def resolve(mealie: Any, a: dict[str, Any]) -> dict[str, Any]:
    """Mealie-backed actions: find the recipe or the shopping list now, so the confirm card shows exactly what will happen."""
    from . import mealie as mealie_mod
    if a["kind"] not in ("log_recipe", "add_shopping"):
        return a
    if mealie is None:
        raise mealie_mod.MealieError("Mealie is not connected")
    if a["kind"] == "log_recipe":
        hits = mealie.search(a["recipe"], 3)
        if not hits:
            raise mealie_mod.MealieError(f'No recipe found for "{a["recipe"]}"')
        rec = mealie.recipe(hits[0]["slug"])
        kcal = (rec.get("nutrition_per_serving") or {}).get("kcal")
        if kcal is None:
            raise mealie_mod.MealieError(f'{rec["name"]} has no nutrition in Mealie, so it cannot be logged')
        sv = a["servings"]
        return {**a, "slug": rec["slug"], "recipe_name": rec["name"],
                "summary": f"Log {sv:g} serving(s) of {rec['name']}, about {round(float(kcal) * sv)} kcal"}
    lists = mealie.shopping_lists()
    if not lists:
        raise mealie_mod.MealieError("No shopping list in Mealie")
    lst = lists[0]
    return {**a, "list_id": lst["id"], "list_name": lst["name"],
            "summary": f"Add {', '.join(a['items'])} to the shopping list \"{lst['name']}\""}
