"""A person's own agent joins the default agent's engine mount only when its
projected entry keeps nothing that Kiro Crew's gate alone would hold it to.

A native sub-agent's request reaches the gate with its parent's limits and none of
its own (``session_handle_permissions._denies_only``). So an entry key that
never reaches the engine either binds a sub-agent to nothing (a grant the gate
answers the session agent's own request by, which a sub-agent's request never
carries) or keeps the agent on ``spawn_run``, where it runs with its own limits.
Every key and every field of the deny record is judged here, and one nobody has
judged keeps the agent out.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from kas_engine_child_helpers import _setting

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
from kiro_crew.acp import (
    kas_agents,
    kas_engine_subagents,
    kas_env_launch,
    kas_permissions,
    kas_spec_denies,
    kas_subagent_targets,
    kas_unread_commands,
)
from kiro_crew.acp.harness import kas as kas_harness
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.kas_permissions import CREW_ONLY_ENTRY_KEYS, CREW_SPEC_DENIES_KEY
from kiro_crew.platform.spec_denied_grant import SpecDeniedGrant, denies_only

_DEFAULTS = Path(kas_harness.__file__).parents[2] / "config" / "defaults.json"
_HELPER = "kirocrew-helper"
_DASHBOARD = "dashboard:t1"
_GATE_ONLY = (
    "(Kiro Crew applies its shell or file-write limits, its blocked web addresses "
    "and its preToolUse hooks only"
)

#: A plain URL and ``.*``: no host glob names it, so the engine gets no deny for it
#: and Kiro Crew's gate refuses a fetch under it.
_BLOCKED_URL = "https://internal\\.example\\.com/admin.*"
#: A bare host: the engine holds it as a ``web_fetch`` deny rule.
_BLOCKED_HOST = "private\\.example\\.org"
_TRUSTED_URL = "https://docs\\.example\\.com/.*"


def _default_spec(**extra: Any) -> dict[str, Any]:
    shipped = json.loads(_DEFAULTS.read_text(encoding="utf-8"))
    return {
        "name": "kirocrew",
        "description": "the default agent",
        "prompt": "You are the default agent.",
        "tools": shipped["tools"],
        "allowedTools": shipped["allowedTools"],
        **extra,
    }


def _own(name: str, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"the person's {name}",
        "prompt": f"You are {name}.",
        "tools": ["fs_read", "web_fetch"],
        **extra,
    }


@pytest.fixture
def agents_dir(monkeypatch, tmp_path) -> Path:
    user = tmp_path / "home" / "agents"
    user.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    return user


setting = pytest.fixture(_setting)


def _write(directory: Path, spec: dict[str, Any]) -> None:
    (directory / f"{spec['name']}.json").write_text(json.dumps(spec), encoding="utf-8")


async def _batch(agent: str = "kirocrew") -> dict[str, dict[str, Any]]:
    extras = await KasHarness().session_extras(agent, work_dir=None, session_key=_DASHBOARD)
    return {a["id"]: a for a in extras.custom_agents}


def _rules(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return (entry.get("permissions") or {}).get("rules") or []


def _refused(entry: dict[str, Any], name: str) -> bool:
    """Whether *entry*'s ``subagent`` rules deny starting *name*."""
    return any(
        r.get("capability") == "subagent"
        and r.get("effect") == "deny"
        and any(fnmatch.fnmatchcase(name, p) for p in r.get("match") or ["*"])
        and not any(fnmatch.fnmatchcase(name, p) for p in r.get("exclude") or [])
        for r in _rules(entry)
    )


def _kept_out(batch: dict[str, dict[str, Any]]) -> bool:
    """Whether ``mine`` is left out of a mounted *batch* and stays denied.

    The mount itself is asserted: the helper is registered and reachable, so
    leaving ``mine`` out costs the chat nothing else.
    """
    parent = batch["kirocrew"]
    assert _HELPER in batch and not _refused(parent, _HELPER)
    assert ("mine" in batch) is not _refused(parent, "mine")
    return "mine" not in batch


