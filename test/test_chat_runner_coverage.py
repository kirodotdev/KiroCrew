"""Coverage tests for ``kiro_crew.dashboard.chat_runner``.

The module's happy paths are well covered by the existing chat suites
(``test_dashboard_approval``, ``test_dashboard_chat``, ``test_eager_spawn``,
``test_goal_command`` …). What this suite targets instead is the set of
*defensive* branches those suites never reach: fail-open ``except`` arms,
deny-by-default returns, the three retry ladders that a terminal
``stop_reason`` walks, and the auto-approve rungs (trusted patterns,
read-only bash, native crew) that sit between the interactive prompt and the
session-trust flag.

Two harnesses are used and the choice between them is deliberate:

* Pure helpers are called directly. They take plain dicts / mocks, so a unit
  call reaches the branch with no turn machinery at all.
* Branches that only exist inside ``_run_chat`` are driven through a real
  turn, with a mocked provider whose ``stream()`` yields a scripted list of
  ``LLMEvent``s. That is the same shape ``test_dashboard_approval`` uses, so
  the assertions run against production dispatch rather than a re-implemented
  copy of it.

The suite is split by topic. This file keeps the ``_run_chat`` turns that read the
transcript off the loop, abort quietly on shutdown, record a start failure and count
WakaTime coding time, and the blocked-link card records. The other topics are in the
``test_chat_runner_coverage_*.py`` files beside it: ``_turn_start`` (the turn-start
fold, the app-slot dispatch guard, prompt context order, pending context, and prompt
and skill expansion), ``_output`` (metrics, snapshots, OAuth banners, channel
mirroring, segment flush and native subagent cards), ``_trust`` (hook verdicts, trust
predicates and command patterns), ``_approval`` (the auto-approve rungs and the
approval window), ``_pending`` (pending resets and discards, eager spawn, prefetch and
steer), ``_queue`` (the queue drain and synthesis) and ``_commands`` (model guards,
local commands and the recovery ladders). The shared builders are in
``chat_runner_coverage_helpers.py``; this module re-exports the ones other suites
import from it.
"""

from __future__ import annotations

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_runner_coverage_helpers import (  # noqa: F401
    _coding_tool_call,
    _coding_tool_result,
    _complete,
    _drive,
    _errors,
    _permission,
    _quiet_sel,
    _runner_state,
    _set_stream,
    _settle,
    _slot,
    _statusless_coding_tool_call,
)

from kiro_crew.acp.types import (
    EVENT_TOOL_CALL,
)
from kiro_crew.dashboard import chat_runner
from kiro_crew.history import ConversationLog
from kiro_crew.providers.base import LLMEvent


class TestPromptSubmitTranscriptRead:
    """The canonical replay's transcript read must not run on the loop."""

    @pytest.mark.asyncio
    async def test_disk_count_read_runs_off_the_loop_thread(self, tmp_path, monkeypatch):
        state, client = _runner_state(tmp_path)
        _set_stream(client, [_complete()])
        slot = _slot()
        # A non-empty in-memory window is what arms the probe; without it the
        # branch holding the read is skipped and the test would pass vacuously.
        slot.append("user", "an earlier turn", "msg msg-u")
        seen: list[int] = []
        real_read = ConversationLog.read_messages_chained

        def recording(self, key):  # noqa: ANN001 -- test double
            seen.append(threading.get_ident())
            return real_read(self, key)

        monkeypatch.setattr(ConversationLog, "read_messages_chained", recording)

        await _drive(state, slot, "hello")

        assert seen, (
            "no transcript read happened on the prompt-submit path -- this test "
            "no longer exercises the re-injection probe and would pass vacuously"
        )
        assert threading.get_ident() not in seen, (
            "the re-injection probe read the transcript on the event-loop thread; "
            "it must go through asyncio.to_thread"
        )


# ── SessionClosingError shutdown race ─────────────────────────────────────


class TestSessionClosingQuietAbort:
    """A gateway-shutdown race during turn prep is a quiet abort, not an error.

    Two dashboard slots were mid ``get_or_create`` when a restart's
    ``close_all`` set ``_closing``; the allocation gate raised
    ``SessionClosingError`` (by design) but the generic terminal handler then
    logged an ERROR traceback, appended an ❌ error card to the slot, and
    recorded a spurious session failure. The dedicated arm must swallow it: no
    card, no record_failure, no exception out of ``_run_chat``.
    """

    @pytest.mark.asyncio
    async def test_get_or_create_closing_appends_no_error_card(self, tmp_path):
        from kiro_crew.session import SessionClosingError

        state, _client = _runner_state(tmp_path)
        state.sessions.get_or_create = AsyncMock(
            side_effect=SessionClosingError(
                "SessionManager began closing during provider startup; "
                "refusing to register a session behind the shutdown snapshot"
            )
        )
        slot = _slot()

        await _drive(state, slot, "hello")

        assert _errors(slot) == [], (
            "a shutdown race during session creation must not surface an "
            "error card in the chat slot"
        )
        state.sessions.record_failure.assert_not_awaited()


class TestSessionStartFailureRowKind:
    """A session start that timed out leaves a row the Continue guard can count.

    ``record_failure`` cannot count this failure: it increments a counter on the
    REGISTERED session, and a start that never answered registered none, so the
    call returns False and nothing anywhere remembers that the start failed.
    The structural ``session_start_failed`` row kind is that memory -- decided
    from the exception's tag (set at the ACP raise sites on both exception
    families), never from the prose, so the Continue endpoint and the error card
    can count consecutive failed starts without matching on message wording.
    """

    @pytest.mark.asyncio
    async def test_shared_runtime_start_timeout_row_carries_the_kind(self, tmp_path):
        from kiro_crew.acp.runtime import AcpSessionStartTimeout
        from kiro_crew.dashboard.chat_utils import SESSION_START_FAILED_KIND

        state, _client = _runner_state(tmp_path)
        state.sessions.get_or_create = AsyncMock(
            side_effect=AcpSessionStartTimeout(
                "Request session/new timed out after 90s "
                "(4/4 session-injected MCP server(s) reported)",
                collector=None,
            )
        )
        slot = _slot()

        await _drive(state, slot, "hello")

        error = next(row for row in slot.messages if row["role"] == "error")
        assert "timed out after 90s" in error["content"]  # the prose is untouched
        assert error["meta"]["kind"] == SESSION_START_FAILED_KIND

    @pytest.mark.asyncio
    async def test_an_untagged_failure_keeps_a_plain_error_row(self, tmp_path):
        state, _client = _runner_state(tmp_path)
        state.sessions.get_or_create = AsyncMock(side_effect=RuntimeError("something else"))
        slot = _slot()

        await _drive(state, slot, "hello")

        error = next(row for row in slot.messages if row["role"] == "error")
        assert "kind" not in (error.get("meta") or {})


