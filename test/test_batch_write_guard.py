"""The batch write guard: a write-class call issued in the same parallel tool
batch as a read-class call is refused at the transport, with the reason steered
into the turn first.

A model that sends a read and a write that quotes the read's answer in ONE
batch has not seen the answer yet: the calls run in parallel, so the write
carries a guessed value. The guard refuses the write and tells the model to
send it again once the read has returned. It is on by default
(``hooks.batch_write_guard``); with the setting ``false`` nothing changes.

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


#: A head sha no call of the batch had returned: the write quotes a guess.
_UNSEEN_SHA = "1f69e7c13c1bb4c1b0bb1fa5b1e2f75c73a25ae0"
_MERGE = f"gh pr merge 7 --squash --match-head-commit {_UNSEEN_SHA}"


def _write_frame(call_id: str = "tc-write") -> JsonRpcMessage:
    return _tool_call_frame(
        call_id,
        "shell",
        {"command": _MERGE},
        kind="execute",
        title=f"Running: {_MERGE}",
    )


def _write_permission(request_id: int, call_id: str = "tc-write") -> AcpEvent:
    command = _MERGE
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
        # Wrapper operands and paths do not hide the program.
        "timeout 60s gh pr merge 7 --squash",
        "timeout -k 5 1.5m git push origin x",
        "/usr/bin/env -C /w GH_TOKEN=x gh pr create --title t",
        "env time -o t.txt git commit -m x",
        "/usr/bin/timeout 30 git push origin x",
        # gh api sends POST once it has parameters, unless told GET.
        "gh api repos/o/r/issues/1/comments -f body=done",
        "gh api repos/o/r/issues --input payload.json",
        "gh api graphql -f query='mutation { addStar(input: {}) { clientMutationId } }'",
        # An attached -f / -F operand is the same field as a separated one.
        "gh api graphql -fquery='mutation { addStar(input: {}) { clientMutationId } }'",
        "gh api graphql -Fquery='mutation { addStar(input: {}) { clientMutationId } }'",
        "gh api repos/o/r/issues/1/comments -fbody=done",
        "gh api -X POST graphql -f query='mutation { addStar(input: {}) { clientMutationId } }'",
        # A query the guard cannot read, or one behind comments, may be a mutation.
        "gh api graphql -F query=@op.graphql",
        "gh api graphql --input op.json",
        "gh api graphql -f query='# star it\nmutation { addStar(input: {}) { clientMutationId } }'",
        # A here-string opens no here-document, and a backslash-newline joins lines.
        'cat <<< "ready"\ngh pr merge 7 --squash',
        "gh pr \\\nmerge 7 --squash",
        "git \\\n  push origin x",
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
        # A here-document body is data for the command, not commands.
        "cat > deploy.sh <<'EOF'\ngit push origin main\nEOF",
        'cat <<"EOF" > n.md\ngh pr merge 7 --squash\nEOF\nls',
        "cat <<-END > x\n\tgit commit -m y\n\tEND",
        "cat <<EOF\ngit push origin main",
        # Known limit: the shared reader stops at a live ``<<``, so a write after
        # the here-document's terminator is not judged either. It passes.
        "cat > f <<'EOF'\nhello\nEOF\ngit push origin x",
        # An unquoted '#' word starts a comment that runs to the newline.
        "git status # && git commit -m example",
        "grep -rn 'gh pr merge' docs/",
        "gh pr view 7 --json body --jq '.body' | grep 'git push'",
        "gh api repos/o/r/pulls/1",
        "gh api -X GET search/issues -f q=repo:o/r",
        "gh api graphql -f query='query { viewer { login } }'",
        # Value-taking gh api options before the endpoint do not hide GraphQL.
        "gh api --hostname ghe.example.com graphql -f query='{viewer{login}}'",
        "gh api -H 'Accept: application/json' graphql -f query='query{x}'",
        # GraphQL always POSTs, so an explicit POST does not make a query a write.
        "gh api graphql -X POST -f query='query { viewer { login } }'",
        "gh api graphql -XPOST -f query='query { viewer { login } }'",
        "gh api graphql --method POST -f query='query { viewer { login } }'",
        "gh api graphql --method=POST -fquery='query { viewer { login } }'",
        "aws logs tail /g --follow",
        "aws s3 presign s3://b/k",
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


@pytest.mark.parametrize("command", ["/usr/bin/rm report.json", "rm report.json"])
def test_a_path_qualified_operator_prefix_matches_by_program_name(command):
    # The command's program is read by basename, so a listed path is too.
    guard = _guard()
    assert guard.classify(_shell(command), ("/usr/bin/rm",)) == guard.WRITE


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


def _next_step(window) -> None:
    """Move *window*'s clock on, as the model's next step does."""
    now = window._clock() + 1.0
    window._clock = lambda: now


def _push(sha: str = _UNSEEN_SHA) -> str:
    """A write that names a sha, so it carries a token the session never saw."""
    return f"git push origin {sha}:refs/heads/x"


def test_a_write_in_a_batch_with_a_read_is_refused():
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("r", guard.READ)
    window.dispatched("w", guard.WRITE, _push())
    reason = window.refusal_for("w", guard.WRITE)
    assert reason is not None
    assert "same parallel tool batch" in reason
    assert "next step" in reason


def test_the_read_itself_is_never_refused():
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("r", guard.READ)
    window.dispatched("w", guard.WRITE, _push())
    assert window.refusal_for("r", guard.READ) is None


def test_a_read_that_returned_before_the_write_was_sent_is_a_different_batch():
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("r", guard.READ)
    window.settled("r")
    _next_step(window)
    window.dispatched("w", guard.WRITE, _push())
    assert window.refusal_for("w", guard.WRITE) is None


def test_a_write_announced_right_after_its_read_returned_starts_a_new_batch():
    # As on main: once no call is open, the next call starts a new batch, however
    # soon it arrives, so the guard never fires on a one-call-at-a-time harness.
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("r", guard.READ)
    window.settled("r")
    window.dispatched("w", guard.WRITE, _push())
    assert window.refusal_for("w", guard.WRITE) is None


def test_a_read_that_settles_while_the_write_waits_still_shares_its_batch():
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("r", guard.READ)
    window.dispatched("w", guard.WRITE, _push())
    window.settled("r")
    assert window.refusal_for("w", guard.WRITE) is not None


def test_writes_alone_and_unknown_peers_pass():
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("w1", guard.WRITE, _push("2a3b4c5d6e"))
    window.dispatched("w2", guard.WRITE, _push())
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
    window.dispatched("w", guard.WRITE, _push())
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
    # Past the classified-command bound but inside the scan bound, so the set
    # stays complete and the unseen sha is still judged.
    window.dispatched(huge, guard.WRITE, _push() + " " + "x" * (64 * 1024))
    assert all(len(k) <= TOOL_CALL_CACHE_MAX_KEY_CHARS for k in window._members)
    assert all(
        len(m.command) <= guard.MAX_CLASSIFIED_COMMAND_CHARS for m in window._members.values()
    )
    # The same id still resolves at approval and at its terminal.
    assert window.refusal_for(huge, guard.WRITE) is not None
    window.settled(huge)
    window.settled("r")
    _next_step(window)
    window.dispatched("next", guard.WRITE)
    assert len(window) == 1


def test_a_batch_of_operator_listed_writes_is_not_refused():
    # Peers are classified with the operator's prefixes too, so two listed writes
    # in one batch hold no read.
    guard = _guard()
    window = guard.BatchWindow()
    for cid, command in (
        ("a", f"example-review-tool upload {_UNSEEN_SHA}"),
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
    _next_step(window)
    window.dispatched("w", guard.WRITE, _push())
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


def test_the_guard_is_on_unless_the_operator_turns_it_off():
    from kiro_crew.hook_runtime.hook_types import HooksConfig

    assert HooksConfig().batch_write_guard is True
    assert HooksConfig.from_dict({}).batch_write_guard is True
    assert HooksConfig.from_dict({"batch_write_guard": True}).batch_write_guard is True
    # ``false`` and its hand-edit spellings turn it off.
    for off in (False, "false", "off", "no", "0"):
        assert HooksConfig.from_dict({"batch_write_guard": off}).batch_write_guard is False
    # An unreadable value keeps the default.
    assert HooksConfig.from_dict({"batch_write_guard": "maybe"}).batch_write_guard is True
    assert HooksConfig(batch_write_guard=False).to_dict()["batch_write_guard"] is False


# ── transports ──


def _answers(send_mock):
    return [call.args[1]["outcome"] for call in send_mock.await_args_list]


def _client(tmp_path):
    from kiro_crew.acp.client import AcpClient

    client = AcpClient(work_dir=tmp_path)
    client._send_response = AsyncMock()
    client._send_error = AsyncMock()
    return _as_after_a_prompt(client)


def _handle():
    from kiro_crew.acp.session_handle import AcpSessionHandle

    runtime = MagicMock()
    runtime.send_response = AsyncMock()
    runtime.send_error = AsyncMock()
    return _as_after_a_prompt(AcpSessionHandle("s1", asyncio.Queue(), runtime))


def _as_after_a_prompt(transport):
    """The window as a sent prompt leaves it: ``refresh_feeding`` read the
    (patched) setting off the loop before the prompt went out."""
    guard = _guard()
    transport._batch_window.set_feeding(guard.guard_settings()[0])
    return transport


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
async def test_a_cancel_during_the_notice_still_answers_the_request(
    tmp_path, monkeypatch, make_transport
):
    # A turn cancelled while the refusal notice is steered must not strand the
    # permission request: an unanswered one wedges every later turn.
    import asyncio

    guard = _guard()
    monkeypatch.setattr(guard, "guard_settings", lambda: (True, ()))
    transport = make_transport(tmp_path)

    async def _cancelled_steer(*_a, **_k):
        raise asyncio.CancelledError

    monkeypatch.setattr(transport, "steer", _cancelled_steer, raising=False)
    monkeypatch.setattr(type(transport), "supports_refusal_steer", property(lambda self: True))
    audited: list[object] = []
    monkeypatch.setattr(
        guard, "audit_refusal", lambda event, reason, **_k: audited.append(event.request_id)
    )
    _feed(transport, _read_frame())
    _feed(transport, _write_frame())
    transport._permission_gate_events[45] = _write_permission(45)

    from kiro_crew.llm_helpers_parts import tool_approval
    from kiro_crew.testing.wait import await_until

    before = set(tool_approval._orphan_rejects)
    with pytest.raises(asyncio.CancelledError):
        await transport.approve_tool(45)
    orphans = tool_approval._orphan_rejects - before
    assert orphans
    await await_until(asyncio.gather(*orphans), what="the orphan reject")

    outcomes = _answers(_sent(transport))
    assert outcomes and outcomes[-1] == {"outcome": OUTCOME_CANCELLED}
    assert all(o.get("optionId") != OPTION_ALLOW_ONCE for o in outcomes)
    # The refusal is still the guard's, with its own audit row.
    assert audited == [45]


def _child_frame(msg: JsonRpcMessage, child_sid: str = "child-1") -> JsonRpcMessage:
    """*msg* as a native sub-agent's frame: on the child's session, stamped with
    the parent's Agent call it serves."""
    update = dict(msg.params["update"])
    update["_meta"] = {**update.get("_meta", {}), "claudeCode": {"parentToolUseId": "tc-agent"}}
    return JsonRpcMessage(
        method="session/update", params={"sessionId": child_sid, "update": update}
    )


