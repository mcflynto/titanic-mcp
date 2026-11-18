"""Token verification (AUTH-01 to AUTH-06). The MCP SDK calls ``verify_token`` for each request to /mcp."""

import hashlib
import hmac
import time
from pathlib import Path
from typing import Any

import anyio
import jwt
import yaml
from mcp.server.auth.provider import AccessToken, TokenVerifier

from tmcp.config import Settings
from tmcp.observability import metrics
from tmcp.observability.logging import get_logger

log = get_logger(__name__)


class JwtTokenVerifier(TokenVerifier):
    """Validates OAuth 2.1 access tokens (JWT) from the IdP with the public keys (JWKS)."""

    def __init__(self, settings: Settings) -> None:
        if not (settings.jwks_url and settings.jwt_issuer):
            raise ValueError("jwt mode needs TMCP_JWKS_URL and TMCP_JWT_ISSUER")
        self._issuer = settings.jwt_issuer
        self._audience = settings.audience
        self._algorithms = settings.jwt_algorithms
        self._agent_claim = settings.agent_id_claim
        self._trust_roles = settings.trust_token_roles
        # AUTH-03: keys stay in the cache for 10 minutes; an unknown key ID causes a new download.
        self._jwks = jwt.PyJWKClient(settings.jwks_url, cache_keys=True, lifespan=600, timeout=5)
        self._jwks_checked_at = 0.0

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            signing_key = await anyio.to_thread.run_sync(self._jwks.get_signing_key_from_jwt, token)
            claims: dict[str, Any] = jwt.decode(
                token,
                signing_key.key,
                algorithms=self._algorithms,
                audience=self._audience,
                issuer=self._issuer,
                leeway=30,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError:
            return self._fail("expired")
        except (jwt.InvalidAudienceError, jwt.InvalidIssuerError):
            return self._fail("wrong_issuer_or_audience")
        except jwt.PyJWKClientError:
            return self._fail("unknown_key")
        except jwt.PyJWTError:
            return self._fail("invalid")
        agent_id = claims.get(self._agent_claim)
        if not isinstance(agent_id, str) or not agent_id:
            return self._fail("missing_agent_claim")
        roles = claims.get("roles") if self._trust_roles else None
        return AccessToken(
            token=token,
            client_id=agent_id,
            scopes=str(claims.get("scope", "")).split(),
            expires_at=int(claims["exp"]),
            resource=self._audience,
            subject=claims.get("sub"),
            claims={"iss": claims["iss"], "roles": roles if isinstance(roles, list) else []},
        )

    @staticmethod
    def _fail(reason: str) -> None:
        metrics.AUTH_FAILURES.labels(reason).inc()
        log.warning("authentication_failed", reason=reason)  # AUTH-06: never log the token

    async def check(self) -> None:
        """Readiness (MON-05): the JWKS must be available. A success stays valid for 30 s.

        After one success, a short IdP outage (up to the 10-minute key cache) does not make the pod unready:
        the cached keys still validate tokens, and all pods would otherwise go unready together.
        """
        now = time.monotonic()
        if now - self._jwks_checked_at < 30:
            return
        try:
            await anyio.to_thread.run_sync(self._jwks.get_jwk_set)
        except Exception:
            if self._jwks_checked_at and now - self._jwks_checked_at < 600:
                log.warning("jwks_unavailable_using_cache")
                return
            raise
        self._jwks_checked_at = time.monotonic()


class ApiKeyTokenVerifier(TokenVerifier):
    """AUTH-05: static API keys for local tests only. The file contains SHA-256 hashes, not keys."""

    def __init__(self, path: Path, resource: str) -> None:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        self._keys: dict[str, str] = {}
        for entry in data.get("keys", []):
            self._keys[str(entry["sha256"]).lower()] = str(entry["agent_id"])
        self._resource = resource
        log.info("api_keys_loaded", count=len(self._keys))

    async def verify_token(self, token: str) -> AccessToken | None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        for known, agent_id in self._keys.items():
            if hmac.compare_digest(known, digest):
                return AccessToken(token=token, client_id=agent_id, scopes=[], resource=self._resource, claims={})
        metrics.AUTH_FAILURES.labels("unknown_api_key").inc()
        log.warning("authentication_failed", reason="unknown_api_key")
        return None

    async def check(self) -> None:
        return None


def build_verifier(settings: Settings) -> JwtTokenVerifier | ApiKeyTokenVerifier:
    if settings.auth_mode == "api_key":
        if settings.api_keys_file is None:
            raise ValueError("api_key mode needs TMCP_API_KEYS_FILE")
        return ApiKeyTokenVerifier(settings.api_keys_file, settings.resource_url)
    return JwtTokenVerifier(settings)
