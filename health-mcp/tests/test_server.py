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
