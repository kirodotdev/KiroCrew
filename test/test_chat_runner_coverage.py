"""Coverage tests for ``kiro_crew.dashboard.chat_runner``.

The module's happy paths are well covered by the existing chat suites
(``test_dashboard_approval``, ``test_dashboard_chat``, ``test_eager_spawn``,
``test_goal_command`` …). What this module targets instead is the set of
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
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew import name_grant
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    STOP_REASON_COMPACTION_FAILED,
    STOP_REASON_STALE_RECOVER,
    STOP_REASON_TOOL_STALL,
)
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.metrics import turns as turns_mod
from kiro_crew.providers.base import LLMEvent
from kiro_crew.security import oauth_url_contains_credential
from kiro_crew.trust_patterns import canonical_non_shell_trust_key, exact_trust_pattern

# ── Shared helpers ────────────────────────────────────────────────────────


def _slot(key: str = "chat-cov-1") -> _ChatSlot:
    slot = _ChatSlot(key)
    # Titled on purpose: an untitled slot makes the end-of-turn cycle spawn
    # _maybe_auto_title, which is a real LLM path. maybe_refresh_title (the
    # titled branch) self-guards and returns without a call.
    slot._titled = True
    return slot


def _state(tmp_path, **kwargs) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_slack_link = MagicMock(return_value=(None, None))
    sessions.set_slack_link = MagicMock()
    sessions.get_mirror_link = MagicMock(return_value=None)
    sessions.reset = AsyncMock()
    # Returns whether it tore down; False means skip_if_busy refused.
    sessions.discard_conversation = AsyncMock(return_value=True)
    # Production returns None when no session is live for the key. Left as a bare
    # MagicMock it would answer a truthy provider whose has_active_turn() is also
    # truthy, so every busy-probe would read "turn in flight" on an idle state.
    sessions.get_provider = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    sessions.record_failure = AsyncMock()
    sessions.remove_if_unclaimed = AsyncMock(return_value=False)
    sessions.check_context_usage = MagicMock()
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
        **kwargs,
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.push_refresh = MagicMock()
    state.refresh_slot_source_status = MagicMock()
    state.broadcast_context_usage = MagicMock()
    return state


async def _async_iter(items):
    for item in items:
        yield item


def _complete(stop_reason: str = "end_turn", **kwargs) -> LLMEvent:
    return LLMEvent(kind=EVENT_COMPLETE, stop_reason=stop_reason, **kwargs)


def _permission(
    title: str = "Running: ls -la",
    tool_input: str = "",
    tool_kind: str = "execute",
    request_id: str = "req-cov-1",
    *,
    is_shell: bool = True,
    tool_name: str = "",
    mcp_server_name: str = "",
    raw_tool_params: dict | None = None,
) -> LLMEvent:
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title=title,
        tool_kind=tool_kind,
        tool_input=tool_input,
        request_id=request_id,
        is_shell=is_shell,
        tool_name=tool_name,
        mcp_server_name=mcp_server_name,
        raw_tool_params=raw_tool_params,
    )


def _runner_state(tmp_path, *, hook_store=None, context_builder=None):
    """Return ``(state, client)`` wired for a scripted ``_run_chat`` turn."""
    state = _state(tmp_path)
    client = AsyncMock()
    # The provider's sync accessors must NOT be AsyncMock: _run_chat calls them
    # inline and would otherwise store un-awaited coroutines in the WS payload.
    client.context_usage_pct = MagicMock(return_value=0.0)
    client.context_window_tokens = MagicMock(return_value=0)
    client.context_used_tokens = MagicMock(return_value=0)
    client.last_prompt_stats = None
    client._client = client
    client.exit_code = None
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    state._hook_store = hook_store or MagicMock(fire=AsyncMock(return_value=[]))
    if context_builder is not None:
        state.context_builder = context_builder
    return state, client


def _set_stream(client, events) -> None:
    """Script ``client.stream`` so only the FIRST turn yields *events*.

    A turn that produced no visible assistant text arms the empty-response
    auto-continue, which re-queues the prompt and runs a second turn. With a
    stream that replays unconditionally, that second turn re-processes the same
    permission event and every ``assert_awaited_once`` on the provider counts
    two calls. Later turns therefore complete immediately.
    """
    calls = {"n": 0}

    def _stream(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _async_iter(events)
        return _async_iter([_complete()])

    client.stream = MagicMock(side_effect=_stream)


@contextmanager
def _quiet_sel():
    """Stub the SEL audit sink so a turn does not write an audit trail."""
    with patch.object(chat_runner, "sel") as mock_sel:
        mock_sel.return_value = MagicMock()
        yield mock_sel


async def _drive(state, slot, message: str = "hello") -> None:
    """Run exactly one turn and leave no task behind.

    ``_empty_response_retries`` is pre-spent on purpose. A turn that streams no
    assistant text re-queues itself, and the finally block then dispatches a
    SECOND turn through ``spawn_guarded_turn`` — which doubles every
    ``assert_awaited_once`` on the provider and leaves a task running past the
    test's event loop. Starting at the exhausted count sends the empty response
    to its terminal notice branch instead, so one call is one turn.
    """
    slot._empty_response_retries = 2
    with _quiet_sel():
        await chat_runner._run_chat(state, slot, message)
    await _settle(slot)


async def _settle(slot) -> None:
    """Await (or cancel) any follow-up turn the finally block dispatched."""
    task = slot.task
    if task is None or not hasattr(task, "cancel"):
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # pragma: no cover — draining, never the assertion
        pass


def _errors(slot) -> list[str]:
    return [m.get("content", "") for m in slot.messages if m.get("role") == "error"]


# ── drain_pending_context ─────────────────────────────────────────────────


class TestDrainPendingContext:
    def test_expired_entry_is_discarded(self):
        """An entry past its ``maxAge`` is dropped, not injected."""
        slot = _slot()
        slot._pending_context = [
            {"content": "stale", "source": "app", "maxAge": 1, "injectedAt": 0},
            {"content": "fresh", "source": "panel"},
        ]

        out = chat_runner.drain_pending_context(slot)

        assert "stale" not in out
        assert "fresh" in out
        assert 'from "panel"' in out
        assert slot._pending_context == []

    def test_all_expired_yields_empty_string(self):
        slot = _slot()
        slot._pending_context = [
            {"content": "stale", "maxAge": 1, "injectedAt": 0},
        ]

        assert chat_runner.drain_pending_context(slot) == ""

    def test_frame_carries_silent_consumption_contract(self):
        """Every drained block instructs the agent to consume it silently.

        Regression for #4780: the feature-request seed (and any other
        pending-context producer) was framed with a bare source label and no
        consumption contract, so on a fresh session the agent recited the
        injected workflow verbatim as its visible reply. The contract line
        must sit INSIDE the frame (between the opening delimiter and the
        content) so it binds per-block, for every producer, and must both
        forbid echoing and redirect the reply to the user's visible message.
        """
        slot = _slot()
        slot._pending_context = [
            {"content": "WORKFLOW: greet the user", "source": "feature-request"},
        ]

        out = chat_runner.drain_pending_context(slot)

        opening = out.index('[Background context from "feature-request"]')
        contract = out.index(chat_runner._CONTEXT_FRAME_CONTRACT)
        content = out.index("WORKFLOW: greet the user")
        closing = out.index("[End of background context]")
        assert opening < contract < content < closing
        # The two load-bearing clauses, pinned as text so a rewording that
        # drops either fails here rather than in production transcripts.
        assert "never quote, echo" in chat_runner._CONTEXT_FRAME_CONTRACT
        assert "user's visible message" in chat_runner._CONTEXT_FRAME_CONTRACT

    def test_every_block_gets_its_own_contract_line(self):
        """Multi-entry drains repeat the contract per frame — a single leading
        notice would detach from later blocks when a consumer reorders or
        truncates, so the contract is part of each frame, not a preamble."""
        slot = _slot()
        slot._pending_context = [
            {"content": "first", "source": "a"},
            {"content": "second", "source": "b"},
        ]

        out = chat_runner.drain_pending_context(slot)

        assert out.count(chat_runner._CONTEXT_FRAME_CONTRACT) == 2

    def test_empty_source_attributes_to_app(self):
        """api_chat_slot_context always writes ``source`` — as "" when the
        caller omitted it — so a dict-default alone never fires and the header
        would read [Background context from ""]: an unattributed block under
        the frame's "not authored by the user" claim."""
        slot = _slot()
        slot._pending_context = [{"content": "x", "source": ""}]

        out = chat_runner.drain_pending_context(slot)

        assert '[Background context from "app"]' in out
        assert 'from ""' not in out


# ── turn metric ───────────────────────────────────────────────────────────


class TestTurnMetric:
    @pytest.mark.parametrize(
        "stop_reason,expected",
        [
            (None, "ok"),
            ("", "ok"),
            ("end_turn", "ok"),
            ("completed", "ok"),
            ("error: timeout waiting", "timeout"),
            ("error: pipe died", "error"),
        ],
    )
    def test_outcome_mapping(self, stop_reason, expected):
        assert chat_runner._turn_outcome(stop_reason) == expected

    def test_session_source_attribute_is_attached(self):
        recorder = MagicMock()
        # The emit and its source derivation moved to ``metrics/turns.py`` so
        # every dispatch surface could reach them (they used to sit in
        # chat_runner, which only the dashboard turn loop runs). The source now
        # comes from ``telemetry_channel_of``, which — unlike infer_use_case —
        # knows the background surfaces this metric was widened to cover.
        with (
            patch.object(turns_mod, "telemetry_channel_of", return_value="cron"),
            patch.object(turns_mod, "get_recorder", return_value=recorder),
        ):
            chat_runner._emit_turn_metric(0, "end_turn", "cron:job", elapsed_ms=12)

        _, kwargs = recorder.histogram.call_args
        assert kwargs["attrs"]["session_source"] == "cron"
        assert kwargs["attrs"]["outcome"] == "ok"

    def test_use_case_failure_does_not_block_the_emit(self):
        """A broken source lookup must still leave the histogram emitted."""
        recorder = MagicMock()
        with (
            patch.object(turns_mod, "telemetry_channel_of", side_effect=RuntimeError("boom")),
            patch.object(turns_mod, "get_recorder", return_value=recorder),
        ):
            chat_runner._emit_turn_metric(50, "end_turn", "dashboard:x")

        recorder.histogram.assert_called_once()
        _, kwargs = recorder.histogram.call_args
        assert "session_source" not in kwargs["attrs"]

    def test_recorder_failure_is_swallowed(self):
        with patch.object(chat_runner, "get_recorder", side_effect=RuntimeError("no recorder")):
            chat_runner._emit_turn_metric(50, "end_turn", "dashboard:x")

    def test_zero_duration_skips_the_emit(self):
        recorder = MagicMock()
        with patch.object(chat_runner, "get_recorder", return_value=recorder):
            chat_runner._emit_turn_metric(0, "end_turn", "dashboard:x", elapsed_ms=0)

        recorder.histogram.assert_not_called()


# ── PreToolUse hook verdicts ──────────────────────────────────────────────


class TestPreToolHookVerdicts:
    @pytest.mark.parametrize(
        "results",
        [None, "BLOCKED:h:no", {"blocked": True}, 7],
    )
    def test_non_list_output_is_denied(self, results):
        """Deny-by-default: anything but a list of strings blocks the tool."""
        assert chat_runner._pre_tool_hooks_should_block(results) is True

    def test_empty_list_is_the_pass_through_contract(self):
        assert chat_runner._pre_tool_hooks_should_block([]) is False

    def test_non_string_member_blocks(self):
        assert chat_runner._pre_tool_hooks_should_block(["ok", 3]) is True

    def test_block_reason_prefers_the_hook_text(self):
        assert (
            chat_runner._pre_tool_block_reason(["BLOCKED:policy: not allowed here "])
            == "not allowed here"
        )

    @pytest.mark.parametrize(
        "results",
        [
            ["BLOCKED:policy:"],  # marker present, reason empty
            ["BLOCKED:policy"],  # marker truncated, no reason field
            [],  # nothing blocked
            "not-a-list",
        ],
    )
    def test_block_reason_falls_back_when_no_reason_is_authored(self, results):
        assert chat_runner._pre_tool_block_reason(results) == "blocked by a PreToolUse policy hook"


# ── snapshot helpers ──────────────────────────────────────────────────────


class TestSnapshotHelpers:
    def test_safe_read_snapshot_declines_on_validator_error(self):
        with patch.object(chat_runner, "validate_file_path", side_effect=OSError("boom")):
            assert chat_runner._safe_read_snapshot("/tmp/whatever") is None

    def test_safe_read_snapshot_declines_a_directory(self, tmp_path):
        assert chat_runner._safe_read_snapshot(str(tmp_path)) is None

    def test_safe_read_snapshot_reads_a_regular_file(self, tmp_path):
        target = tmp_path / "note.txt"
        target.write_text("hello\n", newline="\n")

        assert chat_runner._safe_read_snapshot(str(target)) == "hello\n"

    def test_truncate_snapshot_marks_the_cut(self):
        out = chat_runner._truncate_snapshot("x" * (chat_runner._MAX_SNAPSHOT + 10))

        assert out.endswith(f"... (truncated at {chat_runner._MAX_SNAPSHOT} chars)")

    def test_reconstruct_declines_when_neither_state_is_plausible(self, tmp_path):
        """Ambiguous disk content must decline rather than fabricate a before."""
        target = tmp_path / "amb.txt"
        # oldStr twice (pre-write implausible) and the single-newStr reversal
        # candidate is tool-inconsistent, so neither hypothesis survives.
        target.write_text("cabab", newline="\n")

        out = chat_runner._reconstruct_str_replace_before(
            str(target), {"oldStr": "ab", "newStr": "c"}
        )

        assert out is None

    def test_reconstruct_declines_on_replace_all(self, tmp_path):
        target = tmp_path / "all.txt"
        target.write_text("aaa", newline="\n")

        assert (
            chat_runner._reconstruct_str_replace_before(
                str(target), {"oldStr": "a", "newStr": "b", "replaceAll": True}
            )
            is None
        )

    def test_reconstruct_declines_on_missing_params(self, tmp_path):
        target = tmp_path / "missing.txt"
        target.write_text("body", newline="\n")

        assert chat_runner._reconstruct_str_replace_before(str(target), {"oldStr": "x"}) is None

    def test_snapshot_write_target_ignores_non_write_commands(self):
        assert chat_runner._snapshot_write_target({"command": "read", "path": "/tmp/x"}) is None
        assert chat_runner._snapshot_write_target(None) is None

    def test_snapshot_write_target_prefers_the_diff_block(self, tmp_path):
        target = tmp_path / "create-me.txt"

        got = chat_runner._snapshot_write_target(
            {"command": "create", "path": str(target)}, diff_old_text=""
        )

        assert got is not None
        assert os.path.realpath(got["path"]) == os.path.realpath(str(target))
        assert got["content"] == ""

    def test_snapshot_write_target_records_empty_before_for_a_new_file(self, tmp_path):
        target = tmp_path / "not-yet.txt"

        got = chat_runner._snapshot_write_target({"command": "create", "path": str(target)})

        assert got == {"path": str(target), "content": ""}


class TestFlushFileChanges:
    def test_credentials_are_scrubbed_from_before_and_after(self, tmp_path):
        """A secret in a non-sensitive config must not reach message meta."""
        target = tmp_path / "config.ini"
        target.write_text("key=AKIAIOSFODNN7EXAMPLE\nafter\n", newline="\n")
        slot = _slot()
        slot.append("assistant", "done", "msg msg-a", broadcast=False)
        slot._file_changes = [
            {"path": str(target), "content": "key=AKIAIOSFODNN7EXAMPLE\nbefore\n"}
        ]

        chat_runner._flush_file_changes(slot)

        changes = slot.messages[-1]["meta"]["file_changes"]
        assert len(changes) == 1
        assert "AKIAIOSFODNN7EXAMPLE" not in changes[0]["before"]
        assert "AKIAIOSFODNN7EXAMPLE" not in changes[0]["after"]
        assert slot._dirty is True
        assert slot._file_changes == []

    def test_non_list_changes_are_ignored(self):
        """A MagicMock slot attribute is truthy — the isinstance gate matters."""
        slot = _slot()
        slot._file_changes = MagicMock()

        chat_runner._flush_file_changes(slot)

        assert slot.messages == []

    def test_synthetic_message_is_created_when_no_assistant_row_exists(self, tmp_path):
        target = tmp_path / "orphan.txt"
        target.write_text("after\n", newline="\n")
        slot = _slot()
        slot._file_changes = [{"path": str(target), "content": "before\n"}]

        chat_runner._flush_file_changes(slot)

        assert slot.messages[-1]["role"] == "assistant"
        assert slot.messages[-1]["meta"]["file_changes"][0]["after"] == "after\n"


class TestAttachTurnStats:
    def test_zero_elapsed_attaches_nothing(self):
        slot = _slot()
        slot.append("assistant", "hi", "msg msg-a", broadcast=False)

        chat_runner._attach_turn_stats(slot, 0, 0.0, 0.0)

        assert "turn_stats" not in (slot.messages[-1].get("meta") or {})

    def test_credits_and_cost_are_rounded_and_omitted_when_zero(self):
        slot = _slot()
        slot.append("assistant", "hi", "msg msg-a", broadcast=False)

        chat_runner._attach_turn_stats(slot, 1200, 0.123456, 0.0)

        stats = slot.messages[-1]["meta"]["turn_stats"]
        assert stats == {"elapsed_ms": 1200, "credits": 0.1235}

    def test_boundary_protects_a_previous_turns_message(self):
        """A turn that appended no assistant row must not overwrite the last one."""
        slot = _slot()
        slot.append("assistant", "prior turn", "msg msg-a", broadcast=False)
        boundary = len(slot.messages)

        chat_runner._attach_turn_stats(slot, 999, 0.0, 0.0, turn_boundary=boundary)

        assert "turn_stats" not in (slot.messages[-1].get("meta") or {})


