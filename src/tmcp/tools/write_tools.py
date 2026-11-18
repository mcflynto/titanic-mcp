"""Write tools for the working dataset (OP-01).

An agent with the role 'editor' proposes a change. A different agent with the role 'approver' approves or
rejects it. Only an approval changes the data. The reference dataset never changes.
"""

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field, ValidationError

from tmcp.errors import AppError, ErrorCode, denied, invalid
from tmcp.policy.engine import Permissions
from tmcp.tools.models import ChangeRequest, ChangeRequestList, PassengerChanges, PassengerValues
from tmcp.tools.runtime import governed_tool, state

Reason = Annotated[str, Field(min_length=10, max_length=1000, description="Why the change is necessary.")]
PassengerId = Annotated[int, Field(ge=1, description="passenger_id in the working dataset.")]
ChangeRequestId = Annotated[str, Field(min_length=26, max_length=26, description="ID of the change request.")]


def _can_see(perms: Permissions, cr: dict[str, Any]) -> bool:
    return perms.can("review_change_request") or cr["requested_by"] == perms.agent_id


def _validation_message(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'values'}: {e['msg']}" for e in exc.errors())


def register(server: MCPServer) -> None:
    @governed_tool(
        server, name="propose_passenger_insert", title="Propose a new passenger", output=ChangeRequest, read_only=False
    )
    async def propose_passenger_insert(perms: Permissions, values: PassengerValues, reason: Reason) -> ChangeRequest:
        """Propose a new passenger row in the working dataset. The row is added only after a different
        agent with the role 'approver' approves the change request."""
        cr = (
            await state()
            .require_working()
            .create_change_request(
                operation="insert",
                passenger_id=None,
                payload=values.model_dump(),
                reason=reason,
                requested_by=perms.agent_id,
            )
        )
        return ChangeRequest.model_validate(cr)

    @governed_tool(
        server, name="propose_passenger_update", title="Propose a passenger change", output=ChangeRequest,
        read_only=False,
    )  # fmt: skip
    async def propose_passenger_update(
        perms: Permissions, passenger_id: PassengerId, changes: PassengerChanges, reason: Reason
    ) -> ChangeRequest:
        """Propose a change to one passenger in the working dataset. Give only the columns to change.
        The server keeps a copy of the current row; if the row changes before the approval, the
        change request gets the status 'conflict'."""
        working = state().require_working()
        payload = changes.model_dump(exclude_none=True, exclude={"clear"})
        payload |= dict.fromkeys(changes.clear)
        if not payload:
            raise invalid("Give one or more columns to change.")
        current = await working.fetch(
            "SELECT survived, pclass, sex, age, sibsp, parch, fare, embarked, deck, who, adult_male, alone "
            "FROM passengers WHERE passenger_id = %(p0)s",
            {"p0": passenger_id},
            max_rows=1,
            timeout_s=perms.limits.query_timeout_s,
        )
        if not current.rows:
            raise AppError(ErrorCode.NOT_FOUND, f"Passenger {passenger_id} does not exist in the working data.")
        merged = dict(zip(current.columns, current.rows[0], strict=True)) | payload
        try:
            PassengerValues.model_validate(merged)
        except ValidationError as exc:
            raise invalid(f"The changed row is not valid: {_validation_message(exc)}") from exc
        cr = await working.create_change_request(
            operation="update", passenger_id=passenger_id, payload=payload, reason=reason,
            requested_by=perms.agent_id,
        )  # fmt: skip
        return ChangeRequest.model_validate(cr)

    @governed_tool(
        server, name="propose_passenger_delete", title="Propose to delete a passenger", output=ChangeRequest,
        read_only=False, destructive=True,
    )  # fmt: skip
    async def propose_passenger_delete(perms: Permissions, passenger_id: PassengerId, reason: Reason) -> ChangeRequest:
        """Propose to delete one passenger from the working dataset. A different agent must approve it."""
        cr = (
            await state()
            .require_working()
            .create_change_request(
                operation="delete", passenger_id=passenger_id, payload={}, reason=reason, requested_by=perms.agent_id
            )
        )
        return ChangeRequest.model_validate(cr)

    @governed_tool(server, name="list_change_requests", title="List change requests", output=ChangeRequestList)
    async def list_change_requests(
        perms: Permissions,
        status: Literal["pending", "applied", "rejected", "conflict", "expired"] | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> ChangeRequestList:
        """List change requests, newest first. Approvers see all change requests; other agents see only
        their own. Pending requests expire after the configured time."""
        requested_by = None if perms.can("review_change_request") else perms.agent_id
        items = (
            await state().require_working().list_change_requests(status=status, requested_by=requested_by, limit=limit)
        )
        return ChangeRequestList(change_requests=[ChangeRequest.model_validate(i) for i in items], row_count=len(items))

    @governed_tool(server, name="get_change_request", title="Get a change request", output=ChangeRequest)
    async def get_change_request(perms: Permissions, change_request_id: ChangeRequestId) -> ChangeRequest:
        """Show one change request with its status and result."""
        cr = await state().require_working().get_change_request(change_request_id)
        if not _can_see(perms, cr):
            raise denied("You can see only your own change requests.")
        return ChangeRequest.model_validate(cr)

    @governed_tool(
        server, name="review_change_request", title="Approve or reject a change request", output=ChangeRequest,
        read_only=False, destructive=True,
    )  # fmt: skip
    async def review_change_request(
        perms: Permissions,
        change_request_id: ChangeRequestId,
        decision: Literal["approve", "reject"],
        comment: Annotated[str | None, Field(max_length=1000)] = None,
    ) -> ChangeRequest:
        """Approve or reject a pending change request. An approval applies the change to the working
        dataset in the same transaction. You cannot review your own change requests (four-eyes rule)."""
        cr = (
            await state()
            .require_working()
            .review_change_request(
                change_request_id, reviewer=perms.agent_id, approve=decision == "approve", comment=comment
            )
        )
        return ChangeRequest.model_validate(cr)
