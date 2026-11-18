"""End-to-end tests over HTTP with the official MCP client (section 5, 6, 8)."""

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp.shared.exceptions import MCPError

from tests.conftest import TEST_API_KEYS, RunningServer, _free_port, make_settings

ARGS: dict[str, dict[str, Any]] = {
    "list_tables": {},
    "describe_table": {"table": "passengers"},
    "get_sample_rows": {"table": "passengers", "limit": 2},
    "query_passengers": {"limit": 2},
    "aggregate_passengers": {"group_by": ["class"]},
    "run_sql": {"sql": "SELECT count(*) AS n FROM passengers"},
}

# TST-02: permission matrix (agent -> tools that must be allowed). All other read tools must be denied.
MATRIX = {
    "reporting-agent": {"list_tables", "describe_table", "aggregate_passengers"},
    "churn-agent": {"list_tables", "describe_table", "get_sample_rows", "query_passengers", "aggregate_passengers"},
    "steerage-agent": {"list_tables", "describe_table", "get_sample_rows", "query_passengers", "aggregate_passengers"},
    "research-agent": set(ARGS),
    "unknown-agent": set(),
}


@pytest.mark.parametrize("agent", list(MATRIX))
@pytest.mark.parametrize("tool", list(ARGS))
async def test_tst02_permission_matrix(server: RunningServer, agent: str, tool: str) -> None:
    # 'outsider-agent' has a valid API key but is not in the policy: deny by default.
    async with server.client("outsider-agent" if agent == "unknown-agent" else agent) as client:
        result = await client.call_tool(tool, ARGS[tool])
    allowed = tool in MATRIX[agent]
    assert result.is_error is (not allowed), result.structured_content
    if not allowed:
        assert result.structured_content["error"]["code"] == "PERMISSION_DENIED"


@pytest.mark.parametrize("agent", ["reporting-agent", "churn-agent", "research-agent", "curation-agent"])
async def test_mcp03_tools_list_shows_only_permitted_tools(server: RunningServer, agent: str) -> None:
    async with server.client(agent) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    perms = server_policy_tools(agent)
    assert names == perms


def server_policy_tools(agent: str) -> set[str]:
    from tests.conftest import POLICY_PATH
    from tmcp.policy.store import PolicyStore

    return set(PolicyStore(POLICY_PATH).permissions(agent).tools)


async def test_mcp04_mcp05_schemas_and_annotations(server: RunningServer) -> None:
    async with server.client("research-agent") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    for tool in tools.values():
        assert tool.input_schema and tool.output_schema, tool.name
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
        assert tool.description


async def test_auth04_missing_or_invalid_token_gives_401(server: RunningServer) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    async with httpx2.AsyncClient() as http:
        for extra in ({}, {"Authorization": "Bearer wrong"}):
            response = await http.post(f"{server.url}/mcp", json=body, headers=headers | extra)
            assert response.status_code == 401
            assert "resource_metadata" in response.headers["www-authenticate"]
        metadata = await http.get(f"{server.url}/.well-known/oauth-protected-resource/mcp")
        assert metadata.status_code == 200 and metadata.json()["resource"].endswith("/mcp")


async def test_mcp06_unknown_origin_is_rejected(server: RunningServer) -> None:
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "Authorization": "Bearer dev-key-churn-agent",
        "Origin": "http://evil.example",
    }
    async with httpx2.AsyncClient() as http:
        response = await http.post(
            f"{server.url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=headers
        )
    assert response.status_code == 403


async def test_log02_request_id_header(server: RunningServer) -> None:
    async with httpx2.AsyncClient() as http:
        response = await http.get(f"{server.url}/healthz/live", headers={"X-Request-ID": "demo-123"})
    assert response.headers["x-request-id"] == "demo-123"


async def test_health_and_version(server: RunningServer) -> None:
    async with httpx2.AsyncClient() as http:
        ready = await http.get(f"{server.url}/healthz/ready")
        version = await http.get(f"{server.url}/version")
    assert ready.status_code == 200 and ready.json()["checks"]["reference_db"] == "ok"
    assert version.json()["data_sha256"].startswith("aa9cf768")


