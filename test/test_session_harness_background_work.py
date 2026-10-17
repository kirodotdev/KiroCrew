"""The session watchdog must not recycle a session whose harness is still working.

A free semaphore only proves Kiro Crew's OWN prompt returned. Claude Code, behind
claude-agent-acp, runs a backgrounded Bash command or a Workflow in the session's
own process tree after the prompt has answered ``end_turn``. The RSS recycle and
the idle sweep read the free semaphore, the Kiro Crew sub-agent probe and the
injection counter -- none of which can see that work -- so a session whose tree
crossed ``session.watchdog_rss_max_mb`` while its workflow ran was reset on the
next tick, killing the workflow it had promised to report on.

What a live capture off claude-agent-acp 0.84.0 (driven over stdio, no AIR
capabilities declared, the shape Kiro Crew's client sends) establishes:

1. The launch is reported on the launching call's PostToolUse ``tool_call_update``
   as ``_meta.claudeCode.toolResponse``: a Bash command carries
   ``backgroundTaskId``, a Workflow carries ``status: "async_launched"`` with its
   ``taskId``, ``taskType: "local_workflow"`` and ``workflowName``.
2. Nothing else arrives while the work runs: a 45 s background ``sleep`` sent one
   ``session_info_update`` (a title) and a 66 s Workflow sent nothing at all
   between ``end_turn`` and the model waking up to report the result.
3. So no end is observable, and the hold is bounded by time.

These pin the parse against those captured frames, the record on the transport
that serves the stamping harness, its exposure through the provider, and that
both recycle paths keep a session inside the hold while an older launch still
recycles — with the plain memory-limit notice, since letting a recycle through
past the hold presumes the launched work finished. The record dies with the
process: ``_reset_state`` clears it, so a respawn on the same client object
does not inherit the dead tree's hold. The hold is not absolute for the RSS path:
each launch refreshes its clock, so a tree past the hard ceiling
(``rss_max_mb * HARNESS_BACKGROUND_WORK_HARD_CEILING_FACTOR``) recycles even
inside the hold, or a session that keeps launching work would never be bounded.
"""

from __future__ import annotations

import inspect
import time
import unicodedata
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import session
from kiro_crew.acp._dispatch import (
    BACKGROUND_LAUNCH_LABELS_MAX,
    BackgroundLaunchRecord,
    parse_background_launch,
    unrecognised_background_launch,
)
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, JsonRpcMessage
from kiro_crew.session_cleanup import (
    HARNESS_BACKGROUND_WORK_HARD_CEILING_FACTOR,
    HARNESS_BACKGROUND_WORK_HOLD_SECS,
)

# --------------------------------------------------------------------------- #
# Captured frames (claude-agent-acp 0.84.0; ids and paths shortened)
# --------------------------------------------------------------------------- #

CAPTURED_BASH_LAUNCH: dict[str, Any] = {
    "_meta": {
        "claudeCode": {
            "toolResponse": {
                "stdout": "",
                "stderr": "",
                "interrupted": False,
                "isImage": False,
                "noOutputExpected": False,
                "backgroundTaskId": "b15e5v3i3",
            },
            "toolName": "Bash",
        }
    },
    "toolCallId": "toolu_01JGVHtbb5fAC5EVZHDN3zMW",
    "sessionUpdate": "tool_call_update",
}

CAPTURED_WORKFLOW_LAUNCH: dict[str, Any] = {
    "_meta": {
        "claudeCode": {
            "toolResponse": {
                "status": "async_launched",
                "taskId": "wuda97r0j",
                "taskType": "local_workflow",
                "workflowName": "sleep-echo",
                "runId": "wf_d10b341d-df3",
                "summary": "Single agent runs sleep 60 && echo WF_DONE",
                "transcriptDir": "/home/u/.claude/projects/p/s/subagents/workflows/wf_d10b341d-df3",
                "scriptPath": "/home/u/.claude/projects/p/s/workflows/scripts/sleep-echo.js",
            },
            "toolName": "Workflow",
        }
    },
    "toolCallId": "toolu_01QmKU4L8QqA6dzbfNcDVKzu",
    "sessionUpdate": "tool_call_update",
}

