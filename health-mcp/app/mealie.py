"""Small read/write client for a self-hosted Mealie (https://mealie.io) and the MCP tools built on it.

The API token is a Mealie user token created under User profile > API tokens. It is sent only to the
configured MEALIE_URL and never returned by any tool. Mealie's API changed between major versions
(meal plans moved from /api/groups to /api/households in v2), so the client tries the current path and
falls back to the old one. Nutrition values are free text in Mealie ("350", "12 g"), so they are parsed
leniently and anything that cannot be read is reported as missing rather than guessed.
"""
from __future__ import annotations

import re
import time
import uuid
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


def tag_names(r: Mapping[str, Any]) -> list[str]:
    return [str(t.get("name")) for t in (r.get("tags") or []) if t.get("name")]


def item_text(i: Mapping[str, Any]) -> str:
    return str(i.get("display") or i.get("note") or (i.get("food") or {}).get("name") or "").strip()


def norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def safe_id(v: Any, what: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(v)):
        raise MealieError(f"Bad {what}.")
    return str(v)


def summarise_recipe(r: Mapping[str, Any]) -> dict[str, Any]:
    return {"slug": r.get("slug"), "name": r.get("name"), "description": (r.get("description") or "")[:200] or None,
            "total_time": r.get("totalTime"), "rating": r.get("rating"), "tags": tag_names(r)}


