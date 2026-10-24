"""Stale-config detection: does a live chat run on config changed since it started?

Covers the fingerprint (``dashboard/stale_config.py``) and its per-backend spec
resolution, the record kept against the provider a turn ran on
(``dashboard/config_staleness.py``), and the warm-pool and prewarm records.
The status, the slot badge and the ``session_config_status`` verb, route and MCP
tool live in ``test_stale_config_status.py``; idle chats in
``test_stale_config_idle_refresh.py``; what the fingerprint scans in
``test_stale_config_scan_paths.py``. Nothing here relaunches a process.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from stale_config_helpers import _scan_on_every_os_fixture  # noqa: F401
from stale_config_helpers import kiro_home_fixture  # noqa: F401
from stale_config_helpers import (
    _A,
    _CHANGED,
    _SERVER,
    _fp,
    _gate_refuses,
    _pool_manager,
    _Provider,
    _status_slot,
    _write_spec,
    _write_workspace_spec,
)

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard import config_staleness as cs
from kiro_crew.dashboard import stale_config
from kiro_crew.dashboard.stale_config import (
    ConfigFingerprint,
    SpawnInputs,
    compute_fingerprint,
    is_stale,
    make_record,
)
from kiro_crew.testing.wait import async_wait_until


def _md_spec(path: Path, prompt: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nname: kirocrew\n---\n%s\n" % prompt)


def test_unchanged_config_fingerprints_identically(kiro_home):
    _write_spec(kiro_home, {"mcpServers": _SERVER})
    assert _fp() == _fp()


def _with_env(**env: str) -> dict:
    return {"a": {"command": "a", "env": env}}


@pytest.mark.parametrize("where", ["spec", "mcp_json"])
def test_a_relaunch_nonce_in_an_mcp_server_env_is_not_a_config_change(kiro_home, where):
    def write(servers: dict) -> None:
        if where == "spec":
            _write_spec(kiro_home, {"mcpServers": servers})
        else:
            _write_spec(kiro_home, {"prompt": "a"})
            (kiro_home / "settings" / "mcp.json").write_text(json.dumps({"mcpServers": servers}))

    write(_with_env(AIM_CREDS_AGENT_INJECTION="n1", K="v"))
    before = _fp()
    write(_with_env(AIM_CREDS_AGENT_INJECTION="n2", K="v"))
    assert _fp() == before
    write(_with_env(AIM_CREDS_AGENT_INJECTION="n2", K="rotated"))
    assert _fp().reconcilable != before.reconcilable


def test_a_dropped_nonce_key_is_still_a_config_change(kiro_home):
    _write_spec(kiro_home, {"mcpServers": _with_env(AIM_CREDS_AGENT_INJECTION="n1", K="v")})
    before = _fp()
    _write_spec(kiro_home, {"mcpServers": _with_env(K="v")})
    assert _fp().reconcilable != before.reconcilable


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


@pytest.mark.parametrize("switch_off", ["disabled", "entry-removed"])
def test_a_ref_removed_with_its_server_switched_off_is_not_stale_where_mcp_hot_reloads(
    kiro_home, switch_off
):
    """GPT: the dashboard's toggle removes the ref AND disables the server; the reconcile stops it.

    Nothing stays mounted, so a Reload would be for a change already in effect.
    A provider that does not apply MCP edits itself still needs the Reload.
    """
    _write_spec(kiro_home, {"mcpServers": _SERVER, "tools": ["fs_read", "@a"]})
    before = _fp()
    servers = {"a": {**_SERVER["a"], "disabled": True}} if switch_off == "disabled" else {}
    _write_spec(kiro_home, {"mcpServers": servers, "tools": ["fs_read"]})
    after = _fp()

    assert not is_stale(before, after, hot_reloads=True)
    assert is_stale(before, after, hot_reloads=False)


@pytest.mark.parametrize("ref", ["@a", "@a/tool_one"], ids=["whole-server", "per-tool"])
def test_a_removed_ref_is_judged_by_the_server_it_names(kiro_home, ref):
    """Opus: a ``@server/tool`` ref removed while its server runs stays mounted, as a whole-server ref does.

    The refs are digests of their text, so the server each names is carried
    beside them (``ref_servers``) and the removal is looked up through it.
    """
    _write_spec(kiro_home, {"mcpServers": _SERVER, "tools": ["fs_read", ref]})
    before = _fp()
    assert dict(before.ref_servers) == {
        stale_config._part_digest(ref): stale_config._part_digest("@a")
    }
    _write_spec(kiro_home, {"mcpServers": _SERVER, "tools": ["fs_read"]})
    assert is_stale(before, _fp(), hot_reloads=True)

    # Removed together with the server's switch-off: stopped, so not stale.
    _write_spec(
        kiro_home, {"mcpServers": {"a": {**_SERVER["a"], "disabled": True}}, "tools": ["fs_read"]}
    )
    assert not is_stale(before, _fp(), hot_reloads=True)


def test_a_removed_ref_with_no_server_mapping_reads_as_mounted():
    """A hand-built or pre-mapping record cannot say which server a ref named: err toward Reload."""
    ref = stale_config._part_digest("@a/tool_one")
    before = stale_config.ConfigFingerprint("r", "s", tool_refs=frozenset({ref}))
    after = stale_config.ConfigFingerprint("r", "s", live_server_refs=frozenset())
    assert is_stale(before, after, hot_reloads=True)


def test_a_ref_removed_while_its_server_is_declared_elsewhere_stays_stale(kiro_home, tmp_path):
    """A server still enabled in either ``mcp.json`` may be the one mounted: the removal is stale."""
    project = tmp_path / "proj"
    (project / ".kiro" / "settings").mkdir(parents=True)
    _write_spec(kiro_home, {"mcpServers": _SERVER, "tools": ["@a"]})
    (kiro_home / "settings" / "mcp.json").write_text(json.dumps({"mcpServers": _SERVER}))
    before = _fp(project=str(project))
    _write_spec(kiro_home, {"mcpServers": {}, "tools": []})
    assert is_stale(before, _fp(project=str(project)), hot_reloads=True)

    # Switched off there as well: stopped everywhere, so not stale.
    (kiro_home / "settings" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"a": {**_SERVER["a"], "disabled": True}}})
    )
    assert not is_stale(before, _fp(project=str(project)), hot_reloads=True)


def test_a_live_set_past_the_bound_reads_every_removed_ref_as_mounted():
    cap = stale_config.FINGERPRINT_MAX_TOOL_REFS
    before = stale_config.ConfigFingerprint(
        "r", "s", tool_refs=frozenset({stale_config._part_digest("@a")})
    )
    many = {f"s{i}": {"command": "x"} for i in range(cap + 2)}
    live = stale_config._live_server_refs(("ok", {"mcpServers": many}))
    assert any(r.startswith("overflow:") for r in live)
    after = stale_config.ConfigFingerprint("r", "s", live_server_refs=live)

    assert is_stale(before, after, hot_reloads=True)


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
        _gate_refuses(monkeypatch, lambda raw: raw == str(target))
    name = {"spec": "spec", "global-mcp": "global_mcp", "workspace-mcp": "workspace_mcp"}[where]
    # The file planted is the user-level spec, which carries its own input too.
    names = {"spec", "user_spec"} if where == "spec" else {name}
    assert _fp(project=str(project)).unreadable == frozenset(names)


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


def test_a_comment_only_mcp_json_edit_is_not_a_change(kiro_home):
    """``mcp.json`` is JSONC to every reader the session has; it is here too.

    A commented settings file parses to the same document as its plain form, so
    a comment-only edit leaves the digest alone: identical effective settings
    never flag a chat stale. The document is what is digested, as the session
    reads it.
    """
    _write_spec(kiro_home, {})
    mcp = kiro_home / "settings" / "mcp.json"
    mcp.write_text('{"mcpServers": {"x": {"command": "x"}}}')
    before = _fp()
    mcp.write_text('{\n  // the x server\n  "mcpServers": {"x": {"command": "x"},},\n}')
    assert _fp() == before
    assert dict(_fp().parts)["global_mcp"] == dict(before.parts)["global_mcp"]
    mcp.write_text('{\n  // the x server\n  "mcpServers": {"x": {"command": "y"}}\n}')
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
@pytest.mark.parametrize("backend", ["", "claude"], ids=["kiro-opts-out", "claude"])
def test_creating_an_unmounted_mcp_json_with_an_ordinary_server_is_not_stale(
    kiro_home, tmp_path, where, backend
):
    """GPT: a file that did not exist restricts nothing, as one with only ordinary servers does.

    A session that does not mount the file's servers (a spec that opts out on
    kiro-cli; every mirror backend) gains nothing from a Reload, so the file
    appearing must read as the empty restriction view it already had.
    """
    spec = {"includeMcpJson": False} if backend == "" else {}
    _write_spec(kiro_home, spec)
    project = tmp_path / "proj"
    (project / ".kiro" / "settings").mkdir(parents=True)
    before = _fp(project=str(project), backend=backend)
    path = (
        kiro_home / "settings" / "mcp.json"
        if where == "global"
        else project / ".kiro" / "settings" / "mcp.json"
    )
    assert not path.exists()
    path.write_text('{"mcpServers": {"x": {"command": "x"}}}')
    after = _fp(project=str(project), backend=backend)

    assert not is_stale(before, after, hot_reloads=False)
    assert stale_config.changed_inputs(before, after) == frozenset()
    # A switch-off in the new file is still a change the session reads at start.
    path.write_text('{"mcpServers": {"x": {"command": "x", "disabled": true}}}')
    assert is_stale(before, _fp(project=str(project), backend=backend), hot_reloads=False)


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


def test_a_workspace_mcp_json_linked_at_a_credential_file_is_never_read(kiro_home, tmp_path):
    """The workspace file is workspace-controlled: it goes through the credential gate.

    A link planted at ``<project>/.kiro/settings/mcp.json`` that points at a
    credential file hashes as ``refused`` -- the shared gate screens it before
    the file is probed -- so its bytes never reach the digest.
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
    """Past the resolver's read budget the file is unknown, never unchanged."""
    from kiro_crew import agent

    _write_spec(kiro_home, {"allowedTools": ["@a"]})
    monkeypatch.setattr(agent, "PINNED_SPEC_DIR_MAX_TOTAL_BYTES", 16)
    marker, document = stale_config._read_json(kiro_home / "agents" / "kirocrew.json")
    assert (marker, document) == ("unreadable", None)
    assert marker in stale_config._NO_FIELDS  # so carry_forward keeps the recorded value


