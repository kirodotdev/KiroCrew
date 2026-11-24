"""``GET /v1/crew/owner/grants``."""

from __future__ import annotations

from e2e.crew_api_contract_spec import APP, OWNER, Case

CASES = [
    Case("the owner lists the grants", OWNER, 200),
    Case("an app is refused on an Owner route", APP, 403, "owner_only"),
]
