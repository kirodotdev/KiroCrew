"""``GET /v1/crew/owner/grant-requests``."""

from __future__ import annotations

from typing import Any

from e2e.crew_api_contract_spec import APP, OWNER, Case


def _lists_a_request(body: Any) -> None:
    # A row must be there for the schema check to have held one.
    assert body["data"], body


CASES = [
    Case(
        "the owner lists the grant requests",
        OWNER,
        200,
        before=lambda gateway: gateway.pending_grant_request(),
        check=_lists_a_request,
    ),
    Case("an app is refused on an Owner route", APP, 403, "owner_only"),
]
