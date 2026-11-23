"""With the KAS sub-agents preview on, a KAS sub-agent run is approved as its founding chat is.

A run that names another agent than the chat that founded its chain runs unasked only
what that chat's agent runs unasked; the grants in its own agent file do nothing; every
``ask`` and ``deny`` of every agent above it, and of its own file, holds. Preview off,
every other session and every other harness: nothing changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_kas_parent_limits import _judge, _rules, _shell, _stamped, _write

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
import kiro_crew.subagent as subagent_mod
from kiro_crew import mate_held_rules, mate_worker
from kiro_crew.acp import kas_agents, kas_engine_subagents, kas_parent_grants, kas_parent_limits
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.kas_permissions import (
    CREW_ANSWERED_BUILTINS_KEY,
    CREW_ANSWERED_COMMANDS_KEY,
    CREW_SHELL_ALLOWLIST_KEY,
)
from kiro_crew.acp.session_handle import AcpAgentUnavailable
from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp

_SHELL_ASK = {"capability": "shell", "effect": "ask"}
_BASE = {"prompt": "p", "tools": ["*"]}
#: Runs the shell unasked.
_BROAD = {**_BASE, "name": "broad", "allowedTools": ["execute_bash"]}
#: Runs the shell and fetches unasked.
_WIDE = {**_BASE, "name": "wide", "allowedTools": ["execute_bash", "web_fetch"]}
#: Runs nothing unasked.
_NARROW = {**_BASE, "name": "narrow"}
_PLAIN = {**_BASE, "name": "plain"}
#: A file that claims the shell and file writes for itself.
_CLAIMS = {**_BASE, "name": "claims", "allowedTools": ["execute_bash", "fs_write"]}
_ASKS = {**_BASE, "name": "asks", "permissions": {"rules": [_SHELL_ASK]}}
#: Its own file lets the shell run ``echo`` and ``touch`` only, and refuses one ``touch``.
_LIMITED = {
    **_BASE,
    "name": "limited",
    "toolsSettings": {
        "shell": {
            "denyByDefault": True,
            "allowedCommands": ["echo .*", "touch .*"],
            "deniedCommands": ["touch secret.*"],
        }
    },
}
_SPECS = (_BROAD, _WIDE, _NARROW, _PLAIN, _CLAIMS, _ASKS, _LIMITED)
_WRITE_COMMAND = "touch marker"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    yield
    gp.reset_store()
    ctx_mod.reset_context()


@pytest.fixture
def agents(monkeypatch, tmp_path) -> Path:
    user = tmp_path / "home" / "agents"
    user.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    for spec in _SPECS:
        _write(user, spec)
    return user


@pytest.fixture
def sessions(monkeypatch) -> dict[str, Any]:
    """Session key -> the agent that session runs; an exception is raised instead."""
    known: dict[str, Any] = {}

    def template(key: str) -> str:
        if isinstance(known.get(key), Exception):
            raise known[key]
        return known.get(key, "")

    monkeypatch.setattr(subagent_mod, "_parent_template_for_spawn", template)
    return known


@pytest.fixture
def stored():
    """Store the "KAS sub-agents" Feature Preview in the real ``config.json`` of the
    test's own data home, the way the dashboard does. ``None`` leaves it never set.
    Nothing is replaced: ``preview_on`` and the switch's own helper read the file."""
    from kiro_crew.config.loader import config_path

    def store(value: Any) -> None:
        path = Path(config_path())
        body = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        previews = body.setdefault("dashboard", {}).setdefault("feature_previews", {})
        previews.pop(kas_engine_subagents.KAS_SUBAGENTS_PREVIEW, None)
        if value is not None:
            previews[kas_engine_subagents.KAS_SUBAGENTS_PREVIEW] = value
        fresh = path.with_name("config.json.new")
        fresh.write_text(json.dumps(body), encoding="utf-8")
        fresh.replace(path)

    return store


@pytest.fixture
def preview(stored) -> None:
    stored(True)


async def _batch(agent: str, key: str, work_dir: Path | None = None) -> list[dict]:
    extras = await KasHarness().session_extras(agent, work_dir=work_dir, session_key=key)
    return list(extras.custom_agents)


async def _child(
    sessions, parent: str, child: str, key: str = "subagent:c1", work_dir: Path | None = None
) -> list[dict]:
    sessions["dashboard:p"] = parent
    kas_parent_limits.note_parent(key, "dashboard:p")
    return await _batch(child, key, work_dir)


@pytest.fixture
def one_directory(monkeypatch, tmp_path) -> Path:
    """Every session above a run is known to run in one directory, the one returned:
    pass it as the run's ``work_dir`` to start the run there too."""
    where = tmp_path / "work"
    where.mkdir()
    monkeypatch.setattr(subagent_mod, "parent_project_dirs", lambda key: (str(where),))
    return where


def _own(batch: list[dict], agent: str) -> dict:
    return next(entry for entry in batch if entry["id"] == agent)


def _answer(batch: list[dict], agent: str, command: str = _WRITE_COMMAND):
    return _judge(_stamped(_shell(command), batch, agent))


def _allows(batch: list[dict], agent: str) -> set[str]:
    return {r["capability"] for r in _rules(batch, agent) if r.get("effect") == "allow"}


# ── the rule ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_child_runs_unasked_what_its_founding_agent_grants(agents, sessions, preview):
    batch = await _child(sessions, "broad", "plain")

    assert _own(batch, "plain")[CREW_ANSWERED_BUILTINS_KEY] == ["execute_bash"]
    verdict = _answer(batch, "plain")
    assert (verdict.action, verdict.tier) == (TOOL_AUTO_APPROVE, "spec-builtin-grant")


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", ["narrow", "broad"], ids=["under-narrow", "under-broad"])
async def test_a_childs_own_grants_do_nothing(agents, sessions, preview, parent):
    """The impostor cell: a file that claims grants for itself gains what its
    founder holds and nothing more."""
    batch = await _child(sessions, parent, "claims")

    held = _own(batch, "claims").get(CREW_ANSWERED_BUILTINS_KEY, [])
    assert held == (["execute_bash"] if parent == "broad" else [])
    assert (_answer(batch, "claims").action == TOOL_AUTO_APPROVE) is (parent == "broad")


@pytest.mark.asyncio
async def test_a_childs_own_ask_holds_against_its_founders_grant(agents, sessions, preview):
    batch = await _child(sessions, "broad", "asks")

    assert _SHELL_ASK in _rules(batch, "asks")
    assert CREW_ANSWERED_BUILTINS_KEY not in _own(batch, "asks")
    assert _answer(batch, "asks").action != TOOL_AUTO_APPROVE


@pytest.mark.asyncio
async def test_an_ask_of_an_agent_above_reaches_the_run(agents, sessions, preview):
    """The founder's own ask beside its grant, and a middle agent's ask, two levels down."""
    _write(agents, {**_BROAD, "name": "careful", "permissions": {"rules": [_SHELL_ASK]}})
    founder = await _child(sessions, "careful", "plain")
    assert _SHELL_ASK in _rules(founder, "plain")
    assert _answer(founder, "plain").action != TOOL_AUTO_APPROVE

    sessions["subagent:mid"] = "asks"
    kas_parent_limits.note_parent("subagent:mid", "dashboard:p")
    kas_parent_limits.note_parent("subagent:leaf", "subagent:mid")
    sessions["dashboard:p"] = "broad"
    leaf = await _batch("plain", "subagent:leaf")
    assert _SHELL_ASK in _rules(leaf, "plain")
    assert _answer(leaf, "plain").action != TOOL_AUTO_APPROVE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "root, middle, runs",
    [("broad", "narrow", True), ("narrow", "claims", False)],
    ids=["root-grants", "only-the-middle-grants"],
)
async def test_a_grandchild_runs_on_the_roots_grants(agents, sessions, preview, root, middle, runs):
    sessions["dashboard:p"], sessions["subagent:mid"] = root, middle
    kas_parent_limits.note_parent("subagent:mid", "dashboard:p")
    kas_parent_limits.note_parent("subagent:leaf", "subagent:mid")

    leaf = await _batch("plain", "subagent:leaf")
    assert (_answer(leaf, "plain").action == TOOL_AUTO_APPROVE) is runs


