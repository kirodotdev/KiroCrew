"""Session-control audit tests: refusal outcomes, off-loop writes and redaction.

Covers the ``kirocrew-dashboard`` refusal audit: a tool call refused by its route,
or by an unverified caller, replies with an ``Error:`` prefix and is logged with
``outcome="failed"``. A source scan pins that no line of the ``mcp_dashboard*.py``
files pairs ``return`` with an escaped cross mark.

Also pins that the audit write from ``create_session`` and the denial audit from
``authorize_target`` run off the event loop's thread, and that a ``ghp_`` token
passed as ``target`` stays out of the denial's ``resources`` and ``error`` fields
while the ``target_not_found`` code survives in ``resources``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
)

from kiro_crew.dashboard import session_control as sc
from kiro_crew.testing.wait import wait_until

#: One valid call per kirocrew-dashboard tool, for the refusal-audit sweep below.
_DASHBOARD_TOOL_CALLS = {
    "chat_folder_tree": {},
    "chat_folder_create": {"name": "New"},
    "chat_folder_move": {"folder": "Travel", "new_parent": "kirocrew"},
    "chat_folder_update": {"folder": "Travel", "name": "Trips"},
    "chat_folder_move_session": {"session": "chat-3", "folder": "Travel"},
    "chat_folder_delete": {"folder": "Travel"},
    "chat_folder_file_self": {"folder": "Travel"},
    "chat_tag_list": {},
    "chat_tag_create": {"name": "urgent"},
    "chat_tag_update": {"tag": "todo", "name": "later"},
    "chat_tag_assign": {"session": "chat-3", "add": ["todo"]},
    "chat_session_pin": {"session": "chat-3", "pinned": True},
    "chat_session_pin_move": {"session": "chat-3", "before": "chat-4"},
    "chat_tag_column_list": {},
    "chat_tag_column_create": {"name": "Urgent", "tag": "todo"},
    "chat_tag_column_move": {"column": "Todo", "after": "Live"},
    "session_move_to_column": {"session": "chat-3", "column": "Todo"},
    "session_create": {},
    "session_fork": {},
    "session_stop": {"target": "chat-2"},
    "session_end_wait": {"target": "chat-2"},
    "session_retry": {"target": "chat-2"},
    "session_continue": {"target": "chat-2"},
    "session_set_model": {"target": "chat-2", "model": "sonnet"},
    "session_reload": {"target": "chat-2"},
    "session_set_color": {"target": "chat-2", "color": "1"},
    "session_rename": {"target": "chat-2", "title": "Worker"},
    "session_generate_title": {"target": "chat-2"},
    "session_close": {"target": "chat-2"},
    "session_revive": {"target": "chat-2"},
    "session_history_list": {},
    "session_send": {"target": "chat-2", "message": "hi"},
    "session_broadcast": {"message": "hi", "mode": "queue", "all": True},
    "session_status": {},
    "session_adopt": {"target": "chat-2"},
    "session_handoff": {"target": "chat-2", "to": "chat-3"},
    "session_release": {"target": "chat-2"},
    "session_read_message": {"target": "chat-2"},
    "session_summary": {"target": "chat-2"},
    "work_dispatch": {"title": "t", "acceptance": {}, "agent": "a", "seed": "s"},
}


@pytest.mark.parametrize("refusal", ["route", "identity"])
def test_every_session_control_refusal_is_audited_as_failed(monkeypatch, refusal):
    """A refused tool call must not be recorded as a completed one.

    `call_tool_with_logging` classifies by prefix -- `outcome="failed"` only when
    the result starts with "Error:". A refusal without it lands in the audit as a
    successful invocation, which inverts the record for exactly the calls a
    reviewer would go looking for. Swept over every `kirocrew-dashboard` tool, the
    session-control verbs included, for both ways one is refused: every route
    answers a refusal, or the caller cannot be verified. The tool list is checked
    against the server's rows, so a new tool must be added here to pass.

    Mutation guard: a refusal reply opening with a cross mark instead of "Error:"
    is recorded `completed` and fails here.
    """
    from kiro_crew import mcp_shared
    from kiro_crew.mcp_dashboard import SESSION_CONTROL_TOOLS, TABLE
    from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
    from kiro_crew.mcp_tools.table import Caller, ToolContext

    assert set(_DASHBOARD_TOOL_CALLS) == set(TABLE.names())
    assert set(SESSION_CONTROL_TOOLS) <= set(_DASHBOARD_TOOL_CALLS)
    rows: list[dict] = []
    monkeypatch.setattr(
        mcp_shared, "sel", lambda: SimpleNamespace(log_tool_invocation=lambda **r: rows.append(r))
    )
    caller = (
        Caller.strict("dashboard:chat-1")
        if refusal == "route"
        else Caller.unverified("dashboard:chat-1")
    )
    refused = {"error": "refused", "code": "x"}
    for tool, args in _DASHBOARD_TOOL_CALLS.items():
        dash = InMemoryDashboardClient(
            {
                f"{method} /api/{{route}}": refused
                for method in ("GET", "POST", "PATCH", "PUT", "DELETE")
            }
        )
        out = TABLE.call(tool, dict(args), ToolContext(dash, caller))
        assert out.startswith("Error:"), (tool, out)
        assert rows[-1]["tool_name"] == tool and rows[-1]["outcome"] == "failed", (tool, out)


def test_no_dashboard_refusal_opens_with_a_cross_mark():
    """The sweep above reaches each tool's FIRST refusal; deeper branches need this.

    A refusal deeper in a body (one route answering after another refused) has
    no fixture-free behavioural reach, so its prefix is held by the text: a
    return whose string opens with the cross mark is audited as completed.
    """
    import re
    from pathlib import Path

    parts = sorted(Path(sc.__file__).parent.parent.glob("mcp_dashboard*.py"))
    text = "".join(p.read_text(encoding="utf-8") for p in parts)
    bare = re.findall(r"return[^\n]*\\u274c[^\n]*", text)
    assert not bare, f"dashboard refusals not prefixed with 'Error:': {bare}"


def test_the_audit_write_does_not_run_on_the_event_loop(tmp_path, monkeypatch):
    """Constructing the SEL must not happen on the loop.

    `log_tool_invocation` only enqueues, but the FIRST `sel()` of a process
    constructs the log -- trust-dir creation, key validation, and on Windows a
    DACL write that can block on a network volume round-trip. This can genuinely
    be that first call, because
    `sel_audit_middleware` logs AFTER `await handler(...)`: on a fresh gateway the
    first authenticated request constructs the log inside whatever handler runs
    first.

    Mutation guard: calling `sel()` inline runs it on the calling thread.
    """
    import threading

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    seen: dict[str, int] = {}

    class _Recorder:
        def log_tool_invocation(self, **_kw):
            seen["thread"] = threading.get_ident()

    monkeypatch.setattr(sc, "sel", lambda: _Recorder())
    monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)

    async def _drive() -> int:
        await sc.create_session(state, caller_session_key=_key(caller))
        return threading.get_ident()

    loop_thread = asyncio.run(_drive())

    # The offload is fire-and-forget, so wait for it rather than racing it.
    wait_until(lambda: "thread" in seen, describe=lambda: "the audit never ran")
    assert (
        seen["thread"] != loop_thread
    ), "the audit must be dispatched off the loop's thread, not called inline"


def test_the_denial_audit_does_not_run_on_the_event_loop(tmp_path, monkeypatch):
    """The same property for the DENIAL path, which is the likelier first `sel()`.

    A fresh gateway refuses before it ever allows -- a disabled feature, an
    unidentified caller -- so the denial audit is the more probable site of the
    first-ever `sel()` construction, not the rarer one.

    Mutation guard: calling `sel()` inline in `deny` runs it on the loop thread.
    """
    import threading

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    seen: dict[str, int] = {}

    class _Recorder:
        def log_api_access(self, **_kw):
            seen["thread"] = threading.get_ident()

    monkeypatch.setattr(sc, "sel", lambda: _Recorder())

    async def _drive() -> int:
        with pytest.raises(sc.SessionControlError):
            sc.authorize_target(
                state,
                caller_session_key=_key(caller),
                target="does-not-exist",
                operation="read",
            )
        return threading.get_ident()

    loop_thread = asyncio.run(_drive())

    wait_until(lambda: "thread" in seen, describe=lambda: "the denial was never audited")
    assert (
        seen["thread"] != loop_thread
    ), "the denial audit must be dispatched off the loop's thread"


def test_the_denial_audit_does_not_persist_caller_supplied_credentials(tmp_path, monkeypatch):
    """A credential passed as `target` must not survive into the audit sink.

    `target` is raw MCP input. The audit is durable AND served back: the SEL
    writer does not redact on-disk records (`sel.py` says so outright) and
    `/api/sel/events` returns `recent()` rows verbatim to the dashboard, so an
    unredacted write is readable long after the refusal.

    Both fields are asserted because both carry caller text: `resources`
    interpolates `target` directly, and the `target_not_found` refusal
    interpolates it into `reason`, which becomes `error`. A fix that redacted
    only one of them would leave the other serving the secret.

    Mutation guard: dropping either `redact` call in `deny` fails this.
    """

    # Assembled rather than written literally: a real-looking token in source
    # trips secret scanners for no benefit. `redact_credentials` matches the
    # `ghp_` + 36 shape.
    noisy_target = "ghp_" + ("a" * 36)

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    captured: dict[str, str] = {}

    class _Recorder:
        def log_api_access(self, **kw):
            captured.update({k: str(v) for k, v in kw.items()})

    monkeypatch.setattr(sc, "sel", lambda: _Recorder())

    async def _drive() -> None:
        with pytest.raises(sc.SessionControlError):
            sc.authorize_target(
                state,
                caller_session_key=_key(caller),
                target=noisy_target,
                operation="read",
            )

    asyncio.run(_drive())

    wait_until(lambda: "resources" in captured, describe=lambda: "the denial was never audited")
    assert noisy_target not in captured["resources"], (
        "the raw target reached the audit's `resources`; /api/sel/events would "
        "serve the credential back to the dashboard"
    )
    assert noisy_target not in captured.get("error", ""), (
        "the target_not_found reason interpolates the target, so `error` carried "
        "the credential even though `resources` was redacted"
    )
    # The refusal must still be diagnosable: the code survives redaction.
    assert "target_not_found" in captured["resources"]
