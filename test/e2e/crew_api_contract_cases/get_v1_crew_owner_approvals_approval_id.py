"""``GET /v1/crew/owner/approvals/{approval_id}``."""

from __future__ import annotations

from typing import Any

from e2e.crew_api_contract_spec import APP, OWNER, Case


def _waits(body: Any) -> None:
    assert body["state"] == "pending" and body["decided_by"] is None, body
    # The wrapped backend's command, as the approval card shows it.
    assert "touch " in body["arguments"]["input"], body


def _was_allowed_by_the_owner(body: Any) -> None:
    assert body["state"] == "allowed" and body["decided_at"] is not None, body
    assert body["decided_by"]["kind"] == "owner", body


CASES = [
    Case(
        "the owner reads an approval that waits",
        OWNER,
        200,
        path=lambda gateway: {"approval_id": gateway.pending_approval()},
        check=_waits,
    ),
    Case(
        "the owner reads an approval that was answered",
        OWNER,
        200,
        path=lambda gateway: {"approval_id": gateway.decided_approval()},
        check=_was_allowed_by_the_owner,
    ),
    Case(
        "an approval nobody asked for is not found",
        OWNER,
        404,
        "not_found",
        path={"approval_id": "apr_" + "0" * 32},
    ),
    Case("an app is refused on an Owner route", APP, 403, "owner_only"),
]