# The same call's terminal frame: the prose result. Not the marker, and must not
# be read as one -- the parse is structural.
CAPTURED_BASH_TERMINAL: dict[str, Any] = {
    "_meta": {"claudeCode": {"toolName": "Bash"}},
    "toolCallId": "toolu_01JGVHtbb5fAC5EVZHDN3zMW",
    "sessionUpdate": "tool_call_update",
    "status": "completed",
    "rawOutput": "Command running in background with ID: b15e5v3i3. Output is being "
    "written to: /tmp/x/tasks/b15e5v3i3.output. You will be notified when it completes.",
}

# An ordinary foreground command's PostToolUse frame.
FOREGROUND_BASH: dict[str, Any] = {
    "_meta": {
        "claudeCode": {
            "toolResponse": {"stdout": "hi\n", "stderr": "", "interrupted": False},
            "toolName": "Bash",
        }
    },
    "toolCallId": "toolu_fg",
    "sessionUpdate": "tool_call_update",
}

# An asynchronous sub-agent: the adapter holds the prompt open until it settles,
# so the turn is still in flight and the semaphore already covers it.
ASYNC_AGENT_LAUNCH: dict[str, Any] = {
    "_meta": {
        "claudeCode": {
            "toolResponse": {"status": "async_launched", "taskId": "a1", "isAsync": True},
            "toolName": "Agent",
        }
    },
    "toolCallId": "toolu_agent",
    "sessionUpdate": "tool_call_update",
}


def _update_msg(update: dict[str, Any]) -> JsonRpcMessage:
    return JsonRpcMessage(method="session/update", params={"sessionId": "s-1", "update": update})


# --------------------------------------------------------------------------- #
# The parse
# --------------------------------------------------------------------------- #


class TestParseBackgroundLaunch:
    def test_a_backgrounded_bash_command_is_a_launch(self) -> None:
        assert parse_background_launch(CAPTURED_BASH_LAUNCH) == "background command"

    def test_a_workflow_is_a_launch_named_by_its_workflow(self) -> None:
        assert parse_background_launch(CAPTURED_WORKFLOW_LAUNCH) == 'workflow "sleep-echo"'

    @pytest.mark.parametrize(
        "update",
        [CAPTURED_BASH_TERMINAL, FOREGROUND_BASH, ASYNC_AGENT_LAUNCH],
        ids=["prose-result", "foreground", "held-async-agent"],
    )
    def test_everything_else_is_not(self, update: dict[str, Any]) -> None:
        assert parse_background_launch(update) is None

    def test_only_a_tool_call_update_carries_it(self) -> None:
        frame = {**CAPTURED_BASH_LAUNCH, "sessionUpdate": "tool_call"}
        assert parse_background_launch(frame) is None

    def test_a_blank_task_id_is_not_a_launch(self) -> None:
        blank = {
            **CAPTURED_BASH_LAUNCH,
            "_meta": {"claudeCode": {"toolResponse": {"backgroundTaskId": "  "}}},
        }
        assert parse_background_launch(blank) is None

    @pytest.mark.parametrize(
        "meta", [None, "x", {"claudeCode": "x"}, {"claudeCode": {"toolResponse": []}}]
    )
    def test_malformed_meta_is_not_a_launch(self, meta: object) -> None:
        assert parse_background_launch({"sessionUpdate": "tool_call_update", "_meta": meta}) is None

    @staticmethod
    def _workflow_launch_named(name: str) -> dict[str, Any]:
        return {
            "sessionUpdate": "tool_call_update",
            "_meta": {
                "claudeCode": {
                    "toolResponse": {
                        "status": "async_launched",
                        "taskId": "wuda97r0j",
                        "workflowName": name,
                    },
                    "toolName": "Workflow",
                }
            },
        }

    def test_a_control_bearing_workflow_name_is_defanged(self) -> None:
        """No Cc/Cf character survives into the label; the printable text does.

        The name is model-authored and the label reaches a ``logger.info``
        line and the user-facing recycle notice reason, so a newline, ANSI
        escape, bidi override or NUL in it must come out as plain text.
        """
        hostile = "deploy\nnotes\r\x1b[31mred‮\x00end"
        label = parse_background_launch(self._workflow_launch_named(hostile))
        assert label is not None
        assert not any(unicodedata.category(c) in ("Cc", "Cf") for c in label)
        for printable in ("deploy", "notes", "red", "end"):
            assert printable in label

    def test_a_name_of_only_control_characters_is_the_generic_label(self) -> None:
        update = self._workflow_launch_named("\x1b\x00‮\r\n\t")
        assert parse_background_launch(update) == "background task"


