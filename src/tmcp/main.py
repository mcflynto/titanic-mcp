"""FastAPI application: health probes, version, HTTP middleware and the mounted MCP endpoint."""

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
from prometheus_client import start_http_server
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from ulid import ULID

from tmcp import __version__
from tmcp.auth.verifiers import build_verifier
from tmcp.config import Settings, get_settings
from tmcp.db.reference import ReferenceDatabase
from tmcp.db.working import WorkingDatabase
from tmcp.mcp_server import build_mcp_server
from tmcp.observability import metrics
from tmcp.observability.context import client_ip_var, request_id_var
from tmcp.observability.logging import configure_logging, get_logger
from tmcp.observability.tracing import configure_tracing
from tmcp.policy.models import KNOWN_TOOLS
from tmcp.policy.store import PolicyStore
from tmcp.ratelimit import RateLimiter
from tmcp.tools.runtime import AppState, set_state

log = get_logger("tmcp")
GIT_COMMIT = os.environ.get("GIT_COMMIT", "unknown")
BUILD_DATE = os.environ.get("BUILD_DATE", "unknown")
_QUIET_PATHS = ("/healthz/live", "/healthz/ready")


class RequestContextMiddleware:
    """LOG-02: request ID for each request, client IP, access log, HTTP metrics. Pure ASGI (safe for streaming)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        incoming = headers.get("x-request-id", "")
        request_id = incoming if 0 < len(incoming) <= 128 and incoming.isprintable() else str(ULID())
        forwarded = headers.get("x-forwarded-for", "")
        client = forwarded.split(",")[0].strip() if forwarded else (scope.get("client") or ("", 0))[0]
        rid_token, ip_token = request_id_var.set(request_id), client_ip_var.set(client)
        status_code = 500
        start = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
            await send(message)

        path = scope.get("path", "")
        metrics.INFLIGHT.inc()
        try:
            await self.app(scope, receive, send_with_id)
        finally:
            metrics.INFLIGHT.dec()
            label = path if path in ("/mcp", *_QUIET_PATHS, "/version") else "other"
            metrics.HTTP_REQUESTS.labels(label, str(status_code)).inc()
            if path not in _QUIET_PATHS:
                log.info(
                    "http_request",
                    method=scope.get("method"),
                    path=path,
                    status=status_code,
                    duration_ms=round((time.perf_counter() - start) * 1000, 2),
                    client_ip=client,
                )
            request_id_var.reset(rid_token)
            client_ip_var.reset(ip_token)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json, service_version=__version__)
    tracer_provider = configure_tracing(settings, __version__)
    verifier = build_verifier(settings)
    mcp = build_mcp_server(settings, verifier)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,  # MCP-01: no session state, so any pod can serve any request
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,  # MCP-06
            allowed_hosts=settings.allowed_hosts,
            allowed_origins=settings.allowed_origins,
        ),
        host=settings.host,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        policy = PolicyStore(settings.policy_path)
        reference = ReferenceDatabase(settings.db_path, settings.db_sha256)
        working: WorkingDatabase | None = None
        if settings.working_db_url is not None:
            working = WorkingDatabase(
                settings.working_db_url.get_secret_value(),
                pool_size=settings.working_db_pool_size,
                ttl_hours=settings.change_request_ttl_hours,
            )
            await working.connect()
            rows = await working.bootstrap(settings.db_path)
            log.info("working_db_ready", rows=rows)
        set_state(AppState(settings, __version__, policy, reference, working, RateLimiter()))
        metrics.BUILD_INFO.labels(__version__, GIT_COMMIT, reference.sha256, policy.version).set(1)
        metrics.initialize_series(policy.known_agents, KNOWN_TOOLS)

        metrics_server = None
        if settings.metrics_port:
            metrics_server, _thread = start_http_server(settings.metrics_port)
        watcher = asyncio.create_task(policy.watch(settings.policy_reload_interval_s))
        log.info("server_started", env=settings.env, auth_mode=settings.auth_mode, version=__version__)
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            log.info("server_stopping")
            watcher.cancel()
            if working:
                await working.close()
            if metrics_server:
                metrics_server.shutdown()
            if tracer_provider:
                tracer_provider.shutdown()
            set_state(None)

    app = FastAPI(
        title="Titanic MCP Server",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/healthz/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/healthz/ready", include_in_schema=False)
    async def ready() -> JSONResponse:
        from tmcp.tools.runtime import state

        checks: dict[str, Any] = {}
        try:
            st = state()
        except Exception:
            return JSONResponse({"status": "starting"}, status_code=503)
        probes: dict[str, Any] = {"reference_db": st.reference.check(), "auth": verifier.check()}
        if st.working is not None:
            probes["working_db"] = st.working.check()
        for name, probe in probes.items():
            try:
                await asyncio.wait_for(probe, timeout=3)
                checks[name] = "ok"
            except Exception as exc:
                checks[name] = f"failed: {type(exc).__name__}"
        healthy = all(v == "ok" for v in checks.values())
        if not healthy:
            log.warning("readiness_failed", checks=checks)
        return JSONResponse({"status": "ok" if healthy else "unavailable", "checks": checks}, 200 if healthy else 503)

    @app.get("/version", include_in_schema=False)
    async def version() -> dict[str, Any]:
        from tmcp.tools.runtime import state

        st = state()
        return {
            "version": __version__,
            "commit": GIT_COMMIT,
            "build_date": BUILD_DATE,
            "data_sha256": st.reference.sha256,
            "policy_version": st.policy.version,
        }

    if tracer_provider is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider, excluded_urls="healthz")

    app.mount("/", mcp_app)  # /mcp and /.well-known/oauth-protected-resource/...
    app.add_middleware(RequestContextMiddleware)
    return app


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_keep_alive=settings.keep_alive_timeout_s,
        timeout_graceful_shutdown=20,  # CON-05
        server_header=False,
    )


if __name__ == "__main__":
    run()