async def _mounted_with(
    agents_dir: Path, setting: Any, monkeypatch: Any, extra: dict[str, Any], **keys: Any
) -> dict[str, dict[str, Any]]:
    """The default agent's batch when the projection writes *extra* on ``mine``'s entry."""
    real = kas_agents.build_kas_custom_agents

    def _build(prompt_dir: Any, agent_id: str, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        out = real(prompt_dir, agent_id, *args, **kwargs)
        return [{**e, **extra} if e.get("id") == "mine" else e for e in out]

    monkeypatch.setattr(kas_agents, "build_kas_custom_agents", _build)
    _write(agents_dir, _default_spec())
    _write(agents_dir, _own("mine", **keys))
    setting("on")
    return await _batch()


# ── From the agent's file: a blocked URL only the gate refuses ────────────────

_GATE_REFUSED: dict[str, dict[str, Any]] = {
    "a-blocked-url": {"toolsSettings": {"web_fetch": {"blocked": [_BLOCKED_URL]}}},
    "a-blocked-url-beside-a-grant": {
        "allowedTools": ["web_fetch"],
        "toolsSettings": {"web_fetch": {"blocked": [_BLOCKED_URL]}},
    },
    "a-blocked-url-beside-a-trusted-url": {
        "toolsSettings": {"web_fetch": {"blocked": [_BLOCKED_URL], "trusted": [_TRUSTED_URL]}},
    },
    "a-blocked-url-beside-a-host-the-engine-holds": {
        "toolsSettings": {"web_fetch": {"blocked": [_BLOCKED_HOST, _BLOCKED_URL]}},
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(_GATE_REFUSED))
async def test_a_blocked_url_only_the_gate_refuses_keeps_the_agent_out(
    agents_dir, setting, caplog, kind
):
    caplog.set_level(logging.INFO, logger=kas_engine_subagents.__name__)
    spec = _own("mine", **_GATE_REFUSED[kind])
    # The premise: the projection records the entry for the gate and for no one else.
    [entry] = kas_agents.build_kas_custom_agents(agents_dir, "mine", spec)
    assert _BLOCKED_URL in entry[CREW_SPEC_DENIES_KEY]["webFetchBlocked"]
    assert not any(
        r.get("capability") == "web_fetch"
        and r.get("effect") == "deny"
        and "internal.example.com/admin" in "".join(r.get("match") or [])
        for r in _rules(entry)
    )
    _write(agents_dir, _default_spec())
    _write(agents_dir, spec)
    setting("on")

    batch = await _batch()

    assert _kept_out(batch)
    assert f"mine {_GATE_ONLY}" in caplog.text


@pytest.mark.asyncio
async def test_a_blocked_host_the_engine_holds_registers_and_travels_as_a_rule(agents_dir, setting):
    _write(agents_dir, _default_spec())
    _write(agents_dir, _own("mine", toolsSettings={"web_fetch": {"blocked": [_BLOCKED_HOST]}}))
    setting("on")

    batch = await _batch()

    assert not _kept_out(batch)
    assert CREW_SPEC_DENIES_KEY not in batch["mine"]
    assert {"capability": "web_fetch", "match": ["private.example.org"], "effect": "deny"} in (
        _rules(batch["mine"])
    )


@pytest.mark.asyncio
async def test_a_trusted_url_alone_registers_since_it_only_grants(agents_dir, setting):
    _write(agents_dir, _default_spec())
    _write(agents_dir, _own("mine", toolsSettings={"web_fetch": {"trusted": [_TRUSTED_URL]}}))
    setting("on")

    batch = await _batch()

    assert not _kept_out(batch)
    assert batch["mine"][CREW_SPEC_DENIES_KEY] == {
        "builtins": [],
        "commands": [],
        "deniedCommands": [],
        "deniedPaths": [],
        "webFetchBlocked": [],
        "webFetchBlockedComplete": True,
        "webFetchTrusted": [_TRUSTED_URL],
    }


@pytest.mark.asyncio
async def test_a_blocked_url_the_engine_refuses_every_fetch_for_registers(
    agents_dir, setting, monkeypatch
):
    """``agent.kas_fail_closed_web_fetch_blocked``: the engine's own deny holds it."""
    for reader in (kas_permissions, kas_unread_commands):
        monkeypatch.setattr(reader, "fails_closed_on_uncarried_blocked", lambda: True)
    _write(agents_dir, _default_spec())
    _write(agents_dir, _own("mine", toolsSettings={"web_fetch": {"blocked": [_BLOCKED_URL]}}))
    setting("on")

    batch = await _batch()

    assert not _kept_out(batch)
    assert CREW_SPEC_DENIES_KEY not in batch["mine"]
    assert any(
        r.get("capability") == "web_fetch" and r.get("effect") == "deny" and r.get("match") == ["*"]
        for r in _rules(batch["mine"])
    )


@pytest.mark.asyncio
async def test_a_denied_command_only_the_gate_refuses_keeps_the_agent_out(agents_dir, setting):
    """With no grant beside it: the engine has no glob for the entry, so the
    projection records it for the gate to refuse on every approval policy."""
    spec = _own(
        "mine",
        tools=["fs_read", "execute_bash"],
        toolsSettings={"shell": {"deniedCommands": ["rm [a-z]+\\.txt"]}},
    )
    [entry] = kas_agents.build_kas_custom_agents(agents_dir, "mine", spec)
    assert entry[CREW_SPEC_DENIES_KEY]["deniedCommands"] == ["rm [a-z]+\\.txt"]
    assert entry[CREW_SPEC_DENIES_KEY]["builtins"] == []
    _write(agents_dir, _default_spec())
    _write(agents_dir, spec)
    setting("on")

    assert _kept_out(await _batch())


@pytest.mark.asyncio
async def test_a_custom_parents_listed_agent_is_judged_as_before(agents_dir, setting):
    """A custom parent's listed agent is judged by ``_gate_only_limits``, as with the
    preview off, which counts a blocked URL only the gate refuses: it is left out
    and denied there too."""
    _write(
        agents_dir,
        {
            "name": "delegator",
            "prompt": "Delegate.",
            "tools": ["fs_read", "use_subagent"],
            "toolsSettings": {"subagent": {"availableAgents": ["mine"]}},
        },
    )
    _write(agents_dir, _own("mine", toolsSettings={"web_fetch": {"blocked": [_BLOCKED_URL]}}))
    setting("on")

    batch = await _batch("delegator")

    assert "mine" not in batch
    assert _refused(batch["delegator"], "mine")


@pytest.mark.asyncio
async def test_an_agent_kept_out_releases_what_its_projection_wrote(
    agents_dir, setting, monkeypatch
):
    released: list[str] = []
    real = kas_env_launch.discard_batch

    def _record(custom_agents: Any, written: Any = ()) -> Any:
        released.extend(str(a.get("id")) for a in custom_agents if isinstance(a, dict))
        return real(custom_agents, written)

    monkeypatch.setattr(kas_env_launch, "discard_batch", _record)
    _write(agents_dir, _default_spec())
    _write(agents_dir, _own("mine", toolsSettings={"web_fetch": {"blocked": [_BLOCKED_URL]}}))
    setting("on")

    assert _kept_out(await _batch())
    assert "mine" in released


# ── Every key an entry keeps for Kiro Crew alone ──────────────────────────────

#: key -> (a value the projection writes there, whether the agent still joins, why).
#: The keys are ``kas_permissions.CREW_ONLY_ENTRY_KEYS``, pinned below, so a key
#: added to that set fails here until it is judged.
_ENTRY_KEYS: dict[str, tuple[Any, bool, str]] = {
    "_crewAnsweredBuiltins": (["fs_read"], True, "a grant: unanswered, the call asks"),
    "_crewAnsweredCommands": (["ls"], True, "a grant: unanswered, the call asks"),
    "_crewAnsweredHookHeld": (["@srv/tool"], True, "a grant: the hook's ask stays the engine's"),
    "_crewAnsweredWrites": (["@srv/write"], True, "a grant: the guard's ask stays the engine's"),
    "_crewAnsweredPaths": (["out/**"], True, "a grant: unanswered, the write asks"),
    "_crewAnsweredReadPaths": (["~/notes"], True, "a grant: unanswered, the read asks"),
    "_crewSettingsMcp": ({}, True, "finished into the entry's own exclusions at the wire"),
    "_crewShellAllowlist": (["ls"], False, "the gate alone refuses every other command"),
    "_crewShellReadonly": (True, False, "rides the shell allowlist; judged with it"),
    "_crewWriteAllowlist": (["out"], False, "the gate alone refuses every other write"),
    "_crewShellUnjudgedDenies": (
        [{"key": "shell.deniedCommands", "entry": "rm (?=x).*", "conditional": False}],
        False,
        "the gate alone holds the shell for them",
    ),
    "_crewFolderPreToolHooks": (True, False, "marks hooks the turn fires for its own agent"),
    "_crewSpecDenies": (
        {"builtins": [], "commands": [], "deniedCommands": ["rm .*"], "deniedPaths": []},
        False,
        "judged field by field, below",
    ),
}


def test_the_judged_keys_are_every_key_that_never_reaches_the_engine():
    assert set(_ENTRY_KEYS) == CREW_ONLY_ENTRY_KEYS
    joining = {key for key, (_value, joins, _why) in _ENTRY_KEYS.items() if joins}
    assert joining == kas_subagent_targets._BINDS_NO_CHILD


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(_ENTRY_KEYS))
async def test_each_key_an_entry_keeps_for_kiro_crew_alone_is_judged(
    agents_dir, setting, monkeypatch, key
):
    value, joins, _why = _ENTRY_KEYS[key]

    batch = await _mounted_with(agents_dir, setting, monkeypatch, {key: value})

    assert _kept_out(batch) is not joins


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["_crewSomethingNew", "_somethingNew"])
async def test_a_key_nobody_judged_keeps_the_agent_out(agents_dir, setting, monkeypatch, key):
    assert _kept_out(await _mounted_with(agents_dir, setting, monkeypatch, {key: ["x"]}))