# ── ACP string redaction / OAuth URL gate ─────────────────────────────────


class TestAcpRedaction:
    def test_empty_string_passes_through(self):
        assert chat_runner._redact_acp_string("") == ""

    def test_credential_is_scrubbed(self):
        out = chat_runner._redact_acp_string("token AKIAIOSFODNN7EXAMPLE")

        assert "AKIAIOSFODNN7EXAMPLE" not in out


class TestOauthUrlCredentialGate:
    def test_empty_url_is_not_a_credential(self):
        assert oauth_url_contains_credential("") is False

    def test_plain_consent_url_is_allowed(self):
        assert (
            oauth_url_contains_credential(
                "https://github.com/login/oauth/authorize?client_id=abc&state=xyz"
            )
            is False
        )

    def test_unparseable_url_is_refused(self):
        # An invalid IPv6 authority makes urlparse raise ValueError inside the
        # shared security gate, which fails closed.
        assert oauth_url_contains_credential("https://[bad-ipv6/x") is True

    def test_credential_signature_inside_an_oauth_param_is_refused(self):
        url = "https://example.test/authorize?state=AKIAIOSFODNN7EXAMPLE1"

        assert oauth_url_contains_credential(url) is True

    def test_exfiltration_pattern_in_a_non_oauth_param_is_refused(self):
        url = "https://example.test/authorize?payload=" + ("A" * 260)

        assert oauth_url_contains_credential(url) is True


