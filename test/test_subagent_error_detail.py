"""Failure-reporting fidelity for subagents.

A subagent that dies must say WHAT killed it. These tests pin the three places
that previously flattened that information: the exception rendering, the
durable tombstone, and the native-card truncation.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from kiro_crew.dashboard.chat_runner import _MAX_NATIVE_CARD_ERROR, _clip_card_error
from kiro_crew.subagent import (
    _MAX_ERROR_CHAIN,
    _MAX_ERROR_DETAIL_LEN,
    SubagentInfo,
    SubagentManager,
    _describe_exception,
)


@pytest.fixture()
def agent_root(tmp_path, monkeypatch):
    """Point persistence at a temp directory."""
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path)
    return tmp_path


class TestDescribeException:
    def test_names_the_subsystem_for_an_unattributable_message(self):
        """The message alone cannot be attributed; the type is the whole point.

        ``bad parameter or other API misuse`` is sqlite's wording for
        SQLITE_MISUSE. Rendered without its class it reads as an unspecified
        argument bug and sends a reader looking in the wrong subsystem.
        """
        rendered = _describe_exception(
            sqlite3.InterfaceError("bad parameter or other API misuse")
        )
        assert rendered == "sqlite3.InterfaceError: bad parameter or other API misuse"

    def test_builtins_are_not_module_qualified(self):
        assert _describe_exception(ValueError("nope")) == "ValueError: nope"

    def test_follows_an_explicit_cause(self):
        try:
            try:
                raise sqlite3.InterfaceError("bad parameter or other API misuse")
            except sqlite3.InterfaceError as inner:
                raise RuntimeError("context build failed") from inner
        except RuntimeError as outer:
            rendered = _describe_exception(outer)
        assert rendered == (
            "RuntimeError: context build failed <- caused by "
            "sqlite3.InterfaceError: bad parameter or other API misuse"
        )

    def test_follows_an_unsuppressed_context(self):
        """A bare ``raise`` inside an except block sets __context__, not __cause__."""
        try:
            try:
                raise KeyError("lessons")
            except KeyError:
                raise RuntimeError("wrapper")
        except RuntimeError as outer:
            rendered = _describe_exception(outer)
        assert rendered == "RuntimeError: wrapper <- caused by KeyError: 'lessons'"

    def test_suppressed_context_is_not_reported_as_a_cause(self):
        """``from None`` means the author said the context is unrelated."""
        try:
            try:
                raise KeyError("unrelated")
            except KeyError:
                raise RuntimeError("wrapper") from None
        except RuntimeError as outer:
            rendered = _describe_exception(outer)
        assert rendered == "RuntimeError: wrapper"

    def test_empty_message_renders_the_bare_type(self):
        assert _describe_exception(RuntimeError()) == "RuntimeError"

    def test_output_is_bounded(self):
        rendered = _describe_exception(ValueError("x" * (_MAX_ERROR_DETAIL_LEN * 3)))
        assert len(rendered) == _MAX_ERROR_DETAIL_LEN

    def test_chain_is_capped(self):
        exc = ValueError("link0")
        for depth in range(1, _MAX_ERROR_CHAIN + 5):
            nxt = ValueError(f"link{depth}")
            nxt.__cause__ = exc
            exc = nxt
        rendered = _describe_exception(exc)
        assert rendered.count("<- caused by") == _MAX_ERROR_CHAIN - 1

    def test_self_referential_chain_terminates(self):
        """A cycle must not spin; ``__cause__`` is writable so this is reachable."""
        exc = ValueError("loop")
        exc.__cause__ = exc
        assert _describe_exception(exc) == "ValueError: loop"


class TestTombstoneCarriesTheReason:
    def _tombstone(self, agent_root, agent_id):
        return json.loads(
            (agent_root / agent_id / "tombstone.json").read_text(encoding="utf-8")
        )

    def test_records_the_specific_reason_not_just_the_bucket(self, agent_root):
        """``cause`` is a bucket; without ``detail`` the reason dies with the process."""
        from kiro_crew.subagent_persistence import create_agent_folder

        create_agent_folder("deadbeef", task="t")
        info = SubagentInfo(id="deadbeef", task="t")
        info.error = "sqlite3.InterfaceError: bad parameter or other API misuse"

        SubagentManager._write_tombstone(info, "error")

        tomb = self._tombstone(agent_root, "deadbeef")
        assert tomb["cause"] == "error"
        assert tomb["detail"] == "sqlite3.InterfaceError: bad parameter or other API misuse"

    def test_absent_error_yields_an_empty_detail(self, agent_root):
        from kiro_crew.subagent_persistence import create_agent_folder

        create_agent_folder("nodetail", task="t")
        SubagentManager._write_tombstone(SubagentInfo(id="nodetail", task="t"), "reaped")

        assert self._tombstone(agent_root, "nodetail")["detail"] == ""

    def test_detail_is_bounded(self, agent_root):
        from kiro_crew.subagent_persistence import create_agent_folder

        create_agent_folder("longone", task="t")
        info = SubagentInfo(id="longone", task="t")
        info.error = "E" * (_MAX_ERROR_DETAIL_LEN * 3)

        SubagentManager._write_tombstone(info, "error")

        assert len(self._tombstone(agent_root, "longone")["detail"]) == _MAX_ERROR_DETAIL_LEN


class TestTerminalArmUsesTheDescription:
    """The rendering is only useful if the arm that stores the error calls it."""

    @pytest.mark.asyncio
    async def test_run_records_the_exception_type(self, agent_root):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.subagent_persistence import create_agent_folder

        sessions = MagicMock()
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        manager = SubagentManager(sessions=sessions, ctx_builder=MagicMock())

        info = SubagentInfo(id="sqlitedeath", task="t", parent_session_key="dashboard:default")
        create_agent_folder("sqlitedeath", task="t")
        manager._agents["sqlitedeath"] = info
        manager._running_count = 1

        async def _raise_sqlite_misuse(*_a, **_kw):
            raise sqlite3.InterfaceError("bad parameter or other API misuse")

        with patch.object(manager, "_run_inner", _raise_sqlite_misuse), \
             patch("kiro_crew.subagent.Stats"), \
             patch("kiro_crew.subagent.sel"), \
             patch.object(manager, "_fire_event", new_callable=AsyncMock), \
             patch.object(manager, "_on_done", new_callable=AsyncMock):
            await asyncio.wait_for(manager._run(info), timeout=10)

        assert info.error == "sqlite3.InterfaceError: bad parameter or other API misuse"
        tomb = json.loads(
            (agent_root / "sqlitedeath" / "tombstone.json").read_text(encoding="utf-8")
        )
        assert tomb["cause"] == "error"
        assert tomb["detail"] == "sqlite3.InterfaceError: bad parameter or other API misuse"


class TestClipCardError:
    def test_short_error_is_unchanged(self):
        assert _clip_card_error("boom") == "boom"

    def test_long_error_without_a_request_id_is_head_sliced(self):
        clipped = _clip_card_error("y" * 500)
        assert clipped == "y" * _MAX_NATIVE_CARD_ERROR

    def test_trailing_request_id_survives_truncation(self):
        """The request id is the only part support can act on, and it sits last."""
        text = (
            "Your account does not have access to model 'claude-opus-4-8'. "
            + "filler " * 40
            + "(request_id: 596ec62b-e3e8-4d66-9afb-2ad3a2217172)"
        )
        assert len(text) > _MAX_NATIVE_CARD_ERROR

        clipped = _clip_card_error(text)

        assert "(request_id: 596ec62b-e3e8-4d66-9afb-2ad3a2217172)" in clipped
        assert clipped.startswith("Your account does not have access to model")
        assert len(clipped) <= _MAX_NATIVE_CARD_ERROR

    def test_request_id_not_at_the_end_is_not_preserved(self):
        """Only a trailing id is the formatter's suffix; mid-string is prose."""
        text = "(request_id: abc-123) " + "z" * 500
        clipped = _clip_card_error(text)
        assert len(clipped) == _MAX_NATIVE_CARD_ERROR
        assert clipped.endswith("z")

    def test_native_sync_publishes_an_error_keeping_the_request_id(self):
        """Pins the call site: the native card is where the clip is applied."""
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.chat_runner import _native_subagent_sync

        state = MagicMock()
        state.broadcast_ws = MagicMock()
        slot = MagicMock()
        slot.key = "slot-1"
        slot._native_subagent_tracker = {}
        slot._native_subagent_output = {}
        tracker: dict = {}

        def _sub(status_type, message=""):
            return {
                "sessionId": "s1",
                "sessionName": "n",
                "agentName": "worker",
                "role": "worker",
                "initialQuery": "q",
                "status": {"type": status_type, "message": message},
            }

        _native_subagent_sync(state, slot, [_sub("working")], tracker)
        long_error = (
            "Your account does not have access to model 'claude-opus-4-8'. "
            + "filler " * 40
            + "(request_id: 596ec62b-e3e8-4d66-9afb-2ad3a2217172)"
        )
        _native_subagent_sync(state, slot, [_sub("failed", long_error)], tracker)

        errors = [
            call.args[1]["error"]
            for call in state.broadcast_ws.call_args_list
            if call.args[0] == "subagent_done" and call.args[1].get("error")
        ]
        assert errors, "no terminal frame carried an error"
        assert "(request_id: 596ec62b-e3e8-4d66-9afb-2ad3a2217172)" in errors[-1]
