"""Stale-config detection: does a live chat run on config changed since it started?

Covers the fingerprint (``dashboard/stale_config.py``) and its per-backend spec
resolution, the record kept against the provider a turn ran on
(``dashboard/config_staleness.py``), the status and the slot badge it drives,
the warm-pool and prewarm records, and the read-only ``session_config_status``
verb, its route and its MCP tool -- including that a principal the verb refuses
is refused identically through the tool. Nothing here relaunches a process.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.agent import DerivedSpecStale
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard import config_staleness as cs
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import stale_config
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.dashboard.stale_config import (
    ConfigFingerprint,
    SpawnInputs,
    compute_fingerprint,
    is_stale,
    make_record,
)
from kiro_crew.mcp_dashboard import SESSION_CONTROL_TOOLS, TABLE
from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext


class _Provider:
    """A weak-referenceable provider double with the attributes the gate reads."""

    #: The ``LLMProvider`` default: no warm-pool receipt.
    pool_spawn_config = None

    def __init__(self, *, hot_reload: bool = False, active: bool = False) -> None:
        self.mcp_config_hot_reload = hot_reload
        self._active = active

    def has_active_turn(self) -> bool:
        return self._active


_A = ConfigFingerprint(reconcilable="r-a", spawn_only="s-a")
_CHANGED = ConfigFingerprint(reconcilable="r-b", spawn_only="s-a")


@pytest.fixture
def kiro_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "kiro"
    (home / "agents").mkdir(parents=True)
    (home / "settings").mkdir()
    monkeypatch.setattr("kiro_crew.config.paths.kiro_home", lambda: home)
    monkeypatch.setattr("kiro_crew.agent.kiro_agents_dir_path", lambda: home / "agents")
    # The Windows sweep spells the agents dir itself rather than through the
    # resolving accessor; the redirect it honours points it here too.
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", home / "agents")
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", home / "settings" / "mcp.json")
    return home


@pytest.fixture(params=[False, True], ids=["shared-gate", "windows-gate"])
def either_gate(request, monkeypatch) -> bool:
    """Run the test through the shared path gate and through the Windows one."""
    monkeypatch.setattr(stale_config, "_WINDOWS", request.param)
    return request.param


def _gate_refuses(monkeypatch, refused) -> None:
    """Make the path gate refuse every raw path *refused* names, on either OS.

    ``_gated_dir`` routes to the shared ``validate_file_path`` or, on Windows,
    to the lexical ``_gated_dir_windows``; both are wrapped so a faked refusal
    holds whichever one runs.
    """
    for name in ("validate_file_path", "_gated_dir_windows"):
        real = getattr(stale_config, name)
        monkeypatch.setattr(
            stale_config, name, lambda raw, _real=real: None if refused(raw) else _real(raw)
        )


def _write_spec(home: Path, body: dict) -> None:
    (home / "agents" / "kirocrew.json").write_text(json.dumps({"name": "kirocrew", **body}))


def _write_workspace_spec(project: Path, body: dict) -> None:
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "kirocrew.json").write_text(json.dumps({"name": "kirocrew", **body}))


def _fp(project: str = "", backend: str = "") -> ConfigFingerprint:
    return compute_fingerprint(SpawnInputs(agent="kirocrew", project=project), backend=backend)


_SERVER = {"a": {"command": "a"}}


def _md_spec(path: Path, prompt: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nname: kirocrew\n---\n%s\n" % prompt)


def _pool_manager(monkeypatch, tmp_path, *, pool_agent: str = "kirocrew"):
    from test_session_pool import _make_manager

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "kirocrew_home"))
    mgr, _factory = _make_manager(pool_size=1, pool_agent=pool_agent)
    return mgr


def test_unchanged_config_fingerprints_identically(kiro_home):
    _write_spec(kiro_home, {"mcpServers": _SERVER})
    assert _fp() == _fp()


def test_an_mcp_server_edit_in_the_spec_changes_only_the_reconcilable_half(kiro_home):
    _write_spec(kiro_home, {"mcpServers": _SERVER})
    before = _fp()
    _write_spec(kiro_home, {"mcpServers": {"a": {"command": "a", "env": {"K": "v"}}}})
    after = _fp()
    assert after.reconcilable != before.reconcilable
    assert after.spawn_only == before.spawn_only


@pytest.mark.parametrize(
    "field",
    [("disabled", True), ("disabledTools", ["t"])],
    ids=["disabled", "disabledTools"],
)
def test_a_server_disabled_toggle_is_reconcilable(kiro_home, field):
    _write_spec(kiro_home, {"mcpServers": _SERVER})
    before = _fp()
    _write_spec(kiro_home, {"mcpServers": {"a": {"command": "a", field[0]: field[1]}}})
    after = _fp()
    assert after.reconcilable != before.reconcilable
    assert after.spawn_only == before.spawn_only


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("prompt", "be terse"),
        ("hooks", {"agentSpawn": [{"command": "echo hi"}]}),
        ("tools", ["fs_read"]),
        ("allowedTools", ["fs_read"]),
        ("allowedTools", ["@a"]),
        ("resources", ["file://README.md"]),
    ],
    ids=["prompt", "hooks", "tools", "allowedTools", "allowedTools-ref", "resources"],
)
def test_a_user_spec_non_mcp_field_edit_is_spawn_only(kiro_home, key, value):
    _write_spec(kiro_home, {"mcpServers": _SERVER})
    before = _fp()
    _write_spec(kiro_home, {"mcpServers": _SERVER, key: value})
    after = _fp()
    assert after.spawn_only != before.spawn_only
    assert after.reconcilable == before.reconcilable


def test_an_added_server_ref_in_tools_is_reconcilable(kiro_home):
    _write_spec(kiro_home, {"mcpServers": _SERVER, "tools": ["fs_read"]})
    before = _fp()
    _write_spec(kiro_home, {"mcpServers": _SERVER, "tools": ["fs_read", "@a"]})
    after = _fp()
    assert after.tool_refs == frozenset({stale_config._part_digest("@a")})
    assert before.tool_refs == frozenset()
    assert after.spawn_only == before.spawn_only
    assert not is_stale(before, after, hot_reloads=True)
    assert is_stale(before, after, hot_reloads=False)
    # A built-in tool entry in the same list stays spawn-only.
    _write_spec(kiro_home, {"mcpServers": _SERVER, "tools": ["fs_read", "fs_write", "@a"]})
    again = _fp()
    assert again.spawn_only != after.spawn_only
    assert again.reconcilable == after.reconcilable


@pytest.mark.parametrize(
    ("before_list", "after_list"),
    [(["fs_read"], ["fs_read", "@a"]), (["fs_read", "@a"], ["fs_read"]), (None, ["@a"])],
    ids=["grant-added", "grant-revoked", "list-created"],
)
def test_all_of_allowed_tools_is_spawn_only(kiro_home, before_list, after_list):
    """The auto-approve list: a revoked ``@server`` grant is stale on every provider."""
    base = {"mcpServers": _SERVER, "tools": ["@a"]}
    _write_spec(kiro_home, base if before_list is None else {**base, "allowedTools": before_list})
    before = _fp()
    _write_spec(kiro_home, {**base, "allowedTools": after_list})
    after = _fp()
    assert after.spawn_only != before.spawn_only
    assert after.reconcilable == before.reconcilable
    assert is_stale(before, after, hot_reloads=True)


def test_a_workspace_allowed_tools_grant_is_stale_everywhere(kiro_home, tmp_path):
    project = tmp_path / "proj"
    _write_workspace_spec(project, {"mcpServers": _SERVER, "tools": ["@a"]})
    before = _fp(project=str(project))
    _write_workspace_spec(project, {"mcpServers": _SERVER, "tools": ["@a"], "allowedTools": ["@a"]})
    assert is_stale(before, _fp(project=str(project)), hot_reloads=True)


@pytest.mark.parametrize("hot_reload", [False, True], ids=["plain", "hot-reloading"])
def test_a_server_ref_removed_from_user_tools_is_spawn_only(kiro_home, hot_reload):
    _write_spec(kiro_home, {"mcpServers": _SERVER, "tools": ["fs_read", "@a"]})
    before = _fp()
    _write_spec(kiro_home, {"mcpServers": _SERVER, "tools": ["fs_read"]})
    assert is_stale(before, _fp(), hot_reloads=hot_reload)


def test_a_server_ref_removed_from_workspace_tools_is_stale(kiro_home, tmp_path):
    project = tmp_path / "proj"
    _write_workspace_spec(project, {"mcpServers": _SERVER, "tools": ["@a"]})
    before = _fp(project=str(project))
    _write_workspace_spec(project, {"mcpServers": _SERVER, "tools": []})
    after = _fp(project=str(project))
    assert is_stale(before, after, hot_reloads=True)
    assert is_stale(before, after, hot_reloads=False)


@pytest.mark.parametrize("where", ["spec", "global-mcp", "workspace-mcp"])
@pytest.mark.parametrize("how", ["unreadable", "refused"])
def test_an_existing_input_that_cannot_be_read_is_named_unreadable(
    kiro_home, tmp_path, monkeypatch, either_gate, where, how
):
    project = tmp_path / "proj"
    (project / ".kiro" / "settings").mkdir(parents=True)
    _write_spec(kiro_home, {"mcpServers": _SERVER})
    target = {
        "spec": kiro_home / "agents" / "kirocrew.json",
        "global-mcp": kiro_home / "settings" / "mcp.json",
        "workspace-mcp": project / ".kiro" / "settings" / "mcp.json",
    }[where]
    target.write_text("{}")
    if where == "spec" or how == "unreadable":
        # Every input is read through a pin of its directory, not by its gated path.
        real_spec = stale_config._read_pinned_spec
        monkeypatch.setattr(
            stale_config,
            "_read_pinned_spec",
            lambda pinned, path, **kw: (
                (how, None) if path == target else real_spec(pinned, path, **kw)
            ),
        )
    else:
        _gate_refuses(monkeypatch, lambda raw: raw == str(target))
    name = {"spec": "spec", "global-mcp": "global_mcp", "workspace-mcp": "workspace_mcp"}[where]
    assert _fp(project=str(project)).unreadable == frozenset({name})


def test_an_absent_input_is_a_change_not_an_error(kiro_home):
    _write_spec(kiro_home, {"mcpServers": _SERVER})
    (kiro_home / "settings" / "mcp.json").write_text('{"mcpServers": {"x": {}}}')
    before = _fp()
    (kiro_home / "settings" / "mcp.json").unlink()
    after = _fp()
    assert after.reconcilable != before.reconcilable


def test_a_model_only_rewrite_of_the_spec_changes_nothing(kiro_home):
    _write_spec(kiro_home, {"mcpServers": _SERVER, "model": "model-a"})
    before = _fp()
    _write_spec(kiro_home, {"mcpServers": _SERVER, "model": "model-b"})
    assert _fp() == before
    _write_spec(kiro_home, {"mcpServers": _SERVER})
    assert _fp() == before


def test_a_reformatted_spec_with_the_same_fields_is_not_a_change(kiro_home):
    body = {"name": "kirocrew", "mcpServers": _SERVER, "prompt": "p"}
    (kiro_home / "agents" / "kirocrew.json").write_text(json.dumps(body))
    before = _fp()
    (kiro_home / "agents" / "kirocrew.json").write_text(json.dumps(body, indent=4))
    assert _fp() == before


def test_a_workspace_spec_non_mcp_edit_is_spawn_only(kiro_home, tmp_path):
    project = tmp_path / "proj"
    _write_workspace_spec(project, {"mcpServers": _SERVER})
    before = _fp(project=str(project))
    _write_workspace_spec(project, {"mcpServers": _SERVER, "prompt": "planted"})
    after = _fp(project=str(project))
    assert after.spawn_only != before.spawn_only
    assert after.reconcilable == before.reconcilable


def test_a_workspace_spec_mcp_edit_is_reconcilable(kiro_home, tmp_path):
    project = tmp_path / "proj"
    _write_workspace_spec(project, {"mcpServers": _SERVER})
    before = _fp(project=str(project))
    _write_workspace_spec(project, {"mcpServers": {"b": {"command": "b"}}})
    after = _fp(project=str(project))
    assert after.reconcilable != before.reconcilable
    assert after.spawn_only == before.spawn_only


def test_planting_a_workspace_spec_is_stale(kiro_home, tmp_path):
    """A workspace spec that appears shadows the user one, which is a change."""
    project = tmp_path / "proj"
    project.mkdir()
    _write_spec(kiro_home, {"mcpServers": _SERVER})
    recorded = _fp(project=str(project))
    _write_workspace_spec(project, {"mcpServers": _SERVER, "prompt": "planted"})
    assert is_stale(recorded, _fp(project=str(project)), hot_reloads=True)
    assert is_stale(recorded, _fp(project=str(project)), hot_reloads=False)


@pytest.mark.parametrize("hot_reloads", [True, False])
def test_adding_a_shadowed_user_spec_is_not_stale(kiro_home, tmp_path, hot_reloads):
    """The workspace spec is loaded, so a same-named user spec it shadows is no change."""
    project = tmp_path / "proj"
    _write_workspace_spec(project, {"mcpServers": _SERVER, "prompt": "workspace"})
    recorded = _fp(project=str(project))
    assert dict(recorded.parts)["spec_ws"]
    _write_spec(kiro_home, {"mcpServers": _SERVER, "prompt": "user"})
    current = _fp(project=str(project))
    assert dict(current.parts)["user_spec_path"]
    assert not is_stale(recorded, current, hot_reloads=hot_reloads)


@pytest.mark.parametrize("hot_reloads", [True, False])
def test_removing_a_shadowed_user_spec_is_not_stale(kiro_home, tmp_path, hot_reloads):
    project = tmp_path / "proj"
    _write_workspace_spec(project, {"mcpServers": _SERVER, "prompt": "workspace"})
    _write_spec(kiro_home, {"mcpServers": _SERVER, "prompt": "user"})
    recorded = _fp(project=str(project))
    assert dict(recorded.parts)["user_spec_path"]
    (kiro_home / "agents" / "kirocrew.json").unlink()
    current = _fp(project=str(project))
    assert not dict(current.parts)["user_spec_path"]
    assert not is_stale(recorded, current, hot_reloads=hot_reloads)


def test_an_mcp_json_edit_changes_the_fingerprint(kiro_home):
    _write_spec(kiro_home, {})
    before = _fp()
    (kiro_home / "settings" / "mcp.json").write_text('{"mcpServers": {"x": {}}}')
    assert _fp().reconcilable != before.reconcilable


def test_the_global_mcp_json_hashed_is_the_one_the_session_reads(kiro_home, tmp_path, monkeypatch):
    """Not a ``KIRO_HOME``-relative path: the fixed file the spawned session reads.

    ``kiro_home()`` and the agent's own ``mcp.json`` path are pointed at
    different directories, as they are when ``KIRO_HOME`` is set; an edit to
    the file the session reads must move the fingerprint, and one to the
    ``KIRO_HOME`` copy must not.
    """
    read_by_session = tmp_path / "home" / ".kiro" / "settings" / "mcp.json"
    read_by_session.parent.mkdir(parents=True)
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", read_by_session)
    _write_spec(kiro_home, {})
    before = _fp()
    (kiro_home / "settings" / "mcp.json").write_text('{"mcpServers": {"x": {}}}')
    assert _fp() == before
    read_by_session.write_text('{"mcpServers": {"x": {}}}')
    assert _fp().reconcilable != before.reconcilable


def test_a_workspace_mcp_json_edit_changes_the_fingerprint(kiro_home, tmp_path):
    _write_spec(kiro_home, {})
    project = tmp_path / "proj"
    (project / ".kiro" / "settings").mkdir(parents=True)
    before = _fp(project=str(project))
    (project / ".kiro" / "settings" / "mcp.json").write_text("{}")
    after = _fp(project=str(project))
    assert dict(after.parts)["workspace_mcp"] != dict(before.parts)["workspace_mcp"]


@pytest.mark.parametrize("hot_reloads", [True, False])
def test_a_project_mcp_json_change_is_stale_only_where_mcp_is_not_hot_applied(
    kiro_home, tmp_path, hot_reloads
):
    """The agent can write ``.kiro/settings/mcp.json``: never relaunch onto it unasked."""
    _write_spec(kiro_home, {})
    project = tmp_path / "proj"
    (project / ".kiro" / "settings").mkdir(parents=True)
    before = _fp(project=str(project))
    (project / ".kiro" / "settings" / "mcp.json").write_text('{"mcpServers": {"x": {}}}')
    after = _fp(project=str(project))

    stale = is_stale(before, after, hot_reloads=hot_reloads)

    assert stale is (not hot_reloads)


def test_a_user_mcp_json_change_is_stale_where_mcp_is_not_hot_applied(
    kiro_home, tmp_path, monkeypatch
):
    """The user-level mcp.json is stale on a provider that does not hot-reload MCP."""
    user_mcp = tmp_path / "home" / ".kiro" / "settings" / "mcp.json"
    user_mcp.parent.mkdir(parents=True)
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", user_mcp)
    _write_spec(kiro_home, {})
    before = _fp()
    user_mcp.write_text('{"mcpServers": {"x": {}}}')
    assert is_stale(before, _fp(), hot_reloads=False)


def _edit_both_mcp_json(home: Path, project: Path, servers: dict) -> None:
    body = json.dumps({"mcpServers": servers})
    (home / "settings" / "mcp.json").write_text(body)
    (project / ".kiro" / "settings").mkdir(parents=True, exist_ok=True)
    (project / ".kiro" / "settings" / "mcp.json").write_text(body)


@pytest.mark.parametrize(
    "backend, flag, mounted",
    [
        ("", None, True),
        ("", True, True),
        ("", False, False),
        ("kas", None, False),
        ("kas", True, True),
        ("kas", False, False),
        ("claude", None, False),
        ("claude", True, False),
    ],
    ids=[
        "kiro-absent",
        "kiro-true",
        "kiro-false",
        "kas-absent",
        "kas-true",
        "kas-false",
        "array-absent",
        "array-true",
    ],
)
def test_an_mcp_json_server_edit_is_stale_only_when_the_spec_mounts_the_file(
    kiro_home, tmp_path, backend, flag, mounted
):
    """``includeMcpJson``, read the way the chat's backend reads it, decides the comparison.

    A spec that opts out never mounts the files' servers, so adding one there
    is nothing a Reload could apply; kiro-cli reads an absent flag as true, KAS
    as false, and an array-backed backend is handed the spec's servers alone.
    """
    _write_spec(kiro_home, {} if flag is None else {"includeMcpJson": flag})
    project = tmp_path / "proj"
    _edit_both_mcp_json(kiro_home, project, {})
    before = _fp(project=str(project), backend=backend)
    _edit_both_mcp_json(kiro_home, project, {"x": {"command": "x"}})
    after = _fp(project=str(project), backend=backend)

    assert is_stale(before, after, hot_reloads=False) is mounted
    assert bool(stale_config.changed_inputs(before, after)) is mounted
    assert dict(after.parts)["mcp_json"] == ("1" if mounted else "0")


@pytest.mark.parametrize("where", ["global", "workspace"])
def test_one_mcp_json_server_edit_is_not_stale_for_a_spec_that_opts_out(kiro_home, tmp_path, where):
    """Each file on its own: the workspace one is compared outside the digests."""
    _write_spec(kiro_home, {"includeMcpJson": False})
    project = tmp_path / "proj"
    _edit_both_mcp_json(kiro_home, project, {})
    before = _fp(project=str(project))
    path = (
        kiro_home / "settings" / "mcp.json"
        if where == "global"
        else project / ".kiro" / "settings" / "mcp.json"
    )
    path.write_text('{"mcpServers": {"x": {"command": "x"}}}')
    after = _fp(project=str(project))

    assert dict(after.parts)[f"{where}_mcp"] != dict(before.parts)[f"{where}_mcp"]
    assert not is_stale(before, after, hot_reloads=False)
    assert stale_config.changed_inputs(before, after) == frozenset()


@pytest.mark.parametrize("where", ["global", "workspace"])
@pytest.mark.parametrize(
    "entry",
    [{"command": "x", "disabled": True}, {"command": "x", "disabledTools": ["t"]}],
    ids=["disabled", "disabledTools"],
)
def test_an_mcp_json_switch_off_is_stale_even_for_a_spec_that_opts_out(
    kiro_home, tmp_path, where, entry
):
    """Crew reads every server's ``disabled`` / ``disabledTools`` at spawn whatever the flag."""
    _write_spec(kiro_home, {"includeMcpJson": False})
    project = tmp_path / "proj"
    _edit_both_mcp_json(kiro_home, project, {"x": {"command": "x"}})
    before = _fp(project=str(project))
    path = (
        kiro_home / "settings" / "mcp.json"
        if where == "global"
        else project / ".kiro" / "settings" / "mcp.json"
    )
    path.write_text(json.dumps({"mcpServers": {"x": entry}}))
    after = _fp(project=str(project))

    assert is_stale(before, after, hot_reloads=False)
    assert stale_config.changed_inputs(before, after) == {f"{where}_mcp"}


