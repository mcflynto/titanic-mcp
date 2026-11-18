"""Presentation scenario for the demo environment (spec section 14, "Presentation demo").

Each agent gets an OAuth token from Keycloak (client credentials) and calls the MCP server with the official
MCP Python client. Run it after 'make demo':

    uv run python scripts/demo.py            # all steps
    uv run python scripts/demo.py --step 4   # one step
    uv run python scripts/demo.py --traffic 120   # background traffic for the Grafana dashboard
"""

import argparse
import asyncio
import json
import random
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

MCP_URL = "http://localhost:18080/mcp"
TOKEN_URL = "http://localhost:18081/realms/titanic/protocol/openid-connect/token"
AGENTS = ["reporting-agent", "churn-agent", "steerage-agent", "research-agent", "curation-agent", "review-agent"]

BOLD, DIM, GREEN, RED, YELLOW, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"


def header(text: str) -> None:
    print(f"\n{BOLD}{'=' * 100}\n{text}\n{'=' * 100}{RESET}")


def secret_for(agent: str) -> str:
    return f"{agent}-secret-change-me"  # placeholder secrets from deploy/kind/keycloak.yaml


async def get_token(agent: str) -> str:
    async with httpx2.AsyncClient(timeout=10) as http:
        response = await http.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials", "client_id": agent, "client_secret": secret_for(agent)},
        )
        response.raise_for_status()
        return str(response.json()["access_token"])


@asynccontextmanager
async def connect(agent: str) -> AsyncIterator[Client]:
    token = await get_token(agent)
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30)
    async with Client(streamable_http_client(MCP_URL, http_client=http)) as client:
        yield client


async def call(client: Client, agent: str, tool: str, args: dict[str, Any], *, rows: int = 8) -> dict[str, Any]:
    print(f"\n{DIM}{agent} -> {tool}({json.dumps(args)}){RESET}")
    result = await client.call_tool(tool, args)
    body: dict[str, Any] = result.structured_content or json.loads(result.content[0].text)
    if result.is_error:
        err = body["error"]
        print(f"  {RED}{err['code']}{RESET}: {err['message']}")
        if err.get("hint"):
            print(f"  {DIM}hint: {err['hint']} | request_id: {err.get('request_id')}{RESET}")
    elif "rows" in body:
        print(f"  {GREEN}OK{RESET} columns={body['columns']}")
        for row in body["rows"][:rows]:
            print(f"    {row}")
        extra = [f"{k}={body[k]}" for k in ("row_count", "truncated", "masked_columns") if k in body]
        print(f"  {DIM}{' '.join(extra)}{RESET}")
    else:
        compact = {k: body[k] for k in ("id", "operation", "status", "passenger_id", "payload", "result") if k in body}
        print(f"  {GREEN}OK{RESET} {json.dumps(compact or body)[:300]}")
    return body


async def step_1() -> None:
    header("STEP 1 - Each agent sees only the tools that its roles permit (MCP-03)")
    for agent in AGENTS:
        async with connect(agent) as client:
            tools = [t.name for t in (await client.list_tools()).tools]
            print(f"{BOLD}{agent:16}{RESET} {tools}")


async def step_2() -> None:
    header("STEP 2 - The statistician gets survival rates, but cannot read single rows")
    async with connect("reporting-agent") as client:
        await call(client, "reporting-agent", "aggregate_passengers",
                   {"group_by": ["class", "sex"], "metrics": [{"fn": "avg", "column": "survived"}]})  # fmt: skip
        await call(client, "reporting-agent", "query_passengers", {"limit": 3})
        print(f"\n{YELLOW}k-anonymity: groups with fewer than 5 passengers are removed:{RESET}")
        await call(client, "reporting-agent", "aggregate_passengers", {"group_by": ["deck", "class", "sex"]}, rows=30)


async def step_3() -> None:
    header("STEP 3 - Column and row rules: 'fare' is masked, the steerage agent sees only third class")
    async with connect("churn-agent") as client:
        await call(client, "churn-agent", "query_passengers",
                   {"filters": [{"column": "age", "op": "<", "value": 1}], "limit": 5})  # fmt: skip
        await call(client, "churn-agent", "query_passengers", {"columns": ["fare"], "limit": 1})
    async with connect("steerage-agent") as client:
        await call(client, "steerage-agent", "aggregate_passengers",
                   {"group_by": ["class"], "metrics": [{"fn": "avg", "column": "survived"}]})  # fmt: skip


