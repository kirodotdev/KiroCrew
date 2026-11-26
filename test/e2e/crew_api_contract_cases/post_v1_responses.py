"""``POST /v1/responses``, answered as one JSON body.

The engine is the fake ACP backend, so these cases hold the gateway's side of a turn:
what it accepts, what it refuses and the shape it answers in. What a signed-in engine
says is not covered here.
"""

from __future__ import annotations

from typing import Any

from e2e.crew_api_contract_spec import APP, OWNER, Case

from kiro_crew.testing.fake_acp_backend import ERROR_TRIGGER


def _ask(**change: Any) -> Any:
    return lambda gateway: {"model": gateway.model, "input": "hello", **change}


def _completed(body: Any) -> None:
    assert body["status"] == "completed", body
    assert any(item.get("type") == "message" for item in body["output"]), body


def _failed(body: Any) -> None:
    assert body["status"] == "failed", body


def _in_a_conversation(body: Any) -> None:
    _completed(body)
    assert body["conversation"]["id"].startswith("conv_"), body


CASES = [
    Case("the owner runs a turn", OWNER, 200, json=_ask(), check=_completed),
    Case(
        "a turn the engine fails is a failed response",
        OWNER,
        200,
        json=_ask(input=f"hello {ERROR_TRIGGER}"),
        check=_failed,
    ),
    Case(
        "a turn runs in a conversation the owner made",
        OWNER,
        200,
        json=lambda gateway: _ask(conversation=gateway.conversation())(gateway),
        check=_in_a_conversation,
    ),
    Case(
        "a turn continues the conversation an earlier turn ran in",
        OWNER,
        200,
        json=lambda gateway: _ask(previous_response_id=gateway.kept_response())(gateway),
        check=_in_a_conversation,
    ),
    Case(
        "a conversation nobody made is not found",
        OWNER,
        404,
        "not_found",
        json=_ask(conversation="conv_" + "0" * 32),
    ),
    Case(
        "a conversation and a previous response that do not go together are refused",
        OWNER,
        400,
        "invalid_request",
        json=lambda gateway: _ask(
            conversation=gateway.conversation(), previous_response_id=gateway.kept_response()
        )(gateway),
    ),
    Case("a body with no input is refused", OWNER, 400, "invalid_request", json={"model": "x"}),
    Case(
        "a field the API refuses is named",
        OWNER,
        400,
        "unsupported_parameter",
        json=_ask(store=False),
    ),
    Case(
        "a tool type the API refuses is named",
        OWNER,
        400,
        "unsupported_tool_type",
        json=_ask(tools=[{"type": "web_search"}]),
    ),
    Case(
        "a model nobody has is not found",
        OWNER,
        404,
        "model_not_found",
        json={"model": "no-such-agent-or-bot", "input": "hello"},
    ),
    Case(
        "an app is refused while the route serves the owner alone",
        APP,
        403,
        "owner_only",
        json={"model": "x", "input": "hello"},
    ),
]
