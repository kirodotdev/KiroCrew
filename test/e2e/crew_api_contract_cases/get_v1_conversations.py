"""``GET /v1/conversations``: the owner's conversations, paged."""

from __future__ import annotations

from typing import Any

from e2e.crew_api_contract_spec import APP, OWNER, Case


def _lists_a_conversation(body: Any) -> None:
    assert body["data"], body
    assert body["first_id"] == body["data"][0]["id"], body


CASES = [
    Case(
        "the owner lists conversations",
        OWNER,
        200,
        before=lambda gateway: gateway.conversation(),
        check=_lists_a_conversation,
    ),
    Case("an app is refused while the route serves the owner alone", APP, 403, "owner_only"),
]
