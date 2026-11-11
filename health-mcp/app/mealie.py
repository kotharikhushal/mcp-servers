"""Small read/write client for a self-hosted Mealie (https://mealie.io) and the MCP tools built on it.

The API token is a Mealie user token created under User profile > API tokens. It is sent only to the
configured MEALIE_URL and never returned by any tool. Mealie's API changed between major versions
(meal plans moved from /api/groups to /api/households in v2), so the client tries the current path and
falls back to the old one. Nutrition values are free text in Mealie ("350", "12 g"), so they are parsed
leniently and anything that cannot be read is reported as missing rather than guessed.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Mapping

import httpx

ENTRY_TYPES = ("breakfast", "lunch", "dinner", "side")
SALT_G_PER_SODIUM_MG = 2.5 / 1000
_NUM = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


class MealieError(RuntimeError):
    """Message is safe to show to the user (it never contains the token)."""


def number(v: Any) -> float | None:
    """'350', '12.5 g', 12, '1,5' -> float; anything else -> None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = _NUM.search(str(v))
    return float(m.group().replace(",", ".")) if m else None


def nutrition_per_serving(recipe: Mapping[str, Any]) -> dict[str, Any]:
    n = recipe.get("nutrition") or {}
    sodium = number(n.get("sodiumContent"))
    out = {"kcal": number(n.get("calories")), "protein_g": number(n.get("proteinContent")),
           "carbs_g": number(n.get("carbohydrateContent")), "fat_g": number(n.get("fatContent")),
           "fibre_g": number(n.get("fiberContent")), "sugar_g": number(n.get("sugarContent")),
           "salt_g": round(sodium * SALT_G_PER_SODIUM_MG, 2) if sodium is not None else None}
    return {k: v for k, v in out.items() if v is not None}


def summarise_recipe(r: Mapping[str, Any]) -> dict[str, Any]:
    return {"slug": r.get("slug"), "name": r.get("name"), "description": (r.get("description") or "")[:200] or None,
            "total_time": r.get("totalTime"), "rating": r.get("rating")}


