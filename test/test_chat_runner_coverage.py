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
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    STOP_REASON_STALE_RECOVER,
    STOP_REASON_TOOL_STALL,
)
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.providers.base import LLMEvent
from kiro_crew.security import oauth_url_contains_credential

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
) -> LLMEvent:
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title=title,
        tool_kind=tool_kind,
        tool_input=tool_input,
        request_id=request_id,
        is_shell=True,
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
        with patch.object(chat_runner, "infer_use_case", return_value="cron"), patch.object(
            chat_runner, "get_recorder", return_value=recorder
        ):
            chat_runner._emit_turn_metric(0, "end_turn", "cron:job", elapsed_ms=12)

        _, kwargs = recorder.histogram.call_args
        assert kwargs["attrs"]["session_source"] == "cron"
        assert kwargs["attrs"]["outcome"] == "ok"

    def test_use_case_failure_does_not_block_the_emit(self):
        """A broken source lookup must still leave the histogram emitted."""
        recorder = MagicMock()
        with patch.object(chat_runner, "infer_use_case", side_effect=RuntimeError("boom")), patch.object(
            chat_runner, "get_recorder", return_value=recorder
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
        assert (
            chat_runner._pre_tool_block_reason(results) == "blocked by a PreToolUse policy hook"
        )


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
        with patch.object(
            chat_runner, "kirocrew_managed_names", return_value={"github", "handmade"}
        ), patch.object(
            chat_runner,
            "get_visible_providers",
            return_value=[{"slug": "github"}, {"slug": "slack"}],
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
        link = MagicMock(channel_id="123", thread_id=None, channel_type="telegram")

        with patch.object(chat_runner, "_resolve_mirror_target", return_value=(link, transport)):
            await chat_runner._deliver_cross_surface_reply(state, "dashboard:x", "ab " * 20)

        assert transport.send_message.await_count > 1

    @pytest.mark.asyncio
    async def test_transport_failure_never_disrupts_the_turn(self, tmp_path):
        state = _state(tmp_path)
        transport = AsyncMock()
        transport.capabilities.max_message_chars = 4096
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

        assert "AKIAIOSFODNN7EXAMPLE" not in slot.messages[-1]["content"]

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

        with patch.object(
            chat_runner, "register_widgets_off_loop", new=AsyncMock()
        ) as widgets, patch.object(
            chat_runner, "register_images_off_loop", new=AsyncMock()
        ) as images:
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

        with patch.object(
            chat_runner, "_find_prompt", return_value={"path": "/home/u/.aws/credentials"}
        ), patch.object(chat_runner, "is_sensitive_path", return_value=True):
            assert chat_runner._expand_prompt_mention("@sop", state, slot) == ("@sop", "blocked")

    def test_oversized_prompt_is_refused(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        big = tmp_path / "big.md"
        big.write_text("y" * (chat_runner.MAX_PROMPT_BYTES + 1), newline="\n")

        with patch.object(
            chat_runner, "_find_prompt", return_value={"path": str(big), "fullName": "big"}
        ), patch.object(chat_runner, "is_sensitive_path", return_value=False):
            assert chat_runner._expand_prompt_mention("@big", state, slot) == ("@big", "too_large")

    def test_unreadable_prompt_is_not_found(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        missing = tmp_path / "gone.md"

        with patch.object(
            chat_runner, "_find_prompt", return_value={"path": str(missing), "fullName": "gone"}
        ), patch.object(chat_runner, "is_sensitive_path", return_value=False):
            assert chat_runner._expand_prompt_mention("@gone", state, slot) == (
                "@gone",
                "not_found",
            )

    def test_resolved_prompt_carries_user_text_and_a_chip(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        sop = tmp_path / "sop.md"
        sop.write_text("Do the thing\n", newline="\n")

        with patch.object(
            chat_runner, "_find_prompt", return_value={"path": str(sop), "fullName": "sop"}
        ), patch.object(chat_runner, "is_sensitive_path", return_value=False):
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

        await chat_runner._consume_pending_reset(state, slot)

        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_successful_reset_clears_the_flag(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_reset_history_key = "dashboard:chat-cov-1"

        await chat_runner._consume_pending_reset(state, slot)

        state.sessions.reset.assert_awaited_once_with("dashboard:chat-cov-1")
        assert slot._pending_reset_history_key is None

    @pytest.mark.asyncio
    async def test_a_key_queued_during_the_await_is_not_clobbered(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_reset_history_key = "old-key"

        async def _reset(_key):
            slot._pending_reset_history_key = "newer-key"

        state.sessions.reset = AsyncMock(side_effect=_reset)

        await chat_runner._consume_pending_reset(state, slot)

        assert slot._pending_reset_history_key == "newer-key"

    @pytest.mark.asyncio
    async def test_reset_failure_leaves_the_flag_armed(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        slot._pending_reset_history_key = "old-key"
        state.sessions.reset = AsyncMock(side_effect=RuntimeError("no session"))

        await chat_runner._consume_pending_reset(state, slot)

        assert slot._pending_reset_history_key == "old-key"


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

        with patch.object(
            chat_runner.KiroCrewConfig, "load", side_effect=RuntimeError("bad toml")
        ):
            assert chat_runner.schedule_eager_spawn(state, slot) is None

    @pytest.mark.asyncio
    async def test_a_newer_signal_cancels_the_pending_task(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
        cfg = MagicMock()
        cfg.session.eager_spawn = True

        with patch.object(chat_runner.KiroCrewConfig, "load", return_value=cfg), patch.object(
            chat_runner, "_eager_spawn", new=AsyncMock()
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

        with patch.object(
            chat_runner.KiroCrewConfig, "load", side_effect=RuntimeError("bad toml")
        ), patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()) as spawn, patch.object(
            chat_runner, "_run_chat", return_value=MagicMock()
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

        with patch.object(
            chat_runner, "spawn_guarded_turn", return_value=MagicMock()
        ), patch.object(chat_runner, "_run_chat", return_value=MagicMock()):
            assert await chat_runner._start_next_queued_turn(state, slot) is True

        assert any("Session reset" in err for err in _errors(slot))
        assert slot._stopping is False


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

        with patch.object(
            chat_runner, "spawn_guarded_turn", return_value=asyncio.ensure_future(_boom())
        ), patch.object(chat_runner, "_run_chat", return_value=MagicMock()):
            await chat_runner._run_pending_synthesis(state, slot)

        assert slot._pending_synthesis is False
        assert slot._synthesis_inflight is False


class TestFinishQueueCycle:
    @pytest.mark.asyncio
    async def test_eligible_synthesis_is_started_instead_of_going_idle(self, tmp_path):
        state, slot = _state(tmp_path), _slot()
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
        out = chat_runner._native_done_result(["z" * (chat_runner.NATIVE_SUBAGENT_DONE_RESULT_CAP + 5)])

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
        assert chat_runner._matches_trusted_pattern("Running: echo $(whoami)", {"echo*"}) is None

    def test_every_segment_must_match_for_a_chained_command(self):
        assert chat_runner._matches_trusted_pattern("Running: ls | rm -rf x", {"ls*"}) is None

    def test_all_matching_segments_return_joined_patterns(self):
        matched = chat_runner._matches_trusted_pattern("Running: ls | wc -l", {"ls*", "wc*"})

        assert matched is not None
        assert matched.count(",") == 1

    def test_redirect_forms_are_not_treated_as_separators(self):
        assert chat_runner._matches_trusted_pattern("Running: ls 2>&1", {"ls*"}) is not None

    def test_an_escaped_quote_does_not_hide_a_real_separator(self):
        """A closing quote followed by `\\'` leaves quoted context, so the `;`
        after it is a separator the shell acts on.

        Reading that `\\'` as an OPENING quote makes the remainder look quoted, the
        separator gets masked, the line collapses to one segment, and an appended
        command inherits whatever the first segment was allowed to do. Verified
        against a real shell: `echo 'foo'\\'; cmd` runs `cmd`.
        """
        command = "Running: echo 'foo'\\'; whoami"
        _, segments = chat_runner._split_command_segments(command) or ("", [])

        assert len(segments) == 2, segments
        assert chat_runner._matches_trusted_pattern(command, {"echo*"}) is None

    def test_an_escaped_double_quote_keeps_the_quote_open(self):
        """Inside double quotes `\\"` is an escaped literal, so the quote stays
        OPEN and the `;` after it really is quoted. One segment is the correct
        reading: the line is an unterminated quote, which a shell refuses to run
        at all rather than executing a second command, so there is nothing here
        for segmentation to protect against."""
        command = 'Running: echo "foo\\"; whoami'
        _, segments = chat_runner._split_command_segments(command) or ("", [])

        assert len(segments) == 1, segments

    def test_an_escaped_separator_outside_quotes_still_segments(self):
        """`\\;` is an escaped literal to the shell, not a separator -- but the
        allowlist must not approve the tail either way, so segmentation stays
        fail-closed rather than trying to model every escape."""
        assert chat_runner._matches_trusted_pattern("Running: ls \\; whoami", {"ls*"}) is None

    def test_a_backslash_inside_single_quotes_stays_literal(self):
        """The shell does not honor escapes inside single quotes, so a trailing
        backslash there must not swallow the closing quote."""
        masked, restore = chat_runner._mask_quoted_separators("echo 'a|b\\' && wc -l")

        assert list(restore.values()) == ["|"]
        assert masked.endswith("&& wc -l")

    def test_base_command_extraction_dedups_across_segments(self):
        assert chat_runner._extract_base_command("Running: cat a | wc -l | cat b") == "cat,wc"

    def test_full_command_strips_the_display_prefix(self):
        assert chat_runner._extract_full_command("Running: ls -la") == "ls -la"


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
    @pytest.mark.parametrize("model,provider", [("", "kiro"), ("auto", "kiro"), ("x", "claude_code")])
    def test_unpinnable_combinations_are_never_withheld(self, model, provider):
        assert chat_runner._pinned_model_withheld(MagicMock(), model, provider) is False

    def test_claude_backend_is_exempt(self):
        client = MagicMock()
        client.is_claude_backend = True

        assert chat_runner._pinned_model_withheld(client, "claude-opus-5", "kiro") is False

    def test_provider_without_an_advertiser_leaves_the_pin_alone(self):
        client = MagicMock()
        client.is_claude_backend = False
        client.available_models = "not-callable"

        assert chat_runner._pinned_model_withheld(client, "claude-opus-5", "kiro") is False

    def test_advertiser_failure_fails_open(self):
        client = MagicMock()
        client.is_claude_backend = False
        client.available_models = MagicMock(side_effect=RuntimeError("no session"))

        assert chat_runner._pinned_model_withheld(client, "claude-opus-5", "kiro") is False

    def test_unadvertised_pin_is_reported_as_withheld(self):
        client = MagicMock()
        client.is_claude_backend = False
        client.available_models = MagicMock(return_value=[{"modelId": "claude-sonnet-4.6"}])

        with patch.object(chat_runner, "advertised_model_ids", return_value={"claude-sonnet-4.6"}):
            assert chat_runner._pinned_model_withheld(client, "claude-opus-5", "kiro") is True


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
            m.get("role") == "tool" and m.get("content", "").startswith("🔧")
            for m in slot.messages
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

        with patch.object(chat_runner, "looks_like_plan", return_value=True), patch.object(
            chat_runner, "_rephrase_plan_lite", new=AsyncMock(return_value=good)
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
