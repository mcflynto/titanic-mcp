"""Rolling update under load (spec section 14, demo step 5; DEP-04 / CON-05).

Sends MCP calls without a pause while 'kubectl rollout restart' replaces all pods. Prints the number of
failed calls. The expected result is 0.
"""

import asyncio
import subprocess
import time

import httpx2
from demo import CLIENT_LIMITS, MCP_URL, get_token  # scripts/ is on sys.path when run as a file
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

NAMESPACE = "titanic-mcp"


async def main() -> None:
    token = await get_token("research-agent")  # 120 calls/min per pod
    ok = failed = throttled = 0
    errors: list[str] = []
    restart = subprocess.Popen(
        ["kubectl", "-n", NAMESPACE, "rollout", "restart", "deploy/titanic-mcp"],
    )
    restart.wait()
    status = subprocess.Popen(
        ["kubectl", "-n", NAMESPACE, "rollout", "status", "deploy/titanic-mcp", "--timeout=5m"],
        stdout=subprocess.DEVNULL,
    )
    start = time.monotonic()
    while status.poll() is None or time.monotonic() - start < 10:
        try:
            # A new connection for each call, so that each call can reach a different pod.
            http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=10, limits=CLIENT_LIMITS)
            async with Client(streamable_http_client(MCP_URL, http_client=http)) as client:
                result = await client.call_tool("aggregate_passengers", {"group_by": ["class"]})
            if result.is_error and result.structured_content["error"]["code"] == "RATE_LIMITED":
                throttled += 1  # correct behavior of the rate limiter, not an availability failure
            elif result.is_error:
                failed += 1
                errors.append(str(result.structured_content)[:200])
            else:
                ok += 1
        except Exception as exc:
            failed += 1
            errors.append(f"{type(exc).__name__}: {exc}"[:200])
        print(
            f"\rcalls ok={ok} failed={failed} throttled={throttled}  ({time.monotonic() - start:.0f} s)",
            end="",
            flush=True,
        )
        await asyncio.sleep(0.2)
    print(f"\nRollout complete. Successful calls: {ok}. Failed calls: {failed}. Rate-limited calls: {throttled}.")
    for line in errors[:10]:
        print("  ", line)


if __name__ == "__main__":
    asyncio.run(main())