class TestEmitMcpOauthRequest:
    def test_unsafe_scheme_renders_a_rejected_banner(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        chat_runner._emit_mcp_oauth_request(state, slot, "svc", "file:///etc/passwd")

        banner = slot.messages[-1]
        assert banner["role"] == "mcp_oauth"
        assert banner["meta"]["rejected_url"] is True
        assert banner["meta"]["error"] == "unsafe URL scheme"

    def test_credential_bearing_url_renders_a_rejected_banner(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        chat_runner._emit_mcp_oauth_request(
            state, slot, "svc", "https://example.test/a?state=AKIAIOSFODNN7EXAMPLE1"
        )

        assert slot.messages[-1]["meta"]["rejected_url"] is True

    def test_card_owned_annotation_rides_on_the_authorize_banner(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        url = "https://github.test/authorize?client_id=x"

        chat_runner._emit_mcp_oauth_request(state, slot, "github", url, card_owned=True)

        assert slot.messages[-1]["meta"]["card_owned"] is True
        # Assert the whole URL, not a prefix. A startswith() check would also
        # accept https://github.test.example.com/... -- a different host that
        # merely begins with the expected string -- so it is both a weaker
        # assertion and the pattern CodeQL flags as incomplete URL substring
        # sanitization.
        assert slot.messages[-1]["meta"]["oauth_url"] == url


class TestConnectionsManagedNames:
    def test_failure_fails_open_to_the_empty_set(self):
        with patch.object(chat_runner, "kirocrew_managed_names", side_effect=RuntimeError("io")):
            assert chat_runner._connections_managed_mcp_names() == frozenset()

    def test_intersection_of_managed_and_carded(self):
        with (
            patch.object(
                chat_runner, "kirocrew_managed_names", return_value={"github", "handmade"}
            ),
            patch.object(
                chat_runner,
                "get_visible_providers",
                return_value=[{"slug": "github"}, {"slug": "slack"}],
            ),
        ):
            assert chat_runner._connections_managed_mcp_names() == frozenset({"github"})


class TestDrainSessionInitOauthRequests:
    @pytest.mark.asyncio
    async def test_provider_without_the_accessor_is_a_noop(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        client = MagicMock()
        client.client = object()  # no pop_pending_oauth_requests

        await chat_runner._drain_session_init_oauth_requests(state, slot, client)

        assert slot.messages == []

    @pytest.mark.asyncio
    async def test_empty_pending_list_skips_ownership_resolution(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        client = MagicMock()
        client.client.pop_pending_oauth_requests = MagicMock(return_value=[])

        with patch.object(chat_runner, "_connections_managed_mcp_names") as managed:
            await chat_runner._drain_session_init_oauth_requests(state, slot, client)

        managed.assert_not_called()
        assert slot.messages == []

    @pytest.mark.asyncio
    async def test_non_dict_entries_are_skipped_and_the_rest_emitted(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        client = MagicMock()
        client.client.pop_pending_oauth_requests = MagicMock(
            return_value=[
                "not-a-dict",
                {"serverName": "github", "oauthUrl": "https://github.test/authorize?client_id=x"},
            ]
        )

        with patch.object(
            chat_runner, "_connections_managed_mcp_names", return_value=frozenset({"github"})
        ):
            await chat_runner._drain_session_init_oauth_requests(state, slot, client)

        banners = [m for m in slot.messages if m.get("role") == "mcp_oauth"]
        assert len(banners) == 1
        assert banners[0]["meta"]["card_owned"] is True


class TestMarkMcpOauthCompleted:
    def _open_banner(self, state, slot, name: str = "github") -> None:
        chat_runner._emit_mcp_oauth_request(
            state, slot, name, "https://x.test/authorize?client_id=1"
        )

    def test_no_matching_banner_is_a_noop(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        self._open_banner(state, slot, "github")
        before = len(slot.messages)

        chat_runner._mark_mcp_oauth_completed(state, slot, "other", True)

        assert len(slot.messages) == before
        assert "completed" not in slot.messages[-1]["meta"]

    def test_already_terminal_banner_is_not_patched_again(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        self._open_banner(state, slot)
        chat_runner._mark_mcp_oauth_completed(state, slot, "github", True)
        content_after_first = slot.messages[-1]["content"]

        chat_runner._mark_mcp_oauth_completed(state, slot, "github", False, "second try")

        assert slot.messages[-1]["content"] == content_after_first
        assert slot.messages[-1]["meta"]["completed"] is True

    def test_failure_records_the_redacted_error(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        self._open_banner(state, slot)

        chat_runner._mark_mcp_oauth_completed(
            state, slot, "github", False, "denied for AKIAIOSFODNN7EXAMPLE"
        )

        meta = slot.messages[-1]["meta"]
        assert meta["failed"] is True
        assert "AKIAIOSFODNN7EXAMPLE" not in meta["error"]
        assert "authentication failed" in slot.messages[-1]["content"]

    def test_untimestamped_banner_cannot_be_updated(self, tmp_path):
        """A history line with no ``ts`` has no update handle — bail, don't broadcast."""
        state, slot = _state(tmp_path), _slot()
        slot.messages.append(
            {"role": "mcp_oauth", "content": "legacy", "meta": {"server_name": "github"}}
        )

        chat_runner._mark_mcp_oauth_completed(state, slot, "github", True)

        assert not any(
            call.args and call.args[0] == "chat_message_update"
            for call in state.broadcast_ws.call_args_list
        )


# ── trust / auto-approve predicates ───────────────────────────────────────


class TestTrustPredicates:
    def test_scope_grant_is_rechecked_on_every_call(self):
        slot = _slot()
        slot._trust_scope = "scope-1"

        with patch.object(chat_runner, "safety_override") as override:
            override.return_value.is_scope_active.return_value = True
            assert chat_runner._slot_is_trusted(slot) is True
            override.return_value.is_scope_active.return_value = False
            assert chat_runner._slot_is_trusted(slot) is False

    def test_no_grant_at_all_is_untrusted(self):
        assert chat_runner._slot_is_trusted(_slot()) is False

    @pytest.mark.parametrize(
        "trust,scope,yolo,expected",
        [
            (False, "", True, "yolo"),
            (True, "", False, "trust"),
            (False, "s-1", False, "trust_scope"),
            (False, "", False, "trust"),
        ],
    )
    def test_auto_approve_reason_precedence(self, trust, scope, yolo, expected):
        slot = _slot()
        slot._trust = trust
        slot._trust_scope = scope

        assert chat_runner._auto_approve_reason(slot, yolo) == expected

    def test_scoped_grant_is_never_persisted_as_a_session_policy(self):
        """A lapsing grant must not be cached where nothing re-checks it."""
        slot = _slot()
        slot._trust_scope = "scope-1"

        assert chat_runner._persistable_session_policy(slot, False) == ""

    @pytest.mark.parametrize("yolo,trust", [(True, False), (False, True)])
    def test_non_lapsing_grants_are_persistable(self, yolo, trust):
        slot = _slot()
        slot._trust = trust

        assert chat_runner._persistable_session_policy(slot, yolo) == "auto"

    def test_native_crew_auto_approve_requires_an_active_crew(self, tmp_path):
        state = _state(tmp_path)
        state.is_yolo_active = MagicMock(return_value=True)
        slot = _slot()
        slot._trust = True

        assert chat_runner._native_crew_should_auto_approve({}, state, slot) is False
        assert (
            chat_runner._native_crew_should_auto_approve({"s1": {"done": True}}, state, slot)
            is False
        )
        assert (
            chat_runner._native_crew_should_auto_approve({"s1": {"done": False}}, state, slot)
            is True
        )

    def test_active_crew_without_any_grant_is_still_denied(self, tmp_path):
        state = _state(tmp_path)
        state.is_yolo_active = MagicMock(return_value=False)
        state.context_builder = None

        assert (
            chat_runner._native_crew_should_auto_approve({"s1": {"done": False}}, state, _slot())
            is False
        )


# ── channel mirror ladder ─────────────────────────────────────────────────


class TestChannelTargetLadder:
    def test_missing_link_resolves_to_nothing(self, tmp_path):
        assert chat_runner._resolve_channel_target(_state(tmp_path), "dashboard:x", None) is None

    def test_slack_is_skipped(self, tmp_path):
        link = MagicMock(channel_type=chat_runner.SLACK_NAMESPACE, channel_id="C1")

        assert chat_runner._resolve_channel_target(_state(tmp_path), "dashboard:x", link) is None

    def test_governance_denial_skips_the_mirror(self, tmp_path):
        link = MagicMock(channel_type="telegram", channel_id="123", thread_id=None)
        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            return_value=MagicMock(permitted=False),
        ):
            assert (
                chat_runner._resolve_channel_target(_state(tmp_path), "dashboard:x", link) is None
            )

    def test_unregistered_transport_skips_the_mirror(self, tmp_path):
        state = _state(tmp_path)
        state.get_channel_transport = MagicMock(return_value=None)
        link = MagicMock(channel_type="telegram", channel_id="123", thread_id=None)

        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            return_value=MagicMock(permitted=True),
        ):
            assert chat_runner._resolve_channel_target(state, "dashboard:x", link) is None

    def test_transport_without_proactive_send_skips_the_mirror(self, tmp_path):
        state = _state(tmp_path)
        transport = MagicMock()
        transport.capabilities.supports_proactive_send = False
        state.get_channel_transport = MagicMock(return_value=transport)
        link = MagicMock(channel_type="wecom", channel_id="123", thread_id=None)

        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            return_value=MagicMock(permitted=True),
        ):
            assert chat_runner._resolve_channel_target(state, "dashboard:x", link) is None

    def test_capable_transport_is_returned(self, tmp_path):
        state = _state(tmp_path)
        transport = MagicMock()
        transport.capabilities.supports_proactive_send = True
        state.get_channel_transport = MagicMock(return_value=transport)
        link = MagicMock(channel_type="telegram", channel_id="123", thread_id=None)

        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            return_value=MagicMock(permitted=True),
        ):
            got = chat_runner._resolve_channel_target(state, "dashboard:x", link)

        assert got == (link, transport)


class TestMarkKiroSignedOut:
    def test_absent_service_is_a_noop(self):
        state = MagicMock(spec=[])

        chat_runner._mark_kiro_signed_out(state)

    def test_latch_failure_never_raises(self):
        state = MagicMock()
        state.kiro_prerequisite_service.mark_signed_out.side_effect = RuntimeError("io")

        chat_runner._mark_kiro_signed_out(state)


class TestDeliverAuthErrorToSlack:
    @pytest.mark.asyncio
    async def test_no_slack_client_is_a_noop(self, tmp_path):
        state = _state(tmp_path)
        state.slack_client = None

        await chat_runner._deliver_auth_error_to_slack(
            state, _slot(), state.sessions, "dashboard:x", "signed out"
        )

    @pytest.mark.asyncio
    async def test_unlinked_session_is_a_noop(self, tmp_path):
        state = _state(tmp_path)
        state.slack_client = AsyncMock()

        await chat_runner._deliver_auth_error_to_slack(
            state, _slot(), state.sessions, "dashboard:x", "signed out"
        )

        state.slack_client.post_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_link_from_the_session_store_is_used(self, tmp_path):
        state = _state(tmp_path)
        state.slack_client = AsyncMock()
        state.sessions.get_slack_link = MagicMock(return_value=("111.222", "C123"))

        await chat_runner._deliver_auth_error_to_slack(
            state, _slot(), state.sessions, "dashboard:x", "signed out"
        )

        state.slack_client.post_message.assert_awaited_once_with("C123", "signed out", "111.222")

    @pytest.mark.asyncio
    async def test_post_failure_is_swallowed(self, tmp_path):
        state = _state(tmp_path)
        state.slack_client = AsyncMock()
        state.slack_client.post_message.side_effect = RuntimeError("slack down")
        slot = _slot()
        slot._slack_thread_ts = "1.2"
        slot._slack_channel = "C1"

        await chat_runner._deliver_auth_error_to_slack(
            state, slot, state.sessions, "dashboard:x", "signed out"
        )


class TestCrossSurfaceReply:
    @pytest.mark.asyncio
    async def test_empty_text_is_not_mirrored(self, tmp_path):
        state = _state(tmp_path)

        with patch.object(chat_runner, "_resolve_mirror_target") as resolve:
            await chat_runner._deliver_cross_surface_reply(state, "dashboard:x", "")

        resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_reply_is_chunked_per_transport_limit(self, tmp_path):
        state = _state(tmp_path)
        transport = AsyncMock()
        transport.capabilities.max_message_chars = 10
        # Explicit 0: a MagicMock attribute is a child object, not a number,
        # so chunk_for_transport cannot compare it. 0 = not byte-capped, which
        # is the character path this test is about.
        transport.capabilities.max_message_bytes = 0
        link = MagicMock(channel_id="123", thread_id=None, channel_type="telegram")

        with patch.object(chat_runner, "_resolve_mirror_target", return_value=(link, transport)):
            await chat_runner._deliver_cross_surface_reply(state, "dashboard:x", "ab " * 20)

        assert transport.send_message.await_count > 1

    @pytest.mark.asyncio
    async def test_transport_failure_never_disrupts_the_turn(self, tmp_path):
        state = _state(tmp_path)
        transport = AsyncMock()
        transport.capabilities.max_message_chars = 4096
        transport.capabilities.max_message_bytes = 0
        transport.send_message.side_effect = RuntimeError("offline")
        link = MagicMock(channel_id="123", thread_id=None, channel_type="telegram")

        with patch.object(chat_runner, "_resolve_mirror_target", return_value=(link, transport)):
            await chat_runner._deliver_cross_surface_reply(state, "dashboard:x", "hello")


class TestPrepareMirrorMsg:
    def test_truncates_then_redacts(self):
        out = chat_runner._prepare_mirror_msg("x" * 900)

        assert len(out) <= 500

    def test_none_becomes_empty(self):
        assert chat_runner._prepare_mirror_msg("") == ""


# ── segment flush / widget registration ───────────────────────────────────


class TestFlushSegment:
    def test_pending_variants_are_attached_and_broadcast(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_variants = [{"content": "older draft", "ts": "1"}, "not-a-dict"]

        chat_runner._flush_segment(state, slot, "newest draft")

        last = slot.messages[-1]
        assert last["variant_idx"] == 1
        assert [v["content"] for v in last["variants"]] == ["older draft", "newest draft"]
        assert slot._pending_variants == []
        assert any(
            call.args[0] == "chat_variant_switch" for call in state.broadcast_ws.call_args_list
        )

    def test_credentials_in_the_segment_are_redacted(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        chat_runner._flush_segment(state, slot, "secret AKIAIOSFODNN7EXAMPLE here")

        # Target the assistant row by role, not `messages[-1]`: a redacted
        # segment now also appends a notice row after it, and asserting on the
        # last row would pass even if the assistant text still held the secret.
        # Selected via a list + assertion rather than `next(...)` so a missing row
        # fails on an assertion naming what WAS there, instead of a StopIteration
        # that cannot distinguish "no assistant row" from "the flush threw".
        assistants = [m for m in slot.messages if m.get("role") == "assistant"]
        assert len(assistants) == 1, f"expected one assistant row, got {slot.messages}"
        assert "AKIAIOSFODNN7EXAMPLE" not in assistants[0]["content"]

    def test_a_redacted_connection_string_warns_the_user(self, tmp_path):
        """issue #6189: the corruption must not be silent.

        Uses the reporter's exact command. The assistant row keeps the mangled
        text (redaction is not weakened), but a notice row now follows it saying
        so and warning that the command will not run as pasted.
        """
        from kiro_crew.security import REDACTED_CREDENTIAL_TAG

        state, slot = _state(tmp_path), _slot()
        command = "echo 'DATABASE_URL=postgresql://user:pass@host:5432/db' >> .env"

        chat_runner._flush_segment(state, slot, command)

        roles = [m.get("role") for m in slot.messages]
        assert roles == ["assistant", "notice"]
        assistant, notice = slot.messages[0], slot.messages[1]
        # Redaction still happened -- the credential is gone from what is stored.
        assert "user:pass" not in assistant["content"]
        assert REDACTED_CREDENTIAL_TAG in assistant["content"]
        # ...and the user is now told, including that it will not run as pasted.
        assert "Security notice" in notice["content"]
        assert "will not work if you paste it as-is" in notice["content"]
        assert notice["cls"] == "msg msg-info"
        # The notice must never carry the secret it is reporting on.
        assert "user:pass" not in notice["content"]

    def test_the_notice_counts_multiple_credentials(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        chat_runner._flush_segment(
            state,
            slot,
            "first postgresql://a:b@h1/db then mysql://c:d@h2/db",
        )

        # Assert on the collected rows rather than `next(...)`: a bare `next` over a
        # generator raises StopIteration when the row is missing, and a raise cannot
        # distinguish "no notice was appended" from "_flush_segment threw before
        # appending one". An assertion on the list says which.
        notices = [m for m in slot.messages if m.get("role") == "notice"]
        assert len(notices) == 1, f"expected exactly one notice row, got {notices}"
        assert "2 credentials" in notices[0]["content"]
        assert "were replaced" in notices[0]["content"]

    def test_a_clean_segment_gets_no_notice(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        chat_runner._flush_segment(state, slot, "here is a plain answer")

        assert [m.get("role") for m in slot.messages] == ["assistant"]

    def test_an_encoded_credential_also_warns(self, tmp_path):
        """A base64-encoded credential is redacted by a DIFFERENT pass and tag.

        `redact_credentials` pass 2 substitutes `[REDACTED: encoded credential]`,
        which is not a substring of the plaintext tag. Counting only the plaintext
        tag left this segment silently rewritten -- the same #6189 failure, just
        reached by another pass.
        """
        import base64

        from kiro_crew.security import (
            _REDACTED_ENCODED_CREDENTIAL_TAG,
            REDACTED_CREDENTIAL_TAG,
        )

        state, slot = _state(tmp_path), _slot()
        # The issue's own connection string, base64-encoded.
        blob = base64.b64encode(b"postgresql://user:pass@host:5432/db").decode()

        chat_runner._flush_segment(state, slot, f"decode this: {blob}")

        # Assert on collected rows, not `next(...)`. The roles list is also the
        # witness that `_flush_segment` RAN to completion: if it had thrown before
        # appending, there would be no assistant row either, so a missing notice
        # beside a present assistant row isolates the count as the cause.
        roles = [m.get("role") for m in slot.messages]
        assert roles == ["assistant", "notice"], f"unexpected rows: {roles}"
        assistant, notice = slot.messages[0], slot.messages[1]
        # Redacted by pass 2, so ONLY the encoded tag is present.
        assert _REDACTED_ENCODED_CREDENTIAL_TAG in assistant["content"]
        assert REDACTED_CREDENTIAL_TAG not in assistant["content"]
        assert "A credential" in notice["content"]
        assert "will not work if you paste it as-is" in notice["content"]

    def test_the_two_credential_tags_do_not_overlap(self):
        """Summing per-tag counts must not double-count one substitution.

        `_flush_segment` adds `redacted.count(tag)` across every tag in
        `CREDENTIAL_REDACTION_TAGS`. That is only safe while no tag contains
        another; if a tag were ever renamed to a superstring of another, a single
        redaction would report as two and the notice would overcount.
        """
        from kiro_crew.security import CREDENTIAL_REDACTION_TAGS

        for outer in CREDENTIAL_REDACTION_TAGS:
            for inner in CREDENTIAL_REDACTION_TAGS:
                if outer is inner:
                    continue
                assert inner not in outer, f"{inner!r} is a substring of {outer!r}"
        assert len(set(CREDENTIAL_REDACTION_TAGS)) == len(CREDENTIAL_REDACTION_TAGS)

    def test_trailing_stop_event_is_replaced_below_the_segment(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot.append("chunk", "partial", "chunk", broadcast=False)
        slot.append("stop", "", json.dumps({"kind": "stop_event"}), broadcast=False)

        chat_runner._flush_segment(state, slot, "final text")

        assert [m.get("role") for m in slot.messages] == ["assistant", "stop"]

    def test_unparseable_cls_is_not_a_stop_event(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot.append("chunk", "partial", "not json", broadcast=False)

        chat_runner._flush_segment(state, slot, "final text")

        assert [m.get("role") for m in slot.messages] == ["assistant"]


class TestAppendRedactionNotice:
    """Unit coverage for the shared notice helper (issue #8311).

    ``_flush_segment`` and the seven exception/teardown persists all route
    through ``_append_redaction_notice`` so the credential-redaction notice
    lands wherever a pasteable assistant body is finalized, not only on the
    happy path. These tests exercise the helper directly: it takes the
    ALREADY-REDACTED string (the same bytes persisted to the assistant row),
    counts the artifact tags, and appends one ``msg-info`` notice.
    """

    def test_a_clean_body_gets_no_notice(self, tmp_path):
        _state(tmp_path)  # not needed, but keeps the harness parity explicit
        slot = _slot()

        chat_runner._append_redaction_notice(slot, "here is a plain answer")

        assert [m.get("role") for m in slot.messages] == []

    def test_a_single_credential_gets_one_notice(self, tmp_path):
        from kiro_crew.security import REDACTED_CREDENTIAL_TAG, redact_credentials

        slot = _slot()
        secret = "postgresql://user:pass@host:5432/db"
        redacted, _ = redact_credentials(f"connect with {secret}")

        chat_runner._append_redaction_notice(slot, redacted)

        notices = [m for m in slot.messages if m.get("role") == "notice"]
        assert len(notices) == 1, f"expected one notice row, got {slot.messages}"
        assert notices[0]["cls"] == "msg msg-info"
        assert "Security notice" in notices[0]["content"]
        assert "will not work if you paste it as-is" in notices[0]["content"]
        # The notice must never carry the secret it is reporting on.
        assert "user:pass" not in notices[0]["content"]
        # Coherence check: the tag the count reads is genuinely present in the body.
        assert REDACTED_CREDENTIAL_TAG in redacted

    def test_multiple_credentials_are_counted(self, tmp_path):
        from kiro_crew.security import redact_credentials

        slot = _slot()
        redacted, _ = redact_credentials("first postgresql://a:b@h1/db then mysql://c:d@h2/db")

        chat_runner._append_redaction_notice(slot, redacted)

        notices = [m for m in slot.messages if m.get("role") == "notice"]
        assert len(notices) == 1, f"expected exactly one notice row, got {notices}"
        assert "2 credentials" in notices[0]["content"]
        assert "were replaced" in notices[0]["content"]

    def test_an_encoded_credential_tag_is_counted(self, tmp_path):
        """A base64-encoded credential is redacted by a DIFFERENT pass and tag.

        Counting only the plaintext tag would leave this silently rewritten --
        the same #6189 failure, reached by another pass. The helper reads
        ``CREDENTIAL_REDACTION_TAGS`` so a newly added tag cannot escape.
        """
        import base64

        from kiro_crew.security import (
            _REDACTED_ENCODED_CREDENTIAL_TAG,
            REDACTED_CREDENTIAL_TAG,
            redact_credentials,
        )

        slot = _slot()
        blob = base64.b64encode(b"postgresql://user:pass@host:5432/db").decode()
        redacted, _ = redact_credentials(f"decode this: {blob}")

        chat_runner._append_redaction_notice(slot, redacted)

        # Redacted by pass 2, so ONLY the encoded tag is present.
        assert _REDACTED_ENCODED_CREDENTIAL_TAG in redacted
        assert REDACTED_CREDENTIAL_TAG not in redacted
        notices = [m for m in slot.messages if m.get("role") == "notice"]
        assert len(notices) == 1, f"expected one notice row, got {slot.messages}"
        assert "A credential" in notices[0]["content"]


class TestExceptionPathRedactionNotice:
    """The notice must fire on the exception/teardown persists too (issue #8311).

    Seven branches finalize a pasteable assistant body directly via
    ``slot.append("assistant", <redacted>, "msg msg-a")`` and bypass
    ``_flush_segment``. This drives a real ``AcpProcessDied`` turn (the same
    scripted-provider harness the recovery-ladder tests use) with a credential
    in the streamed text and asserts the notice now lands immediately after the
    assistant row and BEFORE the subsequent retry/error card. It fails if the
    ``_append_redaction_notice`` call is removed from that branch, which is the
    mutation guard the issue asks for.
    """

    @pytest.mark.asyncio
    async def test_process_death_persist_warns_about_a_redacted_credential(self, tmp_path):
        from kiro_crew.acp.client import AcpProcessDied
        from kiro_crew.security import REDACTED_CREDENTIAL_TAG

        state, client = _runner_state(tmp_path)
        slot = _slot()

        # Stream a credential-bearing chunk, then die: assistant_text is non-empty
        # so the AcpProcessDied branch persists the (redacted) partial and, with
        # this fix, appends the notice.
        def _stream(*_args, **_kwargs):
            async def _gen():
                yield LLMEvent(
                    kind=EVENT_TEXT_CHUNK,
                    text="run: psql postgresql://user:pass@host:5432/db",
                )
                raise AcpProcessDied("process exited")

            return _gen()

        client.stream = MagicMock(side_effect=_stream)

        await _drive(state, slot)

        roles = [m.get("role") for m in slot.messages]
        assert "assistant" in roles, f"no assistant row persisted: {slot.messages}"
        assistant = next(m for m in slot.messages if m.get("role") == "assistant")
        notice = next((m for m in slot.messages if m.get("role") == "notice"), None)
        # Redaction still happened on the exception path.
        assert "user:pass" not in assistant["content"]
        assert REDACTED_CREDENTIAL_TAG in assistant["content"]
        # ...and the notice now fires (this is what regresses if the helper call
        # is removed from the AcpProcessDied branch).
        assert notice is not None, f"expected a redaction notice row, got {roles}"
        assert notice["cls"] == "msg msg-info"
        assert "Security notice" in notice["content"]
        assert "user:pass" not in notice["content"]
        # Ordering: notice sits between the assistant body and the retry/error
        # card, mirroring _flush_segment (notice immediately follows its message).
        assert roles.index("notice") == roles.index("assistant") + 1
        assert "error" in roles, f"expected a retry/error card after the notice: {roles}"
        assert roles.index("notice") < roles.index("error")


class TestScheduleWidgetRegistration:
    def test_empty_text_registers_nothing(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        with patch.object(chat_runner.asyncio, "create_task") as create:
            chat_runner._schedule_widget_registration(state, slot, "", "1")

        create.assert_not_called()

    def test_restricted_session_never_registers(self, tmp_path):
        """Incognito slots are denied artifact writes at the HTTP gate too."""
        state, slot = _state(tmp_path), _slot()
        slot.memory_mode = "temporary"
        assert slot.is_restricted is True

        with patch.object(chat_runner.asyncio, "create_task") as create:
            chat_runner._schedule_widget_registration(state, slot, "<mcwidget>x</mcwidget>", "1")

        create.assert_not_called()

    def test_no_running_loop_skips_registration(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        with patch.object(chat_runner.asyncio, "create_task") as create:
            chat_runner._schedule_widget_registration(state, slot, "<mcwidget>x</mcwidget>", "1")

        create.assert_not_called()

    @pytest.mark.asyncio
    async def test_widget_and_image_each_schedule_one_task(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        with (
            patch.object(chat_runner, "register_widgets_off_loop", new=AsyncMock()) as widgets,
            patch.object(chat_runner, "register_images_off_loop", new=AsyncMock()) as images,
        ):
            chat_runner._schedule_widget_registration(
                state, slot, "<mcwidget>x</mcwidget> ![a](/tmp/a.png)", "1"
            )
            await asyncio.sleep(0)

            assert widgets.await_count == 1
            assert images.await_count == 1


# ── prompt / skill expansion ──────────────────────────────────────────────


class TestExpandPromptMention:
    def test_message_without_a_mention_is_untouched(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        assert chat_runner._expand_prompt_mention("plain text", state, slot) == (
            "plain text",
            "not_found",
        )

    def test_lookup_failure_is_reported_as_not_found(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        with patch.object(chat_runner, "_find_prompt", side_effect=RuntimeError("io")):
            assert chat_runner._expand_prompt_mention("@sop", state, slot) == ("@sop", "not_found")

    def test_sensitive_path_is_blocked(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        with (
            patch.object(
                chat_runner, "_find_prompt", return_value={"path": "/home/u/.aws/credentials"}
            ),
            patch.object(chat_runner, "is_sensitive_path", return_value=True),
        ):
            assert chat_runner._expand_prompt_mention("@sop", state, slot) == ("@sop", "blocked")

    def test_oversized_prompt_is_refused(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        big = tmp_path / "big.md"
        big.write_text("y" * (chat_runner.MAX_PROMPT_BYTES + 1), newline="\n")

        with (
            patch.object(
                chat_runner, "_find_prompt", return_value={"path": str(big), "fullName": "big"}
            ),
            patch.object(chat_runner, "is_sensitive_path", return_value=False),
        ):
            assert chat_runner._expand_prompt_mention("@big", state, slot) == ("@big", "too_large")

    def test_unreadable_prompt_is_not_found(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        missing = tmp_path / "gone.md"

        with (
            patch.object(
                chat_runner, "_find_prompt", return_value={"path": str(missing), "fullName": "gone"}
            ),
            patch.object(chat_runner, "is_sensitive_path", return_value=False),
        ):
            assert chat_runner._expand_prompt_mention("@gone", state, slot) == (
                "@gone",
                "not_found",
            )

    def test_resolved_prompt_carries_user_text_and_a_chip(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        sop = tmp_path / "sop.md"
        sop.write_text("Do the thing\n", newline="\n")

        with (
            patch.object(
                chat_runner, "_find_prompt", return_value={"path": str(sop), "fullName": "sop"}
            ),
            patch.object(chat_runner, "is_sensitive_path", return_value=False),
        ):
            expanded, status = chat_runner._expand_prompt_mention("@sop extra ask", state, slot)

        assert status == "ok"
        assert "Do the thing" in expanded
        assert "extra ask" in expanded
        assert slot.messages[-1]["role"] == "system"


class TestExpandDollarSkills:
    def test_no_dollar_token_is_untouched(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        assert chat_runner._expand_dollar_skills("plain", state, slot, "dashboard:x") == (
            "plain",
            0,
        )

    def test_resolution_failure_is_audited_and_swallowed(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        skills = MagicMock()
        skills.resolve_dollar_skills.side_effect = RuntimeError("bad glob")

        with patch.object(chat_runner, "_get_skills", return_value=skills), _quiet_sel() as sel:
            out = chat_runner._expand_dollar_skills("$broken", state, slot, "dashboard:x")

        assert out == ("$broken", 0)
        assert sel.return_value.log_tool_invocation.call_args.kwargs["outcome"] == "error"

    def test_unknown_candidate_is_audited_as_not_found(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        skills = MagicMock()
        skills.resolve_dollar_skills.return_value = []
        skills.has_dollar_candidate.return_value = True

        with patch.object(chat_runner, "_get_skills", return_value=skills), _quiet_sel() as sel:
            out = chat_runner._expand_dollar_skills("$nope", state, slot, "dashboard:x")

        assert out == ("$nope", 0)
        assert sel.return_value.log_tool_invocation.call_args.kwargs["outcome"] == "not_found"

    def test_resolved_skill_body_is_appended_and_redacted(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        skills = MagicMock()
        skills.resolve_dollar_skills.return_value = [
            ("$deploy", "deploy", "step one AKIAIOSFODNN7EXAMPLE")
        ]

        with patch.object(chat_runner, "_get_skills", return_value=skills):
            expanded, count = chat_runner._expand_dollar_skills(
                "run $deploy", state, slot, "dashboard:x"
            )

        assert count == 1
        assert "[Skill: deploy]" in expanded
        assert "AKIAIOSFODNN7EXAMPLE" not in expanded
        assert slot.messages[-1]["role"] == "system"


# ── requeue suppression / pending reset ───────────────────────────────────


class TestRequeueSuppression:
    @pytest.mark.parametrize(
        "stop_state,expected", [("idle", False), ("soft_pending", True), ("killing", True)]
    )
    def test_stop_in_progress_suppresses_requeue(self, stop_state, expected):
        slot = _slot()
        slot._stop_state = stop_state

        assert chat_runner._should_suppress_requeue(slot) is expected


class TestConsumePendingReset:
    @pytest.mark.asyncio
    async def test_no_pending_key_is_a_noop(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_successful_reset_clears_the_flag(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_reset_history_key = "dashboard:chat-cov-1"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.reset.assert_awaited_once_with("dashboard:chat-cov-1")
        assert slot._pending_reset_history_key is None

    @pytest.mark.asyncio
    async def test_a_key_queued_during_the_await_is_not_clobbered(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_reset_history_key = "old-key"

        async def _reset(_key):
            slot._pending_reset_history_key = "newer-key"

        state.sessions.reset = AsyncMock(side_effect=_reset)

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        assert slot._pending_reset_history_key == "newer-key"

    @pytest.mark.asyncio
    async def test_reset_failure_leaves_the_flag_armed(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_reset_history_key = "old-key"
        state.sessions.reset = AsyncMock(side_effect=RuntimeError("no session"))

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        assert slot._pending_reset_history_key == "old-key"

    @pytest.mark.asyncio
    async def test_no_pending_discard_is_a_noop(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_discard_passes_replay_through_and_clears_the_flag(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:chat-cov-1", replay=False, skip_if_busy=True
        )
        assert slot._pending_discard_conversation_key is None

    @pytest.mark.asyncio
    async def test_the_discard_always_asks_for_no_replay(self, tmp_path):
        """One value, not a plumbed choice: replaying the transcript into the
        fresh conversation returns most of what the reset reclaimed. The manager
        keeps the flag for the HTTP route, which does let a caller choose."""
        state, slot = _state(tmp_path), _slot()
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:chat-cov-1", replay=False, skip_if_busy=True
        )

    @pytest.mark.asyncio
    async def test_discard_failure_leaves_the_flag_armed(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_discard_conversation_key = "old-key"
        state.sessions.discard_conversation = AsyncMock(side_effect=RuntimeError("no session"))

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        assert slot._pending_discard_conversation_key == "old-key"

    @pytest.mark.asyncio
    async def test_a_discard_key_queued_during_the_await_is_not_clobbered(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_discard_conversation_key = "old-key"

        async def _discard(_key, **_kw):
            slot._pending_discard_conversation_key = "newer-key"

        state.sessions.discard_conversation = AsyncMock(side_effect=_discard)

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        assert slot._pending_discard_conversation_key == "newer-key"

    @pytest.mark.asyncio
    async def test_both_deferrals_run_because_neither_subsumes_the_other(self, tmp_path):
        """A project reset recreates the session but leaves replay suppression
        alone, so skipping the discard would hand the next turn a rebuilt
        [CONVERSATION HISTORY] block for the conversation that was discarded."""
        state, slot = _state(tmp_path), _slot()
        slot._pending_reset_history_key = "dashboard:chat-cov-1"
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.reset.assert_awaited_once_with("dashboard:chat-cov-1")
        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:chat-cov-1", replay=False, skip_if_busy=True
        )
        assert slot._pending_reset_history_key is None
        assert slot._pending_discard_conversation_key is None

    @pytest.mark.asyncio
    async def test_a_failed_project_reset_does_not_block_the_discard(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_reset_history_key = "old-key"
        slot._pending_discard_conversation_key = "old-key"
        state.sessions.reset = AsyncMock(side_effect=RuntimeError("no session"))

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_awaited_once()
        assert slot._pending_reset_history_key == "old-key"
        assert slot._pending_discard_conversation_key is None


def _subs(running, *, queued: int = 0):
    """A subagent registry stub shaped like the two probes the guard reads."""
    subs = MagicMock()
    subs.running_agents_for = MagicMock(return_value=running)
    subs._queued_depth = MagicMock(return_value=queued)
    return subs


class TestConsumePendingDiscardBoundary:
    """The discard is a full provider teardown, so it is consumed ONLY at the
    end-of-turn boundary. The other two consume points run just before a turn
    acquires the session — a channel turn (Slack, Discord) runs on the linked
    session with no dashboard task at all, so a teardown there lands under a
    reply that is still streaming and loses it."""

    @pytest.mark.asyncio
    async def test_default_caller_may_not_consume_a_discard(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        torn_down = await chat_runner._consume_pending_reset(state, slot)

        state.sessions.discard_conversation.assert_not_awaited()
        assert slot._pending_discard_conversation_key == "dashboard:chat-cov-1"
        assert torn_down is False

    @pytest.mark.asyncio
    async def test_the_end_of_turn_caller_may(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        torn_down = await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:chat-cov-1", replay=False, skip_if_busy=True
        )
        assert torn_down is True

    @pytest.mark.asyncio
    async def test_the_default_caller_still_consumes_a_project_reset(self, tmp_path):
        """Scope pin: only the DISCARD moved to the end-of-turn boundary. The
        project reset must still run before get_or_create or the turn would reuse
        the stale session for one turn."""
        state, slot = _state(tmp_path), _slot()
        slot._pending_reset_history_key = "dashboard:chat-cov-1"

        torn_down = await chat_runner._consume_pending_reset(state, slot)

        state.sessions.reset.assert_awaited_once_with("dashboard:chat-cov-1")
        assert torn_down is True

    @pytest.mark.asyncio
    async def test_the_discard_goes_through_the_atomic_skip_if_busy_path(self, tmp_path):
        """The busy-check and the teardown must be ONE step under the session
        lock. Probing here and tearing down afterwards leaves a window in which a
        channel turn acquires the session's semaphore and begins streaming, and
        the teardown then removes its provider. So the consumer must delegate the
        check rather than perform it."""
        state, slot = _state(tmp_path), _slot()
        slot._pending_discard_conversation_key = "slack:C1:123"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_awaited_once_with(
            "slack:C1:123", replay=False, skip_if_busy=True
        )

    @pytest.mark.asyncio
    async def test_a_manager_refusal_leaves_the_discard_armed(self, tmp_path):
        """False from the manager means it refused under the lock — a turn was in
        flight. Nothing was torn down, so the flag must stay armed."""
        state, slot = _state(tmp_path), _slot()
        state.sessions.discard_conversation = AsyncMock(return_value=False)
        slot._pending_discard_conversation_key = "slack:C1:123"

        torn_down = await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        assert slot._pending_discard_conversation_key == "slack:C1:123"
        assert torn_down is False

    @pytest.mark.asyncio
    async def test_a_refused_discard_lands_at_a_later_boundary(self, tmp_path):
        """The refusal is a wait, not a cancellation."""
        state, slot = _state(tmp_path), _slot()
        state.sessions.discard_conversation = AsyncMock(return_value=False)
        slot._pending_discard_conversation_key = "slack:A"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)
        assert slot._pending_discard_conversation_key == "slack:A"

        state.sessions.discard_conversation = AsyncMock(return_value=True)
        torn_down = await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        assert slot._pending_discard_conversation_key is None
        assert torn_down is True

    @pytest.mark.asyncio
    async def test_an_accepted_discard_reports_a_teardown(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        torn_down = await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        assert torn_down is True
        assert slot._pending_discard_conversation_key is None


class TestConsumePendingDiscardWaitsForSubagents:
    """``discard_conversation`` releases the shared runtime sub-agent children
    run on, and turn end is exactly when they outlive their parent — the parent
    turn ends first, so ``slot.running`` is already False while they keep going.
    Consuming the discard there would kill their work, so an attached child
    leaves the flag ARMED and a later consume applies it."""

    @pytest.mark.asyncio
    async def test_running_child_leaves_the_discard_armed(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        state.subagents = _subs([{"id": "a1"}])
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        torn_down = await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_not_awaited()
        assert slot._pending_discard_conversation_key == "dashboard:chat-cov-1"
        assert torn_down is False

    @pytest.mark.asyncio
    async def test_queued_child_leaves_the_discard_armed(self, tmp_path):
        """A spawn held by the concurrency gate is absent from the running list
        yet WILL start on its own, so it counts as attached."""
        state, slot = _state(tmp_path), _slot()
        state.subagents = _subs([], queued=2)
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_not_awaited()
        assert slot._pending_discard_conversation_key == "dashboard:chat-cov-1"

    @pytest.mark.asyncio
    async def test_inflight_result_delivery_leaves_the_discard_armed(self, tmp_path):
        """The last child can finish — emptying both probes — while its
        completion-event injection is still landing."""
        state, slot = _state(tmp_path), _slot()
        state.subagents = _subs([])
        slot._subagent_deliveries_inflight = 1
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_not_awaited()
        assert slot._pending_discard_conversation_key == "dashboard:chat-cov-1"

    @pytest.mark.asyncio
    async def test_a_failing_running_probe_leaves_the_discard_armed(self, tmp_path):
        """A None running-probe is the probe FAILING, not a slot with no
        children. Fail closed — unknown children are not zero children."""
        state, slot = _state(tmp_path), _slot()
        state.subagents = _subs(None)
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_not_awaited()
        assert slot._pending_discard_conversation_key == "dashboard:chat-cov-1"

    @pytest.mark.asyncio
    async def test_an_unreadable_queue_leaves_the_discard_armed(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        subs = _subs([])
        subs._queued_depth = MagicMock(side_effect=RuntimeError("queue unreadable"))
        state.subagents = subs
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_not_awaited()
        assert slot._pending_discard_conversation_key == "dashboard:chat-cov-1"

    @pytest.mark.asyncio
    async def test_the_armed_discard_lands_once_the_children_are_gone(self, tmp_path):
        """The deferral is a wait, not a cancellation: the caller's reset still
        happens, at the next consume that finds no children."""
        state, slot = _state(tmp_path), _slot()
        state.subagents = _subs([{"id": "a1"}])
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)
        state.sessions.discard_conversation.assert_not_awaited()

        state.subagents = _subs([])
        torn_down = await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:chat-cov-1", replay=False, skip_if_busy=True
        )
        assert slot._pending_discard_conversation_key is None
        assert torn_down is True

    @pytest.mark.asyncio
    async def test_no_children_applies_the_discard(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        state.subagents = _subs([])
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:chat-cov-1", replay=False, skip_if_busy=True
        )

    @pytest.mark.asyncio
    async def test_the_project_reset_is_not_gated_on_children(self, tmp_path):
        """Scope pin: the guard covers the conversation discard this change
        introduced. The pre-existing project-change reset keeps its behaviour."""
        state, slot = _state(tmp_path), _slot()
        state.subagents = _subs([{"id": "a1"}])
        slot._pending_reset_history_key = "dashboard:chat-cov-1"

        torn_down = await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.reset.assert_awaited_once_with("dashboard:chat-cov-1")
        assert slot._pending_reset_history_key is None
        assert torn_down is True

    @pytest.mark.asyncio
    async def test_a_deferred_discard_does_not_hide_a_project_reset(self, tmp_path):
        """Both queued, children attached: the project reset still runs and the
        discard stays armed, so neither deferral swallows the other."""
        state, slot = _state(tmp_path), _slot()
        state.subagents = _subs([{"id": "a1"}])
        slot._pending_reset_history_key = "dashboard:chat-cov-1"
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        torn_down = await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.reset.assert_awaited_once_with("dashboard:chat-cov-1")
        state.sessions.discard_conversation.assert_not_awaited()
        assert slot._pending_reset_history_key is None
        assert slot._pending_discard_conversation_key == "dashboard:chat-cov-1"
        assert torn_down is True

    @pytest.mark.asyncio
    async def test_no_registry_abstains_rather_than_blocking_forever(self, tmp_path):
        """No registry means no runtime for a child to be attached to. Blocking
        on that would arm a discard that could never land."""
        state, slot = _state(tmp_path), _slot()
        state.subagents = None
        slot._pending_discard_conversation_key = "dashboard:chat-cov-1"

        await chat_runner._consume_pending_reset(state, slot, allow_discard=True)

        state.sessions.discard_conversation.assert_awaited_once()


# ── eager spawn / resume prefetch ─────────────────────────────────────────


class TestScheduleEagerSpawn:
    def test_disabled_config_returns_no_task(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        cfg = MagicMock()
        cfg.session.eager_spawn = False

        with patch.object(chat_runner.KiroCrewConfig, "load", return_value=cfg):
            assert chat_runner.schedule_eager_spawn(state, slot) is None

    def test_config_load_failure_returns_no_task(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        with patch.object(chat_runner.KiroCrewConfig, "load", side_effect=RuntimeError("bad toml")):
            assert chat_runner.schedule_eager_spawn(state, slot) is None

    @pytest.mark.asyncio
    async def test_a_newer_signal_cancels_the_pending_task(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        cfg = MagicMock()
        cfg.session.eager_spawn = True

        with (
            patch.object(chat_runner.KiroCrewConfig, "load", return_value=cfg),
            patch.object(chat_runner, "_eager_spawn", new=AsyncMock()),
        ):
            first = chat_runner.schedule_eager_spawn(state, slot)
            second = chat_runner.schedule_eager_spawn(state, slot)
            await asyncio.sleep(0)

        assert first is not None and second is not None
        assert first.cancelled() or first.done()
        second.cancel()


class TestCapArmedPrefetches:
    @pytest.mark.asyncio
    async def test_eviction_failure_still_drops_the_registry_entry(self, tmp_path):
        state = _state(tmp_path)
        state.sessions.remove_if_unclaimed = AsyncMock(side_effect=RuntimeError("shutdown hung"))
        chat_runner._armed_prefetches.clear()
        try:
            for i in range(chat_runner._RESUME_PREFETCH_MAX_LIVE + 1):
                await chat_runner._cap_armed_prefetches(state.sessions, f"key-{i}")

            assert len(chat_runner._armed_prefetches) == chat_runner._RESUME_PREFETCH_MAX_LIVE
            assert "key-0" not in chat_runner._armed_prefetches
        finally:
            chat_runner._armed_prefetches.clear()

    @pytest.mark.asyncio
    async def test_rearming_moves_a_key_to_newest(self, tmp_path):
        state = _state(tmp_path)
        state.sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        chat_runner._armed_prefetches.clear()
        try:
            for i in range(chat_runner._RESUME_PREFETCH_MAX_LIVE):
                await chat_runner._cap_armed_prefetches(state.sessions, f"key-{i}")
            await chat_runner._cap_armed_prefetches(state.sessions, "key-0")
            await chat_runner._cap_armed_prefetches(state.sessions, "newest")

            assert "key-0" in chat_runner._armed_prefetches
            assert "key-1" not in chat_runner._armed_prefetches
        finally:
            chat_runner._armed_prefetches.clear()


class TestPrefetchTtl:
    @pytest.mark.asyncio
    async def test_replaced_slot_owner_keeps_the_session(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        state.get_slot = MagicMock(return_value=_slot("other"))

        with patch.object(chat_runner.asyncio, "sleep", new=AsyncMock()):
            await chat_runner._prefetch_ttl(state, slot, "dashboard:chat-cov-1")

        state.sessions.remove_if_unclaimed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_running_turn_claims_the_session(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        state.get_slot = MagicMock(return_value=slot)
        slot.task = MagicMock(done=MagicMock(return_value=False))

        with patch.object(chat_runner.asyncio, "sleep", new=AsyncMock()):
            await chat_runner._prefetch_ttl(state, slot, "dashboard:chat-cov-1")

        state.sessions.remove_if_unclaimed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deleted_slot_still_tears_down_a_linked_session(self, tmp_path):
        """A channel-born slot's prefetch key is not the slot-derived one."""
        state, slot = _state(tmp_path), _slot()
        state.get_slot = MagicMock(return_value=None)
        state.sessions.remove_if_unclaimed = AsyncMock(return_value=True)

        with patch.object(chat_runner.asyncio, "sleep", new=AsyncMock()):
            await chat_runner._prefetch_ttl(state, slot, "telegram:123")

        state.sessions.remove_if_unclaimed.assert_awaited_once_with("telegram:123")

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        with patch.object(
            chat_runner.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)
        ):
            with pytest.raises(asyncio.CancelledError):
                await chat_runner._prefetch_ttl(state, slot, "dashboard:chat-cov-1")

    @pytest.mark.asyncio
    async def test_unexpected_failure_is_logged_not_raised(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        state.get_slot = MagicMock(side_effect=RuntimeError("map gone"))

        with patch.object(chat_runner.asyncio, "sleep", new=AsyncMock()):
            await chat_runner._prefetch_ttl(state, slot, "dashboard:chat-cov-1")

    @pytest.mark.asyncio
    async def test_scheduling_cancels_the_previous_timer(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        with patch.object(chat_runner, "_prefetch_ttl", new=AsyncMock()):
            chat_runner._schedule_prefetch_ttl(state, slot, "k1")
            first = slot._prefetch_ttl_task
            chat_runner._schedule_prefetch_ttl(state, slot, "k2")
            second = slot._prefetch_ttl_task
            await asyncio.sleep(0)

        assert first is not second
        assert first.cancelled() or first.done()
        second.cancel()


# ── steer settle / requeue ────────────────────────────────────────────────


class TestSteerLifecycle:
    def test_settle_is_a_noop_without_pending_steers(self):
        slot = _slot()

        chat_runner._settle_consumed_steers(slot, "anything")

        assert slot._pending_steers == []

    def test_settle_delegates_to_the_shared_rules(self):
        slot = _slot()
        slot._pending_steers = ["a", "b"]

        with patch.object(chat_runner, "settle_consumed_steers", return_value=["b"]) as settle:
            chat_runner._settle_consumed_steers(slot, "a")

        assert slot._pending_steers == ["b"]
        assert settle.call_args.kwargs["settle_all_on_empty"] is True

    def test_requeue_is_a_noop_without_pending_steers(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        chat_runner._requeue_unconsumed_steers(state, slot)

        assert slot._queue == []

    def test_unconsumed_steers_requeue_at_the_head_in_order(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot.queue_append("already queued")
        slot._pending_steers = ["first steer", "second steer"]

        chat_runner._requeue_unconsumed_steers(state, slot)

        assert [entry["content"] for entry in slot._queue] == [
            "first steer",
            "second steer",
            "already queued",
        ]
        assert slot._pending_steers == []

    def test_broadcast_failure_still_leaves_the_steer_queued(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_steers = ["steer me"]
        state.broadcast_ws = MagicMock(side_effect=RuntimeError("ws closed"))

        chat_runner._requeue_unconsumed_steers(state, slot)

        assert [entry["content"] for entry in slot._queue] == ["steer me"]


# ── queue drain / synthesis ───────────────────────────────────────────────


class TestStartNextQueuedTurn:
    @pytest.mark.asyncio
    async def test_empty_queue_starts_nothing(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        assert await chat_runner._start_next_queued_turn(state, slot) is False

    @pytest.mark.asyncio
    async def test_config_failure_falls_back_to_sequential_dequeue(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot.queue_append("one")
        slot.queue_append("two")
        state.subagents = None

        with (
            patch.object(chat_runner.KiroCrewConfig, "load", side_effect=RuntimeError("bad toml")),
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()) as spawn,
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
        ):
            assert await chat_runner._start_next_queued_turn(state, slot) is True

        assert spawn.call_count == 1
        assert len(slot._queue) == 1

    @pytest.mark.asyncio
    async def test_running_subagents_hold_user_messages_back(self, tmp_path):
        """With a fan-out in flight only a system injection may drain."""
        state, slot = _state(tmp_path), _slot()
        slot.queue_append("a user message")
        state.subagents = MagicMock(running_agents_for=MagicMock(return_value=["agent-1"]))

        assert await chat_runner._start_next_queued_turn(state, slot) is False
        assert len(slot._queue) == 1

    @pytest.mark.asyncio
    async def test_reset_notice_is_emitted_for_a_stopping_slot(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot.queue_append("next please")
        slot._stopping = True
        state.subagents = None

        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
        ):
            assert await chat_runner._start_next_queued_turn(state, slot) is True

        assert any("Session reset" in err for err in _errors(slot))
        assert slot._stopping is False

    @pytest.mark.asyncio
    async def test_a_held_note_lands_above_the_successors_own_row(self, tmp_path):
        """A note held mid-turn must be written before the next turn's user row.

        The note's context half drains inside that turn's ``_run_chat``, so
        flushing after it started would put the visible line below the response
        the note shaped. Only ``_finish_queue_cycle`` used to flush, and the main
        dispatch path calls it AFTER this function.
        """
        state, slot = _state(tmp_path), _slot()
        slot._deferred_notes.append({"content": "held", "cls": "reconcile-note"})
        slot.queue_append("next please")
        state.subagents = None

        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
        ):
            assert await chat_runner._start_next_queued_turn(state, slot) is True

        roles = [m["role"] for m in slot.messages]
        contents = [m["content"] for m in slot.messages]
        assert "held" in contents
        assert "user" in roles
        assert contents.index("held") < roles.index("user")
        assert slot._deferred_notes == []

    @pytest.mark.asyncio
    async def test_a_held_note_is_withheld_from_a_plans_next_stage(self, tmp_path):
        """A plain user message queued during a plan must not release the note.

        This is the flush that leaks FIRST. A queued user message carries no
        ``kind``, so the origin-tag guard admits the flush -- and it runs ABOVE the
        ``in_stage`` dequeue gate that then holds that message back. So the note
        was released while no user turn started at all, and the next stage drained
        its context half. ``_stage_loop``'s exit flush is the seam that owes it
        delivery, so withholding delays rather than loses it.
        """
        state, slot = _state(tmp_path), _slot()
        slot._deferred_notes.append({"content": "held", "cls": "reconcile-note"})
        slot.queue_append("a plain user message")  # carries no `kind`
        slot._in_stage_execution = True
        state.subagents = None

        assert await chat_runner._start_next_queued_turn(state, slot) is False

        assert len(slot._queue) == 1, "fixture: the user message must be held back"
        assert len(slot._deferred_notes) == 1, "the note was released into the next stage"
        assert "held" not in [m["content"] for m in slot.messages]

        # Control: the same fixture with the plan gate CLEAR does flush, so the
        # assertion above measures the stage guard rather than the dequeue hold.
        state2, slot2 = _state(tmp_path), _slot()
        slot2._deferred_notes.append({"content": "held", "cls": "reconcile-note"})
        slot2.queue_append("a plain user message")
        state2.subagents = None
        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
        ):
            assert await chat_runner._start_next_queued_turn(state2, slot2) is True
        assert slot2._deferred_notes == [], "control: the note should flush off-plan"


class TestRunPendingSynthesis:
    @pytest.mark.asyncio
    async def test_unarmed_synthesis_just_finishes_the_cycle(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_synthesis = False
        slot._synthesis_inflight = True

        with patch.object(chat_runner, "_finish_queue_cycle") as finish:
            await chat_runner._run_pending_synthesis(state, slot)

        finish.assert_called_once()
        assert slot._synthesis_inflight is False

    @pytest.mark.asyncio
    async def test_a_queued_message_takes_priority_over_synthesis(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_synthesis = True
        slot.queue_append("queued work")

        with patch.object(
            chat_runner, "_start_next_queued_turn", new=AsyncMock(return_value=True)
        ) as start:
            await chat_runner._run_pending_synthesis(state, slot)

        start.assert_awaited_once()
        assert slot._pending_synthesis is True

    @pytest.mark.asyncio
    async def test_running_agents_defer_synthesis(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_synthesis = True
        state.subagents = MagicMock(running_agents_for=MagicMock(return_value=["a"]))

        with patch.object(chat_runner, "_finish_queue_cycle") as finish:
            await chat_runner._run_pending_synthesis(state, slot)

        finish.assert_called_once()
        assert slot._pending_synthesis is True

    @pytest.mark.asyncio
    async def test_synthesis_timeout_is_swallowed(self, tmp_path):
        """The ceiling already rendered a card; re-raising would go unretrieved."""
        state, slot = _state(tmp_path), _slot()
        slot._pending_synthesis = True
        state.subagents = MagicMock(running_agents_for=MagicMock(return_value=[]))

        async def _boom():
            raise asyncio.TimeoutError

        with (
            patch.object(
                chat_runner, "spawn_guarded_turn", return_value=asyncio.ensure_future(_boom())
            ),
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
        ):
            await chat_runner._run_pending_synthesis(state, slot)

        assert slot._pending_synthesis is False
        assert slot._synthesis_inflight is False

    @pytest.mark.asyncio
    async def test_synthesis_prompt_is_appended_as_an_inject_row(self, tmp_path):
        """The prompt must reach the transcript as `inject`, never as user speech.

        This site bypasses `_start_next_queued_turn` (it runs no queue entry),
        which is the only other place a turn-dispatching path appends a row. It
        previously appended nothing at all, so the prompt reached the
        conversation log with no dashboard row and resurfaced attributed to the
        USER on replay.
        """
        state, slot = _state(tmp_path), _slot()
        slot._pending_synthesis = True
        state.subagents = MagicMock(running_agents_for=MagicMock(return_value=[]))

        async def _ok():
            return None

        # Observed INSIDE the dispatch, so the test can prove the row was already
        # appended when the turn started. Asserting only on the final state cannot
        # tell append-before-dispatch from append-after, and the ordering is the
        # whole point: a turn that dies immediately must still leave the row.
        seen: dict = {}

        def _capture(_state, _slot, coro, *a, **kw):
            seen["rows"] = len(_slot.messages)
            coro.close()  # never awaited; avoids an un-awaited-coroutine warning
            return asyncio.ensure_future(_ok())

        with (
            patch.object(chat_runner, "spawn_guarded_turn", side_effect=_capture),
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()) as run_chat,
        ):
            await chat_runner._run_pending_synthesis(state, slot)

        assert seen.get("rows") == 1, "the row must exist BEFORE the turn is dispatched"
        rows = [m for m in slot.messages if m.get("content", "").startswith("[SYSTEM] Sub-agent")]
        assert len(rows) == 1, "the synthesis prompt must appear exactly once"
        row = rows[0]
        assert row["role"] == "inject"
        # Never the user-bubble class: that is what produced the reported defect.
        assert row.get("cls") == "msg msg-inject"
        # Durable provenance. `cls` is NOT persisted for role `inject`, so a render
        # side keyed on cls-derived data mis-renders every restored row; `meta` is.
        assert (row.get("meta") or {}).get("injectKind") == "synthesis"
        # And the turn itself must declare it is runner-authored, rather than
        # leaving a downstream marker-match to recover the same fact.
        assert run_chat.call_args.kwargs.get("_synthetic_payload") is True

    def test_inject_provenance_survives_the_persistence_boundary(self):
        """`injectKind` must round-trip; the older `cls` channel does not.

        This is the property the whole render decision now rests on. An inject
        row's ``cls`` is persisted only for ``role == "system"``, so the
        ``cronLabel`` the frontend reads (synthesized from ``cls`` at emit time)
        silently disappears on the next rehydrate — which is exactly how a
        carve-out keyed on it swallowed every restored cron notification. Assert
        the durable channel directly rather than trusting the live shape.
        """
        from kiro_crew.dashboard.chat_persistence import _build_message_entry_uncached

        cron_cls = json.dumps({"cronLabel": "nightly-audit"})
        entry = _build_message_entry_uncached(
            {
                "role": "inject",
                "content": '[Cron notification from "nightly-audit"]\nreport\n',
                "cls": cron_cls,
                "meta": {"injectKind": "cron", "cronLabel": "nightly-audit"},
            }
        )
        # The negative half — proves the bug this replaces was real, and fails if
        # someone "fixes" it by widening the cls gate instead.
        assert "cls" not in entry, "cls is not persisted for an inject row"
        assert entry["meta"]["injectKind"] == "cron"
        assert entry["meta"]["cronLabel"] == "nightly-audit"

        synth = _build_message_entry_uncached(
            {
                "role": "inject",
                "content": "[SYSTEM] Sub-agent synthesis: go",
                "cls": "msg msg-inject",
                "meta": {"injectKind": "synthesis"},
            }
        )
        assert synth["meta"]["injectKind"] == "synthesis"


class TestFinishQueueCycle:
    @pytest.mark.asyncio
    async def test_eligible_synthesis_is_started_instead_of_going_idle(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        state._slots[slot.key] = slot  # a live slot is registered
        slot._pending_synthesis = True
        state.subagents = MagicMock(running_agents_for=MagicMock(return_value=[]))

        with patch.object(chat_runner, "_run_pending_synthesis", new=AsyncMock()):
            chat_runner._finish_queue_cycle(state, slot)
            await asyncio.sleep(0)

        assert slot._synthesis_inflight is True
        assert not any(m.get("role") == "done" for m in slot.messages)
        if slot.task is not None:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_a_held_note_is_withheld_from_an_automatic_synthesis_turn(self, tmp_path):
        """A note is owed to the next USER turn, so synthesis must not drain it.

        Synthesis is dispatched from this same function, so flushing here would
        hand the held context to a turn the user never asked for. The user-turn
        seams flush on their own, so withholding cannot lose the note.
        """
        state, slot = _state(tmp_path), _slot()
        state._slots[slot.key] = slot  # a live slot is registered
        slot._pending_synthesis = True
        state.subagents = MagicMock(running_agents_for=MagicMock(return_value=[]))

        with (
            patch.object(type(slot), "flush_deferred_notes", return_value=0) as flush,
            patch.object(chat_runner, "_run_pending_synthesis", new=AsyncMock()),
        ):
            chat_runner._finish_queue_cycle(state, slot)
            await asyncio.sleep(0)

        assert slot._synthesis_inflight is True
        flush.assert_not_called()
        if slot.task is not None:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_a_held_note_is_withheld_from_a_plans_next_stage(self, tmp_path):
        """This function runs per stage, so a flush here feeds stage N+1.

        Each stage of a plan is its own ``_run_chat``, and this is called from that
        turn's ``finally`` while ``_in_stage_execution`` is still set -- so the note
        reached the next stage instead of the next USER turn. Distinct from the
        synthesis case above: here ``will_synthesize`` is False, which is exactly
        why the old guard admitted the flush.
        """
        state, slot = _state(tmp_path), _slot()
        state._slots[slot.key] = slot
        slot._in_stage_execution = True
        state.subagents = MagicMock(running_agents_for=MagicMock(return_value=[]))

        with patch.object(type(slot), "flush_deferred_notes", return_value=0) as flush:
            chat_runner._finish_queue_cycle(state, slot)
            await asyncio.sleep(0)
        flush.assert_not_called()
        if slot.task is not None:
            slot.task.cancel()

        # Control: identical state with the plan gate clear DOES flush, so the
        # assertion above cannot pass for some reason unrelated to the stage.
        state2, slot2 = _state(tmp_path), _slot()
        state2._slots[slot2.key] = slot2
        state2.subagents = MagicMock(running_agents_for=MagicMock(return_value=[]))

        with patch.object(type(slot2), "flush_deferred_notes", return_value=0) as flush2:
            chat_runner._finish_queue_cycle(state2, slot2)
            await asyncio.sleep(0)
        flush2.assert_called_once()
        if slot2.task is not None:
            slot2.task.cancel()

    @pytest.mark.asyncio
    async def test_a_closing_slot_does_not_lose_its_held_note_to_synthesis(self, tmp_path):
        """A slot already removed from the registry has no next USER turn.

        The withhold above is scoped to WHICH successor drains the note, and it
        assumes a successor exists. A slot gone from ``state._slots`` is being
        torn down, so withholding there discards the note the POST acknowledged.
        """
        state, slot = _state(tmp_path), _slot()
        state.subagents = MagicMock(running_agents_for=MagicMock(return_value=[]))
        slot._pending_synthesis = True
        slot._deferred_notes.append({"content": "held across the close", "cls": "reconcile-note"})
        # The teardown already dropped it from the registry.
        assert state._slots.get(slot.key) is None

        with patch.object(chat_runner, "_run_pending_synthesis", new=AsyncMock()):
            chat_runner._finish_queue_cycle(state, slot)
            await asyncio.sleep(0)

        assert slot._deferred_notes == [], "the held note was discarded on close"
        assert any(
            m.get("role") == "inject" and "held across the close" in m.get("content", "")
            for m in slot.messages
        )
        if slot.task is not None:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_a_cycle_with_no_synthesis_still_flushes(self, tmp_path):
        """The withhold is scoped to synthesis; every other cycle still flushes."""
        state, slot = _state(tmp_path), _slot()
        slot._pending_synthesis = False

        with (
            patch.object(type(slot), "flush_deferred_notes", return_value=0) as flush,
            patch.object(chat_runner, "maybe_refresh_title", new=AsyncMock()),
        ):
            chat_runner._finish_queue_cycle(state, slot)
            await asyncio.sleep(0)

        flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_idle_cycle_emits_done_and_refreshes_the_sidebar(self, tmp_path):
        state, slot = _state(tmp_path), _slot()

        with patch.object(chat_runner, "maybe_refresh_title", new=AsyncMock()):
            chat_runner._finish_queue_cycle(state, slot)
            await asyncio.sleep(0)

        assert slot.messages[-1]["role"] == "done"
        assert slot.task is None
        state.refresh_slot_source_status.assert_called_once_with(slot.key)
        state.broadcast_ws.assert_any_call("chat_done", {"slot": slot.key})


class TestTtftMetric:
    def test_emission_failure_is_swallowed(self):
        with patch("kiro_crew.metrics.provider.get_recorder", side_effect=RuntimeError("down")):
            chat_runner._emit_ttft_metric(0.0, "dashboard:x", is_new=True, resumed=False)

    def test_attributes_split_cold_and_resumed_populations(self):
        recorder = MagicMock()
        with patch("kiro_crew.metrics.provider.get_recorder", return_value=recorder):
            chat_runner._emit_ttft_metric(0.0, "dashboard:x", is_new=True, resumed=True)

        attrs = recorder.histogram.call_args.kwargs["attrs"]
        assert attrs["first_turn"] is True
        assert attrs["resumed"] is True


# ── native subagent cards ─────────────────────────────────────────────────


class TestNativeSubagentCards:
    def test_non_list_payload_is_ignored(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        tracker: dict = {}

        chat_runner._native_subagent_sync(state, slot, "not-a-list", tracker)

        assert tracker == {}

    def test_entry_without_a_task_is_marked_done_immediately(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        tracker: dict = {}

        chat_runner._native_subagent_sync(
            state, slot, [{"sessionId": "s1", "role": "worker"}], tracker
        )

        assert tracker["s1"]["done"] is True
        assert tracker["s1"]["task"] == ""

    def test_terminal_status_completes_the_card_with_a_redacted_error(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        tracker: dict = {}
        entry = {"sessionId": "s2", "role": "worker", "initialQuery": "do it"}

        chat_runner._native_subagent_sync(state, slot, [entry], tracker)
        entry["status"] = {"type": "failed", "message": "boom AKIAIOSFODNN7EXAMPLE"}
        chat_runner._native_subagent_sync(state, slot, [entry], tracker)

        assert tracker["s2"]["done"] is True
        assert "AKIAIOSFODNN7EXAMPLE" not in tracker["s2"]["error"]

    def test_status_message_surfaces_as_the_current_tool(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        tracker: dict = {}
        entry = {"sessionId": "s3", "initialQuery": "do it"}

        chat_runner._native_subagent_sync(state, slot, [entry], tracker)
        entry["status"] = {"type": "working", "message": "reading files"}
        chat_runner._native_subagent_sync(state, slot, [entry], tracker)

        assert tracker["s3"]["last_tool"] == "reading files"
        assert any(call.args[0] == "subagent_tool" for call in state.broadcast_ws.call_args_list)

    def test_unreported_stale_card_is_auto_closed(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        tracker: dict = {
            "gone": {
                "id": "native:gone",
                "started": 0.0,
                "done": False,
                "agent": "worker",
                "task": "stalled work",
                "last_activity": 0.0,
            }
        }

        chat_runner._native_subagent_sync(state, slot, [], tracker)

        assert tracker["gone"]["done"] is True
        assert tracker["gone"]["error"] == "timed out (no activity)"

    def test_close_all_completes_open_cards_without_an_error(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        tracker = {"open": {"started": 0.0, "done": False, "task": "t", "agent": "a"}}

        chat_runner._native_subagent_close_all(state, slot, tracker)

        assert tracker["open"]["done"] is True
        assert tracker["open"]["error"] is None

    def test_card_registry_round_trip(self, tmp_path):
        state = _state(tmp_path)

        chat_runner._register_native_card(state, "native:x", "chat-1", "sid-1")
        assert state._native_cards["native:x"]["session_id"] == "sid-1"

        chat_runner._unregister_native_card(state, "native:x")
        assert "native:x" not in state._native_cards

    def test_unregister_without_a_registry_is_a_noop(self, tmp_path):
        state = _state(tmp_path)
        if hasattr(state, "_native_cards"):
            del state._native_cards

        chat_runner._unregister_native_card(state, "native:x")

    def test_terminal_records_are_bounded_by_keep_and_ttl(self):
        now = 1000.0
        tracker = {
            "fresh": {"id": "native:fresh", "done": True, "done_at": now - 1},
            "older": {"id": "native:older", "done": True, "done_at": now - 2},
            "expired": {"id": "native:expired", "done": True, "done_at": now - 10_000},
            "running": {"id": "native:running", "done": False, "done_at": now},
            "unidentified": {"done": True, "done_at": now},
        }

        kept = chat_runner._retain_terminal_native(tracker, keep=1, ttl_secs=100.0, now=now)

        assert list(kept) == ["fresh"]

    def test_native_output_collapses_to_the_newest_tail(self):
        buf: list[str] = []
        total = chat_runner._append_native_output(buf, "a" * 50, 0, cap=10, hard=20)

        assert total == 10
        assert buf == ["a" * 10]

    def test_done_result_marks_a_truncated_feed(self):
        out = chat_runner._native_done_result(
            ["z" * (chat_runner.NATIVE_SUBAGENT_DONE_RESULT_CAP + 5)]
        )

        assert out.startswith(chat_runner.NATIVE_SUBAGENT_DONE_TRUNC_MARKER)

    def test_card_feed_redacts_before_the_broadcast(self):
        feed = chat_runner._native_card_feed({"native:x": ["AKIAIOSFODNN7EXAMPLE"]}, "native:x")

        assert "AKIAIOSFODNN7EXAMPLE" not in feed

    def test_empty_card_feed_is_empty(self):
        assert chat_runner._native_card_feed(None, "native:x") == ""


# ── command parsing helpers ───────────────────────────────────────────────


class TestCommandParsing:
    def test_quoted_separators_are_masked_and_restored(self):
        masked, restore = chat_runner._mask_quoted_separators('grep "a|b" f && wc -l')

        assert "|" not in masked.split("&&")[0].replace("&&", "")
        assert list(restore.values()) == ["|"]

    def test_command_substitution_is_denied_by_default(self):
        assert chat_runner._matches_trusted_pattern("echo $(whoami)", {"echo*"}) is None

    def test_every_segment_must_match_for_a_chained_command(self):
        assert chat_runner._matches_trusted_pattern("ls | rm -rf x", {"ls*"}) is None

    def test_all_matching_segments_return_joined_patterns(self):
        matched = chat_runner._matches_trusted_pattern("ls | wc -l", {"ls*", "wc*"})

        assert matched is not None
        assert matched.count(",") == 1

    def test_redirect_forms_are_not_treated_as_separators(self):
        assert chat_runner._matches_trusted_pattern("ls 2>&1", {"ls*"}) is not None

    def test_an_escaped_quote_does_not_hide_a_real_separator(self):
        """A closing quote followed by `\\'` leaves quoted context, so the `;`
        after it is a separator the shell acts on.

        Reading that `\\'` as an OPENING quote makes the remainder look quoted, the
        separator gets masked, the line collapses to one segment, and an appended
        command inherits whatever the first segment was allowed to do. Verified
        against a real shell: `echo 'foo'\\'; cmd` runs `cmd`.
        """
        command = "echo 'foo'\\'; whoami"
        _, segments = chat_runner._split_command_segments(command) or ("", [])

        assert len(segments) == 2, segments
        assert chat_runner._matches_trusted_pattern(command, {"echo*"}) is None

    def test_an_escaped_double_quote_keeps_the_quote_open(self):
        """Inside double quotes `\\"` is an escaped literal, so the quote stays
        OPEN and the `;` after it really is quoted. One segment is the correct
        reading: the line is an unterminated quote, which a shell refuses to run
        at all rather than executing a second command, so there is nothing here
        for segmentation to protect against."""
        command = 'echo "foo\\"; whoami'
        _, segments = chat_runner._split_command_segments(command) or ("", [])

        assert len(segments) == 1, segments

    def test_an_escaped_separator_outside_quotes_still_segments(self):
        """`\\;` is an escaped literal to the shell, not a separator -- but the
        allowlist must not approve the tail either way, so segmentation stays
        fail-closed rather than trying to model every escape."""
        assert chat_runner._matches_trusted_pattern("ls \\; whoami", {"ls*"}) is None

    def test_a_backslash_inside_single_quotes_stays_literal(self):
        """The shell does not honor escapes inside single quotes, so a trailing
        backslash there must not swallow the closing quote."""
        masked, restore = chat_runner._mask_quoted_separators("echo 'a|b\\' && wc -l")

        assert list(restore.values()) == ["|"]
        assert masked.endswith("&& wc -l")

    def test_base_command_extraction_dedups_across_segments(self):
        assert chat_runner._extract_base_command("cat a | wc -l | cat b") == "cat,wc"

    def test_full_command_preserves_canonical_text(self):
        assert chat_runner._extract_full_command("Reading /usr/bin/id") == "Reading /usr/bin/id"


# ── model backfill / pin guards ───────────────────────────────────────────


class TestModelBackfill:
    @pytest.mark.parametrize(
        "model,expected",
        [
            ("global.anthropic.claude-opus-4-8[1m]", True),
            ("us.anthropic.claude-sonnet-4-6", True),
            ("claude-opus-4.7", False),
            ("deepseek-3.2", False),
        ],
    )
    def test_bedrock_profile_detection(self, model, expected):
        assert chat_runner._is_bedrock_profile_id(model) is expected

    def test_missing_provider_model_backfills_nothing(self):
        client = MagicMock()
        client.client._model = ""

        assert chat_runner._backfill_canonical_model(client, "kiro") == ""

    def test_auto_sentinel_is_skipped(self):
        client = MagicMock()
        client.client._model = "auto"

        assert chat_runner._backfill_canonical_model(client, "kiro") == ""

    def test_profile_id_is_dropped_off_the_claude_code_path(self):
        """Caching a resolved profile id would pin the slot to one region."""
        client = MagicMock()
        client.client._model = "global.anthropic.claude-opus-4-8[1m]"

        assert chat_runner._backfill_canonical_model(client, "kiro") == ""

    def test_portable_alias_is_kept(self):
        client = MagicMock()
        client.client._model = "deepseek-3.2"

        with patch.object(
            chat_runner.model_registry, "canonicalize_for_provider", return_value="deepseek-3.2"
        ):
            assert chat_runner._backfill_canonical_model(client, "kiro") == "deepseek-3.2"


class TestPinnedModelWithheld:
    @pytest.mark.parametrize(
        "model,provider", [("", "kiro"), ("auto", "kiro"), ("x", "claude_code")]
    )
    def test_unpinnable_combinations_are_never_withheld(self, model, provider):
        assert chat_runner._pinned_model_verdict(MagicMock(), model, provider) is None

    def test_claude_backend_is_exempt(self):
        client = MagicMock()
        client.is_claude_backend = True

        assert chat_runner._pinned_model_verdict(client, "claude-opus-5", "kiro") is None

    def test_provider_without_an_advertiser_leaves_the_pin_alone(self):
        client = MagicMock()
        client.is_claude_backend = False
        client.available_models = "not-callable"

        assert chat_runner._pinned_model_verdict(client, "claude-opus-5", "kiro") is None

    def test_advertiser_failure_fails_open(self):
        client = MagicMock()
        client.is_claude_backend = False
        client.available_models = MagicMock(side_effect=RuntimeError("no session"))

        assert chat_runner._pinned_model_verdict(client, "claude-opus-5", "kiro") is None

    def test_unadvertised_pin_is_reported_as_withheld(self):
        client = MagicMock()
        client.is_claude_backend = False
        client.available_models = MagicMock(return_value=[{"modelId": "claude-sonnet-4.6"}])

        with patch.object(chat_runner, "advertised_model_ids", return_value={"claude-sonnet-4.6"}):
            assert chat_runner._pinned_model_verdict(client, "claude-opus-5", "kiro") is True


class TestContextUsagePayload:
    def test_missing_counts_emit_a_reset_frame(self):
        """A bare {slot, pct} frame would strand stale token counts on the ring."""
        client = MagicMock()
        client.context_usage_pct = MagicMock(return_value=44.44)
        client.context_window_tokens = MagicMock(return_value=0)

        payload = chat_runner._context_usage_payload("chat-1", client)

        assert payload == {"slot": "chat-1", "pct": 44.4, "reset": True}

    def test_real_counts_ship_the_pair(self):
        client = MagicMock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        client.context_window_tokens = MagicMock(return_value=200_000)
        client.context_used_tokens = MagicMock(return_value=20_000)

        payload = chat_runner._context_usage_payload("chat-1", client)

        assert payload["used_tokens"] == 20_000
        assert payload["window_tokens"] == 200_000
        assert "reset" not in payload

    def test_zero_used_still_emits_a_reset_frame(self):
        client = MagicMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.context_window_tokens = MagicMock(return_value=200_000)
        client.context_used_tokens = MagicMock(return_value=0)

        assert chat_runner._context_usage_payload("chat-1", client)["reset"] is True


# ── _run_chat: local commands ─────────────────────────────────────────────


class TestRunChatLocalCommands:
    @pytest.mark.asyncio
    async def test_blocked_slash_command_never_acquires_a_session(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        blocked = sorted(chat_runner._BLOCKED_SLASH_COMMANDS)[0]

        await _drive(state, slot, blocked)

        state.sessions.get_or_create.assert_not_awaited()
        assert any("not available in the dashboard" in m.get("content", "") for m in slot.messages)
        assert slot.messages[-1]["role"] == "done"

    @pytest.mark.asyncio
    async def test_goal_command_is_handled_locally(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()

        with patch.object(chat_runner, "_handle_goal_command", new=AsyncMock()) as handler:
            await _drive(state, slot, "/goal ship the thing")

        handler.assert_awaited_once()
        state.sessions.get_or_create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prompts_get_blocked_path_reports_a_sensitive_path(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()

        with patch.object(
            chat_runner, "_expand_prompt_mention", return_value=("@secret", "blocked")
        ):
            await _drive(state, slot, "/prompts get secret")

        assert any("sensitive path" in m.get("content", "") for m in slot.messages)
        state.sessions.get_or_create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prompts_get_too_large_path_reports_the_limit(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()

        with patch.object(
            chat_runner, "_expand_prompt_mention", return_value=("@big", "too_large")
        ):
            await _drive(state, slot, "/prompts get big")

        assert any("exceeds size limit" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_prompts_get_missing_prompt_reports_not_found(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()

        with patch.object(
            chat_runner, "_expand_prompt_mention", return_value=("@nope", "not_found")
        ):
            await _drive(state, slot, "/prompts get nope")

        assert any("not found" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_prompts_listing_with_none_available(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()

        with patch.object(chat_runner, "_list_aim_prompts", return_value=[]):
            await _drive(state, slot, "/prompts")

        assert any("No prompts found" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_prompts_listing_groups_by_source(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        prompts = [
            {"fullName": "a", "description": "first", "source": "aim"},
            {"fullName": "b", "description": "", "source": "team"},
        ]

        with patch.object(chat_runner, "_list_aim_prompts", return_value=prompts):
            await _drive(state, slot, "/prompts list")

        body = "\n".join(m.get("content", "") for m in slot.messages)
        assert "User Prompts (team)" in body
        assert "`@a` — first" in body

    @pytest.mark.asyncio
    async def test_prompts_listing_survives_a_walk_failure(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()

        with patch.object(chat_runner, "_list_aim_prompts", side_effect=OSError("bad root")):
            await _drive(state, slot, "/prompts")

        assert any("No prompts found" in m.get("content", "") for m in slot.messages)


# ── _run_chat: recovery ladders ───────────────────────────────────────────


class TestRunChatRecoveryLadders:
    @pytest.mark.asyncio
    async def test_stale_recover_requeues_a_continuation(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(client, [_complete(STOP_REASON_STALE_RECOVER)])

        with patch.object(chat_runner, "_start_next_queued_turn", new=AsyncMock(return_value=True)):
            await _drive(state, slot)

        assert slot._stale_recovery_retries == 1
        assert any("Recovering a stalled turn" in err for err in _errors(slot))

    @pytest.mark.asyncio
    async def test_stale_recover_budget_exhaustion_asks_for_a_new_chat(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._stale_recovery_retries = 3
        _set_stream(client, [_complete(STOP_REASON_STALE_RECOVER)])

        await _drive(state, slot)

        assert any("start a new chat" in err for err in _errors(slot))

    @pytest.mark.asyncio
    async def test_nested_stale_recover_surfaces_a_retry_notice(self, tmp_path):
        """depth>0 resets the session but must not re-queue — it still reports."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(client, [_complete(STOP_REASON_STALE_RECOVER)])

        with _quiet_sel():
            await chat_runner._run_chat(state, slot, "hello", _prompt_depth=1)
        await _settle(slot)

        assert any("please retry" in err for err in _errors(slot))
        assert slot._queue == []

    @pytest.mark.asyncio
    async def test_compaction_failure_neither_retries_nor_claims_a_lost_link(self, tmp_path):
        """A PERMANENT compaction failure is terminal: the reason is in the
        "error:" family, so without its own branch it lands in pipe-death
        recovery — a re-queue plus a "Connection lost" card, both false. An
        overflowing conversation fails again identically, so it earns no retry
        (issue #3583)."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(
            client,
            [
                LLMEvent(
                    kind="compaction_status",
                    text="failed",
                    title="context window exceeded",
                ),
                _complete(STOP_REASON_COMPACTION_FAILED),
            ],
        )

        await _drive(state, slot)

        errors = _errors(slot)
        assert not any("Connection lost" in err for err in errors), errors
        assert not any("Session stuck" in err for err in errors), errors
        assert slot._acp_pipe_death_retries == 0
        assert slot._queue == []
        # The failure is still explained — by the compaction notice the status
        # path appended, which is the only message this turn needs.
        assert any(
            "Compaction failed" in m.get("content", "")
            and "context window exceeded" in m.get("content", "")
            for m in slot.messages
        ), slot.messages

    @pytest.mark.asyncio
    async def test_compaction_failure_resets_the_abandoned_backend_session(self, tmp_path):
        """The completion is synthetic — the client stopped reading; the
        backend never sent end_turn — so without a reset the runtime still
        counts the turn as in progress and the NEXT prompt collides with
        "prompt already in progress". The finally must reset (tear down +
        session/load resume) WITHOUT re-queuing anything."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        state.sessions.reset = AsyncMock()
        _set_stream(client, [_complete(STOP_REASON_COMPACTION_FAILED)])

        await _drive(state, slot)

        state.sessions.reset.assert_awaited_once()
        assert slot._queue == []
        assert slot._acp_pipe_death_retries == 0

    @pytest.mark.asyncio
    async def test_a_throttled_compaction_requeues_the_abandoned_message(self, tmp_path):
        """A summarization call that was throttled has nothing wrong with it, so
        the turn it abandoned is worth running again. Dropping the message here
        ended the turn on a backend hiccup the next attempt would clear."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        client.last_compaction_transient = True
        _set_stream(client, [_complete(STOP_REASON_COMPACTION_FAILED)])

        await _drive(state, slot, "do the thing")

        assert slot._compaction_failed_retries == 1
        assert any("Compaction failed — retrying" in err for err in _errors(slot)), _errors(slot)
        # The requeue is proven by the follow-up turn the finally DISPATCHED
        # from it. Asserting on slot._queue cannot see it: that dispatch is the
        # drain, so the entry is already gone by the time the turn returns.
        assert slot.task is not None
        # Still not pipe-death: that budget and its card stay untouched.
        assert slot._acp_pipe_death_retries == 0
        assert not any("Connection lost" in err for err in _errors(slot))

    @pytest.mark.asyncio
    async def test_a_stopped_turn_is_not_revived_by_the_throttle_requeue(self, tmp_path):
        """A requeue lands at queue index 0, so a message the user STOPPED during
        the turn would come back and run first. The stop completed, so _stopping
        and _stop_state have both snapped back to idle — only the monotonic
        generation counter still records that it happened."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        client.last_compaction_transient = True
        # The stop has to land DURING the turn: the generation is snapshotted at
        # turn start, and the branch reads it before the finally block runs.
        slot._stop_generation = 7

        def _stream(*_a, **_kw):
            slot._stop_generation = 8
            return _async_iter([_complete(STOP_REASON_COMPACTION_FAILED)])

        client.stream = MagicMock(side_effect=_stream)

        slot._empty_response_retries = 2
        with _quiet_sel():
            await chat_runner._run_chat(state, slot, "do the thing")
        await _settle(slot)

        assert slot._compaction_failed_retries == 0
        assert not any("retrying" in err for err in _errors(slot)), _errors(slot)

    @pytest.mark.asyncio
    async def test_a_user_followup_outranks_the_throttle_requeue(self, tmp_path):
        """The user typed a correction while the turn was failing. Re-queuing the
        superseded message at index 0 would run it BEFORE their correction, so the
        turn stays abandoned and their message is what runs."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        client.last_compaction_transient = True
        _set_stream(client, [_complete(STOP_REASON_COMPACTION_FAILED)])
        slot.queue_insert(0, "actually, do this instead", kind="user", payload="")

        with _quiet_sel():
            await chat_runner._run_chat(state, slot, "do the thing")
        await _settle(slot)

        assert slot._compaction_failed_retries == 0
        assert not any("retrying" in err for err in _errors(slot)), _errors(slot)

    @pytest.mark.asyncio
    async def test_the_throttle_requeue_is_bounded(self, tmp_path):
        """A throttle still firing after the budget is not clearing inside this
        turn, and every attempt pays for the summarization call again."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        client.last_compaction_transient = True
        slot._compaction_failed_retries = chat_runner._COMPACTION_FAILED_RETRIES
        _set_stream(client, [_complete(STOP_REASON_COMPACTION_FAILED)])

        await _drive(state, slot)

        # Unchanged: the budget was already spent, so this failure bought no
        # further attempt and added no retry notice.
        assert slot._compaction_failed_retries == chat_runner._COMPACTION_FAILED_RETRIES
        assert slot._queue == []
        assert not any("retrying" in err for err in _errors(slot)), _errors(slot)

    @pytest.mark.asyncio
    async def test_a_transient_failure_after_output_is_not_replayed(self, tmp_path):
        """Verbatim replay is only safe before anything landed. Once a tool call
        has been delivered, re-sending the message could repeat its side
        effect — so an emitted turn keeps the give-up behaviour even for a
        reason that would otherwise be retried."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        client.last_compaction_transient = True
        _set_stream(
            client,
            [
                LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial answer"),
                _complete(STOP_REASON_COMPACTION_FAILED),
            ],
        )

        await _drive(state, slot)

        assert slot._compaction_failed_retries == 0
        assert slot._queue == []
        assert not any("retrying" in err for err in _errors(slot)), _errors(slot)

    @pytest.mark.asyncio
    async def test_tool_stall_recovery_names_the_stalled_tool(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(
            client,
            [
                _complete(
                    STOP_REASON_TOOL_STALL,
                    title="Running: tail -f app.log",
                    tool_input="tail -f app.log",
                    text="idle_secs=900 stuck_input",
                )
            ],
        )

        with patch.object(chat_runner, "_start_next_queued_turn", new=AsyncMock(return_value=True)):
            await _drive(state, slot)

        assert slot._tool_stall_retries == 1
        assert any("Tool appeared stalled" in err for err in _errors(slot))

    @pytest.mark.asyncio
    async def test_tool_stall_budget_exhaustion_asks_for_a_new_chat(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._tool_stall_retries = 3
        _set_stream(client, [_complete(STOP_REASON_TOOL_STALL, text="idle_secs=60")])

        await _drive(state, slot)

        assert any("start a new chat" in err for err in _errors(slot))

    @pytest.mark.asyncio
    async def test_nested_tool_stall_surfaces_a_retry_notice(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(client, [_complete(STOP_REASON_TOOL_STALL, text="idle_secs=60")])

        with _quiet_sel():
            await chat_runner._run_chat(state, slot, "hello", _prompt_depth=1)
        await _settle(slot)

        assert any("please retry" in err for err in _errors(slot))
        assert slot._queue == []

    @pytest.mark.asyncio
    async def test_pipe_death_requeues_and_reports_the_exit_code(self, tmp_path):
        state, client = _runner_state(tmp_path)
        client.exit_code = 137
        slot = _slot()
        _set_stream(client, [_complete("error: pipe closed")])

        with patch.object(chat_runner, "_start_next_queued_turn", new=AsyncMock(return_value=True)):
            await _drive(state, slot)

        assert slot._acp_pipe_death_retries == 1
        assert any("exit 137" in err for err in _errors(slot))

    @pytest.mark.asyncio
    async def test_pipe_death_budget_exhaustion_asks_for_a_new_chat(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._acp_pipe_death_retries = 3
        _set_stream(client, [_complete("error: pipe closed")])

        await _drive(state, slot)

        assert any("start a new chat" in err for err in _errors(slot))

    @pytest.mark.asyncio
    async def test_nested_pipe_death_surfaces_a_retry_notice(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(client, [_complete("error: pipe closed")])

        with _quiet_sel():
            await chat_runner._run_chat(state, slot, "hello", _prompt_depth=1)
        await _settle(slot)

        assert any("please retry" in err for err in _errors(slot))
        assert slot._queue == []


# ── _run_chat: auto-approve rungs ─────────────────────────────────────────


class TestRunChatAutoApproveRungs:
    @pytest.fixture(autouse=True)
    def _vouch_for_the_program(self):
        """Let these tests exercise the RUNG, not the host's program layout.

        Each rung now re-judges its own auto-approve by asking whether the
        command's program names still identify the programs they name
        (``name_grant``), which resolves against the real ``PATH`` and reads the
        file behind each name. That made these tests depend on the machine: they
        use ``ls -la``, which passes on Linux because ``ls`` lives in a trusted
        system directory and FAILS on Windows, where there is no such ``ls`` --
        so the rung declined, `approve_tool` was never called, and the assertions
        below broke on one platform only. The thread hop the real check needs also
        outlives these tests' event loop and crashed the xdist worker.

        Stubbing it keeps each test measuring what its name claims -- does the
        rung approve, reject, and render -- while the check itself is covered
        directly in ``test/test_name_grant.py``. It patches the ONE off-loop
        entry point every rung goes through; patching only the event-shaped
        wrapper left two rungs spawning real threads, which is why the Windows
        workers kept crashing after the first attempt at this.
        """

        with patch.object(
            chat_runner, "_name_grant_refusal_off_loop", new=AsyncMock(return_value=None)
        ):
            yield

    @pytest.mark.asyncio
    async def test_a_non_shell_approval_mints_no_shell_witness(self, tmp_path):
        """GPT 5.6 round-22: a non-shell approval must not pin a shell program.

        `extract_bash_command` reads a `command` key out of ANY structured input,
        so a non-shell MCP call carrying `{"command": "gh ..."}` would otherwise
        record a durable identity for `gh` from an approval that was never about
        running `gh`.
        """
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(
            client,
            [
                _permission(
                    title="Looking up the record",
                    tool_input='{"command": "gh repo view"}',
                    tool_kind="other",
                    is_shell=False,
                    tool_name="read_record",
                    mcp_server_name="records:primary",
                ),
                _complete(),
            ],
        )

        def _allow_once_when_registered():
            future = slot._approval_futures.get("req-cov-1")
            if future is not None and not future.done():
                future.set_result("approved")

        state.push_slots_update.side_effect = _allow_once_when_registered
        with patch.object(chat_runner, "pin_human_approval") as pin:
            await _drive(state, slot)

        # The branch really was reached -- otherwise the assertion below would
        # pass for the wrong reason, which is exactly how a guard like this gets
        # a test that cannot fail.
        client.approve_tool.assert_awaited_once_with("req-cov-1")
        pin.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_declined_name_grant_is_audited(self, tmp_path):
        """GPT 5.6 round-19: declining a grant is a security decision, so it is logged.

        Without this the audit trail shows a command arriving at the interactive
        card and never says a name grant was withheld, or why.
        """
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trust_reads = True
        _set_stream(
            client,
            [_permission(title="Running: ls -la", tool_input='{"command": "ls -la"}'), _complete()],
        )
        refusal = name_grant.Refusal(name_grant.SHADOWED, "ls resolves to /writable/ls")
        slot._empty_response_retries = 2
        with (
            patch.object(chat_runner, "sel") as mock_sel,
            patch.object(
                chat_runner, "_name_grant_refusal_off_loop", new=AsyncMock(return_value=refusal)
            ),
            patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0),
        ):
            audit = MagicMock()
            mock_sel.return_value = audit
            await chat_runner._run_chat(state, slot, "hello")
        await _settle(slot)

        declined = [
            c.kwargs
            for c in audit.log_tool_invocation.call_args_list
            if c.kwargs.get("outcome") == "auto_approve_declined"
        ]
        assert declined, audit.log_tool_invocation.call_args_list
        assert declined[0]["metadata"]["code"] == name_grant.SHADOWED
        assert declined[0]["metadata"]["tier"] == "trust_reads"
        # The CODE, never the detail: the detail names resolved paths, and an
        # audit sink is exactly where that becomes a disclosure.
        assert "/writable/ls" not in json.dumps(declined[0], default=str)

    @pytest.mark.asyncio
    async def test_non_shell_trust_matches_cached_server_tool_identity(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        canonical = canonical_non_shell_trust_key("records:primary", "read_record")
        slot._trusted_patterns = {exact_trust_pattern(canonical)}
        _set_stream(
            client,
            [
                _permission(
                    title="Looking up the record",
                    tool_kind="other",
                    is_shell=False,
                    tool_name="read_record",
                    mcp_server_name="records:primary",
                ),
                _complete(),
            ],
        )

        await _drive(state, slot)

        client.approve_tool.assert_awaited_once_with("req-cov-1")

    @pytest.mark.asyncio
    async def test_structured_non_shell_reprompt_never_matches_tool_identity_trust(self, tmp_path):
        """Consumed display input must not erase argument provenance."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        canonical = canonical_non_shell_trust_key("records:primary", "read_record")
        slot._trusted_patterns = {exact_trust_pattern(canonical)}
        permission = _permission(
            title="Looking up the record",
            tool_input="",
            tool_kind="other",
            is_shell=False,
            tool_name="read_record",
            mcp_server_name="records:primary",
            raw_tool_params={"record_id": "sensitive-record"},
        )
        _set_stream(client, [permission, _complete()])

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        client.approve_tool.assert_not_awaited()
        client.reject_tool.assert_awaited_once_with("req-cov-1")
        (card,) = [m for m in slot.messages if m.get("role") == "permission"]
        meta = json.loads(card["cls"])
        assert "full_command" not in meta
        assert "trust_command_key" not in meta
        assert "trust_command_grantable" not in meta
        assert "trust_grantable" not in meta

    @pytest.mark.asyncio
    async def test_reused_non_shell_title_cannot_match_another_tools_trust(self, tmp_path):
        """The title is model prose; only the cached ACP identity may match."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = {
            exact_trust_pattern(canonical_non_shell_trust_key("records:primary", "read_record"))
        }
        _set_stream(
            client,
            [
                _permission(
                    title="Looking up the record",
                    tool_kind="other",
                    is_shell=False,
                    tool_name="delete_record",
                    mcp_server_name="records:primary",
                ),
                _complete(),
            ],
        )

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        client.approve_tool.assert_not_awaited()
        client.reject_tool.assert_awaited_once_with("req-cov-1")

    @pytest.mark.asyncio
    async def test_non_shell_identity_collision_cannot_auto_approve_other_tool(self, tmp_path):
        """One wire-shaped display identity may not imply one trust identity."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        granted = canonical_non_shell_trust_key("github", "repo__delete")
        colliding = canonical_non_shell_trust_key("github__repo", "delete")
        assert granted != colliding
        slot._trusted_patterns = {exact_trust_pattern(granted)}
        _set_stream(
            client,
            [
                _permission(
                    title="Repository action",
                    tool_kind="other",
                    is_shell=False,
                    tool_name="delete",
                    mcp_server_name="github__repo",
                ),
                _complete(),
            ],
        )

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        client.approve_tool.assert_not_awaited()
        client.reject_tool.assert_awaited_once_with("req-cov-1")

    @pytest.mark.asyncio
    async def test_non_shell_identity_cache_miss_stays_allow_once_or_reject(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = {"*"}
        _set_stream(
            client,
            [
                _permission(
                    title="Shared model-authored title",
                    tool_kind="other",
                    is_shell=False,
                ),
                _complete(),
            ],
        )

        def _allow_once_when_registered():
            future = slot._approval_futures.get("req-cov-1")
            if future is not None and not future.done():
                future.set_result("approved")

        state.push_slots_update.side_effect = _allow_once_when_registered
        await _drive(state, slot)

        (card,) = [m for m in slot.messages if m.get("role") == "permission"]
        meta = json.loads(card["cls"])
        assert "full_command" not in meta
        assert "trust_command_key" not in meta
        assert "trust_command_grantable" not in meta
        assert "trust_grantable" not in meta
        client.approve_tool.assert_awaited_once_with("req-cov-1")

    @pytest.mark.asyncio
    async def test_redacted_command_never_matches_existing_trust_pattern(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        displayed = "echo [REDACTED: credential]"
        # This exact pattern can legitimately be granted for a command whose
        # literal argument is the display marker.  It must not cover a different
        # command that only became identical after transport redaction.
        slot._trusted_patterns = {exact_trust_pattern(displayed)}
        permission = _permission(tool_input=json.dumps({"command": displayed}))
        permission.tool_input_redacted = True
        _set_stream(client, [permission, _complete()])

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        client.approve_tool.assert_not_awaited()
        client.reject_tool.assert_awaited_once_with("req-cov-1")

    @pytest.mark.asyncio
    async def test_browser_cli_presence_does_not_skip_shell_approval(self, tmp_path, monkeypatch):
        from kiro_crew.browser_cli import install

        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(
            client,
            [
                _permission(
                    title="Running: playwright-cli snapshot",
                    tool_input=json.dumps({"command": "playwright-cli snapshot"}),
                ),
                _complete(),
            ],
        )
        # Pin the capability as present without consulting or touching the host.
        monkeypatch.setattr(install, "cli_path", lambda: "/agent-writable/playwright-cli")

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        client.approve_tool.assert_not_awaited()
        client.reject_tool.assert_awaited_once_with("req-cov-1")

    @pytest.mark.asyncio
    async def test_browser_cli_explicit_trusted_pattern_still_auto_approves(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = {"playwright-cli*"}
        _set_stream(
            client,
            [
                _permission(
                    title="Running: playwright-cli snapshot",
                    tool_input=json.dumps({"command": "playwright-cli snapshot"}),
                ),
                _complete(),
            ],
        )

        await _drive(state, slot)

        client.approve_tool.assert_awaited_once_with("req-cov-1")

    @pytest.mark.asyncio
    async def test_trusted_pattern_auto_approves_a_matching_command(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = {"ls*"}
        _set_stream(
            client,
            [
                _permission(tool_input=json.dumps({"command": "ls -la"})),
                _complete(),
            ],
        )

        await _drive(state, slot)

        client.approve_tool.assert_awaited_once_with("req-cov-1")
        assert any(
            m.get("role") == "tool" and m.get("content", "").startswith("🔧") for m in slot.messages
        )

    @pytest.mark.asyncio
    async def test_trusted_pattern_rejects_an_invalid_tool_name(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = {"ls*"}
        _set_stream(
            client,
            [
                _permission(tool_input=json.dumps({"command": "ls -la"})),
                _complete(),
            ],
        )

        with patch.object(
            chat_runner, "_validate_tool_name", side_effect=ValueError("name too long")
        ):
            await _drive(state, slot)

        client.reject_tool.assert_awaited_once_with("req-cov-1")
        assert any("invalid: name too long" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_unrecognised_tool_input_skips_pattern_matching(self, tmp_path):
        """Deny-by-default: a non-bash tool_input must not reach fnmatch."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = {"*"}
        slot._trust = True  # falls through to the trust rung instead
        _set_stream(client, [_permission(tool_input="{}"), _complete()])

        with patch.object(chat_runner, "_matches_trusted_pattern") as matcher:
            await _drive(state, slot)

        matcher.assert_not_called()
        client.approve_tool.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_trust_reads_auto_approves_a_read_only_command(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trust_reads = True
        _set_stream(
            client,
            [_permission(tool_input=json.dumps({"command": "cat README.md"})), _complete()],
        )

        await _drive(state, slot)

        client.approve_tool.assert_awaited_once_with("req-cov-1")

    @pytest.mark.asyncio
    async def test_trust_reads_rejects_an_invalid_tool_name(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trust_reads = True
        _set_stream(
            client,
            [_permission(tool_input=json.dumps({"command": "cat README.md"})), _complete()],
        )

        with patch.object(chat_runner, "_validate_tool_name", side_effect=ValueError("bad name")):
            await _drive(state, slot)

        client.reject_tool.assert_awaited_once_with("req-cov-1")


class TestRunChatApprovalWindow:
    @pytest.mark.asyncio
    async def test_non_shell_card_separates_display_from_collision_free_trust_key(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(
            client,
            [
                _permission(
                    title="Repository action",
                    tool_input="",
                    tool_kind="other",
                    is_shell=False,
                    tool_name="repo__delete",
                    mcp_server_name="github",
                ),
                _complete(),
            ],
        )

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        (card,) = [m for m in slot.messages if m.get("role") == "permission"]
        meta = json.loads(card["cls"])
        # Preserve the ACP-compatible UI label while keeping authority in the
        # injective server-only field.
        assert meta["full_command"] == "mcp__github__repo__delete"
        assert meta["trust_command_key"] == canonical_non_shell_trust_key("github", "repo__delete")
        assert meta["trust_command_key"] != canonical_non_shell_trust_key("github__repo", "delete")
        assert meta["trust_command_grantable"] == "1"
        client.reject_tool.assert_awaited_once_with("req-cov-1")

    @pytest.mark.asyncio
    async def test_redacted_command_still_allows_one_time_approval(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        permission = _permission(tool_input=json.dumps({"command": "echo [REDACTED: credential]"}))
        permission.tool_input_redacted = True
        _set_stream(client, [permission, _complete()])

        def _approve_when_registered():
            future = slot._approval_futures.get("req-cov-1")
            if future is not None and not future.done():
                future.set_result("approved")

        state.push_slots_update.side_effect = _approve_when_registered

        await _drive(state, slot)

        client.approve_tool.assert_awaited_once_with("req-cov-1")
        client.reject_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_redacted_command_is_allow_once_only(self, tmp_path):
        """Different hidden commands collapse to the same display marker.

        The transport provenance therefore suppresses every durable command
        scope while leaving the ordinary approval card live.
        """
        state, client = _runner_state(tmp_path)
        slot = _slot()
        permission = _permission(tool_input=json.dumps({"command": "echo [REDACTED: credential]"}))
        permission.tool_input_redacted = True
        _set_stream(client, [permission, _complete()])

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        (card,) = [m for m in slot.messages if m.get("role") == "permission"]
        meta = json.loads(card["cls"])
        assert meta["tool_input"] == '{"command": "echo [REDACTED: credential]"}'
        assert "full_command" not in meta
        assert "trust_command_key" not in meta
        assert "base_command" not in meta
        assert "trust_command_grantable" not in meta
        assert "trust_base_grantable" not in meta
        assert "trust_grantable" not in meta
        client.reject_tool.assert_awaited_once_with("req-cov-1")

    @pytest.mark.asyncio
    async def test_no_remaining_budget_declines_without_waiting(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(client, [_permission(), _complete()])

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        client.reject_tool.assert_awaited_once_with("req-cov-1")
        assert _errors(slot)

    @pytest.mark.asyncio
    async def test_unanswered_prompt_times_out_and_names_the_cause(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(client, [_permission(), _complete()])

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.01):
            await _drive(state, slot)

        client.reject_tool.assert_awaited_once_with("req-cov-1")
        assert slot._approval_futures == {}

    @pytest.mark.asyncio
    async def test_unattended_slot_is_told_to_ask_instead_of_retrying(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._app = "worker-app"
        slot._human_seen = False
        assert slot.unattended is True
        _set_stream(client, [_permission(), _complete()])

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.01):
            await _drive(state, slot)

        assert any(
            "running unattended" in m.get("content", "")
            for m in slot.messages
            if m.get("role") == "assistant"
        )


# ── _run_chat: plan gate ──────────────────────────────────────────────────


class TestRunChatPlanGate:
    @pytest.mark.asyncio
    async def test_valid_plan_arms_the_option_gate(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot.mode = "orchestrator"
        plan = (
            "📋 Plan for: ship it\n"
            "Stage 1: build\n"
            "Stage 2: verify\n"
            "[OPTION: Go | Go All | Cancel]"
        )
        _set_stream(client, [LLMEvent(kind=EVENT_TEXT_CHUNK, text=plan), _complete()])

        await _drive(state, slot)

        assert slot._stage_titles

    @pytest.mark.asyncio
    async def test_invalid_plan_is_stripped_when_the_rephrase_fails(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot.mode = "orchestrator"
        _set_stream(
            client,
            [LLMEvent(kind=EVENT_TEXT_CHUNK, text="📋 Plan for: x\nno stages here"), _complete()],
        )

        with patch.object(chat_runner, "_rephrase_plan_lite", new=AsyncMock(return_value="")):
            await _drive(state, slot)

        assert not slot._stage_titles

    @pytest.mark.asyncio
    async def test_plan_like_text_is_reformatted_by_the_rephrase_pass(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot.mode = "orchestrator"
        good = (
            "📋 Plan for: ship it\n"
            "Stage 1: build\n"
            "Stage 2: verify\n"
            "[OPTION: Go | Go All | Cancel]"
        )
        _set_stream(
            client,
            [
                LLMEvent(kind=EVENT_TEXT_CHUNK, text="Step 1: build\nStep 2: verify\n"),
                _complete(),
            ],
        )

        with (
            patch.object(chat_runner, "looks_like_plan", return_value=True),
            patch.object(chat_runner, "_rephrase_plan_lite", new=AsyncMock(return_value=good)),
        ):
            await _drive(state, slot)

        assert slot._stage_titles

    @pytest.mark.asyncio
    async def test_stage_execution_turn_never_arms_a_plan(self, tmp_path):
        """A stage turn whose output looks like a plan must not re-arm the gate."""
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot.mode = "orchestrator"
        slot._in_stage_execution = True
        plan = (
            "📋 Plan for: ship it\n"
            "Stage 1: build\n"
            "Stage 2: verify\n"
            "[OPTION: Go | Go All | Cancel]"
        )
        _set_stream(client, [LLMEvent(kind=EVENT_TEXT_CHUNK, text=plan), _complete()])

        await _drive(state, slot)

        assert not slot._stage_titles


def _bindings(*, kiro_agent, resolved_alias, requested_resolved):
    """A minimal ResolvedBindings stand-in for the app-agent dispatch guard.

    Only the fields ``_run_chat`` reads off the resolve result are populated;
    ``model`` is a real ``str`` so ``normalize_agent_model`` stays happy.
    """
    return SimpleNamespace(
        kiro_agent=kiro_agent,
        resolved_alias=resolved_alias,
        memory_store_name="default",
        model="",
        requested_resolved=requested_resolved,
    )


class TestAppAgentDispatchGuard:
    """SELF-HEAL + FAIL-LOUD for an app-owned slot whose agent read cold.

    An app's agents live only in ``~/.kiro/agents/<app>--<agent>.json`` and are
    never in ``config.agents``; resolve_agent_bindings can honor them only via
    the materialized-agent snapshot, which is cold on the event loop until the
    off-loop boot/registration warm lands. A cold read silently falls back to
    the default agent (``requested_resolved=False``).
    """

    @pytest.mark.asyncio
    async def test_cold_app_slot_self_heals_to_the_app_agent(self, tmp_path):
        state, client = _runner_state(tmp_path)
        _set_stream(client, [LLMEvent(kind=EVENT_TEXT_CHUNK, text="hi"), _complete()])
        slot = _slot()
        slot._app = "myapp"
        slot.agent = "my-app-agent"

        cold = _bindings(kiro_agent="kirocrew", resolved_alias="default", requested_resolved=False)
        warm = _bindings(
            kiro_agent="my-app-agent",
            resolved_alias="my-app-agent",
            requested_resolved=True,
        )
        refresh = MagicMock()
        with (
            patch.object(
                chat_runner, "resolve_agent_bindings", side_effect=[cold, warm]
            ) as resolve,
            patch.object(chat_runner, "refresh_materialized_agents", refresh),
            patch.object(chat_runner, "subprocess_executor", MagicMock(return_value=None)),
            patch.object(chat_runner, "warm_project_agent_names", new=AsyncMock()),
        ):
            await _drive(state, slot)

        # Warmed the snapshot off the loop and re-resolved exactly once.
        refresh.assert_called_once()
        assert resolve.call_count == 2
        # The healed agent — not the default — was dispatched, and no error card.
        state.sessions.get_or_create.assert_awaited()
        assert state.sessions.get_or_create.await_args.kwargs["agent"] == "my-app-agent"
        assert _errors(slot) == []

    @pytest.mark.asyncio
    async def test_app_slot_recovers_from_source_after_rescan_miss(self, tmp_path):
        # The snapshot RESCAN misses (spec never materialized though source is
        # intact), so the self-heal escalates: re-register this app's agents FROM
        # SOURCE (refresh_app_agents) then re-resolve — which now succeeds.
        state, client = _runner_state(tmp_path)
        _set_stream(client, [LLMEvent(kind=EVENT_TEXT_CHUNK, text="hi"), _complete()])
        slot = _slot()
        slot._app = "myapp"
        slot.agent = "my-app-agent"

        cold = _bindings(kiro_agent="kirocrew", resolved_alias="default", requested_resolved=False)
        warm = _bindings(
            kiro_agent="my-app-agent",
            resolved_alias="my-app-agent",
            requested_resolved=True,
        )
        refresh = MagicMock()
        reregister = MagicMock(return_value=["my-app-agent"])
        with (
            patch.object(
                chat_runner, "resolve_agent_bindings", side_effect=[cold, cold, warm]
            ) as resolve,
            patch.object(chat_runner, "refresh_materialized_agents", refresh),
            patch("kiro_crew.apps.bridges.register_app", reregister),
            patch("kiro_crew.apps.manager.is_app_enabled", MagicMock(return_value=True)),
            patch.object(chat_runner, "subprocess_executor", MagicMock(return_value=None)),
            patch.object(chat_runner, "warm_project_agent_names", new=AsyncMock()),
        ):
            await _drive(state, slot)

        # Rescan missed, so the from-source re-registration ran for THIS app,
        # and the resolver was consulted a third time.
        refresh.assert_called_once()
        reregister.assert_called_once_with("myapp")
        assert resolve.call_count == 3
        # The healed agent — not the default — was dispatched, and no error card.
        state.sessions.get_or_create.assert_awaited()
        assert state.sessions.get_or_create.await_args.kwargs["agent"] == "my-app-agent"
        assert _errors(slot) == []

    @pytest.mark.asyncio
    async def test_still_cold_app_slot_fails_loud_and_never_runs_default(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._app = "myapp"
        slot.agent = "my-app-agent"

        cold = _bindings(kiro_agent="kirocrew", resolved_alias="default", requested_resolved=False)
        refresh = MagicMock()
        reregister = MagicMock(return_value=[])
        with (
            patch.object(
                chat_runner, "resolve_agent_bindings", side_effect=[cold, cold, cold]
            ) as resolve,
            patch.object(chat_runner, "refresh_materialized_agents", refresh),
            patch("kiro_crew.apps.bridges.register_app", reregister),
            patch("kiro_crew.apps.manager.is_app_enabled", MagicMock(return_value=True)),
            patch.object(chat_runner, "subprocess_executor", MagicMock(return_value=None)),
            patch.object(chat_runner, "warm_project_agent_names", new=AsyncMock()),
        ):
            await _drive(state, slot)

        # Self-heal was fully attempted (rescan + re-register-from-source + three
        # re-resolves) but the agent stayed cold.
        refresh.assert_called_once()
        reregister.assert_called_once_with("myapp")
        assert resolve.call_count == 3
        # Fail-loud: the default agent is NEVER dispatched...
        state.sessions.get_or_create.assert_not_awaited()
        # ...and a clear card names the requested agent.
        errors = _errors(slot)
        assert any("my-app-agent" in e and "isn't loaded yet" in e for e in errors)

    @pytest.mark.asyncio
    async def test_disabled_app_slot_skips_source_recovery_and_fails_loud(self, tmp_path):
        # A DISABLED app whose slot still gets a turn must NOT have its
        # deregistered agent re-materialized: the from-source recovery is gated on
        # is_app_enabled (under the app lifecycle lock), so refresh_app_agents is
        # never called and the turn fails loud instead of reactivating a disabled
        # app's agent.
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._app = "myapp"
        slot.agent = "my-app-agent"

        cold = _bindings(kiro_agent="kirocrew", resolved_alias="default", requested_resolved=False)
        reregister = MagicMock(return_value=["my-app-agent"])
        with (
            patch.object(chat_runner, "resolve_agent_bindings", side_effect=[cold, cold, cold]),
            patch.object(chat_runner, "refresh_materialized_agents", MagicMock()),
            patch("kiro_crew.apps.bridges.register_app", reregister),
            patch("kiro_crew.apps.manager.is_app_enabled", MagicMock(return_value=False)),
            patch.object(chat_runner, "subprocess_executor", MagicMock(return_value=None)),
            patch.object(chat_runner, "warm_project_agent_names", new=AsyncMock()),
        ):
            await _drive(state, slot)

        # Disabled -> recovery skipped: refresh_app_agents never ran...
        reregister.assert_not_called()
        # ...the default agent was NOT dispatched, and the turn failed loud.
        state.sessions.get_or_create.assert_not_awaited()
        errors = _errors(slot)
        assert any("my-app-agent" in e and "isn't loaded yet" in e for e in errors)

    @pytest.mark.asyncio
    async def test_recovery_awaits_register_before_releasing_lock_on_cancel(self, tmp_path):
        # register_app runs in a non-cancellable executor thread. If the recovery
        # coroutine is cancelled mid-registration it must WAIT for that thread to
        # finish before the lifecycle lock releases — otherwise a concurrent
        # disable could deregister and the still-running thread republish a
        # now-disabled agent. Prove the cancelled task stays blocked until
        # register_app completes.
        import kiro_crew.dashboard.chat_runner as cr

        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow_register(_name):
            started.set()
            release.wait(5)
            finished.set()

        slot = _slot()
        slot._app = "myapp"
        slot.agent = "my-app-agent"
        warm = _bindings(
            kiro_agent="my-app-agent",
            resolved_alias="my-app-agent",
            requested_resolved=True,
        )
        with (
            patch.object(cr, "resolve_agent_bindings", return_value=warm),
            patch.object(cr, "subprocess_executor", MagicMock(return_value=None)),
            patch("kiro_crew.apps.manager.is_app_enabled", MagicMock(return_value=True)),
            patch("kiro_crew.apps.bridges.register_app", slow_register),
        ):
            task = asyncio.create_task(
                cr._recover_app_agent_binding(MagicMock(), slot, project=None)
            )
            await asyncio.to_thread(started.wait, 5)  # register_app is now running
            task.cancel()
            # Shielded: the cancelled task must NOT complete while register_app is
            # still running — it is blocked awaiting the executor future.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), 0.2)
            release.set()  # let register_app finish
            with pytest.raises(asyncio.CancelledError):
                await task

        assert finished.is_set()  # register_app ran to completion before propagating

    @pytest.mark.asyncio
    async def test_eager_spawn_bails_for_unresolved_app_slot(self, tmp_path):
        # An app slot that stays cold after the eager self-heal must NOT register
        # a speculative session: resolve_agent_bindings returns the DEFAULT agent
        # on a cold miss, so a registered session would bind the wrong agent and
        # the first real turn would reuse it, bypassing the _run_chat fail-loud
        # guard. The eager path bails and leaves it to the first real turn.
        state, slot = _state(tmp_path), _slot()
        slot._app = "myapp"
        slot.agent = "my-app-agent"
        state.get_slot = MagicMock(return_value=slot)
        # Never reached when the bail is present; set so a REMOVED bail would fail
        # on the assertion below (get_or_create awaited) rather than on unpacking.
        state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
        cold = _bindings(kiro_agent="kirocrew", resolved_alias="default", requested_resolved=False)
        with (
            patch.object(chat_runner.asyncio, "sleep", new=AsyncMock()),
            patch.object(chat_runner, "_consume_pending_reset", new=AsyncMock()),
            patch.object(chat_runner, "resolve_agent_bindings", side_effect=[cold, cold, cold]),
            patch.object(chat_runner, "refresh_materialized_agents", MagicMock()),
            patch("kiro_crew.apps.bridges.register_app", MagicMock(return_value=[])),
            patch.object(chat_runner, "subprocess_executor", MagicMock(return_value=None)),
        ):
            await chat_runner._eager_spawn(state, slot)

        state.sessions.get_or_create.assert_not_awaited()


class TestPromptSubmitTranscriptRead:
    """The re-injection probe's transcript read must not run on the loop.

    ``_run_chat`` compares the in-memory message count against the on-disk one to
    decide whether a reset session needs its history re-injected. That disk count
    comes from ``read_messages``, which parses the whole transcript -- 100-300 ms
    on a large store, on the hottest path there is (issue #7408).
    """

    @pytest.mark.asyncio
    async def test_disk_count_read_runs_off_the_loop_thread(self, tmp_path, monkeypatch):
        state, client = _runner_state(tmp_path)
        _set_stream(client, [_complete()])
        slot = _slot()
        # A non-empty in-memory window is what arms the probe; without it the
        # branch holding the read is skipped and the test would pass vacuously.
        slot.append("user", "an earlier turn", "msg msg-u")
        seen: list[int] = []
        real_read = ConversationLog.read_messages

        def recording(self, key):  # noqa: ANN001 -- test double
            seen.append(threading.get_ident())
            return real_read(self, key)

        monkeypatch.setattr(ConversationLog, "read_messages", recording)

        await _drive(state, slot, "hello")

        assert seen, (
            "no transcript read happened on the prompt-submit path -- this test "
            "no longer exercises the re-injection probe and would pass vacuously"
        )
        assert threading.get_ident() not in seen, (
            "the re-injection probe read the transcript on the event-loop thread; "
            "it must go through asyncio.to_thread"
        )
