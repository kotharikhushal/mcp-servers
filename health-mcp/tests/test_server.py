import pytest
from starlette.testclient import TestClient

from app import server
from app.server import ConfigError, create

BASE = {"PUBLIC_URL": "https://health.example.com", "INGEST_TOKEN": "x" * 32,
        "OIDC_ISSUER": "https://auth.example.com", "OIDC_AUDIENCE": "health-mcp"}
GOOD = {"x-api-key": "x" * 32}


def client(tmp_path, **extra):
    mcp, store, _ = create(dict(BASE, DATA_DIR=str(tmp_path), **extra))
    return TestClient(mcp.streamable_http_app(), base_url="https://health.example.com",
                      raise_server_exceptions=False), store


@pytest.mark.parametrize("missing", ["PUBLIC_URL", "INGEST_TOKEN", "OIDC_ISSUER", "OIDC_AUDIENCE"])
def test_refuses_to_start_without_required_config(tmp_path, missing):
    env = {k: v for k, v in BASE.items() if k != missing}
    with pytest.raises(ConfigError):
        create(dict(env, DATA_DIR=str(tmp_path)))


def test_refuses_short_ingest_token(tmp_path):
    with pytest.raises(ConfigError):
        create(dict(BASE, INGEST_TOKEN="short", DATA_DIR=str(tmp_path)))


def test_healthz_reveals_nothing(tmp_path):
    c, _ = client(tmp_path)
    r = c.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_ingest_auth_methods(tmp_path):
    c, store = client(tmp_path)
    body = {"weight": [{"kilograms": 78.0, "time": "2026-11-10T07:00:00Z"}]}
    assert c.post("/ingest", json=body).status_code == 401
    assert c.post("/ingest", json=body, headers={"x-api-key": "y" * 32}).status_code == 401
    assert c.post("/ingest", json=body, headers=GOOD).status_code == 200
    assert c.post("/ingest", json=body, headers={"authorization": "Bearer " + "x" * 32}).status_code == 200
    assert store.status()["tables"]["weight"]["rows"] == 1


def test_query_key_is_opt_in(tmp_path):
    body = {"weight": [{"kilograms": 78.0, "time": "2026-11-10T07:00:00Z"}]}
    c, _ = client(tmp_path)
    assert c.post("/ingest?key=" + "x" * 32, json=body).status_code == 401
    c2, _ = client(tmp_path, ALLOW_QUERY_KEY="true")
    assert c2.post("/ingest?key=" + "x" * 32, json=body).status_code == 200


def test_repeated_failures_are_throttled(tmp_path):
    c, _ = client(tmp_path)
    for _ in range(server.FAIL_LIMIT):
        assert c.post("/ingest", json={}, headers={"x-api-key": "bad" * 12}).status_code == 401
    assert c.post("/ingest", json={}, headers={"x-api-key": "bad" * 12}).status_code == 429


def test_oversized_payload_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAX_BODY", 100)
    c, _ = client(tmp_path)
    r = c.post("/ingest", content=b"{" + b" " * 500 + b"}", headers={**GOOD, "content-type": "application/json"})
    assert r.status_code == 413


def test_each_person_reaches_only_their_own_data(tmp_path, monkeypatch):
    from mcp.server.auth.provider import AccessToken
    from app import server, web
    k = server.Store(str(tmp_path / "health.db"), tz="Europe/London")
    stores = web.Stores(str(tmp_path), "khushal", "Europe/London", k)
    stores.get("shivani").log_weight(60.0, None)
    k.log_weight(76.0, None)
    links = server.parse_mcp_users("khushal:K-SUB|khushal@example.com, shivani:S-SUB")
    assert links == {"k-sub": "khushal", "khushal@example.com": "khushal", "s-sub": "shivani"}
    ps = server.PersonStore(stores, links)
    who = {"v": None}
    monkeypatch.setattr(server, "get_access_token", lambda: who["v"])
    who["v"] = AccessToken(token="t", client_id="c", scopes=[], subject="S-SUB", claims={"sub": "S-SUB"})
    assert ps.weight_readings(1)[0]["kg"] == 60.0
    who["v"] = AccessToken(token="t", client_id="c", scopes=[], subject="uuid-1", claims={"email": "Khushal@example.com"})
    assert ps.weight_readings(1)[0]["kg"] == 76.0
    who["v"] = AccessToken(token="t", client_id="c", scopes=[], subject="stranger", claims={})
    with pytest.raises(server.PersonRequired) as e:
        ps.weight_readings(1)
    assert "stranger" in str(e.value) and "MCP_USERS" in str(e.value)
    assert server.PersonStore(stores, {}).weight_readings(1)[0]["kg"] == 76.0   # unset: default person, as before
    with pytest.raises(server.ConfigError):
        server.parse_mcp_users("shivani")