async def test_statistician_gets_survival_rates_with_k_anonymity(server: RunningServer) -> None:
    async with server.client("reporting-agent") as client:
        result = await client.call_tool(
            "aggregate_passengers",
            {"group_by": ["class", "sex"], "metrics": [{"fn": "avg", "column": "survived"}]},
        )
        small = await client.call_tool("aggregate_passengers", {"group_by": ["deck", "class", "embarked", "sex"]})
    rows = {(r[0], r[1]): r[3] for r in result.structured_content["rows"]}
    assert rows[("First", "female")] == pytest.approx(0.968085)
    assert all(r[-1] >= 5 for r in small.structured_content["rows"])


async def test_row_filter_limits_rows(server: RunningServer) -> None:
    async with server.client("steerage-agent") as client:
        result = await client.call_tool("aggregate_passengers", {"group_by": ["class"]})
    assert result.structured_content["rows"] == [["Third", 491]]


async def test_masked_column_is_reported(server: RunningServer) -> None:
    async with server.client("churn-agent") as client:
        result = await client.call_tool("query_passengers", {"limit": 1})
        denied = await client.call_tool("query_passengers", {"columns": ["fare"]})
    assert result.structured_content["masked_columns"] == ["fare"]
    assert "fare" not in result.structured_content["columns"]
    assert denied.structured_content["error"]["code"] == "PERMISSION_DENIED"


async def test_truncated_flag(server: RunningServer) -> None:
    async with server.client("churn-agent") as client:
        result = await client.call_tool("query_passengers", {"limit": 3, "columns": ["passenger_id"]})
    assert result.structured_content["row_count"] == 3 and result.structured_content["truncated"] is True


async def test_error_model_has_code_hint_and_request_id(server: RunningServer) -> None:
    async with server.client("research-agent") as client:
        result = await client.call_tool("run_sql", {"sql": "SELECT * FROM sqlite_master"})
    error = result.structured_content["error"]
    assert error["code"] == "QUERY_REJECTED" and error["request_id"]
    assert json.loads(result.content[0].text)["error"]["code"] == "QUERY_REJECTED"


async def test_resources_and_prompt(server: RunningServer) -> None:
    async with server.client("reporting-agent") as client:
        uris = {str(r.uri) for r in (await client.list_resources()).resources}
        me = json.loads((await client.read_resource("titanic://me")).contents[0].text)
        prompt = await client.get_prompt("analyze_survival", {"dimensions": "class"})
    assert uris == {"titanic://schema", "titanic://dataset/info", "titanic://me"}
    assert me["agent_id"] == "reporting-agent" and me["min_group_size"] == 5
    assert "aggregate_passengers" in prompt.messages[0].content.text


async def test_resource_permission_is_enforced(tmp_path: Path) -> None:
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "version: 1\nroles:\n  e: {tools: [list_change_requests]}\nagents:\n  churn-agent: {roles: [e]}\n"
    )
    port = _free_port()
    with RunningServer(make_settings(port, policy_path=policy, api_keys_file=TEST_API_KEYS)) as restricted:
        async with restricted.client("churn-agent") as client:
            uris = {str(r.uri) for r in (await client.list_resources()).resources}
            assert uris == {"titanic://me"}
            with pytest.raises(MCPError, match="PERMISSION_DENIED"):
                await client.read_resource("titanic://schema")
            assert {t.name for t in (await client.list_tools()).tools} == {"list_change_requests"}


async def test_working_dataset_unavailable_without_postgres(server: RunningServer) -> None:
    async with server.client("curation-agent") as client:
        result = await client.call_tool("query_passengers", {"dataset": "working", "limit": 1})
    assert result.structured_content["error"]["code"] == "DATABASE_UNAVAILABLE"


async def test_invalid_arguments_are_tool_errors(server: RunningServer) -> None:
    async with server.client("churn-agent") as client:
        result = await client.call_tool("query_passengers", {"limit": "many"})
    assert result.is_error


async def test_unknown_tool_is_protocol_error(server: RunningServer) -> None:
    async with server.client("churn-agent") as client:
        result = await client.call_tool("drop_everything", {})
    assert result.is_error or isinstance(result, MCPError)
