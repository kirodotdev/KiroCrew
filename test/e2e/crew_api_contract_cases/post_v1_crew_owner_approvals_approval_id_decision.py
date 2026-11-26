"""``POST /v1/crew/owner/approvals/{approval_id}/decision``."""

from __future__ import annotations

from typing import Any

from e2e.crew_api_contract_spec import APP, OWNER, Case

# The approval the app's case raised, and the gateway it waits on: the check
# after the app's refusal reads its state again.
_raised: dict[str, Any] = {}


def _one_that_waits(gateway: Any) -> dict[str, str]:
    _raised.update(gateway=gateway, id=gateway.pending_approval())
    return {"approval_id": _raised["id"]}


def _still_waits(_body: Any) -> None:
    assert _raised["gateway"].approval_state(_raised["id"]) == "pending"


def _is(state: str) -> Any:
    def check(body: Any) -> None:
        assert body["state"] == state and body["decided_at"] is not None, body
        assert body["decided_by"]["kind"] == "owner", body

    return check


CASES = [
    Case(
        "the owner allows an approval that waits",
        OWNER,
        200,
        json={"decision": "allow"},
        path=_one_that_waits,
        check=_is("allowed"),
    ),
    Case(
        "the owner denies an approval that waits",
        OWNER,
        200,
        json={"decision": "deny"},
        path=_one_that_waits,
        check=_is("denied"),
    ),
    Case(
        "an approval already answered is refused",
        OWNER,
        409,
        "approval_already_resolved",
        json={"decision": "deny"},
        path=lambda gateway: {"approval_id": gateway.decided_approval()},
    ),
    Case(
        "a decision the API does not know is refused",
        OWNER,
        400,
        "invalid_request",
        json={"decision": "maybe"},
        path=_one_that_waits,
    ),
    Case(
        "an approval nobody asked for is not found",
        OWNER,
        404,
        "not_found",
        json={"decision": "deny"},
        path={"approval_id": "apr_" + "0" * 32},
    ),
    Case(
        "an app cannot answer an approval that waits",
        APP,
        403,
        "owner_only",
        json={"decision": "allow"},
        path=_one_that_waits,
        check=_still_waits,
    ),
]