class TestRunChatWakaTimeCodingAccounting:
    @staticmethod
    def _wakatime_config():
        config = chat_runner.KiroCrewConfig.load()
        config.wakatime.enabled = True
        config.wakatime.send_heartbeats = True
        return config

    @pytest.mark.asyncio
    async def test_statusless_coding_call_demotes_when_permission_is_denied(self, tmp_path):
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        tool_call_id = "tc-wt-denied"
        _set_stream(
            client,
            [
                _coding_tool_call(tool_call_id),
                _permission(
                    title="Writing the file",
                    tool_kind="edit",
                    tool_call_id=tool_call_id,
                    is_shell=False,
                ),
                _complete(),
            ],
        )

        def _deny_when_registered() -> None:
            future = slot._approval_futures.get("req-cov-1")
            if future is not None and not future.done():
                future.set_result("rejected")

        state.push_slots_update.side_effect = _deny_when_registered
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity") as note_activity,
        ):
            await _drive(state, slot)

        client.reject_tool.assert_awaited_once_with("req-cov-1")
        note_activity.assert_not_called()

    @pytest.mark.asyncio
    async def test_denied_coding_call_emits_no_cadence_heartbeat(self, tmp_path):
        # F1 regression: the cadence beat fires only on CONFIRMED coding, never at
        # pre-decision dispatch, so a denied write emits neither the attribution
        # beat nor a cadence beat — no is_write ai-coding row for work that never
        # ran.
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        tool_call_id = "tc-wt-denied-cad"
        _set_stream(
            client,
            [
                _coding_tool_call(tool_call_id),
                _permission(
                    title="Writing the file",
                    tool_kind="edit",
                    tool_call_id=tool_call_id,
                    is_shell=False,
                ),
                _complete(),
            ],
        )

        def _deny_when_registered() -> None:
            future = slot._approval_futures.get("req-cov-1")
            if future is not None and not future.done():
                future.set_result("rejected")

        state.push_slots_update.side_effect = _deny_when_registered
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity") as note_activity,
            patch.object(chat_runner, "note_coding_cadence") as note_cadence,
        ):
            await _drive(state, slot)

        client.reject_tool.assert_awaited_once_with("req-cov-1")
        note_activity.assert_not_called()
        note_cadence.assert_not_called()

    @pytest.mark.asyncio
    async def test_statusless_coding_call_counts_after_permission_is_approved(self, tmp_path):
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        tool_call_id = "tc-wt-approved"
        _set_stream(
            client,
            [
                _coding_tool_call(tool_call_id),
                _permission(
                    title="Writing the file",
                    tool_kind="edit",
                    tool_call_id=tool_call_id,
                    is_shell=False,
                ),
                _complete(),
            ],
        )

        def _approve_when_registered() -> None:
            future = slot._approval_futures.get("req-cov-1")
            if future is not None and not future.done():
                future.set_result("approved")

        state.push_slots_update.side_effect = _approve_when_registered
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity") as note_activity,
        ):
            await _drive(state, slot)

        client.approve_tool.assert_awaited_once_with("req-cov-1")
        note_activity.assert_called_once()

    @pytest.mark.asyncio
    async def test_statusless_coding_call_without_permission_stays_counted(self, tmp_path):
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        _set_stream(client, [_statusless_coding_tool_call(), _complete()])

        config = self._wakatime_config()
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=config,
            ),
            patch.object(chat_runner, "note_coding_activity") as note_activity,
        ):
            await _drive(state, slot)

        client.approve_tool.assert_not_awaited()
        client.reject_tool.assert_not_awaited()
        note_activity.assert_called_once()
        assert note_activity.call_args.kwargs["config"] is config

    @pytest.mark.asyncio
    async def test_heartbeat_project_is_the_turn_start_snapshot(self, tmp_path):
        """A project picked while the turn streams does not relabel its heartbeat.

        The pick lands as the session registers (the route is free to write
        slot.project from then on); the heartbeat still names the project the
        turn prepared on and runs in.
        """
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        start = tmp_path / "start-project"
        picked = tmp_path / "picked-mid-turn"
        start.mkdir()
        picked.mkdir()
        slot.project = str(start)

        async def _claim(*_args, **_kwargs):
            slot.project = str(picked)
            return client, True, False

        state.sessions.get_or_create = AsyncMock(side_effect=_claim)
        _set_stream(client, [_statusless_coding_tool_call(), _complete()])
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity") as note_activity,
        ):
            await _drive(state, slot)

        note_activity.assert_called_once()
        assert note_activity.call_args.args[0] == str(start)
        assert slot.project == str(picked)

    @pytest.mark.asyncio
    async def test_many_auto_approved_coding_calls_keep_one_turn_bit(self, tmp_path):
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        events = [_statusless_coding_tool_call(f"tc-wt-{i}") for i in range(500)]
        _set_stream(client, [*events, _complete()])

        config = self._wakatime_config()
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=config,
            ),
            patch.object(chat_runner, "note_coding_activity") as note_activity,
        ):
            await _drive(state, slot)

        # Per-call ids are not retained: any number of successful coding calls
        # collapses to one bounded bit and one turn-level heartbeat.
        note_activity.assert_called_once()
        assert note_activity.call_args.kwargs["config"] is config

    @pytest.mark.asyncio
    async def test_auto_approved_coding_call_emits_one_cadence_heartbeat(self, tmp_path):
        # Duration accrual for trusted tools: an auto-approved coding call never
        # reaches a permission decision, so _wt_note_approved never fires. Its
        # completed result frame is the point past gating where it has run, so a
        # cadence beat fires there — a turn of pre-authorized coding tools
        # accrues intra-turn duration like a prompted one, not just one
        # end-of-turn instant.
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        tool_call_id = "tc-cad-auto"
        _set_stream(
            client,
            [
                _coding_tool_call(tool_call_id),
                _coding_tool_result(tool_call_id),
                _complete(),
            ],
        )

        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity"),
            patch.object(chat_runner, "note_coding_cadence") as note_cadence,
        ):
            await _drive(state, slot)

        client.approve_tool.assert_not_awaited()
        client.reject_tool.assert_not_awaited()
        note_cadence.assert_called_once_with(slot.project or None)

    @pytest.mark.asyncio
    async def test_many_auto_approved_coding_results_emit_one_throttled_cadence(self, tmp_path):
        # The throttle: several auto-approved coding calls completing inside one
        # turn (well under CADENCE_MIN_INTERVAL_SECS apart) collapse to a single
        # cadence beat, matching WakaTime's coalescing bucket, instead of one
        # beat per completed tool.
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        pairs = []
        for i in range(5):
            tcid = f"tc-cad-burst-{i}"
            pairs.append(_coding_tool_call(tcid))
            pairs.append(_coding_tool_result(tcid))
        _set_stream(client, [*pairs, _complete()])

        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity"),
            patch.object(chat_runner, "note_coding_cadence") as note_cadence,
        ):
            await _drive(state, slot)

        client.approve_tool.assert_not_awaited()
        note_cadence.assert_called_once_with(slot.project or None)

    @pytest.mark.asyncio
    async def test_approved_coding_call_emits_one_cadence_heartbeat(self, tmp_path):
        # Duration accrual: an approved coding call emits a cadence heartbeat so
        # WakaTime sees a mid-turn timestamp and accrues wall-clock duration,
        # instead of a long turn collapsing to a single end-of-turn instant.
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        tool_call_id = "tc-cad-approved"
        _set_stream(
            client,
            [
                _coding_tool_call(tool_call_id),
                _permission(
                    title="Writing the file",
                    tool_kind="edit",
                    tool_call_id=tool_call_id,
                    is_shell=False,
                ),
                _complete(),
            ],
        )

        def _approve_when_registered() -> None:
            future = slot._approval_futures.get("req-cov-1")
            if future is not None and not future.done():
                future.set_result("approved")

        state.push_slots_update.side_effect = _approve_when_registered
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity"),
            patch.object(chat_runner, "note_coding_cadence") as note_cadence,
        ):
            await _drive(state, slot)

        client.approve_tool.assert_awaited_once_with("req-cov-1")
        note_cadence.assert_called_once_with(slot.project or None)

    @pytest.mark.asyncio
    async def test_denied_pending_id_is_drained_before_a_later_decision(self, tmp_path):
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        tool_call_id = "tc-wt-drained"
        _set_stream(
            client,
            [
                _coding_tool_call(tool_call_id),
                _permission(
                    request_id="req-wt-deny",
                    tool_call_id=tool_call_id,
                    tool_kind="edit",
                    is_shell=False,
                ),
                # No second tool call: if the first decision failed to drain the
                # pending id, this unrelated duplicate approval would count it.
                _permission(
                    request_id="req-wt-later",
                    tool_call_id=tool_call_id,
                    tool_kind="read",
                    is_shell=False,
                ),
                _complete(),
            ],
        )

        def _decide_when_registered() -> None:
            deny = slot._approval_futures.get("req-wt-deny")
            if deny is not None and not deny.done():
                deny.set_result("rejected_once")
            approve = slot._approval_futures.get("req-wt-later")
            if approve is not None and not approve.done():
                approve.set_result("approved")

        state.push_slots_update.side_effect = _decide_when_registered
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity") as note_activity,
        ):
            await _drive(state, slot)

        client.reject_tool.assert_awaited_once_with("req-wt-deny")
        client.approve_tool.assert_awaited_once_with("req-wt-later")
        note_activity.assert_not_called()

    @pytest.mark.asyncio
    async def test_collapsed_coding_id_cannot_be_counted_by_unrelated_approval(self, tmp_path):
        from kiro_crew.security import REDACTED_CREDENTIAL_TAG

        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        coding_tool_call_id = "AKIAIOSFODNN7EXAMPLE"
        _set_stream(
            client,
            [
                # This non-coding call records the literal redaction tag as the
                # first raw source. The distinct credential-shaped coding id
                # redacts to the same key and therefore collapses its identity.
                LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="Reading the file",
                    tool_call_id=REDACTED_CREDENTIAL_TAG,
                    tool_kind="read",
                    tool_name="fs_read",
                ),
                _coding_tool_call(coding_tool_call_id),
                # An unrelated approval under the collapsed key must not consume
                # and count the coding call. Its own denial follows afterward.
                _permission(
                    request_id="req-wt-unrelated",
                    tool_call_id=REDACTED_CREDENTIAL_TAG,
                    tool_kind="read",
                    is_shell=False,
                ),
                _permission(
                    request_id="req-wt-deny",
                    tool_call_id=coding_tool_call_id,
                    tool_kind="edit",
                    is_shell=False,
                ),
                _complete(),
            ],
        )

        def _decide_when_registered() -> None:
            approve = slot._approval_futures.get("req-wt-unrelated")
            if approve is not None and not approve.done():
                approve.set_result("approved")
            deny = slot._approval_futures.get("req-wt-deny")
            if deny is not None and not deny.done():
                deny.set_result("rejected_once")

        state.push_slots_update.side_effect = _decide_when_registered
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity") as note_activity,
        ):
            await _drive(state, slot)

        client.approve_tool.assert_awaited_once_with("req-wt-unrelated")
        client.reject_tool.assert_awaited_once_with("req-wt-deny")
        note_activity.assert_not_called()

    @pytest.mark.asyncio
    async def test_restored_queued_turn_does_not_emit_heartbeat(self, tmp_path):
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        _set_stream(client, [_statusless_coding_tool_call(), _complete()])
        slot._empty_response_retries = 2

        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity") as note_activity,
            _quiet_sel(),
        ):
            await chat_runner._run_chat(
                state,
                slot,
                "hello",
                _turn_provenance_restored=True,
            )
        await _settle(slot)

        client.stream.assert_called_once()
        note_activity.assert_not_called()

    @pytest.mark.asyncio
    async def test_restored_queued_turn_emits_no_cadence_heartbeat(self, tmp_path):
        # Opus regression: a gateway-restart replay (restored queued prompt)
        # lost its actor stamp, so even an approved coding call must not emit a
        # cadence beat — the provenance guard keeps the user fallback from
        # misclassifying the replay as interactive.
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        tool_call_id = "tc-cad-restored"
        _set_stream(
            client,
            [
                _coding_tool_call(tool_call_id),
                _permission(
                    title="Writing the file",
                    tool_kind="edit",
                    tool_call_id=tool_call_id,
                    is_shell=False,
                ),
                _complete(),
            ],
        )

        def _approve_when_registered() -> None:
            future = slot._approval_futures.get("req-cov-1")
            if future is not None and not future.done():
                future.set_result("approved")

        state.push_slots_update.side_effect = _approve_when_registered
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity"),
            patch.object(chat_runner, "note_coding_cadence") as note_cadence,
        ):
            await chat_runner._run_chat(
                state,
                slot,
                "hello",
                _turn_provenance_restored=True,
            )
        await _settle(slot)

        client.approve_tool.assert_awaited_once_with("req-cov-1")
        note_cadence.assert_not_called()

    @pytest.mark.asyncio
    async def test_mcp_name_fallback_is_not_used_by_chat_runner(self, tmp_path):
        # A write-named MCP tool with no edit kind must not fall back to the
        # tool name to count as coding activity: only a recognised coding kind
        # counts, so a third-party MCP that happens to be named "write" stays
        # out of the WakaTime lane.
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        _set_stream(
            client,
            [
                _coding_tool_call(
                    tool_kind="",
                    tool_name="write",
                    mcp_server_name="third-party-mcp",
                ),
                _complete(),
            ],
        )

        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity") as note_activity,
        ):
            await _drive(state, slot)

        client.stream.assert_called_once()
        note_activity.assert_not_called()

    @pytest.mark.asyncio
    async def test_many_undecided_pending_coding_ids_stay_capped(self, tmp_path, caplog):
        # A flood of pending coding calls whose permission decision never
        # arrives must not grow the retained set without bound: past the cap the
        # key is dropped and tallied, and one summary is emitted at turn end
        # (never a line per drop). The undecided calls reached no gating denial,
        # so the turn does count as coding activity — the point of this test is
        # the bound and the single summary, not suppression.
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        over_cap = chat_runner._MAX_TCID_SOURCES + 50
        events = [_coding_tool_call(f"tc-wt-pending-{i}") for i in range(over_cap)]
        _set_stream(client, [*events, _complete()])

        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity") as note_activity,
            caplog.at_level("WARNING", logger=chat_runner.logger.name),
        ):
            await _drive(state, slot)

        # Exactly one shed summary for the whole turn, not one line per dropped
        # call.
        shed = [
            r
            for r in caplog.records
            if r.name == chat_runner.logger.name and "wakatime pending-coding cap" in r.message
        ]
        assert len(shed) == 1
        assert "shed" in shed[0].message
        # Unguarded execution counts once for the turn.
        note_activity.assert_called_once()

    @pytest.mark.asyncio
    async def test_mcp_edit_kind_still_emits_heartbeat(self, tmp_path):
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        _set_stream(
            client,
            [
                _coding_tool_call(
                    tool_kind="edit",
                    tool_name="anything",
                    mcp_server_name="third-party-mcp",
                ),
                _complete(),
            ],
        )

        config = self._wakatime_config()
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=config,
            ),
            patch.object(chat_runner, "note_coding_activity") as note_activity,
        ):
            await _drive(state, slot)

        note_activity.assert_called_once()
        assert note_activity.call_args.kwargs["config"] is config

    @pytest.mark.asyncio
    async def test_heartbeat_uses_project_captured_at_turn_start(self, tmp_path):
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        project_at_start = str(tmp_path / "project-a")
        project_after_switch = str(tmp_path / "project-b")
        slot.project = project_at_start

        async def _events():
            yield _statusless_coding_tool_call()
            slot.project = project_after_switch
            yield _complete()

        client.stream = MagicMock(return_value=_events())
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity") as note_activity,
        ):
            await _drive(state, slot)

        assert slot.project == project_after_switch
        note_activity.assert_called_once()
        assert note_activity.call_args.args[0] == project_at_start

    @pytest.mark.asyncio
    async def test_injected_turn_does_not_emit_heartbeat(self, tmp_path):
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        _set_stream(client, [_statusless_coding_tool_call(), _complete()])

        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity") as note_activity,
        ):
            await _drive(state, slot, _turn_actor="cron")

        client.stream.assert_called_once()
        note_activity.assert_not_called()

    @pytest.mark.asyncio
    async def test_injected_turn_emits_no_cadence_heartbeat(self, tmp_path):
        # The cadence beat honors the same scope as the end-of-turn beat: a
        # cron/subagent/injected (non-user) turn is outside the declared surface,
        # so even an APPROVED coding call emits no cadence beat.
        state, client = _runner_state(tmp_path)
        client.mcp_session_report = MagicMock(return_value=None)
        slot = _slot()
        tool_call_id = "tc-cad-cron"
        _set_stream(
            client,
            [
                _coding_tool_call(tool_call_id),
                _permission(
                    title="Writing the file",
                    tool_kind="edit",
                    tool_call_id=tool_call_id,
                    is_shell=False,
                ),
                _complete(),
            ],
        )

        def _approve_when_registered() -> None:
            future = slot._approval_futures.get("req-cov-1")
            if future is not None and not future.done():
                future.set_result("approved")

        state.push_slots_update.side_effect = _approve_when_registered
        with (
            patch.object(
                chat_runner.KiroCrewConfig,
                "load",
                return_value=self._wakatime_config(),
            ),
            patch.object(chat_runner, "note_coding_activity"),
            patch.object(chat_runner, "note_coding_cadence") as note_cadence,
        ):
            await _drive(state, slot, _turn_actor="cron")

        client.approve_tool.assert_awaited_once_with("req-cov-1")
        note_cadence.assert_not_called()


