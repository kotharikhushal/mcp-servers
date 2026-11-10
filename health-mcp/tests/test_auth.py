import asyncio
import time

import httpx
from joserfc import jwt
from joserfc.jwk import OctKey, RSAKey

from app import auth as auth_mod
from app.auth import OIDCVerifier

ISSUER = "https://auth.example.com"
AUD = "health-mcp"


def keypair(kid):
    return RSAKey.generate_key(2048, parameters={"kid": kid, "use": "sig", "alg": "RS256"})


class FakeIssuer:
    """Mock Authelia: discovery, JWKS and introspection, served through httpx.MockTransport."""

    def __init__(self, keys, introspect=None):
        self.keys = keys
        self.introspect = introspect or {}
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks.json",
                                             "introspection_endpoint": f"{ISSUER}/api/oidc/introspection"})
        if request.url.path == "/jwks.json":
            return httpx.Response(200, json={"keys": [k.as_dict(private=False) for k in self.keys]})
        if request.url.path == "/api/oidc/introspection":
            assert request.headers["authorization"].startswith("Basic ")
            token = dict(x.split("=") for x in request.content.decode().split("&"))["token"]
            return httpx.Response(200, json=self.introspect.get(token, {"active": False}))
        return httpx.Response(404)

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def make_token(key, **over):
    claims = {"iss": ISSUER, "aud": [AUD], "sub": "khushal", "exp": int(time.time()) + 600,
              "scope": "openid profile"}
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode({"alg": "RS256", "kid": key.kid}, claims, key)


def verifier(issuer, **kw):
    return OIDCVerifier(ISSUER, AUD, client_id=AUD, client_secret="s3cret", http=issuer.client(), **kw)


def run(coro):
    return asyncio.run(coro)


def test_valid_jwt_accepted():
    k = keypair("k1"); v = verifier(FakeIssuer([k]))
    at = run(v.verify_token(make_token(k)))
    assert at is not None and at.subject == "khushal" and "openid" in at.scopes


def test_bad_jwts_rejected():
    k, other = keypair("k1"), keypair("k1")        # same kid, different key => bad signature
    v = verifier(FakeIssuer([k]))
    cases = {
        "expired": make_token(k, exp=int(time.time()) - 5),
        "no exp": make_token(k, exp=None),
        "wrong audience": make_token(k, aud=["other-app"]),
        "no audience": make_token(k, aud=None),
        "wrong issuer": make_token(k, iss="https://evil.example.com"),
        "not yet valid": make_token(k, nbf=int(time.time()) + 3600),
        "forged signature": make_token(other),
        "garbage": "a.b.c",
    }
    for name, tok in cases.items():
        assert run(v.verify_token(tok)) is None, name


def test_algorithm_confusion_rejected():
    k = keypair("k1")
    v = verifier(FakeIssuer([k]))
    # HS256 token "signed" with the public key text as the secret, and an unsigned alg=none token
    secret = OctKey.import_key(str(k.as_dict(private=False)["n"]))
    hs = jwt.encode({"alg": "HS256", "kid": "k1"}, {"iss": ISSUER, "aud": [AUD], "exp": int(time.time()) + 60}, secret)
    none = "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJpc3MiOiJodHRwczovL2F1dGguZXhhbXBsZS5jb20iLCJhdWQiOlsiaGVhbHRoLW1jcCJdLCJleHAiOjk5OTk5OTk5OTl9."
    assert run(v.verify_token(hs)) is None
    assert run(v.verify_token(none)) is None


def test_allowed_subjects():
    k = keypair("k1")
    v = verifier(FakeIssuer([k]), allowed_subjects={"khushal"})
    assert run(v.verify_token(make_token(k))) is not None
    assert run(v.verify_token(make_token(k, sub="intruder"))) is None


def test_key_rotation_refetches_jwks(monkeypatch):
    monkeypatch.setattr(auth_mod, "JWKS_MIN_REFETCH", 0)
    k1, k2 = keypair("k1"), keypair("k2")
    issuer = FakeIssuer([k1])
    v = verifier(issuer)
    assert run(v.verify_token(make_token(k1))) is not None
    issuer.keys = [k1, k2]                                    # issuer rotates in a new key
    assert run(v.verify_token(make_token(k2))) is not None


def test_opaque_token_introspection():
    now = int(time.time())
    good = {"active": True, "sub": "khushal", "client_id": AUD, "scope": "openid", "exp": now + 300, "iss": ISSUER}
    issuer = FakeIssuer([], introspect={
        "opaque-good": good,
        "opaque-other-client": {**good, "client_id": "someone-else"},
        "opaque-expired": {**good, "exp": now - 5},
        "opaque-wrong-issuer": {**good, "iss": "https://evil.example.com"},
    })
    v = verifier(issuer)
    assert run(v.verify_token("opaque-good")).subject == "khushal"
    assert run(v.verify_token("opaque-good")) is not None
    assert issuer.calls.count("/api/oidc/introspection") == 1          # second call served from cache
    for bad in ("opaque-unknown", "opaque-other-client", "opaque-expired", "opaque-wrong-issuer"):
        assert run(v.verify_token(bad)) is None, bad


def test_opaque_token_without_credentials_rejected():
    issuer = FakeIssuer([])
    v = OIDCVerifier(ISSUER, AUD, http=issuer.client())          # no client id/secret
    assert run(v.verify_token("opaque-good")) is None


def test_issuer_outage_fails_closed():
    def boom(request):
        raise httpx.ConnectError("down")
    v = OIDCVerifier(ISSUER, AUD, http=httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    assert run(v.verify_token(make_token(keypair("k1")))) is None