# ── Every field of the deny record ────────────────────────────────────────────

#: The record with nothing in it: no grant and no refusal.
_EMPTY_RECORD: dict[str, Any] = {
    "builtins": [],
    "commands": [],
    "deniedCommands": [],
    "deniedPaths": [],
    "webFetchBlocked": [],
    "webFetchBlockedComplete": True,
    "webFetchTrusted": [],
}

#: field -> (a value that makes the field count, whether the agent still joins).
_RECORD_FIELDS: dict[str, tuple[Any, bool]] = {
    "builtins": (["execute_bash"], True),
    "commands": (["ls"], True),
    "webFetchTrusted": (["https://docs.example.com/"], True),
    "deniedCommands": (["rm .*"], False),
    "deniedPaths": (["/secret/**"], False),
    "webFetchBlocked": (["https://internal.example.com/admin"], False),
    "webFetchBlockedComplete": (False, False),
}


def test_the_judged_fields_are_every_field_the_projection_writes():
    spec = {
        "allowedTools": ["execute_bash", "fs_write"],
        "toolsSettings": {
            "shell": {"deniedCommands": ["rm .*"]},
            "fs_write": {"deniedPaths": ["secret/**"]},
            "web_fetch": {"blocked": [_BLOCKED_URL], "trusted": [_TRUSTED_URL]},
        },
    }
    record = kas_spec_denies.spec_denies_record(
        spec,
        candidates=frozenset({"execute_bash", "fs_write"}),
        answered=frozenset(),
        policy=None,
        commands=[],
        commands_answered=False,
    )
    assert record is not None
    written = {**record, **kas_spec_denies.web_fetch_gate(spec["toolsSettings"])}

    assert set(written) == set(_RECORD_FIELDS) == set(_EMPTY_RECORD)
    # The gate's own value holds one field per record field, and the session's directory.
    gate_fields = {f.name for f in dataclasses.fields(SpecDeniedGrant)} - {"cwd"}
    assert len(gate_fields) == len(_RECORD_FIELDS)


