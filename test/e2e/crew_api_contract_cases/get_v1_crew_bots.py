"""``GET /v1/crew/bots``."""

from __future__ import annotations

from typing import Any

from e2e.crew_api_contract_spec import APP, OWNER, Case


def _is_a_list_of_bots(body: Any) -> None:
    assert body["object"] == "list" and body["has_more"] is False, body
    assert all(row["object"] == "bot" and row["avatar_url"] is None for row in body["data"]), body


CASES = [
    Case("the owner lists the Bots", OWNER, 200, check=_is_a_list_of_bots),
    # The suite's app holds neither bots:read nor bots:chat.
    Case("an app that holds no Bot scope is refused", APP, 403, "insufficient_scope"),
]
