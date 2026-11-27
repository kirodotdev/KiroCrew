"""``POST /v1/conversations``: a conversation with no turn in it."""

from __future__ import annotations

from typing import Any

from e2e.crew_api_contract_spec import APP, OWNER, Case


def _is_new(body: Any) -> None:
    assert body["metadata"] == {"doc_id": "d42"}, body
    assert body["crew"]["status"] == "idle", body


CASES = [
    Case(
        "the owner makes a conversation",
        OWNER,
        200,
        json={"metadata": {"doc_id": "d42"}},
        check=_is_new,
    ),
    Case(
        "a conversation cannot start with items in it",
        OWNER,
        400,
        "invalid_request",
        json={"items": [{"type": "message", "role": "user", "content": "hello"}]},
    ),
    Case(
        "metadata that is not an object of strings is refused",
        OWNER,
        400,
        "invalid_request",
        json={"metadata": {"k": 1}},
    ),
    Case(
        "an app is refused while the route serves the owner alone",
        APP,
        403,
        "owner_only",
        json={},
    ),
]