@pytest.mark.asyncio
async def test_two_founders_grant_only_what_both_hold(agents, sessions, preview):
    sessions["dashboard:p"], sessions["dashboard:q"] = "wide", "broad"
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    kas_parent_limits.note_parent("subagent:c1", "dashboard:q")

    batch = await _batch("plain", "subagent:c1")
    assert _own(batch, "plain")[CREW_ANSWERED_BUILTINS_KEY] == ["execute_bash"]
    assert "web_fetch" not in _allows(batch, "plain")

    sessions["dashboard:q"] = "narrow"
    batch = await _batch("plain", "subagent:c1")
    assert CREW_ANSWERED_BUILTINS_KEY not in _own(batch, "plain")
    assert _allows(batch, "plain") == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("withheld_for", ["dashboard:p", "subagent:c1"], ids=["founder", "run"])
async def test_a_grant_passes_the_founders_ceiling_and_the_runs(
    agents, sessions, preview, monkeypatch, withheld_for
):
    """The impostor cell: a founder whose own session is withheld a grant cannot
    hand it to a run, and a run's own ceiling withholds it too."""
    real = kas_agents._authored_allow_gate

    def gate(session_key: str, governance_app):
        permits = real(session_key, governance_app)
        return lambda ref: permits(ref) and not (ref == "web_fetch" and session_key == withheld_for)

    monkeypatch.setattr(kas_agents, "_authored_allow_gate", gate)
    batch = await _child(sessions, "wide", "plain")
    assert "web_fetch" not in _allows(batch, "plain")
    assert _own(batch, "plain")[CREW_ANSWERED_BUILTINS_KEY] == ["execute_bash"]

    monkeypatch.setattr(kas_agents, "_authored_allow_gate", real)
    assert "web_fetch" in _allows(await _batch("plain", "subagent:c1"), "plain")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "founders, runs, reaches",
    [
        ({"command": "srv", "args": ["a"]}, {"command": "srv", "args": ["a"]}, True),
        ({"command": "srv", "args": ["a"]}, {"command": "other", "args": ["a"]}, True),
        (None, {"command": "srv", "args": ["a"]}, True),
        ({"command": "srv", "args": ["a"]}, None, False),
    ],
    ids=["held-alike", "another-command", "founder-holds-none", "run-holds-none"],
)
async def test_a_grant_by_server_name_reaches_the_runs_server_of_that_name(
    agents, sessions, preview, monkeypatch, founders, runs, reaches
):
    """As on kiro-cli: the run's own server of that name answers unasked, whichever
    program it is and whether or not the founding agent holds a server of that name."""
    monkeypatch.setattr(kas_agents, "_project_mcp_servers", lambda *a, **k: {})
    parent = {**_BASE, "name": "names", "allowedTools": ["@casrv", "execute_bash"]}
    if founders is not None:
        parent["mcpServers"] = {"casrv": {**founders, "autoApprove": ["look"]}}
    _write(agents, parent)
    _write(agents, {**_BASE, "name": "holds", **({"mcpServers": {"casrv": runs}} if runs else {})})

    batch = await _child(sessions, "names", "holds")
    matches = [m for r in _rules(batch, "holds") if r["effect"] == "allow" for m in r["match"]]
    assert (matches != []) is reaches
    assert all(m.startswith("casrv/") for m in matches)
    # The founder's grant that names no server travels either way.
    assert _answer(batch, "holds").action == TOOL_AUTO_APPROVE


#: Every kind of limit the gate reads in an agent's own file.
_OWN_LIMITS = {
    "shell": {
        "denyByDefault": True,
        "allowedCommands": ["echo .*", "touch .*"],
        "deniedCommands": ["touch secret.*"],
    },
    "fs_write": {
        "fallbackAction": "deny",
        "allowedPaths": ["out/**"],
        "deniedPaths": ["out/secret/**"],
    },
    "web_fetch": {"blocked": [r"https://example\.com/private/.*"]},
}
_GRANTS = ["execute_bash", "fs_write", "web_fetch"]
_OWNS = {**_BASE, "name": "owns", "toolsSettings": _OWN_LIMITS}
#: The same limits beside grants of its own. Listing the shell lifts its
#: ``denyByDefault``, as on kiro-cli: a command off the allowlist asks.
_OWNS_AND_GRANTS = {**_OWNS, "name": "owns-and-grants", "allowedTools": _GRANTS}
#: Grants everything, so only the run's own limits can refuse or ask.
_GENEROUS = {**_BASE, "name": "generous", "allowedTools": _GRANTS}
_OWN_SPEC = "Blocked by this agent's spec: "
_NOT_ALLOWLISTED = (
    _OWN_SPEC + "toolsSettings.execute_bash.denyByDefault is set and the command matches "
    "none of its allowedCommands"
)


def _own_limit_calls() -> dict[str, Any]:
    """One call per kind of limit in ``_OWN_LIMITS``, and one each limit lets through."""
    from test_tool_call_classification_kas import _kas_builtin_flow

    def write(path: str):
        raw = {"path": path, "command": "create"}
        return _kas_builtin_flow("fs_write", title="Write", kind="edit", raw_input=raw)

    def fetch(url: str):
        return _kas_builtin_flow("web_fetch", title="Fetch", kind="fetch", raw_input={"url": url})

    return {
        "off-the-shell-allowlist": _shell("mkdir d1"),
        "off-the-shell-allowlist-composed": _shell("echo $(rm x)"),
        "denied-command": _shell("touch secret"),
        "off-the-write-allowlist": write("/work/elsewhere.txt"),
        "denied-path": write("/work/out/secret/key"),
        "blocked-url": fetch("https://example.com/private/x"),
        "on-the-shell-allowlist": _shell(_WRITE_COMMAND),
        "on-the-write-allowlist": write("/work/out/a.txt"),
        "another-url": fetch("https://example.com/public"),
    }


#: The run's OWN denyByDefault list refusing a command, not a parent's limit.
_OWN_LIST_TIER = "spec-shell-allowlist"
_OWN_LIST_TEXT = "toolsSettings.execute_bash.denyByDefault is set"


def _verdicts(batch: list[dict], agent: str) -> dict[str, tuple[str, str, str]]:
    out = {}
    for name, call in _own_limit_calls().items():
        verdict = _judge(_stamped(call, batch, agent))
        out[name] = (verdict.action, verdict.tier, verdict.reason)
    return out


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", ["generous", "narrow"])
@pytest.mark.parametrize("child", ["owns", "owns-and-grants"])
async def test_the_runs_own_limits_decide_the_same_calls_with_the_preview_on_and_off(
    agents, sessions, stored, parent, child
):
    """What the run's own file refuses is refused by the same row with the same
    text, and what it leaves asked is asked. No call turns from refused to asked or
    run, or back. The one change: what its own grants ran unasked now asks, unless
    its founder grants it."""
    for spec in (_OWNS, _OWNS_AND_GRANTS, _GENEROUS):
        _write(agents, spec)
    off = _verdicts(await _child(sessions, parent, child), child)
    stored(True)
    on = _verdicts(await _child(sessions, parent, child), child)

    refused = {name for name, verdict in off.items() if verdict[0] == TOOL_DENY}
    assert refused == {
        "denied-command",
        "off-the-write-allowlist",
        "denied-path",
        "blocked-url",
        *(
            ("off-the-shell-allowlist", "off-the-shell-allowlist-composed")
            if child == "owns"
            else ()
        ),
    }
    # With the preview on, the run's own denyByDefault list holds beside its own
    # shell grant, so the two calls off that list are refused by the list itself.
    own_list = (
        {"off-the-shell-allowlist", "off-the-shell-allowlist-composed"}
        if child == "owns-and-grants"
        else set()
    )
    assert {name for name, verdict in on.items() if verdict[0] == TOOL_DENY} == refused | own_list
    assert {name: on[name] for name in refused} == {name: off[name] for name in refused}
    for name in own_list:
        assert on[name][1] == _OWN_LIST_TIER
        assert _OWN_LIST_TEXT in on[name][2]
    changed = {name for name in off if on[name][0] != off[name][0]}
    assert changed == ({"on-the-shell-allowlist"} if parent == "narrow" else set()) | own_list
    assert off["on-the-shell-allowlist"][0] == TOOL_AUTO_APPROVE
    if (parent, child) == ("generous", "owns-and-grants"):
        # The founder grants all the run's own file grants: nothing else changes.
        assert {k: v for k, v in on.items() if k not in own_list} == {
            k: v for k, v in off.items() if k not in own_list
        }


