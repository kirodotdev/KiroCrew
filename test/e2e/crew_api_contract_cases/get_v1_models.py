"""``GET /v1/models``."""

from __future__ import annotations

from typing import Any

from e2e.crew_api_contract_spec import APP, OWNER, Case


def _lists_an_agent(body: Any) -> None:
    assert any(row.get("crew", {}).get("kind") == "agent" for row in body["data"]), body


CASES = [
    Case("the owner lists the models", OWNER, 200, check=_lists_an_agent),
    Case("an app lists the models its grant allows", APP, 200),
]
