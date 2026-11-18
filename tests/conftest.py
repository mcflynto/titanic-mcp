"""Shared fixtures. Integration tests start the real server (uvicorn) and call it with the official MCP client."""

import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from tmcp.config import Settings
from tmcp.db.reference import ReferenceDatabase
from tmcp.main import create_app
from tmcp.policy.store import PolicyStore

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "titanic.db"
POLICY_PATH = ROOT / "policy" / "policy.yaml"
API_KEYS = ROOT / "deploy" / "compose" / "api-keys.yaml"
TEST_API_KEYS = ROOT / "tests" / "fixtures" / "api-keys.yaml"
DB_SHA256 = "aa9cf7683387fefae3759a6d51fd03c917836fdc9c61299ffea15de5bf5f327d"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def policy_store() -> PolicyStore:
    return PolicyStore(POLICY_PATH)


@pytest.fixture(scope="session")
def reference_db() -> ReferenceDatabase:
    return ReferenceDatabase(DB_PATH, DB_SHA256)


def _docker() -> str | None:
    for candidate in (shutil.which("docker"), os.path.expanduser("~/.docker/bin/docker")):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """A throw-away PostgreSQL container for the working dataset. Set TMCP_TEST_PG_URL to use your own."""
    if url := os.environ.get("TMCP_TEST_PG_URL"):
        yield url
        return
    docker = _docker()
    if docker is None:
        pytest.skip("Docker is not available")
    name, port = f"tmcp-test-pg-{uuid.uuid4().hex[:8]}", _free_port()
    subprocess.run(
        [docker, "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=test", "-e", "POSTGRES_USER=test",
         "-e", "POSTGRES_DB=test", "-p", f"{port}:5432", "postgres:17-alpine"],
        check=True, capture_output=True,
    )  # fmt: skip
    try:
        for _ in range(60):
            ready = subprocess.run(
                [docker, "exec", name, "pg_isready", "-U", "test", "-h", "127.0.0.1"], capture_output=True
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        yield f"postgresql://test:test@127.0.0.1:{port}/test"
    finally:
        subprocess.run([docker, "rm", "-f", name], capture_output=True)


class RunningServer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.port = settings.port
        self.url = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(create_app(settings), host="127.0.0.1", port=self.port, log_config=None, lifespan="on")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "RunningServer":
        self.thread.start()
        for _attempt in range(200):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("server did not start")

    def __exit__(self, *_: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    def client(self, agent: str | None, *, key: str | None = None) -> Client:
        headers = {}
        if agent or key:
            headers["Authorization"] = f"Bearer {key or f'dev-key-{agent}'}"
        http = httpx2.AsyncClient(headers=headers, timeout=30)
        return Client(streamable_http_client(f"{self.url}/mcp", http_client=http))


def make_settings(port: int, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "env": "development",
        "port": port,
        "metrics_port": 0,
        "db_path": DB_PATH,
        "db_sha256": DB_SHA256,
        "policy_path": POLICY_PATH,
        "auth_mode": "api_key",
        "api_keys_file": TEST_API_KEYS,
        "resource_url": f"http://127.0.0.1:{port}/mcp",
        "allowed_hosts": ["127.0.0.1:*", "localhost:*"],
        "allowed_origins": ["http://127.0.0.1:*", "http://localhost:*"],
        "log_json": True,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def server() -> Iterator[RunningServer]:
    port = _free_port()
    with RunningServer(make_settings(port)) as running:
        yield running


@pytest.fixture(scope="session")
def server_with_working(postgres_url: str) -> Iterator[RunningServer]:
    port = _free_port()
    from pydantic import SecretStr

    with RunningServer(make_settings(port, working_db_url=SecretStr(postgres_url))) as running:
        yield running


@pytest.fixture
async def agent_client(server: RunningServer) -> AsyncIterator[object]:
    clients: list[Client] = []

    async def _open(agent: str) -> Client:
        client = server.client(agent)
        await client.__aenter__()
        clients.append(client)
        return client

    yield _open
    for c in clients:
        await c.__aexit__(None, None, None)