def _claude_client(tmp_path):
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE

    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    client._send_response = AsyncMock()
    client._send_error = AsyncMock()
    client._session_id = "s1"
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_transport",
    [
        pytest.param(_claude_client, id="AcpClient"),
        pytest.param(lambda tmp_path: _handle(), id="AcpSessionHandle"),
    ],
)
async def test_native_sub_agents_frames_do_not_join_the_parents_batch(
    tmp_path, monkeypatch, make_transport
):
    # The parent's Agent call stays open while its child works. A child's write
    # sent after its own read returned is not in the parent's parallel batch.
    guard = _guard()
    monkeypatch.setattr(guard, "guard_settings", lambda: (True, ()))
    transport = make_transport(tmp_path)
    _feed(transport, _tool_call_frame("tc-agent", "Agent", {"prompt": "x"}, kind="think"))
    _feed(transport, _child_frame(_read_frame("tc-child-read")))
    _feed(transport, _child_frame(_settled_frame("tc-child-read")))
    _feed(transport, _child_frame(_write_frame("tc-child-write")))
    transport._permission_gate_events[46] = _write_permission(46, "tc-child-write")

    assert await transport.approve_tool(46) is True


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
    _next_step(transport._batch_window)
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
    command = f"example-review-tool upload --new-review --parent {_UNSEEN_SHA}"
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
    assert guard.guard_settings() == (True, ())
    _Cfg.hooks = {"batch_write_guard": False}
    assert guard.guard_settings() == (False, ())


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A file that does not parse: its ``hooks`` cannot carry an operator's
        # ``false``, so the guard stays off rather than taking the default.
        ('{"hooks": {"batch_write_guard": false', False),
        ('{"hooks": {"batch_write_guard": false}}', False),
        # Validation drops a quoted boolean with a hint, so it reads as the default.
        ('{"hooks": {"batch_write_guard": "false"}}', True),
        ("{}", True),
    ],
)
def test_guard_settings_through_a_real_config_load(tmp_path, text, expected):
    from unittest.mock import patch

    from kiro_crew.config.loader import _invalidate_config_cache

    guard = _guard()
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(text, encoding="utf-8")
    _invalidate_config_cache()
    try:
        with (
            patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            patch(
                "kiro_crew.config.loader.config_local_path",
                return_value=tmp_path / "config.local.json",
            ),
        ):
            assert guard.guard_settings()[0] is expected
    finally:
        _invalidate_config_cache()