#: Fixture tokens for the glued-credential cases (made up, not real keys).
_AKIA_SAMPLE = "AKIA" + "5Q7XTR2M9LBC4WZN"
_PEM_BEGIN = "-----BEGIN " + "PRIVATE KEY-----"
_PEM_END = "-----END " + "PRIVATE KEY-----"
_GLUED_PAT_A = "ghp_" + "eG7e5uHyQp2Lw9Rk4Ts8Vb1Nx6Jm3Cd0Zf5"
_GLUED_PAT_B = "ghp_" + "Kd8Wq2Hr6Tz0Lp4Ys9Mb3Vn7Xc1Jg5Rf2Pa"
_GLUED_SK = (
    "sk-ant-api03-" + "upB1YZZcv7k7Qw3Er5Ty8Ui0Op2As4Df6Gh8Jk1Lz3Xc5Vb7Nm9Qa2Ws4" + "-CaI4AA"
)


class TestBlockedLinkCardRecords:
    """What the dashboard's blocked-link card lists after a flush.

    The card lists the host's records in the saved reply, each under its own
    address, and never claims which one a chip stands for
    (``BlockedLinkCard`` in ``website/src/components/RedactionCards.tsx``).
    Counts cannot prove that a placeholder and a record are the same URL, so
    these pin what each record says about itself. They stream the deltas
    through both per-delta removers in the order the streaming loop applies
    them, then flush.
    """

    H = "h.example-sink.net"
    KEY = "AKIA" + "IOSFODNN7EXAMPLE"  # AWS docs example key, split so no literal sits in the repo

    @staticmethod
    def _flush(deltas: list[str]) -> tuple[str, list[dict]]:
        slot = _slot()
        text = ""
        for delta in deltas:
            safe, removed = chat_runner._redact_delta(delta)
            chat_runner._accumulate_segment_raw(slot, delta, safe, removed)
            text += safe
        redacted, links, _ = chat_runner._redact_segment(slot, text)
        return redacted, links

    def test_one_address_on_its_host_is_its_one_record(self):
        url = f"https://{self.H}/only?q=" + "a" * 250
        content, links = self._flush(["See ", url[:40], url[40:], " now."])
        assert content.count(f"[REDACTED: suspicious URL to {self.H}]") == 1
        assert [r["url"] for r in links] == [url]

    @pytest.mark.parametrize(
        "cut_before",
        [
            "?X-Amz-Algorithm",
            "&X-Amz-Credential",
            "IOSFODNN7",
            "&X-Amz-Date",
            "&X-Amz-Security-Token",
            "&X-Amz-Signature",
            "bbbb",
        ],
    )
    def test_presigned_link_cut_by_a_delta_saves_as_one_placeholder(self, cut_before):
        """Wherever a delta boundary cuts a blocked presigned link, the saved
        reply (what a copy and a channel post read) holds one placeholder and
        none of the link's query."""
        host = "my-bucket.s3.us-east-1.amazonaws.com"
        url = (
            f"https://{host}/report.csv?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential={self.KEY}"
            "%2F20261008%2Fus-east-1%2Fs3%2Faws4_request&X-Amz-Date=20261008T000000Z"
            "&X-Amz-Expires=3600&X-Amz-SignedHeaders=host&X-Amz-Security-Token="
            + "T" * 300
            + "&X-Amz-Signature="
            + "b" * 64
        )
        cut = url.index(cut_before)
        content, _ = self._flush(["Link: ", url[:cut], url[cut:], " . done"])
        assert content == f"Link: [REDACTED: suspicious URL to {host}] . done"

    def test_presigned_links_cut_anywhere_save_no_query(self):
        """A sweep of delta cuts through three presigned layouts (every 7th single
        cut and fixed sets of two to five cuts): no saved reply keeps the session
        token, the signature or any other query text of the link."""
        import random

        host = "my-bucket.s3.us-east-1.amazonaws.com"
        cred = f"X-Amz-Credential={self.KEY}%2F20261008%2Fus-east-1%2Fs3%2Faws4_request"
        layouts = [
            f"https://{host}/r.csv?X-Amz-Algorithm=AWS4-HMAC-SHA256&{cred}&X-Amz-Date=20261008T000000Z"
            "&X-Amz-Expires=3600&X-Amz-Security-Token="
            + "T" * 300
            + "&X-Amz-Signature="
            + "b" * 64,
            f"https://{host}/r.csv?X-Amz-Security-Token="
            + "T" * 300
            + f"&{cred}&X-Amz-Signature="
            + "b" * 64,
            f"https://{host}/r.csv?{cred}&X-Amz-Signature=" + "b" * 64 + "&leak=1",
        ]
        rng = random.Random(20769)
        for url in layouts:
            cut_sets = [[c] for c in range(1, len(url), 7)]
            cut_sets += [
                sorted(rng.sample(range(1, len(url)), k)) for k in (2, 3, 5) for _ in range(10)
            ]
            for cuts in cut_sets:
                bounds = [0, *cuts, len(url)]
                pieces = [url[a:b] for a, b in zip(bounds, bounds[1:])]
                content, _ = self._flush(["Link: ", *pieces, " . done"])
                assert "TTTT" not in content and "bbbb" not in content, cuts
                assert content == f"Link: [REDACTED: suspicious URL to {host}] . done", cuts

    def test_an_echoed_same_host_placeholder_does_not_keep_a_cut_link_tail(self):
        """The reply already quotes a placeholder for the host, then a blocked
        link on it is cut before ``&k=<token>``: the saved reply keeps neither
        the session token nor the signature."""
        host = "my-bucket.s3.us-east-1.amazonaws.com"
        token = "ghp_" + "A1b2C3d4E5" * 3 + "f6G7h8"
        url = (
            f"https://{host}/r?X-Amz-Credential={self.KEY}%2Fx&k={token}"
            "&X-Amz-Security-Token=" + "T" * 300 + "&X-Amz-Signature=" + "b" * 64
        )
        cut = url.index("&k=")
        echoed = f"Earlier: [REDACTED: suspicious URL to {host}]. Now: "
        content, _ = self._flush([echoed, url[:cut], url[cut:], " . done"])
        assert "TTTT" not in content and "bbbb" not in content
        assert content.endswith(f"[REDACTED: suspicious URL to {host}] . done")

    def test_two_echoed_placeholders_do_not_shelter_a_cut_link_tail(self):
        """Two quoted same-host placeholders, then a presigned link cut before
        ``&X-Amz-Security-Token``: the saved reply keeps no token or signature."""
        host = "my-bucket.s3.us-east-1.amazonaws.com"
        ph = f"[REDACTED: suspicious URL to {host}]"
        url = (
            f"https://{host}/r?X-Amz-Credential={self.KEY}%2Fx&X-Amz-Security-Token="
            + "T" * 300
            + "&X-Amz-Signature="
            + "b" * 64
        )
        cut = url.index("&X-Amz-Security-Token")
        content, _ = self._flush([f"{ph}. Link: {ph}. Link: ", url[:cut], url[cut:], " done"])
        assert "TTTT" not in content and "bbbb" not in content

    def test_a_credential_tag_spanning_query_separators_does_not_shelter_the_rest(self):
        """A delta whose credential replacement swallows ``&`` separators sits
        inside a blocked address: the token and signature after it are not saved."""
        secret = "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCY" + "EXAMPLEKEY"  # AWS docs example, split
        deltas = [
            "See https://example.com/r?",
            f"aws_secret_access_key={secret}&x=1&id={self.KEY}",
            "&X-Amz-Security-Token=" + "T" * 300 + "&X-Amz-Signature=" + "b" * 64,
            " done",
        ]
        content, _ = self._flush(deltas)
        assert "TTTT" not in content and "bbbb" not in content

    def test_a_nested_link_cut_inside_a_blocked_query_saves_no_tail(self):
        """A blocked link whose query holds a nested ``https://`` address, cut
        right before it and again inside its data: no query text is saved."""
        url = (
            "https://host.example/r?u=https://other.example/?d="
            + "QUJD" * 60
            + "XYZsession-"
            + "Z" * 40
        )
        a, b = url.index("https://other"), url.index("XYZ")
        content, _ = self._flush(["see ", url[:a], url[a:b], url[b:], " ok"])
        assert "XYZ" not in content and "ZZZZ" not in content

    def test_a_nested_presigned_link_behind_a_long_outer_query_saves_no_tail(self):
        """An outer link ending at ``?pad=``, 210 filler characters and ``&u=``,
        a nested presigned link through its key, then its token and signature,
        each in its own delta: no token or signature is saved."""
        host = "my-bucket.s3.us-east-1.amazonaws.com"
        nested = (
            f"https://{host}/r?X-Amz-Credential={self.KEY}%2Fx&X-Amz-Security-Token="
            + "T" * 300
            + "&X-Amz-Signature="
            + "b" * 64
        )
        k = nested.index("&X-Amz-Security-Token")
        deltas = [
            "see https://outer.example/r?pad=",
            "p" * 210 + "&u=",
            nested[:k],
            nested[k:],
            " ok",
        ]
        content, _ = self._flush(deltas)
        assert "TTTT" not in content and "bbbb" not in content

    def test_a_credential_pass_that_eats_the_address_head_saves_no_tail(self):
        """``aws_secret_access_key=`` then a presigned link, cut right before its
        ``?``: per delta the credential pass swallows the scheme and host with
        the value; the saved copy is redacted whole, so no query is saved."""
        host = "my-bucket.s3.us-east-1.amazonaws.com"
        url = (
            f"https://{host}/r.csv?X-Amz-Credential={self.KEY}%2Fx&X-Amz-Security-Token="
            + "T" * 300
            + "&X-Amz-Signature="
            + "b" * 64
        )
        q = url.index("?")
        for label in ("aws_secret_access_key=", "aws_secret_access_key: "):
            content, _ = self._flush([f"Set {label}{url[:q]}", url[q:], " done"])
            assert "X-Amz" not in content and "TTTT" not in content and "bbbb" not in content
            # The per-delta credential value holds the host, so the saved
            # placeholder's host pieces touch a removed credential and nothing
            # but main's own text may vouch for them: the segment is withheld.
            assert content == chat_runner._SEGMENT_UNCHECKED_TEXT

    def test_a_credential_label_that_damages_a_placeholder_saves_no_tail(self):
        """``aws_secret_access_key: <presigned link>`` cut before
        ``&X-Amz-Signature``: the label's credential pass eats the placeholder's
        ``[REDACTED:`` head, and the signature after it is still not saved."""
        host = "my-bucket.s3.us-east-1.amazonaws.com"
        url = f"https://{host}/r.csv?X-Amz-Credential={self.KEY}%2Fx&X-Amz-Signature=" + "b" * 64
        cut = url.index("&X-Amz-Signature")
        for label in ("aws_secret_access_key: ", "aws_secret_access_key "):
            content, _ = self._flush([f"Set {label}{url[:cut]}", url[cut:], " done"])
            assert "bbbb" not in content and "X-Amz" not in content, content

    def test_a_damaged_nested_placeholder_inside_an_outer_link_saves_no_tail(self):
        """An outer link's query holds ``aws_secret_access_key=`` and a nested
        presigned link through its key, then the signature in its own delta:
        the damaged nested placeholder is restored before the outer fold, and
        the signature is not saved."""
        host = "my-bucket.s3.us-east-1.amazonaws.com"
        nested = f"https://{host}/r?X-Amz-Credential={self.KEY}%2Fx"
        for label in ("aws_secret_access_key=", "aws_secret_access_key: "):
            deltas = [
                "see https://outer.example/r?u=",
                f"{label}{nested}",
                "&X-Amz-Signature=" + "b" * 64,
                " ok",
            ]
            content, _ = self._flush(deltas)
            assert "bbbb" not in content and "X-Amz" not in content, content

    def test_an_unrelated_placeholder_glued_before_a_cut_link_shelters_nothing(self):
        """A placeholder for another host glued straight before a presigned
        link cut at ``&X-Amz-Security-Token``: no token or signature is saved."""
        host = "my-bucket.s3.us-east-1.amazonaws.com"
        url = (
            f"https://{host}/r?X-Amz-Credential={self.KEY}%2Fx&X-Amz-Security-Token="
            + "T" * 300
            + "&X-Amz-Signature="
            + "b" * 64
        )
        cut = url.index("&X-Amz-Security-Token")
        other = "[REDACTED: suspicious URL to other.example]"
        content, _ = self._flush([f"see {other}", url[:cut], url[cut:], " ok"])
        assert "TTTT" not in content and "bbbb" not in content, content

    def test_a_segment_with_no_cut_or_no_blocked_host_saves_as_main_does(self):
        """With no blocked link on a placeholder's host, the folds change
        nothing: the saved text equals one pass of both removers over it, even
        with quoted placeholders and a blocked link on another host."""
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        host = "my-bucket.s3.us-east-1.amazonaws.com"
        ph = f"[REDACTED: suspicious URL to {host}]"
        url = f"https://{host}/r?X-Amz-Credential={self.KEY}%2Fx&X-Amz-Signature=" + "b" * 64
        for text in (
            f"Link: {url.replace(host, 'other-bucket.s3.amazonaws.com')} and **{ph}**, quoted.",
            f"Quoted `{ph}`. Plain https://example.com/a?b=1 here.",
            "No links at all, just [REDACTED: credential] text.",
        ):
            once, _ = redact_exfiltration_urls(text)
            once, _ = redact_credentials(once)
            content, _ = self._flush([text])
            assert content == once

    def test_many_glued_unrelated_placeholders_save_without_recursion(self):
        """1,100 glued placeholders for another host before a presigned link
        cut at ``&X-Amz-Signature``: the flush finishes and saves no signature."""
        host = "my-bucket.s3.us-east-1.amazonaws.com"
        url = f"https://{host}/r?X-Amz-Credential={self.KEY}%2Fx&X-Amz-Signature=" + "b" * 64
        cut = url.index("&X-Amz-Signature")
        many = "[REDACTED: suspicious URL to other.example]" * 1100
        content, _ = self._flush([f"see {many}", url[:cut], url[cut:], " ok"])
        assert "bbbb" not in content

    def test_a_kept_link_keeps_its_query_after_a_masked_credential(self):
        """A kept link on another host whose credential a delta masked keeps
        the query after the tag, even when that query also appears in a blocked
        link in the same segment."""
        long = "A" * 260
        kept = "https://api.ok.example/v1?token=opaque1234567890abcdef&format=json"
        deltas = [f"bad https://evil.example/x?a={long}&format=json then ", kept, " end"]
        content, _ = self._flush(deltas)
        assert "&format=json end" in content

    def test_a_tag_inside_an_unblocked_outer_link_is_not_a_kept_link(self):
        """``https://outer.example/r?u=aws_secret_access_key: <presigned>``: the
        space ends the outer link in the raw pass, so it is kept, but the tag the
        label wrote inside it is not that kept link, and the query goes."""
        host = "my-bucket.s3.us-east-1.amazonaws.com"
        url = (
            f"https://{host}/r?X-Amz-Credential={self.KEY}%2Fx&X-Amz-Security-Token="
            + "T" * 60
            + "&X-Amz-Signature="
            + "b" * 40
        )
        text = f"see https://outer.example/r?u=aws_secret_access_key: {url} ok"
        for cut in (56, 81, 87, 98, 134):
            content, _ = self._flush([text[:cut], text[cut:]])
            assert "TTTT" not in content and "bbbb" not in content, (cut, content)

    def test_a_segment_too_large_for_a_raw_copy_fails_closed(self):
        """Past the raw copy's cap the flush cannot redact the segment as one
        text, so it saves the withheld notice, not the streamed pieces: a link
        cut before its key leaves no placeholder in the streamed text at all."""
        host = "my-bucket.s3.us-east-1.amazonaws.com"
        url = f"https://{host}/r?X-Amz-Credential={self.KEY}%2Fx&X-Amz-Signature=" + "b" * 64
        for cut_before in ("&X-Amz-Signature", "?X-Amz-Credential"):
            cut = url.index(cut_before)
            with patch.object(chat_runner, "_SEGMENT_RAW_MAX_CHARS", 10):
                content, links = self._flush(["see ", url[:cut], url[cut:], " ok"])
            assert (content, links) == (chat_runner._SEGMENT_UNCHECKED_TEXT, [])

    def test_a_saved_segment_is_the_raw_copy_redacted_once(self):
        """Quoted placeholders, a kept same-host link with a masked token and a
        cut blocked link in one segment: the saved text is the raw copy through
        both removers once, so nothing next to a placeholder is lost."""
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        host = "my-bucket.s3.us-east-1.amazonaws.com"
        ph = f"[REDACTED: suspicious URL to {host}]"
        url = f"https://{host}/r?X-Amz-Credential={self.KEY}%2Fx&X-Amz-Signature=" + "b" * 64
        cut = url.index("&X-Amz-Signature")
        deltas = [
            f"Earlier {ph}was blocked; also {ph} stays. Kept https://{host}/k?token=",
            "opaque1234567890abcdef ",
            "now ",
            url[:cut],
            url[cut:],
            " end",
        ]
        once = redact_credentials(redact_exfiltration_urls("".join(deltas))[0])[0]
        content, _ = self._flush(deltas)
        assert content == once
        assert content.startswith(f"Earlier {ph}was blocked; also {ph} stays.")
        assert "bbbb" not in content

    def test_a_hostname_shaped_like_a_key_does_not_shelter_a_cut_query(self):
        """A host whose first label is an access-key shape, its presigned link
        cut at every 5th character: no saved reply keeps the query."""
        host = f"{self.KEY}.s3.us-east-1.amazonaws.com"
        url = (
            f"https://{host}/r?X-Amz-Credential={self.KEY}%2Fx&X-Amz-Security-Token="
            + "T" * 300
            + "&X-Amz-Signature="
            + "b" * 64
        )
        for cut in range(1, len(url), 5):
            content, _ = self._flush(["see ", url[:cut], url[cut:], " ok"])
            assert "TTTT" not in content and "bbbb" not in content, (cut, content)

    def test_the_streamed_footer_reflow_is_applied_to_the_saved_copy(self):
        """A glued ``[OPTIONS:]`` footer split across deltas is saved reflowed,
        the same repair the streamed text gets."""
        from kiro_crew.constants import reflow_and_label_glued_option_marker

        raw = "Pick one.\n[OPTIONS: A | B]Anytime."
        content, _ = self._flush([raw[:18], raw[18:]])
        assert content == reflow_and_label_glued_option_marker(raw)[0] != raw

    def test_a_value_only_a_per_delta_pass_removed_withholds_the_segment(self):
        """When the whole-text pass would keep a value a per-delta pass
        replaced, the segment is withheld. The raw copy is never edited to drop
        the value: a mask changes what the next pass sees."""
        slot = _slot()
        chat_runner._accumulate_segment_raw(slot, "hello ", "hello ", ())
        tag = "[REDACTED: credential]"
        chat_runner._accumulate_segment_raw(slot, "world", tag, [("world", tag)])
        result = chat_runner._redact_segment(slot, f"hello {tag}")
        assert result == (chat_runner._SEGMENT_UNCHECKED_TEXT, [], [])

    def test_a_kept_removed_value_fails_closed(self):
        slot = _slot()
        tag = "[REDACTED: credential]"
        chat_runner._accumulate_segment_raw(slot, "world", tag, [("world", tag)])
        with patch.object(chat_runner, "_kept_removed_values", return_value=["world"]):
            result = chat_runner._redact_segment(slot, tag)
        assert result == (chat_runner._SEGMENT_UNCHECKED_TEXT, [], [])

    def test_a_token_repeated_inside_a_blocked_link_is_never_saved(self):
        """Token A only a per-delta pass removes, then a
        blocked link whose path repeats A and whose query holds token B. A mask
        of A in the raw copy would put a space inside the link, cut it before
        its query and save B with no record. The raw copy is never masked, so
        the segment is withheld and neither token is saved anywhere."""
        a = "eyJ" + "a-b_" * 25 + "." + "c" * 21 + "-" + "d" * 21
        b = "eyJ" + "x-y_" * 25 + "." + "e" * 21 + "-" + "f" * 21
        deltas = [
            "prefix.",
            a,
            f" see https://h.example/r/{a}?q=prefix.{b}&padding=" + "z" * 80,
        ]
        content, links = self._flush(deltas)
        for token in (a, b):
            assert token not in content
            assert all(token not in str(r) for r in links)

    def test_each_removed_value_is_checked_on_its_own(self):
        """A token glued to the one before it is removed whole only by a
        per-delta pass, and the same delta also redacts a PAT. The whole-text
        pass's first match runs into the glued token and leaves its body in the
        text, so the saved text shows more than the streamed redaction did: the
        segment is withheld and neither value is saved."""
        first = "ghp_" + "Ab1" * 12
        token = "ghp_" + "Zq7" * 12
        pat = "ghp_" + "A1b2C3d4E5" * 3 + "f6G7h8"
        deltas = [f"tokens: {first}", f"{token} and {pat}", " end"]
        shown, removed = chat_runner._redact_delta(deltas[1])
        assert token not in shown and pat not in shown
        assert token in [value for value, _ in removed]
        content, _ = self._flush(deltas)
        assert content == chat_runner._SEGMENT_UNCHECKED_TEXT
        assert token not in content and pat not in content

    def test_a_record_holding_a_value_only_a_per_delta_pass_removed_fails_closed(self):
        """A link token glued to a word, which only a per-delta pass removes
        (the whole-text pass reads the letter before ``eyJ`` as part of a word),
        inside a blocked link's query, must not be saved in the link's record
        (``meta.blocked_links[].url``) any more than in the text. Any record
        string holding such a value fails the flush closed."""
        token = "eyJ" + "a-b_" * 25 + "." + "c" * 21 + "-" + "d" * 21
        deltas = [f"https://{self.H}/r?q=prefix", token, "&padding=" + "z" * 80]
        shown, removed = chat_runner._redact_delta(deltas[1])
        assert token not in shown and token in [value for value, _ in removed]
        content, links = self._flush(deltas)
        assert token not in content
        assert all(token not in str(r) for r in links)
        assert (content, links) == (chat_runner._SEGMENT_UNCHECKED_TEXT, [])

    def test_a_link_token_in_a_blocked_link_query_is_saved_in_neither_text_nor_record(self):
        """A link token after ``.`` inside a blocked link's query, split across
        deltas, is saved in neither the text nor the link's record
        (``meta.blocked_links[].url``): the whole-text pass removes it, so the
        segment saves the link's placeholder. The record check for a value only
        a per-delta pass removed is covered by the
        ``_records_hold_removed_credential`` tests below."""
        token = "eyJ" + "a-b_" * 25 + "." + "c" * 21 + "-" + "d" * 21
        deltas = [f"https://{self.H}/r?q=prefix.", token, "&padding=" + "z" * 80]
        shown, removed = chat_runner._redact_delta(deltas[1])
        assert token not in shown and token in [value for value, _ in removed]
        content, links = self._flush(deltas)
        assert token not in content
        assert all(token not in str(r) for r in links)
        assert content == f"[REDACTED: suspicious URL to {self.H}]"

    def test_a_check_past_the_work_ceiling_fails_closed_without_scanning(self):
        """Tens of thousands of removed values would cost seconds of substring
        work on the event loop; past the ceiling the segment is withheld."""
        slot = _slot()
        tag = chat_runner.REDACTED_CREDENTIAL_TAG
        chat_runner._accumulate_segment_raw(slot, "hello ", "hello ", ())
        chat_runner._accumulate_segment_raw(slot, "world", tag, [("world", tag)])
        with (
            patch.object(chat_runner, "_SEGMENT_CHECK_MAX_WORK", 1),
            patch.object(chat_runner, "_kept_removed_values") as kept,
        ):
            result = chat_runner._redact_segment(slot, f"hello {tag}")
        assert result == (chat_runner._SEGMENT_UNCHECKED_TEXT, [], [])
        kept.assert_not_called()
        removed = {"tok": tag}
        with patch.object(chat_runner, "_SEGMENT_CHECK_MAX_WORK", 3):
            assert chat_runner._records_hold_removed_credential([{"url": "abcd"}], removed)
            assert not chat_runner._records_hold_removed_credential([{"url": "abc"}], removed)

    def test_a_record_check_reads_quoted_fields_and_standalone_credentials_only(self):
        tag = chat_runner.REDACTED_CREDENTIAL_TAG
        token = "eyJ" + "a-b_" * 25 + "." + "c" * 21 + "-" + "d" * 21
        check = chat_runner._records_hold_removed_credential
        link_tag = "[REDACTED: suspicious URL to x.test]"
        assert not check([{"url": "https://x.test/a"}], {"https://x.test/a": link_tag})
        for field in ("url", "path", "domain"):
            assert check([{field: f"https://x.test/r?q=.{token}&z=1"}], {token: tag})
        assert not check([{"rule": token}], {token: tag})
        assert not check([{"url": token}], {token: link_tag})

    def test_a_credential_record_quoting_a_removed_value_fails_closed(self):
        """A credential record's source command and view command quote a tool
        command; a removed credential there fails the flush closed too."""
        tag = chat_runner.REDACTED_CREDENTIAL_TAG
        token = "eyJ" + "a-b_" * 25 + "." + "c" * 21 + "-" + "d" * 21
        check = chat_runner._records_hold_removed_credential
        command = f"printf prefix.{token}"
        base = {"ordinal": 0, "rule": "github_pat", "label": "", "profile_command": None}
        quoting = [
            dict(base, source={"type": "command", "command": command}, view_command=command),
            dict(base, source=None, view_command=command),
            dict(base, source={"type": "file", "path": f"/x/{token}", "section": None}),
        ]
        for record in quoting:
            assert check([], {token: tag}, [record])
        clean = dict(base, source={"type": "command", "command": "printenv A"}, view_command=None)
        assert not check([], {token: tag}, [clean])
        assert not check([], {token: tag}, [dict(base, rule=token, source=None, view_command=None)])

    def test_a_credential_a_glued_neighbour_cut_into_withholds_the_segment(self):
        """Two glued tokens cut between them: each delta removes its whole
        token, but the whole-text match of the first swallows the second's
        head. The rest of the second is not saved; the segment is withheld."""
        first, second = "ghp_" + "Ab1" * 12, "ghp_" + "Zq7" * 12
        content, links = self._flush(["tokens: " + first, second + " end"])
        assert (content, links) == (chat_runner._SEGMENT_UNCHECKED_TEXT, [])

    @pytest.mark.parametrize(
        "deltas",
        [
            pytest.param(
                [_GLUED_PAT_A[:9], _GLUED_PAT_A[9:] + _GLUED_SK + " done"],
                id="head-swallowed-and-tail-masked",
            ),
            pytest.param(
                [
                    "x " + _GLUED_PAT_A,
                    _GLUED_PAT_B + " y https://h.example-sink.net/p?aws_access_key_id=",
                    "AKIA" + "5Q7XTR2M9LBC4WZN",
                    "&note=" + _GLUED_PAT_B[-8:] + " z",
                ],
                id="an-unrelated-copy-of-the-tail",
            ),
            pytest.param(
                [
                    "x " + _GLUED_PAT_A,
                    _GLUED_PAT_B + " y",
                    " https://h.example-sink.net/p?note="
                    + _GLUED_PAT_B[3:]
                    + "&id=AKIA"
                    + "5Q7XTR2M9LBC4WZN z",
                ],
                id="a-blocked-address-quoting-the-body",
            ),
            *(
                pytest.param(
                    [
                        "x " + _GLUED_PAT_A,
                        _GLUED_PAT_B + " y",
                        f" https://h.example-sink.net/p?note={quoted}&id={_AKIA_SAMPLE} z",
                    ],
                    id=f"a-blocked-address-quoting-{name}",
                )
                for name, quoted in (
                    ("nothing", ""),
                    ("a-glued-token", "Q" + _GLUED_PAT_B),
                    ("a-suffixed-token", _GLUED_PAT_B + "Q"),
                    ("a-prefixed-body", "Q" + _GLUED_PAT_B[3:]),
                    ("a-suffixed-body", _GLUED_PAT_B[3:] + "z"),
                )
            ),
            pytest.param(
                [
                    "Words p ss w0rd cache internal 6379 ok. Key: " + _GLUED_PAT_A,
                    "redis://:p@ss.w0rd!@cache.internal:6379 ok",
                ],
                id="a-connection-string-a-glued-token-swallowed",
            ),
            pytest.param(
                [
                    "Words ://:p ss @cache.internal:6379 ok. Key: " + _GLUED_PAT_A,
                    "redis://:p(ss)@cache.internal:6379 ok",
                ],
                id="a-bracketed-password-whose-pieces-the-reply-repeats",
            ),
            pytest.param(
                [
                    "x [REDACTED: eyJ" + "a-b_" * 25 + "." + "c" * 21 + "-" + "d" * 21,
                    "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY end",
                ],
                id="a-forged-placeholder-opener",
            ),
            pytest.param(
                [
                    "x " + _GLUED_PAT_A,
                    _GLUED_PAT_B + " y\n-----BEGIN PR",
                    "IVATE KEY-----\nhttps://" + _GLUED_PAT_B[3:] + ".example/p?id=" + _AKIA_SAMPLE,
                    "\n" + _PEM_END + "\n z",
                ],
                id="a-removed-link-domain-holding-the-body",
            ),
        ],
    )
    def test_text_a_delta_hid_and_a_glued_match_exposes_withholds_the_segment(self, deltas):
        """A glued neighbour's whole-text match swallows one end of a credential
        and another match the other end, or the tail also appears elsewhere:
        no removed value is left whole, but its body is. The run check finds
        text the streamed redaction hid, and the segment is withheld."""
        content, links = self._flush(deltas)
        assert (content, links) == (chat_runner._SEGMENT_UNCHECKED_TEXT, [])

    def test_a_presigned_link_cut_anywhere_in_two_deltas_saves_no_key(self):
        """Every two-delta cut of a reply with a presigned link: the saved reply
        is either main's per-delta text redacted whole or the withheld notice,
        and never holds the key or a run main's save hid."""
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        host = "my-bucket.s3.us-east-1.amazonaws.com"
        url = (
            f"https://{host}/report.csv?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential="
            f"{self.KEY}%2F20261017%2Fus-east-1%2Fs3%2Faws4_request&X-Amz-Date=20261017T000000Z"
            "&X-Amz-Expires=3600&X-Amz-SignedHeaders=host&X-Amz-Signature=" + "3f9a" * 16
        )
        reply = f"Here is your link:\n\n{url}\n\nIt expires in one hour."
        for cut in range(1, len(reply) - 1, 3):
            deltas = [reply[:cut], reply[cut:]]
            content, _ = self._flush(deltas)
            assert self.KEY[4:] not in content, cut
            if content != chat_runner._SEGMENT_UNCHECKED_TEXT:
                streamed = "".join(chat_runner._redact_delta(d)[0] for d in deltas)
                main = redact_credentials(redact_exfiltration_urls(streamed)[0])[0]
                assert not chat_runner._saved_text_shows_more(content, main), cut

    def test_a_record_whose_placeholder_a_later_match_swallowed_is_dropped(self):
        """A delta blocks a link whose host is a token body; the whole text's
        PEM match then swallows the placeholder. Its record would be the only
        place the body survives, so it is dropped."""
        body = _GLUED_PAT_B[4:]
        deltas = [
            "PRIVATE KEY----- " + _PEM_END + "\n" + _GLUED_SK,
            "\n" + _PEM_BEGIN + "\nhttps://" + body + ".example/p?q=" + "z" * 210 + "\n" + _PEM_END,
        ]
        content, links = self._flush(deltas)
        assert body not in content
        assert not any(body in str(record) for record in links)

    def test_only_mains_own_text_vouches_for_a_saved_run(self):
        tag = chat_runner.REDACTED_CREDENTIAL_TAG
        link_tag = "[REDACTED: suspicious URL to x.test]"
        check = chat_runner._saved_text_shows_more
        assert not check("see it here", "see it here")
        assert not check(f"kept {link_tag} now", f"kept {link_tag}&q=1 now")
        # A blocked address main hid is not vouched for by anything.
        assert check("kept https://x.test/a?q=QQ now", f"kept {link_tag} now")
        # A placeholder domain is text the reader sees: main must show it too.
        assert check("x [REDACTED: suspicious URL to body0123.example] y", f"x {tag} y")
        # A forged opener hides nothing.
        assert check(f"x [REDACTED: hidden0123456789 {tag} end", f"x {tag} end")
        assert not check(f"{tag} x", f"{tag}{tag} x")
        # A password's punctuation keeps it one run: prose pieces cannot vouch.
        assert check(f"{tag}://:p@ss.w0rd!@h", f"p ss w0rd h {tag}{tag}h")
        assert check(f"{tag}_body0123456789 x", f"{tag}{tag} x")

    def test_a_short_piece_of_a_cut_token_value_is_saved(self):
        """A delta cut inside a ``token=`` value makes its first piece a
        credential match of its own (``pa``). That piece is not a credential
        alone, so a record field that happens to contain it (``token_parameter``)
        does not withhold the reply."""
        deltas = ["Open https://app.example.com/join?token=pa", "rtner42xyz to join."]
        content, _ = self._flush(deltas)
        assert content != chat_runner._SEGMENT_UNCHECKED_TEXT
        assert "partner42xyz" not in content and content.endswith(" to join.")

    @pytest.mark.asyncio
    async def test_a_normally_streamed_turn_never_saves_the_withheld_notice(self, tmp_path):
        """A real turn through ``_run_chat``: the slot's raw copy must hash to the
        streamed text, so the flush saves the reply. A new writer to the streamed
        text that skips the raw copy would turn every reply into the notice, and
        this goes red."""
        from kiro_crew.acp.types import EVENT_TEXT_CHUNK

        state, client = _runner_state(tmp_path)
        slot = _slot()
        url = f"https://{self.H}/x?d=" + "eyJhIjoiYiJ9" * 30
        reply = f"Here is the plan.\n\nSee [the doc]({url}) and run `make test`. Done."
        cuts = [0, 7, 30, 70, 200, len(reply)]
        chunks = [reply[a:b] for a, b in zip(cuts, cuts[1:])]
        _set_stream(
            client,
            [LLMEvent(kind=EVENT_TEXT_CHUNK, text=chunk) for chunk in chunks] + [_complete()],
        )

        await _drive(state, slot, "hello")

        saved = [row["content"] for row in slot.messages if row["role"] == "assistant"]
        assert saved and chat_runner._SEGMENT_UNCHECKED_TEXT not in saved
        assert saved[-1].startswith("Here is the plan.") and saved[-1].endswith("Done.")
        assert "eyJhIjoiYiJ9" not in saved[-1]
        assert f"[REDACTED: suspicious URL to {self.H}]" in saved[-1]

    @pytest.mark.asyncio
    async def test_a_blocked_link_extended_into_a_token_form_in_a_later_delta_is_saved_redacted(
        self, tmp_path
    ):
        """A real turn: a blocked link in one delta, then the same link with a
        session token appended in the next. Each delta alone redacts both; the
        saved reply must too, with no piece of the token's tail."""
        from kiro_crew.acp.types import EVENT_TEXT_CHUNK

        state, client = _runner_state(tmp_path)
        slot = _slot()
        short = f"https://{self.H}/v1/ingest?d=" + "eyJhIjoiYiJ9" * 30
        longer = short + "&X-Amz-Security-Token=" + "T" * 200
        chunks = ["See " + short + " and ", longer + " done."]
        _set_stream(
            client,
            [LLMEvent(kind=EVENT_TEXT_CHUNK, text=chunk) for chunk in chunks] + [_complete()],
        )

        await _drive(state, slot, "hello")

        saved = [row["content"] for row in slot.messages if row["role"] == "assistant"]
        placeholder = f"[REDACTED: suspicious URL to {self.H}]"
        assert saved[-1] == f"See {placeholder} and {placeholder} done."

    @pytest.mark.parametrize("with_blob", [False, True])
    def test_benign_replies_cut_anywhere_are_saved(self, with_blob):
        """Code with long identifiers, a plain link and prose, cut at random
        delta boundaries: every flush saves the reply, never the withheld
        notice. Accepted cost: when a cut makes a
        per-delta pass remove a piece of a base64 blob the whole text keeps,
        that segment is withheld rather than saved with a mask, so with a blob
        each flush is the saved reply or the withheld notice."""
        import random

        ident = "eyJsonSerializerConfigurationFactoryBuilder.deserializeFromStringValue()"
        link = "https://docs.example.com/guide/getting-started#install"
        code = "result = some_module.compute_checksum(payload_bytes, algorithm='sha256')"
        blob = "Blob: SGVsbG8gd29ybGQsIHRoaXMgaXMgYSBiZW5pZ24gYmxvYg== " if with_blob else ""
        reply = f"Use `{ident}` then open {link} .\n```python\n{code}\n```\n" f"{blob}and done."
        rng = random.Random(20805)
        for _ in range(200):
            cuts = sorted(rng.sample(range(1, len(reply)), rng.randint(1, 8)))
            bounds = [0, *cuts, len(reply)]
            content, _ = self._flush([reply[a:b] for a, b in zip(bounds, bounds[1:])])
            if with_blob and content == chat_runner._SEGMENT_UNCHECKED_TEXT:
                continue
            assert content != chat_runner._SEGMENT_UNCHECKED_TEXT, cuts
            assert content.endswith("and done."), cuts

    def test_a_redaction_pass_that_raises_fails_closed(self):
        url = "https://h.example-sink.net/only?q=" + "a" * 250
        slot = _slot()
        text = ""
        for delta in ("see ", url[:40], url[40:]):
            safe, removed = chat_runner._redact_delta(delta)
            chat_runner._accumulate_segment_raw(slot, delta, safe, removed)
            text += safe
        with patch.object(
            chat_runner,
            "redact_exfiltration_urls_with_records",
            side_effect=RuntimeError("boom"),
        ):
            result = chat_runner._redact_segment(slot, text)
        assert result == (chat_runner._SEGMENT_UNCHECKED_TEXT, [], [])

    def test_a_folded_address_leaves_credential_ordinals_on_the_saved_text(self):
        """A key-cut address folded into its placeholder takes its credential
        tag with it, so a later token's record points at that token's own tag,
        not at a literal tag after it."""
        host = "my-bucket.s3.us-east-1.amazonaws.com"
        url = f"https://{host}/r?X-Amz-Credential={self.KEY}%2Fx&leak=" + "b" * 64
        k = url.index(self.KEY)
        token = "ghp_" + "A1b2C3d4E5" * 3 + "f6G7h8"
        deltas = [
            "Link: ",
            url[:k],
            url[k:],
            " then ",
            token[:20],
            token[20:],
            " and [REDACTED: credential] end",
        ]
        slot = _slot()
        text = ""
        for delta in deltas:
            safe, removed = chat_runner._redact_delta(delta)
            chat_runner._accumulate_segment_raw(slot, delta, safe, removed)
            text += safe
        content, _, redactions = chat_runner._redact_segment(slot, text)
        assert content == (
            f"Link: [REDACTED: suspicious URL to {host}] then [REDACTED: credential]"
            " and [REDACTED: credential] end"
        )
        assert [r["ordinal"] for r in redactions] == [0]

    def test_long_query_before_a_delta_cut_key_saves_as_one_placeholder(self):
        """A query over the length rule streams before the key's own delta: the
        saved pass blocks the address up to the key's tag, and the rest of the
        tag and the address after it are still removed."""
        host = "my-bucket.s3.us-east-1.amazonaws.com"
        url = (
            f"https://{host}/r.csv?X-Amz-Security-Token="
            + "T" * 300
            + f"&X-Amz-Credential={self.KEY}%2F20261008%2Fus-east-1%2Fs3%2Faws4_request"
            + "&X-Amz-Signature="
            + "b" * 64
        )
        q, k = url.index("?"), url.index(self.KEY)
        content, _ = self._flush(["Link: ", url[:q], url[q:k], url[k:], " . done"])
        assert content == f"Link: [REDACTED: suspicious URL to {host}] . done"

    def test_glued_pair_a_credential_tag_and_a_key_split_link_list_every_record(self):
        # Two same-host long-query addresses glued across a delta, a
        # credential tag, then a same-host address whose key a delta redacted.
        g1 = f"https://{self.H}/g1?q=" + "a" * 250
        g2 = f"https://{self.H}/g2?q=" + "b" * 250
        deltas = [
            "Two links: ",
            g1,
            g2,
            " [REDACTED: credential] then ",
            f"https://{self.H}/other?k=",
            self.KEY + "&t=x",
            " end.",
        ]
        content, links = self._flush(deltas)
        assert content != chat_runner._SEGMENT_UNCHECKED_TEXT
        # The glued pair saves as one address that no single delta blocked, so
        # its record (which would quote it) is dropped; the placeholder stays.
        assert content.count(f"[REDACTED: suspicious URL to {self.H}]") >= 1
        assert links and all(r["path"] for r in links if r["domain"] == self.H)

    @staticmethod
    def _key_split_presigned_deltas() -> list[str]:
        """Presigned S3 addresses split at ``&X-Amz-Date`` beside same-host
        addresses whose key arrives in its own delta. A build that saved each
        delta's redaction kept a placeholder with the query tail after it (the
        rows ``website/src/test/fixtures/credInUrl.json`` and
        ``blockedLinkOneOne.json`` still render)."""
        host = "my-bucket.s3.us-east-1.amazonaws.com"
        key = TestBlockedLinkCardRecords.KEY
        deltas: list[str] = []
        for i, name in enumerate(("report.csv", "invoice.pdf")):
            url = (
                f"https://{host}/{name}?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential={key}"
                "%2F20261008%2Fus-east-1%2Fs3%2Faws4_request&X-Amz-Date=20261008T000000Z"
                "&X-Amz-Expires=3600&X-Amz-SignedHeaders=host&X-Amz-Signature=" + "a" * 64
            )
            cut = url.index("&X-Amz-Date")
            deltas += [f"Link {i + 1}: ", url[:cut], url[cut:], " . "]
        for name in ("other-a", "other-b"):
            deltas += [" plain ", f"https://{host}/{name}?X-Amz-Credential=", key + "%2Fx", " . "]
        return deltas

    def test_key_split_links_are_withheld_not_saved_unredacted(self):
        """A delta blocked each address's head, but the whole text keeps the
        address (its key tagged): main's save never showed that address, so
        under default refuse the segment is withheld, and no key is saved."""
        content, links = self._flush(self._key_split_presigned_deltas())
        assert (content, links) == (chat_runner._SEGMENT_UNCHECKED_TEXT, [])

    def test_clearing_the_streamed_text_clears_its_raw_copy(self):
        """``_cleared_segment_text`` is how a turn drops its streamed text
        mid-segment, and it takes the raw copy with it."""
        slot = _slot()
        raw = "unseen https://h.example-sink.net/x"
        chat_runner._accumulate_segment_raw(slot, raw, raw, ())
        assert slot.segment_raw_text

        assert chat_runner._cleared_segment_text(slot) == ""
        assert slot.segment_raw_text == ""