def test_a_managed_server_declaration_in_mcp_json_is_stale_for_a_spec_that_opts_out(
    kiro_home, tmp_path
):
    """A Crew server's declaration in the files decides its per-session element at spawn."""
    from kiro_crew.acp.session_mcp import IDENTITY_BOUND_SERVERS

    assert stale_config._IDENTITY_BOUND_SERVERS == frozenset(IDENTITY_BOUND_SERVERS)
    name = IDENTITY_BOUND_SERVERS[-1]
    _write_spec(kiro_home, {"includeMcpJson": False})
    project = tmp_path / "proj"
    _edit_both_mcp_json(kiro_home, project, {name: {"command": "a"}})
    before = _fp(project=str(project))
    _edit_both_mcp_json(kiro_home, project, {name: {"command": "a", "timeout": 5}})

    assert is_stale(before, _fp(project=str(project)), hot_reloads=False)


@pytest.mark.parametrize("hot_reloads", [True, False])
@pytest.mark.parametrize(
    "before_flag, after_flag",
    [(None, False), (True, False), (False, True), (False, None)],
    ids=["absent-to-false", "true-to-false", "false-to-true", "false-to-absent"],
)
def test_toggling_include_mcp_json_is_a_spawn_only_spec_change(
    kiro_home, before_flag, after_flag, hot_reloads
):
    """The flag is read at spawn: a toggle is stale on every provider, MCP hot-reload or not."""
    (kiro_home / "settings" / "mcp.json").write_text('{"mcpServers": {"x": {"command": "x"}}}')
    _write_spec(kiro_home, {} if before_flag is None else {"includeMcpJson": before_flag})
    before = _fp()
    _write_spec(kiro_home, {} if after_flag is None else {"includeMcpJson": after_flag})
    after = _fp()

    assert after.spawn_only != before.spawn_only
    assert is_stale(before, after, hot_reloads=hot_reloads)
    assert "spec" in stale_config.changed_inputs(before, after)


def test_an_unreadable_spec_keeps_the_recorded_include_mcp_json_reading(
    kiro_home, tmp_path, monkeypatch
):
    """With no spec to read, the opt-out recorded at spawn still governs the files' comparison."""
    _write_spec(kiro_home, {"includeMcpJson": False})
    project = tmp_path / "proj"
    _edit_both_mcp_json(kiro_home, project, {})
    before = _fp(project=str(project))
    spec_path = kiro_home / "agents" / "kirocrew.json"
    real_spec = stale_config._read_pinned_spec
    monkeypatch.setattr(
        stale_config,
        "_read_pinned_spec",
        lambda pinned, path, **kw: (
            ("unreadable", None) if path == spec_path else real_spec(pinned, path, **kw)
        ),
    )
    _edit_both_mcp_json(kiro_home, project, {"x": {"command": "x"}})
    current = _fp(project=str(project))
    assert "spec" in current.unreadable

    carried = stale_config.carry_forward(before, current)

    assert not is_stale(before, carried, hot_reloads=False)
    assert stale_config.changed_inputs(before, carried) == frozenset()


def test_a_backend_change_moves_the_spawn_only_half(kiro_home):
    _write_spec(kiro_home, {})
    base = _fp()
    assert _fp(backend="claude").spawn_only != base.spawn_only


_HOOKS_V1 = {"preToolUse": [{"command": "echo one"}]}
_HOOKS_V2 = {"preToolUse": [{"command": "echo two"}]}


@pytest.mark.parametrize("backend", ["codex", "claude"])
def test_a_hooks_edit_on_a_backend_without_a_hooks_channel_is_not_stale(kiro_home, backend):
    """The mirror rules ``hooks`` no-channel there, so a reload would change nothing."""
    _write_spec(kiro_home, {"hooks": _HOOKS_V1})
    before = _fp(backend=backend)
    _write_spec(kiro_home, {"hooks": _HOOKS_V2})
    after = _fp(backend=backend)
    assert not is_stale(before, after, hot_reloads=False)
    # A field the backend does receive still badges on the same backend.
    _write_spec(kiro_home, {"hooks": _HOOKS_V2, "prompt": "changed"})
    assert is_stale(before, _fp(backend=backend), hot_reloads=False)


@pytest.mark.parametrize("backend", ["", "goose", "opencode"])
def test_a_hooks_edit_on_a_backend_that_runs_hooks_is_stale(kiro_home, backend):
    """kiro-cli runs the field itself; on goose and opencode Crew fires it."""
    _write_spec(kiro_home, {"hooks": _HOOKS_V1})
    before = _fp(backend=backend)
    _write_spec(kiro_home, {"hooks": _HOOKS_V2})
    assert is_stale(before, _fp(backend=backend), hot_reloads=False)


def test_an_unparseable_spec_is_a_change(kiro_home):
    _write_spec(kiro_home, {"mcpServers": _SERVER})
    before = _fp()
    (kiro_home / "agents" / "kirocrew.json").write_text("{not json")
    after = _fp()
    assert after != before


_TOO_DEEP = "[" * 100000


def test_a_spec_nested_past_the_parser_depth_reads_as_unparseable(kiro_home):
    """``json.loads`` raises ``RecursionError`` on it, which is a syntax error too."""
    _write_spec(kiro_home, {"mcpServers": _SERVER})
    before = _fp()
    (kiro_home / "agents" / "kirocrew.json").write_text(_TOO_DEEP)
    after = _fp()
    assert after != before
    (kiro_home / "agents" / "kirocrew.json").write_text("{not json")
    # The same outcome as a syntax error.
    assert _fp() == after


def test_an_mcp_json_nested_past_the_parser_depth_reads_as_unparseable(kiro_home):
    mcp = kiro_home / "settings" / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": _SERVER}))
    assert stale_config._read_json(mcp)[0] == "ok"
    mcp.write_text(_TOO_DEEP)
    marker, document = stale_config._read_json(mcp)
    assert marker.startswith("unparseable:") and document is None
    _fp()


def test_a_too_deep_sibling_spec_in_a_checkout_is_skipped_like_a_syntax_error(kiro_home, tmp_path):
    project = tmp_path / "proj"
    _write_workspace_spec(project, {"mcpServers": _SERVER})
    agents = project / ".kiro" / "agents"
    (agents / "broken.json").write_text("{not json")
    syntax_error = _fp(project=str(project))
    (agents / "broken.json").write_text(_TOO_DEEP)
    assert _fp(project=str(project)) == syntax_error


def test_an_absent_spec_and_an_unreadable_one_are_different(tmp_path):
    missing = tmp_path / "nope.json"
    assert stale_config._read_json(missing)[0] == "absent"
    assert stale_config._read_json(tmp_path)[0] == "unreadable"


def test_a_workspace_mcp_json_linked_at_a_credential_file_is_never_read(
    kiro_home, tmp_path, either_gate
):
    """The workspace file is workspace-controlled: it goes through the credential gate.

    A link planted at ``<project>/.kiro/settings/mcp.json`` that points at a
    credential file hashes as ``refused`` -- the shared gate screens it before
    the file is probed, and the Windows gate refuses any link at the name
    through the pinned directory without resolving it -- so its bytes never
    reach the digest.
    """

    project = tmp_path / "proj"
    settings = project / ".kiro" / "settings"
    settings.mkdir(parents=True)
    credential = Path(os.path.expanduser("~")) / ".aws" / "credentials"
    try:
        (settings / "mcp.json").symlink_to(credential)
    except OSError:
        pytest.skip("this host cannot create a symlink")

    assert stale_config._read_json(settings / "mcp.json")[0] == "refused"


def test_a_refused_file_never_reaches_the_bounded_read(tmp_path, monkeypatch, either_gate):
    target = tmp_path / "mcp.json"
    target.write_text("{}")
    _gate_refuses(monkeypatch, lambda _raw: True)
    pin = MagicMock(side_effect=AssertionError("a refused path was pinned"))
    monkeypatch.setattr(stale_config, "_pin", pin)

    assert stale_config._read_json(target)[0] == "refused"
    pin.assert_not_called()


def test_a_readable_file_is_parsed_through_a_pin_of_its_directory(tmp_path, monkeypatch):
    """The admitted file is read through the pinned parent, never reopened by name."""
    target = (tmp_path / "mcp.json").resolve()
    target.write_text('{"mcpServers": {}}')
    pinned_at: list[Path] = []
    read: list[str] = []
    real_pin = stale_config._pin
    real_read = stale_config._read_pinned_spec

    def _spy_pin(admitted: Path):
        pinned_at.append(admitted)
        return real_pin(admitted)

    def _spy_read(pinned, path, **kw):
        read.append(kw.get("name") or path.name)
        return real_read(pinned, path, **kw)

    monkeypatch.setattr(stale_config, "_pin", _spy_pin)
    monkeypatch.setattr(stale_config, "_read_pinned_spec", _spy_read)

    assert stale_config._read_json(target) == ("ok", {"mcpServers": {}})
    assert pinned_at == [target.parent]
    assert read == ["mcp.json"]


@pytest.mark.skipif(os.name == "nt", reason="creating a symlink needs privilege on Windows")
@pytest.mark.parametrize("where", ["workspace", "global"])
@pytest.mark.parametrize("ancestor_name", ["settings", ".kiro"])
def test_an_mcp_json_ancestor_swapped_for_a_link_after_the_screen_is_never_followed(
    kiro_home, tmp_path, monkeypatch, where, ancestor_name
):
    """The gate admits ``.../settings/mcp.json``; an ancestor turns into a link before the read.

    Re-resolving the admitted path by name would follow it -- on Windows a
    junction aimed at a UNC share is an outbound SMB authentication on every
    turn and sweep. The read goes through a component-wise pin of the parent,
    which refuses the link, so the input is unreadable and the planted file
    behind the link is never read.
    """
    if where == "workspace":
        root = tmp_path / "proj" / ".kiro"
    else:
        # The fixture's home stands in for ``~/.kiro``.
        root = kiro_home
    target = root / "settings" / "mcp.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"mcpServers": _SERVER}))
    share = tmp_path / "share"
    (share / "settings").mkdir(parents=True)
    (share / "settings" / "mcp.json").write_text(json.dumps({"mcpServers": {"planted": {}}}))
    ancestor = root / "settings" if ancestor_name == "settings" else root
    decoy = share / "settings" if ancestor_name == "settings" else share
    real_gate = stale_config._gated_dir

    def _screen_then_swap(candidate: Path):
        admitted = real_gate(candidate)
        if candidate == target and not ancestor.is_symlink():
            ancestor.rename(tmp_path / f"moved-{where}-{ancestor_name}")
            ancestor.symlink_to(decoy, target_is_directory=True)
        return admitted

    monkeypatch.setattr(stale_config, "_gated_dir", _screen_then_swap)

    assert stale_config._read_json(target) == ("unreadable", None)


@pytest.mark.parametrize("where", ["workspace", "global"])
def test_an_mcp_json_in_a_plain_directory_is_still_read(kiro_home, tmp_path, where):
    """The pinned read is a no-op for an ordinary file: its fields are fingerprinted."""
    project = tmp_path / "proj"
    root = project / ".kiro" if where == "workspace" else kiro_home
    target = root / "settings" / "mcp.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"mcpServers": _SERVER}))
    name = "workspace_mcp" if where == "workspace" else "global_mcp"

    before = _fp(project=str(project))
    target.write_text(json.dumps({"mcpServers": {**_SERVER, "github": {"command": "gh"}}}))
    after = _fp(project=str(project))

    assert stale_config._read_json(target) == (
        "ok",
        {"mcpServers": {**_SERVER, "github": {"command": "gh"}}},
    )
    assert before.unreadable == after.unreadable == frozenset()
    assert stale_config.changed_inputs(before, after) == frozenset({name})


def test_a_change_at_the_end_of_a_large_spec_is_seen(kiro_home):
    """No prefix: an ``allowedTools`` revocation past the first megabytes still counts."""
    padding = "x" * (5 * 1024 * 1024)
    _write_spec(kiro_home, {"prompt": padding, "allowedTools": ["@a"]})
    before = _fp()
    assert before.unreadable == frozenset()
    _write_spec(kiro_home, {"prompt": padding, "allowedTools": []})
    assert is_stale(before, _fp(), hot_reloads=True)


def test_a_spec_over_the_size_cap_reads_as_unreadable(kiro_home, monkeypatch):
    """Past the gate's cap the file is unknown, never unchanged."""
    from kiro_crew import hooks

    _write_spec(kiro_home, {"allowedTools": ["@a"]})
    monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 16)
    marker, document = stale_config._read_json(kiro_home / "agents" / "kirocrew.json")
    assert (marker, document) == ("unreadable", None)
    assert marker in stale_config._NO_FIELDS  # so carry_forward keeps the recorded value


def test_a_markdown_agent_spec_is_parsed_by_field(kiro_home):
    spec = kiro_home / "agents" / "kirocrew.md"

    def _write(prompt: str, model: str) -> None:
        spec.write_text(
            "---\nname: kirocrew\nmodel: %s\nmcpServers:\n  a:\n    command: a\n---\n%s\n"
            % (model, prompt)
        )

    # KAS loads the markdown form (kiro-cli does not: see below).
    _write("be terse", "model-a")
    before = _fp(backend="kas")
    assert before.unreadable == frozenset()
    _write("be terse", "model-b")
    assert _fp(backend="kas") == before, "a model-only rewrite of a markdown spec is not a change"
    _write("be verbose", "model-b")
    after = _fp(backend="kas")
    assert after.spawn_only != before.spawn_only
    assert after.reconcilable == before.reconcilable


def test_kiro_cli_watches_the_json_it_loads_beside_a_workspace_markdown(tmp_path, kiro_home):
    """kiro-cli ignores markdown: a workspace .md is not its spec, the user JSON is."""
    project = tmp_path / "proj"
    _md_spec(project / ".kiro" / "agents" / "kirocrew.md", "planted")
    _write_spec(kiro_home, {"allowedTools": ["@a"]})
    before = _fp(project=str(project))
    assert not dict(before.parts)["spec_ws"]
    assert dict(before.parts)["spec_path"].endswith("kirocrew.json")

    # Editing the markdown alone changes nothing kiro-cli runs on.
    _md_spec(project / ".kiro" / "agents" / "kirocrew.md", "planted again")
    assert not is_stale(before, _fp(project=str(project)), hot_reloads=True)

    # Revoking the user JSON grant is detected.
    _write_spec(kiro_home, {"allowedTools": []})
    assert is_stale(before, _fp(project=str(project)), hot_reloads=True)


def test_kiro_cli_never_takes_a_markdown_spec_from_either_scope(tmp_path, kiro_home):
    project = tmp_path / "proj"
    _md_spec(project / ".kiro" / "agents" / "kirocrew.md", "p")
    _md_spec(kiro_home / "agents" / "kirocrew.md", "u")
    parts = dict(_fp(project=str(project)).parts)
    assert parts["spec_path"] == "" and not parts["spec_ws"]


def test_kas_reads_the_user_scope_only_in_either_form(tmp_path, kiro_home):
    """KAS projects the user-level spec, markdown included, and never the checkout's."""
    project = tmp_path / "proj"
    _write_workspace_spec(project, {"prompt": "workspace"})
    _md_spec(kiro_home / "agents" / "kirocrew.md", "user")
    before = _fp(project=str(project), backend="kas")
    parts = dict(before.parts)
    assert not parts["spec_ws"] and parts["spec_path"].endswith("kirocrew.md")

    _write_workspace_spec(project, {"prompt": "planted"})
    assert not is_stale(before, _fp(project=str(project), backend="kas"), hot_reloads=True)
    _md_spec(kiro_home / "agents" / "kirocrew.md", "user, edited")
    assert is_stale(before, _fp(project=str(project), backend="kas"), hot_reloads=True)


def test_an_array_backed_backend_loads_a_workspace_markdown_spec(tmp_path, kiro_home):
    """Crew reads the checkout's spec for it in either form, so the .md is watched."""
    project = tmp_path / "proj"
    _md_spec(project / ".kiro" / "agents" / "kirocrew.md", "p")
    _write_spec(kiro_home, {"allowedTools": ["@a"]})
    before = _fp(project=str(project), backend="claude")
    assert dict(before.parts)["spec_ws"]
    _md_spec(project / ".kiro" / "agents" / "kirocrew.md", "planted")
    assert is_stale(before, _fp(project=str(project), backend="claude"), hot_reloads=True)


def test_an_added_workspace_tools_ref_stays_reconcilable(kiro_home, tmp_path):
    project = tmp_path / "proj"
    _write_workspace_spec(project, {"mcpServers": _SERVER, "tools": []})
    before = _fp(project=str(project))
    _write_workspace_spec(project, {"mcpServers": _SERVER, "tools": ["@a"]})
    after = _fp(project=str(project))
    assert not is_stale(before, after, hot_reloads=True)
    assert is_stale(before, after, hot_reloads=False)


def test_changed_inputs_names_each_input_that_moved(kiro_home, tmp_path):
    project = tmp_path / "proj"
    (project / ".kiro" / "settings").mkdir(parents=True)
    (project / ".kiro" / "settings" / "mcp.json").write_text("{}")
    _write_spec(kiro_home, {"prompt": "a"})
    before = _fp(project=str(project))
    _write_spec(kiro_home, {"prompt": "b"})
    assert stale_config.changed_inputs(before, _fp(project=str(project))) == {"spec"}
    (project / ".kiro" / "settings" / "mcp.json").write_text('{"mcpServers": {"x": {}}}')
    assert stale_config.changed_inputs(before, _fp(project=str(project))) == {
        "spec",
        "workspace_mcp",
    }