@pytest.mark.asyncio
async def test_an_own_deny_by_default_beside_an_own_shell_grant_asks_off_and_is_refused_on(
    agents, sessions, stored
):
    """The run's own file lists the shell and sets ``denyByDefault``. With the preview
    off a command off its allowlist asks. With the preview on, under a founder that
    grants the shell, its own list refuses it: never run unasked, as a design that
    kept the run's limits apart from the founder's grant did."""
    for spec in (_OWNS_AND_GRANTS, _GENEROUS):
        _write(agents, spec)
    stored(False)
    off = await _child(sessions, "generous", "owns-and-grants")
    stored(True)
    on = await _child(sessions, "generous", "owns-and-grants")

    for command in ("mkdir d1", "echo $(rm x)"):
        before = _answer(off, "owns-and-grants", command).action
        assert before not in (TOOL_AUTO_APPROVE, TOOL_DENY)
        refused = _answer(on, "owns-and-grants", command)
        assert (refused.action, refused.tier) == (TOOL_DENY, _OWN_LIST_TIER)
        assert _OWN_LIST_TEXT in refused.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [False, True], ids=["preview-off", "preview-on"])
@pytest.mark.parametrize("parent", ["broad", "limited-too"])
async def test_an_own_allowlist_refusal_keeps_its_row_and_text(
    agents, sessions, stored, parent, on
):
    """The exact row and text of a refusal the run's own file makes, the same with
    the preview on and off, under a founder with no limit and under one whose
    allowlist equals the run's own."""
    _write(agents, {**_LIMITED, "name": "limited-too"})
    stored(on)
    batch = await _child(sessions, parent, "limited")

    assert _own(batch, "limited")[CREW_SHELL_ALLOWLIST_KEY] == ["echo .*", "touch .*"]
    verdict = _answer(batch, "limited", "mkdir d1")
    assert (verdict.action, verdict.tier, verdict.reason) == (
        TOOL_DENY,
        "spec-shell-allowlist",
        _NOT_ALLOWLISTED,
    )
    denied = _answer(batch, "limited", "touch secret")
    assert (denied.action, denied.tier, denied.reason) == (
        TOOL_DENY,
        "spec-denies",
        _OWN_SPEC + "the command matches its deniedCommands",
    )
    # A founder's grant of commands is bound to its directory, which is not known here.
    runs = parent == "broad" or not on
    assert (_answer(batch, "limited").action == TOOL_AUTO_APPROVE) is runs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parent, child",
    [("fenced", "plain"), ("broad", "fenced-child"), ("fenced", "fenced-child")],
    ids=["the-founders-deny", "the-runs-own-deny", "both"],
)
@pytest.mark.parametrize("command", ["echo $(rm x)", "touch `rm x`", "touch a > out.txt"])
async def test_a_command_a_deny_cannot_decide_asks(
    agents, sessions, preview, parent, child, command
):
    """A deny reads a command it can split. One it cannot (a substitution, a
    redirect) is neither refused nor answered by the founder's grant: it asks, as
    the founding chat's own call does. The founder's deny list and the run's own
    are both read, under whichever spelling each file filed them."""
    _write(
        agents,
        {**_BROAD, "name": "fenced", "toolsSettings": {"shell": {"deniedCommands": ["rm .*"]}}},
    )
    own = {"execute_bash": {"deniedCommands": ["shred .*"]}}
    _write(agents, {**_BASE, "name": "fenced-child", "toolsSettings": own})
    batch = await _child(sessions, parent, child)

    assert _answer(batch, child, command).action not in (TOOL_AUTO_APPROVE, TOOL_DENY)
    # What a deny can decide: refused when it matches, answered when none does.
    assert (_answer(batch, child, "rm x").action == TOOL_DENY) is (parent == "fenced")
    assert (_answer(batch, child, "shred x").action == TOOL_DENY) is (child == "fenced-child")
    assert _answer(batch, child).action == TOOL_AUTO_APPROVE
    if parent == "fenced":
        founders_own = await _batch("fenced", "dashboard:p")
        asked = _answer(founders_own, "fenced", command).action
        assert asked not in (TOOL_AUTO_APPROVE, TOOL_DENY)


def _fenced(agents) -> None:
    _write(
        agents,
        {**_BROAD, "name": "fenced", "toolsSettings": {"shell": {"deniedCommands": ["rm .*"]}}},
    )


_FENCED_REFUSAL = (
    TOOL_DENY,
    "spec-parent-limits",
    "Blocked by the spec of agent 'fenced', under which this sub-agent runs: "
    "the command matches its deniedCommands",
)


@pytest.mark.asyncio
async def test_a_founders_refusal_is_its_parent_records_never_run_on_its_grant(
    agents, sessions, preview
):
    """The founder allows the shell and refuses ``rm``. Its grant travels; its
    refusal does not travel with it. The record the run carries refuses, on the row
    and with the text it has with the preview off, before any grant row is read."""
    import kiro_crew.hooks as hooks_mod

    _fenced(agents)
    batch = await _child(sessions, "fenced", "plain")

    refused = _answer(batch, "plain", "rm x")
    assert (refused.action, refused.tier, refused.reason) == _FENCED_REFUSAL
    assert _answer(batch, "plain").action == TOOL_AUTO_APPROVE
    # The founder's deny list is not on the run's own entry: one record holds it.
    assert "deniedCommands" not in json.dumps(
        {
            k: v
            for k, v in _own(batch, "plain").items()
            if k != kas_parent_limits.CREW_PARENT_LIMITS_KEY
        }
    )
    # No gate can read the grant first: a table with a grant row before a refusing
    # row is not a sound table, and the gate then refuses every call.
    names = [tier.name for tier in hooks_mod.GATE_TIERS]
    assert names.index("spec-parent-limits") < names.index("spec-builtin-grant")
    tiers = list(hooks_mod.GATE_TIERS)
    tiers.insert(names.index("spec-parent-limits"), tiers.pop(names.index("spec-builtin-grant")))
    assert hooks_mod.gate_tier_problems(tiers)


@pytest.mark.asyncio
@pytest.mark.parametrize("child", ["plain", "claims"])
@pytest.mark.parametrize(
    "command, refused",
    [
        ("echo a && touch b", False),
        ("touch a > out.txt", False),
        ("echo $(rm x)", False),
        ("touch `rm x`", False),
        ("echo a && rm x", True),
    ],
)
async def test_a_command_the_founders_refusal_cannot_decide_asks_on_and_off(
    agents, sessions, stored, child, command, refused
):
    """A chain, a redirect, a substitution: the founder's own call asks, and the run
    asks with the preview on as with it off, whatever its own file grants. A chain
    the record can split is refused when one of its commands is, both ways."""
    _fenced(agents)
    stored(False)
    off = _answer(await _child(sessions, "fenced", child), child, command)
    stored(True)
    on = _answer(await _child(sessions, "fenced", child), child, command)

    assert off.action != TOOL_AUTO_APPROVE
    assert (off.action == TOOL_DENY) is refused
    assert (on.action, on.tier, on.reason) == (off.action, off.tier, off.reason)


#: One founder per kind of refusal, each beside grants of the tools it limits, with
#: the call it refuses. ``{root}`` is the directory both sessions run in.
_REFUSALS: dict[str, tuple[dict[str, Any], str, str]] = {
    "denied-commands": ({"shell": {"deniedCommands": ["rm .*"]}}, "shell", "rm x"),
    "denied-paths": ({"fs_write": {"deniedPaths": ["secret/**"]}}, "write", "{root}/secret/key"),
    "write-allowlist": (
        {"fs_write": {"fallbackAction": "deny", "allowedPaths": ["out/**"]}},
        "write",
        "{root}/elsewhere.txt",
    ),
    "shell-allowlist": (
        {"shell": {"denyByDefault": True, "allowedCommands": ["echo .*"]}},
        "shell",
        "mkdir d1",
    ),
    "blocked-address": (
        {"web_fetch": {"blocked": [r"https://example\.com/private/.*"]}},
        "fetch",
        "https://example.com/private/x",
    ),
}