class TestTheRecord:
    def test_it_reports_the_newest_launch_and_names_it(self) -> None:
        record = BackgroundLaunchRecord()
        assert record.age(100.0) is None
        assert record.note(CAPTURED_BASH_LAUNCH, now=10.0)
        assert record.note(CAPTURED_WORKFLOW_LAUNCH, now=40.0)
        assert record.age(100.0) == 60.0
        assert record.describe() == 'background command, workflow "sleep-echo"'

    def test_a_frame_that_is_not_a_launch_changes_nothing(self) -> None:
        record = BackgroundLaunchRecord()
        record.note(CAPTURED_BASH_LAUNCH, now=10.0)
        assert not record.note(FOREGROUND_BASH, now=90.0)
        assert not record.note("not a dict", now=90.0)
        assert record.age(100.0) == 90.0

    def test_the_labels_stay_bounded(self) -> None:
        record = BackgroundLaunchRecord()
        for i in range(BACKGROUND_LAUNCH_LABELS_MAX + 4):
            update = {
                "sessionUpdate": "tool_call_update",
                "_meta": {
                    "claudeCode": {
                        "toolResponse": {
                            "status": "async_launched",
                            "taskId": f"t{i}",
                            "workflowName": f"wf{i}",
                        }
                    }
                },
            }
            record.note(update, now=float(i))
        assert len(record.labels) == BACKGROUND_LAUNCH_LABELS_MAX
        assert record.labels[-1] == f'workflow "wf{BACKGROUND_LAUNCH_LABELS_MAX + 3}"'

    def test_labels_dropped_at_the_bound_are_counted_in_describe(self) -> None:
        def launch(name: str) -> dict[str, Any]:
            return {
                "sessionUpdate": "tool_call_update",
                "_meta": {
                    "claudeCode": {
                        "toolResponse": {
                            "status": "async_launched",
                            "taskId": name,
                            "workflowName": name,
                        }
                    }
                },
            }

        record = BackgroundLaunchRecord()
        for i in range(5):
            assert record.note(launch(f"wf{i}"), now=float(i))
        assert record.describe() == ('workflow "wf2", workflow "wf3", workflow "wf4" (and 2 more)')
        assert record.note(launch("wf3"), now=10.0)
        assert record.omitted == 2
        assert record.describe() == ('workflow "wf2", workflow "wf4", workflow "wf3" (and 2 more)')


# --------------------------------------------------------------------------- #
# The drift tripwire: a launch-shaped response the parse does not recognise
# --------------------------------------------------------------------------- #


def _claude_update(response: dict[str, Any], tool: str = "Bash") -> dict[str, Any]:
    return {
        "sessionUpdate": "tool_call_update",
        "_meta": {"claudeCode": {"toolResponse": response, "toolName": tool}},
    }


