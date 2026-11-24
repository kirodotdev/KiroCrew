"""A held sub-agent run asks where its parent's denies cannot decide a call.

An agent whose spec denies a command gets its own grants only once the deny check
answers a clear no, so a command the check cannot read (a chain, a redirect, a
substitution) or a deny list Kiro Crew cannot read asks a person. A sub-agent run
held to that agent is given the same answer: its own spec grants do not answer the
call, and what its parent's denies can decide is judged as before.
"""

from __future__ import annotations

import pytest
from test_kas_engine_subagents_switch import setting  # noqa: F401 -- fixture
from test_kas_parent_limits import (  # noqa: F401 -- the fixtures register by import
    _PLAIN,
    _batch,
    _isolated,
    _judge,
    _shell,
    _stamped,
    _write,
    agents,
    sessions,
)

from kiro_crew.acp import kas_parent_limits
from kiro_crew.hooks import TOOL_ALLOW, TOOL_AUTO_APPROVE, TOOL_DENY

#: Grants the shell and denies one command: its other plain commands run unasked.
_BOSS = {
    **_PLAIN,
    "name": "boss",
    "allowedTools": ["execute_bash"],
    "toolsSettings": {"shell": {"deniedCommands": ["make clean"]}},
}
#: The same grant beside a deny entry Kiro Crew cannot read.
_UNREAD = {
    **_BOSS,
    "name": "unread",
    "toolsSettings": {"shell": {"deniedCommands": ["rm (?!-i).*"]}},
}
_CHILD = {**_PLAIN, "name": "shell", "allowedTools": ["execute_bash"]}

#: Commands the deny check cannot read: a chain, a redirect, a substitution.
_UNDECIDABLE = ["make build && make test", "echo hi > out.txt", "echo $(date)"]


@pytest.fixture
def held(agents, sessions):  # noqa: F811
    """``held(parent)``: the child's own batch, and its batch held to *parent*."""
    for spec in (_BOSS, _UNREAD, _CHILD):
        _write(agents, spec)

    async def _held(parent: str):
        own = await _batch("shell", "subagent:c1")
        sessions["dashboard:p"] = parent
        kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
        return own, await _batch("shell", "subagent:c1")

    return _held


async def _parents_own(parent: str, command: str):
    """The verdict on *parent*'s own call of *command*, in its own chat session."""
    batch = await _batch(parent, "dashboard:p")
    return _judge(_stamped(_shell(command), batch, parent))


@pytest.mark.asyncio
@pytest.mark.parametrize("command", _UNDECIDABLE)
async def test_the_childs_own_grant_does_not_answer_a_command_the_parent_cannot_decide(
    held, command
):
    own, batch = await held("boss")
    assert _judge(_stamped(_shell(command), own, "shell")).action == TOOL_AUTO_APPROVE

    verdict = _judge(_stamped(_shell(command), batch, "shell"))
    assert (verdict.action, verdict.tier) == (TOOL_ALLOW, "")
    assert "the denies of agent 'boss', under which this sub-agent runs" in verdict.reason
    # The parent's own call of the same command asks too.
    assert (await _parents_own("boss", command)).action == TOOL_ALLOW


@pytest.mark.asyncio
async def test_a_command_the_parent_can_decide_is_judged_as_before(held):
    _own, batch = await held("boss")
    for command in ("make build", 'git commit -m "msg"'):
        assert _judge(_stamped(_shell(command), batch, "shell")).action == TOOL_AUTO_APPROVE
        assert (await _parents_own("boss", command)).action == TOOL_AUTO_APPROVE
    refused = _judge(_stamped(_shell("make clean now"), batch, "shell"))
    assert (refused.action, refused.tier) == (TOOL_DENY, "spec-parent-limits")


@pytest.mark.asyncio
async def test_a_deny_list_kiro_crew_cannot_read_makes_the_childs_shell_ask(held):
    own, batch = await held("unread")
    assert _judge(_stamped(_shell("make build"), own, "shell")).action == TOOL_AUTO_APPROVE
    [record] = kas_parent_limits.projected_parent_limits(batch, "shell", "/work")
    assert record.unread_denies == {"execute_bash"}

    assert _judge(_stamped(_shell("make build"), batch, "shell")).action == TOOL_ALLOW
    assert (await _parents_own("unread", "make build")).action == TOOL_ALLOW
    # A read-only command is answered by the read-only row, for the parent and the child.
    for verdict in (
        _judge(_stamped(_shell("ls"), batch, "shell")),
        await _parents_own("unread", "ls"),
    ):
        assert (verdict.action, verdict.tier) == (TOOL_AUTO_APPROVE, "read-only")


