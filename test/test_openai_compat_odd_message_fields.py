"""``POST /v1/chat/completions`` answers an odd message role or text part without a 500.

A ``role`` that is a list or an object cannot be looked up among the unsupported
roles, and a text part whose ``text`` is null or a number cannot be joined into the
prompt; either would raise ``TypeError`` out of the handler. The role is refused
through the existing unsupported-role 400, and the text part is coerced the way a
scalar ``content`` is. Every request the endpoint answers otherwise keeps its answer.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.openai_compat import _flatten_messages, api_completions
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService


class _Ready(KiroPrerequisiteService):
    async def session_ready(self) -> bool:
        return True

    async def verified_ready(self, *, max_age_secs: float) -> bool:
        del max_age_secs
        return True


def _slot():
    slot = MagicMock()
    slot.key = "test-slot"
    slot.linked_session_key = ""
    slot.channel_origin = False
    slot.agent = ""
    slot.task = None
    slot.running = False
    slot.turn_running = False
    slot.is_closing = False
    slot.event = asyncio.Event()
    slot._pending = []
    slot._pending_consumers = 0
    slot._stream_epoch = 0
    slot._stream_handoffs = {}

    def drain():
        out = slot._pending[:]
        slot._pending.clear()
        slot.event.clear()
        return out

    slot.drain = drain
    return slot


async def _post(messages: list) -> tuple[int, dict, list[str]]:
    """Run the handler once; return the status, the body and every prompt sent."""
    slot = _slot()
    state = MagicMock()
    state.get_or_create_slot = MagicMock(return_value=slot)
    state._slots = {slot.key: slot}
    state._background_tasks = set()
    state.session_still_loading = AsyncMock(return_value=False)
    request = MagicMock()
    request.json = AsyncMock(
        return_value={"model": "vanellope", "messages": messages, "stream": False}
    )
    request.app = {"state": state, "kiro_prerequisite_service": object.__new__(_Ready)}
    request.get = MagicMock(side_effect=lambda k, d="": d)
    request.remote = "127.0.0.1"
    prompts: list[str] = []

    async def fake_run_chat(_state, _slot, prompt, **_kwargs):
        prompts.append(prompt)
        slot._pending.append({"role": "assistant", "content": "ok"})
        slot._pending.append({"cls": "done"})
        slot.event.set()

    with patch("kiro_crew.dashboard.openai_compat._run_chat", side_effect=fake_run_chat):
        resp = await api_completions(request)
    return resp.status, json.loads(resp.body), prompts


@pytest.mark.parametrize("role", [["user"], {"name": "user"}, []], ids=["list", "object", "empty"])
@pytest.mark.asyncio
async def test_a_list_or_object_role_is_refused_not_a_server_error(role) -> None:
    status, body, prompts = await _post(
        [{"role": "user", "content": "hi"}, {"role": role, "content": "x"}]
    )
    assert status == 400
    assert body["error"]["type"] == "invalid_request_error"
    assert "not supported" in body["error"]["message"]
    assert prompts == []


@pytest.mark.parametrize("role", ["tool", "function"])
@pytest.mark.asyncio
async def test_the_named_unsupported_roles_keep_their_answer(role) -> None:
    status, body, _ = await _post([{"role": "user", "content": "hi"}, {"role": role}])
    assert status == 400
    assert body["error"]["message"] == f"role {role!r} not supported"


@pytest.mark.parametrize("role", [None, 5, True, "narrator"], ids=repr)
@pytest.mark.asyncio
async def test_a_scalar_or_unknown_role_is_still_answered_as_before(role) -> None:
    """Main drops such a message and answers; the refusal does not widen to it."""
    status, _, prompts = await _post(
        [{"role": "user", "content": "hi"}, {"role": role, "content": "dropped"}]
    )
    assert status == 200
    assert prompts == ["hi"]


@pytest.mark.parametrize(
    ("text", "joined"),
    [(None, "a  c"), (5, "a 5 c"), (1.5, "a 1.5 c"), (True, "a True c"), ("b", "a b c")],
    ids=["null", "int", "float", "bool", "string"],
)
def test_a_non_string_text_part_is_coerced_like_a_scalar_content(text, joined) -> None:
    parts = [
        {"type": "text", "text": "a"},
        {"type": "text", "text": text},
        {"type": "text", "text": "c"},
    ]
    assert _flatten_messages([{"role": "user", "content": parts}]) == joined


def test_a_text_part_without_text_still_reads_as_empty() -> None:
    parts = [{"type": "text"}, {"type": "text", "text": "z"}]
    assert _flatten_messages([{"role": "user", "content": parts}]) == " z"


def test_a_scalar_content_keeps_its_coercion() -> None:
    assert _flatten_messages([{"role": "user", "content": 7}]) == "7"
    assert _flatten_messages([{"role": "user", "content": None}]) == ""


@pytest.mark.parametrize("text", [None, 5], ids=["null", "int"])
@pytest.mark.asyncio
async def test_a_text_part_with_odd_text_reaches_the_agent(text) -> None:
    status, _, prompts = await _post(
        [
            {
                "role": "user",
                "content": [{"type": "text", "text": text}, {"type": "text", "text": "q"}],
            }
        ]
    )
    assert status == 200
    assert len(prompts) == 1
    assert prompts[0].endswith("q")
