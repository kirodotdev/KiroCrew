"""``GET /v1/conversations/{conversation_id}/items``: what was said, paged."""

from __future__ import annotations

from typing import Any

from e2e.crew_api_contract_spec import APP, OWNER, Case


def _is_the_question_and_then_a_reply(body: Any) -> None:
    newest, oldest = body["data"]
    assert (oldest["role"], newest["role"]) == ("user", "assistant"), body
    assert oldest["content"] == [{"type": "input_text", "text": "hello"}], body
    assert body["has_more"] is False, body


CASES = [
    Case(
        "the owner reads a conversation's history",
        OWNER,
        200,
        path=lambda gateway: {"conversation_id": gateway.conversation_with_a_turn()},
        check=_is_the_question_and_then_a_reply,
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