def test_an_mcp_json_just_over_the_8_mb_budget_is_unreadable_not_parsed(kiro_home, monkeypatch):
    """The read cap is the resolver's 8 MB budget, not the 50 MB gate cap."""
    from kiro_crew import agent

    assert agent.PINNED_SPEC_DIR_MAX_TOTAL_BYTES == 8 * 1024 * 1024
    parsed = MagicMock(side_effect=AssertionError("an over-budget file was parsed"))
    monkeypatch.setattr(stale_config, "parse_agent_spec_bytes", parsed)
    target = kiro_home / "settings" / "mcp.json"
    target.write_bytes(b" " * (agent.PINNED_SPEC_DIR_MAX_TOTAL_BYTES + 1))

    assert stale_config._read_json(target) == ("unreadable", None)
    parsed.assert_not_called()


def _scope_unreadable(monkeypatch, directory: Path) -> dict[str, bool]:
    """Make resolving the agents scope *directory* raise, while the returned flag is set."""
    real = stale_config._resolve_in_scope
    flag = {"on": True}

    def _resolve(scope_dir, agent, *, json_only):
        if flag["on"] and Path(scope_dir) == directory:
            raise PermissionError(f"unreadable: {scope_dir}")
        return real(scope_dir, agent, json_only=json_only)

    monkeypatch.setattr(stale_config, "_resolve_in_scope", _resolve)
    return flag