def log_recipe_nutrition(store: Any, rec: Mapping[str, Any], servings: float, when: Any) -> dict[str, Any]:
    """Write one recipe's own nutrition (times servings) to the food log. rec comes from Mealie.recipe."""
    n = rec.get("nutrition_per_serving") or {}
    if "kcal" not in n:
        return {"logged": False, "reason": "This recipe has no calories filled in in Mealie. Estimate and use log_meal."}

    def scaled(k: str) -> float | None:
        return round(n[k] * servings, 2) if k in n else None
    name = f'{rec["name"]} x{servings:g} (from Mealie)'
    saved = store.log_meal(name, round(n["kcal"] * servings), scaled("protein_g"), scaled("carbs_g"), scaled("fat_g"),
                           scaled("fibre_g"), scaled("sugar_g"), scaled("salt_g"), when)
    return {"logged": True, **saved}


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
        if not r.content:
            return {}
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

    def search(self, query: str, limit: int = 10, tag: str | None = None) -> list[dict[str, Any]]:
        if tag:
            want, q = tag.strip().lower(), query.strip().lower()
            hits = [r for r in self.all_recipes() if want in [t.lower() for t in tag_names(r)]
                    and (not q or q in str(r.get("name", "")).lower())]
            return [summarise_recipe(r) for r in hits[:max(1, min(limit, 30))]]
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
                "total_time": r.get("totalTime"), "tags": tag_names(r), "ingredients": ings,
                "nutrition_per_serving": nut or None,
                "nutrition_note": None if nut else "This recipe has no nutrition filled in in Mealie, so any figures must be estimated."}

    def all_recipes(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, 21):
            data = self._call("GET", "/api/recipes", params={"page": page, "perPage": 100})
            items = data.get("items") or []
            out.extend(items)
            if len(items) < 100 or page >= (data.get("total_pages") or page):
                break
        return out

    def needing_nutrition(self, limit: int = 10, skip: int = 0) -> dict[str, Any]:
        """Recipes with no calories filled in, with what is needed to estimate them."""
        everything = self.all_recipes()
        todo, checked = [], 0
        for r in everything:
            slug = r.get("slug")
            if not slug:
                continue
            checked += 1
            full = self.recipe(slug)
            if "kcal" not in (full.get("nutrition_per_serving") or {}):
                todo.append(full)
        page = todo[max(skip, 0):max(skip, 0) + max(1, min(limit, 25))]
        return {"recipes_total": checked, "missing_nutrition": len(todo), "shown": len(page),
                "recipes": [{k: r[k] for k in ("slug", "name", "servings", "ingredients")} for r in page]}

    def set_nutrition(self, slug: str, n: Mapping[str, float | None]) -> dict[str, Any]:
        """Write per-serving nutrition (kcal, protein_g, carbs_g, fat_g, fibre_g, sugar_g, salt_g) to a recipe,
        then read it back so a silent failure is reported instead of assumed."""
        if not n.get("kcal") or not 0 < n["kcal"] <= 3000:
            raise MealieError("kcal per serving must be between 1 and 3000.")
        for k in ("protein_g", "carbs_g", "fat_g", "fibre_g", "sugar_g", "salt_g"):
            v = n.get(k)
            if v is not None and not 0 <= v <= 500:
                raise MealieError(f"{k} per serving looks wrong ({v}).")
        body: dict[str, str] = {"calories": f"{n['kcal']:g}"}
        for key, field in (("protein_g", "proteinContent"), ("carbs_g", "carbohydrateContent"), ("fat_g", "fatContent"),
                           ("fibre_g", "fiberContent"), ("sugar_g", "sugarContent")):
            if n.get(key) is not None:
                body[field] = f"{n[key]:g}"
        if n.get("salt_g") is not None:
            body["sodiumContent"] = f"{n['salt_g'] / SALT_G_PER_SODIUM_MG:.0f}"
        self.recipe(slug)  # validates the slug and that it exists
        self._call("PATCH", f"/api/recipes/{slug.strip().strip('/')}", json={"nutrition": body})
        back = self.recipe(slug)["nutrition_per_serving"] or {}
        if back.get("kcal") != float(n["kcal"]):
            raise MealieError("Mealie accepted the request but the nutrition did not save. Check the Mealie version.")
        return {"slug": slug, "saved": back}

    def delete_plan_entry(self, entry_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", str(entry_id)):
            raise MealieError("Bad meal plan entry id.")
        self._call("DELETE", f"{self._plans_path()}/{entry_id}")
        return {"deleted": True}

    def shopping_lists(self, with_items: bool = False) -> list[dict[str, Any]]:
        """The list endpoint returns summaries without items, so each list is fetched by id for the real count."""
        data = self._call("GET", "/api/households/shopping/lists", params={"perPage": 50})
        out = []
        for x in data.get("items") or []:
            lid = str(x.get("id") or "")
            full = self._call("GET", f"/api/households/shopping/lists/{lid}") if re.fullmatch(r"[A-Za-z0-9_-]+", lid) else x
            items = full.get("listItems") or []
            open_items = [i for i in items if not i.get("checked")]
            row: dict[str, Any] = {"id": lid, "name": x.get("name"), "items": len(items), "unchecked": len(open_items)}
            if with_items:
                row["unchecked_items"] = [{"id": i.get("id"), "text": item_text(i)} for i in open_items][:200]
            out.append(row)
        return out

    def recipes_without_ingredients(self) -> list[dict[str, Any]]:
        out = []
        for r in self.all_recipes():
            slug = r.get("slug")
            if slug and not self.recipe(slug)["ingredients"]:
                out.append({"slug": slug, "name": r.get("name")})
        return out

    def delete_recipe(self, slug: str, allow_with_ingredients: bool = False) -> dict[str, Any]:
        """Deletes a recipe only if it has no ingredients, unless the caller was told by the user to delete this exact one."""
        full = self.recipe(slug)
        if full["ingredients"] and not allow_with_ingredients:
            raise MealieError("This recipe has ingredients, so it was not deleted. Delete it in Mealie yourself if you want it gone.")
        self._call("DELETE", f"/api/recipes/{full['slug']}")
        return {"slug": full["slug"], "deleted": True}

    def add_recipes_to_list(self, list_id: str, slugs: Mapping[str, float]) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", str(list_id)):
            raise MealieError("Bad shopping list id.")
        body = [{"recipeId": self.recipe(slug)["id"], "recipeIncrementQuantity": q} for slug, q in slugs.items()]
        self._call("POST", f"/api/households/shopping/lists/{list_id}/recipe", json=body)
        return {"added_recipes": list(slugs)}

    # ----- shopping lists -----
    _LISTS = "/api/households/shopping/lists"
    _ITEMS = "/api/households/shopping/items"

    def _list_items(self, list_id: str) -> list[dict[str, Any]]:
        return self._call("GET", f"{self._LISTS}/{safe_id(list_id, 'shopping list id')}").get("listItems") or []

    def create_shopping_list(self, name: str) -> dict[str, Any]:
        name = name.strip()[:100]
        if not name:
            raise MealieError("Give the list a name.")
        made = self._call("POST", self._LISTS, json={"name": name})
        return {"id": made.get("id"), "name": made.get("name") or name}

    def delete_shopping_list(self, list_id: str, confirm_name: str) -> dict[str, Any]:
        lid = safe_id(list_id, "shopping list id")
        got = self._call("GET", f"{self._LISTS}/{lid}")
        if str(got.get("name", "")).strip().lower() != confirm_name.strip().lower():
            raise MealieError(f'Not deleted: confirm_name must be exactly the list name ("{got.get("name")}").')
        self._call("DELETE", f"{self._LISTS}/{lid}")
        return {"deleted": got.get("name")}

    def add_shopping_items(self, list_id: str, items: list[Any]) -> dict[str, Any]:
        lid = safe_id(list_id, "shopping list id")
        body = []
        for it in items:
            text, qty = (it.get("text"), it.get("quantity")) if isinstance(it, Mapping) else (it, None)
            text = str(text or "").strip()[:200]
            if text:
                body.append({"shoppingListId": lid, "note": text, "quantity": float(qty) if qty else 1.0, "checked": False})
        if not body:
            raise MealieError("No item text given.")
        before = len(self._list_items(lid))
        self._call("POST", f"{self._ITEMS}/create-bulk", json=body)
        after = len(self._list_items(lid))
        if after < before + len(body):
            raise MealieError(f"Mealie accepted the request but only {after - before} of {len(body)} items appear in the list.")
        return {"added": len(body), "items_now": after}

    def change_shopping_items(self, list_id: str, action: str, ids: list[str], match: list[str]) -> dict[str, Any]:
        """check / uncheck / remove items picked by id or by a case-insensitive words match; read back to confirm."""
        if action not in ("check", "uncheck", "remove"):
            raise MealieError("action must be check, uncheck or remove.")
        lid = safe_id(list_id, "shopping list id")
        wanted = {safe_id(i, "item id") for i in ids}
        words = [m.strip().lower() for m in match if m.strip()]
        if not wanted and not words:
            raise MealieError("Give item ids or words to match.")
        pick = [i for i in self._list_items(lid) if i.get("id") in wanted or any(w in item_text(i).lower() for w in words)]
        if not pick:
            return {"changed": 0, "items": []}
        pick_ids = [str(i["id"]) for i in pick]
        if action == "remove":
            self._call("DELETE", self._ITEMS, params={"ids": pick_ids})
            left = {i.get("id") for i in self._list_items(lid)}
            if left & set(pick_ids):
                raise MealieError("Mealie accepted the request but some items are still in the list.")
        else:
            flag = action == "check"
            self._call("PUT", self._ITEMS, json=[{**i, "checked": flag} for i in pick])
            now = {i.get("id"): bool(i.get("checked")) for i in self._list_items(lid)}
            if any(now.get(i) != flag for i in pick_ids):
                raise MealieError("Mealie accepted the request but the items did not change.")
        return {"changed": len(pick), "items": [item_text(i) for i in pick]}

    def clear_checked_items(self, list_id: str) -> dict[str, Any]:
        lid = safe_id(list_id, "shopping list id")
        ids = [str(i["id"]) for i in self._list_items(lid) if i.get("checked")]
        if ids:
            self._call("DELETE", self._ITEMS, params={"ids": ids})
        return {"removed": len(ids)}

    # ----- recipes -----
    def _ingredient(self, text: str) -> dict[str, Any]:
        return {"referenceId": str(uuid.uuid4()), "note": text, "quantity": 0, "unit": None, "food": None,
                "title": None, "originalText": None, "disableAmount": True}

    def update_recipe(self, slug: str, name: str | None = None, servings: float | None = None,
                      ingredients: list[str] | None = None, instructions: list[str] | None = None) -> dict[str, Any]:
        slug = safe_id(slug.strip().strip("/"), "recipe slug")
        body: dict[str, Any] = {}
        if name:
            body["name"] = name.strip()[:200]
        if servings is not None:
            if not 0 < servings <= 100:
                raise MealieError("servings must be more than 0 and at most 100.")
            body["recipeServings"] = servings
            body["recipeYield"] = f"{servings:g} servings"
        if ingredients is not None:
            lines = [str(x).strip()[:300] for x in ingredients if str(x).strip()]
            if not 0 < len(lines) <= 80:
                raise MealieError("Send between 1 and 80 ingredient lines.")
            body["recipeIngredient"] = [self._ingredient(x) for x in lines]
        if instructions is not None:
            steps = [str(x).strip()[:2000] for x in instructions if str(x).strip()]
            if not 0 < len(steps) <= 50:
                raise MealieError("Send between 1 and 50 instruction steps.")
            body["recipeInstructions"] = [{"id": str(uuid.uuid4()), "title": "", "text": x, "ingredientReferences": []} for x in steps]
        if not body:
            raise MealieError("Nothing to change.")
        self.recipe(slug)
        self._call("PATCH", f"/api/recipes/{slug}", json=body)
        back = self._call("GET", f"/api/recipes/{slug}")
        if ingredients is not None and len(back.get("recipeIngredient") or []) != len(body["recipeIngredient"]):
            raise MealieError("Mealie accepted the request but the ingredients did not save.")
        if instructions is not None and len(back.get("recipeInstructions") or []) != len(body["recipeInstructions"]):
            raise MealieError("Mealie accepted the request but the instructions did not save.")
        if servings is not None and number(back.get("recipeServings")) != float(servings):
            raise MealieError("Mealie accepted the request but the servings did not save.")
        return {"slug": slug, "updated": sorted(body)}

    def create_recipe(self, name: str, ingredients: list[str], instructions: list[str] | None, servings: float | None,
                      tags: list[str] | None) -> dict[str, Any]:
        name = name.strip()[:200]
        if not name:
            raise MealieError("Give the recipe a name.")
        made = self._call("POST", "/api/recipes", json={"name": name})
        slug = made if isinstance(made, str) else None
        if not slug:
            raise MealieError("Mealie did not return the new recipe's slug.")
        try:
            self.update_recipe(slug, servings=servings, ingredients=ingredients, instructions=instructions)
            if tags:
                self.set_tags([slug], tags, replace=False)
        except MealieError as e:
            raise MealieError(f"The recipe was created as '{slug}' but filling it in failed: {e}") from e
        return {"slug": slug, "name": name, "created": True}

    def import_recipe(self, url: str) -> dict[str, Any]:
        if not re.fullmatch(r"https?://[^\s]+", url.strip()):
            raise MealieError("url must start with http:// or https://")
        slug = self._call("POST", "/api/recipes/create/url", json={"url": url.strip(), "includeTags": False}, timeout=60)
        if not isinstance(slug, str) or not slug:
            raise MealieError("Mealie could not import that page.")
        full = self.recipe(slug)
        return {"slug": slug, "name": full["name"], "ingredients": len(full["ingredients"]),
                "note": None if full["ingredients"] else "Imported but no ingredients were found. Add them with update_recipe or delete it."}

    def set_tags(self, slugs: list[str], names: list[str], replace: bool = False) -> list[dict[str, Any]]:
        names = [n.strip()[:50] for n in names if n.strip()]
        if not names:
            raise MealieError("Give at least one tag.")
        have = {str(t.get("name", "")).lower(): t for t in
                (self._call("GET", "/api/organizers/tags", params={"perPage": 200}).get("items") or [])}
        wanted = []
        for n in names:
            t = have.get(n.lower()) or self._call("POST", "/api/organizers/tags", json={"name": n})
            have[n.lower()] = t
            wanted.append({"id": t.get("id"), "name": t.get("name"), "slug": t.get("slug")})
        out = []
        for slug in slugs:
            slug = safe_id(slug.strip().strip("/"), "recipe slug")
            cur = self._call("GET", f"/api/recipes/{slug}").get("tags") or []
            merged = list(wanted) if replace else [*({t["id"]: t for t in cur}.values()), *[w for w in wanted if w["id"] not in {t["id"] for t in cur}]]
            self._call("PATCH", f"/api/recipes/{slug}", json={"tags": merged})
            back = tag_names(self._call("GET", f"/api/recipes/{slug}"))
            if not all(n.lower() in [b.lower() for b in back] for n in names):
                raise MealieError(f"Mealie accepted the request but the tags did not save on {slug}.")
            out.append({"slug": slug, "tags": back})
        return out

    def find_duplicates(self) -> dict[str, Any]:
        everything = [r for r in self.all_recipes() if r.get("slug")]
        by_name: dict[str, list[dict[str, Any]]] = {}
        for r in everything:
            by_name.setdefault(norm_name(str(r.get("name", ""))) or str(r["slug"]), []).append(r)
            by_name.setdefault("slug:" + re.sub(r"-\d+$", "", str(r["slug"])), []).append(r)
        seen: set[tuple[str, ...]] = set()
        same_name = []
        for g in by_name.values():
            key = tuple(sorted({str(x["slug"]) for x in g}))
            if len(key) > 1 and key not in seen:
                seen.add(key)
                same_name.append([{"slug": x["slug"], "name": x.get("name")} for x in g if x["slug"] in key])
        sig: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for r in everything:
            full = self.recipe(str(r["slug"]))
            if full["ingredients"]:
                sig.setdefault(tuple(sorted(i.lower() for i in full["ingredients"])), []).append(
                    {"slug": full["slug"], "name": full["name"]})
        return {"same_name": same_name, "same_ingredients": [g for g in sig.values() if len(g) > 1]}

    def find_by_nutrition(self, max_kcal: float | None, min_protein_g: float | None, max_salt_g: float | None,
                          tag: str | None, query: str | None, limit: int) -> list[dict[str, Any]]:
        want, q, out = (tag or "").strip().lower(), (query or "").strip().lower(), []
        for r in self.all_recipes():
            if not r.get("slug") or (want and want not in [t.lower() for t in tag_names(r)]) \
                    or (q and q not in str(r.get("name", "")).lower()):
                continue
            full = self.recipe(str(r["slug"]))
            n = full.get("nutrition_per_serving") or {}
            if "kcal" not in n:
                continue
            if (max_kcal is not None and n["kcal"] > max_kcal) or (min_protein_g is not None and n.get("protein_g", 0) < min_protein_g) \
                    or (max_salt_g is not None and n.get("salt_g", 0) > max_salt_g):
                continue
            out.append({"slug": full["slug"], "name": full["name"], "tags": full["tags"], "per_serving": n})
        return sorted(out, key=lambda x: -x["per_serving"].get("protein_g", 0))[:max(1, min(limit, 30))]

    # ----- meal plan extras -----
    def plan_nutrition(self, days: int, start: date) -> dict[str, Any]:
        cache: dict[str, dict[str, Any]] = {}
        per_day: dict[str, dict[str, Any]] = {}
        for e in self.plan(days, start):
            d = per_day.setdefault(str(e["date"]), {"date": e["date"], "meals": [], "unknown": [], "_t": {}})
            n: dict[str, Any] = {}
            if e["slug"]:
                n = cache.setdefault(e["slug"], self.recipe(e["slug"]).get("nutrition_per_serving") or {})
            d["meals"].append({"meal": e["meal"], "recipe": e["recipe"], "per_serving": n or None})
            if not n:
                d["unknown"].append(e["recipe"] or "free-text entry")
            for k, v in n.items():
                d["_t"][k] = d["_t"].get(k, 0) + v
        out = []
        for k in sorted(per_day):
            d = per_day[k]
            d["totals_for_one_serving_each"] = {a: round(b, 1) for a, b in d.pop("_t").items()}
            out.append(d)
        return {"days": out, "note": "Assumes one serving of every planned meal. Entries without nutrition are listed under unknown and not counted."}

    def move_plan_entry(self, entry_id: str, day: date, meal: str | None) -> dict[str, Any]:
        eid = safe_id(entry_id, "meal plan entry id")
        if meal is not None and meal not in ENTRY_TYPES:
            raise MealieError(f"meal must be one of {', '.join(ENTRY_TYPES)}.")
        cur = self._call("GET", f"{self._plans_path()}/{eid}")
        body = {k: cur.get(k) for k in ("id", "groupId", "userId", "householdId", "title", "text", "recipeId") if k in cur}
        body["date"], body["entryType"] = day.isoformat(), meal or cur.get("entryType")
        self._call("PUT", f"{self._plans_path()}/{eid}", json=body)
        back = self._call("GET", f"{self._plans_path()}/{eid}")
        if str(back.get("date"))[:10] != day.isoformat():
            raise MealieError("Mealie accepted the request but the entry did not move.")
        return {"id": eid, "date": day.isoformat(), "meal": back.get("entryType")}

    def clear_plan(self, start: date, end: date, meals: list[str] | None) -> dict[str, Any]:
        if end < start or (end - start).days > 59:
            raise MealieError("Use a range of at most 60 days.")
        removed = []
        for e in self.plan((end - start).days + 1, start):
            if meals and e["meal"] not in meals:
                continue
            self.delete_plan_entry(str(e["id"]))
            removed.append({"date": e["date"], "meal": e["meal"], "recipe": e["recipe"]})
        return {"removed": len(removed), "entries": removed}

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

    def swap_options(self, target_kcal: float | None, query: str = "", exclude_slug: str | None = None,
                     limit: int = 12) -> list[dict[str, Any]]:
        """Recipes with calories filled in, closest to the planned meal's calories first."""
        q = query.strip().lower()
        cache = self.__dict__.setdefault("_swap_cache", {})
        now = time.monotonic()
        out = []
        for r in self.all_recipes():
            slug = r.get("slug")
            if not slug or slug == exclude_slug or (q and q not in str(r.get("name", "")).lower()):
                continue
            hit = cache.get(slug)
            if not hit or now - hit[0] > 600:
                hit = (now, self.recipe(str(slug)))
                cache[slug] = hit
            n = hit[1].get("nutrition_per_serving") or {}
            if "kcal" in n:
                out.append({"slug": slug, "name": hit[1]["name"], "per_serving": n})
        out.sort(key=lambda x: abs(x["per_serving"]["kcal"] - target_kcal) if target_kcal else x["name"])
        return out[:max(1, min(limit, 30))]

    def swap_plan_entry(self, entry_id: str, slug: str) -> dict[str, Any]:
        """Replace the recipe on a meal plan entry, keeping its date and meal slot."""
        eid = safe_id(entry_id, "meal plan entry id")
        rec = self.recipe(slug)
        cur = self._call("GET", f"{self._plans_path()}/{eid}")
        body = {k: cur.get(k) for k in ("id", "groupId", "userId", "householdId", "date", "entryType") if k in cur}
        body.update({"recipeId": rec["id"], "title": "", "text": ""})
        self._call("PUT", f"{self._plans_path()}/{eid}", json=body)
        back = self._call("GET", f"{self._plans_path()}/{eid}")
        if str(back.get("recipeId") or (back.get("recipe") or {}).get("id")) != str(rec["id"]):
            raise MealieError("Mealie accepted the request but the meal was not changed.")
        return {"id": eid, "date": str(back.get("date"))[:10], "meal": back.get("entryType"), "recipe": rec["name"]}

    def day_menu(self, day: date) -> list[dict[str, Any]]:
        """What is planned for one day, with each recipe's nutrition per serving (for the wellness app)."""
        out, cache = [], {}
        for e in self.plan(1, day):
            if not e["slug"]:
                out.append({**e, "per_serving": None})
                continue
            rec = cache.setdefault(e["slug"], self.recipe(e["slug"]))
            out.append({**e, "recipe_name": rec["name"], "per_serving": rec.get("nutrition_per_serving") or None,
                        "_rec": rec})
        return out

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


def register(mcp: Any, store: Any, mealie: Mealie, parse_when: Any, read: Any, write: Any, delete: Any,
             personal: Any = lambda f: f) -> None:
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
    def search_recipes(query: str = "", limit: int = 10, tag: str | None = None) -> dict[str, Any]:
        """Search the user's Mealie recipes by name or ingredient, optionally only those with a tag
        (for example breakfast). Returns slugs to pass to get_recipe."""
        return guard(lambda: {"recipes": mealie.search(query, limit, tag)})

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

    @mcp.tool(annotations=read)
    def recipes_missing_nutrition(limit: int = 10, skip: int = 0) -> dict[str, Any]:
        """Mealie recipes with no calories filled in, with servings and ingredients so the nutrition can be
        estimated. Pass `skip` to page. Follow with set_recipes_nutrition."""
        return guard(lambda: mealie.needing_nutrition(limit, skip))

    @mcp.tool(annotations=write)
    def set_recipes_nutrition(recipes: list[dict[str, Any]]) -> dict[str, Any]:
        """Save estimated nutrition PER SERVING onto Mealie recipes. Each entry: slug, kcal, protein_g, carbs_g,
        fat_g, fibre_g, sugar_g, salt_g (all numbers; only slug and kcal are required). Up to 25 at once.
        These are estimates from the ingredient list: tell the user so, and show them before saving."""
        if not 0 < len(recipes) <= 25:
            return {"error": "Send between 1 and 25 recipes at a time."}
        results = []
        for r in recipes:
            slug = str(r.get("slug", ""))
            try:
                fields = {k: (float(r[k]) if r.get(k) is not None else None) for k in
                          ("kcal", "protein_g", "carbs_g", "fat_g", "fibre_g", "sugar_g", "salt_g")}
                results.append(mealie.set_nutrition(slug, fields))
            except (MealieError, TypeError, ValueError) as e:
                results.append({"slug": slug, "error": str(e) if isinstance(e, MealieError) else "numbers only"})
        return {"results": results}

    @mcp.tool(annotations=write)
    def add_meals_to_plan(entries: list[dict[str, Any]]) -> dict[str, Any]:
        """Add several entries to the Mealie meal plan in one call. Each: date_iso, meal (breakfast, lunch, dinner
        or side) and recipe_slug or title. Up to 30. Only add what the user agreed to."""
        if not 0 < len(entries) <= 30:
            return {"error": "Send between 1 and 30 entries at a time."}
        results = []
        for e in entries:
            try:
                d = date.fromisoformat(str(e.get("date_iso")))
                results.append(mealie.add_to_plan(d, str(e.get("meal")), e.get("recipe_slug"), e.get("title")))
            except ValueError:
                results.append({"error": "bad date_iso", "entry": str(e)[:80]})
            except MealieError as err:
                results.append({"error": str(err), "entry": str(e)[:80]})
        return {"results": results}

    @mcp.tool(annotations=delete)
    def remove_meal_plan_entry(entry_id: str) -> dict[str, Any]:
        """Remove one entry from the Mealie meal plan (ids come from get_meal_plan). Confirm with the user first."""
        return guard(lambda: mealie.delete_plan_entry(entry_id))

    @mcp.tool(annotations=read)
    def get_shopping_lists(include_items: bool = False) -> dict[str, Any]:
        """The user's Mealie shopping lists (id, name, item counts). include_items=true also lists the unchecked
        items with their ids (use them with update_shopping_items)."""
        return guard(lambda: {"lists": mealie.shopping_lists(include_items)})

    @mcp.tool(annotations=read)
    def recipes_without_ingredients() -> dict[str, Any]:
        """Mealie recipes that have no ingredients (usually broken imports). Show them to the user before deleting."""
        return guard(lambda: {"recipes": mealie.recipes_without_ingredients()})

    @mcp.tool(annotations=delete)
    def delete_recipes(slugs: list[str], allow_with_ingredients: bool = False) -> dict[str, Any]:
        """Delete Mealie recipes (up to 20). By default only recipes with NO ingredients are deleted and the
        rest are refused. Set allow_with_ingredients only for exact recipes the user told you to delete,
        for example duplicates. Only after the user agreed to the exact list."""
        if not 0 < len(slugs) <= 20:
            return {"error": "Send between 1 and 20 slugs."}
        results = []
        for s in slugs:
            try:
                results.append(mealie.delete_recipe(str(s), allow_with_ingredients))
            except MealieError as e:
                results.append({"slug": str(s), "error": str(e)})
        return {"results": results}

    @mcp.tool(annotations=write)
    def add_recipes_to_shopping_list(list_id: str, recipe_slugs: list[str], servings_multiplier: float = 1.0) -> dict[str, Any]:
        """Add the ingredients of Mealie recipes to a shopping list (Mealie merges and scales them).
        servings_multiplier 2 means double quantities."""
        if not 0 < servings_multiplier <= 10 or not 0 < len(recipe_slugs) <= 30:
            return {"error": "Use 1 to 30 recipes and a multiplier above 0 and at most 10."}
        return guard(lambda: mealie.add_recipes_to_list(list_id, {s: servings_multiplier for s in recipe_slugs}))

    def log_from_recipe(slug: str, servings: float, when: str | None) -> dict[str, Any]:
        if not 0 < servings <= 20:
            return {"error": "servings must be more than 0 and at most 20"}
        try:
            rec = mealie.recipe(slug)
        except MealieError as e:
            return {"error": str(e)}
        return log_recipe_nutrition(store, rec, servings, parse_when(store, when))

    @mcp.tool(annotations=write)
    @personal
    def log_recipe_as_meal(slug: str, servings: float = 1.0, when: str | None = None) -> dict[str, Any]:
        """Log what the user ate from a Mealie recipe to the food log, using the recipe's own nutrition
        multiplied by the servings they actually had. If the recipe has no nutrition, nothing is logged:
        estimate it yourself and use log_meal instead."""
        return log_from_recipe(slug, servings, when)

    @mcp.tool(annotations=write)
    @personal
    def log_planned_meal(date_iso: str, meal: str, servings: float = 1.0, when: str | None = None) -> dict[str, Any]:
        """Log a meal from the Mealie meal plan as eaten (looks up the recipe planned for that date and meal).
        Use only when the user says they ate it as planned; `when` is the time they ate it."""
        try:
            d = date.fromisoformat(date_iso)
        except ValueError:
            return {"error": "date_iso must be an ISO date such as 2026-11-12"}
        try:
            found = [e for e in mealie.plan(1, d) if e["meal"] == meal and e["slug"]]
        except MealieError as e:
            return {"error": str(e)}
        if len(found) != 1:
            return {"error": f"Expected one planned recipe for {meal} on {date_iso}, found {len(found)}.",
                    "found": [e["recipe"] for e in found]}
        return {"recipe": found[0]["recipe"], **log_from_recipe(found[0]["slug"], servings, when)}

    # ----- shopping list management -----
    @mcp.tool(annotations=write)
    def create_shopping_list(name: str) -> dict[str, Any]:
        """Create a new, empty Mealie shopping list."""
        return guard(lambda: mealie.create_shopping_list(name))

    @mcp.tool(annotations=delete)
    def delete_shopping_list(list_id: str, confirm_name: str) -> dict[str, Any]:
        """Delete a whole shopping list. confirm_name must be the list's exact name. Only when the user asked."""
        return guard(lambda: mealie.delete_shopping_list(list_id, confirm_name))

    @mcp.tool(annotations=write)
    def add_shopping_items(list_id: str, items: list[Any]) -> dict[str, Any]:
        """Add free-text items to a shopping list (up to 50). Each item is a string like '2 lemons' or an object
        {text, quantity}. Verified by reading the list back."""
        if not 0 < len(items) <= 50:
            return {"error": "Send between 1 and 50 items."}
        return guard(lambda: mealie.add_shopping_items(list_id, items))

    @mcp.tool(annotations=delete)
    def update_shopping_items(list_id: str, action: str, item_ids: list[str] | None = None,
                              match: list[str] | None = None) -> dict[str, Any]:
        """Tick off (check), untick (uncheck) or remove items on a shopping list. Pick items by id (from
        get_shopping_lists with include_items) or by words that appear in the item (for example ['salt','oil']
        for pantry staples). action is check, uncheck or remove. Remove only when the user asked."""
        return guard(lambda: mealie.change_shopping_items(list_id, action, item_ids or [], match or []))

    @mcp.tool(annotations=delete)
    def clear_checked_shopping_items(list_id: str) -> dict[str, Any]:
        """Remove all ticked-off items from a shopping list."""
        return guard(lambda: mealie.clear_checked_items(list_id))

    # ----- recipes -----
    @mcp.tool(annotations=write)
    def create_recipe(name: str, ingredients: list[str], instructions: list[str] | None = None,
                      servings: float | None = None, tags: list[str] | None = None) -> dict[str, Any]:
        """Create a Mealie recipe from text the user gave (one string per ingredient line and per step). Use for
        recipes the user pastes or dictates, then estimate nutrition with set_recipes_nutrition."""
        return guard(lambda: mealie.create_recipe(name, ingredients, instructions, servings, tags))

    @mcp.tool(annotations=write)
    def update_recipe(slug: str, name: str | None = None, servings: float | None = None,
                      ingredients: list[str] | None = None, instructions: list[str] | None = None) -> dict[str, Any]:
        """Change a Mealie recipe's name, servings, ingredients or instructions. Ingredients and instructions
        REPLACE the existing lists, so send the full new list. Verified by reading the recipe back."""
        return guard(lambda: mealie.update_recipe(slug, name, servings, ingredients, instructions))

    @mcp.tool(annotations=write)
    def import_recipe_from_url(url: str) -> dict[str, Any]:
        """Import a recipe from a web page into Mealie. Check the result has ingredients."""
        return guard(lambda: mealie.import_recipe(url))

    @mcp.tool(annotations=write)
    def set_recipe_tags(slugs: list[str], tags: list[str], replace: bool = False) -> dict[str, Any]:
        """Tag Mealie recipes (for example breakfast, lunch, dinner, snack, high-protein), up to 30 at once.
        Tags are created if they do not exist. replace=true swaps the recipe's tags instead of adding."""
        if not 0 < len(slugs) <= 30:
            return {"error": "Send between 1 and 30 slugs."}
        return guard(lambda: {"results": mealie.set_tags(slugs, tags, replace)})

    @mcp.tool(annotations=read)
    def find_duplicate_recipes() -> dict[str, Any]:
        """Mealie recipes that look like duplicates: same name, or identical ingredient lists. Show them to the
        user and let them choose which to keep before deleting any."""
        return guard(mealie.find_duplicates)

    @mcp.tool(annotations=read)
    def find_recipes_by_nutrition(max_kcal: float | None = None, min_protein_g: float | None = None,
                                  max_salt_g: float | None = None, tag: str | None = None, query: str | None = None,
                                  limit: int = 15) -> dict[str, Any]:
        """Mealie recipes that meet nutrition limits per serving (calories, protein, salt), best protein first.
        Optionally only a tag such as breakfast or a name word. Recipes without nutrition are skipped."""
        return guard(lambda: {"recipes": mealie.find_by_nutrition(max_kcal, min_protein_g, max_salt_g, tag, query, limit)})

    # ----- meal plan extras -----
    @mcp.tool(annotations=read)
    @personal
    def get_plan_nutrition(days: int = 7, start: str | None = None) -> dict[str, Any]:
        """The meal plan with calories, protein, carbs, fat, fibre and salt per meal and per day, from each
        recipe's own nutrition (one serving of each planned meal). Fasting days from the health log are marked."""
        try:
            s = date.fromisoformat(start) if start else store.local(store.now()).date()
        except ValueError:
            return {"error": "start must be an ISO date such as 2026-11-12"}
        d = max(1, min(days, 31))
        res = guard(lambda: mealie.plan_nutrition(d, s))
        if "days" in res:
            fasting = store.fasting_days(max(1, (store.local(store.now()).date() - s).days + d + 1))
            for day in res["days"]:
                day["fasting"] = date.fromisoformat(day["date"]) in fasting
        return res

    @mcp.tool(annotations=write)
    def swap_meal_plan_entry(entry_id: str, recipe_slug: str) -> dict[str, Any]:
        """Replace the recipe on a meal plan entry with another recipe, keeping its date and meal slot.
        Ids come from get_meal_plan; slugs from search_recipes."""
        return guard(lambda: mealie.swap_plan_entry(entry_id, recipe_slug))

    @mcp.tool(annotations=write)
    def move_meal_plan_entry(entry_id: str, date_iso: str, meal: str | None = None) -> dict[str, Any]:
        """Move a meal plan entry to another date (and optionally another meal slot). Ids come from get_meal_plan."""
        try:
            d = date.fromisoformat(date_iso)
        except ValueError:
            return {"error": "date_iso must be an ISO date such as 2026-11-12"}
        return guard(lambda: mealie.move_plan_entry(entry_id, d, meal))

    @mcp.tool(annotations=delete)
    def clear_meal_plan(start_iso: str, end_iso: str, meals: list[str] | None = None) -> dict[str, Any]:
        """Remove every meal plan entry between two dates inclusive (at most 60 days), optionally only some meal
        slots such as ['breakfast','lunch']. Confirm the range with the user first."""
        try:
            a, b = date.fromisoformat(start_iso), date.fromisoformat(end_iso)
        except ValueError:
            return {"error": "Dates must be ISO dates such as 2026-11-12"}
        return guard(lambda: mealie.clear_plan(a, b, meals))