def test_demo_refusal_text():
    """The refusal a model reads, printed for the PR body's demo transcript."""
    guard = _guard()
    from kiro_crew.deny_notice import build_refusal_steer_notice

    window = guard.BatchWindow()
    window.dispatched("tc-read", guard.READ, "gh pr view 7 --json headRefOid")
    window.dispatched("tc-write", guard.WRITE, _MERGE)
    reason = window.refusal_for("tc-write", guard.WRITE)
    assert reason is not None
    assert _UNSEEN_SHA[:12] in reason
    notice = build_refusal_steer_notice(
        f"Running: {_MERGE}", reason, cause=guard.DENY_CAUSE_BATCH_READ_WRITE
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


def _mcp_permission(
    request_id: int, call_id: str, server: str, tool: str, tool_input: str = ""
) -> AcpEvent:
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=request_id,
        tool_call_id=call_id,
        title=f"Running: @{server}/{tool}",
        mcp_server_name=server,
        tool_name=tool,
        mcp_identity_trusted=True,
        tool_input=tool_input,
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
        47,
        "tc-ledger-write",
        "kirocrew-work",
        "work_ledger_record",
        json.dumps({"action": "decide", "item_id": "it_9fc6cc99"}),
    )

    assert await transport.approve_tool(46) is True
    assert await transport.approve_tool(47) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("make_transport", _TRANSPORTS)