def test_an_unreadable_project_scope_still_compares_the_user_spec(kiro_home, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    (project / ".kiro" / "agents").mkdir(parents=True)
    _write_spec(kiro_home, {"prompt": "a"})
    recorded = _fp(project=str(project))
    assert recorded.unreadable == frozenset()

    _scope_unreadable(monkeypatch, project / ".kiro" / "agents")
    _write_spec(kiro_home, {"prompt": "b"})
    current = _fp(project=str(project))

    assert "spec" in current.unreadable
    compared = stale_config.carry_forward(recorded, current)
    assert is_stale(recorded, compared, hot_reloads=False)
    assert "spec" in stale_config.changed_inputs(recorded, compared)


def test_a_user_spec_edit_made_while_the_project_scope_was_unreadable_is_not_adopted(
    kiro_home, tmp_path, monkeypatch
):
    project = tmp_path / "proj"
    (project / ".kiro" / "agents").mkdir(parents=True)
    _write_spec(kiro_home, {"prompt": "a"})
    flag = _scope_unreadable(monkeypatch, project / ".kiro" / "agents")
    recorded = _fp(project=str(project))
    assert "spec" in recorded.unreadable

    _write_spec(kiro_home, {"prompt": "b"})
    flag["on"] = False
    current = _fp(project=str(project))

    baseline = stale_config.adopt_first_reads(recorded, current)
    assert is_stale(baseline, stale_config.carry_forward(baseline, current), hot_reloads=False)


def test_an_unreadable_user_scope_reads_unknown_not_stale(kiro_home, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    (project / ".kiro" / "agents").mkdir(parents=True)
    _write_spec(kiro_home, {"prompt": "a"})
    state, slot = _status_slot(tmp_path, monkeypatch, project=str(project))

    _scope_unreadable(monkeypatch, kiro_home / "agents")
    _write_spec(kiro_home, {"prompt": "b"})
    out = asyncio.run(cs.config_stale_status(state, slot))

    assert out["stale"] is None
    assert out["changed"] == []
    assert len(out["unreadable"]) == 1


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
    """KAS projects the user-level spec, markdown included; a checkout spec is ignored while it exists."""
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


def test_kas_falls_back_to_the_checkout_spec_the_user_level_does_not_declare(tmp_path, kiro_home):
    """A project-only agent on KAS runs on the checkout spec, so that is the file watched.

    Only the keys KAS honours there are compared: an edit to one it ignores is
    no change to the running session, and a user-level spec appearing takes
    over the selection (KAS reads the user level first).
    """
    project = tmp_path / "proj"
    _write_workspace_spec(project, {"prompt": "project", "mcpServers": {"gh": {"command": "gh"}}})
    before = _fp(project=str(project), backend="kas")
    parts = dict(before.parts)
    assert parts["spec_ws"] and parts["spec_path"].startswith(str(project))

    _write_workspace_spec(project, {"prompt": "project", "mcpServers": {"gh": {"command": "gh2"}}})
    assert not is_stale(before, _fp(project=str(project), backend="kas"), hot_reloads=False)
    _write_workspace_spec(
        project, {"prompt": "project, edited", "mcpServers": {"gh": {"command": "gh2"}}}
    )
    assert is_stale(before, _fp(project=str(project), backend="kas"), hot_reloads=False)

    _md_spec(kiro_home / "agents" / "kirocrew.md", "user")
    after = _fp(project=str(project), backend="kas")
    assert not dict(after.parts)["spec_ws"]
    assert is_stale(before, after, hot_reloads=False)


def test_a_crewmate_folder_spec_edit_badges_a_kas_chat_and_moves_the_sweep_signature(
    kiro_home, tmp_path, monkeypatch
):
    """KAS loads a crewmate's own spec from the crewmate folder: an edit there is watched."""
    from kiro_crew.dashboard.stale_config import input_signature

    spec = kiro_home / "mates" / "agents" / "kirocrew.json"
    spec.parent.mkdir(parents=True)
    spec.write_text(json.dumps({"name": "kirocrew", "prompt": "crewmate"}))
    before = _fp(backend="kas")
    assert dict(before.parts)["spec_path"] == str(spec)
    sig = input_signature(SpawnInputs("kirocrew", ""))

    spec.write_text(json.dumps({"name": "kirocrew", "prompt": "crewmate, edited"}))
    os.utime(spec, ns=(spec.stat().st_atime_ns, spec.stat().st_mtime_ns + 2_000_000))

    assert is_stale(before, _fp(backend="kas"), hot_reloads=False)
    assert input_signature(SpawnInputs("kirocrew", "")) != sig

    # End to end: the status names the crewmate folder's file, not a user-level twin.
    monkeypatch.setattr(cs.Path, "home", classmethod(lambda cls: kiro_home.parent))
    state, slot = _status_slot(tmp_path, monkeypatch, recorded=before)
    cs.KiroCrewConfig.load.return_value.agent.acp_backend = "kas"
    out = asyncio.run(cs.config_stale_status(state, slot))
    assert out["stale"] is True
    assert out["changed"] == [f"~/{kiro_home.name}/mates/agents/kirocrew.json"]


def test_an_array_backed_backend_loads_a_workspace_markdown_spec(tmp_path, kiro_home):
    """Crew reads the checkout's spec for it in either form, so the .md is watched.

    Its content reaches a mirrored session only when no same-named user spec
    exists: with one, the session consumes the user spec plus the project spec's
    switch-off keys (``acp/session_mcp._agent_spec_and_snapshot_for``), so a
    project prompt edit then changes nothing the session runs on.
    """
    project = tmp_path / "proj"
    _md_spec(project / ".kiro" / "agents" / "kirocrew.md", "p")
    before = _fp(project=str(project), backend="claude")
    assert dict(before.parts)["spec_ws"]
    _md_spec(project / ".kiro" / "agents" / "kirocrew.md", "planted")
    assert is_stale(before, _fp(project=str(project), backend="claude"), hot_reloads=True)

    # With a same-named user spec the session runs on that one: the same
    # project edit is not a change (kiro-cli, which loads the .json itself,
    # is covered by the parity tests in test_stale_config_scan_paths).
    _write_spec(kiro_home, {"allowedTools": ["@a"]})
    _md_spec(project / ".kiro" / "agents" / "kirocrew.md", "p")
    before = _fp(project=str(project), backend="claude")
    _md_spec(project / ".kiro" / "agents" / "kirocrew.md", "planted")
    assert not is_stale(before, _fp(project=str(project), backend="claude"), hot_reloads=True)


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


@pytest.mark.parametrize(
    ("user_spec_path", "expected"),
    [
        # Unreadable user-level spec while a workspace spec is selected: the
        # label names the user-level file, never the readable workspace one.
        ("/home/someone/.kiro/agents/kirocrew.json", "~/.kiro/agents/kirocrew.json"),
        # No user-level file was found (the directory itself could not be
        # listed): the directory is named.
        ("", "~/.kiro/agents"),
    ],
)
def test_an_unreadable_user_spec_is_labelled_by_its_own_path(user_spec_path, expected):
    agents_dir = Path.home().joinpath(".kiro", "agents")
    fp = ConfigFingerprint(
        "r",
        "s",
        parts=(
            ("spec_path", "/proj/.kiro/agents/kirocrew.json"),
            ("spec_ws", "1"),
            ("user_spec_path", user_spec_path),
        ),
    )
    label = cs.display_input("user_spec", fp, agents_dir)
    assert label == expected
    assert label != cs.display_input("spec", fp, agents_dir)


def test_a_crewmate_folder_spec_is_labelled_by_its_own_folder(monkeypatch, tmp_path):
    """KAS read the crewmate folder's spec: the label names that file, not a user-level twin.

    The directories are resolved off the loop (``_recompute``) and handed in; the
    label builder itself resolves nothing, so it touches no filesystem on the loop.
    """
    home = tmp_path / "home"
    monkeypatch.setattr(cs.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(
        "kiro_crew.config.paths.kiro_home", lambda: (_ for _ in ()).throw(AssertionError("on loop"))
    )
    fp = ConfigFingerprint(
        "r", "s", parts=(("spec_path", str(home / ".kiro/mates/agents/kirocrew.json")),)
    )
    agents_dir, mate_dir = home / ".kiro" / "agents", home / ".kiro" / "mates" / "agents"
    assert (
        cs.display_input("spec", fp, agents_dir, mate_dir) == "~/.kiro/mates/agents/kirocrew.json"
    )
    # The user-level twin, when that is the recorded path, keeps its own label.
    fp_user = ConfigFingerprint("r", "s", parts=(("spec_path", str(agents_dir / "kirocrew.json")),))
    assert cs.display_input("spec", fp_user, agents_dir, mate_dir) == "~/.kiro/agents/kirocrew.json"


def test_a_user_spec_label_outside_home_shows_no_absolute_path_either(monkeypatch, tmp_path):
    monkeypatch.setattr(cs.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    fp = ConfigFingerprint(
        "r",
        "s",
        parts=(
            ("spec_path", "/proj/.kiro/agents/kirocrew.json"),
            ("spec_ws", "1"),
            ("user_spec_path", str(tmp_path / "kiro/agents/kirocrew.json")),
        ),
    )
    label = cs.display_input("user_spec", fp, tmp_path / "kiro" / "agents")
    assert label == "agents/kirocrew.json"
    assert str(tmp_path) not in label


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
    from kiro_crew.dashboard.server_runtime import api_entrypoint

    # One wiring per entrypoint: start_dashboard in server, start_api_server in its owner.
    for module in (server, api_entrypoint):
        src = inspect.getsource(module)
        assert src.count("state.sessions.spawn_config_reader = _pool_spawn_config") == 1


def test_a_cancel_during_the_prewarm_fingerprint_leaks_no_reservation(tmp_path, monkeypatch):
    """The fingerprint await runs before admission: cancelling it reserves nothing."""
    import threading

    from test_chat_runner_coverage import _runner_state, _slot
    from test_chat_send_agent_model_default import _config, _pin_sync_accessors

    monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)
    monkeypatch.setattr(chat_runner, "_eager_spawn_sem", asyncio.Semaphore(1))
    # Host-derived: a tight host admits no pre-warm at all, which is not under test.
    monkeypatch.setattr(chat_runner, "_prewarm_allowance", lambda: 1)
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
        # before reaching it fails here instead of hanging the test. Polled on
        # the loop rather than parked on a pool thread: the spawn under test
        # takes the fingerprint through that same pool.
        waiter = asyncio.ensure_future(async_wait_until(started.is_set, timeout=5))
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
    # Host-derived: a tight host admits no pre-warm at all, which is not under test.
    monkeypatch.setattr(chat_runner, "_prewarm_allowance", lambda: 1)
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


def test_a_turn_leaves_a_missing_managed_spec_for_its_spawn_to_heal(monkeypatch):
    """The turn path takes no fingerprint while the managed spec is missing.

    Healing it there regenerates every managed spec ahead of the turn, which
    delays the turn itself; the spawn heals it, and a later turn records.
    """
    heal = MagicMock(side_effect=AssertionError("the turn path healed the managed spec"))
    monkeypatch.setattr(cs, "ensure_agent_materialized", heal)
    monkeypatch.setattr(chat_runner, "managed_spec_missing", lambda _a: True)
    slot = MagicMock(agent="kirocrew", project="")

    out = asyncio.run(chat_runner._take_spawn_fingerprint(MagicMock(), slot, "s1", None))

    assert out is None
    heal.assert_not_called()


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
    # Host-derived: a tight host admits no pre-warm at all, which is not under test.
    monkeypatch.setattr(chat_runner, "_prewarm_allowance", lambda: 1)
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