def _refused_call(kind: str, target: str):
    from test_tool_call_classification_kas import _kas_builtin_flow

    if kind == "shell":
        return _shell(target)
    if kind == "write":
        raw = {"path": target, "command": "create"}
        return _kas_builtin_flow("fs_write", title="Write", kind="edit", raw_input=raw)
    return _kas_builtin_flow("web_fetch", title="Fetch", kind="fetch", raw_input={"url": target})


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", sorted(_REFUSALS))
@pytest.mark.parametrize("directory", ["known", "unknown"])
async def test_each_kind_of_founders_refusal_is_the_same_with_the_preview_on_and_off(
    agents, sessions, stored, monkeypatch, tmp_path, refusal, directory
):
    """A founder's denied command, denied path, write allowlist, shell allowlist and
    blocked address: the run is refused by the same row with the same text, naming
    the founder, with the preview on and off, in the founder's directory and with
    none known. The run's own file grants every tool, and so does the founder."""
    settings, kind, target = _REFUSALS[refusal]
    root = str(tmp_path)
    dirs = (root,) if directory == "known" else ()
    monkeypatch.setattr(subagent_mod, "parent_project_dirs", lambda key: dirs)
    grants = ["fs_write", "web_fetch"] if refusal == "shell-allowlist" else _GRANTS
    _write(agents, {**_BASE, "name": "refuser", "allowedTools": grants, "toolsSettings": settings})
    _write(agents, {**_BASE, "name": "grants-all", "allowedTools": _GRANTS})
    sessions["dashboard:p"] = "refuser"
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    call = _refused_call(kind, target.format(root=root))

    verdicts = []
    for value in (False, True):
        stored(value)
        extras = await KasHarness().session_extras(
            "grants-all", work_dir=tmp_path, session_key="subagent:c1"
        )
        verdict = _judge(_stamped(call, list(extras.custom_agents), "grants-all", cwd=root))
        verdicts.append((verdict.action, verdict.tier, verdict.reason))
    off, on = verdicts
    assert off[:2] == (TOOL_DENY, "spec-parent-limits"), off
    assert "the spec of agent 'refuser', under which this sub-agent runs" in off[2]
    assert on == off


@pytest.mark.asyncio
async def test_a_founders_engine_deny_rule_is_on_the_runs_entry_on_and_off(
    agents, sessions, stored
):
    deny = {"capability": "shell", "effect": "deny", "match": ["rm *"]}
    _write(agents, {**_BROAD, "name": "ruled", "permissions": {"rules": [deny]}})
    for value in (False, True):
        stored(value)
        assert deny in _rules(await _child(sessions, "ruled", "claims"), "claims"), value


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [False, True], ids=["preview-off", "preview-on"])
async def test_a_session_between_refuses_with_the_preview_on_as_off(
    agents, sessions, stored, value
):
    """A middle run's own refusal holds the leaf, under the middle agent's name,
    whatever the founder grants."""
    _fenced(agents)
    stored(value)
    sessions.update({"dashboard:p": "broad", "subagent:mid": "fenced"})
    kas_parent_limits.note_parent("subagent:mid", "dashboard:p")
    kas_parent_limits.note_parent("subagent:leaf", "subagent:mid")

    leaf = await _batch("claims", "subagent:leaf")
    refused = _answer(leaf, "claims", "rm x")
    assert (refused.action, refused.tier, refused.reason) == _FENCED_REFUSAL
    assert _answer(leaf, "claims").action == TOOL_AUTO_APPROVE


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [False, True], ids=["preview-off", "preview-on"])
async def test_a_founder_whose_limits_cannot_be_read_stops_the_run_on_and_off(
    agents, sessions, stored, monkeypatch, value
):
    """The record that refuses for the founder cannot be built: the run is not
    started, with the preview on as with it off, so no grant travels without it."""
    _fenced(agents)
    stored(value)

    def unreadable(agent: str, parent_key: str, cwd: str):
        raise OSError("agent file unreadable")

    monkeypatch.setattr(kas_parent_limits, "_parent_limits", unreadable)
    with pytest.raises(AcpAgentUnavailable, match="could not be read"):
        await _child(sessions, "fenced", "claims")


@pytest.mark.asyncio
async def test_a_founders_allowlist_refuses_under_the_founders_name(agents, sessions, preview):
    batch = await _child(sessions, "limited", "plain")

    assert CREW_SHELL_ALLOWLIST_KEY not in _own(batch, "plain")
    verdict = _answer(batch, "plain", "mkdir d1")
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-parent-limits")
    assert "the spec of agent 'limited', under which this sub-agent runs" in verdict.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "founder_dirs, run_dir, answered",
    [
        (("base",), "base", True),
        (("base",), "base/sub", False),
        (("base/a",), "base/b", False),
        ((), "base", False),
        (("base", "base/other"), "base", False),
    ],
    ids=["equal", "a-subdirectory", "a-sibling", "unknown", "two-known-directories"],
)
async def test_a_grant_of_commands_or_paths_reaches_a_run_in_the_founders_directory_only(
    agents, sessions, preview, monkeypatch, tmp_path, founder_dirs, run_dir, answered
):
    """A command or path grant names files relative to where a session runs: in
    another directory it would cover what the founding chat is asked for."""
    for name in ("base/sub", "base/a", "base/b", "base/other"):
        (tmp_path / name).mkdir(parents=True)
    dirs = tuple(str(tmp_path / d) for d in founder_dirs)
    monkeypatch.setattr(subagent_mod, "parent_project_dirs", lambda key: dirs)
    settings = {
        "shell": {"allowedCommands": ["touch .*"]},
        "fs_write": {"allowedPaths": ["out/**"]},
    }
    _write(agents, {**_BASE, "name": "scoped", "toolsSettings": settings})
    sessions["dashboard:p"] = "scoped"
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    extras = await KasHarness().session_extras(
        "plain", work_dir=tmp_path / run_dir, session_key="subagent:c1"
    )

    own = _own(list(extras.custom_agents), "plain")
    assert (own.get(CREW_ANSWERED_COMMANDS_KEY) == ["touch .*"]) is answered
    assert ("_crewAnsweredPaths" in own) is answered
    # A grant of the whole tool names no directory, so it reaches the run anywhere.
    whole = await _child(sessions, "broad", "plain", "subagent:c2")
    assert _own(whole, "plain")[CREW_ANSWERED_BUILTINS_KEY] == ["execute_bash"]


_GUARD = {"matcher": "execute_bash", "command": "guard.sh"}
_SECOND = {"matcher": "fs_write", "command": "second.sh"}
_GUARD_DOCUMENT = {
    "name": "guard",
    "trigger": "PreToolUse",
    "matcher": "execute_bash",
    "action": {"type": "command", "command": "guard.sh"},
}


def _pre(*entries: dict) -> dict:
    return {"preToolUse": list(entries)}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "theirs, ours, hands_down",
    [
        (_pre(_GUARD), {}, False),
        (_pre(_GUARD), _pre(_GUARD), True),
        (_pre(_GUARD), [_GUARD_DOCUMENT], True),
        (_pre(_GUARD), _pre({**_GUARD, "matcher": "fs_write"}), False),
        (_pre(_GUARD), _pre({**_GUARD, "command": "other.sh"}), False),
        (_pre(_GUARD), _pre({**_GUARD, "timeout_ms": 5000}), False),
        (_pre(_GUARD, _SECOND), _pre(_GUARD), False),
        (_pre(_GUARD), _pre(_SECOND, _GUARD), True),
        ([_GUARD_DOCUMENT], [{**_GUARD_DOCUMENT, "enabled": False}], False),
        (_pre(_GUARD), "not hooks", False),
        ("not hooks", {}, False),
        ({"postToolUse": [{"matcher": "execute_bash", "command": "log.sh"}]}, {}, True),
        ({}, _pre(_GUARD), True),
    ],
    ids=[
        "the-run-lacks-it",
        "the-same-entry",
        "the-same-entry-in-the-other-form",
        "another-matcher",
        "another-command",
        "another-timeout",
        "two-and-the-run-has-one",
        "the-run-has-more",
        "the-runs-entry-is-disabled",
        "the-runs-hooks-unreadable",
        "the-founders-hooks-unreadable",
        "a-post-tool-hook-only",
        "no-founder-hook",
    ],
)
async def test_a_founders_own_pre_tool_hook_hands_down_grants_only_to_a_run_that_carries_it(
    agents, sessions, preview, one_directory, theirs, ours, hands_down
):
    """A spec's own hook runs on its own session's calls. A founder's hook the run
    lacks would stop a call in the founding chat and not in the run, so that founder
    hands down no grant. The same whole entry, enabled, in the run's own spec runs
    on the run's calls, so the grants travel. A hook of another event stops no call."""
    _write(agents, {**_WIDE, "name": "guarded", "hooks": theirs})
    _write(agents, {**_PLAIN, "name": "carries", "hooks": ours})
    # The same grants with no hook in the way travel in every case.
    control = await _child(sessions, "wide", "plain", "subagent:c2")
    assert _answer(control, "plain").action == TOOL_AUTO_APPROVE

    try:
        batch = await _child(sessions, "guarded", "carries", work_dir=one_directory)
    except AcpAgentUnavailable:
        assert not hands_down
        return
    assert (CREW_ANSWERED_BUILTINS_KEY in _own(batch, "carries")) is hands_down
    assert (_answer(batch, "carries").action == TOOL_AUTO_APPROVE) is hands_down
    if not hands_down:
        assert _allows(batch, "carries") == set()