async def test_a_batch_refusal_settles_as_a_host_refusal_with_its_cause(
    tmp_path, monkeypatch, make_transport
):
    # A surface that settles requests through tool_permission (a subagent) must
    # see the guard's refusal as a host refusal with its cause and reason, so the
    # recovery path that collects refused outcomes gets them, not a bare
    # floor refusal.
    guard = _guard()
    from kiro_crew import tool_permission as tp

    monkeypatch.setattr(guard, "guard_settings", lambda: (True, ()))
    transport = make_transport(tmp_path)
    _feed(transport, _read_frame())
    _feed(transport, _write_frame())
    event = _write_permission(48)
    transport._permission_gate_events[48] = event
    # Production wraps the transport in a provider (subagents, the task runner);
    # the reason must reach the caller through the wrapper.
    wire = tp.AcpWire(_provider_for(transport))
    ask = MagicMock()
    ask.request_id = 48
    ask.event = event
    ask.wire = wire
    policy = MagicMock()
    policy.stopping = None

    settled = await tp._allow(ask, policy, "test_rung", "auto_approved", "rule")

    assert settled.outcome == "refused"
    assert settled.by == "host"
    assert settled.cause == guard.DENY_CAUSE_BATCH_READ_WRITE
    assert "batch guard" in settled.reason
    # A later floor refusal for another id is still a plain floor refusal.
    transport._permission_gate_events[49] = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST, request_id=49, title="x"
    )
    monkeypatch.setattr("kiro_crew.permission_floor.refusal_for", lambda *a, **k: "Blocked: floor")
    ask.request_id = 49
    ask.event = transport._permission_gate_events[49]
    settled = await tp._allow(ask, policy, "test_rung", "auto_approved", "rule")
    assert settled.outcome == "floor_refused"


