"""``POST /v1/crew/owner/approvals/{approval_id}/decision``.

No approval waits on this gateway (see ``get_v1_crew_owner_approvals.py``), so there
is no case that answers one.
"""

from __future__ import annotations

from e2e.crew_api_contract_spec import APP, OWNER, Case

CASES = [
    Case(
        "an approval nobody asked for is not found",
        OWNER,
        404,
        "not_found",
        json={"decision": "deny"},
        path={"approval_id": "apr_" + "0" * 32},
    ),
    Case(
        "an app is refused on an Owner route",
        APP,
        403,
        "owner_only",
        json={"decision": "deny"},
    ),
]