@pytest.mark.asyncio
async def test_a_second_founder_whose_hook_the_run_lacks_withholds_every_grant(
    agents, sessions, preview, one_directory
):
    _write(agents, {**_WIDE, "name": "guarded", "hooks": _pre(_GUARD)})
    _write(agents, {**_WIDE, "name": "guarded-too", "hooks": _pre(_GUARD, _SECOND)})
    _write(agents, {**_PLAIN, "name": "carries", "hooks": _pre(_GUARD)})
    kas_parent_limits.note_parent("subagent:c1", "dashboard:q")
    sessions["dashboard:q"] = "guarded"
    held = await _child(sessions, "guarded", "carries", work_dir=one_directory)
    assert _answer(held, "carries").action == TOOL_AUTO_APPROVE

    sessions["dashboard:q"] = "guarded-too"
    batch = await _child(sessions, "guarded", "carries", work_dir=one_directory)
    assert CREW_ANSWERED_BUILTINS_KEY not in _own(batch, "carries")
    assert _allows(batch, "carries") == set()


@pytest.mark.asyncio
async def test_a_pre_tool_hook_from_settings_runs_on_the_run_so_grants_still_travel(
    agents, sessions, preview, monkeypatch
):
    """A Hooks-page hook is no agent file's: the run's own calls meet it, so its
    founder's grants travel and the run's entry stops auto-approving what it covers."""

    def asks(batch: list[dict]) -> set[str]:
        return {r["capability"] for r in _rules(batch, "plain") if r.get("effect") == "ask"}

    plain = await _child(sessions, "wide", "plain")
    assert "web_fetch" in _allows(plain, "plain") and "web_fetch" not in asks(plain)

    hook = SimpleNamespace(
        enabled=True, event="PreToolUse", command="guard.sh", matcher="web_fetch"
    )
    store = SimpleNamespace(list_all=lambda: [hook])
    monkeypatch.setattr(kas_agents, "get_global_hook_store", lambda: store)
    assert "web_fetch" in kas_agents.pre_tool_hook_matchers("plain", _PLAIN)

    hooked = await _child(sessions, "wide", "plain")
    assert _own(hooked, "plain")[CREW_ANSWERED_BUILTINS_KEY] == ["execute_bash"]
    assert "web_fetch" in asks(hooked)


@pytest.mark.asyncio
async def test_a_bots_held_limit_strips_the_carried_grant(agents, sessions, preview, monkeypatch):
    held = [{"text": "no shell", "capability": "shell"}]
    monkeypatch.setattr(mate_held_rules, "_bot_rules", lambda key, agent: held)

    batch = await _child(sessions, "broad", "plain")
    assert CREW_ANSWERED_BUILTINS_KEY not in _own(batch, "plain")
    assert _answer(batch, "plain").action != TOOL_AUTO_APPROVE


@pytest.mark.asyncio
async def test_a_bots_worker_runs_what_both_it_and_its_founder_grant(
    agents, sessions, preview, monkeypatch, tmp_path
):
    worker = mate_worker.MATE_WORKER_AGENT
    mates = tmp_path / "mates" / "agents"
    mates.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member_id, config=None: "pixel")
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution",
        lambda key: SimpleNamespace(member_id="pixel"),
    )
    _write(mates, {"name": "pixel", "prompt": ""})
    _write(
        agents,
        {**_BASE, "name": worker, "allowedTools": ["web_fetch", "web_search"], "mcpServers": {}},
    )
    monkeypatch.setattr(
        "kiro_crew.agent_sdk.kas_model_catalog.vault_identity", lambda *_a: None, raising=False
    )

    under_wide = await _child(sessions, "wide", worker)
    assert _allows(under_wide, worker) == {"web_fetch"}
    assert CREW_ANSWERED_BUILTINS_KEY not in _own(under_wide, worker)
    assert _allows(await _child(sessions, "narrow", worker, "subagent:c2"), worker) == set()


# ── what cannot be read grants nothing ───────────────────────────────────────


@pytest.mark.asyncio
async def test_a_founder_with_no_agent_file_leaves_the_run_no_grant(agents, sessions, preview):
    sessions["dashboard:p"] = "gone"
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    extras = await KasHarness().session_extras("claims", work_dir=None, session_key="subagent:c1")

    own = _own(list(extras.custom_agents), "claims")
    assert CREW_ANSWERED_BUILTINS_KEY not in own
    assert _allows(list(extras.custom_agents), "claims") == set()
    assert "no agent file defines 'gone'" in extras.withheld_credentials_notice


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [False, True], ids=["preview-off", "preview-on"])
async def test_an_unreadable_chain_refuses_or_grants_nothing(agents, sessions, stored, value):
    stored(value)
    sessions["dashboard:p"] = subagent_mod.ParentRecordUnreadable("corrupt")
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    with pytest.raises(AcpAgentUnavailable, match="could not be read"):
        await _batch("claims", "subagent:c1")

    # A middle run whose own parent is not known: the founder cannot be named.
    kas_parent_limits.note_parent("subagent:leaf", "subagent:orphan")
    sessions["subagent:orphan"] = "broad"
    leaf = await _batch("plain", "subagent:leaf")
    assert CREW_ANSWERED_BUILTINS_KEY not in _own(leaf, "plain")


@pytest.mark.asyncio
async def test_a_founder_that_cannot_be_projected_grants_nothing(
    agents, sessions, preview, monkeypatch
):
    real = kas_agents.to_client_custom_agent

    def project(agent_id, *args, **kwargs):
        if agent_id == "broad":
            raise RuntimeError("projection failed")
        return real(agent_id, *args, **kwargs)

    monkeypatch.setattr(kas_agents, "to_client_custom_agent", project)
    batch = await _child(sessions, "broad", "plain")
    assert CREW_ANSWERED_BUILTINS_KEY not in _own(batch, "plain")


# ── each session on its own, never one per agent name ────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second_dir, answered",
    [("base", True), ("base/other", False)],
    ids=["both-in-the-runs-directory", "one-elsewhere"],
)
async def test_two_founding_sessions_of_one_agent_each_count_on_their_own(
    agents, sessions, preview, monkeypatch, tmp_path, second_dir, answered
):
    """Two chats on one agent are two founders. Each one's own directory decides what
    it hands down, so the one elsewhere withholds its directory-bound grant."""
    (tmp_path / "base/other").mkdir(parents=True)
    dirs = {
        "dashboard:p": (str(tmp_path / "base"),),
        "dashboard:q": (str(tmp_path / second_dir),),
    }
    monkeypatch.setattr(subagent_mod, "parent_project_dirs", lambda key: dirs.get(key, ()))
    settings = {"shell": {"allowedCommands": ["touch .*"]}}
    _write(agents, {**_BASE, "name": "scoped", "toolsSettings": settings})
    sessions["dashboard:p"] = sessions["dashboard:q"] = "scoped"
    for parent in dirs:
        kas_parent_limits.note_parent("subagent:c1", parent)

    extras = await KasHarness().session_extras(
        "plain", work_dir=tmp_path / "base", session_key="subagent:c1"
    )
    own = _own(list(extras.custom_agents), "plain")
    assert (own.get(CREW_ANSWERED_COMMANDS_KEY) == ["touch .*"]) is answered
    found = kas_parent_grants._founding("plain", "subagent:c1", [], str(tmp_path / "base"))
    assert [(name, key) for name, key, _spec, _same in found.founders] == [
        ("scoped", "dashboard:p"),
        ("scoped", "dashboard:q"),
    ]


@pytest.mark.asyncio
async def test_a_same_named_session_above_with_another_spec_keeps_its_ask(
    agents, sessions, preview, monkeypatch
):
    """One agent name can read to two specs (a folder, an app binding). Each session
    above the run is read on its own, so the ask of the farther one reaches the run
    although a nearer session carries the same agent name."""
    _write(agents, {**_PLAIN, "name": "twin"})
    asking = {**_PLAIN, "name": "twin", "permissions": {"rules": [_SHELL_ASK]}}
    real = kas_parent_grants.parent_spec
    monkeypatch.setattr(
        kas_parent_grants,
        "parent_spec",
        lambda agent, key, cwd: asking if key == "subagent:far" else real(agent, key, cwd),
    )
    sessions.update({"dashboard:p": "broad", "subagent:far": "twin", "subagent:near": "twin"})
    kas_parent_limits.note_parent("subagent:far", "dashboard:p")
    kas_parent_limits.note_parent("subagent:near", "subagent:far")
    kas_parent_limits.note_parent("subagent:leaf", "subagent:near")

    leaf = await _batch("plain", "subagent:leaf")
    assert _SHELL_ASK in _rules(leaf, "plain")
    assert _answer(leaf, "plain").action != TOOL_AUTO_APPROVE


