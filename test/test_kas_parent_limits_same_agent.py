"""Each session above a KAS sub-agent run holds it with its own limits.

An agent name is not one set of limits: a write allowlist is anchored at the
directory its session runs in, and the directory (or the session) can decide which
spec a name resolves to. So two sessions on one agent name, in two directories, are
two holders, and a parent on the run's own agent holds the run unless it resolves
to the run's own limits in the run's own directory.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from test_kas_parent_limits import (  # noqa: F401 -- the fixtures register by import
    _GRANTING,
    _LIMITED,
    _PLAIN,
    _WRITE_ALLOWLIST,
    _isolated,
    _judge,
    _pool_decision,
    _shell,
    _shipped_specs,
    _stamped,
    _write,
    agents,
    sessions,
)
from test_tool_call_classification_kas import _kas_builtin_flow

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
import kiro_crew.subagent as subagent_mod
from kiro_crew.acp import kas_parent_limits
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.session_handle import AcpAgentUnavailable
from kiro_crew.acp.types import AcpEvent
from kiro_crew.acp_backends import ACP_BACKEND_KAS
from kiro_crew.hooks import TOOL_DENY
from kiro_crew.subagent_persistence import create_agent_folder

_NOWRITE = {**_PLAIN, "name": "nowrite", "toolsSettings": _WRITE_ALLOWLIST}


@pytest.fixture
def dirs(tmp_path) -> tuple[Path, Path]:
    """Two project directories, each with an ``out`` folder."""
    root = Path(os.path.realpath(tmp_path)) / "review"
    for name in ("a", "b"):
        (root / name / "out").mkdir(parents=True)
    return root / "a", root / "b"


@pytest.fixture
def runs_in(monkeypatch):
    """Session key -> the directory that session runs in."""
    where: dict[str, Path] = {}
    monkeypatch.setattr(
        subagent_mod,
        "parent_project_dirs",
        lambda key: (str(where[key]),) if key in where else (),
    )
    return where


@pytest.fixture
def shifty(monkeypatch, tmp_path, dirs) -> None:
    """One name, two specs: ``shifty`` is a limited file left in the Bot folder, and
    the folder ``b`` declares a plain ``shifty`` that answers the name there."""
    mates = tmp_path / "home" / "mates" / "agents"
    mates.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "mate_agents_dir", lambda: mates)
    _write(mates, {**_LIMITED, "name": "shifty"})
    project = paths_mod.project_agents_dir(dirs[1])
    project.mkdir(parents=True)
    _write(project, {**_PLAIN, "name": "shifty"})


async def _batch_in(agent: str, key: str, cwd: Path) -> list[dict]:
    extras = await KasHarness().session_extras(agent, work_dir=str(cwd), session_key=key)
    return list(extras.custom_agents)


def _write_to(path: Path) -> AcpEvent:
    raw = {"path": str(path), "command": "create"}
    return _kas_builtin_flow("fs_write", title="Write", kind="edit", raw_input=raw)


def _anchors(batch: list[dict], agent: str) -> list[tuple[str, str]]:
    entry = next(a for a in batch if a["id"] == agent)
    carried = entry.get(kas_parent_limits.CREW_PARENT_LIMITS_KEY) or []
    return [(item["agent"], item["cwd"]) for item in carried]


# ── two sessions on one agent name above the run ─────────────────────────────


@pytest.mark.asyncio
async def test_a_grandparent_on_the_parents_agent_keeps_its_own_write_ceiling(
    agents, sessions, dirs, runs_in  # noqa: F811
):
    """``nowrite`` in ``a`` starts ``nowrite`` in ``b``, which starts ``plain``."""
    a, b = dirs
    _write(agents, _NOWRITE)
    sessions.update({"dashboard:gp": "nowrite", "subagent:p1": "nowrite"})
    runs_in.update({"dashboard:gp": a, "subagent:p1": b, "subagent:c2": b})
    create_agent_folder("p1", task="t", agent="nowrite", parent_session="dashboard:gp")
    create_agent_folder("c2", task="t", agent="plain", parent_session="subagent:p1")
    batch = await _batch_in("plain", "subagent:c2", b)

    # The parent's ceiling admits it; the grandparent's, anchored in ``a``, refuses it.
    verdict = _judge(_stamped(_write_to(b / "out" / "update.txt"), batch, "plain", cwd=str(b)))
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-parent-limits")
    assert "'nowrite'" in verdict.reason
    other = _judge(_stamped(_write_to(a / "out" / "update.txt"), batch, "plain", cwd=str(b)))
    assert (other.action, other.tier) == (TOOL_DENY, "spec-parent-limits")
    assert _anchors(batch, "plain") == [("nowrite", str(b)), ("nowrite", str(a))]


@pytest.mark.asyncio
async def test_a_grandparent_whose_name_resolves_to_another_spec_there_still_holds(
    agents, sessions, dirs, runs_in, shifty  # noqa: F811
):
    """The nearer ``shifty`` session, which runs the plain spec, does not stand for
    the one above it, which runs the limited spec."""
    a, b = dirs
    sessions.update({"dashboard:gp": "shifty", "subagent:p1": "shifty"})
    runs_in.update({"dashboard:gp": a, "subagent:p1": b, "subagent:c2": b})
    create_agent_folder("p1", task="t", agent="shifty", parent_session="dashboard:gp")
    create_agent_folder("c2", task="t", agent="plain", parent_session="subagent:p1")
    batch = await _batch_in("plain", "subagent:c2", b)

    verdict = _judge(_stamped(_shell("touch marker"), batch, "plain"))
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-parent-limits")
    assert _anchors(batch, "plain") == [("shifty", str(a))]


@pytest.mark.asyncio
async def test_two_sessions_with_the_same_limits_in_one_directory_hold_once(
    agents, sessions, dirs, runs_in  # noqa: F811
):
    a, _b = dirs
    _write(agents, _NOWRITE)
    sessions.update({"dashboard:gp": "nowrite", "subagent:p1": "nowrite"})
    runs_in.update({"dashboard:gp": a, "subagent:p1": a, "subagent:c2": a})
    create_agent_folder("p1", task="t", agent="nowrite", parent_session="dashboard:gp")
    create_agent_folder("c2", task="t", agent="plain", parent_session="subagent:p1")
    batch = await _batch_in("plain", "subagent:c2", a)

    assert _anchors(batch, "plain") == [("nowrite", str(a))]
    inside = _judge(_stamped(_write_to(a / "out" / "update.txt"), batch, "plain", cwd=str(a)))
    assert inside.tier != "spec-parent-limits"


# ── a parent on the run's own agent ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_parent_on_the_runs_own_agent_in_another_directory_holds_it(
    agents, sessions, dirs, runs_in  # noqa: F811
):
    """``nowrite`` in ``a`` starts ``nowrite`` in ``b``: the run's own ceiling is
    ``b/out``, its parent's is ``a/out``, and the run is refused what either refuses."""
    a, b = dirs
    _write(agents, _NOWRITE)
    sessions["dashboard:p"] = "nowrite"
    runs_in.update({"dashboard:p": a, "subagent:c1": b})
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    batch = await _batch_in("nowrite", "subagent:c1", b)

    verdict = _judge(_stamped(_write_to(b / "out" / "update.txt"), batch, "nowrite", cwd=str(b)))
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-parent-limits")
    assert _anchors(batch, "nowrite") == [("nowrite", str(a))]


