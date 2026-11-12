import asyncio
from datetime import date, datetime, timezone

import httpx
import pytest

from app import mealie, server
from app.store import Store

RECIPE = {"id": "r1", "slug": "aloo-gobi", "name": "Aloo gobi", "recipeServings": 4, "totalTime": "45 min",
          "nutrition": {"calories": "210", "proteinContent": "5 g", "carbohydrateContent": "28", "fatContent": "9.5",
                        "fiberContent": "6 g", "sugarContent": "4", "sodiumContent": "400 mg"},
          "recipeIngredient": [{"display": "1 cauliflower"}, {"note": "2 potatoes"}]}
BARE = {"id": "r2", "slug": "mystery", "name": "Mystery", "nutrition": {}, "recipeIngredient": []}
SEEN = []


STATE = {"patched": None, "refuse_patch": False}


def handler(req: httpx.Request) -> httpx.Response:
    import json as _j
    if req.method == "PATCH" and req.url.path == "/api/recipes/mystery":
        if not STATE["refuse_patch"]:
            BARE["nutrition"] = _j.loads(req.content)["nutrition"]
        STATE["patched"] = _j.loads(req.content)
        return httpx.Response(200, json={})
    if req.url.path == "/api/recipes" and req.url.params.get("perPage") == "100":
        return httpx.Response(200, json={"items": [RECIPE, BARE], "total_pages": 1})
    if req.method == "DELETE" and req.url.path == "/api/households/mealplans/abc":
        return httpx.Response(200, json={})
    if req.url.path == "/api/households/shopping/lists" and req.method == "GET":
        return httpx.Response(200, json={"items": [{"id": "L1", "name": "Weekly"}]})  # summaries carry no items
    if req.url.path == "/api/households/shopping/lists/L1" and req.method == "GET":
        return httpx.Response(200, json={"id": "L1", "name": "Weekly", "listItems": [
            {"display": "2 onions", "checked": False}, {"note": "salt", "checked": True}]})
    if req.method == "DELETE" and req.url.path in ("/api/recipes/mystery", "/api/recipes/aloo-gobi"):
        STATE["deleted_recipe"] = req.url.path
        return httpx.Response(200, json={})
    if req.url.path == "/api/households/shopping/lists/L1/recipe" and req.method == "POST":
        STATE["list_body"] = _j.loads(req.content)
        return httpx.Response(200, json={})
    SEEN.append((req.method, req.url.path, dict(req.url.params), req.headers.get("authorization")))
    p = req.url.path
    if req.headers.get("authorization") != "Bearer tok":
        return httpx.Response(401)
    if p == "/api/app/about":
        return httpx.Response(200, json={"version": "v2.3.0"})
    if p == "/api/users/self":
        return httpx.Response(200, json={"fullName": "Khushal", "username": "kuku"})
    if p == "/api/recipes":
        return httpx.Response(200, json={"items": [{"slug": "aloo-gobi", "name": "Aloo gobi", "description": None}]})
    if p == "/api/recipes/aloo-gobi":
        return httpx.Response(200, json=RECIPE)
    if p == "/api/recipes/mystery":
        return httpx.Response(200, json=BARE)
    if p == "/api/households/mealplans" and req.method == "GET":
        return httpx.Response(200, json={"items": [
            {"id": "b", "date": "2026-11-12", "entryType": "dinner", "recipe": {"name": "Dal", "slug": "dal"}},
            {"id": "a", "date": "2026-11-12", "entryType": "breakfast", "title": "Oats", "text": "with fruit"}]})
    if p == "/api/households/mealplans" and req.method == "POST":
        import json
        body = json.loads(req.content)
        return httpx.Response(201, json={"id": "new", "date": body["date"], "entryType": body["entryType"]})
    return httpx.Response(404)


def client(token="tok"):
    return mealie.Mealie("https://mealie.example.com/", token, httpx.Client(transport=httpx.MockTransport(handler)))


def test_number_parsing():
    assert mealie.number("350") == 350 and mealie.number("12.5 g") == 12.5 and mealie.number("1,5") == 1.5
    assert mealie.number("") is None and mealie.number(None) is None and mealie.number("lots") is None
    assert mealie.number(True) is None


def test_status_search_recipe_and_nutrition():
    c = client()
    assert c.status() == {"reachable": True, "version": "v2.3.0", "signed_in_as": "Khushal"}
    assert c.search("gobi")[0]["slug"] == "aloo-gobi"
    r = c.recipe("aloo-gobi")
    assert r["servings"] == 4 and r["ingredients"] == ["1 cauliflower", "2 potatoes"]
    n = r["nutrition_per_serving"]
    assert n["kcal"] == 210 and n["protein_g"] == 5 and n["salt_g"] == 1.0
    assert client().recipe("mystery")["nutrition_per_serving"] is None