def test_a_shadowed_user_spec_appearing_is_not_named_as_a_spec_change(kiro_home, tmp_path):
    """A workspace-spec chat stale for an MCP edit names only that input."""
    project = tmp_path / "proj"
    (project / ".kiro" / "settings").mkdir(parents=True)
    _write_workspace_spec(project, {"prompt": "a"})
    before = _fp(project=str(project))
    assert before.unreadable == frozenset()
    (kiro_home / "settings" / "mcp.json").write_text('{"mcpServers": {"x": {}}}')
    _write_spec(kiro_home, {"prompt": "shadowed"})
    after = _fp(project=str(project))
    assert dict(after.parts)["user_spec_path"] != dict(before.parts)["user_spec_path"]
    assert stale_config.changed_inputs(before, after) == {"global_mcp"}


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="POSIX permission bits, which root reads through",
)
def test_a_loaded_spec_losing_read_permission_is_unknown_not_absent(kiro_home):
    """A revoked read on the loaded spec carries it forward; the chat stays fresh."""
    _write_spec(kiro_home, {"prompt": "a", "mcpServers": _SERVER})
    before = _fp()
    spec = kiro_home / "agents" / "kirocrew.json"
    spec.chmod(0o000)
    try:
        current = _fp()
    finally:
        spec.chmod(0o600)
    assert "spec" in current.unreadable
    carried = stale_config.carry_forward(before, current)
    assert not is_stale(before, carried, hot_reloads=False)
    assert stale_config.changed_inputs(before, carried) == frozenset()


def _plant_unreadable_entry(agents: Path, outside: Path, kind: str) -> None:
    """Put a spec-named entry for ANOTHER agent that a pinned read refuses."""
    entry = agents / "other.json"
    target = outside / "other-target.json"
    target.write_text(json.dumps({"name": "other"}))
    try:
        if kind == "symlink":
            entry.symlink_to(target)
        elif kind == "hardlink":
            os.link(target, entry)
        else:
            entry.mkdir()
    except OSError:
        pytest.skip(f"this host cannot create a {kind}")


@pytest.mark.skipif(os.name == "nt", reason="creating a link needs privilege on Windows")
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory"])
@pytest.mark.parametrize("scope", ["user", "workspace"])
def test_a_refused_entry_for_another_agent_does_not_hide_spec_edits(
    kiro_home, tmp_path, kind, scope
):
    """A link or non-regular entry next to the spec is skipped, not made unknown.

    A dotfile manager symlinking some other agent's spec into the agents
    directory must not mark every fingerprint unknown, which would carry the
    old value forward and hide every later edit of the real spec.
    """
    project = tmp_path / "proj"
    agents = kiro_home / "agents" if scope == "user" else project / ".kiro" / "agents"

    def write(body: dict) -> None:
        if scope == "user":
            _write_spec(kiro_home, body)
        else:
            _write_workspace_spec(project, body)

    write({"prompt": "a"})
    _plant_unreadable_entry(agents, tmp_path, kind)
    before = _fp(project=str(project))
    assert before.unreadable == frozenset()
    write({"prompt": "b"})
    after = _fp(project=str(project))
    assert after.unreadable == frozenset()
    carried = stale_config.carry_forward(before, after)
    assert is_stale(before, carried, hot_reloads=False)
    assert stale_config.changed_inputs(before, carried) == {"spec"}
    """A stub change applies only at the next gateway start, so it is never stale."""
    import inspect

    assert "stub" not in " ".join(inspect.signature(compute_fingerprint).parameters)
    _write_spec(kiro_home, {})
    cfg = MagicMock()
    cfg.agent.member_acp_backend = ""
    cfg.agent.acp_backend = ""
    cfg.mcp_gateway.stub_servers = ["a"]
    inputs = SpawnInputs("kirocrew", "")
    before = cs.current_config_fingerprint(cfg, "dashboard:s1", inputs)
    cfg.mcp_gateway.stub_servers = ["a", "github"]
    assert cs.current_config_fingerprint(cfg, "dashboard:s1", inputs) == before


def test_the_first_record_on_a_provider_is_kept(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    provider = _Provider()
    inputs = SpawnInputs(agent="kirocrew", project="")
    cs.record_spawn_config(slot, provider, inputs, _A)
    later = _CHANGED
    cs.record_spawn_config(slot, provider, inputs, later)
    assert slot._spawn_config.fingerprint == _A, "a later turn must not erase the difference"

    successor = _Provider()
    cs.record_spawn_config(slot, successor, inputs, later)
    assert slot._spawn_config.fingerprint == later
    assert slot._spawn_config.describes(successor)
    assert not slot._spawn_config.describes(provider)


@pytest.mark.parametrize(
    ("name", "spec_ws", "expected"),
    [
        ("spec", "1", ".kiro/agents/kirocrew.json"),
        ("spec", "", "~/.kiro/agents/kirocrew.json"),
        ("global_mcp", "", "~/.kiro/settings/mcp.json"),
        ("workspace_mcp", "", ".kiro/settings/mcp.json"),
    ],
)
def test_input_labels_are_display_safe(name, spec_ws, expected):
    # The user-level agents dir as it sits by default, under the home directory.
    agents_dir = Path.home().joinpath(".kiro", "agents")
    fp = ConfigFingerprint(
        "r",
        "s",
        parts=(("spec_path", "/home/someone/x/kirocrew.json"), ("spec_ws", spec_ws)),
    )
    assert cs.display_input(name, fp, agents_dir) == expected


def test_a_user_spec_label_outside_home_shows_no_absolute_path(monkeypatch, tmp_path):
    # Home somewhere the agents dir is not under (on Windows tmp_path itself
    # sits under the home directory, so the real home cannot stand in).
    monkeypatch.setattr(cs.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    fp = ConfigFingerprint(
        "r", "s", parts=(("spec_path", str(tmp_path / "kiro/agents/kirocrew.json")),)
    )
    label = cs.display_input("spec", fp, tmp_path / "kiro" / "agents")
    assert label == "agents/kirocrew.json"
    assert str(tmp_path) not in label


#: A file name shaped like an AWS access-key id, which the outbound sanitizer
#: redacts. Split so the literal never sits whole in the source.
_TOKEN_NAME = "AKIA" + "IOSFODNN7EXAMPLE"


@pytest.mark.parametrize("spec_ws", ["1", ""])
def test_a_credential_shaped_spec_name_is_redacted_in_its_label(spec_ws):
    fp = ConfigFingerprint(
        "r",
        "s",
        parts=(("spec_path", f"/home/someone/x/{_TOKEN_NAME}.json"), ("spec_ws", spec_ws)),
    )
    label = cs.display_input("spec", fp, Path.home().joinpath(".kiro", "agents"))
    assert _TOKEN_NAME not in label
    assert "[REDACTED" in label and label.endswith(".json")


def _plant_token_named_spec(project: Path) -> Path:
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    spec = agents / f"{_TOKEN_NAME}.json"
    spec.write_text(json.dumps({"name": "kirocrew", "prompt": "planted"}))
    return spec


def test_a_planted_credential_named_spec_is_redacted_on_the_slot_and_in_the_status(
    kiro_home, tmp_path, monkeypatch
):
    """An agent-planted workspace spec's file name never leaves unredacted.

    Mutation guard: dropping the ``sanitize_outbound`` call in
    ``display_input`` puts the token-shaped name on the slot projection and in
    the config-status ``changed`` list.
    """
    project = tmp_path / "proj"
    project.mkdir()
    _write_spec(kiro_home, {"prompt": "a"})
    state, slot = _status_slot(tmp_path, monkeypatch, project=str(project))
    _plant_token_named_spec(project)

    out = asyncio.run(cs.refresh_config_stale(state, slot))

    assert out["stale"] is True
    assert out["changed"] and all(_TOKEN_NAME not in label for label in out["changed"])
    assert any("[REDACTED" in label for label in out["changed"])
    assert _TOKEN_NAME not in slot.config_stale_inputs
    assert _TOKEN_NAME not in json.dumps(slot.to_dict())


def test_an_unreadable_credential_named_spec_is_redacted_in_the_status(
    kiro_home, tmp_path, monkeypatch
):
    """The ``unreadable`` labels go through the same redaction as ``changed``."""
    project = tmp_path / "proj"
    project.mkdir()
    _write_spec(kiro_home, {"prompt": "a"})
    spec = _plant_token_named_spec(project)
    state, slot = _status_slot(tmp_path, monkeypatch, project=str(project))
    real_read = stale_config._read_pinned_spec
    monkeypatch.setattr(
        stale_config,
        "_read_pinned_spec",
        lambda pinned, path, **kw: (
            ("unreadable", None) if Path(path).name == spec.name else real_read(pinned, path, **kw)
        ),
    )

    out = asyncio.run(cs.config_stale_status(state, slot))

    assert out["unreadable"], out
    assert all(_TOKEN_NAME not in label for label in out["unreadable"])
    assert _TOKEN_NAME not in json.dumps(out)


def test_a_pool_fill_attaches_the_fingerprint_taken_before_the_process_starts(
    monkeypatch, tmp_path
):
    mgr = _pool_manager(monkeypatch, tmp_path)
    mgr._pool_cwd = "/proj"
    started: list[bool] = []
    seen: list[tuple[str, str]] = []

    def _reader(agent: str, cwd: str):
        seen.append((agent, cwd))
        started.append(False)
        return (SpawnInputs(agent, cwd), _A)

    mgr.spawn_config_reader = _reader

    asyncio.run(mgr._fill_warm_pool())

    provider, _ = mgr._warm_pool.get_nowait()
    assert seen == [("kirocrew", "/proj")]
    provider.start.assert_awaited_once()
    assert provider.pool_spawn_config == (SpawnInputs("kirocrew", "/proj"), _A)


def test_a_pool_fill_with_no_reader_or_a_failing_one_still_fills(monkeypatch, tmp_path):
    mgr = _pool_manager(monkeypatch, tmp_path)
    mgr.spawn_config_reader = MagicMock(side_effect=OSError("boom"))

    asyncio.run(mgr._fill_warm_pool())

    provider, _ = mgr._warm_pool.get_nowait()
    provider.start.assert_awaited_once()
    assert not isinstance(provider.pool_spawn_config, tuple)


def test_a_pool_fingerprint_for_other_inputs_is_not_used(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    pooled = _Provider()
    pooled.pool_spawn_config = (SpawnInputs("kirocrew", "/elsewhere"), _A)

    cs.record_spawn_config(slot, pooled, SpawnInputs("kirocrew", ""), _CHANGED)

    assert slot._spawn_config.fingerprint == _CHANGED


def test_the_pool_reader_fingerprints_the_default_backend_and_agent(monkeypatch):
    cfg = MagicMock()
    cfg.agent.member_acp_backend = "member-backend"
    cfg.agent.acp_backend = ""
    monkeypatch.setattr(cs.KiroCrewConfig, "load", MagicMock(return_value=cfg))
    seen: list[tuple] = []
    monkeypatch.setattr(
        cs,
        "compute_fingerprint",
        lambda inputs, *, backend, backend_key: seen.append((inputs, backend, backend_key)) or _A,
    )

    assert cs.pool_spawn_config("", "/proj") == (SpawnInputs("kirocrew", "/proj"), _A)
    assert seen == [(SpawnInputs("kirocrew", "/proj"), "", "agent.acp_backend")]


def test_the_gateway_wires_the_pool_reader():
    import inspect

    from kiro_crew.dashboard import server

    src = inspect.getsource(server)
    assert src.count("state.sessions.spawn_config_reader = _pool_spawn_config") == 2


def test_a_cancel_during_the_prewarm_fingerprint_leaks_no_reservation(tmp_path, monkeypatch):
    """The fingerprint await runs before admission: cancelling it reserves nothing."""
    import threading

    from test_chat_runner_coverage import _runner_state, _slot
    from test_chat_send_agent_model_default import _config, _pin_sync_accessors

    monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)
    monkeypatch.setattr(chat_runner, "_eager_spawn_sem", asyncio.Semaphore(1))
    monkeypatch.setattr(chat_runner, "_armed_prefetches", {})
    monkeypatch.setattr(chat_runner, "_prewarm_allowance", lambda: 4)
    cfg = _config(tmp_path)
    monkeypatch.setattr(cs.KiroCrewConfig, "load", MagicMock(return_value=cfg))
    state, client = _runner_state(tmp_path)
    _pin_sync_accessors(client)
    slot = _slot()
    state._slots[slot.key] = slot
    state.sessions.get_provider = MagicMock(return_value=None)
    started = threading.Event()
    release = threading.Event()

    def _slow_fingerprint(*_a):
        started.set()
        release.wait(5)
        return _A

    monkeypatch.setattr(chat_runner, "spawn_config_fingerprint", _slow_fingerprint)
    monkeypatch.setattr(chat_runner, "managed_spec_missing", lambda _a: False)

    async def _run() -> bool:
        task = asyncio.create_task(chat_runner._eager_spawn(state, slot))
        # Wait, bounded, for the fingerprint stub; a spawn that returns
        # before reaching it fails here instead of hanging the test.
        waiter = asyncio.ensure_future(asyncio.to_thread(started.wait, 5))
        try:
            done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            assert task not in done, "_eager_spawn finished before taking the fingerprint"
            assert await waiter, "the fingerprint stub was never reached"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            started.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, waiter, return_exceptions=True)
        assert chat_runner._RESERVED not in chat_runner._armed_prefetches.values()
        # The key is free for a later signal.
        return await chat_runner._admit_prefetch(state.sessions, "dashboard:later", 4)

    assert asyncio.run(_run()) is True
    state.sessions.get_or_create.assert_not_awaited()


def test_the_status_compares_against_the_adopted_baseline(tmp_path, monkeypatch, kiro_home):
    """An input unreadable at spawn and readable now is not reported stale."""
    project = tmp_path / "proj"
    _write_workspace_spec(project, {"mcpServers": _SERVER, "prompt": "p"})
    spec = project / ".kiro" / "agents" / "kirocrew.json"
    real_read = stale_config._read_pinned_spec
    monkeypatch.setattr(
        stale_config,
        "_read_pinned_spec",
        lambda pinned, path, **kw: (
            ("unreadable", None) if str(path) == str(spec) else real_read(pinned, path, **kw)
        ),
    )
    recorded = _fp(project=str(project))
    monkeypatch.setattr(stale_config, "_read_pinned_spec", real_read)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    provider = _Provider()
    state.sessions.get_provider = MagicMock(return_value=provider)
    slot._spawn_config = make_record(provider, SpawnInputs("kirocrew", str(project)), recorded)
    cfg = MagicMock()
    cfg.agent.member_acp_backend = ""
    cfg.agent.acp_backend = ""
    monkeypatch.setattr(cs.KiroCrewConfig, "load", MagicMock(return_value=cfg))

    out = asyncio.run(cs.config_stale_status(state, slot))

    assert out["stale"] is False and out["changed"] == []


def test_the_status_reports_an_unreadable_input_as_unknown(tmp_path, monkeypatch, kiro_home):
    project = tmp_path / "proj"
    settings = project / ".kiro" / "settings"
    settings.mkdir(parents=True)
    (settings / "mcp.json").write_text("{}")
    _write_spec(kiro_home, {"mcpServers": _SERVER})
    recorded = _fp(project=str(project))
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    provider = _Provider()
    state.sessions.get_provider = MagicMock(return_value=provider)
    slot._spawn_config = make_record(provider, SpawnInputs("kirocrew", str(project)), recorded)
    (settings / "mcp.json").unlink()
    (settings / "mcp.json").mkdir()
    cfg = MagicMock()
    cfg.agent.member_acp_backend = ""
    cfg.agent.acp_backend = ""
    monkeypatch.setattr(cs.KiroCrewConfig, "load", MagicMock(return_value=cfg))

    out = asyncio.run(cs.config_stale_status(state, slot))

    assert out["stale"] is None
    assert out["unreadable"] == [".kiro/settings/mcp.json"]


def test_a_prewarmed_provider_records_the_config_it_was_spawned_under(tmp_path, monkeypatch):
    """An edit between the prewarm and the first message is stale for that first turn.

    The eager spawn records the fingerprint it took before its handshake against
    the provider it registers. Were the first turn to record instead, it would
    stamp the post-edit fingerprint on the pre-edit process and never see it.
    """
    from test_chat_runner_coverage import _runner_state, _slot
    from test_chat_send_agent_model_default import _config, _pin_sync_accessors

    monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)
    # A fresh permit: the module-level cap is shared by every test in this
    # worker, and one left held elsewhere would skip the spawn under test.
    monkeypatch.setattr(chat_runner, "_eager_spawn_sem", asyncio.Semaphore(1))
    cfg = _config(tmp_path)
    monkeypatch.setattr(cs.KiroCrewConfig, "load", MagicMock(return_value=cfg))
    state, client = _runner_state(tmp_path)
    _pin_sync_accessors(client)
    slot = _slot()
    state._slots[slot.key] = slot
    state.sessions.release = MagicMock()
    state.sessions.remove_if_unclaimed = AsyncMock(return_value=True)
    provider = _Provider()
    state.sessions.get_provider = MagicMock(return_value=provider)
    monkeypatch.setattr(chat_runner, "spawn_config_fingerprint", lambda *_a: _A)
    monkeypatch.setattr(chat_runner, "managed_spec_missing", lambda _a: False)

    asyncio.run(chat_runner._eager_spawn(state, slot))

    state.sessions.get_or_create.assert_awaited_once()
    record = slot._spawn_config
    assert record is not None and record.describes(provider)
    assert record.fingerprint == _A

    # The first turn's own record_spawn_config keeps the prewarm's record, so
    # an edit made before the first message is a difference the badge shows.
    cs.record_spawn_config(slot, provider, record.inputs, _CHANGED)
    assert slot._spawn_config.fingerprint == _A


def test_a_prewarm_leaves_a_missing_managed_spec_for_its_spawn_to_heal(tmp_path, monkeypatch):
    """No pre-spawn fingerprint when the managed spec is missing.

    Healing it there would rebuild every managed spec and re-read the config
    dozens of times; the spawn heals it anyway, and the first turn, finding no
    record, fingerprints the healed spec.
    """
    from test_chat_runner_coverage import _runner_state, _slot
    from test_chat_send_agent_model_default import _config, _pin_sync_accessors

    monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)
    monkeypatch.setattr(chat_runner, "_eager_spawn_sem", asyncio.Semaphore(1))
    cfg = _config(tmp_path)
    monkeypatch.setattr(cs.KiroCrewConfig, "load", MagicMock(return_value=cfg))
    state, client = _runner_state(tmp_path)
    _pin_sync_accessors(client)
    slot = _slot()
    state._slots[slot.key] = slot
    state.sessions.release = MagicMock()
    state.sessions.remove_if_unclaimed = AsyncMock(return_value=True)
    provider = _Provider()
    state.sessions.get_provider = MagicMock(return_value=provider)
    fingerprinted: list[tuple] = []
    monkeypatch.setattr(
        chat_runner, "spawn_config_fingerprint", lambda *a: fingerprinted.append(a) or _A
    )
    monkeypatch.setattr(chat_runner, "managed_spec_missing", lambda _a: True)

    asyncio.run(chat_runner._eager_spawn(state, slot))

    state.sessions.get_or_create.assert_awaited_once()
    assert fingerprinted == []
    assert slot._spawn_config is None