@pytest.mark.parametrize("field", sorted(_RECORD_FIELDS))
def test_a_field_keeps_the_agent_out_exactly_when_the_gate_holds_a_sub_agent_to_it(field):
    """``denies_only`` is what the gate keeps of a record for a sub-agent's request:
    a field it keeps is a limit, and a field it drops is a grant."""
    value, joins = _RECORD_FIELDS[field]
    entry = {CREW_SPEC_DENIES_KEY: {**_EMPTY_RECORD, field: value}}

    held = denies_only(kas_spec_denies._entry_spec_denies(entry, "/work"))
    nothing = denies_only(
        kas_spec_denies._entry_spec_denies({CREW_SPEC_DENIES_KEY: _EMPTY_RECORD}, "/work")
    )

    assert held is not None and nothing is not None
    assert (held == nothing) is joins
    assert kas_subagent_targets._left_to_the_gate(entry) is not joins


@pytest.mark.asyncio
@pytest.mark.parametrize("field", sorted(_RECORD_FIELDS))
async def test_each_field_of_the_deny_record_is_judged(agents_dir, setting, monkeypatch, field):
    value, joins = _RECORD_FIELDS[field]
    record = {**_EMPTY_RECORD, field: value}

    batch = await _mounted_with(agents_dir, setting, monkeypatch, {CREW_SPEC_DENIES_KEY: record})

    assert _kept_out(batch) is not joins


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record",
    [
        {**_EMPTY_RECORD, "somethingNew": ["x"]},
        {**_EMPTY_RECORD, "deniedCommands": "rm .*"},
        {**_EMPTY_RECORD, "webFetchBlocked": None},
        {**_EMPTY_RECORD, "webFetchBlockedComplete": 1},
        {**_EMPTY_RECORD, "builtins": "execute_bash"},
        "not a record",
        ["rm .*"],
    ],
    ids=[
        "a field nobody judged",
        "denied commands that are no list",
        "blocked urls that are no list",
        "a completeness mark that is no boolean",
        "a grant that is no list",
        "a record that is text",
        "a record that is a list",
    ],
)
async def test_a_record_nobody_can_read_keeps_the_agent_out(
    agents_dir, setting, monkeypatch, record
):
    batch = await _mounted_with(agents_dir, setting, monkeypatch, {CREW_SPEC_DENIES_KEY: record})

    assert _kept_out(batch)


@pytest.mark.asyncio
async def test_an_empty_record_holds_the_agent_to_nothing(agents_dir, setting, monkeypatch):
    batch = await _mounted_with(
        agents_dir, setting, monkeypatch, {CREW_SPEC_DENIES_KEY: dict(_EMPTY_RECORD)}
    )

    assert not _kept_out(batch)