class TestDriftTripwire:
    @pytest.mark.parametrize(
        "update",
        [
            CAPTURED_BASH_LAUNCH,
            CAPTURED_WORKFLOW_LAUNCH,
            CAPTURED_BASH_TERMINAL,
            FOREGROUND_BASH,
            ASYNC_AGENT_LAUNCH,
            _claude_update(
                {"status": "async_launched", "taskId": "t1", "isAsync": True}, tool="Task"
            ),
            {**CAPTURED_BASH_LAUNCH, "sessionUpdate": "tool_call"},
        ],
        ids=[
            "bash",
            "workflow",
            "prose-result",
            "foreground",
            "held-agent",
            "held-task",
            "not-an-update",
        ],
    )
    def test_silent_on_every_frame_the_parse_already_handles(self, update: dict[str, Any]) -> None:
        assert unrecognised_background_launch(update) is None

    @pytest.mark.parametrize(
        ("response", "expected"),
        [
            ({"stdout": "", "background_task_id": "b1"}, "keys background_task_id; tool Bash"),
            ({"status": "asyncLaunched", "taskId": "w1"}, 'status "asyncLaunched"; tool Bash'),
            ({"status": "async_launched", "task_id": "w1"}, 'status "async_launched"; tool Bash'),
            ({"backgroundTaskId": "  "}, "keys backgroundTaskId; tool Bash"),
            ({"BackgroundJob": {"id": "j"}}, "keys BackgroundJob; tool Bash"),
        ],
        ids=[
            "renamed-bash-key",
            "renamed-status",
            "renamed-task-id",
            "blank-id",
            "reshaped-nested",
        ],
    )
    def test_fires_on_a_renamed_or_reshaped_launch(
        self, response: dict[str, Any], expected: str
    ) -> None:
        update = _claude_update(response)
        assert parse_background_launch(update) is None
        assert unrecognised_background_launch(update) == expected

    def test_a_rename_without_either_word_is_not_caught(self) -> None:
        """Residual: a rename that drops both words (``bgTaskId``) slips past.

        The tripwire is a word match by design; a closed list of every future
        spelling does not exist. Pinned so whoever widens it sees this flip.
        """
        assert unrecognised_background_launch(_claude_update({"bgTaskId": "b1"})) is None

    def test_adapter_authored_names_are_folded_and_bounded(self) -> None:
        key = "background\x1b[31m\nforged" + "x" * 100
        drift = unrecognised_background_launch(_claude_update({key: "b1"}, tool="Ba\u202esh"))
        assert drift is not None
        assert not any(unicodedata.category(c) in ("Cc", "Cf") for c in drift)
        assert len(drift) < 120

    def test_the_record_hands_out_drift_once(self) -> None:
        record = BackgroundLaunchRecord()
        renamed = _claude_update({"background_task_id": "b1"})
        assert not record.note(renamed, now=1.0)
        assert record.take_drift() == "keys background_task_id; tool Bash"
        assert not record.note(_claude_update({"status": "asyncLaunched"}), now=2.0)
        assert record.take_drift() is None
        assert record.age(10.0) is None


# --------------------------------------------------------------------------- #
# The transport that serves the stamping harness records it
# --------------------------------------------------------------------------- #


def _bare_client() -> AcpClient:
    """An AcpClient carrying only the field the launch helpers touch.

    Built without ``__init__`` because the real one spawns a backend process.
    """
    client = AcpClient.__new__(AcpClient)
    client._acp_backend = ACP_BACKEND_CLAUDE
    client._background_launches = BackgroundLaunchRecord()
    return client


