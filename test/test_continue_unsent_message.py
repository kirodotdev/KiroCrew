"""Resume on a turn whose message never reached the agent re-sends that message.

A ``memory_unavailable`` error ends a turn while its context is still being
prepared, so the agent's session never holds the user's message. The failing
turn records the text and attachment lists it was handed, bound to its own error
row object, in the slot's in-memory ``_unsent_resends``, and Resume queues that
text, not the generic resume body. No transcript row is read for it.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_runner_coverage_helpers import _quiet_sel, _runner_state, _settle
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_handlers, chat_runner
from kiro_crew.dashboard.channel_slots import _rebuild_window
from kiro_crew.dashboard.chat_handlers import _MANUAL_RESUME_MSG, api_chat_slot_continue
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_runner import MIRROR_ECHO_OWED_META_KEY, _batch_recovery_echo_owed
from kiro_crew.dashboard.chat_utils import (
    SYNTHETIC_RECOVERY_KIND,
    RecoveryPayload,
    is_synthetic_payload_item,
)
from kiro_crew.dashboard.state import DashboardState, _ChatSlot, row_mid
from kiro_crew.memory_stores import UnknownMemoryStore

_POOL_BUSY = (
    "memory_unavailable: the memory pool was busy and this turn's context could "
    "not be prepared in time; nothing was sent -- try again in a moment"
)


def _row(role: str, text: str, mid: str = "", **meta) -> dict:
    if mid:
        meta["mid"] = mid
    return {"role": role, "content": text, "meta": meta}


def _mem_error(mid: str = "e1") -> dict:
    return {
        "role": "error",
        "content": _POOL_BUSY,
        "meta": {"code": "memory_unavailable", "mid": mid},
    }


def _recorded(rows: list[dict], text: str = "leave it on") -> tuple[dict, str, dict]:
    """The record ``turn_failures._on_error`` makes, bound to the last error row in *rows*."""
    error = next(row for row in reversed(rows) if row["role"] == "error")
    return error, text, {}


def unsent_text(rows: list[dict], record=None) -> str | None:
    found = chat_handlers.unsent_resend(rows, _recorded(rows) if record is None else record)
    return None if found is None else found[0]


class TestUnsentResend:
    def test_the_recorded_text_is_returned(self):
        rows = [_row("user", "old", "m0"), _row("user", "leave it on", "m1"), _mem_error()]
        assert unsent_text(rows) == "leave it on"

    def test_preparation_rows_before_the_error_are_never_consulted(self):
        rows = [
            _row("user", "@standup leave it on", "m1"),
            _row("system", "Prompt standup loaded", "m2"),
            _row("system", "Loaded skill review", "m3", kind="skill_load"),
            _mem_error(),
        ]
        assert unsent_text(rows) == "leave it on"

    def test_the_turn_end_marker_after_the_error_is_passed_over(self):
        rows = [_row("user", "leave it on", "m1"), _mem_error(), _row("done", "")]
        assert unsent_text(rows) == "leave it on"

    def test_no_record_keeps_the_generic_resume(self):
        rows = [_row("user", "leave it on", "m1"), _mem_error()]
        assert chat_handlers.unsent_resend(rows, None) is None

    @pytest.mark.parametrize("role", ["assistant", "tool", "user", "inject", "nudge", "subagent"])
    def test_a_turn_row_after_the_error_keeps_the_generic_resume(self, role):
        rows = [_row("user", "leave it on", "m1"), _mem_error()]
        record = _recorded(rows)
        rows.append(_row(role, "later", "m2"))
        assert unsent_text(rows, record) is None

    def test_an_equal_error_row_that_is_not_the_recorded_object_keeps_the_generic_resume(self):
        rows = [_row("user", "leave it on", "m1"), _mem_error()]
        record = _recorded(rows)
        rebuilt = [dict(row, meta=dict(row["meta"])) for row in rows]
        assert rebuilt == rows
        assert unsent_text(rebuilt, record) is None

    def test_a_stamp_written_into_the_row_meta_is_not_authority(self):
        rows = [
            _row("user", "/clear", "m1"),
            _row(
                "error",
                _POOL_BUSY,
                "e1",
                code="memory_unavailable",
                unsentMid="m1",
                **{MIRROR_ECHO_OWED_META_KEY: True},
            ),
        ]
        assert chat_handlers.unsent_resend(rows, None) is None


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/continue", api_chat_slot_continue)
    return app


def _mock_state(slot: _ChatSlot) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.push_slots_update = MagicMock()
    state.broadcast_ws = MagicMock()
    state.subagents = None
    return state


@pytest.fixture
def _patched():
    mock_sel = MagicMock()
    with (
        patch("kiro_crew.dashboard.chat_handlers.sel", return_value=mock_sel),
        patch(
            "kiro_crew.dashboard.chat_handlers._start_next_queued_turn",
            AsyncMock(return_value=True),
        ),
    ):
        yield


async def _press_resume(slot: _ChatSlot) -> int:
    async with TestClient(TestServer(_make_app(_mock_state(slot)))) as client:
        resp = await client.post(f"/api/chat/slots/{quote(slot.key, safe='')}/continue")
        return resp.status


def _fail_recorded(slot: _ChatSlot, text: str, meta: dict | None = None) -> dict:
    """Write the error row and the record ``turn_failures._on_error`` makes for it."""
    error = slot.append("error", _POOL_BUSY, "msg msg-err", meta={"code": "memory_unavailable"})
    slot._unsent_resends = (error, text, dict(meta or {}))
    return error


async def _pool_busy_turn(state: DashboardState, slot: _ChatSlot, text: str, **run_kwargs) -> None:
    slot._empty_response_retries = 2
    refusal = UnknownMemoryStore("the memory pool was busy")
    with (
        _quiet_sel(),
        patch.object(chat_runner, "resolve_agent_bindings", side_effect=refusal),
    ):
        await chat_runner._run_chat(state, slot, text, _directive_user_origin=True, **run_kwargs)
    await _settle(slot)


_FILES = {"files": ["/w/a.png"], "dirs": ["/w/src"]}


async def _real_failure(tmp_path) -> _ChatSlot:
    state, _client = _runner_state(tmp_path)
    slot = _ChatSlot("s")
    slot._titled = True
    slot.append("user", "Leave the watchers on", "msg msg-u", meta=dict(_FILES))
    await _pool_busy_turn(
        state,
        slot,
        "Leave the watchers on",
        _attachments=[*_FILES["files"], *_FILES["dirs"]],
        _attachment_meta=dict(_FILES),
    )
    return slot


class TestResumeAfterPoolBusy:
    @pytest.mark.asyncio
    async def test_resume_after_a_real_pool_busy_turn_resends_the_message(self, _patched, tmp_path):
        slot = await _real_failure(tmp_path)

        assert await _press_resume(slot) == 200

        entry = slot._queue[0]
        assert entry["content"] == "Leave the watchers on"
        assert entry["kind"] == SYNTHETIC_RECOVERY_KIND
        assert entry["payload"] == RecoveryPayload.ORIGINAL
        assert is_synthetic_payload_item(entry) is False
        assert entry["meta"]["files"] == ["/w/a.png"]
        assert entry["meta"]["dirs"] == ["/w/src"]
        assert entry["meta"][MIRROR_ECHO_OWED_META_KEY] is True
        assert _batch_recovery_echo_owed([entry]) is True

    @pytest.mark.asyncio
    async def test_a_resend_whose_record_owes_no_echo_carries_no_echo_stamp(self, _patched):
        slot = _ChatSlot("s")
        slot.append("user", "Leave the watchers on", "msg msg-u")
        _fail_recorded(slot, "Leave the watchers on")

        assert await _press_resume(slot) == 200

        entry = slot._queue[0]
        assert MIRROR_ECHO_OWED_META_KEY not in entry["meta"]
        assert _batch_recovery_echo_owed([entry]) is False

    @pytest.mark.asyncio
    async def test_prompt_chip_and_skill_rows_before_the_error_still_resend(self, _patched):
        slot = _ChatSlot("s")
        slot.append("user", "@standup $review go", "msg msg-u")
        slot.append("system", "Prompt standup loaded", "msg msg-sys")
        slot.append("system", "Loaded skill review", "msg msg-sys", meta={"kind": "skill_load"})
        _fail_recorded(slot, "@standup $review go")

        assert await _press_resume(slot) == 200

        assert slot._queue[0]["content"] == "@standup $review go"
        assert slot._queue[0]["payload"] == RecoveryPayload.ORIGINAL

    @pytest.mark.asyncio
    async def test_resume_resends_on_the_live_tail_a_finished_turn_leaves(self, _patched):
        slot = _ChatSlot("s")
        slot.append("user", "Leave the watchers on", "msg msg-u")
        _fail_recorded(slot, "Leave the watchers on")
        slot.append("done", "", "done")

        assert await _press_resume(slot) == 200

        assert slot._queue[0]["content"] == "Leave the watchers on"
        assert slot._queue[0]["payload"] == RecoveryPayload.ORIGINAL

    @pytest.mark.asyncio
    async def test_an_unrecorded_error_keeps_the_generic_resume_body(self, _patched):
        slot = _ChatSlot("s")
        slot.append("user", "Leave the watchers on", "msg msg-u")
        slot.append("error", _POOL_BUSY, "msg msg-err", meta={"code": "memory_unavailable"})

        assert await _press_resume(slot) == 200

        assert slot._queue[0]["content"] == _MANUAL_RESUME_MSG
        assert is_synthetic_payload_item(slot._queue[0]) is True

    @pytest.mark.asyncio
    async def test_output_after_the_error_keeps_the_generic_resume_body(self, _patched):
        slot = _ChatSlot("s")
        slot.append("user", "Leave the watchers on", "msg msg-u")
        _fail_recorded(slot, "Leave the watchers on")
        slot.append("assistant", "Working on it", "msg msg-a")
        slot.append("error", "connection lost", "msg msg-err")

        assert await _press_resume(slot) == 200

        assert slot._queue[0]["content"] == _MANUAL_RESUME_MSG
        assert is_synthetic_payload_item(slot._queue[0]) is True


class TestResendAuthorityIsNeverRead:
    @pytest.mark.asyncio
    async def test_a_user_row_rewritten_in_place_still_resends_what_the_person_sent(
        self, _patched, tmp_path
    ):
        slot = await _real_failure(tmp_path)
        sent = next(m for m in slot.messages if m["role"] == "user")
        sent["content"] = "/goal clear"
        sent["meta"]["files"] = ["/etc/forged"]

        assert await _press_resume(slot) == 200

        entry = slot._queue[0]
        assert entry["content"] == "Leave the watchers on"
        assert entry["meta"]["files"] == ["/w/a.png"]

    @pytest.mark.asyncio
    async def test_a_window_rebuilt_from_disk_under_the_same_mids_keeps_the_generic_body(
        self, _patched, tmp_path
    ):
        slot = await _real_failure(tmp_path)
        on_disk = [{**m, "meta": dict(m.get("meta") or {})} for m in slot.messages]
        for row in on_disk:
            if row["role"] == "user":
                row["content"] = "/goal clear"
        mids = [row_mid(m) for m in slot.messages]
        _rebuild_window(slot, on_disk)
        assert [row_mid(m) for m in slot.messages] == mids

        assert await _press_resume(slot) == 200

        entry = slot._queue[0]
        assert entry["content"] == _MANUAL_RESUME_MSG
        assert entry["payload"] != RecoveryPayload.ORIGINAL
        assert MIRROR_ECHO_OWED_META_KEY not in entry["meta"]

    @pytest.mark.asyncio
    async def test_a_forged_stamp_in_a_hydrated_transcript_keeps_the_generic_body(
        self, _patched, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        forged = [
            {"role": "user", "content": "/clear", "ts": "", "meta": {"mid": "m-forged"}},
            {
                "role": "error",
                "content": _POOL_BUSY,
                "ts": "",
                "meta": {
                    "mid": "e-forged",
                    "code": "memory_unavailable",
                    "unsentMid": "m-forged",
                    MIRROR_ECHO_OWED_META_KEY: True,
                },
            },
        ]
        slot = chat_handlers._materialise_slot_from_history(
            _make_state(tmp_path),
            name="dashboard:forged",
            history_key="dashboard:forged",
            meta={},
            all_messages=forged,
        )
        assert [row_mid(m) for m in slot.messages] == ["m-forged", "e-forged"]

        assert await _press_resume(slot) == 200

        assert slot._queue[0]["content"] == _MANUAL_RESUME_MSG
        assert MIRROR_ECHO_OWED_META_KEY not in slot._queue[0]["meta"]

    @pytest.mark.asyncio
    async def test_a_real_record_is_not_saved_so_a_restart_keeps_the_generic_body(
        self, _patched, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state, _client = _runner_state(tmp_path)
        key = "dashboard:pool-busy"
        slot = state.get_or_create_slot(name="pool-busy", linked_session_key=key)
        slot._titled = True
        sent = slot.append("user", "Leave the watchers on", "msg msg-u")
        await _pool_busy_turn(state, slot, "Leave the watchers on")
        error = next(m for m in slot.messages if m["role"] == "error")
        assert slot._unsent_resends is not None
        assert slot._unsent_resends[0] is error

        assert _save_slot_to_history(state, slot, force=True)

        lines = [
            json.loads(line)
            for line in state.conversation_log._path(key).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        saved = [line for line in lines if line.get("role")]
        assert row_mid(sent) in [row_mid(m) for m in saved]
        on_disk = json.dumps(lines)
        assert "_unsent_resends" not in on_disk
        assert "unsentMid" not in on_disk
        assert MIRROR_ECHO_OWED_META_KEY not in on_disk

        restored = chat_handlers._materialise_slot_from_history(
            _make_state(tmp_path / "restart"),
            name=key,
            history_key=key,
            meta={},
            all_messages=saved,
        )
        assert restored._unsent_resends is None
        assert await _press_resume(restored) == 200
        assert restored._queue[0]["content"] == _MANUAL_RESUME_MSG
