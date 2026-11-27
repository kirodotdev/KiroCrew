"""``GET /v1/crew/events``: one conversation's event stream, as paged JSON."""

from __future__ import annotations

from typing import Any

from e2e.crew_api_contract_spec import APP, OWNER, Case


def _is_one_turn_from_start_to_end(body: Any) -> None:
    kinds = [event["type"] for event in body["events"]]
    assert (kinds[0], kinds[-1]) == ("turn.started", "turn.completed"), body
    assert set(kinds[1:-1]) == {"message.created"}, body
    assert [event["seq"] for event in body["events"]] == list(range(1, len(kinds) + 1)), body
    assert (body["latest_seq"], body["has_more"]) == (len(kinds), False), body
    assert body["events"][-1]["data"] == {"result": "completed"}, body
    assert "hello" not in str(body), body


def _is_only_the_newest_number(body: Any) -> None:
    assert body["events"] == [] and body["latest_seq"] >= 2, body


def _names_where_to_resync(body: Any) -> None:
    error = body["error"]
    assert error["type"] == "resync_required_error", body
    assert error["latest_seq"] >= error["oldest_seq"] >= 1, body


def _of_a_turn(**query: str) -> Any:
    return lambda gateway: {"conversation": gateway.conversation_with_a_turn(), **query}


CASES = [
    Case(
        "the owner reads a conversation's events from the start",
        OWNER,
        200,
        query=_of_a_turn(since="0"),
        check=_is_one_turn_from_start_to_end,
    ),
    Case(
        "limit 0 answers the newest seq alone",
        OWNER,
        200,
        query=_of_a_turn(since="0", limit="0"),
        check=_is_only_the_newest_number,
    ),
    Case(
        "a cursor past the newest event must resync",
        OWNER,
        410,
        "resync_required",
        query=_of_a_turn(since="100000"),
        check=_names_where_to_resync,
    ),
    Case(
        "a conversation nobody made has no stream",
        OWNER,
        404,
        "not_found",
        query={"conversation": "conv_" + "0" * 32, "since": "0"},
    ),
    Case(
        "the instance stream is not served yet",
        OWNER,
        422,
        "capability_unavailable",
    ),
    Case(
        "an app is refused while the route serves the owner alone",
        APP,
        403,
        "owner_only",
        query={"conversation": "conv_" + "0" * 32, "since": "0", "wait": "30"},
    ),
]