class TestTheDedicatedClientRecordsIt:
    """claude is served by ``AcpClient``, so this is the implementation that runs."""

    def test_a_launch_frame_is_recorded_and_exposed(self) -> None:
        client = _bare_client()
        assert client.background_launch() is None
        client._note_background_launch(_update_msg(CAPTURED_WORKFLOW_LAUNCH))
        launch = client.background_launch()
        assert launch is not None
        age, description = launch
        assert 0.0 <= age < 5.0
        assert description == 'workflow "sleep-echo"'

    def test_a_turn_frame_that_is_not_a_launch_records_nothing(self) -> None:
        client = _bare_client()
        client._note_background_launch(_update_msg(CAPTURED_BASH_TERMINAL))
        assert client.background_launch() is None

    def test_a_drifted_shape_is_logged_once_per_session(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _bare_client()
        renamed = _claude_update({"background_task_id": "b1"})
        with caplog.at_level("INFO", logger="kiro_crew.acp.client"):
            client._note_background_launch(_update_msg(renamed))
            client._note_background_launch(_update_msg(renamed))
            client._note_background_launch(_update_msg(FOREGROUND_BASH))
        lines = [
            r.getMessage()
            for r in caplog.records
            if "recognised claude-agent-acp shape" in r.getMessage()
        ]
        assert lines == [
            "ACP: a tool response looks like a background launch but matches no recognised "
            "claude-agent-acp shape (keys background_task_id; tool Bash); the watchdog will "
            "not hold this session for it. Logged once per session."
        ]
        assert client.background_launch() is None

    @pytest.mark.parametrize(
        "func",
        [
            AcpClient._dispatch_events,
            AcpClient.send_message_stream,
            AcpClient._read_prompt_response,
        ],
    )
    def test_every_prompt_loop_notes_it(self, func: Any) -> None:
        """A loop that skipped the call would leave one prompt API blind to a launch."""
        assert "_note_background_launch" in inspect.getsource(func)


class TestTheRecordDiesWithTheProcess:
    def test_reset_state_clears_the_record(self, tmp_path: Any) -> None:
        """``_reset_state`` is the process-death reset ``ensure_ready`` runs
        before respawning on the same client object. The launched work ran in
        the DEAD process's own tree, so a record that survived it would grant
        the fresh tree the watchdog's hold (and hard-ceiling grace) on behalf
        of work that is already dead."""
        # A fully built client, as test_acp_mcp_ref_guard constructs one: the
        # constructor does not spawn, and _reset_state walks real attributes
        # (process, liveness oracle, seed provenance) a bare __new__ client
        # would have to fake one by one.
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._note_background_launch(_update_msg(CAPTURED_WORKFLOW_LAUNCH))
        assert client.background_launch() is not None
        client._reset_state()
        assert client.background_launch() is None


class TestProviderExposesIt:
    def test_the_session_layer_reads_only_the_real_shape(self) -> None:
        """A MagicMock or AsyncMock provider must read as "nothing launched"."""
        assert session._provider_background_launch(MagicMock()) is None
        assert session._provider_background_launch(AsyncMock()) is None
        assert session._provider_background_launch(object()) is None
        odd = MagicMock()
        odd.background_launch = lambda: (True, "x")
        assert session._provider_background_launch(odd) is None
        real = MagicMock()
        real.background_launch = lambda: (3, "background command")
        assert session._provider_background_launch(real) == (3.0, "background command")


# --------------------------------------------------------------------------- #
# The two recycle paths
# --------------------------------------------------------------------------- #


def _make_manager(rss_max_mb: int):
    from kiro_crew.session import SessionManager

    cfg = MagicMock()
    cfg.session.pool_size = 0
    cfg.session.pool_agent = ""
    cfg.session.pool_ttl_secs = 0
    cfg.session.watchdog_rss_max_mb = rss_max_mb
    return SessionManager(cfg=cfg, provider_factory=None)


def _session_with_launch(launch: tuple[float, str] | None):
    sess = MagicMock()
    sess.semaphore = MagicMock()
    sess.semaphore.locked.return_value = False
    sess.provider.background_launch = lambda: launch
    return sess


_INSIDE = (30.0, 'workflow "sleep-echo"')
_PAST = (HARNESS_BACKGROUND_WORK_HOLD_SECS + 1.0, 'workflow "sleep-echo"')


@pytest.fixture
def _proc_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session.platform_compat, "IS_WINDOWS", False)


async def _rss_tick(manager: Any, rss: int = 2514) -> None:
    with (
        patch("kiro_crew.session._build_child_map", return_value={}),
        patch("kiro_crew.session._rss_mb_from_tree", return_value=rss),
    ):
        await manager._rss_threshold_check()


