"""Tests for the second batch of Mealie tools, against a small stateful fake Mealie."""
import json
from datetime import date

import httpx
import pytest

from app import mealie, server
from tests.test_mealie import BASE, call, tool_names


class Fake:
    def __init__(self):
        self.recipes = {
            "dal": {"id": "r1", "slug": "dal", "name": "Dal", "recipeServings": 2, "tags": [],
                    "nutrition": {"calories": "400", "proteinContent": "20", "sodiumContent": "400"},
                    "recipeIngredient": [{"note": "lentils"}]},
            "dal-1": {"id": "r2", "slug": "dal-1", "name": "Dal", "recipeServings": 2, "tags": [], "nutrition": {},
                      "recipeIngredient": [{"note": "lentils"}]},
            "empty": {"id": "r3", "slug": "empty", "name": "Empty", "tags": [], "nutrition": {}, "recipeIngredient": []},
        }
        self.tags = []
        self.items = [{"id": "i1", "note": "salt", "checked": False, "shoppingListId": "L1"},
                      {"id": "i2", "note": "2 onions", "checked": False, "shoppingListId": "L1"},
                      {"id": "i3", "note": "olive oil", "checked": True, "shoppingListId": "L1"}]
        self.plan = {"p1": {"id": "p1", "date": "2026-11-13", "entryType": "lunch", "recipe": {"name": "Dal", "slug": "dal"}, "recipeId": "r1"}}
        self.lists = {"L1": "Grocery"}
        self.drop_bulk = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        p, m = req.url.path, req.method
        body = json.loads(req.content) if req.content else None
        ok = lambda j=None: httpx.Response(200, json={} if j is None else j)
        if p == "/api/recipes" and m == "GET":
            return ok({"items": list(self.recipes.values()), "total_pages": 1})
        if p == "/api/recipes" and m == "POST":
            slug = body["name"].lower().replace(" ", "-")
            self.recipes[slug] = {"id": "n" + slug, "slug": slug, "name": body["name"], "tags": [], "nutrition": {}, "recipeIngredient": []}
            return ok(slug)
        if p == "/api/recipes/create/url":
            self.recipes["imported"] = {"id": "ri", "slug": "imported", "name": "Imported", "tags": [], "nutrition": {}, "recipeIngredient": []}
            return ok("imported")
        if p.startswith("/api/recipes/"):
            slug = p.rsplit("/", 1)[1]
            r = self.recipes.get(slug)
            if r is None:
                return httpx.Response(404)
            if m == "PATCH":
                r.update(body)
                if "recipeIngredient" in body:
                    r["recipeIngredient"] = body["recipeIngredient"]
            if m == "DELETE":
                del self.recipes[slug]
            return ok(r)
        if p == "/api/organizers/tags" and m == "GET":
            return ok({"items": self.tags})
        if p == "/api/organizers/tags" and m == "POST":
            t = {"id": "t" + str(len(self.tags)), "name": body["name"], "slug": body["name"].lower()}
            self.tags.append(t)
            return ok(t)
        if p == "/api/households/shopping/lists" and m == "POST":
            self.lists["L2"] = body["name"]
            return ok({"id": "L2", "name": body["name"]})
        if p.startswith("/api/households/shopping/lists/"):
            lid = p.rsplit("/", 1)[1]
            if lid not in self.lists:
                return httpx.Response(404)
            if m == "DELETE":
                del self.lists[lid]
                return ok()
            return ok({"id": lid, "name": self.lists[lid], "listItems": self.items if lid == "L1" else []})
        if p == "/api/households/shopping/items/create-bulk":
            if not self.drop_bulk:
                n = len(self.items)
                self.items += [{"id": f"n{n + k}", "note": b["note"], "checked": False} for k, b in enumerate(body)]
            return ok()
        if p == "/api/households/shopping/items" and m == "PUT":
            for b in body:
                for i in self.items:
                    if i["id"] == b["id"]:
                        i["checked"] = b["checked"]
            return ok()
        if p == "/api/households/shopping/items" and m == "DELETE":
            gone = req.url.params.get_list("ids")
            self.items = [i for i in self.items if i["id"] not in gone]
            return ok()
        if p == "/api/households/mealplans" and m == "GET":
            return ok({"items": list(self.plan.values())})
        if p.startswith("/api/households/mealplans/"):
            eid = p.rsplit("/", 1)[1]
            if m == "PUT":
                self.plan[eid].update(body)
            if m == "DELETE":
                del self.plan[eid]
            return ok(self.plan[eid] if eid in self.plan else {})
        return httpx.Response(404)


