"""The default agent's engine sub-agent tool also reaches the person's own agents.

With the ``kas_subagents`` preview on, a KAS dashboard chat on the default agent
registers each of the person's user-level and project agents through the target
projection a custom parent's ``availableAgents`` list uses, and the mount's deny
spares the ones it registers. Each kind of agent is either registered, held to the
parent's limits and its own, or refused and still denied. The preview off or
never set, and every other session kind, project what they did before.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from pathlib import Path

import pytest
from kas_engine_child_helpers import _setting

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
from kiro_crew.acp import kas_engine_subagents as engine
from kiro_crew.acp import kas_native_batch
from kiro_crew.acp.harness import kas as kas_harness
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.kas_subagent_targets import MAX_SUBAGENT_TARGETS

_DEFAULTS = Path(kas_harness.__file__).parents[2] / "config" / "defaults.json"
_HELPER = "kirocrew-helper"
_DASHBOARD = "dashboard:t1"
_GATE_ONLY = (
    "(Kiro Crew applies its shell or file-write limits, its blocked web addresses "
    "and its preToolUse hooks"
)


def _default_spec(**extra) -> dict:
    shipped = json.loads(_DEFAULTS.read_text(encoding="utf-8"))
    return {
        "name": "kirocrew",
        "description": "the default agent",
        "prompt": "You are the default agent.",
        "tools": shipped["tools"],
        "allowedTools": shipped["allowedTools"],
        **extra,
    }


def _own(name: str, **extra) -> dict:
    return {
        "name": name,
        "description": f"the person's {name}",
        "prompt": f"You are {name}.",
        "tools": ["fs_read", "execute_bash"],
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


def _write(directory: Path, spec: dict) -> None:
    (directory / f"{spec['name']}.json").write_text(json.dumps(spec), encoding="utf-8")


async def _extras(agent: str = "kirocrew", key: str = _DASHBOARD, work_dir=None, **kw):
    return await KasHarness().session_extras(agent, work_dir=work_dir, session_key=key, **kw)


def _by_id(batch: list[dict]) -> dict[str, dict]:
    return {a["id"]: a for a in batch}


def _rules(entry: dict) -> list[dict]:
    return (entry.get("permissions") or {}).get("rules") or []


def _refused(entry: dict, name: str) -> bool:
    """Whether *entry*'s ``subagent`` rules deny starting *name*."""
    return any(
        r.get("capability") == "subagent"
        and r.get("effect") == "deny"
        and any(fnmatch.fnmatchcase(name, p) for p in r.get("match") or ["*"])
        and not any(fnmatch.fnmatchcase(name, p) for p in r.get("exclude") or [])
        for r in _rules(entry)
    )


# ── The kinds: which of the person's agents the mount registers ───────────────

