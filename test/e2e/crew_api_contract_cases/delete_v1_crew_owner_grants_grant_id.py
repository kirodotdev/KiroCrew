"""``DELETE /v1/crew/owner/grants/{grant_id}``."""

from __future__ import annotations

from e2e.crew_api_contract_spec import APP, OWNER, Case

CASES = [
    Case(
        "the owner revokes a grant",
        OWNER,
        200,
        path=lambda gateway: {"grant_id": gateway.revocable_grant()},
    ),
    Case("a grant nobody has is not found", OWNER, 404, "not_found"),
    Case("an app is refused on an Owner route", APP, 403, "owner_only"),
]
