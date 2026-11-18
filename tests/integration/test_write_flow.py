"""Write workflow on the working dataset (OP-01). Needs Docker for a PostgreSQL container."""

import pytest

from tests.conftest import DB_PATH, RunningServer
from tmcp.db.working import WorkingDatabase
from tmcp.errors import AppError, ErrorCode

pytestmark = pytest.mark.postgres

NEW_PASSENGER = {
    "survived": 1, "pclass": 2, "sex": "female", "age": 30, "sibsp": 0, "parch": 0, "fare": 13.0,
    "embarked": "S", "who": "woman", "adult_male": 0, "alone": 1,
}  # fmt: skip


async def test_op01_update_needs_approval_by_second_agent(server_with_working: RunningServer) -> None:
    async with (
        server_with_working.client("curation-agent") as editor,
        server_with_working.client("review-agent") as approver,
    ):
        proposed = await editor.call_tool(
            "propose_passenger_update",
            {"passenger_id": 2, "changes": {"age": 39}, "reason": "Corrected from the passenger list."},
        )
        cr = proposed.structured_content
        assert cr["status"] == "pending" and cr["before"]["age"] == 38.0

        before = await approver.call_tool(
            "query_passengers",
            {"dataset": "working", "columns": ["age"], "filters": [{"column": "passenger_id", "op": "=", "value": 2}]},
        )
        assert before.structured_content["rows"] == [[38.0]]  # not changed before the approval

        self_review = await editor.call_tool(
            "review_change_request", {"change_request_id": cr["id"], "decision": "approve"}
        )
        assert self_review.structured_content["error"]["code"] == "PERMISSION_DENIED"

        approved = await approver.call_tool(
            "review_change_request", {"change_request_id": cr["id"], "decision": "approve"}
        )
        assert approved.structured_content["status"] == "applied"

        after = await approver.call_tool(
            "query_passengers",
            {"dataset": "working", "columns": ["age"], "filters": [{"column": "passenger_id", "op": "=", "value": 2}]},
        )
        assert after.structured_content["rows"] == [[39.0]]

        reference = await approver.call_tool(
            "query_passengers", {"columns": ["age"], "filters": [{"column": "passenger_id", "op": "=", "value": 2}]}
        )
        assert reference.structured_content["rows"] == [[38.0]]  # reference data never changes


async def test_op01_conflict_when_row_changed(server_with_working: RunningServer) -> None:
    async with (
        server_with_working.client("curation-agent") as editor,
        server_with_working.client("review-agent") as approver,
    ):
        first = (
            await editor.call_tool(
                "propose_passenger_update", {"passenger_id": 3, "changes": {"age": 27}, "reason": "First correction."}
            )
        ).structured_content
        second = (
            await editor.call_tool(
                "propose_passenger_update", {"passenger_id": 3, "changes": {"age": 28}, "reason": "Second correction."}
            )
        ).structured_content
        await approver.call_tool("review_change_request", {"change_request_id": first["id"], "decision": "approve"})
        result = await approver.call_tool(
            "review_change_request", {"change_request_id": second["id"], "decision": "approve"}
        )
    assert result.structured_content["status"] == "conflict"


async def test_op01_insert_delete_reject_and_validation(server_with_working: RunningServer) -> None:
    async with (
        server_with_working.client("curation-agent") as editor,
        server_with_working.client("review-agent") as approver,
    ):
        bad = await editor.call_tool(
            "propose_passenger_insert", {"values": NEW_PASSENGER | {"who": "man"}, "reason": "Invalid example row."}
        )
        assert bad.is_error

        inserted = (
            await editor.call_tool(
                "propose_passenger_insert", {"values": NEW_PASSENGER, "reason": "Missing passenger."}
            )
        ).structured_content
        applied = (
            await approver.call_tool(
                "review_change_request", {"change_request_id": inserted["id"], "decision": "approve"}
            )
        ).structured_content
        new_id = applied["result"]["passenger_id"]
        assert new_id > 891

        deletion = (
            await editor.call_tool(
                "propose_passenger_delete", {"passenger_id": new_id, "reason": "Duplicate row found."}
            )
        ).structured_content
        rejected = (
            await approver.call_tool(
                "review_change_request",
                {"change_request_id": deletion["id"], "decision": "reject", "comment": "Not a duplicate."},
            )
        ).structured_content
        assert rejected["status"] == "rejected"

        own = (await editor.call_tool("list_change_requests", {})).structured_content
        assert own["row_count"] >= 2 and all(c["requested_by"] == "curation-agent" for c in own["change_requests"])

        missing = await editor.call_tool(
            "propose_passenger_delete", {"passenger_id": 999_999, "reason": "Does not exist."}
        )
        assert missing.structured_content["error"]["code"] == "NOT_FOUND"


async def test_four_eyes_rule_in_database_layer(postgres_url: str) -> None:
    db = WorkingDatabase(postgres_url, pool_size=2, ttl_hours=1)
    await db.connect(attempts=5)
    try:
        await db.bootstrap(DB_PATH)
        cr = await db.create_change_request(
            operation="delete", passenger_id=5, payload={}, reason="test", requested_by="same-agent"
        )
        with pytest.raises(AppError) as info:
            await db.review_change_request(cr["id"], reviewer="same-agent", approve=True, comment=None)
        assert info.value.code == ErrorCode.PERMISSION_DENIED
        assert await db.bootstrap(DB_PATH) >= 891  # idempotent: a second replica does not copy again
    finally:
        await db.close()
