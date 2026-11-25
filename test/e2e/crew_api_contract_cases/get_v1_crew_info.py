"""``GET /v1/crew/info``."""

from __future__ import annotations

from typing import Any

from e2e.crew_api_contract_spec import APP, OWNER, Case


def _is_this_local_instance(body: Any) -> None:
    assert body["id"] and body["form"] == "local", body


CASES = [
    Case("the owner reads the instance", OWNER, 200, check=_is_this_local_instance),
    Case("an app is refused while the route serves the owner", APP, 403, "owner_only"),
]