@pytest.mark.asyncio
async def test_a_parent_with_no_deny_on_the_tool_leaves_the_childs_grant_alone(
    held, agents  # noqa: F811
):
    write_only = {"fs_write": {"fallbackAction": "deny", "allowedPaths": ["out/**"]}}
    _write(agents, {**_PLAIN, "name": "nowrite", "toolsSettings": write_only})
    _own, batch = await held("nowrite")
    for command in _UNDECIDABLE:
        assert _judge(_stamped(_shell(command), batch, "shell")).action == TOOL_AUTO_APPROVE


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["on", "off"])
async def test_the_answer_does_not_depend_on_the_sub_agent_setting(
    held, setting, value  # noqa: F811
):
    """The ``kas_subagents`` preview on or off answers as the unset default does above."""
    setting(value)
    _own, batch = await held("boss")
    assert _judge(_stamped(_shell(_UNDECIDABLE[0]), batch, "shell")).action == TOOL_ALLOW
    assert _judge(_stamped(_shell("make build"), batch, "shell")).action == TOOL_AUTO_APPROVE
    refused = _judge(_stamped(_shell("make clean now"), batch, "shell"))
    assert (refused.action, refused.tier) == (TOOL_DENY, "spec-parent-limits")


@pytest.mark.asyncio
async def test_a_write_the_parents_denied_paths_cannot_decide_asks(
    held, agents, monkeypatch  # noqa: F811
):
    from test_tool_call_classification_kas import _kas_builtin_flow

    from kiro_crew.platform import spec_parent_gate

    paths = {"fs_write": {"deniedPaths": ["secret/**"]}}
    nosecret = {**_PLAIN, "name": "nosecret", "allowedTools": ["fs_write"], "toolsSettings": paths}
    _write(agents, nosecret)
    _write(agents, {**_PLAIN, "name": "shell", "allowedTools": ["fs_write"]})

    def write(path: str):
        raw = {"path": path, "command": "create"}
        return _kas_builtin_flow("fs_write", title="Write", kind="edit", raw_input=raw)

    _own, batch = await held("nosecret")
    clear = _stamped(write("/work/notes.txt"), batch, "shell")
    assert _judge(clear).action == TOOL_AUTO_APPROVE
    denied = _judge(_stamped(write("/work/secret/key"), batch, "shell"))
    assert (denied.action, denied.tier) == (TOOL_DENY, "spec-parent-limits")
    # A target the deny check cannot resolve (a stalled symlink walk, say) is unknown.
    monkeypatch.setattr(spec_parent_gate, "_write_denied", lambda call, record: None)
    assert _judge(clear).action == TOOL_ALLOW


def _fetch(url: str):
    from test_tool_call_classification_kas import _kas_builtin_flow

    return _kas_builtin_flow("web_fetch", title="Fetch", kind="fetch", raw_input={"url": url})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocked",
    [["https://a.example/(x|y)"], [r"https://a\.example/private/.*", "https://a.example/(x|y)"]],
    ids=["no-readable-entry", "one-readable-entry"],
)
async def test_a_blocked_list_kiro_crew_cannot_read_takes_the_childs_fetch_grant(
    held, agents, blocked  # noqa: F811
):
    """The parent's trusted fetches wait on a blocked list read whole, so its own
    trusted list does not answer them, and the child's own trusted list does not
    either: the child's fetch gets the verdict the parent's gets, from the row
    that answers the parent's (here the read-only classifier, which reads a fetch)."""
    trusted = {"trusted": [r"https://a\.example/.*"]}
    webby = {"web_fetch": {**trusted, "blocked": blocked}}
    _write(agents, {**_PLAIN, "name": "webby", "toolsSettings": webby})
    _write(agents, {**_PLAIN, "name": "shell", "toolsSettings": {"web_fetch": trusted}})
    fetch = _fetch("https://a.example/x")

    own, batch = await held("webby")
    assert _judge(_stamped(fetch, own, "shell")).tier == "spec-builtin-grant"
    [record] = kas_parent_limits.projected_parent_limits(batch, "shell", "/work")
    assert "web_fetch" in record.unread_denies
    verdict = _judge(_stamped(fetch, batch, "shell"))
    parents = _judge(_stamped(fetch, await _batch("webby", "dashboard:p"), "webby"))
    assert verdict.tier == parents.tier != "spec-builtin-grant"
    assert verdict.action == parents.action
