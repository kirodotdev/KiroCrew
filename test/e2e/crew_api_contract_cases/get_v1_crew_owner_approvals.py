"""``GET /v1/crew/owner/approvals``.

The approval that waits is raised by a turn of the wrapped fake backend
(``e2e/crew_api_contract_gated_engine.py``), whose tool call the gateway holds.
"""

from __future__ import annotations

from typing import Any

from e2e.crew_api_contract_spec import APP, OWNER, Case


def _lists_one_that_waits(body: Any) -> None:
    # A row must be there for the schema check to have held one.
    assert [row["state"] for row in body["data"]] == ["pending"], body


CASES = [
    Case(
        "the owner lists the approval that waits",
        OWNER,
        200,
        before=lambda gateway: gateway.pending_approval(),
        check=_lists_one_that_waits,
    ),
    Case("an app is refused on an Owner route", APP, 403, "owner_only"),
]