@pytest.mark.asyncio
async def test_a_parent_whose_own_agent_name_resolves_to_another_spec_holds_the_run(
    agents, sessions, dirs, runs_in, shifty  # noqa: F811
):
    a, b = dirs
    sessions["dashboard:p"] = "shifty"
    runs_in.update({"dashboard:p": a, "subagent:c1": b})
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    batch = await _batch_in("shifty", "subagent:c1", b)

    verdict = _judge(_stamped(_shell("touch marker"), batch, "shifty"))
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-parent-limits")


#: A spec with every kind of limit a parent hands down: an engine deny, shell and
#: write allow and deny lists, and a blocked URL.
_EVERY_LIMIT = {
    **_GRANTING,
    "name": "everylimit",
    "permissions": {"rules": [{"capability": "shell", "effect": "deny", "match": ["rm *"]}]},
    "toolsSettings": {
        **_GRANTING["toolsSettings"],
        "web_fetch": {"blocked": [r"https://example\.com/private/.*"]},
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", [_NOWRITE, _LIMITED, _EVERY_LIMIT], ids=lambda s: s["name"])
@pytest.mark.parametrize("known", [True, False], ids=["one-directory", "directory-unknown"])
async def test_a_parent_on_the_runs_own_agent_in_its_directory_adds_nothing(
    agents, sessions, dirs, runs_in, known, spec  # noqa: F811
):
    """The same spec anchored in the same place is the run's own limit, once. A
    parent whose directory is not known is anchored at the run's."""
    a, _b = dirs
    name = spec["name"]
    _write(agents, spec)
    before = await _batch_in(name, "subagent:c1", a)
    sessions["dashboard:p"] = name
    if known:
        runs_in.update({"dashboard:p": a, "subagent:c1": a})
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    assert await _batch_in(name, "subagent:c1", a) == before


@pytest.mark.asyncio
async def test_an_unreadable_grandparent_on_the_parents_agent_refuses_the_run(
    agents, sessions, dirs, runs_in  # noqa: F811
):
    a, b = dirs
    _write(agents, _NOWRITE)
    sessions.update(
        {"dashboard:gp": subagent_mod.ParentRecordUnreadable("torn"), "subagent:p1": "nowrite"}
    )
    runs_in.update({"subagent:p1": b, "subagent:c2": b})
    create_agent_folder("p1", task="t", agent="nowrite", parent_session="dashboard:gp")
    create_agent_folder("c2", task="t", agent="plain", parent_session="subagent:p1")
    with pytest.raises(AcpAgentUnavailable, match="could not be read"):
        await _batch_in("plain", "subagent:c2", b)


# ── the warm pool ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("child_dir", "decision"),
    [("b", "bypass_parent_denies"), ("a", "miss_empty")],
    ids=["another-directory", "one-directory"],
)
async def test_the_pool_decision_follows_the_same_rule(
    agents, sessions, dirs, runs_in, child_dir, decision  # noqa: F811
):
    a, b = dirs
    _write(agents, _NOWRITE)
    sessions["dashboard:p"] = "nowrite"
    runs_in.update({"dashboard:p": a, "subagent:c1": {"a": a, "b": b}[child_dir]})
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    assert await _pool_decision("subagent:c1", "nowrite", ACP_BACKEND_KAS) == [decision]


# ── shipped agents ───────────────────────────────────────────────────────────


def _held_by_own_agent_elsewhere(name: str, known: dict, runs_in: dict, dirs) -> bool:
    """Whether a run of *name* in ``b`` is held by a parent on *name* in ``a``."""
    a, b = dirs
    child = [{"id": name, "prompt": "p", "tools": "*"}]
    known["dashboard:p"] = name
    runs_in.update({"dashboard:p": a, "subagent:c1": b})
    kas_parent_limits._NOTED.clear()
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    return kas_parent_limits.hold_to_parents(child, name, "subagent:c1") is not child


def test_no_shipped_agent_holds_a_run_on_its_own_agent_elsewhere(
    agents, sessions, dirs, runs_in  # noqa: F811
):
    specs = _shipped_specs()
    assert len(specs) > 10
    for spec in specs:
        _write(agents, spec)
        assert not _held_by_own_agent_elsewhere(spec["name"], sessions, runs_in, dirs), spec["name"]


def test_no_installed_agent_holds_a_run_on_its_own_agent_elsewhere(
    tmp_path, monkeypatch, sessions, dirs, runs_in  # noqa: F811
):
    """Every spec a real rebuild writes, a Bot's two service agents left out as in
    the first table: the worker's spec is merged per Bot from what a person added."""
    from test_agent_install_composition_fail_closed import _Rig

    from kiro_crew.agent_files import OWNED_KIRO_AGENT_NAMES, is_mate_service_agent

    rig = _Rig(tmp_path, monkeypatch)
    agent_mod.rebuild_agent_config()
    mate = paths_mod.mate_agents_dir()
    installed = sorted(
        {p.stem for d in (rig.agents, mate) if d.is_dir() for p in d.glob("*.json")}
        & OWNED_KIRO_AGENT_NAMES
    )
    installed = [name for name in installed if not is_mate_service_agent(name)]
    assert len(installed) >= 12, installed
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: rig.agents)
    for name in installed:
        assert not _held_by_own_agent_elsewhere(name, sessions, runs_in, dirs), name