async def step_4() -> None:
    header("STEP 4 - Free SQL is guarded: injection attempts are rejected, audited and alerted")
    async with connect("research-agent") as client:
        await call(client, "research-agent", "run_sql",
                   {"sql": "SELECT class, round(avg(survived), 3) AS survival_rate, count(*) AS n "
                           "FROM passengers GROUP BY class"})  # fmt: skip
        for sql in [
            "SELECT * FROM passengers; DROP TABLE Observation",
            "SELECT name, sql FROM sqlite_master",
            "ATTACH DATABASE '/tmp/x.db' AS x",
            "SELECT load_extension('/tmp/evil.so')",
        ]:
            await call(client, "research-agent", "run_sql", {"sql": sql})
    print(f"\n{YELLOW}Audit log:{RESET} kubectl -n titanic-mcp logs -l app.kubernetes.io/name=titanic-mcp "
          "--prefix | grep '\"audit\"' | grep QUERY_REJECTED")  # fmt: skip
    print(f"{YELLOW}Alert:{RESET} TmcpSqlRejected in Grafana http://localhost:13000 (fires within about 30 s)")


async def step_5() -> None:
    header("STEP 5 - Write access with approval (OP-01): propose, four-eyes rule, approve")
    async with connect("curation-agent") as editor, connect("review-agent") as approver:
        target = random.randint(1, 891)
        await call(editor, "curation-agent", "query_passengers",
                   {"dataset": "working", "columns": ["passenger_id", "age", "sex", "class"],
                    "filters": [{"column": "passenger_id", "op": "=", "value": target}]})  # fmt: skip
        cr = await call(editor, "curation-agent", "propose_passenger_update",
                        {"passenger_id": target, "changes": {"age": 42},
                         "reason": "Age confirmed by the passenger manifest (demo)."})  # fmt: skip
        await call(editor, "curation-agent", "review_change_request",
                   {"change_request_id": cr["id"], "decision": "approve"})  # fmt: skip
        await call(approver, "review-agent", "review_change_request",
                   {"change_request_id": cr["id"], "decision": "approve", "comment": "Source checked."})  # fmt: skip
        query = {
            "columns": ["passenger_id", "age"],
            "filters": [{"column": "passenger_id", "op": "=", "value": target}],
        }
        await call(approver, "review-agent", "query_passengers", {"dataset": "working", **query})
        print(f"\n{YELLOW}The reference dataset does not change:{RESET}")
        await call(approver, "review-agent", "query_passengers", {"dataset": "reference", **query})


async def traffic(seconds: int) -> None:
    header(f"Traffic for {seconds} s (for the Grafana dashboard). Stop with Ctrl+C.")
    calls = [
        (
            "reporting-agent",
            "aggregate_passengers",
            {"group_by": ["class"], "metrics": [{"fn": "avg", "column": "survived"}]},
        ),
        (
            "churn-agent",
            "query_passengers",
            {"filters": [{"column": "sex", "op": "=", "value": "female"}], "limit": 20},
        ),
        ("churn-agent", "query_passengers", {"columns": ["fare"]}),
        ("research-agent", "run_sql", {"sql": "SELECT embark_town, count(*) FROM passengers GROUP BY 1"}),
        ("research-agent", "run_sql", {"sql": "SELECT * FROM sqlite_master"}),
        ("steerage-agent", "describe_table", {"table": "passengers"}),
        ("reporting-agent", "run_sql", {"sql": "SELECT 1"}),
    ]
    end, count = time.monotonic() + seconds, 0
    # AsyncExitStack closes the clients in reverse order. The MCP client uses anyio task groups,
    # which must exit in the reverse order of entry (LIFO), in the same task.
    async with AsyncExitStack() as stack:
        clients: dict[str, Client] = {}
        for agent in sorted({c[0] for c in calls}):
            clients[agent] = await stack.enter_async_context(connect(agent))
        try:
            while time.monotonic() < end:
                agent, tool, args = random.choice(calls)
                await clients[agent].call_tool(tool, args)
                count += 1
                if count % 50 == 0:
                    print(f"  {count} calls sent", flush=True)
                await asyncio.sleep(random.uniform(0.05, 0.3))
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("Stopped.")
    print(f"{count} calls sent.")


STEPS = {1: step_1, 2: step_2, 3: step_3, 4: step_4, 5: step_5}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--step", type=int, choices=sorted(STEPS), help="run only this step")
    parser.add_argument("--traffic", type=int, metavar="SECONDS", help="send random traffic")
    args = parser.parse_args()
    if args.traffic:
        await traffic(args.traffic)
        return
    for number, step in STEPS.items():
        if args.step in (None, number):
            await step()
    print(f"\n{BOLD}Grafana: http://localhost:13000  Jaeger: http://localhost:16686  "
          f"Prometheus: http://localhost:19090{RESET}")  # fmt: skip


if __name__ == "__main__":
    asyncio.run(main())