@pytest.mark.usefixtures("_proc_route")
class TestRssRecycleHonoursHarnessWork:
    @pytest.mark.asyncio
    async def test_a_launch_inside_the_hold_keeps_the_session(self) -> None:
        # rss=1500 is above the 1000 MB ceiling but inside the 2x hard ceiling,
        # so the hold applies.
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:x"] = _session_with_launch(_INSIDE)
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=4242)

        await _rss_tick(manager, rss=1500)

        manager.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_hard_ceiling_recycles_even_inside_the_hold(self) -> None:
        # Each launch refreshes the hold's clock, so a session that keeps
        # launching could hold the memory ceiling off forever; past
        # rss_max_mb * HARNESS_BACKGROUND_WORK_HARD_CEILING_FACTOR the recycle
        # proceeds anyway, and the notice still names the launched work.
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:x"] = _session_with_launch(_INSIDE)
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=4242)
        cb = AsyncMock()
        manager.set_recycle_callback(cb)
        rss = 1000 * HARNESS_BACKGROUND_WORK_HARD_CEILING_FACTOR + 1

        await _rss_tick(manager, rss=rss)

        manager.reset.assert_awaited_once()
        reason = cb.await_args.kwargs["reason"]
        assert reason.startswith(f"memory limit ({rss}MB)")
        assert 'workflow "sleep-echo"' in reason

    @pytest.mark.asyncio
    async def test_no_launch_recycles_as_before(self) -> None:
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:x"] = _session_with_launch(None)
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=4242)
        cb = AsyncMock()
        manager.set_recycle_callback(cb)

        await _rss_tick(manager)

        manager.reset.assert_awaited_once()
        assert cb.await_args.kwargs["reason"] == "memory limit (2514MB)"

    @pytest.mark.asyncio
    async def test_a_launch_past_the_hold_recycles_with_the_plain_notice(self) -> None:
        """Letting the recycle through past the hold presumes the work
        finished, so the notice must not claim it "may have been stopped" --
        the record is never cleared while the process lives, and naming a
        launch from hours ago would be permanent noise on every recycle."""
        manager = _make_manager(rss_max_mb=1000)
        manager._sessions["dashboard:x"] = _session_with_launch(_PAST)
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=4242)
        cb = AsyncMock()
        manager.set_recycle_callback(cb)

        await _rss_tick(manager)

        manager.reset.assert_awaited_once()
        assert cb.await_args.kwargs["reason"] == "memory limit (2514MB)"

    @pytest.mark.asyncio
    async def test_a_raising_provider_does_not_break_the_tick(self) -> None:
        manager = _make_manager(rss_max_mb=1000)
        sess = _session_with_launch(None)

        def boom() -> tuple[float, str]:
            raise RuntimeError("client gone")

        sess.provider.background_launch = boom
        manager._sessions["dashboard:x"] = sess
        manager.reset = AsyncMock(return_value=True)
        manager.get_pid = MagicMock(return_value=4242)

        await _rss_tick(manager)

        manager.reset.assert_awaited_once()


class TestIdleSweepHonoursHarnessWork:
    @pytest.mark.asyncio
    async def test_an_idle_session_inside_the_hold_is_kept(self) -> None:
        manager = _make_manager(rss_max_mb=0)
        sess = _session_with_launch(_INSIDE)
        sess.last_used = time.monotonic() - 10_000
        manager._sessions["dashboard:x"] = sess
        manager.reset = AsyncMock(return_value=True)

        await manager._expire_idle(timeout_secs=1)

        manager.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_orphaned_session_inside_the_hold_is_kept(self) -> None:
        manager = _make_manager(rss_max_mb=0)
        sess = _session_with_launch(_INSIDE)
        sess.last_used = time.monotonic()
        manager._sessions["dashboard:x"] = sess
        manager.reset = AsyncMock(return_value=True)
        manager.set_active_dashboard_slots({"dashboard:x"})
        manager.set_active_dashboard_slots({"dashboard:other"})  # x's tab closed

        await manager._expire_idle(timeout_secs=9999)

        manager.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_orphaned_session_without_a_launch_is_reaped(self) -> None:
        """The control for the case above: the orphan axis is really armed."""
        manager = _make_manager(rss_max_mb=0)
        sess = _session_with_launch(None)
        sess.last_used = time.monotonic()
        manager._sessions["dashboard:x"] = sess
        manager.reset = AsyncMock(return_value=True)
        manager.set_active_dashboard_slots({"dashboard:x"})
        manager.set_active_dashboard_slots({"dashboard:other"})

        await manager._expire_idle(timeout_secs=9999)

        manager.reset.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_idle_session_past_the_hold_expires(self) -> None:
        manager = _make_manager(rss_max_mb=0)
        sess = _session_with_launch(_PAST)
        sess.last_used = time.monotonic() - 10_000
        manager._sessions["dashboard:x"] = sess
        manager.reset = AsyncMock(return_value=True)

        await manager._expire_idle(timeout_secs=1)

        manager.reset.assert_awaited_once()