def test_managed_spec_missing_names_only_an_absent_managed_default(kiro_home):
    spec = kiro_home / "agents" / "kirocrew.json"
    spec.unlink(missing_ok=True)
    assert cs.managed_spec_missing("kirocrew") is True
    assert cs.managed_spec_missing("someone-else") is False
    assert cs.managed_spec_missing(None) is False
    _write_spec(kiro_home, {"prompt": "p"})
    assert cs.managed_spec_missing("kirocrew") is False


# ── Status and the badge ─────────────────────────────────────────────────────


def _status_slot(tmp_path, monkeypatch, *, hot_reload=False, recorded=None, project=""):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    provider = _Provider(hot_reload=hot_reload)
    state.sessions.get_provider = MagicMock(return_value=provider)
    state.push_slots_update = MagicMock()
    slot._spawn_config = make_record(
        provider, SpawnInputs("kirocrew", project), recorded or _fp(project=project)
    )
    cfg = MagicMock()
    cfg.agent.member_acp_backend = ""
    cfg.agent.acp_backend = ""
    monkeypatch.setattr(cs.KiroCrewConfig, "load", MagicMock(return_value=cfg))
    return state, slot


def test_the_badge_is_set_only_when_the_config_changed(kiro_home, tmp_path, monkeypatch):
    _write_spec(kiro_home, {"prompt": "a"})
    state, slot = _status_slot(tmp_path, monkeypatch)

    assert asyncio.run(cs.refresh_config_stale(state, slot))["stale"] is False
    assert slot.config_stale is False and slot.config_stale_inputs == ""
    state.push_slots_update.assert_not_called()

    _write_spec(kiro_home, {"prompt": "b"})
    out = asyncio.run(cs.refresh_config_stale(state, slot))

    assert out["stale"] is True
    assert slot.config_stale is True
    assert slot.config_stale_inputs.endswith("kirocrew.json")
    assert str(tmp_path) not in slot.config_stale_inputs
    assert slot.to_dict()["config_stale"] is True
    assert slot.to_dict()["config_stale_inputs"] == slot.config_stale_inputs
    state.push_slots_update.assert_called_once()


def test_the_badge_clears_when_the_config_returns_to_the_spawned_state(
    kiro_home, tmp_path, monkeypatch
):
    _write_spec(kiro_home, {"prompt": "a"})
    state, slot = _status_slot(tmp_path, monkeypatch)
    _write_spec(kiro_home, {"prompt": "b"})
    asyncio.run(cs.refresh_config_stale(state, slot))
    assert slot.config_stale is True

    _write_spec(kiro_home, {"prompt": "a"})
    asyncio.run(cs.refresh_config_stale(state, slot))

    assert slot.config_stale is False and slot.config_stale_inputs == ""


def test_an_mcp_edit_a_hot_reloading_provider_applied_is_not_stale(
    kiro_home, tmp_path, monkeypatch
):
    _write_spec(kiro_home, {"mcpServers": _SERVER})
    state, slot = _status_slot(tmp_path, monkeypatch, hot_reload=True)
    _write_spec(kiro_home, {"mcpServers": {**_SERVER, "b": {"command": "b"}}})

    assert asyncio.run(cs.refresh_config_stale(state, slot))["stale"] is False
    assert slot.config_stale is False


def test_the_same_mcp_edit_is_stale_on_a_provider_that_does_not_hot_reload(
    kiro_home, tmp_path, monkeypatch
):
    _write_spec(kiro_home, {"mcpServers": _SERVER})
    state, slot = _status_slot(tmp_path, monkeypatch, hot_reload=False)
    _write_spec(kiro_home, {"mcpServers": {**_SERVER, "b": {"command": "b"}}})

    assert asyncio.run(cs.refresh_config_stale(state, slot))["stale"] is True


def test_the_status_resolves_no_path_on_the_event_loop(kiro_home, tmp_path, monkeypatch):
    """The agents dir behind the labels is resolved off the loop.

    ``kiro_agents_dir`` resolves ``KIRO_HOME`` (``Path.resolve``), which stalls
    on unreachable storage; on the loop it would stall every gateway request.
    Mutation guard: calling ``kiro_agents_dir()`` inside ``_input_label`` (on
    the loop) fails this test, and the labels still come out right.
    """
    import pathlib

    def _on_loop() -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True

    real_agents_dir = cs.kiro_agents_dir
    real_resolve = pathlib.Path.resolve

    def _agents_dir():
        assert not _on_loop(), "kiro_agents_dir() ran on the event loop"
        return real_agents_dir()

    def _resolve(self, *args, **kwargs):
        assert not _on_loop(), f"Path.resolve({self}) ran on the event loop"
        return real_resolve(self, *args, **kwargs)

    _write_spec(kiro_home, {"prompt": "a"})
    state, slot = _status_slot(tmp_path, monkeypatch)
    _write_spec(kiro_home, {"prompt": "b"})
    monkeypatch.setattr(cs, "kiro_agents_dir", _agents_dir)
    monkeypatch.setattr(pathlib.Path, "resolve", _resolve)

    out = asyncio.run(cs.refresh_config_stale(state, slot))

    assert out["stale"] is True
    assert out["changed"] and out["changed"][0].endswith("agents/kirocrew.json")
    assert slot.config_stale_inputs == out["changed"][0]
    assert str(tmp_path) not in slot.config_stale_inputs


def test_an_unknown_reading_leaves_the_badge_as_it_was(kiro_home, tmp_path, monkeypatch):
    _write_spec(kiro_home, {"prompt": "a"})
    state, slot = _status_slot(tmp_path, monkeypatch)
    slot.config_stale = True
    slot.config_stale_inputs = "~/.kiro/agents/kirocrew.json"
    monkeypatch.setattr(cs, "current_config_fingerprint", MagicMock(side_effect=OSError("x")))

    assert asyncio.run(cs.refresh_config_stale(state, slot))["stale"] is None
    assert slot.config_stale is True


def test_a_live_process_no_record_describes_is_unknown_not_current(tmp_path):
    """Nothing was compared, so the answer is unknown and the badge stays off."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    state.sessions.get_provider = MagicMock(return_value=_Provider())
    state.push_slots_update = MagicMock()
    slot._spawn_config = make_record(_Provider(), SpawnInputs("kirocrew", ""), _A)

    out = asyncio.run(cs.config_stale_status(state, slot))

    assert out == {"stale": None, "changed": [], "unreadable": []}
    asyncio.run(cs.refresh_config_stale(state, slot))
    assert slot.config_stale is False


def test_a_session_with_no_live_process_is_not_stale(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    state.sessions.get_provider = MagicMock(return_value=None)
    slot._spawn_config = make_record(_Provider(), SpawnInputs("kirocrew", ""), _A)

    assert asyncio.run(cs.config_stale_status(state, slot))["stale"] is False


def _respawn_manager(monkeypatch, tmp_path, state, respawned):
    """A session manager whose start returns *respawned*, wired to *state*'s reader."""
    mgr = _pool_manager(monkeypatch, tmp_path)
    mgr.get_or_create = AsyncMock(return_value=(respawned, True, False))
    mgr.release = MagicMock()
    mgr.respawn_config_reader = lambda key: cs.respawn_spawn_config(state, key)
    # The spawn's heal and freshness gate are covered by their own tests.
    monkeypatch.setattr(cs, "ensure_agent_materialized", lambda *_a: None)
    monkeypatch.setattr(cs, "require_fresh_derived_spec", lambda *_a: None)
    return mgr


@pytest.mark.parametrize("path", ["eager_respawn", "reset_successor"])
def test_a_respawned_process_records_its_config_so_a_later_edit_is_stale(
    kiro_home, tmp_path, monkeypatch, path
):
    """A hard stop's eager respawn (or a reset's successor) is recorded with no turn.

    Without the record an edit made before the chat's next turn read as current.
    """
    _write_spec(kiro_home, {"prompt": "a"})
    state, slot = _status_slot(tmp_path, monkeypatch)
    respawned = _Provider()
    mgr = _respawn_manager(monkeypatch, tmp_path, state, respawned)
    key = cs.effective_session_key(slot)
    boundary = mgr._lifecycle_boundary()

    if path == "eager_respawn":
        asyncio.run(boundary._eager_respawn(key))
    else:
        asyncio.run(boundary._respawn_as(key, {}))

    mgr.release.assert_called_once_with(key)
    state.sessions.get_provider = MagicMock(return_value=respawned)
    assert asyncio.run(cs.config_stale_status(state, slot))["stale"] is False
    assert slot._spawn_config.describes(respawned)

    _write_spec(kiro_home, {"prompt": "b"})
    asyncio.run(cs.refresh_all_config_stale(state, guarded=False))

    assert slot.config_stale is True
    assert slot.config_stale_inputs.endswith("kirocrew.json")


