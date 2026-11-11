"""OIDC bearer-token verification for the MCP endpoint (works with Authelia).

Two modes, chosen automatically per token:
  * JWT access tokens: verified locally against the issuer's JWKS (signature, exp,
    nbf, iss, aud). Needs the OIDC client to issue signed JWT access tokens.
  * Opaque access tokens: checked with the issuer's RFC 7662 introspection endpoint,
    authenticating with the client id and secret. Results are cached briefly.

Anything that fails verification is rejected. There is no unauthenticated mode.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from mcp.server.auth.provider import AccessToken

log = logging.getLogger("health-mcp.auth")

ALLOWED_ALGS = ["RS256", "ES256"]
JWKS_TTL = 3600
JWKS_MIN_REFETCH = 60
INTROSPECT_CACHE_TTL = 60


def _aud_list(aud: Any) -> list[str]:
    if isinstance(aud, str):
        return [aud]
    if isinstance(aud, (list, tuple)):
        return [str(a) for a in aud]
    return []


class OIDCVerifier:
    def __init__(self, issuer: str, audience: str, *, client_id: str | None = None,
                 client_secret: str | None = None, jwks_uri: str | None = None,
                 introspection_endpoint: str | None = None,
                 allowed_subjects: set[str] | None = None,
                 http: httpx.AsyncClient | None = None) -> None:
        if not issuer or not audience:
            raise ValueError("OIDC issuer and audience are required")
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.client_id = client_id
        self.client_secret = client_secret
        self._jwks_uri = jwks_uri
        self._introspection = introspection_endpoint
        self.allowed_subjects = allowed_subjects or set()
        self._http = http or httpx.AsyncClient(timeout=10)
        self._discovery: dict[str, Any] | None = None
        self._jwks: dict[str, Any] | None = None
        self._jwks_at = 0.0
        self._cache: dict[str, tuple[float, AccessToken]] = {}
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- discovery
    async def discovery(self) -> dict[str, Any]:
        if self._discovery is not None:
            return self._discovery
        async with self._lock:
            if self._discovery is None:
                r = await self._http.get(f"{self.issuer}/.well-known/openid-configuration")
                r.raise_for_status()
                self._discovery = r.json()
        return self._discovery

    async def _get_jwks(self, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if self._jwks and not force and now - self._jwks_at < JWKS_TTL:
            return self._jwks
        async with self._lock:
            now = time.monotonic()
            if self._jwks and not force and now - self._jwks_at < JWKS_TTL:
                return self._jwks
            if force and self._jwks and now - self._jwks_at < JWKS_MIN_REFETCH:
                return self._jwks
            uri = self._jwks_uri
            if not uri:
                if self._discovery is None:
                    r = await self._http.get(f"{self.issuer}/.well-known/openid-configuration")
                    r.raise_for_status()
                    self._discovery = r.json()
                uri = self._discovery["jwks_uri"]
            r = await self._http.get(uri)
            r.raise_for_status()
            self._jwks = r.json()
            self._jwks_at = time.monotonic()
            return self._jwks

    # ------------------------------------------------------------------- verify
    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            if token.count(".") == 2:
                return await self._verify_jwt(token)
            return await self._verify_opaque(token)
        except (httpx.HTTPError, KeyError, ValueError) as e:
            log.error("token verification infrastructure error: %s", e)
            return None
        except Exception:  # never let a verifier bug turn into access
            log.exception("unexpected error verifying token")
            return None

    def _finish(self, token: str, claims: dict[str, Any]) -> AccessToken | None:
        sub = str(claims.get("sub") or claims.get("username") or "")
        if self.allowed_subjects and sub not in self.allowed_subjects:
            log.warning("subject not allowed")
            return None
        scope = claims.get("scope", "")
        scopes = scope.split() if isinstance(scope, str) else list(scope or [])
        exp = claims.get("exp")
        return AccessToken(token=token, client_id=str(claims.get("client_id") or claims.get("azp") or sub),
                           scopes=scopes, expires_at=int(exp) if exp else None, subject=sub or None)

    async def claims_from_jwt(self, token: str) -> dict[str, Any] | None:
        """Verified claims of a signed JWT (signature, exp, nbf, iss, aud), or None."""
        jwks = await self._get_jwks()
        try:
            decoded = jwt.decode(token, KeySet.import_key_set(jwks), algorithms=ALLOWED_ALGS)
        except JoseError:
            # maybe the issuer rotated keys; refetch once (rate limited) and retry
            jwks = await self._get_jwks(force=True)
            try:
                decoded = jwt.decode(token, KeySet.import_key_set(jwks), algorithms=ALLOWED_ALGS)
            except JoseError as e:
                log.warning("JWT rejected: %s", e)
                return None
        c = decoded.claims
        now = int(time.time())
        exp = c.get("exp")
        if exp is None or now > int(exp):
            log.warning("JWT expired or missing exp")
            return None
        if c.get("nbf") is not None and now + 30 < int(c["nbf"]):
            log.warning("JWT not yet valid")
            return None
        if str(c.get("iss", "")).rstrip("/") != self.issuer:
            log.warning("JWT issuer mismatch")
            return None
        if self.audience not in _aud_list(c.get("aud")):
            log.warning("JWT audience mismatch")
            return None
        return c

    async def _verify_jwt(self, token: str) -> AccessToken | None:
        c = await self.claims_from_jwt(token)
        return self._finish(token, c) if c is not None else None

    async def _verify_opaque(self, token: str) -> AccessToken | None:
        key = hashlib.sha256(token.encode()).hexdigest()
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        if not (self.client_id and self.client_secret):
            log.warning("opaque token received but no client credentials for introspection")
            return None
        endpoint = self._introspection or (await self.discovery()).get("introspection_endpoint")
        if not endpoint:
            log.warning("issuer exposes no introspection endpoint")
            return None
        r = await self._http.post(endpoint, data={"token": token, "token_type_hint": "access_token"},
                                  auth=(self.client_id, self.client_secret))
        if r.status_code != 200:
            log.warning("introspection returned %s", r.status_code)
            return None
        c = r.json()
        if not c.get("active"):
            return None
        now = int(time.time())
        if c.get("exp") is not None and now > int(c["exp"]):
            return None
        if c.get("iss") and str(c["iss"]).rstrip("/") != self.issuer:
            log.warning("introspection issuer mismatch")
            return None
        aud_ok = self.audience in _aud_list(c.get("aud")) or c.get("client_id") == self.audience
        if not aud_ok:
            log.warning("introspected token not issued for this resource")
            return None
        at = self._finish(token, c)
        if at:
            ttl = INTROSPECT_CACHE_TTL
            if c.get("exp"):
                ttl = max(0, min(ttl, int(c["exp"]) - now))
            self._cache[key] = (time.monotonic() + ttl, at)
            if len(self._cache) > 256:
                self._cache = {k: v for k, v in self._cache.items() if v[0] > time.monotonic()}
        return at