def _provider_for(transport):
    from kiro_crew.acp.session_handle import AcpSessionHandle

    if isinstance(transport, AcpSessionHandle):
        from kiro_crew.acp.session_provider import AcpSessionProvider

        return AcpSessionProvider(transport, MagicMock())
    from kiro_crew.providers.acp import AcpProvider

    provider = AcpProvider()
    provider._client = transport
    return provider


@pytest.mark.parametrize("make_transport", _TRANSPORTS)
def test_a_kept_refusal_is_held_under_a_bounded_key(tmp_path, make_transport):
    guard = _guard()
    from kiro_crew.acp._dispatch_parts.tool_call_provenance import TOOL_CALL_CACHE_MAX_KEY_CHARS

    transport = make_transport(tmp_path)
    huge = "q" * (1024 * 1024)
    guard._keep_refusal(transport, huge, "why")
    assert all(len(k) <= TOOL_CALL_CACHE_MAX_KEY_CHARS for k in transport._batch_refusals)
    assert transport.take_batch_refusal(huge) == "why"
    assert transport.take_batch_refusal(huge) is None


def test_refusal_outcome_labels_a_guard_refusal_as_the_guard():
    # A consumer that writes its own SEL row for an approve_tool False reads the
    # kept reason, then asks refusal_outcome how to label its row.
    guard = _guard()
    from kiro_crew.permission_floor import OUTCOME_REJECTED_TRANSPORT_FLOOR

    outcome, metadata = guard.refusal_outcome("Blocked by the batch guard: ...")
    assert outcome == guard.OUTCOME_REJECTED_BATCH_GUARD
    assert metadata == {
        "mechanism": guard.AUDIT_MECHANISM,
        "cause": guard.DENY_CAUSE_BATCH_READ_WRITE,
    }

    # No kept reason means the floor refused: the plain floor label, no metadata.
    outcome, metadata = guard.refusal_outcome(None)
    assert outcome == OUTCOME_REJECTED_TRANSPORT_FLOOR
    assert metadata is None


# ── retries after a refusal ──


@pytest.mark.asyncio
@pytest.mark.parametrize("make_transport", _TRANSPORTS)
async def test_a_refused_write_resent_alone_is_approved_without_its_own_terminal(
    tmp_path, monkeypatch, make_transport
):
    # The refused write's own tool_call_update never arrives; once the read has
    # returned, the write the model resends alone starts a new batch.
    guard = _guard()
    monkeypatch.setattr(guard, "guard_settings", lambda: (True, ()))
    transport = make_transport(tmp_path)
    _with_steer(transport, monkeypatch)
    _feed(transport, _read_frame())
    _feed(transport, _write_frame())
    transport._permission_gate_events[60] = _write_permission(60)
    assert await transport.approve_tool(60) is False

    _feed(transport, _settled_frame("tc-read"))
    _next_step(transport._batch_window)
    _feed(transport, _write_frame("tc-write-again"))
    transport._permission_gate_events[61] = _write_permission(61, "tc-write-again")
    assert await transport.approve_tool(61) is True


