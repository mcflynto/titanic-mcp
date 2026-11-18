"""Configuration (AUTH-05), rate limits (NFR-05), log redaction (LOG-05) and JWT validation (AUTH-01 to AUTH-03)."""

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from pydantic import ValidationError

from tests.conftest import API_KEYS
from tmcp.auth.verifiers import JwtTokenVerifier
from tmcp.config import Settings
from tmcp.errors import AppError, ErrorCode
from tmcp.observability.logging import REDACTED, redact_secrets
from tmcp.policy.models import Limits
from tmcp.ratelimit import RateLimiter


def test_auth05_api_key_mode_is_forbidden_in_production() -> None:
    with pytest.raises(ValidationError, match="AUTH-05"):
        Settings(env="production", auth_mode="api_key", api_keys_file=API_KEYS)


def test_jwt_mode_needs_issuer_and_jwks() -> None:
    with pytest.raises(ValidationError):
        Settings(auth_mode="jwt")


def test_settings_split_csv_lists() -> None:
    s = Settings(auth_mode="api_key", api_keys_file=API_KEYS, allowed_hosts="a:*, b:80")  # type: ignore[arg-type]
    assert s.allowed_hosts == ["a:*", "b:80"]


async def test_nfr05_rate_limit_per_minute() -> None:
    limiter, limits = RateLimiter(), Limits(requests_per_minute=2, max_concurrency=5)
    for _ in range(2):
        async with limiter.limit("a", limits):
            pass
    with pytest.raises(AppError) as info:
        async with limiter.limit("a", limits):
            pass
    assert info.value.code == ErrorCode.RATE_LIMITED and info.value.details["retry_after_s"] > 0
    async with limiter.limit("b", limits):  # other agents are not affected
        pass


async def test_nfr05_concurrency_limit() -> None:
    limiter, limits = RateLimiter(), Limits(requests_per_minute=100, max_concurrency=1)
    async with limiter.limit("a", limits):
        with pytest.raises(AppError):
            async with limiter.limit("a", limits):
                pass


def test_log05_redacts_secrets() -> None:
    event = redact_secrets(None, "info", {"event": "x", "headers": {"Authorization": "Bearer abc"}, "api_key": "k"})
    assert event["headers"]["Authorization"] == REDACTED and event["api_key"] == REDACTED


# ----- JWT ------------------------------------------------------------------------------------------

ISSUER, AUDIENCE = "http://idp.test/realms/titanic", "http://localhost:8000/mcp"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def jwks_url() -> Iterator[str]:
    jwk = json.loads(RSAAlgorithm.to_jwk(_KEY.public_key()))
    body = json.dumps({"keys": [jwk | {"kid": "k1", "use": "sig", "alg": "RS256"}]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/certs"
    server.shutdown()


def _token(key: Any = _KEY, **claims: Any) -> str:
    now = int(time.time())
    payload = {"iss": ISSUER, "aud": [AUDIENCE, "account"], "azp": "churn-agent", "exp": now + 300, "iat": now}
    payload.update(claims)
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture(scope="module")
def verifier(jwks_url: str) -> JwtTokenVerifier:
    return JwtTokenVerifier(
        Settings(auth_mode="jwt", jwt_issuer=ISSUER, jwks_url=jwks_url, resource_url=AUDIENCE, trust_token_roles=True)
    )


async def test_auth02_valid_token(verifier: JwtTokenVerifier) -> None:
    token = await verifier.verify_token(_token(roles=["statistician"]))
    assert token is not None and token.client_id == "churn-agent"
    assert token.claims is not None and token.claims["roles"] == ["statistician"]


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": int(time.time()) - 3600},
        {"aud": "http://other-service/mcp"},
        {"iss": "http://evil.test"},
        {"azp": None},
    ],
)
async def test_auth02_rejects_invalid_claims(verifier: JwtTokenVerifier, claims: dict[str, Any]) -> None:
    assert await verifier.verify_token(_token(**claims)) is None


async def test_auth02_rejects_wrong_signature(verifier: JwtTokenVerifier) -> None:
    assert await verifier.verify_token(_token(key=_OTHER_KEY)) is None


async def test_auth02_rejects_garbage(verifier: JwtTokenVerifier) -> None:
    assert await verifier.verify_token("not-a-jwt") is None


async def test_auth03_jwks_check(verifier: JwtTokenVerifier) -> None:
    await verifier.check()


def test_metric_label_sets_start_at_zero() -> None:
    from prometheus_client import REGISTRY

    from tmcp.observability import metrics

    metrics.initialize_series(frozenset({"a1"}), frozenset({"run_sql"}))
    assert REGISTRY.get_sample_value("tmcp_query_rejected_total", {"reason": "not_select"}) is not None
    assert REGISTRY.get_sample_value("tmcp_permission_denied_total", {"agent_id": "a1", "tool": "run_sql"}) == 0.0
