"""``GET /v1/responses/{response_id}``, answered as one JSON body."""

from __future__ import annotations

from typing import Any

from e2e.crew_api_contract_spec import APP, OWNER, Case


def _is_the_turn_that_ran(body: Any) -> None:
    assert body["status"] == "completed", body
    assert any(item.get("type") == "message" for item in body["output"]), body


CASES = [
    Case(
        "the owner reads a turn back",
        OWNER,
        200,
        path=lambda gateway: {"response_id": gateway.kept_response()},
        check=_is_the_turn_that_ran,
    ),
    Case(
        "a response nobody ran is not found",
        OWNER,
        404,
        "not_found",
        path={"response_id": "resp_" + "0" * 32},
    ),
    Case("an app is refused while the route serves the owner alone", APP, 403, "owner_only"),
]
