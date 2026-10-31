"""The batch write guard: a write-class call issued in the same parallel tool
batch as a read-class call is refused at the transport, with the reason steered
into the turn first.

A model that sends a read and a write that quotes the read's answer in ONE
batch has not seen the answer yet: the calls run in parallel, so the write
carries a guessed value. The guard refuses the write and tells the model to
send it again once the read has returned. It is opt-in
(``hooks.batch_write_guard``), so with the setting off nothing changes.

Three layers, each pinned here:

* the classifier (:func:`batch_guard.classify`) and its explicit tables;
* the batch window (:class:`batch_guard.BatchWindow`): which calls count as one
  batch;
* both transports' ``approve_tool``, fed by real ``tool_call`` frames, steering
  the notice before the rejection goes on the wire.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.types import (
    EVENT_PERMISSION_REQUEST,
    EVENT_TOOL_CALL,
    OPTION_ALLOW_ONCE,
    OUTCOME_CANCELLED,
    OUTCOME_SELECTED,
    AcpEvent,
    JsonRpcMessage,
)


def _guard():
    # Resolved by name so the unfixed tree fails inside each test, naming the
    # missing module, rather than at collection.
    try:
        return importlib.import_module("kiro_crew.acp.batch_guard")
    except ImportError:
        pytest.fail("kiro_crew.acp.batch_guard is missing: no batch write guard exists")


# ── frames ──


def _tool_call_frame(
    call_id: str,
    tool_name: str,
    raw_input: dict,
    *,
    kind: str = "",
    server: str = "",
    title: str = "",
) -> JsonRpcMessage:
    kiro: dict = {"toolName": tool_name}
    if server:
        kiro["mcpServerName"] = server
    update: dict = {
        "sessionUpdate": "tool_call",
        "toolCallId": call_id,
        "title": title or tool_name,
        "rawInput": raw_input,
        "_meta": {"kiro": kiro},
    }
    if kind:
        update["kind"] = kind
    return JsonRpcMessage(method="session/update", params={"sessionId": "s1", "update": update})


def _settled_frame(call_id: str, status: str = "completed") -> JsonRpcMessage:
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": call_id,
                "status": status,
                "content": [{"type": "content", "content": {"type": "text", "text": "ok"}}],
            },
        },
    )


def _read_frame(call_id: str = "tc-read") -> JsonRpcMessage:
    return _tool_call_frame(
        call_id,
        "shell",
        {"command": "gh pr view 7 --json headRefOid"},
        kind="execute",
        title="Running: gh pr view 7",
    )


def _write_frame(call_id: str = "tc-write") -> JsonRpcMessage:
    return _tool_call_frame(
        call_id,
        "shell",
        {"command": "gh pr merge 7 --squash"},
        kind="execute",
        title="Running: gh pr merge 7 --squash",
    )


def _write_permission(request_id: int, call_id: str = "tc-write") -> AcpEvent:
    command = "gh pr merge 7 --squash"
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=request_id,
        tool_call_id=call_id,
        title=f"Running: {command}",
        tool_kind="execute",
        is_shell=True,
        tool_input=json.dumps({"command": command}),
    )


def _event(**fields) -> AcpEvent:
    return AcpEvent(kind=EVENT_TOOL_CALL, **fields)


def _shell(command: str) -> AcpEvent:
    return _event(is_shell=True, tool_name="shell", tool_input=json.dumps({"command": command}))


# ── classifier ──


@pytest.mark.parametrize(
    "command",
    [
        "gh pr merge 12 --squash --admin --match-head-commit abc",
        "gh pr create --title t --body-file ./b.md",
        "gh pr review 9 --approve -F body.md",
        "gh pr comment 9 --body hi",
        "gh issue comment 3 --body hi",
        "git push -u origin feat/x",
        "git -C /w commit -m 'msg'",
        "cd /w && git commit -F msg.txt",
        "aws --profile p --region us-west-2 dynamodb put-item --table-name t",
        "aws s3 cp a.txt s3://bucket/a.txt",
        "aws lambda invoke --function-name f out.json",
        "gh api -X POST repos/o/r/issues",
        "gh api --method=PATCH repos/o/r/pulls/1",
        "gh api -XDELETE repos/o/r/git/refs/heads/x",
        "gh -R o/r pr merge 1 --squash",
        "gh release create v1",
        "git -c user.name=x push origin HEAD",
        "GH_PROMPT_DISABLED=1 timeout 120 gh pr create --title t < /dev/null",
        # A '#' comment or a '#' inside a word does not hide the next command.
        "cd repo  # go\ngit push origin HEAD",
        "curl https://h/p#frag && gh pr merge 7",
        # gh api sends POST once it has parameters, unless told GET.
        "gh api repos/o/r/issues/1/comments -f body=done",
        "gh api repos/o/r/issues --input payload.json",
        "gh api graphql -f query='mutation { addStar(input: {}) { clientMutationId } }'",
        # Real separators still split, with or without spaces around them.
        "cd /w && git push origin x; ls | wc -l",
        "ls;git push origin x",
        "ls\ngh pr merge 1",
        "ls 'quoted; text' && gh pr create --title \"a; b\"",
    ],
)
def test_clearly_write_shell_commands_are_write_class(command):
    guard = _guard()
    assert guard.classify(_shell(command)) == guard.WRITE


@pytest.mark.parametrize(
    "command",
    [
        "gh pr view 12 --json state,mergeCommit",
        "gh pr checks 12",
        "git log -1 --oneline",
        "git status -sb",
        "aws sts get-caller-identity",
        "aws --profile p ec2 describe-instances",
        "aws s3 ls s3://bucket/",
        "aws logs filter-log-events --log-group-name g",
        "ls -la && cat README.md",
        "python3 -m pytest -q test/test_x.py",
        "echo example-review-tool upload is a phrase here",
        # Write-shaped ARGUMENT text is not a write: only program words count.
        "git log --all --grep='git commit'",
        # Separators inside quotes do not split the command.
        "grep -R 'cd repo && git push origin main' docs/",
        'grep -n "x; gh pr merge 1" notes.md',
        "echo 'a | git commit -m b'",
        "echo '&&' git commit -m example",
        'echo ";" gh pr merge 1',
        # An unquoted '#' word starts a comment that runs to the newline.
        "git status # && git commit -m example",
        "grep -rn 'gh pr merge' docs/",
        "gh pr view 7 --json body --jq '.body' | grep 'git push'",
        "gh api repos/o/r/pulls/1",
        "gh api -X GET search/issues -f q=repo:o/r",
        "gh api graphql -f query='query { viewer { login } }'",
        # Not a write until an operator lists it (see the extra-prefix tests).
        "example-review-tool upload --new-review",
    ],
)
def test_every_other_shell_command_is_read_class(command):
    guard = _guard()
    assert guard.classify(_shell(command)) == guard.READ


def test_only_the_head_of_a_long_command_is_read():
    # Classification runs on the event loop for every tool_call frame, so a
    # backend-authored command is read only up to a bound: a write named past it
    # is missed (the guard never refuses more), and a megabyte command is cheap.
    guard = _guard()
    head = "x" * guard.MAX_CLASSIFIED_COMMAND_CHARS
    assert guard.classify_command(f"ls {head} && git push origin main") == guard.READ
    assert guard.classify_command(f"git push origin main && ls {head}") == guard.WRITE
    assert guard.classify_command("x" * (2 * 1024 * 1024)) == guard.READ


_EXTRA = ("example-review-tool upload",)


@pytest.mark.parametrize(
    "command",
    [
        "example-review-tool upload --new-review",
        "cd /w && example-review-tool upload",
        "X=1 /opt/bin/example-review-tool upload -r 3",
        "timeout 60 example-review-tool upload",
    ],
)
def test_an_operator_listed_prefix_is_write_class(command):
    guard = _guard()
    assert guard.classify(_shell(command), _EXTRA) == guard.WRITE


@pytest.mark.parametrize(
    "command",
    [
        "example-review-tool status",
        "echo example-review-tool upload",
        "example-review-tool",
    ],
)
def test_an_operator_prefix_matches_whole_leading_words_only(command):
    guard = _guard()
    assert guard.classify(_shell(command), _EXTRA) == guard.READ


def test_the_built_in_table_names_no_extra_write_commands():
    from kiro_crew.hook_runtime.hook_types import HooksConfig

    assert HooksConfig.from_dict({}).batch_write_guard_commands == []
    assert HooksConfig.from_dict(
        {"batch_write_guard_commands": ["example-review-tool upload", "", 3]}
    ).batch_write_guard_commands == ["example-review-tool upload"]
    assert (
        HooksConfig.from_dict({"batch_write_guard_commands": "x"}).batch_write_guard_commands == []
    )


@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [
        ("fs_write", "write"),
        ("write", "write"),
        ("str_replace", "write"),
        ("fs_append", "write"),
        ("delete_file", "write"),
        ("fs_read", "read"),
        ("read", "read"),
        ("read_file", "read"),
        ("list_directory", "read"),
        ("grep", "read"),
        ("grep_search", "read"),
        ("glob", "read"),
        ("file_search", "read"),
        ("web_fetch", "read"),
    ],
)
def test_built_in_file_tools_follow_the_table(tool_name, expected):
    guard = _guard()
    assert guard.classify(_event(tool_name=tool_name)) == expected


@pytest.mark.parametrize(
    ("server", "tool", "expected"),
    [
        ("kirocrew-work", "work_report", "write"),
        ("kirocrew-work", "work_ledger_record", "write"),
        ("kirocrew-work", "work_ledger_read", "read"),
        ("kirocrew-work", "work_brief", "read"),
        ("kirocrew-dashboard", "session_send", "write"),
        ("kirocrew-dashboard", "session_broadcast", "write"),
        ("kirocrew-dashboard", "session_status", "read"),
        ("kirocrew-dashboard", "session_read_message", "read"),
        ("kirocrew-core", "send_message", "write"),
        ("kirocrew-core", "session_ledger_record", "write"),
        ("kirocrew-core", "memory_recall", "read"),
        ("kirocrew-core", "spawn_status", "read"),
    ],
)
def test_first_party_mcp_tools_follow_the_table(server, tool, expected):
    guard = _guard()
    event = _event(tool_name=tool, mcp_server_name=server, mcp_identity_trusted=True)
    assert guard.classify(event) == expected


def test_an_unknown_tool_is_not_guarded():
    # Unknown means "neither": the guard never refuses more than its tables name.
    guard = _guard()
    assert guard.classify(_event(tool_name="frobnicate", mcp_server_name="third-party")) == ""
    assert guard.classify(_event(title="something")) == ""


# ── batch window ──


def test_a_write_in_a_batch_with_a_read_is_refused():
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("r", guard.READ)
    window.dispatched("w", guard.WRITE)
    reason = window.refusal_for("w", guard.WRITE)
    assert reason is not None
    assert "same parallel tool batch" in reason
    assert "next step" in reason


def test_the_read_itself_is_never_refused():
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("r", guard.READ)
    window.dispatched("w", guard.WRITE)
    assert window.refusal_for("r", guard.READ) is None


def test_a_read_that_returned_before_the_write_was_sent_is_a_different_batch():
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("r", guard.READ)
    window.settled("r")
    window.dispatched("w", guard.WRITE)
    assert window.refusal_for("w", guard.WRITE) is None


def test_a_read_that_settles_while_the_write_waits_still_shares_its_batch():
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("r", guard.READ)
    window.dispatched("w", guard.WRITE)
    window.settled("r")
    assert window.refusal_for("w", guard.WRITE) is not None


def test_writes_alone_and_unknown_peers_pass():
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("w1", guard.WRITE)
    window.dispatched("w2", guard.WRITE)
    window.dispatched("x", "")
    assert window.refusal_for("w2", guard.WRITE) is None


def test_a_call_the_window_never_saw_is_not_judged():
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("r", guard.READ)
    assert window.refusal_for("never-announced", guard.WRITE) is None


def test_a_new_turn_starts_an_empty_window():
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("r", guard.READ)
    window.reset()
    window.dispatched("w", guard.WRITE)
    assert window.refusal_for("w", guard.WRITE) is None


def test_the_window_shares_the_in_flight_bound_and_logs_overflow(caplog):
    guard = _guard()
    from kiro_crew.acp._dispatch_parts.tool_call_provenance import MAX_ACTIVE_TOOL_CALLS

    window = guard.BatchWindow()
    with caplog.at_level("WARNING", logger=guard.__name__):
        for i in range(MAX_ACTIVE_TOOL_CALLS + 50):
            window.dispatched(f"r{i}", guard.READ)
    assert len(window) == MAX_ACTIVE_TOOL_CALLS
    overflow = [r for r in caplog.records if "later calls are not judged" in r.getMessage()]
    assert len(overflow) == 1


def test_the_window_keeps_a_bounded_key_for_a_huge_id():
    guard = _guard()
    from kiro_crew.acp._dispatch_parts.tool_call_provenance import TOOL_CALL_CACHE_MAX_KEY_CHARS

    huge = "i" * (1024 * 1024)
    window = guard.BatchWindow()
    window.dispatched("r", guard.READ)
    window.dispatched(huge, guard.WRITE, "git push " + "x" * (1024 * 1024))
    assert all(len(k) <= TOOL_CALL_CACHE_MAX_KEY_CHARS for k in window._members)
    assert all(
        len(m.command) <= guard.MAX_CLASSIFIED_COMMAND_CHARS for m in window._members.values()
    )
    # The same id still resolves at approval and at its terminal.
    assert window.refusal_for(huge, guard.WRITE) is not None
    window.settled(huge)
    window.settled("r")
    window.dispatched("next", guard.WRITE)
    assert len(window) == 1


def test_a_batch_of_operator_listed_writes_is_not_refused():
    # Peers are classified with the operator's prefixes too, so two listed writes
    # in one batch hold no read.
    guard = _guard()
    window = guard.BatchWindow()
    for cid, command in (
        ("a", "example-review-tool upload a"),
        ("b", "example-review-tool upload b"),
    ):
        window.dispatched(cid, guard.classify_command(command), command)
    assert window.refusal_for("a", guard.WRITE, _EXTRA) is None
    # Without the prefixes, the peer is a plain shell read.
    assert window.refusal_for("a", guard.WRITE) is not None


def test_a_later_frame_for_a_settled_read_does_not_reopen_its_batch():
    # A terminal update that also carries the read's input arrives as a
    # refinement after the settle; it must refresh the member, not reopen it.
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("r", guard.READ, "gh pr view 7")
    window.settled("r")
    window.dispatched("r", guard.READ, "gh pr view 7")
    window.dispatched("w", guard.WRITE)
    assert window.refusal_for("w", guard.WRITE) is None


def _refinement_frame(call_id: str, command: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": call_id,
                "title": f"Running: {command}",
                "kind": "execute",
                "rawInput": {"command": command},
            },
        },
    )


def test_a_shell_call_with_no_command_yet_is_neither_class():
    guard = _guard()
    assert guard.classify(_shell("")) == ""
    assert guard.classify(_event(is_shell=True, tool_name="shell")) == ""


# ── the setting ──


def test_the_guard_is_off_unless_the_operator_turns_it_on():
    from kiro_crew.hook_runtime.hook_types import HooksConfig

    assert HooksConfig.from_dict({}).batch_write_guard is False
    assert HooksConfig.from_dict({"batch_write_guard": True}).batch_write_guard is True
    # A malformed value keeps it off: the guard refuses calls, so junk must not
    # switch it on.
    assert HooksConfig.from_dict({"batch_write_guard": "yes"}).batch_write_guard is False
    assert HooksConfig(batch_write_guard=True).to_dict()["batch_write_guard"] is True


# ── transports ──


def _answers(send_mock):
    return [call.args[1]["outcome"] for call in send_mock.await_args_list]


def _client(tmp_path):
    from kiro_crew.acp.client import AcpClient

    client = AcpClient(work_dir=tmp_path)
    client._send_response = AsyncMock()
    client._send_error = AsyncMock()
    return client


def _handle():
    from kiro_crew.acp.session_handle import AcpSessionHandle

    runtime = MagicMock()
    runtime.send_response = AsyncMock()
    runtime.send_error = AsyncMock()
    return AcpSessionHandle("s1", asyncio.Queue(), runtime)


def _feed(transport, msg):
    if hasattr(transport, "_handle_update"):
        transport._handle_update(msg)
        return
    transport._extract_tool_event(msg)
    transport._extract_tool_call_update(msg)


def _with_steer(transport, monkeypatch):
    order: list[str] = []
    steered: list[str] = []

    async def _steer(text):
        order.append("steer")
        steered.append(text)
        return True

    send = (
        transport._send_response
        if hasattr(transport, "_send_response")
        else (transport._runtime.send_response)
    )

    async def _send(*args, **kwargs):
        order.append("answer")

    send.side_effect = _send
    monkeypatch.setattr(transport, "steer", _steer, raising=False)
    monkeypatch.setattr(type(transport), "supports_refusal_steer", property(lambda self: True))
    return order, steered


def _sent(transport):
    if hasattr(transport, "_send_response") and isinstance(transport._send_response, AsyncMock):
        return transport._send_response
    return transport._runtime.send_response


_TRANSPORTS = [
    pytest.param(lambda tmp_path: _client(tmp_path), id="AcpClient"),
    pytest.param(lambda tmp_path: _handle(), id="AcpSessionHandle"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("make_transport", _TRANSPORTS)
async def test_transport_refuses_a_write_batched_with_a_read(tmp_path, monkeypatch, make_transport):
    guard = _guard()
    monkeypatch.setattr(guard, "guard_settings", lambda: (True, ()))
    transport = make_transport(tmp_path)
    order, steered = _with_steer(transport, monkeypatch)
    _feed(transport, _read_frame())
    _feed(transport, _write_frame())
    transport._permission_gate_events[41] = _write_permission(41)

    assert await transport.approve_tool(41) is False

    outcomes = _answers(_sent(transport))
    assert outcomes and all(o.get("optionId") != OPTION_ALLOW_ONCE for o in outcomes)
    assert outcomes[-1] == {"outcome": OUTCOME_CANCELLED}
    # The reason reaches the model in-band, BEFORE the rejection is written.
    assert order[:2] == ["steer", "answer"]
    assert "batch guard" in steered[0]
    assert "next step" in steered[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("make_transport", _TRANSPORTS)
async def test_transport_approves_the_same_write_once_the_read_returned(
    tmp_path, monkeypatch, make_transport
):
    guard = _guard()
    monkeypatch.setattr(guard, "guard_settings", lambda: (True, ()))
    transport = make_transport(tmp_path)
    _feed(transport, _read_frame())
    _feed(transport, _settled_frame("tc-read"))
    _feed(transport, _write_frame())
    transport._permission_gate_events[42] = _write_permission(42)

    assert await transport.approve_tool(42) is True
    assert _answers(_sent(transport))[-1].get("outcome") == OUTCOME_SELECTED


@pytest.mark.asyncio
@pytest.mark.parametrize("make_transport", _TRANSPORTS)
async def test_transport_with_the_guard_off_approves_as_before(
    tmp_path, monkeypatch, make_transport
):
    guard = _guard()
    monkeypatch.setattr(guard, "guard_settings", lambda: (False, ()))
    transport = make_transport(tmp_path)
    _feed(transport, _read_frame())
    _feed(transport, _write_frame())
    transport._permission_gate_events[43] = _write_permission(43)

    assert await transport.approve_tool(43) is True


def _tool_frame(call_id: str, command: str) -> JsonRpcMessage:
    return _tool_call_frame(
        call_id, "shell", {"command": command}, kind="execute", title=f"Running: {command}"
    )


def _shell_permission(request_id: int, call_id: str, command: str) -> AcpEvent:
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=request_id,
        tool_call_id=call_id,
        title=f"Running: {command}",
        tool_kind="execute",
        is_shell=True,
        tool_input=json.dumps({"command": command}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("make_transport", _TRANSPORTS)
@pytest.mark.parametrize(
    ("extra", "expected"),
    [pytest.param(_EXTRA, False, id="listed"), pytest.param((), True, id="not-listed")],
)
async def test_transport_applies_the_operators_extra_write_commands(
    tmp_path, monkeypatch, make_transport, extra, expected
):
    guard = _guard()
    monkeypatch.setattr(guard, "guard_settings", lambda: (True, extra))
    transport = make_transport(tmp_path)
    command = "example-review-tool upload --new-review"
    _feed(transport, _read_frame())
    _feed(transport, _tool_frame("tc-upload", command))
    transport._permission_gate_events[44] = _shell_permission(44, "tc-upload", command)

    assert await transport.approve_tool(44) is expected


@pytest.mark.asyncio
async def test_guard_settings_read_the_hooks_section(monkeypatch):
    guard = _guard()
    from kiro_crew.config import KiroCrewConfig

    class _Cfg:
        hooks = {"batch_write_guard": True, "batch_write_guard_commands": list(_EXTRA)}

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _Cfg()))
    assert guard.guard_settings() == (True, _EXTRA)
    _Cfg.hooks = {}
    assert guard.guard_settings() == (False, ())


def test_demo_refusal_text():
    """The refusal a model reads, printed for the PR body's demo transcript."""
    guard = _guard()
    from kiro_crew.deny_notice import build_refusal_steer_notice

    window = guard.BatchWindow()
    window.dispatched("tc-read", guard.READ)
    window.dispatched("tc-write", guard.WRITE)
    reason = window.refusal_for("tc-write", guard.WRITE)
    assert reason is not None
    notice = build_refusal_steer_notice(
        "Running: gh pr merge 7 --squash", reason, cause=guard.DENY_CAUSE_BATCH_READ_WRITE
    )
    print(notice)
    assert "batch guard" in notice
    assert "NOT a user action" in notice


