"""``POST /v1/conversations/{conversation_id}``: the conversation's metadata, replaced."""

from __future__ import annotations

from typing import Any

from e2e.crew_api_contract_spec import APP, OWNER, Case


def _holds_the_new_metadata(body: Any) -> None:
    assert body["metadata"] == {"doc_id": "d43"}, body


CASES = [
    Case(
        "the owner replaces a conversation's metadata",
        OWNER,
        200,
        json={"metadata": {"doc_id": "d43"}},
        path=lambda gateway: {"conversation_id": gateway.conversation()},
        check=_holds_the_new_metadata,
    ),
    Case(
        "a field other than metadata is refused",
        OWNER,
        400,
        "invalid_request",
        json={"metadata": {}, "title": "x"},
        path=lambda gateway: {"conversation_id": gateway.conversation()},
    ),
    Case(
        "a conversation nobody made is not found",
        OWNER,
        404,
        "not_found",
        json={"metadata": {}},
        path={"conversation_id": "conv_" + "0" * 32},
    ),
    Case(
        "an app is refused while the route serves the owner alone",
        APP,
        403,
        "owner_only",
        json={"metadata": {}},
    ),
]