def make(fake):
    return mealie.Mealie("https://m.example.com", "tok", httpx.Client(transport=httpx.MockTransport(fake)))


def test_shopping_items_add_check_remove_and_read_back():
    f = Fake()
    c = make(f)
    assert c.add_shopping_items("L1", ["2 lemons", {"text": "milk", "quantity": 2}]) == {"added": 2, "items_now": 5}
    f.drop_bulk = True
    with pytest.raises(mealie.MealieError) as e:
        c.add_shopping_items("L1", ["ghost"])
    assert "only 0 of 1" in str(e.value)
    r = c.change_shopping_items("L1", "check", [], ["SALT", "onion"])
    assert r["changed"] == 2 and all(i["checked"] for i in f.items if i["id"] in ("i1", "i2"))
    assert c.clear_checked_items("L1") == {"removed": 3} and [i["id"] for i in f.items] == ["n3", "n4"]
    assert c.change_shopping_items("L1", "remove", [], ["milk"])["changed"] == 1
    with pytest.raises(mealie.MealieError):
        c.change_shopping_items("L1", "check", [], [])
    with pytest.raises(mealie.MealieError):
        c.change_shopping_items("L1", "burn", ["i1"], [])


def test_list_create_and_delete_needs_exact_name():
    f = Fake()
    c = make(f)
    assert c.create_shopping_list("Weekend")["id"] == "L2"
    with pytest.raises(mealie.MealieError):
        c.delete_shopping_list("L2", "Other")
    assert "L2" in f.lists
    assert c.delete_shopping_list("L2", "weekend") == {"deleted": "Weekend"} and "L2" not in f.lists


def test_create_update_import_and_tags():
    f = Fake()
    c = make(f)
    made = c.create_recipe("Test Soup", ["1 onion", "2 carrots"], ["Chop", "Boil"], 3, ["Lunch"])
    assert made["slug"] == "test-soup"
    r = f.recipes["test-soup"]
    assert len(r["recipeIngredient"]) == 2 and r["recipeServings"] == 3 and [t["name"] for t in r["tags"]] == ["Lunch"]
    assert c.update_recipe("dal", servings=4, ingredients=["a", "b", "c"])["updated"] == ["recipeIngredient", "recipeServings", "recipeYield"]
    assert c.set_tags(["dal"], ["lunch", "high-protein"]) == [{"slug": "dal", "tags": ["Lunch", "high-protein"]}]
    assert len(f.tags) == 2  # existing 'Lunch' reused case-insensitively
    assert {x["slug"] for x in c.search("", 10, tag="LUNCH")} == {"test-soup", "dal"}
    imp = c.import_recipe("https://example.com/r")
    assert imp["slug"] == "imported" and "no ingredients" in imp["note"]
    for bad in (lambda: c.import_recipe("ftp://x"), lambda: c.update_recipe("dal"), lambda: c.update_recipe("dal", servings=0),
                lambda: c.update_recipe("dal", ingredients=[]), lambda: c.create_recipe(" ", ["x"], None, None, None)):
        with pytest.raises(mealie.MealieError):
            bad()


def test_duplicates_nutrition_search_and_delete_options():
    f = Fake()
    c = make(f)
    d = c.find_duplicates()
    assert [x["slug"] for x in d["same_name"][0]] == ["dal", "dal-1"] and len(d["same_ingredients"]) == 1
    hits = c.find_by_nutrition(500, 10, 1.5, None, None, 10)
    assert [h["slug"] for h in hits] == ["dal"]
    assert c.find_by_nutrition(300, None, None, None, None, 10) == []
    with pytest.raises(mealie.MealieError):
        c.delete_recipe("dal-1")
    assert c.delete_recipe("dal-1", allow_with_ingredients=True)["deleted"] and "dal-1" not in f.recipes


