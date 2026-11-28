"""``POST /v1/crew/owner/grant-requests/{request_id}/decision``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from e2e.crew_api_contract_spec import APP, OWNER, Case


def _pending(gateway: Any) -> dict[str, str]:
    return {"request_id": gateway.pending_grant_request()}


def _answered(gateway: Any) -> dict[str, str]:
    return {"request_id": gateway.answered_grant_request()}


def _is(status: str) -> Callable[[Any], None]:
    def check(body: Any) -> None:
        assert body["status"] == status, body

    return check


CASES = [
    Case(
        "the owner approves a grant request",
        OWNER,
        200,
        json={"decision": "approve"},
        path=_pending,
        check=_is("approved"),
    ),
    Case(
        "the owner denies a grant request",
        OWNER,
        200,
        json={"decision": "deny"},
        path=_pending,
        check=_is("denied"),
    ),
    Case(
        "a decision the API does not know is refused",
        OWNER,
        400,
        "invalid_request",
        json={"decision": "maybe"},
        path=_pending,
    ),
    Case(
        # The app asked for responses:write and conversations:read.
        "an approval that names a scope the app did not ask for is refused",
        OWNER,
        400,
        "invalid_scope",
        json={"decision": "approve", "scopes": ["bots:chat"]},
        path=_pending,
    ),
    Case(
        "a grant request already answered is refused",
        OWNER,
        409,
        "grant_request_already_resolved",
        json={"decision": "approve"},
        path=_answered,
    ),
    Case(
        "a grant request nobody made is not found",
        OWNER,
        404,
        "not_found",
        json={"decision": "deny"},
        path={"request_id": "greq_unknown"},
    ),
    Case(
        "an app is refused on an Owner route",
        APP,
        403,
        "owner_only",
        json={"decision": "deny"},
    ),
]