@pytest.mark.asyncio
async def test_a_founder_of_the_runs_own_name_with_another_spec_is_another_agent(
    agents, sessions, preview, monkeypatch
):
    """The run names the agent its founding chat runs, but that chat reads the name
    to another spec, one that grants nothing. The run's own file's grants do nothing
    then. The same name read to an equal spec is left alone."""
    _write(agents, {**_BROAD, "name": "twin"})
    bare = {**_PLAIN, "name": "twin"}
    real = kas_parent_grants.parent_spec
    with monkeypatch.context() as patch:
        patch.setattr(
            kas_parent_grants,
            "parent_spec",
            lambda agent, key, cwd: bare if key == "dashboard:p" else real(agent, key, cwd),
        )
        batch = await _child(sessions, "twin", "twin")
        assert CREW_ANSWERED_BUILTINS_KEY not in _own(batch, "twin")
        assert _answer(batch, "twin").action != TOOL_AUTO_APPROVE
        assert kas_parent_limits.run_is_held("subagent:c1", "twin") is True

    same = await _child(sessions, "twin", "twin")
    assert _answer(same, "twin").action == TOOL_AUTO_APPROVE
    assert kas_parent_limits.run_is_held("subagent:c1", "twin") is False


@pytest.mark.asyncio
async def test_each_session_above_is_read_from_its_own_directory_not_the_runs(
    agents, sessions, preview, monkeypatch, tmp_path
):
    """Which spec a name reads to can depend on the directory. A founder's grants and
    the ``ask`` rules of a session between are read from the directories that
    session runs in, never from the run's: here the session between reads its agent
    name to a spec with an ``ask`` only in its own directory, and the ask holds the
    run although the founder, in a third directory, grants the shell."""
    founder_dir, between_dir, run_dir = (str(tmp_path / d) for d in ("founder", "between", "run"))
    for directory in (founder_dir, between_dir, run_dir):
        Path(directory).mkdir()
    dirs = {"dashboard:p": (founder_dir,), "subagent:mid": (between_dir,)}
    monkeypatch.setattr(subagent_mod, "parent_project_dirs", lambda key: dirs.get(key, ()))
    asking = {**_NARROW, "permissions": {"rules": [_SHELL_ASK]}}
    real = kas_parent_grants.parent_spec
    read: list[tuple[str, str, str]] = []

    def spec_of(agent: str, key: str, cwd: str):
        read.append((agent, key, cwd))
        return asking if (key, cwd) == ("subagent:mid", between_dir) else real(agent, key, cwd)

    monkeypatch.setattr(kas_parent_grants, "parent_spec", spec_of)
    sessions.update({"dashboard:p": "broad", "subagent:mid": "narrow"})
    kas_parent_limits.note_parent("subagent:mid", "dashboard:p")
    kas_parent_limits.note_parent("subagent:leaf", "subagent:mid")

    extras = await KasHarness().session_extras(
        "plain", work_dir=Path(run_dir), session_key="subagent:leaf"
    )
    batch = list(extras.custom_agents)
    above = {entry for entry in read if entry[1] != "subagent:leaf"}
    assert above == {
        ("broad", "dashboard:p", founder_dir),
        ("narrow", "subagent:mid", between_dir),
    }
    assert _SHELL_ASK in _rules(batch, "plain")
    assert _answer(batch, "plain").action != TOOL_AUTO_APPROVE

    # Without that ask the founder's whole-tool grant reaches the run in any directory.
    monkeypatch.setattr(kas_parent_grants, "parent_spec", real)
    extras = await KasHarness().session_extras(
        "plain", work_dir=Path(run_dir), session_key="subagent:leaf"
    )
    assert _answer(list(extras.custom_agents), "plain").action == TOOL_AUTO_APPROVE


@pytest.mark.asyncio
async def test_a_session_above_with_no_known_folder_is_read_where_it_is_bound(
    agents, sessions, preview, monkeypatch, tmp_path
):
    """A run between, started with no folder, whose session is bound to folder a;
    the leaf runs in folder b. That session's rules are read in a and then with no
    folder, as its denies are, so the ``ask`` its agent name reads to in a holds
    the leaf. With nothing noted (a process restarted since) only the read with no
    folder is left. No grant of that session is read either way: only a founding
    chat's grants travel."""
    bound, run_dir = str(tmp_path / "a"), tmp_path / "b"
    for directory in (bound, run_dir):
        Path(directory).mkdir()
    monkeypatch.setattr(subagent_mod, "parent_project_dirs", lambda key: ())
    asking = {**_CLAIMS, "name": "narrow", "permissions": {"rules": [_SHELL_ASK]}}
    real = kas_parent_grants.parent_spec
    read: list[tuple[str, str, str]] = []

    def spec_of(agent: str, key: str, cwd: str):
        read.append((agent, key, cwd))
        return asking if (key, cwd) == ("subagent:mid", bound) else real(agent, key, cwd)

    monkeypatch.setattr(kas_parent_grants, "parent_spec", spec_of)
    sessions.update({"dashboard:p": "broad", "subagent:mid": "narrow"})
    kas_parent_limits.note_parent("subagent:mid", "dashboard:p")
    kas_parent_limits.note_parent("subagent:leaf", "subagent:mid", bound)

    extras = await KasHarness().session_extras(
        "plain", work_dir=run_dir, session_key="subagent:leaf"
    )
    batch = list(extras.custom_agents)
    assert {entry for entry in read if entry[1] == "subagent:mid"} == {
        ("narrow", "subagent:mid", bound),
        ("narrow", "subagent:mid", ""),
    }
    assert _SHELL_ASK in _rules(batch, "plain")
    assert _answer(batch, "plain").action != TOOL_AUTO_APPROVE
    # The grants in that session's spec (``claims``' shell and write) are not read.
    assert CREW_ANSWERED_BUILTINS_KEY not in _own(batch, "plain")

    # Nothing noted for it: read with no folder only, as its denies are.
    read.clear()
    kas_parent_limits._NOTED_DIRS.clear()
    await KasHarness().session_extras("plain", work_dir=run_dir, session_key="subagent:leaf")
    assert {entry for entry in read if entry[1] == "subagent:mid"} == {
        ("narrow", "subagent:mid", "")
    }


# ── never more than the founding chat ────────────────────────────────────────

_UNREADABLE_RULES = {"rules": [_SHELL_ASK], "unknown-key": 1}
#: Founding agents of each kind this module reads: grants of every form, limits
#: beside them, and a half that cannot be read whole.
_FOUNDERS: dict[str, dict[str, Any]] = {
    "broad": _BROAD,
    "generous": {**_BASE, "allowedTools": ["execute_bash", "fs_write", "web_fetch"]},
    "limited": _LIMITED,
    "lists-and-limits": {
        **_BROAD,
        "toolsSettings": {"shell": {"denyByDefault": True, "allowedCommands": ["echo .*"]}},
    },
    "grants-and-asks": {**_BROAD, "permissions": {"rules": [_SHELL_ASK]}},
    "grants-and-denies": {**_BROAD, "toolsSettings": {"shell": {"deniedCommands": ["rm .*"]}}},
    "paths-and-readonly": {
        **_BASE,
        "allowedTools": ["fs_write"],
        "toolsSettings": {
            "shell": {"autoAllowReadonly": True, "allowedCommands": ["touch .*"]},
            "fs_write": {"fallbackAction": "deny", "allowedPaths": ["out/**"]},
        },
    },
    "refuses-at-the-engine": {
        **_BASE,
        "allowedTools": ["web_fetch", "fs_read", "execute_bash"],
        "toolsSettings": {
            "web_fetch": {"blocked": [".*internal.*", r"https://example\.com/private/.*"]},
            "fs_read": {"deniedPaths": ["secret/**"]},
            "shell": {"deniedCommands": ["rm (?!x).*"]},
        },
    },
    "unreadable-rules": {**_BROAD, "permissions": _UNREADABLE_RULES},
    "unreadable-settings": {**_BROAD, "toolsSettings": "not settings"},
}
_RUNS: dict[str, dict[str, Any]] = {
    "plain": _PLAIN,
    "claims": _CLAIMS,
    "limits-and-grants": {
        **_BASE,
        "allowedTools": ["execute_bash", "fs_write", "web_fetch"],
        "toolsSettings": {
            "execute_bash": {"denyByDefault": True, "allowedCommands": ["echo .*", "touch .*"]},
            "fs_write": {"allowedPaths": ["out/**"]},
        },
    },
    "unreadable-run": {**_CLAIMS, "permissions": _UNREADABLE_RULES},
}


