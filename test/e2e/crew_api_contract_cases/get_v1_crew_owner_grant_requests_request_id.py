"""``GET /v1/crew/owner/grant-requests/{request_id}``."""

from __future__ import annotations

from e2e.crew_api_contract_spec import APP, OWNER, Case

CASES = [
    Case(
        "the owner reads one grant request",
        OWNER,
        200,
        path=lambda gateway: {"request_id": gateway.pending_grant_request()},
    ),
    Case(
        "a grant request nobody made is not found",
        OWNER,
        404,
        "not_found",
        path={"request_id": "greq_unknown"},
    ),
    Case("an app is refused on an Owner route", APP, 403, "owner_only"),
]
