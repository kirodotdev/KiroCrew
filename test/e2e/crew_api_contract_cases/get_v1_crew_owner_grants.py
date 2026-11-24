"""``GET /v1/crew/owner/grants``."""

from __future__ import annotations

from typing import Any

from e2e.crew_api_contract_spec import APP, OWNER, Case


def _lists_a_grant(body: Any) -> None:
    # A row must be there for the schema check to have held one.
    assert body["data"], body


CASES = [
    Case(
        "the owner lists the grants",
        OWNER,
        200,
        before=lambda gateway: gateway.app_grant(),
        check=_lists_a_grant,
    ),
    Case("an app is refused on an Owner route", APP, 403, "owner_only"),
]
