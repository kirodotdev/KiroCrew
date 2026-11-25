"""``GET /v1/crew/owner/approvals``.

No approval waits on this gateway, so the list the owner reads is held to the spec's
schema without a row in it. The packaged fake ACP backend asks for one tool only, and
a gateway in a clean environment answers that one itself.
"""

from __future__ import annotations

from e2e.crew_api_contract_spec import APP, OWNER, Case

CASES = [
    Case("the owner lists the approvals that wait", OWNER, 200),
    Case("an app is refused on an Owner route", APP, 403, "owner_only"),
]
