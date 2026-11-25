"""``GET /v1/crew/capabilities``."""

from __future__ import annotations

from typing import Any

from e2e.crew_api_contract_spec import APP, OWNER, Case


def _reports_no_feature_it_does_not_serve(body: Any) -> None:
    assert body["features"] and not any(body["features"].values()), body
    assert body["agents"], body


CASES = [
    Case(
        "the owner reads what this gateway serves",
        OWNER,
        200,
        check=_reports_no_feature_it_does_not_serve,
    ),
    Case("an app is refused while the route serves the owner", APP, 403, "owner_only"),
]