@pytest.mark.asyncio
@pytest.mark.parametrize("make_transport", _TRANSPORTS)
async def test_a_read_whose_command_arrives_in_a_refinement_still_counts(
    tmp_path, monkeypatch, make_transport
):
    guard = _guard()
    monkeypatch.setattr(guard, "guard_settings", lambda: (True, ()))
    transport = make_transport(tmp_path)
    _with_steer(transport, monkeypatch)
    _feed(transport, _tool_call_frame("tc-read", "shell", {}, kind="execute", title="Shell"))
    refinement = _refinement_frame("tc-read", "gh pr view 7 --json headRefOid")
    if hasattr(transport, "_handle_update"):
        transport._handle_update(refinement)
    else:
        transport._extract_tool_call_refinement(refinement)
    _feed(transport, _write_frame())
    transport._permission_gate_events[45] = _write_permission(45)

    assert await transport.approve_tool(45) is False


def _mcp_frame(call_id: str, server: str) -> JsonRpcMessage:
    # A frame that names only the MCP server; the tool name arrives later.
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": call_id,
                "title": "Running MCP tool",
                "rawInput": {},
                "_meta": {"kiro": {"mcpServerName": server}},
            },
        },
    )


def _mcp_permission(request_id: int, call_id: str, server: str, tool: str) -> AcpEvent:
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=request_id,
        tool_call_id=call_id,
        title=f"Running: @{server}/{tool}",
        mcp_server_name=server,
        tool_name=tool,
        mcp_identity_trusted=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("make_transport", _TRANSPORTS)
async def test_a_peer_named_only_by_its_permission_request_still_counts(
    tmp_path, monkeypatch, make_transport
):
    # The read's frame named only its server; its own approval names the tool,
    # and that refreshes the member, so the ledger write after it is refused.
    guard = _guard()
    monkeypatch.setattr(guard, "guard_settings", lambda: (True, ()))
    transport = make_transport(tmp_path)
    _with_steer(transport, monkeypatch)
    _feed(transport, _mcp_frame("tc-ledger-read", "kirocrew-work"))
    _feed(transport, _mcp_frame("tc-ledger-write", "kirocrew-work"))
    transport._permission_gate_events[46] = _mcp_permission(
        46, "tc-ledger-read", "kirocrew-work", "work_ledger_read"
    )
    transport._permission_gate_events[47] = _mcp_permission(
        47, "tc-ledger-write", "kirocrew-work", "work_ledger_record"
    )

    assert await transport.approve_tool(46) is True
    assert await transport.approve_tool(47) is False
