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
import time
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
from kiro_crew.mcp_dashboard import _call_tool_inner


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
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", home / "settings" / "mcp.json")
    return home


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
    kiro_home, tmp_path, monkeypatch, where, how
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
        real = stale_config.validate_file_path
        monkeypatch.setattr(
            stale_config,
            "validate_file_path",
            lambda raw: None if raw == str(target) else real(raw),
        )
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


def test_an_unparseable_spec_is_a_change(kiro_home):
    _write_spec(kiro_home, {"mcpServers": _SERVER})
    before = _fp()
    (kiro_home / "agents" / "kirocrew.json").write_text("{not json")
    after = _fp()
    assert after != before


def test_an_absent_spec_and_an_unreadable_one_are_different(tmp_path):
    missing = tmp_path / "nope.json"
    assert stale_config._read_json(missing)[0] == "absent"
    assert stale_config._read_json(tmp_path)[0] == "unreadable"


def test_a_workspace_mcp_json_linked_at_a_credential_file_is_never_read(kiro_home, tmp_path):
    """The workspace file is workspace-controlled: it goes through the credential gate.

    A link planted at ``<project>/.kiro/settings/mcp.json`` that points at a
    credential file hashes as ``refused`` -- the gate screens it before the
    file is probed, so its bytes never reach the digest.
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


def test_a_refused_file_never_reaches_the_bounded_read(tmp_path, monkeypatch):
    target = tmp_path / "mcp.json"
    target.write_text("{}")
    monkeypatch.setattr(stale_config, "validate_file_path", lambda _raw: None)
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


def test_the_stub_server_set_is_not_an_input(kiro_home):
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
def test_input_labels_are_display_safe(name, spec_ws, expected, monkeypatch):
    # The user-level agents dir as it sits by default, under the home directory.
    monkeypatch.setattr(cs, "kiro_agents_dir", lambda: Path.home().joinpath(".kiro", "agents"))
    fp = ConfigFingerprint(
        "r",
        "s",
        parts=(("spec_path", "/home/someone/x/kirocrew.json"), ("spec_ws", spec_ws)),
    )
    assert cs.display_input(name, fp) == expected


def test_a_user_spec_label_outside_home_shows_no_absolute_path(monkeypatch, tmp_path):
    # Home somewhere the agents dir is not under (on Windows tmp_path itself
    # sits under the home directory, so the real home cannot stand in).
    monkeypatch.setattr(cs.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(cs, "kiro_agents_dir", lambda: tmp_path / "kiro" / "agents")
    fp = ConfigFingerprint(
        "r", "s", parts=(("spec_path", str(tmp_path / "kiro/agents/kirocrew.json")),)
    )
    label = cs.display_input("spec", fp)
    assert label == "agents/kirocrew.json"
    assert str(tmp_path) not in label


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

    async def _run() -> bool:
        task = asyncio.create_task(chat_runner._eager_spawn(state, slot))
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
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

    asyncio.run(chat_runner._eager_spawn(state, slot))

    state.sessions.get_or_create.assert_awaited_once()
    record = slot._spawn_config
    assert record is not None and record.describes(provider)
    assert record.fingerprint == _A

    # The first turn's own record_spawn_config keeps the prewarm's record, so
    # an edit made before the first message is a difference the badge shows.
    cs.record_spawn_config(slot, provider, record.inputs, _CHANGED)
    assert slot._spawn_config.fingerprint == _A


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


def test_an_unknown_reading_leaves_the_badge_as_it_was(kiro_home, tmp_path, monkeypatch):
    _write_spec(kiro_home, {"prompt": "a"})
    state, slot = _status_slot(tmp_path, monkeypatch)
    slot.config_stale = True
    slot.config_stale_inputs = "~/.kiro/agents/kirocrew.json"
    monkeypatch.setattr(cs, "current_config_fingerprint", MagicMock(side_effect=OSError("x")))

    assert asyncio.run(cs.refresh_config_stale(state, slot))["stale"] is None
    assert slot.config_stale is True


def test_a_session_with_no_record_for_its_live_process_is_not_stale(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    state.sessions.get_provider = MagicMock(return_value=_Provider())
    slot._spawn_config = make_record(_Provider(), SpawnInputs("kirocrew", ""), _A)

    assert asyncio.run(cs.config_stale_status(state, slot))["stale"] is False


def test_a_turn_records_the_spawn_config_and_refreshes_the_badge_at_its_end():
    """``_run_chat`` fingerprints before the acquire and records after it."""
    import inspect

    src = inspect.getsource(chat_runner._run_chat)
    taken = src.index("_take_spawn_fingerprint(loaded_cfg, slot, session_key, kiro_agent)")
    acquired = src.index("_acquired = True")
    assert taken < acquired < src.index("_record_spawn_config_quietly(", acquired)
    finally_at = src.index("    finally:\n        # First: hand back the session-switch lock")
    assert "_schedule_config_stale_refresh(state, slot)" in src[finally_at : finally_at + 900]


def test_nothing_relaunches_a_stale_session():
    """Detection only: the turn path has no automatic reload left to take."""
    import inspect

    src = inspect.getsource(chat_runner)
    for gone in ("_consume_stale_config_reload", "_verify_reloaded_config", "approve_agent_reload"):
        assert gone not in src


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


def _tool(resp: dict, args: dict) -> tuple[str, MagicMock]:
    with (
        patch("kiro_crew.mcp_core._resolve_session_key_strict", return_value=_VERIFIED),
        patch("kiro_crew.mcp_dashboard._get", return_value=resp) as get,
    ):
        out = _call_tool_inner("session_config_status", args)
    return out, get


def test_the_tool_reads_the_verb_route_with_the_verified_key():
    out, get = _tool(
        {"ok": True, "target": "chat-2", "stale": True, "changed": ["~/.kiro/agents/k.json"]},
        {"target": "chat-2"},
    )
    assert get.call_args.args[0] == "/api/session-control/config-status?target=chat-2"
    assert get.call_args.args[1] == _VERIFIED
    assert "stale config (~/.kiro/agents/k.json changed" in out
    assert "Reload" in out


def test_the_tool_says_a_current_target_is_current():
    out, _ = _tool({"ok": True, "target": "chat-2", "stale": False}, {"target": "chat-2"})
    assert "runs on its current config" in out


def test_the_tool_says_an_unreadable_input_is_unknown():
    out, _ = _tool(
        {"ok": True, "target": "chat-2", "stale": None, "unreadable": [".kiro/settings/mcp.json"]},
        {"target": "chat-2"},
    )
    assert "unknown" in out and ".kiro/settings/mcp.json" in out


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
    """In the ordered list each entrypoint assigns, so the assignment cannot drop it."""
    import inspect
    import re

    from kiro_crew.dashboard import server

    for entry in (server.start_dashboard, server.start_api_server):
        src = inspect.getsource(entry)
        chain = re.search(r"app\.middlewares\[:\] = \[(.*?)\n\s*\]\n", src, re.S)
        assert chain is not None, entry.__name__
        assert "_config_write_refresh(state)" in chain.group(1), entry.__name__
        assert "_arm_config_stale_sweep(app, state)" in src
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_permission_change_alone_changes_the_signature(kiro_home):
    _write_spec(kiro_home, {"prompt": "a"})
    spec = kiro_home / "agents" / "kirocrew.json"
    st = spec.stat()
    before = stale_config.input_signature(SpawnInputs("kirocrew", ""))
    # The ctime clock is coarse: repeat the change until it ticks past the write's.
    deadline = time.monotonic() + 5
    mode = 0o400
    while spec.stat().st_ctime_ns == st.st_ctime_ns and time.monotonic() < deadline:
        spec.chmod(mode)
        mode ^= 0o200
        time.sleep(0.005)
    os.utime(spec, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert (spec.stat().st_mtime_ns, spec.stat().st_size) == (st.st_mtime_ns, st.st_size)

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
    @pytest.mark.parametrize("step", ["fingerprint", "inputs", "record", "refresh"])
    async def test_a_raising_step_still_lets_the_turn_complete(self, tmp_path, monkeypatch, step):
        from kiro_crew.acp.types import TurnUsage
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.handlers import usage as usage_mod
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        shard_dir = tmp_path / "usage" / "tokens"
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)

        def _boom(*_a, **_k):
            raise ValueError("badge bookkeeping exploded")

        target = {
            "fingerprint": "spawn_config_fingerprint",
            "inputs": "turn_spawn_inputs",
            "record": "record_spawn_config",
            "refresh": "refresh_config_stale",
        }[step]
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
def test_array_backend_parity_with_session_mcp(kiro_home, tmp_path, layout, monkeypatch):
    from kiro_crew.acp import session_mcp

    project = tmp_path / "proj"
    _layout(layout, kiro_home, project)
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: None)
    monkeypatch.setattr(session_mcp, "require_fresh_derived_spec", lambda *_a: None)

    spec, _snapshot = session_mcp._agent_spec_and_snapshot_for(_PARITY_AGENT, str(project))

    expected = None if spec is None else str(spec.get("prompt", "")).strip()
    assert _watched_prompt(project, "claude") == expected


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
    real_gate = stale_config.validate_file_path
    monkeypatch.setattr(
        stale_config,
        "validate_file_path",
        lambda raw: None if Path(raw) == refused else real_gate(raw),
    )
    scanned: list[str] = []
    real_scandir = os.scandir

    def _spy(path):
        scanned.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", _spy)
    return scanned


def test_a_refused_agents_dir_is_never_listed_by_the_signature(kiro_home, monkeypatch):
    agents = kiro_home / "agents"
    _write_spec(kiro_home, {"prompt": "a"})
    scanned = _refuse(monkeypatch, agents)

    assert stale_config._stat_spec_dir(agents) == ("refused",)
    assert str(agents) not in scanned


def test_a_refused_agents_dir_is_never_scanned_by_the_unlistable_check(kiro_home, monkeypatch):
    agents = kiro_home / "agents"
    scanned = _refuse(monkeypatch, agents)

    assert stale_config._spec_scope_unlistable("", "") is True
    assert str(agents) not in scanned


def test_a_refused_agents_dir_is_never_resolved_and_reads_unknown(kiro_home, monkeypatch):
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


def test_a_refused_mcp_json_is_not_stated(kiro_home, monkeypatch):
    target = kiro_home / "settings" / "mcp.json"
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", target)
    real_gate = stale_config.validate_file_path
    monkeypatch.setattr(
        stale_config,
        "validate_file_path",
        lambda raw: None if Path(raw) == target else real_gate(raw),
    )
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


def test_a_refused_workspace_agents_dir_is_never_resolved(kiro_home, tmp_path, monkeypatch):
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
    assert cs.display_input("backend", fp) == "agent.acp_backend"


def test_an_unattributed_change_names_no_particular_file(kiro_home, tmp_path, monkeypatch):
    """A recorded fingerprint with no per-input parts still reads stale, unattributed."""
    _write_spec(kiro_home, {"prompt": "a"})
    state, slot = _status_slot(
        tmp_path, monkeypatch, recorded=ConfigFingerprint(reconcilable="r", spawn_only="s")
    )

    out = asyncio.run(cs.config_stale_status(state, slot))

    assert out["stale"] is True
    assert out["changed"] == ["session configuration"]