def test_plan_is_sorted_and_add_validates():
    c = client()
    plan = c.plan(3, date(2026, 11, 12))
    assert [e["meal"] for e in plan] == ["breakfast", "dinner"] and plan[0]["note"] == "with fruit"
    assert c.add_to_plan(date(2026, 11, 13), "lunch", "aloo-gobi", None)["meal"] == "lunch"
    for bad in (lambda: c.add_to_plan(date(2026, 11, 13), "brunch", "aloo-gobi", None),
                lambda: c.add_to_plan(date(2026, 11, 13), "lunch", None, None),
                lambda: c.recipe("../users/self")):
        with pytest.raises(mealie.MealieError):
            bad()


def test_bad_token_and_unreachable_do_not_leak_the_token():
    with pytest.raises(mealie.MealieError) as e:
        client("secret-wrong").status()
    assert "secret-wrong" not in str(e.value) and "token" in str(e.value)

    def boom(req):
        raise httpx.ConnectError("no route")
    c = mealie.Mealie("https://m.example.com", "secret-wrong", httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(mealie.MealieError) as e:
        c.status()
    assert "secret-wrong" not in str(e.value) and "Could not reach" in str(e.value)


BASE = {"PUBLIC_URL": "https://health.example.com", "INGEST_TOKEN": "x" * 32,
        "OIDC_ISSUER": "https://auth.example.com", "OIDC_AUDIENCE": "health-mcp"}


def tool_names(mcp):
    return {t.name for t in asyncio.run(mcp.list_tools())}


def test_tools_only_exist_when_configured_and_config_must_be_complete(tmp_path):
    mcp, _, _ = server.create(dict(BASE, DATA_DIR=str(tmp_path)))
    assert "get_recipe" not in tool_names(mcp) and "log_meal" in tool_names(mcp)
    mcp, _, _ = server.create(dict(BASE, DATA_DIR=str(tmp_path), MEALIE_URL="https://m.example.com", MEALIE_API_TOKEN="t"))
    assert {"mealie_status", "search_recipes", "get_recipe", "get_meal_plan", "add_to_meal_plan",
            "log_recipe_as_meal"} <= tool_names(mcp)
    for extra in ({"MEALIE_URL": "https://m.example.com"}, {"MEALIE_API_TOKEN": "t"},
                  {"MEALIE_URL": "ftp://m", "MEALIE_API_TOKEN": "t"}):
        with pytest.raises(server.ConfigError):
            server.create(dict(BASE, DATA_DIR=str(tmp_path), **extra))


def call(mcp, name, **args):
    res = asyncio.run(mcp.call_tool(name, args))
    out = res[1] if isinstance(res, tuple) else res
    return out.get("result", out) if isinstance(out, dict) else out


def test_log_recipe_as_meal_scales_and_refuses_when_no_nutrition(tmp_path, monkeypatch):
    real = mealie.Mealie
    monkeypatch.setattr(mealie, "Mealie", lambda url, tok: real(url, tok, httpx.Client(transport=httpx.MockTransport(handler))))
    mcp, store, _ = server.create(dict(BASE, DATA_DIR=str(tmp_path), MEALIE_URL="https://m.example.com", MEALIE_API_TOKEN="tok"))
    out = call(mcp, "log_recipe_as_meal", slug="aloo-gobi", servings=1.5, when="2026-11-11T14:00")
    assert out["logged"] is True and out["kcal"] == 315
    e = store.food_entries(5)[-1]
    assert e["name"] == "Aloo gobi x1.5 (from Mealie)" and e["kcal"] == 315 and e["salt_g"] == 1.5 and e["time"] == "2026-11-11 14:00"
    n = len(store.food_entries(5))
    assert call(mcp, "log_recipe_as_meal", slug="mystery")["logged"] is False
    assert "error" in call(mcp, "log_recipe_as_meal", slug="aloo-gobi", servings=0)
    assert "error" in call(mcp, "log_recipe_as_meal", slug="nope")
    assert len(store.food_entries(5)) == n
    assert call(mcp, "mealie_status")["version"] == "v2.3.0"


def reset():
    BARE["nutrition"] = {}
    STATE.update(patched=None, refuse_patch=False)


def test_missing_nutrition_lists_only_recipes_without_calories():
    reset()
    out = client().needing_nutrition()
    assert out["recipes_total"] == 2 and out["missing_nutrition"] == 1 and out["recipes"][0]["slug"] == "mystery"


def test_set_nutrition_writes_converts_salt_to_sodium_and_verifies():
    reset()
    c = client()
    res = c.set_nutrition("mystery", {"kcal": 420, "protein_g": 18, "carbs_g": 50, "fat_g": 14, "fibre_g": 9,
                                      "sugar_g": 6, "salt_g": 1.25})
    assert STATE["patched"]["nutrition"]["calories"] == "420" and STATE["patched"]["nutrition"]["sodiumContent"] == "500"
    assert res["saved"]["kcal"] == 420 and res["saved"]["salt_g"] == 1.25
    assert "kcal" in c.recipe("mystery")["nutrition_per_serving"]


def test_set_nutrition_validates_and_reports_a_silent_failure():
    reset()
    c = client()
    for bad in ({"kcal": 0}, {"kcal": 99999}, {"kcal": 300, "protein_g": -4}, {"kcal": 300, "salt_g": 9000}):
        with pytest.raises(mealie.MealieError):
            c.set_nutrition("mystery", bad)
    assert STATE["patched"] is None
    STATE["refuse_patch"] = True
    with pytest.raises(mealie.MealieError) as e:
        c.set_nutrition("mystery", {"kcal": 300})
    assert "did not save" in str(e.value)
    reset()


def test_delete_shopping_and_id_checks():
    c = client()
    assert c.delete_plan_entry("abc") == {"deleted": True}
    assert c.shopping_lists() == [{"id": "L1", "name": "Weekly", "items": 2, "unchecked": 1}]
    assert c.shopping_lists(True)[0]["unchecked_items"] == [{"id": None, "text": "2 onions"}]
    c.add_recipes_to_list("L1", {"aloo-gobi": 2})
    assert STATE["list_body"] == [{"recipeId": "r1", "recipeIncrementQuantity": 2}]
    for bad in (lambda: c.delete_plan_entry("../x"), lambda: c.add_recipes_to_list("a/b", {"aloo-gobi": 1})):
        with pytest.raises(mealie.MealieError):
            bad()


def test_delete_only_recipes_without_ingredients():
    c = client()
    assert c.recipes_without_ingredients() == [{"slug": "mystery", "name": "Mystery"}]
    STATE.pop("deleted_recipe", None)
    with pytest.raises(mealie.MealieError) as e:
        c.delete_recipe("aloo-gobi")
    assert "has ingredients" in str(e.value) and "deleted_recipe" not in STATE
    assert c.delete_recipe("mystery") == {"slug": "mystery", "deleted": True}
    assert STATE["deleted_recipe"] == "/api/recipes/mystery"


def test_new_tools_through_mcp_with_batch_limits(tmp_path, monkeypatch):
    reset()
    real = mealie.Mealie
    monkeypatch.setattr(mealie, "Mealie", lambda url, tok: real(url, tok, httpx.Client(transport=httpx.MockTransport(handler))))
    mcp, _, _ = server.create(dict(BASE, DATA_DIR=str(tmp_path), MEALIE_URL="https://m.example.com", MEALIE_API_TOKEN="tok"))
    assert {"recipes_missing_nutrition", "set_recipes_nutrition", "add_meals_to_plan", "remove_meal_plan_entry",
            "get_shopping_lists", "add_recipes_to_shopping_list"} <= tool_names(mcp)
    r = call(mcp, "set_recipes_nutrition", recipes=[{"slug": "mystery", "kcal": 410, "salt_g": 1.0},
                                                    {"slug": "mystery", "kcal": 0}, {"slug": "../x", "kcal": 5}])
    assert r["results"][0]["saved"]["kcal"] == 410 and "error" in r["results"][1] and "error" in r["results"][2]
    assert "error" in call(mcp, "set_recipes_nutrition", recipes=[])
    assert "error" in call(mcp, "add_meals_to_plan", entries=[])
    res = call(mcp, "add_meals_to_plan", entries=[{"date_iso": "2026-11-13", "meal": "dinner", "recipe_slug": "aloo-gobi"},
                                                  {"date_iso": "nope", "meal": "dinner", "title": "x"},
                                                  {"date_iso": "2026-11-13", "meal": "tea", "title": "x"}])["results"]
    assert res[0]["meal"] == "dinner" and "error" in res[1] and "error" in res[2]
    assert "error" in call(mcp, "add_recipes_to_shopping_list", list_id="L1", recipe_slugs=["a"], servings_multiplier=0)
    assert call(mcp, "add_recipes_to_shopping_list", list_id="L1", recipe_slugs=["aloo-gobi"])["added_recipes"] == ["aloo-gobi"]
    reset()


def test_clients_from_env_gives_each_person_their_own_token():
    env = {"MEALIE_URL": "https://m.example.com", "MEALIE_API_TOKEN": "khushal-tok", "MEALIE_TOKEN_BUNNY": "bunny-tok"}
    c = mealie.clients_from_env(env, ["kuku", "bunny"], "kuku")
    assert set(c) == {"kuku", "bunny"} and c["kuku"] is not c["bunny"]
    assert mealie.clients_from_env({"MEALIE_URL": "https://m.example.com", "MEALIE_API_TOKEN": "t"}, ["kuku", "bunny"], "kuku").keys() == {"kuku"}
    assert mealie.clients_from_env({}, ["kuku"], "kuku") == {}
    for bad in ({"MEALIE_URL": "https://m"}, {"MEALIE_API_TOKEN": "t"}):
        with pytest.raises(ValueError):
            mealie.clients_from_env(bad, ["kuku"], "kuku")


def test_by_person_uses_the_askers_client_and_errors_without_one():
    class C:
        def __init__(self, n): self.n = n
        def status(self): return self.n
    who = ["kuku"]
    bp = mealie.ByPerson({"kuku": C("k")}, lambda: who[0])
    assert bp.status() == "k"
    who[0] = "bunny"
    with pytest.raises(mealie.MealieError, match="MEALIE_TOKEN_BUNNY"):
        bp.status()
