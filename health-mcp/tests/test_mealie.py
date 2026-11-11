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


def handler(req: httpx.Request) -> httpx.Response:
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