def _engine_rules(batch: list[dict], agent: str, effect: str) -> set[tuple[str, str]]:
    """``(capability, match)`` of the rules the engine itself applies with *effect*,
    ``""`` for a rule with no match. The spawn gate owns the ``subagent`` rules."""
    return {
        (rule["capability"], match)
        for rule in _rules(batch, agent)
        if rule.get("effect") == effect and rule.get("capability") != "subagent"
        for match in (rule.get("match") or [""])
    }


def _matrix_verdicts(batch: list[dict], agent: str) -> dict[str, str]:
    calls = {**_own_limit_calls(), "remove": _shell("rm x"), "list": _shell("ls -la")}
    return {name: _judge(_stamped(call, batch, agent)).action for name, call in calls.items()}


@pytest.mark.asyncio
@pytest.mark.parametrize("founder", sorted(_FOUNDERS))
@pytest.mark.parametrize("directory", ["known", "unknown"])
async def test_a_run_never_runs_unasked_what_its_founding_chat_is_asked_for_or_refused(
    agents, sessions, preview, monkeypatch, tmp_path, founder, directory
):
    """Every founder and run pair, in the founding chat's directory and with none
    known: a call the run's entry answers is one the founding chat's own session
    answers, and a call that session refuses is refused in the run. The same for
    what the engine decides alone: the run's entry allows no call there that the
    founding chat's own entry does not, and carries each of its engine denies."""
    dirs = (str(tmp_path),) if directory == "known" else ()
    monkeypatch.setattr(subagent_mod, "parent_project_dirs", lambda key: dirs)
    _write(agents, {**_FOUNDERS[founder], "name": founder})
    for name, spec in _RUNS.items():
        _write(agents, {**spec, "name": name})
    allowed: set[tuple[str, str]] = set()
    denied: set[tuple[str, str]] = set()
    try:
        own_batch = await _batch(founder, "dashboard:p")
        theirs = _matrix_verdicts(own_batch, founder)
        allowed = _engine_rules(own_batch, founder, "allow")
        denied = _engine_rules(own_batch, founder, "deny")
    except AcpAgentUnavailable:
        theirs = {}  # the founding chat itself cannot start: nothing may be handed down
    sessions["dashboard:p"] = founder
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")

    for name in _RUNS:
        try:
            extras = await KasHarness().session_extras(
                name, work_dir=tmp_path, session_key="subagent:c1"
            )
        except AcpAgentUnavailable:
            continue
        ours = _matrix_verdicts(list(extras.custom_agents), name)
        more = {call for call, action in ours.items() if action == TOOL_AUTO_APPROVE} - {
            call for call, action in theirs.items() if action == TOOL_AUTO_APPROVE
        }
        assert more == set(), (name, more)
        kept = {call for call, action in theirs.items() if action == TOOL_DENY}
        assert kept <= {call for call, action in ours.items() if action == TOOL_DENY}, name
        batch = list(extras.custom_agents)
        beyond = {
            (capability, match)
            for capability, match in _engine_rules(batch, name, "allow")
            if (capability, match) not in allowed and (capability, "") not in allowed
        }
        assert beyond == set(), (name, beyond)
        assert denied <= _engine_rules(batch, name, "deny"), name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parent, child",
    [
        ("unreadable-rules", "plain"),
        ("unreadable-settings", "plain"),
        ("broad", "unreadable-run"),
        ("broad", "unreadable-run-settings"),
    ],
)
async def test_a_half_that_cannot_be_read_whole_leaves_the_run_no_grant(
    agents, sessions, preview, parent, child
):
    """What a file asks for or refuses is unknown when its ``permissions`` block or
    its ``toolsSettings`` cannot be read whole. A founder like that hands down
    nothing; a run like that keeps its own spec and gains nothing."""
    for name in ("unreadable-rules", "unreadable-settings"):
        _write(agents, {**_FOUNDERS[name], "name": name})
    _write(agents, {**_RUNS["unreadable-run"], "name": "unreadable-run"})
    settings_run = {**_CLAIMS, "name": "unreadable-run-settings", "toolsSettings": "not settings"}
    _write(agents, settings_run)
    assert _answer(await _child(sessions, "broad", "plain"), "plain").action == TOOL_AUTO_APPROVE

    try:
        batch = await _child(sessions, parent, child)
    except AcpAgentUnavailable:
        return
    assert CREW_ANSWERED_BUILTINS_KEY not in _own(batch, child)
    assert _allows(batch, child) == set()
    assert _answer(batch, child).action != TOOL_AUTO_APPROVE
    if child == "unreadable-run-settings":
        spec, found = kas_parent_grants.founding_grants_spec(settings_run, child, "subagent:c1", [])
        assert spec is settings_run and found.applies and found.founders == ()


# ── what the preview leaves alone ────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent, key, parent",
    [
        ("claims", "dashboard:c1", "broad"),
        ("claims", "subagent:c1", ""),
        ("claims", "subagent:c1", "claims"),
    ],
    ids=["not-a-sub-agent-run", "no-parent-session", "its-founders-own-agent"],
)
async def test_the_preview_leaves_these_sessions_alone(
    agents, sessions, stored, monkeypatch, agent, key, parent
):
    if parent:
        sessions["dashboard:p"] = parent
        kas_parent_limits.note_parent(key, "dashboard:p")
    before = await _batch(agent, key)

    halves: list[str] = []
    real = kas_parent_grants._half
    monkeypatch.setattr(kas_parent_grants, "_half", lambda *a: halves.append("x") or real(*a))
    stored(True)
    assert kas_parent_grants.preview_on() is True
    assert await _batch(agent, key) == before
    assert halves == []


@pytest.mark.asyncio
async def test_preview_off_the_run_keeps_its_own_grants(agents, sessions):
    batch = await _child(sessions, "narrow", "claims")

    assert _own(batch, "claims")[CREW_ANSWERED_BUILTINS_KEY] == ["execute_bash", "fs_write"]
    assert _answer(batch, "claims").action == TOOL_AUTO_APPROVE
    assert kas_parent_grants.preview_on() is False
    assert not kas_parent_grants.runs_on_founding_grants("subagent:c1", "claims")


@pytest.mark.parametrize(
    "value, on",
    [(None, False), (False, False), (True, True), ("true", False), (1, False)],
    ids=["never-set", "off", "on", "a-string", "a-number"],
)
def test_the_preview_is_what_the_switch_helper_reads_from_the_stored_value(
    stored, monkeypatch, value, on
):
    """``preview_on`` asks the switch's own helper, with the config a person stored:
    on for a stored ``true`` and for nothing else."""
    real = kas_engine_subagents.kas_subagents_preview_on
    asked: list[Any] = []
    monkeypatch.setattr(
        kas_engine_subagents,
        "kas_subagents_preview_on",
        lambda cfg: asked.append(cfg) or real(cfg),
    )
    stored(value)

    assert kas_parent_grants.preview_on() is on
    [cfg] = asked
    assert real(cfg) is on
    held = cfg.dashboard.feature_previews.get(kas_engine_subagents.KAS_SUBAGENTS_PREVIEW)
    assert (held is True) is on


def test_the_preview_reads_off_when_the_switch_cannot_be_read(stored, monkeypatch):
    """A build with no helper, a helper that answers anything but ``True`` and one
    that raises all read off, with the value stored on."""
    stored(True)
    assert kas_parent_grants.preview_on() is True
    with monkeypatch.context() as patch:
        patch.setattr(kas_engine_subagents, "kas_subagents_preview_on", lambda cfg: "yes")
        assert kas_parent_grants.preview_on() is False

    def unreadable(cfg):
        raise OSError("config unreadable")

    with monkeypatch.context() as patch:
        patch.setattr(kas_engine_subagents, "kas_subagents_preview_on", unreadable)
        assert kas_parent_grants.preview_on() is False
    with monkeypatch.context() as patch:
        patch.delattr(kas_engine_subagents, "kas_subagents_preview_on")
        assert kas_parent_grants.preview_on() is False
    assert kas_parent_grants.preview_on() is True