def test_a_respawn_fingerprint_for_other_inputs_is_not_adopted(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    respawned = _Provider()
    respawned.pool_spawn_config = (SpawnInputs("kirocrew", "/elsewhere"), _A)
    state.sessions.get_provider = MagicMock(return_value=respawned)
    slot._spawn_config = make_record(_Provider(), SpawnInputs("kirocrew", ""), _A)

    assert asyncio.run(cs.config_stale_status(state, slot))["stale"] is None
    assert not slot._spawn_config.describes(respawned)


def test_a_respawn_keeps_the_fingerprint_a_warm_pool_claim_carries(monkeypatch, tmp_path):
    state = _make_state(tmp_path)
    claimed = _Provider()
    claimed.pool_spawn_config = (SpawnInputs("kirocrew", "/pool"), _A)
    mgr = _respawn_manager(monkeypatch, tmp_path, state, claimed)
    mgr.respawn_config_reader = AsyncMock(return_value=(SpawnInputs("kirocrew", ""), _CHANGED))

    asyncio.run(mgr._lifecycle_boundary()._eager_respawn("k"))

    mgr.respawn_config_reader.assert_awaited_once_with("k")
    assert claimed.pool_spawn_config == (SpawnInputs("kirocrew", "/pool"), _A)


def test_a_failing_respawn_reader_still_respawns_and_releases(monkeypatch, tmp_path):
    state = _make_state(tmp_path)
    respawned = _Provider()
    mgr = _respawn_manager(monkeypatch, tmp_path, state, respawned)
    mgr.respawn_config_reader = AsyncMock(side_effect=OSError("boom"))

    asyncio.run(mgr._lifecycle_boundary()._eager_respawn("k"))

    mgr.get_or_create.assert_awaited_once_with("k")
    mgr.release.assert_called_once_with("k")
    assert respawned.pool_spawn_config is None


def test_the_respawn_reader_is_none_for_a_key_no_recorded_chat_answers_to(tmp_path):
    state = _make_state(tmp_path)
    state.get_or_create_slot("s1")

    assert asyncio.run(cs.respawn_spawn_config(state, "nobody")) is None


def test_the_gateway_wires_the_respawn_reader(tmp_path):
    from kiro_crew.dashboard import server

    state = MagicMock()
    app = MagicMock()
    taken = AsyncMock(return_value=(SpawnInputs("kirocrew", ""), _A))
    with (
        patch.object(server, "start_config_stale_sweep", MagicMock()),
        patch.object(server, "subscribe_backend_changes", MagicMock()),
        patch.object(server, "respawn_spawn_config", taken),
    ):
        server._arm_config_stale_sweep(app, state)
        out = asyncio.run(state.sessions.respawn_config_reader("k"))

    assert out == (SpawnInputs("kirocrew", ""), _A)
    taken.assert_awaited_once_with(state, "k")


def _ordered_badge_turn(monkeypatch, *, stale: bool = False):
    """One harness turn whose badge steps, acquire and stream land in *order*."""
    from turn_harness import Do, TurnScript, run_turn

    from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent, TurnUsage

    order: list[str] = []

    def _fingerprint(*_a, **_k):
        order.append("fingerprint")
        return _A

    def _record(slot, provider, *_spawn):
        order.append("record")

    async def _refresh(_state, _slot):
        order.append("refresh")
        return {}

    monkeypatch.setattr(chat_runner, "spawn_config_fingerprint", _fingerprint)
    monkeypatch.setattr(chat_runner, "record_spawn_config", _record)
    monkeypatch.setattr(chat_runner, "refresh_config_stale", _refresh)

    def _setup(ctx):
        ctx.slot.config_stale = stale
        acquire = ctx.state.sessions.get_or_create.side_effect

        async def _acquire(*args, **kwargs):
            order.append("acquire")
            return await acquire(*args, **kwargs)

        ctx.state.sessions.get_or_create.side_effect = _acquire

    script = TurnScript(
        events=[
            Do(lambda _ctx: order.append("stream")),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok"),
            AcpEvent(kind=EVENT_COMPLETE, usage=TurnUsage(credits=1.0)),
        ],
        setup=_setup,
    )
    return order, asyncio.run(run_turn(script))


def test_a_turn_records_the_spawn_config_and_refreshes_the_badge_at_its_end(monkeypatch):
    """A turn fingerprints before the acquire, records after it, and refreshes
    the badge once its stream has ended."""
    order, record = _ordered_badge_turn(monkeypatch)

    assert record.rows("assistant"), "the turn did not land"
    assert order == ["fingerprint", "acquire", "record", "stream", "refresh"]


def test_nothing_relaunches_a_stale_session(monkeypatch):
    """Detection only: a turn on a badged slot keeps its process."""
    for gone in ("_consume_stale_config_reload", "_verify_reloaded_config", "approve_agent_reload"):
        assert not hasattr(chat_runner, gone)

    _order, record = _ordered_badge_turn(monkeypatch, stale=True)

    assert record.rows("assistant"), "the turn did not land"
    relaunches = {"reset", "destroy", "remove", "recycle_background"}
    assert not [call.name for call in record.session_calls if call.name in relaunches]
    assert [call.name for call in record.session_calls].count("get_or_create") == 1


def test_the_reload_action_clears_the_badge():
    """The badge is cleared in the ONE reload teardown, which the tab menu's
    Reload session route and the ``session_reload`` verb both call -- so either
    way of relaunching the process clears it (the verb's own behavioural pin is
    in test_session_control_reload.py)."""
    import inspect

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard import session_control as sc

    src = inspect.getsource(chat_handlers.reload_slot_session)
    assert "slot.config_stale = False" in src
    assert src.index("slot.config_stale = False") < src.rindex("state.push_slots_update()")
    assert "reload_slot_session(" in inspect.getsource(chat_handlers.api_chat_slot_reload)
    assert "reload_slot_session(" in inspect.getsource(sc.reload_target)


# ── session_config_status: verb, route, tool ─────────────────────────────────


@pytest.fixture
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture
def verb_state(tmp_path, monkeypatch, kiro_home, _enabled):
    _write_spec(kiro_home, {"prompt": "a"})
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    provider = _Provider()
    state.sessions.get_provider = MagicMock(return_value=provider)
    state.push_slots_update = MagicMock()
    target._spawn_config = make_record(provider, SpawnInputs("kirocrew", ""), _fp())
    cfg = MagicMock()
    cfg.agent.member_acp_backend = ""
    cfg.agent.acp_backend = ""
    monkeypatch.setattr(cs.KiroCrewConfig, "load", MagicMock(return_value=cfg))
    return state, caller, target


def _status(state, caller, target: str) -> dict:
    return asyncio.run(
        sc.read_config_status(state, caller_session_key=slot_history_key(caller), target=target)
    )


def test_the_verb_reports_a_stale_target_and_sets_its_badge(verb_state, kiro_home):
    state, caller, target = verb_state
    assert _status(state, caller, "chat-2")["stale"] is False
    _write_spec(kiro_home, {"prompt": "b"})

    out = _status(state, caller, "chat-2")

    assert out["ok"] is True and out["target"] == "chat-2"
    assert out["stale"] is True and out["changed"][0].endswith("kirocrew.json")
    assert target.config_stale is True


def test_the_verb_reads_the_callers_own_chat(verb_state, kiro_home):
    """The documented step: an agent checks its OWN chat after editing its MCP servers."""
    state, caller, _ = verb_state
    provider = state.sessions.get_provider.return_value
    caller._spawn_config = make_record(provider, SpawnInputs("kirocrew", ""), _fp())
    _write_spec(kiro_home, {"prompt": "b"})

    out = _status(state, caller, "chat-1")

    assert out["ok"] is True and out["target"] == "chat-1"
    assert out["stale"] is True and caller.config_stale is True


def test_a_fenced_caller_reads_itself_but_not_a_peer_it_did_not_create(verb_state):
    """Allowing self widens nothing else: the ownership fence still holds for a peer."""
    state, caller, target = verb_state
    provider = state.sessions.get_provider.return_value
    caller._spawn_config = make_record(provider, SpawnInputs("kirocrew", ""), _fp())
    key = slot_history_key(caller)

    def fenced(name: str) -> dict:
        return asyncio.run(
            sc.read_config_status(state, caller_session_key=key, target=name, caller_fenced=True)
        )

    assert fenced("chat-1")["target"] == "chat-1"
    with pytest.raises(sc.SessionControlError) as refused:
        fenced("chat-2")
    assert refused.value.code == "not_creator"
    assert target.config_stale is False


def test_the_verb_reloads_nothing(verb_state, kiro_home):
    state, caller, target = verb_state
    state.sessions.reset = AsyncMock()
    _write_spec(kiro_home, {"prompt": "b"})

    _status(state, caller, "chat-2")

    state.sessions.reset.assert_not_awaited()


def test_the_verb_refuses_a_mirrored_target(verb_state):
    state, caller, target = verb_state
    state.sessions.set_mirror_link(slot_history_key(target), "C0FFEE", "1758.0001")

    with pytest.raises(sc.SessionControlError):
        _status(state, caller, "chat-2")
    assert target.config_stale is False


def test_the_verb_is_off_when_session_control_is_off(verb_state, monkeypatch):
    state, caller, _ = verb_state
    monkeypatch.setattr(sc, "session_control_enabled", lambda: False)

    with pytest.raises(sc.SessionControlError):
        _status(state, caller, "chat-2")


def test_the_route_is_registered_as_strict_internal():
    from kiro_crew.dashboard import server

    assert "/api/session-control/config-status" in server._STRICT_INTERNAL_API_PATHS


def test_the_route_refuses_what_the_verb_refuses(verb_state, monkeypatch):
    """The route maps a verb refusal onto its status, as every session-control route does."""
    state, caller, _ = verb_state
    request = MagicMock()
    request.app = {"state": state}
    request.query = {"target": "chat-2"}
    monkeypatch.setattr(handlers_sc, "_require_internal", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers_sc, "_read_session_key", lambda _r: slot_history_key(caller))
    monkeypatch.setattr(handlers_sc, "_carried_fence", lambda _r: None)
    monkeypatch.setattr(sc, "session_control_enabled", lambda: False)

    resp = asyncio.run(handlers_sc.api_session_control_config_status(request))

    assert resp.status >= 400
    assert json.loads(resp.text)["code"]


_VERIFIED = "dashboard:chat-verified"


def _tool(resp: dict, args: dict) -> tuple[str, InMemoryDashboardClient]:
    dash = InMemoryDashboardClient({"GET /api/session-control/config-status": resp})
    out = TABLE.call("session_config_status", args, ToolContext(dash, Caller.strict(_VERIFIED)))
    return out, dash


def test_the_tool_reads_the_verb_route_with_the_verified_key():
    out, dash = _tool(
        {"ok": True, "target": "chat-2", "stale": True, "changed": ["~/.kiro/agents/k.json"]},
        {"target": "chat-2"},
    )
    (get,) = dash.requests
    assert get.path == "/api/session-control/config-status?target=chat-2"
    assert get.session_key == _VERIFIED
    assert "stale config (~/.kiro/agents/k.json changed" in out
    assert "Reload" in out


def test_the_tool_is_a_strict_session_control_row():
    """Gated on a verified caller like session_summary, so the channel block covers it."""
    assert "session_config_status" in SESSION_CONTROL_TOOLS


def test_the_tool_refuses_an_unverifiable_caller():
    dash = InMemoryDashboardClient({"GET /api/session-control/config-status": {}})
    out = TABLE.call(
        "session_config_status",
        {"target": "chat-2"},
        ToolContext(dash, Caller.unverified(_VERIFIED)),
    )
    assert out.startswith("Error")
    assert dash.requests == []


def test_the_tool_says_a_current_target_is_current():
    out, _ = _tool({"ok": True, "target": "chat-2", "stale": False}, {"target": "chat-2"})
    assert "runs on its current config" in out


def test_the_tool_says_an_unreadable_input_is_unknown():
    out, _ = _tool(
        {"ok": True, "target": "chat-2", "stale": None, "unreadable": [".kiro/settings/mcp.json"]},
        {"target": "chat-2"},
    )
    assert "unknown" in out and ".kiro/settings/mcp.json" in out


def test_the_tool_says_a_check_that_names_no_file_could_not_run():
    """A failed recompute or a reload mid-check names no file, so none is blamed."""
    out, _ = _tool(
        {"ok": True, "target": "chat-2", "stale": None, "unreadable": []}, {"target": "chat-2"}
    )
    assert "unknown" in out and "try again" in out
    assert "could not be read" not in out and "config file" not in out


def test_the_tool_surfaces_the_verb_refusal():
    out, _ = _tool({"error": "target session is mirrored", "code": "mirrored"}, {"target": "x"})
    assert out.startswith("Error:") and "mirrored" in out


def test_a_channel_agent_is_blocked_from_the_tool():
    from kiro_crew.channel import CHANNEL_AGENT_BLOCKED_TOOLS

    assert "session_config_status" in CHANNEL_AGENT_BLOCKED_TOOLS


# ── Idle chats: write-triggered refresh and the stat-guarded sweep ───────────


def _idle_chats(tmp_path, monkeypatch, kiro_home, *, keys=("s1",)):
    """Live, recorded, idle chats on the current config, as a sweep sees them."""
    state = _make_state(tmp_path)
    providers = {}
    for key in keys:
        slot = state.get_or_create_slot(key)
        providers[cs.effective_session_key(slot)] = _Provider()
        slot._spawn_config = make_record(
            providers[cs.effective_session_key(slot)], SpawnInputs("kirocrew", ""), _fp()
        )
    state.sessions.get_provider = MagicMock(side_effect=lambda k: providers.get(k))
    state.push_slots_update = MagicMock()
    cfg = MagicMock()
    cfg.agent.member_acp_backend = ""
    cfg.agent.acp_backend = ""
    monkeypatch.setattr(cs.KiroCrewConfig, "load", MagicMock(return_value=cfg))
    return state


def _settle(state):
    async def _drain():
        cs.schedule_refresh_all(state)
        while state._background_tasks:
            await asyncio.gather(*list(state._background_tasks))

    asyncio.run(_drain())


def test_an_idle_chat_is_badged_right_after_a_gateway_mcp_add(kiro_home, tmp_path, monkeypatch):
    """No turn: the write itself refreshes every live chat."""
    _write_spec(kiro_home, {"mcpServers": _SERVER})
    state = _idle_chats(tmp_path, monkeypatch, kiro_home, keys=("s1", "s2"))
    _write_spec(kiro_home, {"mcpServers": {**_SERVER, "github": {"command": "gh"}}})

    _settle(state)

    assert all(s.config_stale for s in state._slots.values())


def test_the_gateway_config_writes_are_the_ones_that_refresh():
    assert cs.is_config_write("POST", "/api/mcp/toggle")
    assert cs.is_config_write("PUT", "/api/agents/detail/kirocrew")
    assert cs.is_config_write("PUT", "/api/agent/config")
    # The capability manager rewrites the spec's ``mcpServers`` and ``tools``.
    assert cs.is_config_write("POST", "/api/capability/mcp/install")
    assert cs.is_config_write("POST", "/api/capability/mcp/uninstall")
    assert cs.is_config_write("POST", "/api/capability/agents/install")
    assert not cs.is_config_write("GET", "/api/capability/mcp")
    assert not cs.is_config_write("POST", "/api/mcp-gateway/servers/stub")  # not fingerprinted
    assert not cs.is_config_write("POST", "/api/mcp-gateway/enable")
    assert not cs.is_config_write("PUT", "/api/config/kirocrew")  # ConfigWatch hears it
    assert not cs.is_config_write("GET", "/api/mcp/servers")
    assert not cs.is_config_write("POST", "/api/mcp-apps/call")
    assert not cs.is_config_write("POST", "/api/chat/slots/s1/reload")


def test_the_middleware_refreshes_after_a_successful_write_only(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    scheduled = MagicMock()
    monkeypatch.setattr(cs, "schedule_refresh_all", scheduled)
    middleware = cs.config_write_refresh_middleware(state)

    def _request(method, path):
        request = MagicMock()
        request.method, request.path = method, path
        return request

    async def _ok(_request):
        return MagicMock(status=200)

    async def _refused(_request):
        return MagicMock(status=409)

    asyncio.run(middleware(_request("POST", "/api/mcp/toggle"), _ok))
    asyncio.run(middleware(_request("POST", "/api/mcp/toggle"), _refused))
    asyncio.run(middleware(_request("GET", "/api/mcp/servers"), _ok))

    scheduled.assert_called_once_with(state)


def test_the_gateway_wires_the_write_refresh_and_the_sweep():
    """In the ordered list each chain assigns, so the assignment cannot drop it."""
    import inspect
    import re

    from kiro_crew.dashboard import server

    for entry in (server.start_dashboard, server.start_api_server):
        src = inspect.getsource(entry)
        assert "config_write_refresh=_config_write_refresh(state)" in src, entry.__name__
        assert "_arm_config_stale_sweep(app, state)" in src
    for install in (server._install_dashboard_middlewares, server._install_api_middlewares):
        src = inspect.getsource(install)
        chain = re.search(r"app\.middlewares\[:\] = \[(.*?)\n\s*\]\n", src, re.S)
        assert chain is not None, install.__name__
        assert "\n        config_write_refresh," in chain.group(1), install.__name__
    body = inspect.getsource(server._arm_config_stale_sweep)
    assert "start_config_stale_sweep(state)" in body


def _live_watch_subs():
    from kiro_crew.config import live

    return list(live.watch().subscriptions())


@pytest.mark.asyncio
async def test_stop_config_stale_detection_cancels_both(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    monkeypatch.setattr(cs, "CONFIG_STALE_SWEEP_SECS", 3600.0)
    state._config_stale_sweep = cs.start_config_stale_sweep(state)
    sub = MagicMock()
    state._config_stale_backend_sub = sub

    await cs.stop_config_stale_detection(state)

    sub.cancel.assert_called_once_with()
    assert state._config_stale_sweep is None
    assert not [t for t in asyncio.all_tasks() if "config_stale_sweep_loop" in repr(t)]


@pytest.mark.asyncio
async def test_the_built_api_server_keeps_the_write_refresh_and_it_fires(tmp_path, monkeypatch):
    """Built the way production builds it: the middleware survives, and a write refreshes."""
    import kiro_crew.config.loader as _loader
    import kiro_crew.dashboard.server as _srv
    import kiro_crew.dashboard.state as _st
    import kiro_crew.kiro_prerequisite as _prerequisite

    monkeypatch.setattr(_st, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(_srv, "data_home", lambda: tmp_path)
    monkeypatch.setattr(_loader, "config_dir", lambda: tmp_path)
    service = MagicMock()
    service.close = AsyncMock()
    service.seed_sessions_baseline = AsyncMock(return_value=True)
    monkeypatch.setattr(_prerequisite, "KiroPrerequisiteService", MagicMock(return_value=service))
    scheduled = MagicMock()
    monkeypatch.setattr(cs, "schedule_refresh_all", scheduled)

    runner, state = await _srv.start_api_server(
        sessions=MagicMock(count=0),
        crons=MagicMock(
            list_jobs=MagicMock(return_value=[]),
            list_jobs_async=AsyncMock(return_value=[]),
            status=MagicMock(return_value={}),
        ),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        port=0,
        assume_kiro_ready=True,
    )
    try:
        installed = [
            mw for mw in runner.app.middlewares if getattr(mw, "_is_config_write_refresh", False)
        ]
        assert len(installed) == 1
        request = MagicMock()
        request.method, request.path = "POST", "/api/mcp/toggle"

        async def _ok(_request):
            return MagicMock(status=200)

        await installed[0](request, _ok)
        scheduled.assert_called_once_with(state)
        assert state._config_stale_sweep is not None
        sweep = state._config_stale_sweep
        sub = state._config_stale_backend_sub
        assert not sweep.done() and sub is not None
    finally:
        await runner.cleanup()
    # Production cleanup ends both: no sweep task and no subscription outlive the app.
    assert sweep.done() and sweep.cancelled()
    assert state._config_stale_sweep is None and state._config_stale_backend_sub is None
    assert sub not in _live_watch_subs()


def test_an_external_edit_badges_an_idle_chat_on_one_sweep_tick(kiro_home, tmp_path, monkeypatch):
    _write_spec(kiro_home, {"prompt": "a"})
    state = _idle_chats(tmp_path, monkeypatch, kiro_home)
    slot = state._slots["s1"]
    assert asyncio.run(cs.refresh_all_config_stale(state, guarded=True)) == 1
    assert slot.config_stale is False

    _write_spec(kiro_home, {"prompt": "edited by hand"})
    asyncio.run(cs.refresh_all_config_stale(state, guarded=True))

    assert slot.config_stale is True

    # Reverting the edit clears it on the next tick.
    _write_spec(kiro_home, {"prompt": "a"})
    asyncio.run(cs.refresh_all_config_stale(state, guarded=True))
    assert slot.config_stale is False


def test_a_new_spec_file_appearing_counts_as_a_change(kiro_home, tmp_path):
    _write_spec(kiro_home, {"prompt": "a"})
    agents = kiro_home / "agents"
    st = agents.stat()
    before = stale_config.input_signature(SpawnInputs("kirocrew", ""))
    (agents / "other.json").write_text("{}")
    # The listing itself carries it, whatever the directory's own stat does.
    os.utime(agents, ns=(st.st_atime_ns, st.st_mtime_ns))
    after = stale_config.input_signature(SpawnInputs("kirocrew", ""))
    assert after != before
    assert "other.json" in repr(after) and "other.json" not in repr(before)


def test_an_unchanged_stat_skips_the_fingerprint(kiro_home, tmp_path, monkeypatch):
    """The cost guard: an idle sweep over unchanged files fingerprints nothing."""
    _write_spec(kiro_home, {"prompt": "a"})
    state = _idle_chats(tmp_path, monkeypatch, kiro_home)
    asyncio.run(cs.refresh_all_config_stale(state, guarded=True))
    spy = MagicMock(side_effect=cs.current_config_fingerprint)
    monkeypatch.setattr(cs, "current_config_fingerprint", spy)

    assert asyncio.run(cs.refresh_all_config_stale(state, guarded=True)) == 0
    spy.assert_not_called()


def test_an_unchanged_stat_skips_a_chat_whose_reading_is_unknown(kiro_home, tmp_path, monkeypatch):
    """An input that cannot be read is no reason to re-fingerprint on every tick."""
    _write_spec(kiro_home, {"prompt": "a"})
    state = _idle_chats(tmp_path, monkeypatch, kiro_home)
    (kiro_home / "settings" / "mcp.json").mkdir()  # exists, cannot be read
    assert asyncio.run(cs.refresh_all_config_stale(state, guarded=True)) == 1
    spy = MagicMock(side_effect=cs.current_config_fingerprint)
    monkeypatch.setattr(cs, "current_config_fingerprint", spy)

    assert asyncio.run(cs.refresh_all_config_stale(state, guarded=True)) == 0
    spy.assert_not_called()

    # A stat change re-checks it, as for a known reading.
    (kiro_home / "settings" / "mcp.json").rmdir()
    assert asyncio.run(cs.refresh_all_config_stale(state, guarded=True)) == 1
    spy.assert_called_once()


def test_a_failed_recompute_is_retried_on_the_next_tick(kiro_home, tmp_path, monkeypatch):
    """An unknown that names no unreadable input records no guard."""
    _write_spec(kiro_home, {"prompt": "a"})
    state = _idle_chats(tmp_path, monkeypatch, kiro_home)
    monkeypatch.setattr(cs, "current_config_fingerprint", MagicMock(side_effect=OSError("x")))
    asyncio.run(cs.refresh_all_config_stale(state, guarded=True))

    assert next(iter(state._slots.values()))._config_stat_sig is None


class _CtimeShifted:
    """An ``lstat`` result with ``st_ctime_ns`` replaced, every other field as read."""

    def __init__(self, st: os.stat_result, ctime_ns: int) -> None:
        self._st = st
        self.st_ctime_ns = ctime_ns

    def __getattr__(self, name: str) -> object:
        return getattr(self._st, name)


def test_a_permission_change_alone_changes_the_signature(kiro_home, monkeypatch):
    """A ctime move with mtime and size unchanged (a chmod or chown) is a change.

    The ctime values are injected through ``PinnedDirectory.lstat`` rather than
    waiting on the filesystem's coarse ctime clock to tick.
    """
    from kiro_crew import platform_compat

    _write_spec(kiro_home, {"prompt": "a"})
    real_lstat = platform_compat.PinnedDirectory.lstat
    ctime = {"ns": 1_000}

    def _lstat(self, name):
        st = real_lstat(self, name)
        return _CtimeShifted(st, ctime["ns"]) if name == "kirocrew.json" else st

    monkeypatch.setattr(platform_compat.PinnedDirectory, "lstat", _lstat)
    before = stale_config.input_signature(SpawnInputs("kirocrew", ""))
    assert stale_config.input_signature(SpawnInputs("kirocrew", "")) == before
    ctime["ns"] = 2_000

    assert stale_config.input_signature(SpawnInputs("kirocrew", "")) != before


def test_the_spawn_fingerprint_is_taken_after_the_spawn_heals_the_managed_spec(
    kiro_home, tmp_path, monkeypatch
):
    """A deleted managed spec the spawn re-materializes is recorded as present."""
    healed: list[str | None] = []

    def _heal(agent):
        healed.append(agent)
        _write_spec(kiro_home, {"prompt": "regenerated"})
        return True

    monkeypatch.setattr(cs, "ensure_agent_materialized", _heal)
    cfg = MagicMock()
    cfg.agent.member_acp_backend = ""
    cfg.agent.acp_backend = ""
    monkeypatch.setattr(cs.KiroCrewConfig, "load", MagicMock(return_value=cfg))
    inputs = SpawnInputs("kirocrew", "")

    recorded = cs.spawn_config_fingerprint(cfg, "s1", inputs)

    assert healed == ["kirocrew"]
    now = cs.current_config_fingerprint(cfg, "s1", inputs)
    assert not is_stale(recorded, now, hot_reloads=False)
    # The warm pool takes its fingerprint the same way.
    (kiro_home / "agents" / "kirocrew.json").unlink()
    _inputs, pooled = cs.pool_spawn_config("kirocrew", "")
    assert not is_stale(pooled, cs.current_config_fingerprint(cfg, "", inputs), hot_reloads=False)


def test_the_spawn_fingerprint_passes_the_spawn_freshness_gate_after_the_heal(
    kiro_home, tmp_path, monkeypatch
):
    """The spawn's two pre-start steps run here in the spawn's order, with the project.

    A worker mirror the spawn's freshness gate re-derives is hashed as re-derived, and
    a gate refusal -- which refuses the spawn too -- yields no fingerprint at all."""
    calls: list[tuple] = []
    monkeypatch.setattr(cs, "ensure_agent_materialized", lambda a: calls.append(("heal", a)))

    def _gate(agent, work_dir):
        calls.append(("gate", agent, work_dir))
        _write_spec(kiro_home, {"prompt": "re-derived"})

    monkeypatch.setattr(cs, "require_fresh_derived_spec", _gate)
    cfg = MagicMock()
    cfg.agent.member_acp_backend = ""
    cfg.agent.acp_backend = ""
    inputs = SpawnInputs("kirocrew", str(tmp_path))

    recorded = cs.spawn_config_fingerprint(cfg, "s1", inputs)

    assert calls == [("heal", "kirocrew"), ("gate", "kirocrew", str(tmp_path))]
    now = cs.current_config_fingerprint(cfg, "s1", inputs)
    assert not is_stale(recorded, now, hot_reloads=False)

    def _refuse(agent, work_dir):
        raise DerivedSpecStale("refused")

    monkeypatch.setattr(cs, "require_fresh_derived_spec", _refuse)
    with pytest.raises(DerivedSpecStale):
        cs.spawn_config_fingerprint(cfg, "s1", inputs)


def _write_global_mcp(kiro_home: Path, servers: dict) -> None:
    (kiro_home / "settings" / "mcp.json").write_text(json.dumps({"mcpServers": servers}))


def test_a_kirocrew_server_declaration_edit_is_stale_on_a_hot_reloading_provider(
    kiro_home, tmp_path, monkeypatch
):
    """Crew re-reads it at session start, so no backend's hot reload applies it."""
    _write_spec(kiro_home, {"mcpServers": _SERVER})
    _write_global_mcp(kiro_home, {"kirocrew-core": {"command": "kirocrew-core"}})
    state, slot = _status_slot(tmp_path, monkeypatch, hot_reload=True)
    _write_global_mcp(
        kiro_home, {"kirocrew-core": {"command": "kirocrew-core", "disabledTools": ["cron_add"]}}
    )

    out = asyncio.run(cs.refresh_config_stale(state, slot))

    assert out["stale"] is True
    assert out["changed"] == ["~/.kiro/settings/mcp.json"]


def test_another_server_edit_stays_reconcilable_on_a_hot_reloading_provider(
    kiro_home, tmp_path, monkeypatch
):
    _write_spec(kiro_home, {"mcpServers": _SERVER})
    _write_global_mcp(kiro_home, {"kirocrew-core": {"command": "kirocrew-core"}})
    state, slot = _status_slot(tmp_path, monkeypatch, hot_reload=True)
    _write_global_mcp(
        kiro_home,
        {"kirocrew-core": {"command": "kirocrew-core"}, "gh": {"disabledTools": ["x"]}},
    )

    assert asyncio.run(cs.refresh_config_stale(state, slot))["stale"] is False


def test_creating_an_mcp_json_with_no_kirocrew_server_is_no_declaration_change():
    assert stale_config._identity_part("absent", None) == stale_config._identity_part(
        "ok", {"mcpServers": {"gh": {"command": "gh"}}}
    )


def test_the_sweep_skips_a_chat_mid_turn_and_one_with_no_process(kiro_home, tmp_path, monkeypatch):
    _write_spec(kiro_home, {"prompt": "a"})
    state = _idle_chats(tmp_path, monkeypatch, kiro_home, keys=("busy", "unspawned"))
    busy = MagicMock()
    busy.done.return_value = False
    state._slots["busy"].task = busy  # a turn in flight
    state._slots["unspawned"]._spawn_config = None
    _write_spec(kiro_home, {"prompt": "b"})

    assert asyncio.run(cs.refresh_all_config_stale(state, guarded=True)) == 0
    assert not any(s.config_stale for s in state._slots.values())


def test_the_sweep_loop_ticks_the_guarded_refresh(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    ticks: list[bool] = []

    async def _refresh(_state, *, guarded):
        ticks.append(guarded)
        if len(ticks) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(cs, "refresh_all_config_stale", _refresh)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cs.config_stale_sweep_loop(state, interval=0))
    assert ticks == [True, True]
    assert cs.CONFIG_STALE_SWEEP_SECS == 60.0


def test_a_write_burst_coalesces_into_at_most_two_passes(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    passes: list[int] = []

    async def _refresh(_state, *, guarded):
        passes.append(1)
        if len(passes) == 1:
            cs.schedule_refresh_all(state)  # a write landing while the pass runs
            cs.schedule_refresh_all(state)
        return 0

    monkeypatch.setattr(cs, "refresh_all_config_stale", _refresh)

    async def _run():
        cs.schedule_refresh_all(state)
        cs.schedule_refresh_all(state)
        while state._background_tasks:
            await asyncio.gather(*list(state._background_tasks))

    asyncio.run(_run())
    assert len(passes) == 2


# ── Baseline adoption and the carried-forward selection ──────────────────────


def test_an_input_unreadable_at_spawn_is_adopted_once_then_compared(
    kiro_home, tmp_path, monkeypatch
):
    """Readable later: adopted (not stale) and persisted, so a later edit is stale."""
    _write_spec(kiro_home, {"prompt": "a"})
    spec = kiro_home / "agents" / "kirocrew.json"
    real_read = stale_config._read_pinned_spec
    monkeypatch.setattr(
        stale_config,
        "_read_pinned_spec",
        lambda pinned, path, **kw: (
            ("unreadable", None) if path == spec else real_read(pinned, path, **kw)
        ),
    )
    recorded = _fp()
    assert "spec" in recorded.unreadable
    monkeypatch.setattr(stale_config, "_read_pinned_spec", real_read)
    state, slot = _status_slot(tmp_path, monkeypatch, recorded=recorded)

    assert asyncio.run(cs.config_stale_status(state, slot))["stale"] is False
    assert "spec" not in slot._spawn_config.fingerprint.unreadable

    _write_spec(kiro_home, {"prompt": "edited"})
    assert asyncio.run(cs.config_stale_status(state, slot))["stale"] is True


def test_an_unlistable_agents_dir_keeps_the_recorded_spec_selection(kiro_home, monkeypatch):
    """Unknown, not a switch to "no spec": the selection is carried forward too."""
    _write_spec(kiro_home, {"prompt": "a"})
    recorded = _fp()
    monkeypatch.setattr(stale_config, "_spec_scope_unlistable", lambda *_a: True)
    monkeypatch.setattr(
        stale_config, "_agent_spec_files", lambda *_a: stale_config._SpecSelection()
    )
    current = _fp()
    assert "spec" in current.unreadable
    assert dict(current.parts)["spec_path"] == ""

    compared = stale_config.carry_forward(recorded, current)

    assert dict(compared.parts)["spec_path"] == dict(recorded.parts)["spec_path"]
    assert compared.spawn_only == recorded.spawn_only
    assert compared.reconcilable == recorded.reconcilable
    assert not is_stale(recorded, compared, hot_reloads=False)


def test_pool_spawn_config_is_declared_on_the_provider_contract():
    """H14: a capability the session layer reads is declared, never probed."""
    from kiro_crew.providers.base import LLMProvider

    assert isinstance(LLMProvider.__dict__["pool_spawn_config"], property)
    # Default: no receipt; a pool fill's set value is what is read back.
    import types

    holder = types.SimpleNamespace()
    prop = LLMProvider.__dict__["pool_spawn_config"]
    assert prop.fget(holder) is None
    prop.fset(holder, (SpawnInputs("kirocrew", ""), _A))
    assert prop.fget(holder) == (SpawnInputs("kirocrew", ""), _A)
    src = Path(cs.__file__).read_text(encoding="utf-8")
    assert 'getattr(provider, "pool_spawn_config"' not in src


def test_a_badged_chat_whose_session_expired_is_cleared(kiro_home, tmp_path, monkeypatch):
    _write_spec(kiro_home, {"prompt": "a"})
    state = _idle_chats(tmp_path, monkeypatch, kiro_home, keys=("gone",))
    slot = state._slots["gone"]
    slot.config_stale, slot.config_stale_inputs = True, "~/.kiro/agents/kirocrew.json"
    state.sessions.get_provider = MagicMock(return_value=None)  # idle-reaped

    asyncio.run(cs.refresh_all_config_stale(state, guarded=True))

    assert slot.config_stale is False and slot.config_stale_inputs == ""


def test_the_record_holds_fixed_size_digests_not_the_field_text(kiro_home):
    """A 5 MB spec costs a slot record a few 64-character digests, not 5 MB."""
    huge_ref = "@" + "s" * (1024 * 1024)
    _write_spec(
        kiro_home,
        {"prompt": "x" * (5 * 1024 * 1024), "mcpServers": _SERVER, "tools": [huge_ref, "@a"]},
    )
    fp = _fp()
    parts = dict(fp.parts)
    for key in ("spec_mcp", "spec_fields", "global_mcp"):
        assert len(parts[key]) == 64 and int(parts[key], 16) >= 0, key
    assert sum(len(v) for v in parts.values()) < 4096
    # The agent-controlled ``@server`` refs are held as digests too.
    assert len(fp.tool_refs) == 2
    assert all(len(r) == 64 and int(r, 16) >= 0 for r in fp.tool_refs)
    assert huge_ref not in fp.tool_refs


def _refs_spec(kiro_home, refs: list[str]) -> ConfigFingerprint:
    _write_spec(kiro_home, {"mcpServers": _SERVER, "tools": refs})
    return _fp()


def test_tool_refs_past_the_cap_fold_into_one_overflow_entry(kiro_home, monkeypatch):
    """Any number of refs costs a bounded set, and an edit past the cap still reads stale."""
    monkeypatch.setattr(stale_config, "FINGERPRINT_MAX_TOOL_REFS", 4)
    refs = [f"@srv{i}" for i in range(20)]
    before = _refs_spec(kiro_home, refs)
    assert len(before.tool_refs) == 5
    assert sum(1 for r in before.tool_refs if r.startswith("overflow:16:")) == 1
    # The order of the entries does not move it.
    assert _refs_spec(kiro_home, list(reversed(refs))) == before

    # Every single ref, kept or folded, is in the set: removing any one moves it
    # and reads stale on every provider, hot-reloading or not.
    for i in range(len(refs)):
        after = _refs_spec(kiro_home, refs[:i] + refs[i + 1 :])
        assert after.tool_refs != before.tool_refs, i
        assert is_stale(before, after, hot_reloads=True), i
    # So does renaming one, and adding one is never unchanged either.
    renamed = _refs_spec(kiro_home, refs[:-1] + ["@other"])
    assert renamed.tool_refs != before.tool_refs
    grown = _refs_spec(kiro_home, [*refs, "@srv20"])
    assert grown.tool_refs != before.tool_refs
    assert is_stale(before, grown, hot_reloads=False)


# ── Badge bookkeeping never breaks a turn ────────────────────────────────────

from test_fallback_row_attribution_8560 import (  # noqa: E402
    TestFallbackServedTurnAttribution as _TurnHarness,
)
from test_fallback_row_attribution_8560 import _deep_chain_client  # noqa: E402


class TestBadgeBookkeepingNeverBreaksATurn(_TurnHarness):
    """A raising fingerprint, inputs, record or refresh step: the turn still completes."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "target",
        [
            "spawn_config_fingerprint",
            "turn_spawn_inputs",
            "record_spawn_config",
            "refresh_config_stale",
        ],
    )
    async def test_a_raising_step_still_lets_the_turn_complete(self, tmp_path, monkeypatch, target):
        from kiro_crew.acp.types import TurnUsage
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.handlers import usage as usage_mod
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        shard_dir = tmp_path / "usage" / "tokens"
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)

        def _boom(*_a, **_k):
            raise ValueError("badge bookkeeping exploded")

        monkeypatch.setattr(chat_runner, target, _boom)

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(credits=1.0))

        state = self._make_state(tmp_path, monkeypatch)
        client, _handle = _deep_chain_client(_stream)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(state, slot, "hello")
            await self._drain_bg(state)

        assert len(self._rows(shard_dir)) == 1
        assert not [m for m in slot.messages if m.get("role") == "error"]

    @pytest.mark.asyncio
    async def test_a_turn_on_the_recorded_provider_takes_no_spawn_fingerprint(
        self, tmp_path, monkeypatch
    ):
        """The fingerprint is taken for a provider with no record, and only then.

        A later turn on the provider already recorded would compute it on the
        turn's critical path and drop it; a provider replaced while the turn
        acquires one is fingerprinted once it is acquired.
        """
        from kiro_crew.acp.types import TurnUsage
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.handlers import usage as usage_mod
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", tmp_path / "usage" / "tokens")
        spy = MagicMock(return_value=_A)
        monkeypatch.setattr(chat_runner, "spawn_config_fingerprint", spy)
        monkeypatch.setattr(chat_runner, "refresh_config_stale", AsyncMock(return_value={}))

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(credits=1.0))

        state = self._make_state(tmp_path, monkeypatch)
        client, _handle = _deep_chain_client(_stream)
        replacement, _handle2 = _deep_chain_client(_stream)
        # The ``LLMProvider`` default: no warm-pool receipt.
        client.pool_spawn_config = replacement.pool_spawn_config = None
        self._wire_sessions(state, client)
        live = {"provider": client}
        state.sessions.get_provider = MagicMock(side_effect=lambda _key: live["provider"])
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        async def _turn() -> None:
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await _run_chat(state, slot, "hello")
                await self._drain_bg(state)

        await _turn()
        assert spy.call_count == 1, spy.call_count
        assert slot._spawn_config.describes(client)

        await _turn()
        assert spy.call_count == 1

        async def _replace(*_a, **_k):
            live["provider"] = replacement
            return replacement, True, False

        state.sessions.get_or_create = AsyncMock(side_effect=_replace)
        await _turn()
        assert spy.call_count == 2 and slot._spawn_config.describes(replacement)


# ── Bounded signature and a reload during the check ──────────────────────────


def test_the_agents_dir_signature_is_capped_with_an_overflow_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(stale_config, "SIGNATURE_MAX_SPEC_ENTRIES", 3)
    agents = tmp_path / "agents"
    agents.mkdir()
    for i in range(5):
        (agents / f"a{i}.json").write_text("{}")

    signature = stale_config._stat_spec_dir(agents)

    # The directory's stat, three kept entries, one overflow marker.
    assert len(signature) == 1 + 3 + 1
    marker = signature[-1]
    assert marker[0] == "overflow" and marker[1] == 2 and len(marker[2]) == 64
    assert "a4.json" not in repr(signature[:-1])


def test_a_change_past_the_cap_still_changes_the_signature(tmp_path, monkeypatch):
    monkeypatch.setattr(stale_config, "SIGNATURE_MAX_SPEC_ENTRIES", 3)
    agents = tmp_path / "agents"
    agents.mkdir()
    for i in range(5):
        (agents / f"a{i}.json").write_text("{}")
    st = agents.stat()
    before = stale_config._stat_spec_dir(agents)

    # An edit to a file past the cap, no new name.
    (agents / "a4.json").write_text('{"prompt": "edited past the cap"}')
    os.utime(agents, ns=(st.st_atime_ns, st.st_mtime_ns))
    edited = stale_config._stat_spec_dir(agents)
    assert edited[1:-1] == before[1:-1], "the kept entries did not change"
    assert edited[-1] != before[-1]

    # A new file past the cap.
    (agents / "a5.json").write_text("{}")
    assert stale_config._stat_spec_dir(agents)[-1] != edited[-1]


def test_the_default_cap_is_a_shared_named_constant():
    assert stale_config.SIGNATURE_MAX_SPEC_ENTRIES == 256


@pytest.mark.parametrize("what", ["provider", "record", "session"])
def test_a_reload_during_the_check_drops_the_stale_answer(kiro_home, tmp_path, monkeypatch, what):
    """The old process's answer must not re-badge the fresh session."""
    _write_spec(kiro_home, {"prompt": "a"})
    state, slot = _status_slot(tmp_path, monkeypatch)
    _write_spec(kiro_home, {"prompt": "b"})  # stale against the live process
    real = cs.current_config_fingerprint
    live = state.sessions.get_provider.return_value

    def _reload_mid_read(*args):
        # A Reload (or a rebind) lands while the fingerprint is read off the loop.
        if what == "provider":
            state.sessions.get_provider = MagicMock(return_value=_Provider())
        elif what == "record":
            slot._spawn_config = make_record(live, slot._spawn_config.inputs, _A)
        else:
            monkeypatch.setattr(cs, "effective_session_key", lambda _s: "dashboard:rebound")
        slot.config_stale, slot.config_stale_inputs = False, ""
        return real(*args)

    monkeypatch.setattr(cs, "current_config_fingerprint", _reload_mid_read)

    out = asyncio.run(cs.refresh_config_stale(state, slot))

    assert out["stale"] is None
    assert slot.config_stale is False and slot.config_stale_inputs == ""


# ── ConfigWatch subscription and the write-route pin ─────────────────────────


def test_a_configwatch_backend_change_refreshes_every_chat(tmp_path, monkeypatch):
    from kiro_crew.config import live

    state = _make_state(tmp_path)
    scheduled = MagicMock()
    monkeypatch.setattr(cs, "schedule_refresh_all", scheduled)
    watch = live.ConfigWatch()
    monkeypatch.setattr(live, "watch", lambda: watch)

    sub = cs.subscribe_backend_changes(state)
    try:
        assert set(sub.prefixes) == {"agent.acp_backend", "agent.member_acp_backend"}
        cb = sub.callback()
        cb(live.ConfigChange(old=None, new=MagicMock(), changed=frozenset({"agent.model"})))
        scheduled.assert_not_called()
        cb(live.ConfigChange(old=None, new=MagicMock(), changed=frozenset({"agent.acp_backend"})))
        scheduled.assert_called_once_with(state)
    finally:
        sub.cancel()


def test_the_gateway_subscribes_the_badge_to_configwatch():
    import inspect

    from kiro_crew.dashboard import server

    assert "subscribe_backend_changes(state)" in inspect.getsource(server._arm_config_stale_sweep)


def test_the_sweep_does_not_stat_config_json(kiro_home, monkeypatch):
    """config.json has one watcher, ConfigWatch; the sweep leaves it alone."""
    from kiro_crew.config.loader import config_path

    statted: list[str] = []
    real = stale_config._stat_in
    monkeypatch.setattr(
        stale_config,
        "_stat_in",
        lambda d, n: statted.append(os.path.join(d.path, n)) or real(d, n),
    )
    stale_config.input_signature(SpawnInputs("kirocrew", ""))
    assert str(config_path()) not in statted
    assert "config" not in dict(
        (k, v) for k, v in stale_config.input_signature(SpawnInputs("kirocrew", ""))
    )


#: Non-GET routes under the agent and MCP paths that write nothing a chat's
#: fingerprint reads, with why. Every other one must match a refresh prefix.
_NOT_CONFIG_WRITES = {
    "/api/mcp-apps/": "an MCP app's tool call or message, not MCP server config",
    "/api/agent-panel/": "replaces a crew's agent-panel webview, not an agent spec",
    "/api/agent-ask/": "opens, waits on or withdraws an agent's question card, not config",
    "/api/mcp-gateway/": (
        "writes mcp_gateway.* in config.json or an in-process resolve cache; the"
        " stub set and its overlay specs are deliberately not fingerprinted"
    ),
}


def test_every_agent_and_mcp_write_route_is_a_refresh_prefix():
    """Pinned to the routes the gateway registers, so a new writer cannot be missed."""
    from aiohttp import web

    from kiro_crew.dashboard.routes import register_all
    from kiro_crew.dashboard.server import _register_mcp_routes

    app = web.Application()
    _register_mcp_routes(app)
    register_all(app)
    writers = {
        (route.method, str(route.resource.canonical))
        for route in app.router.routes()
        if route.method not in {"GET", "HEAD", "OPTIONS"}
        and route.resource is not None
        and str(route.resource.canonical).startswith(("/api/agent", "/api/capability", "/api/mcp"))
    }
    assert writers, "no agent or MCP write routes found"
    for method, path in sorted(writers):
        if path.startswith(tuple(_NOT_CONFIG_WRITES)):
            assert not cs.is_config_write(method, path), path
            continue
        assert cs.is_config_write(method, path), f"{method} {path} refreshes nothing"


# ── Loader parity: the fingerprint watches the file each backend loads ──────

_PARITY_AGENT = "parity"


def _json_spec(path: Path, prompt: str, *, name: str = _PARITY_AGENT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": name, "prompt": prompt}))


def _md_named(path: Path, prompt: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {_PARITY_AGENT}\n---\n{prompt}\n")


def _layout(kind: str, kiro_home: Path, project: Path) -> None:
    user, ws = kiro_home / "agents", project / ".kiro" / "agents"
    if kind == "workspace-json+user-json":
        _json_spec(ws / f"{_PARITY_AGENT}.json", "ws-json")
        _json_spec(user / f"{_PARITY_AGENT}.json", "user-json")
    elif kind == "workspace-md+user-json":
        _md_named(ws / f"{_PARITY_AGENT}.md", "ws-md")
        _json_spec(user / f"{_PARITY_AGENT}.json", "user-json")
    elif kind == "user-md-only":
        _md_named(user / f"{_PARITY_AGENT}.md", "user-md")
    elif kind == "declared-vs-filename":
        # The filename claims the agent, another file DECLARES it: declared wins.
        _json_spec(user / f"{_PARITY_AGENT}.json", "filename", name="someone-else")
        _json_spec(user / "zz-declares.json", "declared")
    else:  # pragma: no cover
        raise AssertionError(kind)


def _watched_prompt(project: Path, backend: str) -> str | None:
    """The prompt of the file the fingerprint watches, or None for no spec."""
    from kiro_crew.agent_spec_format import parse_agent_spec_bytes

    fp = compute_fingerprint(SpawnInputs(_PARITY_AGENT, str(project)), backend=backend)
    path = dict(fp.parts)["spec_path"]
    if not path:
        return None
    return parse_agent_spec_bytes(Path(path).read_bytes(), Path(path)).get("prompt", "").strip()


_LAYOUTS = [
    "workspace-json+user-json",
    "workspace-md+user-json",
    "user-md-only",
    "declared-vs-filename",
]


@pytest.mark.parametrize("layout", _LAYOUTS)
def test_kas_parity_with_load_agent_spec(kiro_home, tmp_path, layout):
    from kiro_crew.acp.kas_agents import load_agent_spec

    project = tmp_path / "proj"
    _layout(layout, kiro_home, project)

    loaded = load_agent_spec(kiro_home / "agents", _PARITY_AGENT)

    assert _watched_prompt(project, "kas") == str(loaded.get("prompt", "")).strip()


@pytest.mark.parametrize("layout", _LAYOUTS)
@pytest.mark.parametrize("backend", ["claude", "codex", "opencode", "goose", "deepseek"])
def test_array_backend_parity_with_session_mcp(kiro_home, tmp_path, layout, backend, monkeypatch):
    """Every array backend runs on the spec ``_agent_spec_and_snapshot_for`` reads."""
    from kiro_crew.acp import session_mcp
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_SESSION_MCP_ARRAY

    # The parametrization is the array set, so a backend joining it is covered here.
    assert backend in ACP_BACKENDS_SESSION_MCP_ARRAY
    project = tmp_path / "proj"
    _layout(layout, kiro_home, project)
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: None)
    monkeypatch.setattr(session_mcp, "require_fresh_derived_spec", lambda *_a: None)

    spec, _snapshot = session_mcp._agent_spec_and_snapshot_for(_PARITY_AGENT, str(project))

    expected = None if spec is None else str(spec.get("prompt", "")).strip()
    assert _watched_prompt(project, backend) == expected


def test_the_array_parity_set_is_the_array_backends():
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_SESSION_MCP_ARRAY

    assert ACP_BACKENDS_SESSION_MCP_ARRAY == {"claude", "codex", "opencode", "goose", "deepseek"}


@pytest.mark.parametrize("layout", _LAYOUTS)
def test_kiro_cli_parity_with_its_json_discovery(kiro_home, tmp_path, layout):
    """kiro-cli loads JSON only: checkout first, then the user directory."""
    from kiro_crew.agent import _resolve_agent_spec, markdown_spec_for_agent
    from kiro_crew.agent_discovery import project_agent_files, project_agent_name
    from kiro_crew.agent_spec_format import is_markdown_spec, parse_agent_spec_bytes

    project = tmp_path / "proj"
    _layout(layout, kiro_home, project)

    project_json = [
        p
        for p in project_agent_files(str(project))
        if not is_markdown_spec(p) and project_agent_name(p) == _PARITY_AGENT
    ]
    loaded = (
        project_json[0]
        if project_json
        else _resolve_agent_spec(_PARITY_AGENT, None, json_only=True)
    )
    if markdown_spec_for_agent(_PARITY_AGENT, str(project)) is not None:
        # A markdown-only agent: kiro-cli has no spec for it at all.
        assert loaded is None
    expected = (
        None
        if loaded is None
        else parse_agent_spec_bytes(loaded.read_bytes(), loaded).get("prompt", "").strip()
    )
    assert _watched_prompt(project, "") == expected


# ── includeMcpJson parity: the mount rule each backend applies ─────────────

#: Spec bodies covering every shape of the flag: absent, each bool, an explicit
#: null and a non-bool a hand-edited spec can carry.
_MCP_JSON_FLAGS = [
    {},
    {"includeMcpJson": True},
    {"includeMcpJson": False},
    {"includeMcpJson": None},
    {"includeMcpJson": "yes"},
]


@pytest.mark.parametrize("body", _MCP_JSON_FLAGS)
def test_kiro_cli_mcp_json_rule_matches_agent_capabilities(body):
    """kiro-cli mounts ``mcp.json`` unless the spec says ``false``.

    The rule's real owner is the kiro-cli binary, which is not in this repo. The
    nearest in-repo statement of it is the reading ``agent_capabilities`` applies to
    the specs it edits, so the copy here is pinned to that expression: a change to
    it there turns this red.
    """
    import inspect

    from kiro_crew import agent_capabilities
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO

    owner_reading = 'spec.get("includeMcpJson", True) is not False'
    assert owner_reading in inspect.getsource(agent_capabilities)
    spec = {"name": _PARITY_AGENT, **body}
    expected = spec.get("includeMcpJson", True) is not False
    assert stale_config._mcp_json_mounted("ok", spec, ACP_BACKEND_KIRO) is expected


@pytest.mark.parametrize("body", _MCP_JSON_FLAGS)
def test_kas_mcp_json_rule_matches_the_kas_projection(body):
    """KAS mounts ``mcp.json`` only on a stated ``true``.

    The projection ``to_client_custom_agent`` forwards a stated bool and drops
    anything else; KAS's own consumer (outside this repo) reads an absent flag
    as false, so what reaches KAS as ``true`` is the only mounting case.
    """
    from kiro_crew.acp.kas_agents import to_client_custom_agent
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

    spec = {"name": _PARITY_AGENT, "tools": [], **body}
    projected = to_client_custom_agent(_PARITY_AGENT, spec, "prompt")
    expected = projected.get("includeMcpJson", False) is True
    assert stale_config._mcp_json_mounted("ok", spec, ACP_BACKEND_KAS) is expected


@pytest.mark.parametrize("body", _MCP_JSON_FLAGS)
@pytest.mark.parametrize("backend", ["claude", "codex", "opencode", "goose", "deepseek"])
def test_array_backend_mcp_json_rule_matches_session_mcp_servers(kiro_home, body, backend):
    """An array backend is handed the spec's servers and Crew's own, never ``mcp.json``'s."""
    from kiro_crew.acp import session_mcp

    (kiro_home / "settings" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"from-mcp-json": {"command": "x"}}})
    )
    spec = {
        "name": _PARITY_AGENT,
        "mcpServers": {"own": {"command": "x"}},
        "tools": ["*"],
        **body,
    }
    names = {entry.get("name") for entry in session_mcp.session_mcp_servers(None, spec=spec)}
    assert "own" in names
    expected = "from-mcp-json" in names
    assert stale_config._mcp_json_mounted("ok", spec, backend) is expected


def test_every_backend_has_an_mcp_json_rule_pinned():
    """A backend joining the registry must take a side in the rules above.

    pi is the one known backend with no in-repo owner: it is outside the array set,
    so Crew hands it no servers and pi reads its spec itself. The badge reads it as
    not mounting ``mcp.json``, which is what this pins until pi gains an owner here.
    """
    from kiro_crew.agent_sdk.backends import (
        ACP_BACKEND_KAS,
        ACP_BACKEND_KIRO,
        ACP_BACKEND_PI,
        ACP_BACKENDS_KNOWN,
        ACP_BACKENDS_SESSION_MCP_ARRAY,
    )

    pinned = {ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_PI} | ACP_BACKENDS_SESSION_MCP_ARRAY
    assert ACP_BACKENDS_KNOWN == pinned
    spec = {"name": _PARITY_AGENT, "includeMcpJson": True}
    assert stale_config._mcp_json_mounted("ok", spec, ACP_BACKEND_PI) is False


def test_the_capped_signature_is_stable_and_order_independent(tmp_path, monkeypatch):
    """Same directory, any scan order: same signature; any overflow change moves it."""
    monkeypatch.setattr(stale_config, "SIGNATURE_MAX_SPEC_ENTRIES", 3)
    agents = tmp_path / "agents"
    agents.mkdir()
    for i in range(8):
        (agents / f"a{i}.json").write_text("{}")
    first = stale_config._stat_spec_dir(agents)
    assert stale_config._stat_spec_dir(agents) == first
    # The kept entries are the smallest names, whatever order scandir yields.
    assert [e[0] for e in first[1:-1]] == ["a0.json", "a1.json", "a2.json"]

    real_scandir = os.scandir

    class _Reversed:
        def __init__(self, path):
            self._it = real_scandir(path)
            self._entries = list(self._it)[::-1]

        def __enter__(self):
            return iter(self._entries)

        def __exit__(self, *exc):
            self._it.close()

    monkeypatch.setattr(os, "scandir", _Reversed)
    assert stale_config._stat_spec_dir(agents) == first
    monkeypatch.setattr(os, "scandir", real_scandir)

    st = agents.stat()
    (agents / "a6.json").unlink()  # a removal past the cap
    os.utime(agents, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert stale_config._stat_spec_dir(agents)[-1] != first[-1]


def test_the_listing_is_streamed_not_collected(tmp_path, monkeypatch):
    """No list of every name is built: the scan is consumed lazily."""
    import inspect

    src = inspect.getsource(stale_config._stat_spec_dir)
    assert "sorted(e.name for e in entries" not in src
    assert "heapq" in src


# ── The path gate before every directory scan ────────────────────────────────


def _refuse(monkeypatch, refused: Path) -> list[str]:
    """Make the path gate refuse *refused* (a link resolving to a UNC share,
    say) and record every path the scanner would hand ``os.scandir``."""
    _gate_refuses(monkeypatch, lambda raw: Path(raw) == refused)
    scanned: list[str] = []
    real_scandir = os.scandir

    def _spy(path):
        scanned.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", _spy)
    return scanned


def test_a_refused_agents_dir_is_never_listed_by_the_signature(kiro_home, monkeypatch, either_gate):
    agents = kiro_home / "agents"
    _write_spec(kiro_home, {"prompt": "a"})
    scanned = _refuse(monkeypatch, agents)

    assert stale_config._stat_spec_dir(agents) == ("refused",)
    assert str(agents) not in scanned


def test_a_refused_agents_dir_is_never_scanned_by_the_unlistable_check(
    kiro_home, monkeypatch, either_gate
):
    agents = kiro_home / "agents"
    scanned = _refuse(monkeypatch, agents)

    assert stale_config._spec_scope_unlistable("", "") is True
    assert str(agents) not in scanned


def test_a_refused_agents_dir_is_never_resolved_and_reads_unknown(
    kiro_home, monkeypatch, either_gate
):
    """The spec resolver never lists it, and the input is unknown, not changed."""
    _write_spec(kiro_home, {"prompt": "a"})
    recorded = _fp()
    agents = kiro_home / "agents"
    _refuse(monkeypatch, agents)
    from kiro_crew import agent

    listed: list[str] = []
    real_iter = agent.iter_agent_spec_files
    monkeypatch.setattr(
        agent, "iter_agent_spec_files", lambda d, **k: listed.append(str(d)) or real_iter(d, **k)
    )

    current = _fp()

    assert str(agents) not in listed
    assert "spec" in current.unreadable
    compared = stale_config.carry_forward(recorded, current)
    assert not is_stale(recorded, compared, hot_reloads=False)


def test_a_pinned_resolve_refuses_an_oversized_agents_dir(tmp_path, monkeypatch):
    """The listing is bounded: past the cap the resolve raises, never lists it whole."""
    from kiro_crew import agent, platform_compat

    agents = tmp_path / "agents"
    for i in range(4):
        _write_scope_spec(agents, f"s{i}.json", {"name": f"s{i}"})
    monkeypatch.setattr(agent, "PINNED_SPEC_DIR_MAX_ENTRIES", 3)

    with platform_compat.pinned_directory(agents) as pinned:
        with pytest.raises(agent.SpecDirectoryOverflowError):
            agent.pinned_agent_spec_path("s0", agents_dir=agents, pinned=pinned, json_only=False)
        monkeypatch.setattr(agent, "PINNED_SPEC_DIR_MAX_ENTRIES", 4)
        found = agent.pinned_agent_spec_path(
            "s0", agents_dir=agents, pinned=pinned, json_only=False
        )

    assert found == agents / "s0.json"


def test_a_pinned_resolve_refuses_specs_past_the_read_budget(tmp_path, monkeypatch):
    """The reads are bounded in aggregate: past the budget the resolve raises."""
    from kiro_crew import agent, platform_compat

    agents = tmp_path / "agents"
    for i in range(4):
        _write_scope_spec(agents, f"s{i}.json", {"name": f"s{i}", "prompt": "x" * 200})
    total = sum(p.stat().st_size for p in agents.iterdir())

    with platform_compat.pinned_directory(agents) as pinned:
        monkeypatch.setattr(agent, "PINNED_SPEC_DIR_MAX_TOTAL_BYTES", total - 1)
        with pytest.raises(agent.SpecDirectoryOverflowError):
            agent.pinned_agent_spec_path("s0", agents_dir=agents, pinned=pinned, json_only=False)
        monkeypatch.setattr(agent, "PINNED_SPEC_DIR_MAX_TOTAL_BYTES", total)
        found = agent.pinned_agent_spec_path(
            "s0", agents_dir=agents, pinned=pinned, json_only=False
        )

    assert found == agents / "s0.json"


def test_an_agents_dir_past_the_read_budget_reads_unknown_not_changed(kiro_home, monkeypatch):
    """Specs summing past the read budget are an unknown spec, carried forward."""
    _write_spec(kiro_home, {"prompt": "a"})
    recorded = _fp()
    from kiro_crew import agent

    for i in range(3):
        (kiro_home / "agents" / f"pad{i}.json").write_text(json.dumps({"prompt": "x" * 400}))
    monkeypatch.setattr(agent, "PINNED_SPEC_DIR_MAX_TOTAL_BYTES", 512)

    current = _fp()

    assert "spec" in current.unreadable
    compared = stale_config.carry_forward(recorded, current)
    assert not is_stale(recorded, compared, hot_reloads=False)


def test_an_oversized_agents_dir_reads_unknown_not_changed(kiro_home, monkeypatch):
    """An agents directory past the listing cap is an unknown spec, carried forward."""
    _write_spec(kiro_home, {"prompt": "a"})
    recorded = _fp()
    from kiro_crew import agent

    for i in range(3):
        (kiro_home / "agents" / f"pad{i}.txt").write_text("x")
    monkeypatch.setattr(agent, "PINNED_SPEC_DIR_MAX_ENTRIES", 2)

    current = _fp()

    assert "spec" in current.unreadable
    compared = stale_config.carry_forward(recorded, current)
    assert not is_stale(recorded, compared, hot_reloads=False)


def test_a_refused_mcp_json_is_not_stated(kiro_home, monkeypatch, either_gate):
    target = kiro_home / "settings" / "mcp.json"
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", target)
    _gate_refuses(monkeypatch, lambda raw: Path(raw) == target)
    stated: list[str] = []
    real_stat = stale_config._stat_in
    monkeypatch.setattr(
        stale_config,
        "_stat_in",
        lambda d, n: stated.append(os.path.join(d.path, n)) or real_stat(d, n),
    )

    sig = dict(stale_config.input_signature(SpawnInputs("kirocrew", "")))

    assert sig["global-mcp"] == ("refused",)
    assert str(target) not in stated


def test_a_refused_workspace_agents_dir_is_never_resolved(
    kiro_home, tmp_path, monkeypatch, either_gate
):
    project = tmp_path / "proj"
    _write_workspace_spec(project, {"prompt": "ws"})
    ws_agents = project / ".kiro" / "agents"
    _refuse(monkeypatch, ws_agents)
    from kiro_crew import agent

    listed: list[str] = []
    real_iter = agent.iter_agent_spec_files
    monkeypatch.setattr(
        agent, "iter_agent_spec_files", lambda d, **k: listed.append(str(d)) or real_iter(d, **k)
    )

    fp = _fp(project=str(project), backend="claude")

    assert str(ws_agents) not in listed
    assert "spec" in fp.unreadable


# ── Listing and stat'ing through a pin, never the validated name again ──────


def _link_after_the_gate(monkeypatch, link: Path, target: Path) -> list[object]:
    """Put a link at *link* pointing at *target* (a junction to a UNC share, on
    Windows) and admit it at the gate, as a swap landing between the screen and
    the scan would. Records every argument ``os.scandir`` is handed."""
    target.mkdir(parents=True, exist_ok=True)
    (target / "kirocrew.json").write_text('{"name": "kirocrew"}')
    if link.is_dir() and not link.is_symlink():
        for child in link.iterdir():
            child.unlink()
        link.rmdir()
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(stale_config, "_gated_dir", lambda directory: directory)
    scanned: list[object] = []
    real_scandir = os.scandir

    def _spy(path):
        scanned.append(path)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", _spy)
    return scanned


@pytest.mark.skipif(os.name == "nt", reason="creating a symlink needs privilege on Windows")
def test_a_user_agents_dir_swapped_for_a_link_is_never_followed(kiro_home, tmp_path, monkeypatch):
    agents = kiro_home / "agents"
    scanned = _link_after_the_gate(monkeypatch, agents, tmp_path / "share")

    assert stale_config._stat_spec_dir(agents) == ("unreadable",)
    assert stale_config._spec_scope_unlistable("", "") is True
    assert scanned == []


@pytest.mark.skipif(os.name == "nt", reason="creating a symlink needs privilege on Windows")
def test_a_project_agents_dir_swapped_for_a_link_is_never_followed(
    kiro_home, tmp_path, monkeypatch
):
    project = tmp_path / "proj"
    scanned = _link_after_the_gate(monkeypatch, project / ".kiro" / "agents", tmp_path / "share")

    sig = dict(stale_config.input_signature(SpawnInputs("kirocrew", str(project))))

    assert sig["project-agents"] == ("unreadable",)
    assert stale_config._spec_scope_unlistable(str(project), "claude") is True
    # Only the user agents directory, a real one, was scanned -- through its pin.
    assert all(isinstance(path, int) for path in scanned)


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX only: a pinned directory refuses a rename on Windows"
)
def test_the_listing_and_every_entry_stat_come_from_the_pinned_directory(tmp_path, monkeypatch):
    """A rename of the name after the pin is not followed by the scan or the stats."""
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "a.json").write_text("{}")
    moved = tmp_path / "agents.moved"
    real_pin = stale_config._pin

    def _pin_then_swap(admitted):
        pinned = real_pin(admitted)
        admitted.rename(moved)
        admitted.mkdir()
        (admitted / "a.json").write_text('{"name": "planted", "prompt": "longer"}')
        (admitted / "b.json").write_text("{}")
        return pinned

    monkeypatch.setattr(stale_config, "_pin", _pin_then_swap)

    signature = stale_config._stat_spec_dir(agents)

    st = (moved / "a.json").lstat()
    assert signature[1:] == (("a.json", (st.st_mtime_ns, st.st_size, st.st_ctime_ns)),)


# ── Resolving the spec through the pin, never the screened name again ────────


def _scope_dir(kiro_home: Path, project: Path, scope: str) -> Path:
    return kiro_home / "agents" if scope == "user" else project / ".kiro" / "agents"


def _write_scope_spec(directory: Path, filename: str, document: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(json.dumps(document))


@pytest.mark.skipif(os.name == "nt", reason="creating a symlink needs privilege on Windows")
@pytest.mark.parametrize("scope", ["user", "workspace"])
def test_a_spec_dir_swapped_for_a_link_after_the_screen_is_never_resolved(
    kiro_home, tmp_path, monkeypatch, scope
):
    """The gate admits the real directory; a link lands before the resolve.

    On Windows that link is a junction to a UNC share, and a scan by name is an
    outbound SMB authentication: the resolver must not list or read it.
    """
    project = tmp_path / "proj"
    directory = _scope_dir(kiro_home, project, scope)
    _write_scope_spec(directory, "kirocrew.json", {"name": "kirocrew", "prompt": "a"})
    share = tmp_path / "share"
    _write_scope_spec(share, "kirocrew.json", {"name": "kirocrew", "prompt": "share"})
    real_gate = stale_config._gated_dir

    def _screen_then_swap(candidate: Path):
        admitted = real_gate(candidate)
        if candidate == directory and directory.is_dir() and not directory.is_symlink():
            directory.rename(tmp_path / "moved")
            directory.symlink_to(share, target_is_directory=True)
        return admitted

    monkeypatch.setattr(stale_config, "_gated_dir", _screen_then_swap)
    from kiro_crew import agent

    listed: list[str] = []
    real_iter = agent.iter_agent_spec_files
    monkeypatch.setattr(
        agent, "iter_agent_spec_files", lambda d, **k: listed.append(str(d)) or real_iter(d, **k)
    )

    selection = stale_config._agent_spec_files("kirocrew", str(project), "claude")

    assert selection.unknown is True
    assert getattr(selection, scope) is None
    assert listed == []


@pytest.mark.skipif(os.name == "nt", reason="creating a symlink needs privilege on Windows")
def test_an_ancestor_swapped_for_a_link_after_the_screen_is_never_followed(
    kiro_home, tmp_path, monkeypatch
):
    """The gate admits ``proj/.kiro/agents``; ``proj/.kiro`` turns into a link before the pin.

    A pin of the absolute path refuses a link at ``agents`` only and walks
    ``.kiro`` by name -- on Windows a junction there aimed at a UNC share is an
    outbound SMB authentication. The component-wise pin refuses it instead.
    """
    project = tmp_path / "proj"
    directory = project / ".kiro" / "agents"
    _write_scope_spec(directory, "kirocrew.json", {"name": "kirocrew", "prompt": "a"})
    share = tmp_path / "share" / ".kiro"
    _write_scope_spec(share / "agents", "kirocrew.json", {"name": "kirocrew", "prompt": "share"})
    real_gate = stale_config._gated_dir

    def _screen_then_swap_ancestor(candidate: Path):
        admitted = real_gate(candidate)
        ancestor = project / ".kiro"
        if candidate == directory and not ancestor.is_symlink():
            ancestor.rename(tmp_path / "moved")
            ancestor.symlink_to(share, target_is_directory=True)
        return admitted

    monkeypatch.setattr(stale_config, "_gated_dir", _screen_then_swap_ancestor)

    selection = stale_config._agent_spec_files("kirocrew", str(project), "claude")

    assert selection.unknown is True
    assert selection.workspace is None


def test_the_pin_opens_one_component_at_a_time_from_the_anchor(tmp_path, monkeypatch):
    """Only the anchor is opened by an absolute path; every other open is one name."""
    from kiro_crew import platform_compat

    directory = (tmp_path / "a" / "b" / "agents").resolve()
    directory.mkdir(parents=True)
    (directory / "x.json").write_text("{}")
    absolute: list[str] = []
    children: list[str] = []
    real_pinned = platform_compat.pinned_directory
    real_child = platform_compat.PinnedDirectory.child
    monkeypatch.setattr(
        platform_compat,
        "pinned_directory",
        lambda path: absolute.append(os.fspath(path)) or real_pinned(path),
    )
    monkeypatch.setattr(
        platform_compat.PinnedDirectory,
        "child",
        lambda self, name: children.append(name) or real_child(self, name),
    )

    with stale_config._pin(directory) as pinned:
        assert pinned.names() == ["x.json"]
        assert pinned.path == str(directory)

    assert absolute == [directory.anchor]
    assert children == list(directory.relative_to(directory.anchor).parts)


def test_a_plain_file_at_the_pinned_name_is_told_apart_from_one_above_it(tmp_path):
    base = tmp_path.resolve()
    (base / "agents").write_text("not a directory")
    (base / "file").write_text("not a directory either")

    with pytest.raises(stale_config._PlainFileAtName):
        stale_config._pin(base / "agents")
    with pytest.raises(OSError) as above:
        stale_config._pin(base / "file" / "agents")
    assert not isinstance(above.value, stale_config._PlainFileAtName)


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX only: a pinned directory refuses a rename on Windows"
)
@pytest.mark.parametrize("scope", ["user", "workspace"])
def test_the_spec_is_resolved_and_read_from_the_pinned_directory(
    kiro_home, tmp_path, monkeypatch, scope
):
    """A rename of the scope's name after the pin is not followed by the resolve or the read."""
    project = tmp_path / "proj"
    directory = _scope_dir(kiro_home, project, scope)
    _write_scope_spec(directory, "kirocrew.json", {"name": "kirocrew", "prompt": "a"})
    expected = _fp(project=str(project), backend="claude")
    real_pin = stale_config._pin
    swapped: list[Path] = []

    def _pin_then_swap(admitted: Path):
        pinned = real_pin(admitted)
        if admitted == directory and not swapped:
            swapped.append(admitted)
            admitted.rename(tmp_path / "moved")
            _write_scope_spec(admitted, "other.json", {"name": "kirocrew", "prompt": "planted"})
        return pinned

    monkeypatch.setattr(stale_config, "_pin", _pin_then_swap)

    current = _fp(project=str(project), backend="claude")

    assert swapped == [directory]
    parts = dict(current.parts)
    assert parts["spec_path"] == str(directory / "kirocrew.json")
    assert parts["spec_fields"] == dict(expected.parts)["spec_fields"]
    assert current.unreadable == frozenset()


@pytest.mark.parametrize("json_only", [False, True])
def test_a_pinned_resolve_selects_what_the_named_resolve_selects(tmp_path, json_only):
    """Same selection rules through the handle: twins, forms, aliases, declared names."""
    from kiro_crew import agent, platform_compat

    agents = tmp_path / "agents"
    _write_scope_spec(agents, "a.json", {"name": "alpha"})
    (agents / "a.md").write_text("---\nname: shadowed\n---\nbody\n")
    (agents / "b.md").write_text("---\nname: beta\n---\nbody\n")
    _write_scope_spec(agents, "kirocrew-skill-view-x.json", {"name": "alias"})
    _write_scope_spec(agents, "gamma.json", {"name": "other"})
    (agents / "broken.json").write_text("{")
    names = ["alpha", "shadowed", "beta", "alias", "gamma", "broken", "missing"]
    by_name = {n: agent._resolve_agent_spec(n, agents, json_only=json_only) for n in names}
    with platform_compat.pinned_directory(agents) as pinned:
        by_pin = {
            n: agent.pinned_agent_spec_path(
                n, agents_dir=agents, pinned=pinned, json_only=json_only
            )
            for n in names
        }

    assert by_pin == by_name
    assert by_pin["alpha"] == agents / "a.json"
    assert by_pin["beta"] == (None if json_only else agents / "b.md")


def test_a_pinned_resolve_keeps_the_sensitive_path_fence(tmp_path, monkeypatch):
    """A spec the fence refuses is skipped through the handle, as it is by name."""
    from kiro_crew import agent, platform_compat

    agents = tmp_path / "agents"
    _write_scope_spec(agents, "kirocrew.json", {"name": "kirocrew"})
    fenced = str(agents / "kirocrew.json")
    monkeypatch.setattr(agent, "is_sensitive_canonical_path", lambda path: path == fenced)

    with platform_compat.pinned_directory(agents) as pinned:
        found = agent.pinned_agent_spec_path(
            "kirocrew", agents_dir=agents, pinned=pinned, json_only=False
        )

    assert found is None


def test_a_pure_backend_switch_names_the_backend(kiro_home, tmp_path, monkeypatch):
    _write_spec(kiro_home, {"prompt": "a"})
    state, slot = _status_slot(tmp_path, monkeypatch)
    cs.KiroCrewConfig.load.return_value.agent.acp_backend = "claude"

    out = asyncio.run(cs.config_stale_status(state, slot))

    assert out["stale"] is True
    assert out["changed"] == ["agent.acp_backend"]


def test_a_member_dm_backend_switch_names_the_member_key(kiro_home, tmp_path, monkeypatch):
    """A member DM resolves from ``agent.member_acp_backend``: that key is the one named."""
    from kiro_crew import acp_backends

    monkeypatch.setattr(acp_backends, "resolve_selected_backend", lambda v: v or "kiro")
    _write_spec(kiro_home, {"prompt": "a"})
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("member-alice", mode="member")
    provider = _Provider()
    state.sessions.get_provider = MagicMock(return_value=provider)
    state.push_slots_update = MagicMock()
    cfg = MagicMock()
    cfg.agent.member_acp_backend = ""
    cfg.agent.acp_backend = ""
    monkeypatch.setattr(cs.KiroCrewConfig, "load", MagicMock(return_value=cfg))
    key = cs.effective_session_key(slot)
    inputs = SpawnInputs("kirocrew", "")
    slot._spawn_config = make_record(
        provider, inputs, cs.current_config_fingerprint(cfg, key, inputs)
    )

    # The default backend moving does not reach a member DM at all.
    cfg.agent.acp_backend = "claude"
    assert asyncio.run(cs.config_stale_status(state, slot))["stale"] is False

    cfg.agent.member_acp_backend = "claude"
    out = asyncio.run(cs.config_stale_status(state, slot))

    assert out["stale"] is True
    assert out["changed"] == ["agent.member_acp_backend"]


def test_a_backend_label_without_a_recorded_key_falls_back_to_the_default_key():
    fp = ConfigFingerprint("r", "s", parts=(("backend", "kiro"),))
    assert cs.display_input("backend", fp, Path("agents")) == "agent.acp_backend"


def test_an_unattributed_change_names_no_particular_file(kiro_home, tmp_path, monkeypatch):
    """A recorded fingerprint with no per-input parts still reads stale, unattributed."""
    _write_spec(kiro_home, {"prompt": "a"})
    state, slot = _status_slot(
        tmp_path, monkeypatch, recorded=ConfigFingerprint(reconcilable="r", spawn_only="s")
    )

    out = asyncio.run(cs.config_stale_status(state, slot))

    assert out["stale"] is True
    assert out["changed"] == ["session configuration"]


# -- Windows: no by-name resolution of a candidate directory before it is pinned --


def _resolve_spy(monkeypatch) -> list[str]:
    """Record every ``os.path.realpath`` call, and refuse the shared gate outright.

    On Windows the shared gate canonicalizes by name with ``realpath`` after its
    link screen, so a ``.kiro`` swapped in between is followed (an outbound SMB
    authentication when it aims at a share). The Windows sweep must not use it.
    """
    calls: list[str] = []
    real = os.path.realpath

    def _spy(path, *args, **kwargs):
        calls.append(os.fspath(path))
        return real(path, *args, **kwargs)

    def _no_shared_gate(raw):
        raise AssertionError(f"the shared gate resolved {raw!r} by name before the pin")

    monkeypatch.setattr(os.path, "realpath", _spy)
    monkeypatch.setattr(stale_config, "validate_file_path", _no_shared_gate)
    monkeypatch.setattr(stale_config, "_WINDOWS", True)
    return calls


@pytest.mark.skipif(os.name == "nt", reason="creating a symlink needs privilege on Windows")
def test_windows_gate_never_resolves_an_ancestor_swapped_after_the_screen(
    kiro_home, tmp_path, monkeypatch
):
    """``proj/.kiro`` becomes a link to a "share" after the screen, before the pin.

    The Windows gate is lexical, the pin refuses the link, nothing under the
    project is resolved by name, and the scope reads as unknown, not followed.
    """
    project = tmp_path / "proj"
    directory = project / ".kiro" / "agents"
    _write_scope_spec(directory, "kirocrew.json", {"name": "kirocrew", "prompt": "a"})
    share = tmp_path / "share" / ".kiro"
    _write_scope_spec(share / "agents", "kirocrew.json", {"name": "kirocrew", "prompt": "share"})
    calls = _resolve_spy(monkeypatch)
    real_gate = stale_config._gated_dir_windows
    swapped: list[int] = []

    def _screen_then_swap(raw: str):
        admitted = real_gate(raw)
        ancestor = project / ".kiro"
        if raw == str(directory) and not ancestor.is_symlink():
            ancestor.rename(tmp_path / "moved")
            ancestor.symlink_to(share, target_is_directory=True)
            swapped.append(len(calls))
        return admitted

    monkeypatch.setattr(stale_config, "_gated_dir_windows", _screen_then_swap)

    assert stale_config._stat_spec_dir(directory) == ("unreadable",)
    assert stale_config._spec_scope_unlistable(str(project), "claude") is True
    assert swapped
    assert not [c for c in calls if c.startswith(str(project))]


def test_windows_gate_refuses_an_untrusted_share_lexically(monkeypatch):
    calls = _resolve_spy(monkeypatch)
    pinned: list[object] = []
    monkeypatch.setattr(
        stale_config.platform_compat, "pinned_directory", lambda p: pinned.append(p)
    )

    assert stale_config._gated_dir(Path("//attacker/share/.kiro/agents")) is None
    assert stale_config._stat_spec_dir(Path("//attacker/share/.kiro/agents")) == ("refused",)
    # Refused by spelling: neither the candidate nor any trusted root is resolved.
    assert calls == []
    assert pinned == []


def test_windows_pin_fences_the_handles_final_path(tmp_path, monkeypatch):
    """A lexically harmless spelling whose handle is really a credential dir is refused."""
    _resolve_spy(monkeypatch)
    directory = tmp_path / "plain"
    directory.mkdir()
    monkeypatch.setattr(stale_config, "fd_real_path", lambda _fd: "/creds")
    monkeypatch.setattr(stale_config, "is_sensitive_resolved_path", lambda p: p == "/creds")

    admitted = stale_config._gated_dir(directory)
    assert admitted == directory
    with pytest.raises(PermissionError):
        stale_config._pin(admitted)
    assert stale_config._stat_spec_dir(directory) == ("unreadable",)


def _no_resolve_before_the_pin(monkeypatch) -> list[str]:
    """Make every by-name resolver raise until the first component is pinned.

    ``Path.resolve``, ``os.path.realpath`` and the credential fence's own
    resolvers (which ``is_sensitive_resolved_path`` and the shared UNC gate use
    for their anchors) all raise while nothing is pinned. Returns the anchors
    pinned, so a test can also see the pin happened.
    """
    from kiro_crew.security import paths as security_paths

    pinned: list[str] = []
    real_pin = stale_config.platform_compat.pinned_directory

    def _pin_spy(path):
        pinned.append(os.fspath(path))
        return real_pin(path)

    def _guard(name, real):
        def _resolver(*args, **kwargs):
            if not pinned:
                raise AssertionError(f"{name} ran before the pin: {args!r}")
            return real(*args, **kwargs)

        return _resolver

    monkeypatch.setattr(stale_config.platform_compat, "pinned_directory", _pin_spy)
    monkeypatch.setattr(os.path, "realpath", _guard("realpath", os.path.realpath))
    monkeypatch.setattr(Path, "resolve", _guard("Path.resolve", Path.resolve))
    for name in ("_realpath_or_none", "_realpaths_or_none"):
        monkeypatch.setattr(security_paths, name, _guard(name, getattr(security_paths, name)))
    monkeypatch.setattr(
        stale_config,
        "validate_file_path",
        _guard("validate_file_path", stale_config.validate_file_path),
    )
    monkeypatch.setattr(stale_config, "_WINDOWS", True)
    return pinned


def test_windows_sweep_spells_kiro_home_without_resolving_it(tmp_path, monkeypatch):
    """``KIRO_HOME`` set: the user agents dir is spelled lexically and pinned first.

    The shared accessor ``Path.resolve()``s ``KIRO_HOME``, which on Windows
    follows a swapped ancestor link to a share before anything is pinned.
    """
    home = tmp_path / "kh"
    _write_scope_spec(home / "agents", "kirocrew.json", {"name": "kirocrew", "prompt": "a"})
    monkeypatch.setenv("KIRO_HOME", str(home))
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", None)
    monkeypatch.setattr("kiro_crew.config.paths._agents_dir_override", None)
    pinned = _no_resolve_before_the_pin(monkeypatch)

    assert stale_config._user_agents_dir() == home / "agents"
    signature = dict(stale_config.input_signature(SpawnInputs(agent="kirocrew", project="")))
    assert signature["user-agents"] not in (("refused",), ("unreadable",), ("absent",))
    assert pinned
    assert stale_config._spec_scope_unlistable("", "kiro") is False


@pytest.mark.parametrize(
    "raw",
    [
        "//attacker/share/.kiro/agents",
        "\\\\attacker\\share\\.kiro\\agents",
        "\\\\?\\UNC\\attacker\\share\\agents",
        "\\\\.\\pipe\\agents",
    ],
    ids=["unc-slash", "unc-backslash", "extended-unc", "device"],
)
def test_windows_gate_refuses_unc_and_device_paths_even_when_trusted(raw, monkeypatch):
    """Refused by spelling alone, a trusted UNC root included: nothing is probed."""
    pinned = _no_resolve_before_the_pin(monkeypatch)
    monkeypatch.setattr(stale_config.hooks, "unc_probe_allowed", lambda _raw: True)

    assert stale_config._gated_dir_windows(raw) is None
    assert pinned == []


@pytest.mark.parametrize("leaf", [".ssh", ".aws/sso", ".gnupg"])
@pytest.mark.parametrize("root", ["home", "os-home"])
def test_windows_gate_refuses_a_credential_dir_lexically(leaf, root, tmp_path, monkeypatch):
    """The shared fence's leaves, anchored on ``$HOME`` and override roots unresolved."""
    base = tmp_path / root
    if root == "home":
        monkeypatch.setenv("HOME", str(base))
    else:
        monkeypatch.setenv("KIROCREW_OS_HOME", str(base))
    pinned = _no_resolve_before_the_pin(monkeypatch)

    assert stale_config._gated_dir_windows(str(base.joinpath(*leaf.split("/")))) is None
    assert stale_config._gated_dir_windows(str(base / "projects" / ".kiro" / "agents")) is not None
    assert pinned == []


def test_windows_pin_fence_refuses_a_credential_target_with_the_real_gate(tmp_path, monkeypatch):
    """A harmless spelling whose pinned handle is really ``~/.ssh`` is refused after the pin."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    directory = tmp_path / "plain"
    directory.mkdir()
    _no_resolve_before_the_pin(monkeypatch)
    monkeypatch.setattr(stale_config, "fd_real_path", lambda _fd: str(home / ".ssh"))

    admitted = stale_config._gated_dir(directory)
    assert admitted == directory
    with pytest.raises(PermissionError):
        stale_config._pin(admitted)
    assert stale_config._stat_spec_dir(directory) == ("unreadable",)
