"""``POST /api/chat`` with a ``banner``: the row shows the label, the turn gets the text.

A script that starts a turn with a long work order can pass a short ``banner``.
The user row keeps the full message as its ``content`` and carries the label as
``meta.banner``, which the dashboard shows with the full text behind a
disclosure. The turn's input is the full message. The label goes through
``normalize_banner``, the monitor-loop banner's validator, so it has the same
cap and the same credential redaction.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.constants import MAX_BANNER_CHARS
from kiro_crew.dashboard.chat_delivery import banner_meta, queue_entry_view
from kiro_crew.dashboard.chat_utils_display import _redact_meta_for_role

_SLOT = "banner-chat"
_ORDER = "Work order: rebase CR-1 onto mainline, rerun the build, report the result.\n" * 40
_LABEL = "Rebase CR-1 and rebuild"
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def _user_rows(slot) -> list[dict]:
    return [m for m in slot.messages if m.get("role") == "user"]


@pytest.fixture
def idle_state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.get_or_create_slot(_SLOT)
    received: list[str] = []

    async def run_chat(st, slot, message, **_kwargs):
        received.append(message)
        slot.append("done", "", "done", broadcast=False)

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", run_chat)
    return state, received


async def _post(state, body: dict):
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat?ws=1", json={"slot": _SLOT, **body})
        payload = await resp.json()
        slot = state.get_slot(_SLOT)
        if resp.status == 200 and slot.task is not None:
            await slot.task
        return resp.status, payload


@pytest.mark.asyncio
async def test_row_shows_the_label_and_the_turn_gets_the_full_message(idle_state):
    state, received = idle_state

    status, _ = await _post(state, {"message": _ORDER, "banner": f"  {_LABEL}  "})

    assert status == 200
    assert received == [_ORDER.strip()]
    [row] = _user_rows(state.get_slot(_SLOT))
    assert row["content"] == _ORDER.strip()
    assert row["meta"]["banner"] == _LABEL


@pytest.mark.asyncio
async def test_no_banner_keeps_the_row_as_before_and_meta_cannot_forge_one(idle_state):
    state, received = idle_state

    status, _ = await _post(state, {"message": _ORDER, "meta": {"banner": "x" * 5000}})

    assert status == 200
    assert received == [_ORDER.strip()]
    [row] = _user_rows(state.get_slot(_SLOT))
    assert row["content"] == _ORDER.strip()
    assert "banner" not in row.get("meta", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("banner", ["x" * (MAX_BANNER_CHARS + 1), 7, ["a"]])
async def test_bad_banner_is_refused_before_any_row_or_turn(idle_state, banner):
    state, received = idle_state

    status, payload = await _post(state, {"message": _ORDER, "banner": banner})

    assert status == 400
    assert payload["code"] == "invalid_banner"
    assert received == []
    assert _user_rows(state.get_slot(_SLOT)) == []


@pytest.mark.asyncio
async def test_banner_credential_is_masked_on_the_row(idle_state):
    state, _ = idle_state

    await _post(state, {"message": _ORDER, "banner": f"deploy with {_AWS_KEY}"})

    [row] = _user_rows(state.get_slot(_SLOT))
    assert _AWS_KEY not in row["meta"]["banner"]
    assert row["meta"]["banner"].startswith("deploy with ")


@pytest.mark.asyncio
async def test_queued_send_keeps_its_label_and_drains_alone(tmp_path, monkeypatch):
    from kiro_crew.dashboard import chat_runner

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    slot = state.get_or_create_slot(_SLOT)
    slot.task = MagicMock(done=MagicMock(return_value=False))
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", MagicMock())
    async with TestClient(TestServer(_make_app(state))) as client:
        for body in ({"message": "first order", "banner": "First"}, {"message": "plain"}):
            resp = await client.post("/api/chat", json={"slot": _SLOT, **body})
            assert (await resp.json()).get("queued") is True
    slot.task = None

    with (
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(chat_runner, "_run_chat", return_value=MagicMock()) as run,
    ):
        assert await chat_runner._start_next_queued_turn(state, slot) is True

    [row] = _user_rows(slot)
    assert row["content"] == "first order"
    assert row["meta"]["banner"] == "First"
    assert run.call_args.args[2] == "first order"
    frames = {}
    for call in state.broadcast_ws.call_args_list:
        frames.setdefault(call.args[0], []).append(call.args[1])
    assert frames["queue_push"][0]["meta"]["banner"] == "First"
    assert "banner" not in frames["queue_push"][1].get("meta", {})
    assert frames["queue_pop"][0]["meta"]["banner"] == "First"
    assert "banner" not in queue_entry_view(slot._queue[0]).get("meta", {})
    assert [entry["content"] for entry in slot._queue] == ["plain"]


@pytest.mark.asyncio
async def test_steered_send_keeps_its_label_on_the_row_and_the_requeue(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    slot = state.get_or_create_slot(_SLOT)
    slot.task = MagicMock(done=MagicMock(return_value=False))
    requeue_meta: dict = {}

    async def steer(message):
        requeue_meta.update(slot._steer_attachment_meta.get(message) or {})
        return True

    slot._acp_client = MagicMock(supports_steer=True, steer=AsyncMock(side_effect=steer))
    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={"slot": _SLOT, "message": _ORDER, "banner": _LABEL, "steer": True},
            )
            assert (await resp.json()).get("steered") is True

    slot._acp_client.steer.assert_awaited_once_with(_ORDER.strip())
    [row] = _user_rows(slot)
    assert row["content"] == _ORDER.strip()
    assert row["meta"]["banner"] == _LABEL
    assert requeue_meta.get("banner") == _LABEL
    [push] = [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "steer_push"]
    assert push["meta"]["banner"] == _LABEL


@pytest.mark.parametrize(
    "role,banner,kept",
    [
        ("user", _LABEL, True),
        ("user", "x" * (MAX_BANNER_CHARS + 1), False),
        ("user", "   ", False),
        ("user", 3, False),
        ("assistant", _LABEL, False),
    ],
)
def test_emit_keeps_only_a_bounded_user_label(role, banner, kept):
    out = _redact_meta_for_role(role, {"banner": banner})

    assert ("banner" in out) is kept


def test_queue_view_carries_only_a_bounded_label():
    entry = {"id": "q", "content": "order", "meta": {"banner": _LABEL}}

    assert queue_entry_view(entry)["meta"]["banner"] == _LABEL
    for bad in ("   ", "x" * (MAX_BANNER_CHARS + 1), 4):
        assert banner_meta({"banner": bad}) == {}


def test_restored_queue_label_is_redacted_before_it_leaves():
    entry = {"id": "q", "content": "order", "meta": {"banner": f"deploy with {_AWS_KEY}"}}

    shown = queue_entry_view(entry)["meta"]["banner"]

    assert _AWS_KEY not in shown
    assert shown.startswith("deploy with ")
