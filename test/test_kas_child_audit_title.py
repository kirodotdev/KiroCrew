"""The audit row of a KAS native sub-agent's finished tool call names it in at most 512 characters.

The session handle keeps a finished child call's title past that bound
(``acp.kas_child_finished._TITLE_KEPT_CHARS``), so that a reader which cuts it can
see a credential whole first. The dashboard turn writes the title as the audit
row's ``tool_name`` (``chat_turn.tool_results._on_child_tool_finished``): scanned
in full, then cut to 512. The security event log stays redacted whatever the
owner's setting, and its writer's own pass cannot recognise a credential a cut
already split.

Each frame goes through a real session handle and the dashboard turn's own
handler. "Written" is the name after the log writer's own pass (``sel._redact_text``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import redaction_seams, sel
from kiro_crew.acp import kas_child_finished
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import ACP_BACKEND_KAS, AcpEvent, JsonRpcMessage
from kiro_crew.agent_sdk.capabilities import capabilities_for

_CHILD = {"kiro": {"agentSubtaskId": "sa-1"}}
#: The audit row's bound.
_ROW_MAX = 512
_HOST = "@db.internal/app"
#: A command whose credential starts before character 512 and ends after it.
_PASSWORD = "Pw" + "6" * 700
_STRADDLING = f"psql postgres://admin:{_PASSWORD}{_HOST} -c 'select 1'"
#: How much of the password counts as written.
_LEAK = 32

assert _STRADDLING.index(_PASSWORD) < _ROW_MAX < _STRADDLING.index(_HOST)


def _handle() -> AcpSessionHandle:
    rt = MagicMock()
    rt.acp_backend = ACP_BACKEND_KAS
    rt.pid = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    return AcpSessionHandle("sB", asyncio.Queue(), rt)


def _finished_call(command: str) -> AcpEvent:
    """The activity event of a child shell call's finishing frame, from a real handle."""
    handle = _handle()

    def update(frame: dict[str, Any]) -> list[AcpEvent]:
        return handle._handle_update(
            JsonRpcMessage(method="session/update", params={"update": frame})
        )

    update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "child-tc",
            "title": "Run Command",
            "kind": "execute",
            "status": "in_progress",
            "rawInput": {"command": command},
            "_meta": _CHILD,
        }
    )
    [event] = update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "child-tc",
            "status": "completed",
            "rawOutput": {"stdout": "1"},
            "_meta": _CHILD,
        }
    )
    assert event.tool_final or event.tool_status == "completed"
    return event


def _audit_name(monkeypatch: pytest.MonkeyPatch, command: str, *, redaction_off: bool) -> str:
    """The ``tool_name`` the dashboard turn hands the audit log for *command*, a child's
    finished shell call, parsed and handled inside the owner's chat turn."""
    from kiro_crew.dashboard import chat_runner

    names: list[str] = []
    log = SimpleNamespace(log_tool_invocation=lambda **row: names.append(row["tool_name"]))

    async def _no_hooks(turn: Any, event: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(chat_runner, "sel", lambda: log)
    monkeypatch.setattr(chat_runner, "_fire", _no_hooks)
    monkeypatch.setattr(redaction_seams, "_redaction_off", redaction_off)
    turn = SimpleNamespace(
        client=SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS)),
        _pending_tool_ids={},
    )
    with redaction_seams.output_seam(redaction_seams.CHAT):
        asyncio.run(chat_runner._on_child_tool_finished(turn, _finished_call(command)))
    [name] = names
    return name


@pytest.mark.parametrize("redaction_off", [False, True], ids=["setting-on", "setting-off"])
def test_a_credential_that_straddles_the_rows_bound_is_masked_not_cut_in_half(
    monkeypatch: pytest.MonkeyPatch, redaction_off: bool
) -> None:
    name = _audit_name(monkeypatch, _STRADDLING, redaction_off=redaction_off)

    assert len(name) <= _ROW_MAX
    written = sel._redact_text(name)
    assert _PASSWORD[:_LEAK] not in written
    assert _HOST in written, "the name was dropped, not masked"


@pytest.mark.parametrize("redaction_off", [False, True], ids=["setting-on", "setting-off"])
@pytest.mark.parametrize(
    "command",
    ["git status", "echo " + "x" * 600 + " && git status", "echo " + "y" * 6000],
    ids=["short", "past-the-rows-bound", "past-the-kept-bound"],
)
def test_a_name_with_no_credential_is_the_command_cut_to_the_rows_bound(
    monkeypatch: pytest.MonkeyPatch, command: str, redaction_off: bool
) -> None:
    """What the row held before the title was kept longer: the same characters."""
    assert _audit_name(monkeypatch, command, redaction_off=redaction_off) == command[:_ROW_MAX]


def test_the_rows_bound_is_the_bound_a_reader_stores_a_title_to() -> None:
    assert kas_child_finished._TITLE_MAX_CHARS == _ROW_MAX
    assert kas_child_finished._TITLE_KEPT_CHARS == _ROW_MAX + 4096