class Mealie:
    def __init__(self, base_url: str, token: str, client: httpx.Client | None = None) -> None:
        self.base = base_url.rstrip("/")
        self._http = client or httpx.Client(timeout=15)
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _call(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = self._http.request(method, self.base + path, headers=self._headers, **kw)
        except httpx.HTTPError as e:
            raise MealieError(f"Could not reach Mealie ({type(e).__name__}). Check MEALIE_URL and that the "
                              "container can reach it.") from e
        if r.status_code in (401, 403):
            raise MealieError("Mealie refused the API token. Create a new one under User profile > API tokens.")
        if r.status_code == 404:
            raise MealieError("Not found in Mealie.")
        if r.status_code >= 400:
            raise MealieError(f"Mealie returned HTTP {r.status_code}.")
        try:
            return r.json()
        except ValueError as e:
            raise MealieError("Mealie returned something that is not JSON. Is MEALIE_URL the app address?") from e

    def _plans_path(self) -> str:
        return "/api/households/mealplans"

    def status(self) -> dict[str, Any]:
        about = self._call("GET", "/api/app/about")
        me = self._call("GET", "/api/users/self")
        return {"reachable": True, "version": about.get("version"), "signed_in_as": me.get("fullName") or me.get("username")}

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        data = self._call("GET", "/api/recipes", params={"search": query, "perPage": max(1, min(limit, 30)), "page": 1})
        return [summarise_recipe(r) for r in (data.get("items") or [])]

    def recipe(self, slug: str) -> dict[str, Any]:
        slug = slug.strip().strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
            raise MealieError("A recipe slug has only letters, numbers, dashes and underscores. Use search_recipes first.")
        r = self._call("GET", f"/api/recipes/{slug}")
        ings = []
        for i in r.get("recipeIngredient") or []:
            text = i.get("display") or i.get("note") or i.get("originalText") or ""
            if text:
                ings.append(str(text))
        nut = nutrition_per_serving(r)
        return {"id": r.get("id"), "slug": r.get("slug"), "name": r.get("name"), "servings": number(r.get("recipeServings")) or number(r.get("recipeYield")),
                "total_time": r.get("totalTime"), "ingredients": ings,
                "nutrition_per_serving": nut or None,
                "nutrition_note": None if nut else "This recipe has no nutrition filled in in Mealie, so any figures must be estimated."}

    def plan(self, days: int = 7, start: date | None = None) -> list[dict[str, Any]]:
        start = start or date.today()
        end = start + timedelta(days=max(1, min(days, 60)) - 1)
        data = self._call("GET", self._plans_path(), params={"start_date": start.isoformat(), "end_date": end.isoformat(), "perPage": 100})
        items = data.get("items") if isinstance(data, dict) else data
        out = []
        for e in items or []:
            rec = e.get("recipe") or {}
            out.append({"id": e.get("id"), "date": e.get("date"), "meal": e.get("entryType"),
                        "recipe": rec.get("name") or e.get("title"), "slug": rec.get("slug"), "note": e.get("text") or None})
        return sorted(out, key=lambda x: (x["date"] or "", ENTRY_TYPES.index(x["meal"]) if x["meal"] in ENTRY_TYPES else 9))

    def add_to_plan(self, day: date, meal: str, slug: str | None, title: str | None) -> dict[str, Any]:
        if meal not in ENTRY_TYPES:
            raise MealieError(f"meal must be one of {', '.join(ENTRY_TYPES)}.")
        body: dict[str, Any] = {"date": day.isoformat(), "entryType": meal}
        if slug:
            rec = self.recipe(slug)
            body["recipeId"] = rec["id"]
        elif title:
            body["title"] = title[:200]
        else:
            raise MealieError("Give a recipe slug or a title.")
        e = self._call("POST", self._plans_path(), json=body)
        return {"id": e.get("id"), "date": e.get("date"), "meal": e.get("entryType")}


def register(mcp: Any, store: Any, mealie: Mealie, parse_when: Any, read: Any, write: Any) -> None:
    """Adds the Mealie tools. Errors become plain messages instead of stack traces."""
    def guard(fn: Any) -> dict[str, Any]:
        try:
            return fn()
        except MealieError as e:
            return {"error": str(e)}

    @mcp.tool(annotations=read)
    def mealie_status() -> dict[str, Any]:
        """Check the connection to the user's Mealie (version and which account the token belongs to)."""
        return guard(mealie.status)

    @mcp.tool(annotations=read)
    def search_recipes(query: str, limit: int = 10) -> dict[str, Any]:
        """Search the user's Mealie recipes by name or ingredient. Returns slugs to pass to get_recipe."""
        return guard(lambda: {"recipes": mealie.search(query, limit)})

    @mcp.tool(annotations=read)
    def get_recipe(slug: str) -> dict[str, Any]:
        """One Mealie recipe: ingredients, servings and nutrition per serving (kcal, protein, carbs, fat,
        fibre, sugar, salt in grams) when the recipe has nutrition filled in."""
        return guard(lambda: mealie.recipe(slug))

    @mcp.tool(annotations=read)
    def get_meal_plan(days: int = 7, start: str | None = None) -> dict[str, Any]:
        """The user's Mealie meal plan from `start` (ISO date, default today) for `days` days."""
        def run() -> dict[str, Any]:
            s = date.fromisoformat(start) if start else store.local(store.now()).date()
            return {"plan": mealie.plan(days, s)}
        try:
            return guard(run)
        except ValueError:
            return {"error": "start must be an ISO date such as 2026-11-12"}

    @mcp.tool(annotations=write)
    def add_to_meal_plan(date_iso: str, meal: str, recipe_slug: str | None = None, title: str | None = None) -> dict[str, Any]:
        """Add a recipe (or a free-text title) to the user's Mealie meal plan. meal is breakfast, lunch,
        dinner or side. Only add what the user asked for."""
        try:
            d = date.fromisoformat(date_iso)
        except ValueError:
            return {"error": "date_iso must be an ISO date such as 2026-11-12"}
        return guard(lambda: mealie.add_to_plan(d, meal, recipe_slug, title))

    @mcp.tool(annotations=write)
    def log_recipe_as_meal(slug: str, servings: float = 1.0, when: str | None = None) -> dict[str, Any]:
        """Log what the user ate from a Mealie recipe to the food log, using the recipe's own nutrition
        multiplied by the servings they actually had. If the recipe has no nutrition, nothing is logged:
        estimate it yourself and use log_meal instead."""
        if not 0 < servings <= 20:
            return {"error": "servings must be more than 0 and at most 20"}
        try:
            rec = mealie.recipe(slug)
        except MealieError as e:
            return {"error": str(e)}
        n = rec.get("nutrition_per_serving") or {}
        if "kcal" not in n:
            return {"logged": False, "reason": "This recipe has no calories filled in in Mealie. Estimate and use log_meal."}

        def scaled(k: str) -> float | None:
            return round(n[k] * servings, 2) if k in n else None
        name = f'{rec["name"]} x{servings:g} (from Mealie)'
        saved = store.log_meal(name, round(n["kcal"] * servings), scaled("protein_g"), scaled("carbs_g"), scaled("fat_g"),
                               scaled("fibre_g"), scaled("sugar_g"), scaled("salt_g"), parse_when(store, when))
        return {"logged": True, **saved}
