"""``DELETE /v1/conversations/{conversation_id}``: a conversation and its turns, removed."""

from __future__ import annotations

from typing import Any

from e2e.crew_api_contract_spec import APP, OWNER, Case


def _says_it_is_deleted(body: Any) -> None:
    assert (body["object"], body["deleted"]) == ("conversation.deleted", True), body


CASES = [
    Case(
        "the owner deletes a conversation",
        OWNER,
        200,
        path=lambda gateway: {"conversation_id": gateway.conversation_with_a_turn()},
        check=_says_it_is_deleted,
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