def test_a_read_whose_terminal_is_lost_stops_holding_the_batch_open():
    # Bound chosen for a lost read terminal: a member with no terminal stops
    # counting as open after OPEN_MEMBER_TTL_SECS, so the next call starts a
    # new batch. Inside the bound the read still counts.
    guard = _guard()
    now = [1000.0]
    window = guard.BatchWindow(clock=lambda: now[0])
    window.dispatched("r", guard.READ)
    window.dispatched("w", guard.WRITE, _push("a1b2c3d4e5"))
    assert window.refusal_for("w", guard.WRITE) is not None
    window.settled("w")

    now[0] += guard.OPEN_MEMBER_TTL_SECS - 1
    window.dispatched("w2", guard.WRITE, _push("b2c3d4e5f6"))
    assert window.refusal_for("w2", guard.WRITE) is not None
    window.settled("w2")

    now[0] += 2
    window.dispatched("w3", guard.WRITE, _push("c3d4e5f6a7"))
    assert window.refusal_for("w3", guard.WRITE) is None


# ── its own audit label ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_name", "make_transport"),
    [
        ("kiro_crew.acp.client", lambda tmp_path: _client(tmp_path)),
        ("kiro_crew.acp.session_handle", lambda tmp_path: _handle()),
    ],
)
async def test_a_batch_refusal_has_its_own_log_line_and_audit_row(
    tmp_path, monkeypatch, caplog, module_name, make_transport
):
    guard = _guard()
    from kiro_crew import permission_floor

    monkeypatch.setattr(guard, "guard_settings", lambda: (True, ()))
    audit = MagicMock()
    monkeypatch.setattr(permission_floor.sel_mod, "sel", lambda: audit)
    transport = make_transport(tmp_path)
    _with_steer(transport, monkeypatch)
    _feed(transport, _read_frame())
    _feed(transport, _write_frame())
    transport._permission_gate_events[62] = _write_permission(62)

    with caplog.at_level("WARNING"):
        assert await transport.approve_tool(62) is False

    rows = [call.kwargs for call in audit.log_tool_invocation.call_args_list]
    assert [r["outcome"] for r in rows] == [guard.OUTCOME_REJECTED_BATCH_GUARD]
    assert rows[0]["source"] == "batch_guard"
    assert rows[0]["metadata"]["mechanism"] == "batch_write_guard"
    assert rows[0]["metadata"]["cause"] == guard.DENY_CAUSE_BATCH_READ_WRITE
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "batch guard refused" in text
    assert "security floor rejected" not in text


@pytest.mark.asyncio
async def test_a_settled_batch_refusal_writes_no_floor_refused_row(tmp_path, monkeypatch):
    guard = _guard()
    from kiro_crew import tool_permission as tp

    monkeypatch.setattr(guard, "guard_settings", lambda: (True, ()))
    transport = _client(tmp_path)
    _feed(transport, _read_frame())
    _feed(transport, _write_frame())
    event = _write_permission(63)
    transport._permission_gate_events[63] = event
    ask = MagicMock()
    ask.request_id = 63
    ask.event = event
    ask.wire = tp.AcpWire(_provider_for(transport))
    policy = MagicMock()
    policy.stopping = None

    settled = await tp._allow(ask, policy, "test_rung", "auto_approved", "rule")

    assert settled.outcome == "refused"
    policy.audit.approved.assert_not_called()


def test_an_inputless_refinement_keeps_a_members_known_class():
    # A title-only or kind-only refinement for a read must not erase its class.
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("r", guard.READ, "gh pr view 7")
    window.dispatched("w", guard.WRITE, _MERGE)
    guard.note_refined(window, _event(tool_call_id="r", is_shell=True, tool_name="shell"))
    assert window.refusal_for("w", guard.WRITE) is not None
