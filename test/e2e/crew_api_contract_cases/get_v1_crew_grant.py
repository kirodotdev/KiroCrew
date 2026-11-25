"""``GET /v1/crew/grant``."""

from __future__ import annotations

from typing import Any

from e2e.crew_api_contract_spec import APP, OWNER, Case


def _is_a_local_active_grant(body: Any) -> None:
    assert (body["object"], body["kind"], body["status"]) == ("grant", "local", "active"), body
    assert body["id"].startswith("grt_") and body["pause"] is None, body


CASES = [
    Case("an app reads its own grant", APP, 200, check=_is_a_local_active_grant),
    Case("the owner's own credential has no grant", OWNER, 404, "not_found"),
]