#: kind id -> (the agent's spec keys, registered?). A refused kind is left out of
#: the batch, stays denied on the parent, and is named in the session-start notice.
_KINDS: dict[str, tuple[dict, bool]] = {
    "permissions-block": (
        {
            "permissions": {
                "rules": [
                    {"capability": "shell", "match": ["rm *"], "effect": "deny"},
                    {"capability": "shell", "effect": "allow"},
                ]
            }
        },
        True,
    ),
    "legacy-grants": (
        {
            "allowedTools": ["fs_read"],
            "toolsSettings": {"fs_read": {"allowedPaths": ["~/notes"]}},
        },
        True,
    ),
    "own-server": (
        {"tools": ["fs_read", "@own-srv"], "mcpServers": {"own-srv": {"command": "own-srv"}}},
        True,
    ),
    "own-model": ({"model": "a-model-of-its-own"}, True),
    "shell-deny-by-default": (
        {"toolsSettings": {"shell": {"denyByDefault": True, "allowedCommands": ["ls"]}}},
        False,
    ),
    "shell-deny-by-default-beside-a-grant": (
        {
            "allowedTools": ["execute_bash"],
            "toolsSettings": {"shell": {"denyByDefault": True, "allowedCommands": ["ls"]}},
        },
        False,
    ),
    "shell-deny-by-default-beside-a-grant-and-denies": (
        {
            "allowedTools": ["execute_bash"],
            "toolsSettings": {
                "execute_bash": {
                    "denyByDefault": True,
                    "allowedCommands": ["git status.*"],
                    "deniedCommands": ["(touch|mkdir) .*"],
                }
            },
        },
        False,
    ),
    "write-fallback-deny": (
        {
            "tools": ["fs_write"],
            "toolsSettings": {"fs_write": {"fallbackAction": "deny", "allowedPaths": ["out"]}},
        },
        False,
    ),
    "denied-commands-beside-a-grant": (
        {
            "allowedTools": ["execute_bash"],
            "toolsSettings": {"shell": {"deniedCommands": ["rm [a-z]+\\.txt"]}},
        },
        False,
    ),
    "write-denied-paths-beside-a-grant": (
        {
            "tools": ["fs_write"],
            "allowedTools": ["fs_write"],
            "toolsSettings": {"fs_write": {"deniedPaths": ["secret/**"]}},
        },
        False,
    ),
    "own-pre-tool-use-hook": (
        {"hooks": {"preToolUse": [{"matcher": "*", "command": "exit 2"}]}},
        False,
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(_KINDS))
async def test_each_kind_of_own_agent_is_registered_or_stays_refused(
    agents_dir, setting, caplog, kind
):
    keys, registered = _KINDS[kind]
    caplog.set_level(logging.INFO, logger=engine.__name__)
    _write(agents_dir, _default_spec())
    _write(agents_dir, _own("mine", **keys))
    setting("on")

    extras = await _extras()

    batch = _by_id(extras.custom_agents)
    parent = batch["kirocrew"]
    assert _HELPER in batch and not _refused(parent, _HELPER)
    assert ("mine" in batch) is registered
    assert _refused(parent, "mine") is not registered
    # Why it is left out is logged; the chat's session-start notice does not list it.
    assert (f"mine {_GATE_ONLY}" in caplog.text) is not registered
    assert "mine" not in (extras.withheld_credentials_notice or "")


@pytest.mark.asyncio
async def test_a_registered_agent_keeps_its_own_denies_and_servers(agents_dir, setting):
    _write(agents_dir, _default_spec())
    for kind in ("permissions-block", "own-server", "own-model"):
        _write(agents_dir, _own(kind, **_KINDS[kind][0]))
    setting("on")

    batch = _by_id((await _extras()).custom_agents)

    own_deny = [r for r in _rules(batch["permissions-block"]) if r.get("effect") == "deny"]
    assert any(r.get("capability") == "shell" and r.get("match") == ["rm *"] for r in own_deny)
    assert set(batch["own-server"]["mcpServers"]) == {"own-srv"}


@pytest.mark.asyncio
async def test_a_deny_that_travels_only_as_top_level_branches_keeps_the_agent_out(
    agents_dir, setting, caplog
):
    """``touch|mkdir`` reaches the engine only as the globs ``touch`` and ``mkdir``,
    narrower than the entry's own regex, and the gate cannot read the entry. Mounted,
    the agent's ``touch direct-own.txt`` would run on its parent's shell grant, so the
    mount leaves it out, as the Bot worker and the denyByDefault lift do."""
    caplog.set_level(logging.INFO, logger=engine.__name__)
    _write(agents_dir, _default_spec())
    _write(
        agents_dir,
        _own("own-model", toolsSettings={"shell": {"deniedCommands": ["touch|mkdir"]}}),
    )
    setting("on")

    batch = _by_id((await _extras()).custom_agents)

    assert _HELPER in batch and "own-model" not in batch
    assert _refused(batch["kirocrew"], "own-model")
    assert f"own-model {_GATE_ONLY}" in caplog.text


@pytest.mark.asyncio
async def test_a_deny_the_engine_carries_registers_and_travels_as_a_rule(agents_dir, setting):
    _write(agents_dir, _default_spec())
    _write(agents_dir, _own("mine", toolsSettings={"shell": {"deniedCommands": ["touch .*"]}}))
    setting("on")

    batch = _by_id((await _extras()).custom_agents)

    assert any(
        r.get("capability") == "shell" and r.get("effect") == "deny" and r.get("match")
        for r in _rules(batch["mine"])
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    ["kirocrew-worker", "kirocrew-lite", "kirocrew-conductor", "general-task-execution"]
    + ["semantic_reviewer", "context-gatherer", "no-such-agent"],
)
async def test_kiro_crews_own_and_the_engines_bundled_agents_stay_denied(agents_dir, setting, name):
    _write(agents_dir, _default_spec())
    _write(agents_dir, _own("mine"))
    # A file of one of Kiro Crew's own names in the person's folder changes nothing.
    _write(agents_dir, _own("kirocrew-worker"))
    setting("on")

    batch = _by_id((await _extras()).custom_agents)

    assert "mine" in batch
    assert name not in batch
    assert _refused(batch["kirocrew"], name)


@pytest.mark.asyncio
async def test_a_name_the_engine_reserves_is_refused_and_logged(agents_dir, setting, caplog):
    _write(agents_dir, _default_spec())
    _write(agents_dir, _own("vibe"))
    setting("on")
    caplog.set_level(logging.INFO, logger=engine.__name__)

    batch = _by_id((await _extras()).custom_agents)

    assert "vibe" not in batch and _refused(batch["kirocrew"], "vibe")
    assert "vibe (the backend keeps that name" in caplog.text


@pytest.mark.asyncio
async def test_a_project_agent_registers_with_none_of_its_servers(agents_dir, setting, tmp_path):
    checkout = tmp_path / "checkout"
    folder = checkout / ".kiro" / "agents"
    folder.mkdir(parents=True)
    _write(agents_dir, _default_spec())
    _write(
        folder,
        _own(
            "from-checkout",
            tools=["fs_read", "@checkout-srv"],
            mcpServers={"checkout-srv": {"command": "checkout-srv"}},
        ),
    )
    setting("on")

    batch = _by_id((await _extras(work_dir=str(checkout))).custom_agents)

    assert not _refused(batch["kirocrew"], "from-checkout")
    assert not batch["from-checkout"].get("mcpServers")


@pytest.mark.asyncio
async def test_only_the_first_agents_in_name_order_are_tried_and_the_rest_counted(
    agents_dir, setting, caplog
):
    _write(agents_dir, _default_spec())
    names = [f"own-{i:02d}" for i in range(MAX_SUBAGENT_TARGETS + 2)]
    for name in names:
        _write(agents_dir, _own(name))
    setting("on")
    caplog.set_level(logging.INFO, logger=engine.__name__)

    batch = _by_id((await _extras()).custom_agents)

    parent = batch["kirocrew"]
    for name in names[:MAX_SUBAGENT_TARGETS]:
        assert name in batch and not _refused(parent, name)
    for name in names[MAX_SUBAGENT_TARGETS:]:
        assert name not in batch and _refused(parent, name)
    # The default agent names no agent: the count is of the person's own, in name order.
    assert (
        f"2 more agents (only the first {MAX_SUBAGENT_TARGETS} own agents in name order "
        "are tried)"
    ) in caplog.text
    assert "it names are tried" not in caplog.text
    assert names[-1] not in caplog.text


@pytest.mark.asyncio
async def test_a_file_named_like_one_of_kiro_crews_own_agents_takes_no_place(agents_dir, setting):
    from kiro_crew.agent_files import OWNED_KIRO_AGENT_NAMES

    _write(agents_dir, _default_spec())
    for owned in sorted(OWNED_KIRO_AGENT_NAMES - {"kirocrew"}):
        _write(agents_dir, _own(owned))
    names = [f"own-{i:02d}" for i in range(MAX_SUBAGENT_TARGETS)]
    for name in names:
        _write(agents_dir, _own(name))
    setting("on")

    batch = _by_id((await _extras()).custom_agents)

    assert set(batch) == {"kirocrew", _HELPER, *names}
    for owned in OWNED_KIRO_AGENT_NAMES:
        assert _refused(batch["kirocrew"], owned), owned


@pytest.mark.asyncio
async def test_no_own_agent_leaves_the_helper_only_mount_and_no_notice(agents_dir, setting):
    _write(agents_dir, _default_spec())
    setting("on")

    extras = await _extras()

    parent = _by_id(extras.custom_agents)["kirocrew"]
    assert {
        "capability": "subagent",
        "match": ["*"],
        "exclude": [_HELPER],
        "effect": "deny",
    } in _rules(parent)
    assert "cannot hand work to" not in (extras.withheld_credentials_notice or "")


# ── How the parent's limits reach a registered agent ──────────────────────────


@pytest.mark.asyncio
async def test_a_registered_agent_carries_each_deny_and_ask_of_its_parent(agents_dir, setting):
    held = [
        {"capability": "fs_read", "match": ["**/secret/**"], "effect": "deny"},
        {"capability": "shell", "match": ["git push*"], "effect": "ask"},
    ]
    _write(agents_dir, _default_spec(permissions={"rules": held}))
    # Its own grant on the same capability lifts neither.
    _write(agents_dir, _own("mine", allowedTools=["fs_read", "execute_bash"]))
    setting("on")

    batch = _by_id((await _extras()).custom_agents)

    parent_holds = [
        r
        for r in _rules(batch["kirocrew"])
        if r.get("effect") in ("deny", "ask") and r.get("capability") != "subagent"
    ]
    assert len(parent_holds) >= len(held)
    for rule in parent_holds:
        assert rule in _rules(batch["mine"]), rule
        assert rule in _rules(batch[_HELPER]), rule


@pytest.mark.asyncio
async def test_a_registered_agent_reaches_none_of_kiro_crews_own_servers(agents_dir, setting):
    from kiro_crew.acp.kas_agents_mcp_servers import MANAGED_MCP_SERVER_NAMES

    _write(agents_dir, _default_spec())
    _write(agents_dir, _own("mine", tools=["*"]))
    setting("on")

    child = _by_id((await _extras()).custom_agents)["mine"]

    assert {f"@{name}" for name in MANAGED_MCP_SERVER_NAMES} <= set(child["excludedTools"])
    assert not set(child.get("mcpServers") or {}) & set(MANAGED_MCP_SERVER_NAMES)


@pytest.mark.asyncio
async def test_a_registered_agent_asks_for_every_mcp_call(agents_dir, setting):
    # The parent allows a server of the same name the agent sets up its own way.
    shipped = _default_spec()
    _write(
        agents_dir,
        {
            **shipped,
            "tools": [*shipped["tools"], "@own-srv"],
            "allowedTools": [*shipped["allowedTools"], "@own-srv"],
            "mcpServers": {"own-srv": {"command": "the-parents-own-srv"}},
        },
    )
    _write(agents_dir, _own("own-server", **_KINDS["own-server"][0], allowedTools=["@own-srv"]))
    setting("on")

    batch = _by_id((await _extras()).custom_agents)

    assert any(
        r.get("capability") == "mcp" and r.get("effect") == "allow" and "own-srv/*" in r["match"]
        for r in _rules(batch["kirocrew"])
    )
    assert {"capability": "mcp", "effect": "ask"} in _rules(batch["own-server"])
    assert {"capability": "mcp", "effect": "ask"} not in _rules(batch["kirocrew"])


@pytest.mark.asyncio
async def test_a_registered_agent_auto_approves_nothing_the_parents_hooks_cover(
    agents_dir, setting
):
    """What the staleness check reads: no entry auto-approves what the hook covers."""
    from kiro_crew.acp.kas_hook_mcp import approved_mcp_servers
    from kiro_crew.acp.kas_permissions import (
        cached_mcp_tool_names,
        hook_gated_auto_approved,
        hook_gated_capabilities,
    )
    from kiro_crew.acp.kas_subagent_targets import registered_unprompted

    hooks = {"preToolUse": [{"matcher": "fs_read", "command": "echo checked"}]}
    _write(agents_dir, _default_spec(hooks=hooks))
    _write(agents_dir, _own("mine", allowedTools=["fs_read"]))
    setting("on")

    batch = (await _extras()).custom_agents

    assert "mine" in _by_id(batch)
    still: set[str] = set()
    for part in registered_unprompted(None, batch):
        covered = hook_gated_capabilities(
            ("fs_read",),
            mcp_servers=approved_mcp_servers(part),
            mcp_tool_names=cached_mcp_tool_names,
        )
        still |= hook_gated_auto_approved(covered, part)
    assert still == set()


@pytest.mark.asyncio
async def test_the_helper_excludes_a_registered_agents_own_server(agents_dir, setting):
    _write(agents_dir, _default_spec())
    _write(agents_dir, _own("own-server", **_KINDS["own-server"][0]))
    setting("on")

    batch = _by_id((await _extras()).custom_agents)

    assert "@own-srv" in batch[_HELPER]["excludedTools"]


def test_a_mount_with_another_last_rule_keeps_the_names_denied():
    entry = {"id": "kirocrew", "permissions": {"rules": [{"capability": "shell", "effect": "ask"}]}}
    assert engine._also_reaching(entry, ["mine"]) is entry
    assert engine._also_reaching({"id": "kirocrew"}, ["mine"]) == {"id": "kirocrew"}


# ── Where nothing changes ─────────────────────────────────────────────────────


async def _without_own_agents(monkeypatch, **kw) -> list[dict]:
    """The batch as the base projects it: the mount never asks for own agents."""
    with monkeypatch.context() as m:
        real = kas_native_batch.native_batch
        m.setattr(
            kas_harness,
            "native_batch",
            lambda *a, **k: real(*a, **{**k, "own_agents": None}),
        )
        return (await _extras(**kw)).custom_agents


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value, kw",
    [
        ("off", {}),
        ("unset", {}),
        ("on", {"key": "cron:job"}),
        ("on", {"key": "subagent:1"}),
        ("on", {"member_dispatch": True}),
        ("on", {"crew_panel": True}),
        ("on", {"remote_workspace": True}),
        ("on", {"agent": "mine"}),
    ],
    ids=[
        "off",
        "never-set",
        "cron",
        "kiro-crew-sub-agent",
        "member-dispatch",
        "panel",
        "remote",
        "own-agent",
    ],
)
async def test_every_other_session_projects_what_it_did_and_registers_none(
    agents_dir, setting, monkeypatch, value, kw
):
    _write(agents_dir, _default_spec())
    _write(agents_dir, _own("mine", tools=["fs_read", "use_subagent"]))
    _write(agents_dir, _own("other"))
    setting(value)

    batch = (await _extras(**kw)).custom_agents

    assert batch == await _without_own_agents(monkeypatch, **kw)
    assert {a["id"] for a in batch} == {kw.get("agent", "kirocrew")}


