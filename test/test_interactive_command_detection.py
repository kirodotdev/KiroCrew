"""Interactive-command detection at the tool layer (RFC §14.6 / SPEC-ADDENDUM §6).

Pre-dispatch: ``classify_interactive_command`` is a TABLE over known
programs — pagers, editors, REPLs reading stdin, package-manager confirmations,
credential prompts — producing a risk class and a non-interactive HINT; it never
rewrites the command. Post-stall: STUCK_INPUT (Linux) or a ``platform_limited``
no-progress verdict on a prompt-shaped command is classified ``waiting_input``
and yielded as a typed status BEFORE any action, so a scheduler can release the
slot. Never auto-answers; never marks a side-effecting command ``safe_retry``.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.liveness import (
    EVIDENCE_PLATFORM_LIMITED,
    INTERACTIVE_CONFIRM,
    INTERACTIVE_EDITOR,
    INTERACTIVE_NARROWING_RISKS,
    INTERACTIVE_NONE,
    INTERACTIVE_PAGER,
    INTERACTIVE_PROMPT,
    INTERACTIVE_REPL,
    NOT_INTERACTIVE,
    VERDICT_STUCK_INPUT,
    VERDICT_UNKNOWN,
    ToolCallState,
    classify_interactive_command,
    non_interactive_hint,
)
from kiro_crew.acp.session_handle import (
    INTERACTIVE_POLICY_WAIT,
    AcpSessionHandle,
    WatchdogSettings,
)
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_STRUCTURED_STATUS,
    METHOD_SESSION_UPDATE,
    STATUS_ORIGIN_LIVENESS_ORACLE,
    STOP_REASON_TOOL_STALL,
    WAIT_REASON_INPUT,
    JsonRpcMessage,
)

# ── The classifier table ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command, risk, program",
    [
        ("less README.md", INTERACTIVE_PAGER, "less"),
        ("man tar", INTERACTIVE_PAGER, "man"),
        ("git log --oneline", INTERACTIVE_PAGER, "git log"),
        ("git diff HEAD~1", INTERACTIVE_PAGER, "git diff"),
        ("git stash list", INTERACTIVE_PAGER, "git stash"),
        ("vim notes.md", INTERACTIVE_EDITOR, "vim"),
        ("nano /etc/hosts", INTERACTIVE_EDITOR, "nano"),
        ("git commit", INTERACTIVE_EDITOR, "git commit"),
        ("git rebase -i HEAD~3", INTERACTIVE_EDITOR, "git rebase -i"),
        ("git add -p", INTERACTIVE_PROMPT, "git add -p"),
        ("python", INTERACTIVE_REPL, "python"),
        ("node", INTERACTIVE_REPL, "node"),
        ("psql", INTERACTIVE_REPL, "psql"),
        ("cat", INTERACTIVE_REPL, "cat"),
        ("apt-get install jq", INTERACTIVE_CONFIRM, "apt-get"),
        ("dnf install jq", INTERACTIVE_CONFIRM, "dnf"),
        ("pacman -S vim", INTERACTIVE_CONFIRM, "pacman"),
        ("pip uninstall requests", INTERACTIVE_CONFIRM, "pip"),
        ("npm init", INTERACTIVE_CONFIRM, "npm"),
        ("sudo apt-get install -y jq", INTERACTIVE_PROMPT, "sudo"),
        ("ssh build-host uptime", INTERACTIVE_PROMPT, "ssh"),
        ("scp file host:/tmp/", INTERACTIVE_PROMPT, "scp"),
        ("gpg --decrypt secret.gpg", INTERACTIVE_PROMPT, "gpg"),
        ("docker login registry", INTERACTIVE_PROMPT, "docker"),
        ("gh auth login", INTERACTIVE_PROMPT, "gh"),
        ("aws configure", INTERACTIVE_PROMPT, "aws"),
    ],
)
def test_known_interactive_shapes(command, risk, program):
    found = classify_interactive_command(command)
    assert found.risk == risk
    assert found.program == program
    assert found.hint, "every positive match carries a non-interactive hint"
    assert found.reason


@pytest.mark.parametrize(
    "command",
    [
        "git -P log --oneline",
        "git --no-pager diff",
        "git log | head -20",
        "git log > log.txt",
        "less README.md | grep foo",
        "git commit -m 'msg'",
        "git commit --no-edit",
        "git rebase main",
        "git add .",
        "git stash",
        "python -c 'print(1)'",
        "python script.py",
        "python -m pytest -q",
        "node app.js",
        "psql -c 'select 1'",
        "psql < schema.sql",
        "cat file.txt",
        "cat < file.txt",
        "apt-get install -y jq",
        "apt-get -qy install jq",
        "dnf --assumeyes install jq",
        "pacman -S --noconfirm vim",
        "pip uninstall -y requests",
        "pip install requests",
        "npm init -y",
        "npm install",
        "sudo -n ls /root",
        "sudo -S ls /root",
        "ssh -o BatchMode=yes host uptime",
        "scp -B file host:/tmp/",
        "gpg --batch --decrypt secret.gpg",
        "docker ps",
        "docker login --password-stdin registry",
        "gh pr list",
        "aws s3 ls",
        "emacs --batch -l build.el",
        "long-build release > build.log 2>&1",
        "ls -la",
        "",
    ],
)
def test_non_interactive_shapes(command):
    assert classify_interactive_command(command) == NOT_INTERACTIVE
    assert non_interactive_hint(command) == ""


def test_json_wrapped_tool_input_is_unwrapped():
    found = classify_interactive_command('{"command": "git log --graph", "cwd": "/x"}')
    assert found.risk == INTERACTIVE_PAGER
    assert found.program == "git log"


def test_env_assignments_and_wrappers_are_skipped():
    assert classify_interactive_command("FOO=1 env BAR=2 nohup python").risk == INTERACTIVE_REPL
    assert classify_interactive_command("timeout 30 python").risk == INTERACTIVE_REPL
    assert classify_interactive_command("nice -n 10 vim x").risk == INTERACTIVE_EDITOR
    assert classify_interactive_command("exec less x").risk == INTERACTIVE_PAGER


def test_non_interactive_sudo_classifies_what_it_wraps():
    found = classify_interactive_command("sudo -n apt-get install jq")
    assert found.risk == INTERACTIVE_CONFIRM
    assert found.program == "apt-get"


def test_a_prompt_outranks_an_earlier_pager_in_a_pipeline():
    """The pager is advisory (stdout is a pipe under the tool); the prompt is
    what would actually hang."""
    found = classify_interactive_command("git log | ssh host 'cat > log'")
    assert found.risk == INTERACTIVE_PROMPT
    assert found.program == "ssh"


def test_first_positive_segment_wins_among_equals():
    found = classify_interactive_command("ls; python; node")
    assert found.program == "python"


def test_narrowing_eligibility_excludes_the_pager():
    assert INTERACTIVE_PAGER not in INTERACTIVE_NARROWING_RISKS
    assert not classify_interactive_command("git log").narrowing_eligible
    for cmd in ("vim x", "python", "apt-get install jq", "ssh host"):
        assert classify_interactive_command(cmd).narrowing_eligible, cmd


def test_replay_safety_is_fail_closed():
    """Only a read-only viewer is a safe replay; everything else may have acted."""
    assert classify_interactive_command("git log").replay_safe
    assert classify_interactive_command("less x").replay_safe
    assert classify_interactive_command("cat").replay_safe
    for cmd in (
        "git commit",
        "python",
        "apt-get install jq",
        "ssh host reboot",
        "sudo rm -rf build",
        "npm init",
        "vim x",
    ):
        assert not classify_interactive_command(cmd).replay_safe, cmd
    # An unmatched command is never "safe": the classifier has no verdict on it.
    assert not NOT_INTERACTIVE.replay_safe
    assert not classify_interactive_command("ls").replay_safe


def test_classifier_never_raises_on_garbage():
    for junk in (None, 12, "\x00\x00", "|||", "&& && ;;", '{"command": 5}', "'unterminated"):
        found = classify_interactive_command(junk)  # type: ignore[arg-type]
        assert found.risk in {
            INTERACTIVE_NONE,
            INTERACTIVE_PAGER,
            INTERACTIVE_EDITOR,
            INTERACTIVE_REPL,
            INTERACTIVE_CONFIRM,
            INTERACTIVE_PROMPT,
        }


def test_hints_are_proposals_not_rewrites():
    """The hint names the non-interactive form; the classifier never returns a
    rewritten command, and every hint keeps the original program visible."""
    for cmd, expect in (
        ("git log", "git -P log"),
        ("apt-get install jq", "apt-get -y"),
        ("ssh host", "BatchMode=yes"),
        ("python", "python -c"),
        ("git commit", "git commit -m"),
    ):
        assert expect in non_interactive_hint(cmd), cmd


# ── Dispatch: classification rides the in-flight tool state ─────────────────


def _handle(watchdog: WatchdogSettings | None = None) -> AcpSessionHandle:
    rt = MagicMock()
    rt._last_activity = time.monotonic()
    rt.pid = None
    rt.acp_backend = "kiro"
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    rt.send_request = AsyncMock(return_value=1)
    return AcpSessionHandle("sA", asyncio.Queue(), rt, watchdog=watchdog)


def _tool_call_msg(command: str, *, kind: str = "execute", tool_call_id: str = "c1"):
    return JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={
            "sessionId": "sA",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": tool_call_id,
                "title": "run a command",
                "kind": kind,
                "rawInput": {"command": command},
            },
        },
    )


def test_shell_tool_call_is_classified_at_dispatch():
    handle = _handle()
    handle._handle_update(_tool_call_msg("apt-get install jq"))
    assert handle.inflight_interactive is not None
    assert handle.inflight_interactive.risk == INTERACTIVE_CONFIRM
    assert handle._inflight_tool is not None
    assert handle._inflight_tool.interactive_risk == INTERACTIVE_CONFIRM
    assert handle._inflight_tool_call_id == "c1"


def test_non_shell_tool_call_is_never_interactive_risk():
    handle = _handle()
    handle._handle_update(_tool_call_msg("python", kind="read"))
    assert handle.inflight_interactive is None
    assert handle._inflight_tool is not None
    assert handle._inflight_tool.interactive_risk == INTERACTIVE_NONE


def test_classification_reads_the_trusted_command_not_the_title():
    """The title is LLM-authored prose; the command bytes decide."""
    handle = _handle()
    msg = _tool_call_msg("ls -la")
    msg.params["update"]["title"] = "vim notes.md"
    handle._handle_update(msg)
    assert handle.inflight_interactive == NOT_INTERACTIVE


def test_tool_result_clears_the_classification():
    handle = _handle()
    handle._handle_update(_tool_call_msg("python"))
    assert handle.inflight_interactive is not None
    handle._handle_update(
        JsonRpcMessage(
            method=METHOD_SESSION_UPDATE,
            params={
                "sessionId": "sA",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "c1",
                    "status": "completed",
                    "rawOutput": {"items": [{"Text": "done"}]},
                },
            },
        )
    )
    assert handle.inflight_interactive is None
    assert handle._inflight_tool_call_id == ""


# ── Dispatch: post-stall classification → waiting_input ─────────────────────


class _SilentQueue:
    def __init__(self, tick: float = 0.02) -> None:
        self._tick = tick

    async def get(self):
        await asyncio.sleep(self._tick)
        raise asyncio.TimeoutError

    def qsize(self) -> int:
        return 0


def _stalling(wd: WatchdogSettings, command: str, verdict: str, evidence: str) -> AcpSessionHandle:
    handle = _handle(watchdog=wd)
    handle._turn_done.clear()
    handle._stale_eligible = False
    handle._handle_update(_tool_call_msg(command))  # real dispatch path → classification
    handle._queue = _SilentQueue()  # type: ignore[assignment]
    handle._oracle.check_tool = lambda pid, tool: (verdict, evidence)  # type: ignore[method-assign]
    return handle


async def _drain(handle: AcpSessionHandle, timeout: float):
    return [ev async for ev in handle._dispatch_events(1, timeout)]


_FAST = dict(check_after_secs=0.01, tool_stall_hard_cap_secs=999.0, model_silent_probe_secs=999.0)
_PLATFORM_FLAT = (
    f"{EVIDENCE_PLATFORM_LIMITED}: shell child 42 alive, subtree flat (cpu +0ns "
    "(darwin cpu-only)); stdin-block evidence unavailable on this platform"
)
_STUCK = "stuck_input: pid 42 blocked reading /dev/tty with flat subtree"


@pytest.mark.asyncio
async def test_stuck_input_yields_waiting_input_status_before_the_stall_terminal():
    wd = WatchdogSettings(tool_stall_suspect_secs=999.0, stale_window_secs=999.0, **_FAST)
    handle = _stalling(wd, "ssh build-host uptime", VERDICT_STUCK_INPUT, _STUCK)

    events = await _drain(handle, timeout=5.0)

    assert [e.kind for e in events] == [EVENT_STRUCTURED_STATUS, EVENT_COMPLETE]
    status_ev, complete = events
    assert status_ev.status is not None
    assert status_ev.status.is_waiting
    assert status_ev.status.wait_reason == WAIT_REASON_INPUT
    assert status_ev.status.origin == STATUS_ORIGIN_LIVENESS_ORACLE
    assert status_ev.status.tool_call_id == "c1"
    assert status_ev.tool_call_id == "c1"
    assert status_ev.status.cancellable and not status_ev.status.resumable
    assert status_ev.status.evidence.startswith("stuck_input: ")
    # The existing non-lethal recovery still runs (policy "cancel") and the
    # terminal carries the SAME typed status.
    assert complete.stop_reason == STOP_REASON_TOOL_STALL
    assert complete.status is status_ev.status
    assert "verdict=stuck_input" in complete.text


@pytest.mark.asyncio
async def test_side_effecting_command_is_never_a_safe_retry():
    """``ssh host reboot`` may have run: the status must not invite a replay."""
    wd = WatchdogSettings(tool_stall_suspect_secs=999.0, stale_window_secs=999.0, **_FAST)
    handle = _stalling(wd, "ssh build-host reboot", VERDICT_STUCK_INPUT, _STUCK)
    events = await _drain(handle, timeout=5.0)
    assert events[0].status is not None
    assert events[0].status.safe_retry is False


@pytest.mark.asyncio
async def test_read_only_pager_without_output_is_a_safe_retry():
    wd = WatchdogSettings(tool_stall_suspect_secs=999.0, stale_window_secs=999.0, **_FAST)
    handle = _stalling(wd, "git log --oneline", VERDICT_STUCK_INPUT, _STUCK)
    events = await _drain(handle, timeout=5.0)
    assert events[0].status is not None
    assert events[0].status.safe_retry is True


@pytest.mark.asyncio
async def test_a_command_that_already_streamed_output_is_not_a_safe_retry():
    """Recorded output means the command acted; a retry is never proposed as safe."""
    wd = WatchdogSettings(tool_stall_suspect_secs=999.0, stale_window_secs=999.0, **_FAST)
    handle = _stalling(wd, "git log --oneline", VERDICT_STUCK_INPUT, _STUCK)
    handle._tool_output_seen.add("c1")
    events = await _drain(handle, timeout=5.0)
    assert events[0].status is not None
    assert events[0].status.safe_retry is False


@pytest.mark.asyncio
async def test_platform_limited_flat_on_a_prompt_shaped_command_is_waiting_input_narrowed():
    """macOS/Windows: no stdin evidence, but the command is prompt-shaped, so the
    window narrows to the ordinary silence budget and the stall is W4."""
    wd = WatchdogSettings(tool_stall_suspect_secs=999.0, stale_window_secs=0.05, **_FAST)
    handle = _stalling(wd, "npm init", VERDICT_UNKNOWN, _PLATFORM_FLAT)

    events = await _drain(handle, timeout=5.0)

    assert [e.kind for e in events] == [EVENT_STRUCTURED_STATUS, EVENT_COMPLETE]
    assert events[0].status is not None
    assert events[0].status.wait_reason == WAIT_REASON_INPUT
    assert events[0].status.evidence.startswith("platform_limited+interactive_risk: ")
    assert events[1].stop_reason == STOP_REASON_TOOL_STALL


@pytest.mark.asyncio
async def test_platform_limited_flat_on_a_plain_command_keeps_the_build_scale_window():
    """A quiet build on macOS is bounded by the standard budget, not the
    narrowed one: inside the stale window nothing acts."""
    wd = WatchdogSettings(tool_stall_suspect_secs=999.0, stale_window_secs=0.05, **_FAST)
    handle = _stalling(wd, "long-build release > build.log 2>&1", VERDICT_UNKNOWN, _PLATFORM_FLAT)

    events = await _drain(handle, timeout=0.4)
    assert [e.kind for e in events] == [EVENT_COMPLETE]
    assert events[0].stop_reason == "timeout"  # the turn ceiling, not the watchdog


@pytest.mark.asyncio
async def test_platform_limited_flat_on_a_plain_command_is_still_bounded():
    """...but "alive" is never "forever": past the suspect window it is an
    opaque tool stall — no waiting_input status, ordinary recovery."""
    wd = WatchdogSettings(tool_stall_suspect_secs=0.05, stale_window_secs=999.0, **_FAST)
    handle = _stalling(wd, "long-build release > build.log 2>&1", VERDICT_UNKNOWN, _PLATFORM_FLAT)

    events = await _drain(handle, timeout=5.0)

    assert [e.kind for e in events] == [EVENT_COMPLETE]
    assert events[0].stop_reason == STOP_REASON_TOOL_STALL
    assert events[0].status is None


@pytest.mark.asyncio
async def test_pager_risk_does_not_narrow_the_platform_limited_window():
    wd = WatchdogSettings(tool_stall_suspect_secs=999.0, stale_window_secs=0.05, **_FAST)
    handle = _stalling(wd, "git log --oneline", VERDICT_UNKNOWN, _PLATFORM_FLAT)
    events = await _drain(handle, timeout=0.4)
    assert [e.stop_reason for e in events] == ["timeout"]


@pytest.mark.asyncio
async def test_wait_policy_holds_the_turn_open_and_emits_the_status_once():
    """``interactive_command_policy="wait"``: the status is yielded once, the
    call is NOT cancelled, nothing is answered, and the turn's own ceiling is
    the bound."""
    wd = WatchdogSettings(
        tool_stall_suspect_secs=999.0,
        stale_window_secs=999.0,
        interactive_command_policy=INTERACTIVE_POLICY_WAIT,
        **_FAST,
    )
    handle = _stalling(wd, "ssh build-host uptime", VERDICT_STUCK_INPUT, _STUCK)
    handle.cancel = AsyncMock()  # type: ignore[method-assign]

    collected = await _drain(handle, timeout=0.4)

    # One status, then the turn's own ceiling — never a tool-stall terminal.
    assert [e.kind for e in collected] == [EVENT_STRUCTURED_STATUS, EVENT_COMPLETE]
    assert collected[1].stop_reason == "timeout"
    assert collected[0].status is not None
    assert collected[0].status.wait_reason == WAIT_REASON_INPUT
    handle.cancel.assert_not_called()
    # Nothing was written to the backend: no auto-answer of any kind.
    handle._runtime.send_notification.assert_not_called()


@pytest.mark.asyncio
async def test_no_auto_answer_on_the_cancel_policy_either():
    """The only backend traffic on a classified stall is the session/cancel."""
    wd = WatchdogSettings(tool_stall_suspect_secs=999.0, stale_window_secs=999.0, **_FAST)
    handle = _stalling(wd, "apt-get install jq", VERDICT_STUCK_INPUT, _STUCK)
    handle.cancel = AsyncMock()  # type: ignore[method-assign]
    await _drain(handle, timeout=5.0)
    handle.cancel.assert_awaited_once()
    handle._runtime.send_notification.assert_not_called()
    handle._runtime.send_request.assert_not_called()


def test_tool_call_state_carries_the_risk_for_a_detached_consult():
    """A consult that outlives its dispatch reads the risk it was handed, never
    a re-derivation from a different command string."""
    state = ToolCallState(command="python", is_shell=True, interactive_risk=INTERACTIVE_REPL)
    assert state.interactive_risk == INTERACTIVE_REPL
    assert ToolCallState().interactive_risk == INTERACTIVE_NONE
