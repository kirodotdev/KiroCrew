"""``GET /v1/responses/{response_id}/input_items``."""

from __future__ import annotations

from typing import Any

from e2e.crew_api_contract_spec import APP, OWNER, Case


def _is_the_one_item_sent(body: Any) -> None:
    (item,) = body["data"]
    assert item["role"] == "user", body
    assert item["content"] == [{"type": "input_text", "text": "hello"}], body
    assert body["has_more"] is False, body


CASES = [
    Case(
        "the owner reads a turn's input back",
        OWNER,
        200,
        path=lambda gateway: {"response_id": gateway.kept_response()},
        check=_is_the_one_item_sent,
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