def test_plan_nutrition_move_and_clear():
    f = Fake()
    c = make(f)
    n = c.plan_nutrition(7, date(2026, 11, 12))
    assert n["days"][0]["totals_for_one_serving_each"]["kcal"] == 400
    assert c.move_plan_entry("p1", date(2026, 11, 14), "dinner") == {"id": "p1", "date": "2026-11-14", "meal": "dinner"}
    with pytest.raises(mealie.MealieError):
        c.move_plan_entry("p1", date(2026, 11, 14), "tea")
    assert c.clear_plan(date(2026, 11, 12), date(2026, 11, 20), ["lunch"])["removed"] == 0
    assert c.clear_plan(date(2026, 11, 12), date(2026, 11, 20), None)["removed"] == 1 and not f.plan
    with pytest.raises(mealie.MealieError):
        c.clear_plan(date(2026, 1, 1), date(2026, 6, 1), None)


def test_all_new_tools_exist_and_health_settings_tools_work(tmp_path, monkeypatch):
    f = Fake()
    real = mealie.Mealie
    monkeypatch.setattr(mealie, "Mealie", lambda url, tok: real(url, tok, httpx.Client(transport=httpx.MockTransport(f))))
    mcp, store, _ = server.create(dict(BASE, DATA_DIR=str(tmp_path), MEALIE_URL="https://m.example.com", MEALIE_API_TOKEN="tok"))
    assert {"create_shopping_list", "delete_shopping_list", "add_shopping_items", "update_shopping_items",
            "clear_checked_shopping_items", "create_recipe", "update_recipe", "import_recipe_from_url", "set_recipe_tags",
            "find_duplicate_recipes", "find_recipes_by_nutrition", "get_plan_nutrition", "move_meal_plan_entry",
            "clear_meal_plan", "log_planned_meal", "recipes_without_ingredients", "delete_recipes",
            "get_settings", "set_goals", "set_fasting_days", "set_gp_note", "set_ai_analysis"} <= tool_names(mcp)
    assert call(mcp, "recipes_without_ingredients")["recipes"] == [{"slug": "empty", "name": "Empty"}]
    out = call(mcp, "delete_recipes", slugs=["empty", "dal"])["results"]
    assert out[0]["deleted"] and "has ingredients" in out[1]["error"]
    assert "error" in call(mcp, "add_shopping_items", list_id="L1", items=[])
    # a planned meal is logged from the recipe's own nutrition
    logged = call(mcp, "log_planned_meal", date_iso="2026-11-13", meal="lunch", servings=1.5, when="2026-11-13T14:00")
    assert logged["logged"] and logged["recipe"] == "Dal" and "error" in call(mcp, "log_planned_meal", date_iso="2026-11-13", meal="dinner")
    plan = call(mcp, "get_plan_nutrition", days=3, start="2026-11-12")
    assert plan["days"][0]["fasting"] is False
    # health-side settings
    assert call(mcp, "set_fasting_days", dates=["2026-11-12", "2026-11-17"])["updated"] == 2
    assert "error" in call(mcp, "set_goals", steps_goal=5)
    s = call(mcp, "set_goals", steps_goal=10000, sleep_goal_hours=7.5)
    assert s["steps_goal"] == 10000 and s["sleep_goal_hours"] == 7.5 and s["calorie_goal_kcal"] is None
    assert "error" in call(mcp, "set_goals", calorie_goal_kcal=100)
    n = call(mcp, "set_goals", calorie_goal_kcal=1850, protein_goal_g=120, salt_limit_g=6)
    assert (n["calorie_goal_kcal"], n["protein_goal_g"], n["salt_limit_g"]) == (1850, 120, 6) and n["steps_goal"] == 10000
    assert call(mcp, "set_gp_note", text="Losartan 25 mg from 10 Nov")["saved"] and call(mcp, "get_settings")["gp_note"].startswith("Losartan")


def test_swap_options_are_closest_in_calories_and_swap_changes_the_plan():
    f = Fake()
    f.recipes["soup"] = {"id": "r9", "slug": "soup", "name": "Soup", "tags": [], "nutrition": {"calories": "350"}, "recipeIngredient": []}
    c = make(f)
    opts = c.swap_options(380, exclude_slug="dal")
    assert [o["slug"] for o in opts] == ["soup"]          # no-calorie recipes are left out, current recipe excluded
    assert c.swap_options(None, "SOU")[0]["name"] == "Soup"
    res = c.swap_plan_entry("p1", "soup")
    assert res["recipe"] == "Soup" and f.plan["p1"]["recipeId"] == "r9" and f.plan["p1"]["entryType"] == "lunch"
    with pytest.raises(mealie.MealieError):
        c.swap_plan_entry("bad id!", "soup")
