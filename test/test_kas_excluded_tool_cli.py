"""With the ``kas_subagents`` preview on, a person's own agent that excludes the sub-agent tool is read as kiro-cli reads it.

Two agent files were run on kiro-cli 3.5.0 whose agent still started a child:
``tools: ["*"]`` excluding ``use_subagent`` and ``invoke_sub_agent`` (the
per-agent ``subagent_<id>`` tools stay), and ``tools: ["*"]`` excluding
``subagent`` (the engine's tag for those per-agent tools, so ``invoke_sub_agent``
stays). On, in a person's dashboard chat, such a file gets no rule that takes
every engine sub-agent away. Every other file, every agent Kiro Crew writes,
every registered target and every other session keeps that rule, and so does
every file while the preview is off or never set.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
from kiro_crew.acp import kas_subagent_targets as targets
from kiro_crew.acp.harness import kas as kas_harness
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.agent_files import OWNED_KIRO_AGENT_NAMES
from kiro_crew.config.loader import KiroCrewConfig

_DEFAULTS = Path(kas_harness.__file__).parents[2] / "config" / "defaults.json"
_DASHBOARD = "dashboard:t1"
_IDS = ["use_subagent", "invoke_sub_agent"]
_SPAWNS = ["@kirocrew-core/spawn_run", "@kirocrew-core/spawn_sub_agents"]
_EVERY_SUBAGENT = {"capability": "subagent", "effect": "deny"}
_DENY_ALL_NAMES = {"capability": "subagent", "match": ["*"], "effect": "deny"}
_ALLOW_READS = {"capability": "fs_read", "match": ["./**"], "effect": "allow"}

#: ``excludedTools`` lists that hold the two names and nothing else about a
#: sub-agent tool: the file run on kiro-cli, and the same beside unrelated entries.
_BY_ID: dict[str, list[str]] = {
    "the-file-as-run": _IDS,
    "other-order": _IDS[::-1],
    "beside-kiro-crews-spawn-tools": [*_IDS, *_SPAWNS],
    "beside-another-built-in": [*_IDS, "fs_write"],
    "beside-a-server": [*_IDS, "@some-server", "@some-server/a_tool"],
}

#: Files no kiro-cli run covers: each keeps the rule. ``excludedTools``, then any
#: other key the file sets.
_NOT_RUN: dict[str, tuple[list[str], dict]] = {
    "use_subagent-alone": (["use_subagent"], {}),
    "invoke_sub_agent-alone": (["invoke_sub_agent"], {}),
    "both-names-and-the-tag": ([*_IDS, "subagent"], {}),
    "another-spelling": ([*_IDS, "agent_crew"], {}),
    "builtin-tag": ([*_IDS, "@builtin"], {}),
    "subagent-meta-tag": ([*_IDS, "@subagent"], {}),
    "a-pattern": ([*_IDS, "fs_*"], {}),
    "a-server-pattern": ([*_IDS, "@*"], {}),
    "everything": (["*"], {}),
    "one-per-agent-tool": ([*_IDS, "subagent_mine-target"], {}),
    "tools-as-a-string": (_IDS, {"tools": "*"}),
    "tools-by-tag": (_IDS, {"tools": ["@builtin", "@kirocrew-core"]}),
    "tools-star-and-more": (_IDS, {"tools": ["*", "@kirocrew-core"]}),
    "an-agents-list": (_IDS, {"toolsSettings": {"subagent": {"availableAgents": ["x"]}}}),
    "trusted-agents": (_IDS, {"toolsSettings": {"use_subagent": {"trustedAgents": ["x"]}}}),
    "a-subagent-allow": (
        _IDS,
        {"permissions": {"rules": [{"capability": "subagent", "match": ["x"], "effect": "allow"}]}},
    ),
    "a-builtin-rule": (
        _IDS,
        {"permissions": {"rules": [{"capability": "builtin", "effect": "ask"}]}},
    ),
    "a-policies-bundle": (_IDS, {"permissions": {"rules": [], "policies": "p"}}),
}


@pytest.fixture
def agents_dir(monkeypatch, tmp_path) -> Path:
    user = tmp_path / "home" / "agents"
    user.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    monkeypatch.setattr(agent_mod, "require_fresh_derived_spec", lambda *a, **k: None)
    return user


@pytest.fixture
def preview(monkeypatch):
    """Store the ``kas_subagents`` preview: ``True``, ``False`` or ``None`` (never set)."""

    def _set(value: bool | None, *, sharing: bool = False) -> None:
        cfg = KiroCrewConfig()
        previews: dict = {} if value is None else {"kas_subagents": value}
        if sharing:
            previews["chat_runtime_sharing"] = True
        cfg.dashboard.feature_previews = previews
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda _cls, *a, **k: cfg))

    return _set


def _write(directory: Path, name: str, **spec) -> None:
    body = {"name": name, "prompt": "p", "tools": ["*"], **spec}
    (directory / f"{name}.json").write_text(json.dumps(body), encoding="utf-8")


async def _extras(agent: str = "mine", key: str = _DASHBOARD, **kw):
    kw.setdefault("work_dir", None)
    return await KasHarness().session_extras(agent, session_key=key, **kw)


async def _entry(agent: str = "mine", key: str = _DASHBOARD, **kw) -> dict:
    return (await _extras(agent, key, **kw)).custom_agents[0]


def _rules(entry: dict) -> list[dict]:
    return (entry.get("permissions") or {}).get("rules") or []


def _held(entry: dict) -> bool:
    """Whether *entry* carries the rule that takes every engine sub-agent away."""
    return _EVERY_SUBAGENT in _rules(entry)


# ── on: the two files run on kiro-cli ────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(_BY_ID))
async def test_excluding_the_tool_by_id_keeps_the_per_agent_tools(agents_dir, preview, kind):
    preview(True)
    _write(agents_dir, "mine", excludedTools=_BY_ID[kind])

    entry = await _entry()

    assert not _held(entry)
    # ``invoke_sub_agent`` is still excluded, as the file says and as kiro-cli did.
    assert "invoke_sub_agent" in entry["excludedTools"]
    assert not targets.mounts_subagent_tool(entry)
    # No rule on a per-agent tool but the one on Kiro Crew's own agents.
    assert [r for r in _rules(entry) if r.get("capability") == "subagent"] == [
        {"capability": "subagent", "match": sorted(OWNED_KIRO_AGENT_NAMES), "effect": "deny"}
    ]


@pytest.mark.asyncio
async def test_excluding_the_tag_keeps_invoke_sub_agent(agents_dir, preview):
    preview(True)
    _write(agents_dir, "mine", excludedTools=["subagent", "fs_write"])

    entry = await _entry()

    assert not _held(entry)
    # The tag the engine's per-agent tools carry, in place of the tool's id.
    assert "subagent" in entry["excludedTools"]
    assert not {"use_subagent", "invoke_sub_agent"} & set(entry["excludedTools"])
    assert "delete_file" in entry["excludedTools"]
    assert targets.mounts_subagent_tool(entry)


@pytest.mark.asyncio
async def test_a_file_in_the_projects_agents_folder_is_read_the_same(agents_dir, preview, tmp_path):
    preview(True)
    project = tmp_path / "proj"
    (project / ".kiro" / "agents").mkdir(parents=True)
    _write(project / ".kiro" / "agents", "mine", excludedTools=_IDS)

    assert not _held(await _entry(work_dir=str(project)))


@pytest.mark.asyncio
async def test_a_rule_the_file_writes_on_another_capability_changes_nothing(agents_dir, preview):
    preview(True)
    _write(agents_dir, "mine", excludedTools=_IDS, permissions={"rules": [_ALLOW_READS]})

    assert not _held(await _entry())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "excluded,line", [(_IDS, "invoke_sub_agent only"), (["subagent"], "tools only")]
)
async def test_the_person_is_told_and_the_change_is_recorded(
    agents_dir, preview, monkeypatch, excluded, line
):
    events: list[dict] = []
    monkeypatch.setattr(
        targets,
        "sel",
        lambda: types.SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
    )
    preview(True)
    _write(agents_dir, "mine", excludedTools=excluded)

    extras = await _extras()

    assert "Agent mine excludes" in extras.withheld_credentials_notice
    assert line in extras.withheld_credentials_notice
    assert "As on kiro-cli" in extras.withheld_credentials_notice
    operations = [e["operation"] for e in events]
    assert "kas_subagent_exclusion_read_as_cli" in operations
    assert "kas_subagent_tool_excluded" not in operations


# ── on: what removes delegation on kiro-cli still removes it ─────────────────


@pytest.mark.asyncio
async def test_excluding_the_per_agent_tools_too_still_removes_every_sub_agent(agents_dir, preview):
    preview(True)
    _write(agents_dir, "mine", excludedTools=["invoke_sub_agent", "subagent_*"])

    entry = await _entry()

    assert _held(entry)
    assert entry["excludedTools"] == ["invoke_sub_agent", "subagent_*"]


@pytest.mark.asyncio
@pytest.mark.parametrize("excluded", [None, _IDS], ids=["no-exclusion", "both-names"])
async def test_the_files_own_subagent_deny_stays(agents_dir, preview, excluded):
    preview(True)
    spec = {"permissions": {"rules": [_DENY_ALL_NAMES]}}
    if excluded:
        spec["excludedTools"] = excluded
    _write(agents_dir, "mine", **spec)

    entry = await _entry()

    assert _DENY_ALL_NAMES in _rules(entry)
    # With the exclusion too, no kiro-cli run covers the file: the rule stays.
    assert _held(entry) is bool(excluded)


@pytest.mark.asyncio
async def test_a_named_tools_list_without_the_tool_is_untouched(agents_dir, preview, monkeypatch):
    spec = {"tools": ["fs_write", "read_file", "execute_bash"]}
    _write(agents_dir, "mine", **spec)
    preview(None)
    before = (await _extras()).custom_agents
    preview(True)

    assert (await _extras()).custom_agents == before
    assert not targets.mounts_subagent_tool(before[0])


# ── on: a file no kiro-cli run covers keeps the rule ─────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(_NOT_RUN))
async def test_a_file_that_was_not_run_on_kiro_cli_keeps_the_rule(agents_dir, preview, kind):
    excluded, more = _NOT_RUN[kind]
    _write(agents_dir, "mine", excludedTools=excluded, **more)
    _write(agents_dir, "x", tools=["fs_read"])
    preview(None)
    before = await _extras()
    preview(True)

    after = await _extras()

    assert _held(after.custom_agents[0])
    assert after.custom_agents == before.custom_agents
    assert after.withheld_credentials_notice == before.withheld_credentials_notice


# ── on: every agent Kiro Crew writes, and every other entry, keeps the rule ──


def _shipped(name: str, **more) -> dict:
    shipped = json.loads(_DEFAULTS.read_text(encoding="utf-8"))
    return {"description": "d", "allowedTools": shipped["allowedTools"], **more}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(OWNED_KIRO_AGENT_NAMES - {"kirocrew-mate-worker"}))
@pytest.mark.parametrize("excluded", [_IDS, ["subagent"]], ids=["both-names", "tag"])
async def test_an_agent_kiro_crew_writes_keeps_the_rule(agents_dir, preview, name, excluded):
    _write(agents_dir, name, **_shipped(name, excludedTools=excluded))
    preview(None)
    before = await _extras(name)
    preview(True)

    after = await _extras(name)

    assert _held(after.custom_agents[0])
    assert after.custom_agents == before.custom_agents
    assert after.withheld_credentials_notice == before.withheld_credentials_notice


@pytest.mark.asyncio
async def test_the_default_agent_that_excludes_the_tool_gets_no_mount(agents_dir, preview):
    _write(agents_dir, "kirocrew", **_shipped("kirocrew", excludedTools=_IDS))
    preview(True)

    batch = (await _extras("kirocrew")).custom_agents

    assert [a["id"] for a in batch] == ["kirocrew"]
    assert _held(batch[0])


@pytest.mark.asyncio
async def test_the_default_agents_mount_is_what_it_was(agents_dir, preview, monkeypatch):
    """The mount goes through no step of this reading: the old rule gives the same batch."""
    from kiro_crew.acp import kas_native_batch

    shipped = json.loads(_DEFAULTS.read_text(encoding="utf-8"))
    _write(agents_dir, "kirocrew", **_shipped("kirocrew", tools=shipped["tools"]))
    _write(agents_dir, "mine", tools=["fs_read"], excludedTools=_IDS)
    preview(True)

    batch = (await _extras("kirocrew")).custom_agents
    with monkeypatch.context() as m:
        m.setattr(
            kas_native_batch.kas_excluded_tool_cli,
            "hold_excluded_as_cli",
            lambda entries, *a, **k: targets.deny_excluded_subagent_tool(entries),
        )
        old = (await _extras("kirocrew")).custom_agents

    by_id = {a["id"]: a for a in batch}
    assert "kirocrew-helper" in by_id and "invoke_sub_agent" in by_id["kirocrew"]["tools"]
    # A person's own agent the mount registers as a child keeps the rule too.
    assert _held(by_id["mine"])
    assert batch == old


@pytest.mark.parametrize(
    "agent,where",
    [
        ("kirocrew-mate-worker", "user"),
        ("kirocrew-mate-conductor", "user"),
        ("mine", "crewmate-folder"),
        ("mine", "nowhere"),
    ],
)
def test_a_bots_worker_and_a_crewmate_file_keep_the_rule(agents_dir, tmp_path, agent, where):
    from kiro_crew.acp.kas_excluded_tool_cli import hold_excluded_as_cli

    spec = {"name": agent, "prompt": "p", "tools": ["*"], "excludedTools": _IDS}
    batch = [{"id": agent, "tools": "*", "excludedTools": list(_IDS)}]
    spec_dir = {"user": agents_dir, "crewmate-folder": tmp_path / "mates", "nowhere": None}[where]
    notices: list[str] = []

    [entry] = hold_excluded_as_cli(
        batch, agent, spec, spec_dir=spec_dir, agents_dir=agents_dir, work_dir=None, notices=notices
    )

    assert _held(entry)
    assert notices == []


@pytest.mark.asyncio
async def test_a_registered_target_in_the_shape_keeps_the_rule(agents_dir, preview):
    preview(True)
    _write(
        agents_dir,
        "mine",
        tools=["fs_read", "use_subagent"],
        toolsSettings={"subagent": {"availableAgents": ["mine-target"]}},
    )
    _write(agents_dir, "mine-target", excludedTools=_IDS)

    batch = {a["id"]: a for a in (await _extras()).custom_agents}

    assert _held(batch["mine-target"])
    assert not _held(batch["mine"])


# ── every session the preview does not reach, and off or never set ───────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,kw",
    [
        ("cron:job1", {}),
        ("dashboard:member-ada", {}),
        (_DASHBOARD, {"member_dispatch": True}),
        (_DASHBOARD, {"crew_panel": True}),
        (_DASHBOARD, {"remote_workspace": True}),
    ],
    ids=["a-cron-session", "a-crew-members-chat", "a-member-dispatch", "a-crew-panel", "remote"],
)
@pytest.mark.parametrize("excluded", [_IDS, ["subagent"]], ids=["both-names", "tag"])
async def test_a_session_the_preview_does_not_reach_keeps_the_rule(
    agents_dir, preview, key, kw, excluded
):
    _write(agents_dir, "mine", excludedTools=excluded)
    preview(None)
    before = (await _extras(key=key, **kw)).custom_agents
    preview(True)

    after = (await _extras(key=key, **kw)).custom_agents

    assert _held(after[0])
    assert after == before


@pytest.mark.asyncio
async def test_the_chat_runtime_sharing_preview_keeps_the_rule(agents_dir, preview):
    """A chat on a shared runtime is placed by a spec that starts no native sub-agent."""
    _write(agents_dir, "mine", excludedTools=_IDS)
    preview(True, sharing=True)

    assert _held(await _entry())


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, False], ids=["never-set", "off"])
@pytest.mark.parametrize("excluded", [_IDS, ["subagent"]], ids=["both-names", "tag"])
async def test_off_or_never_set_the_file_starts_no_engine_sub_agent(
    agents_dir, preview, value, excluded
):
    preview(value)
    _write(agents_dir, "mine", excludedTools=excluded)

    extras = await _extras()
    entry = extras.custom_agents[0]

    assert _held(entry)
    assert entry["excludedTools"] == ["use_subagent", "invoke_sub_agent"]
    assert extras.withheld_credentials_notice == ""