@pytest.mark.parametrize(
    "refusal",
    ["helper-name-taken", "unmirrored-restriction", "parent-denies-the-helper", "no-parent-spec"],
)
def test_a_refused_mount_never_projects_the_persons_agents(agents_dir, refusal):
    spec: dict | None = _default_spec()
    parent: dict = {"id": "kirocrew", "tools": ["fs_read"]}
    if refusal == "helper-name-taken":
        _write(agents_dir, _own(_HELPER))
    elif refusal == "unmirrored-restriction":
        spec = _default_spec(toolsSettings={"fs_read": {"deniedPaths": ["private/**"]}})
    elif refusal == "parent-denies-the-helper":
        parent["permissions"] = {"rules": [{"capability": "subagent", "effect": "deny"}]}
    else:
        spec = None
    called: list[int] = []

    def own_agents():
        called.append(1)
        return [], ""

    out = engine.with_engine_subagents(
        [parent],
        "kirocrew",
        agents_dir=agents_dir,
        work_dir=None,
        stub_server_names=frozenset(),
        session_key=_DASHBOARD,
        settings_dir=None,
        notices=[],
        parent_spec=spec,
        own_agents=own_agents,
    )

    assert out == [parent]
    assert called == []


def test_a_projection_that_fails_registers_none_and_keeps_the_helper(agents_dir):
    def own_agents():
        raise OSError("agents folder gone")

    out = engine.with_engine_subagents(
        [{"id": "kirocrew", "tools": ["fs_read"]}],
        "kirocrew",
        agents_dir=agents_dir,
        work_dir=None,
        stub_server_names=frozenset(),
        session_key=_DASHBOARD,
        settings_dir=None,
        notices=[],
        parent_spec=_default_spec(),
        own_agents=own_agents,
    )

    assert [a["id"] for a in out] == ["kirocrew", _HELPER]
    assert _refused(out[0], "mine") and not _refused(out[0], _HELPER)


def test_an_entry_under_the_helpers_id_never_joins_a_mounted_batch(agents_dir):
    """A file of the helper's name written after the mount's own check: the
    projection would hand back an entry under that id, and it is dropped."""
    theirs = {"id": _HELPER, "description": "the person's agent", "tools": ["fs_read"]}
    mine = {"id": "own-model", "description": "the person's own-model", "tools": ["fs_read"]}

    out = engine.with_engine_subagents(
        [{"id": "kirocrew", "tools": ["fs_read"]}],
        "kirocrew",
        agents_dir=agents_dir,
        work_dir=None,
        stub_server_names=frozenset(),
        session_key=_DASHBOARD,
        settings_dir=None,
        notices=[],
        parent_spec=_default_spec(),
        own_agents=lambda: ([theirs, mine], ""),
    )

    assert [a["id"] for a in out] == ["kirocrew", "own-model", _HELPER]
    [helper] = [a for a in out if a["id"] == _HELPER]
    assert helper["description"] != theirs["description"]
