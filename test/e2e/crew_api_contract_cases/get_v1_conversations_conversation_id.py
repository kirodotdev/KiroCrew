"""``GET /v1/conversations/{conversation_id}``: a conversation read back."""

from __future__ import annotations

from typing import Any

from e2e.crew_api_contract_spec import APP, OWNER, Case


def _holds_no_turn(body: Any) -> None:
    assert body["crew"] == {"title": None, "status": "idle"}, body


CASES = [
    Case(
        "the owner reads a conversation",
        OWNER,
        200,
        path=lambda gateway: {"conversation_id": gateway.conversation()},
        check=_holds_no_turn,
    ),
    Case(
        "a conversation nobody made is not found",
        OWNER,
        404,
        "not_found",
        path={"conversation_id": "conv_" + "0" * 32},
    ),
    Case("an app is refused while the route serves the owner alone", APP, 403, "owner_only"),
]
