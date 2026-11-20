"""Service configuration (spec section 10). All values come from TMCP_* environment variables."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["development", "demo", "staging", "production"]


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TMCP_", extra="ignore")

    env: Environment = "development"
    service_name: str = "titanic-mcp"
    # The container must listen on all interfaces.
    host: str = "0.0.0.0"  # noqa: S104  # nosec B104
    port: int = 8000
    metrics_port: int = 9090

    # Reference dataset (read-only SQLite file in the image)
    db_path: Path = Path("/app/data/titanic.db")
    db_sha256: str | None = None

    # Working dataset (writable PostgreSQL, OP-01)
    working_db_url: SecretStr | None = None
    working_db_pool_size: int = 5
    change_request_ttl_hours: int = 168

    # Policy
    policy_path: Path = Path("/etc/tmcp/policy.yaml")
    policy_reload_interval_s: float = 10.0

    # Authentication
    auth_mode: Literal["jwt", "api_key"] = "jwt"
    resource_url: str = "http://localhost:8000/mcp"
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwks_url: str | None = None
    jwt_algorithms: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["RS256", "ES256", "PS256"])
    agent_id_claim: str = "azp"
    trust_token_roles: bool = False
    api_keys_file: Path | None = None

    # Transport security (MCP-06)
    allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["localhost:*", "127.0.0.1:*"])
    allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:*", "http://127.0.0.1:*"]
    )

    # HTTP keep-alive: longer than the idle timeout of clients and load balancers (often 60 s), so that the
    # server does not close a connection at the moment when a client sends a new request on it.
    keep_alive_timeout_s: int = Field(75, ge=1)

    # Query limits (section 7)
    query_timeout_s: float = 5.0
    max_sql_length: int = 10_000
    max_result_bytes: int = 1_000_000

    # Observability (sections 8 and 9)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True
    otel_endpoint: str | None = None
    trace_sample_rate: float = 0.1

    @model_validator(mode="before")
    @classmethod
    def _split_lists(cls, data: object) -> object:
        if isinstance(data, dict):
            for key in ("jwt_algorithms", "allowed_hosts", "allowed_origins"):
                if key in data:
                    data[key] = _split_csv(data[key])
        return data

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.auth_mode == "api_key" and self.env in ("staging", "production"):
            raise ValueError("AUTH-05: api_key mode is not permitted when TMCP_ENV is staging or production")
        if self.auth_mode == "api_key" and self.api_keys_file is None:
            raise ValueError("TMCP_API_KEYS_FILE is necessary in api_key mode")
        if self.auth_mode == "jwt" and not (self.jwt_issuer and self.jwks_url):
            raise ValueError("TMCP_JWT_ISSUER and TMCP_JWKS_URL are necessary in jwt mode")
        if not 0.0 <= self.trace_sample_rate <= 1.0:
            raise ValueError("TMCP_TRACE_SAMPLE_RATE must be between 0 and 1")
        return self

    @property
    def audience(self) -> str:
        return self.jwt_audience or self.resource_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