def test_a_run_on_its_founders_grants_never_takes_a_pooled_session(agents, sessions, preview):
    sessions["dashboard:p"] = "broad"
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")

    assert kas_parent_limits.run_is_held("subagent:c1", "plain") is True
    assert kas_parent_limits.run_is_held("subagent:c1", "broad") is False
    assert kas_parent_limits.run_is_held("subagent:none", "plain") is False


def test_only_the_kas_harness_reads_the_founding_grants():
    """Positive backend identity: the two entry points are called from one file."""
    src = Path(kas_parent_grants.__file__).resolve().parents[1]
    callers = {
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if path.name != "kas_parent_grants.py"
        and any(
            name in path.read_text(encoding="utf-8")
            for name in ("founding_grants_spec", "limit_to_founders")
        )
    }
    assert callers == {"acp/harness/kas.py"}


def test_every_grant_bearing_entry_key_is_limited():
    """A new crew-only entry key must be sorted into a grant, a limit or neither here."""
    from kiro_crew.acp.kas_permissions import (
        CREW_FOLDER_PRE_TOOL_KEY,
        CREW_ONLY_ENTRY_KEYS,
        CREW_SPEC_DENIES_KEY,
    )
    from kiro_crew.acp.kas_settings_mcp import CREW_SETTINGS_MCP_KEY

    sorted_keys = {
        *kas_parent_grants._NAMED_GRANT_KEYS,
        *kas_parent_grants._REF_GRANT_KEYS,
        *kas_parent_grants._LIMIT_ONLY_KEYS,
        CREW_SPEC_DENIES_KEY,
        CREW_FOLDER_PRE_TOOL_KEY,
        CREW_SETTINGS_MCP_KEY,
    }
    assert sorted_keys == set(CREW_ONLY_ENTRY_KEYS)
    assert CREW_ANSWERED_COMMANDS_KEY in kas_parent_grants._NAMED_GRANT_KEYS


def test_the_composed_spec_keeps_the_runs_tools_servers_and_hooks():
    founder = {**_BROAD, "toolsSettings": {"subagent": {"trustedAgents": ["x"]}}}
    run = {
        **_CLAIMS,
        "tools": ["fs_read"],
        "hooks": {"preToolUse": []},
        "mcpServers": {"own": {"command": "c", "autoApprove": ["t"]}},
        "toolsSettings": {"subagent": {"availableAgents": ["y"], "trustedAgents": ["y"]}},
    }
    half = kas_parent_grants._half(
        ("broad", "dashboard:p", founder, False), run, "claims", (_SHELL_ASK,), "", with_run=True
    )
    assert half == {
        "allowedTools": ["execute_bash"],
        "toolsSettings": {"subagent": {"availableAgents": ["y"]}},
        "permissions": {"rules": [_SHELL_ASK]},
    }
    assert json.dumps(run).count("trustedAgents") == 1  # the run's own spec is not edited


@pytest.mark.parametrize("theirs, ours", [("shell", "execute_bash"), ("execute_bash", "shell")])
def test_the_half_holds_the_runs_own_entry_and_none_of_the_founders_deny_lists(theirs, ours):
    """A tool both files restrict keeps the run's own entry alone, under the run's
    spelling: two spellings of one tool would lose one entry when the spec is read.
    The founder's deny lists are left to the parent record."""
    founder = {**_BROAD, "toolsSettings": {theirs: {"deniedCommands": ["rm .*"]}}}
    run = {**_PLAIN, "toolsSettings": {ours: {"deniedCommands": ["shred .*"], "x": 1}}}
    half = kas_parent_grants._half(
        ("broad", "dashboard:p", founder, False), run, "plain", (), "", with_run=True
    )
    assert half["toolsSettings"] == {ours: {"deniedCommands": ["shred .*"], "x": 1}}
    alone = kas_parent_grants._half(
        ("broad", "dashboard:p", founder, False), _PLAIN, "plain", (), "", with_run=True
    )
    assert alone == {"allowedTools": ["execute_bash"]}
    # The founder's own session view keeps them: its grant stands with its denies.
    assert kas_parent_grants._session_half(founder)["toolsSettings"] == founder["toolsSettings"]


# ── the real configuration, preview off or never set ─────────────────────────


def _installed(tmp_path, monkeypatch) -> list[str]:
    """Every spec a real rebuild writes, as ``test_kas_parent_limits`` reads them."""
    from test_agent_install_composition_fail_closed import _Rig

    from kiro_crew.agent_files import OWNED_KIRO_AGENT_NAMES, is_mate_service_agent

    rig = _Rig(tmp_path, monkeypatch)
    agent_mod.rebuild_agent_config()
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: rig.agents)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    names = sorted({p.stem for p in rig.agents.glob("*.json")} & OWNED_KIRO_AGENT_NAMES)
    names = [name for name in names if not is_mate_service_agent(name)]
    assert len(names) >= 12, names
    return names


def _gate_record(batch: list[dict], agent: str) -> dict[str, Any]:
    stamped = _stamped(_shell(_WRITE_COMMAND), batch, agent)
    return {name: value for name, value in vars(stamped).items() if name.startswith("spec_")}


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, False], ids=["never-set", "off"])
async def test_preview_off_every_installed_pair_is_built_without_this_module(
    tmp_path, monkeypatch, sessions, stored, value
):
    """The stored value never set, and stored off, read by the real helper. Every
    installed parent and child pair: the two calls hand back what they were given,
    and each child's whole entry and gate record equal the ones built with both
    calls replaced by ones that do nothing."""
    from test_kas_parent_limits import _shipped_specs

    import kiro_crew.acp.harness.kas as kas_mod

    names = _installed(tmp_path, monkeypatch)
    if value is not None:
        stored(value)
    assert kas_parent_grants.preview_on() is False
    directory = paths_mod.kiro_agents_dir()
    for shipped in (spec for spec in _shipped_specs() if spec["name"] not in names):
        _write(directory, shipped)
    specs = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.json"))
    }
    assert len(specs) >= 26, sorted(specs)
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    batch = [{"id": "x"}]
    for parent in specs:
        sessions["dashboard:p"] = parent
        for child, spec in specs.items():
            notices: list[str] = []
            same, found = kas_parent_grants.founding_grants_spec(
                spec, child, "subagent:c1", notices
            )
            assert same is spec and found is kas_parent_grants._UNCHANGED and not notices
            assert kas_parent_grants.limit_to_founders(batch, child, spec, found) is batch
            assert not kas_parent_grants.runs_on_founding_grants("subagent:c1", child)

    def parent_of(child: str) -> str:
        return "kirocrew" if child != "kirocrew" else "kirocrew-conductor"

    with_module = {}
    for child in names:
        sessions["dashboard:p"] = parent_of(child)
        with_module[child] = await _batch(child, "subagent:c1")
    with monkeypatch.context() as patch:
        patch.setattr(
            kas_mod,
            "founding_grants_spec",
            lambda spec, *_a: (spec, kas_parent_grants._UNCHANGED),
        )
        patch.setattr(kas_mod, "limit_to_founders", lambda custom_agents, *_a: custom_agents)
        for child in names:
            sessions["dashboard:p"] = parent_of(child)
            without = await _batch(child, "subagent:c1")
            assert with_module[child] == without, child
            assert _gate_record(with_module[child], child) == _gate_record(without, child), child
    if value is not None:
        return
    # Stored on, each agent under a chat on that same agent, read from another
    # directory: one name, one spec, so the run is left alone.
    stored(True)
    assert kas_parent_grants.preview_on() is True
    for child, spec in specs.items():
        sessions["dashboard:p"] = child
        same, found = kas_parent_grants.founding_grants_spec(
            spec, child, "subagent:c1", [], str(tmp_path)
        )
        assert same is spec and found is kas_parent_grants._UNCHANGED, child


@pytest.mark.asyncio
async def test_another_harness_keeps_its_pool_decision_with_the_preview_on(
    agents, sessions, preview
):
    from test_kas_parent_limits import _pool_decision

    from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS

    sessions["dashboard:p"] = "broad"
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    assert await _pool_decision("subagent:c1", "plain", ACP_BACKEND_CLAUDE) == ["miss_empty"]
    assert await _pool_decision("subagent:c1", "plain", ACP_BACKEND_KAS) == ["bypass_parent_denies"]
