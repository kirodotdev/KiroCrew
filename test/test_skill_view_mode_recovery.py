"""A mode name that is a generated skill view maps back to the agent it was built from.

kiro-cli answers ``session/set_mode`` with ``Mode '<name>' not found`` for a view
the running process never loaded. These tests pin both halves of the fix:
a stored view name maps back to the agent it was built from before anything is
sent, and the error for a view kiro-cli has not loaded names that agent and a
repair that keeps the operator's config. Recovering a view changed under a
warm runtime is the set_mode bracket's job, not this file's.
"""

from __future__ import annotations

import gc
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp import skill_projection as projection
from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError, _format_runtime_rpc_error

VIEW = "kirocrew-skill-view-" + "a" * 24
OTHER_VIEW = "kirocrew-skill-view-" + "b" * 24


@pytest.fixture(autouse=True)
def _fresh_view_memory(monkeypatch, tmp_path):
    # No test reads this host's own agents directory for a sidecar.
    monkeypatch.setattr(projection, "_VIEW_SOURCES", {})
    monkeypatch.setattr(projection, "kiro_agents_dir", lambda: tmp_path / "no-agents")


@pytest.fixture
def native_tree(tmp_path, monkeypatch):
    monkeypatch.delenv("KIROCREW_NATIVE_SKILL_PROJECTION", raising=False)
    home = tmp_path / "kiro"
    agents = home / "agents"
    agents.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(projection, "kiro_home", lambda: home)
    monkeypatch.setattr(projection, "data_home", lambda: tmp_path / "crew", raising=False)
    monkeypatch.setattr(projection, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(projection.platform_compat, "path_volume_is_remote", lambda path: False)
    monkeypatch.setattr(projection.platform_compat, "first_linked_ancestor", lambda path: None)
    monkeypatch.setattr(
        "kiro_crew.agent.managed_mcp_spec_entry",
        lambda name: {"command": "test-core", "args": []},
    )
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="custom", filename="custom.json", scope="global")],
    )
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", home / "settings" / "mcp.json")
    (agents / "custom.json").write_text('{"name":"custom","description":"v1"}', encoding="utf-8")
    return agents, project


# ── A stored view name maps back to its source agent ──


def test_a_view_from_an_earlier_process_maps_back_through_its_sidecar(native_tree):
    agents, project = native_tree
    prepared = projection.prepare_native_skill_projection(project)
    stored = prepared.agent("custom")
    # A new process: nothing in memory, and the view file itself is gone.
    projection._VIEW_SOURCES.clear()
    (agents / f"{stored}.json").unlink()
    assert projection.source_agent_name(stored) == "custom"


def test_a_view_resolves_after_the_boot_drain_removed_alias_and_sidecar(native_tree):
    agents, project = native_tree
    prepared = projection.prepare_native_skill_projection(project)
    stored = prepared.agent("custom")
    # A gateway restart: nothing in memory, and the drain took both files.
    projection._VIEW_SOURCES.clear()
    (agents / f"{stored}.json").unlink()
    (agents / projection._PROJECTION_METADATA_DIR_NAME / f"{stored}.json").unlink()
    assert projection.source_agent_name(stored) == "custom"


def _pre_ledger_backlog(agents, project, count, spec=None):
    """*count* released views of "custom", each alias with its sidecar, and NO ledger.

    The tree a build that predates the ledger left: every version is held while the
    next is published, so no spawn prunes it, then all are released at once.
    """
    held = []
    for n in range(count):
        version = {**(spec or {"name": "custom"}), "description": f"v{n}"}
        (agents / "custom.json").write_text(json.dumps(version), encoding="utf-8")
        held.append(projection.prepare_native_skill_projection(project))
    views = [prepared.agent("custom") for prepared in held]
    del held
    gc.collect()
    metadata = agents / projection._PROJECTION_METADATA_DIR_NAME
    (metadata / projection._VIEW_LEDGER_NAME).unlink()
    projection._VIEW_SOURCES.clear()
    return views, metadata


def test_the_boot_drain_records_a_backlog_in_one_ledger_write(native_tree, monkeypatch):
    agents, project = native_tree
    views, metadata = _pre_ledger_backlog(agents, project, 7)
    # Half lost their alias to an external cleanup; the drain retires those
    # sidecars through the orphan sweep, and the rest as whole pairs.
    for view in views[::2]:
        (agents / f"{view}.json").unlink()
    writes = []
    real_write = projection._record_view_ledger_entries

    def counted(metadata_dir, entries):
        writes.append(dict(entries))
        return real_write(metadata_dir, entries)

    monkeypatch.setattr(projection, "_record_view_ledger_entries", counted)
    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)

    assert projection.drain_stale_aliases() == 3
    assert not any((metadata / f"{view}.json").exists() for view in views)
    # One write for the prune's pairs and one for the sweep's orphans, not one per view.
    assert len(writes) == 2
    assert projection._read_view_ledger(metadata) == {view: "custom" for view in views}
    assert all(projection.source_agent_name(view) == "custom" for view in views)


@pytest.mark.parametrize("alias_removed", [True, False])
def test_a_sidecar_the_ledger_cannot_take_is_kept_and_its_alias_goes(
    native_tree, monkeypatch, alias_removed
):
    agents, project = native_tree
    (view,), metadata = _pre_ledger_backlog(agents, project, 1)
    if alias_removed:
        (agents / f"{view}.json").unlink()

    def refused(metadata_dir, entries):
        raise OSError("read-only metadata directory")

    monkeypatch.setattr(projection, "_record_view_ledger_entries", refused)
    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)

    # The alias, the file kiro-cli pays for, still goes; the sidecar, the only
    # record of the view's agent, stays and answers on its own.
    assert projection.drain_stale_aliases() == (0 if alias_removed else 1)
    assert (metadata / f"{view}.json").is_file()
    assert not (agents / f"{view}.json").exists()
    assert projection.source_agent_name(view) == "custom"


def test_a_ledger_that_already_holds_the_views_is_not_rewritten(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    metadata = agents / projection._PROJECTION_METADATA_DIR_NAME
    metadata.mkdir(parents=True)
    projection._record_view_ledger_entries(metadata, {VIEW: "custom"})
    writes = []
    monkeypatch.setattr(projection, "atomic_write", lambda *a, **k: writes.append(a))
    ledger = projection._ViewLedgerWrites(agents, "home")
    assert ledger.retain_many({VIEW: "custom"})
    assert ledger.retain(VIEW, {"x-kirocrew-agent": "custom"})
    # A record naming no admissible agent has nothing to keep.
    assert ledger.retain(OTHER_VIEW, {"x-kirocrew-agent": OTHER_VIEW})
    assert writes == []


def test_the_look_ahead_records_only_this_homes_views(tmp_path):
    agents = tmp_path / "agents"
    metadata = agents / projection._PROJECTION_METADATA_DIR_NAME
    metadata.mkdir(parents=True)
    names = ["kirocrew-skill-view-" + f"{n:024x}" for n in range(3)]
    homes = ["mine", "theirs", "mine"]
    for name, home in zip(names, homes):
        (metadata / f"{name}.json").write_text(
            '{"x-kirocrew-managed": "skill-view", "x-kirocrew-home": "%s", '
            '"x-kirocrew-agent": "agent-%s"}' % (home, name[-1]),
            encoding="utf-8",
        )
    ledger = projection._ViewLedgerWrites(agents, "mine")
    ledger.look_ahead([agents / f"{name}.json" for name in names], 0)
    assert ledger.retain(names[0], {"x-kirocrew-agent": "agent-0"})
    # One write covered the window; another home's view took no slot.
    assert projection._read_view_ledger(metadata) == {names[0]: "agent-0", names[2]: "agent-2"}


def test_a_reserialized_pair_the_ledger_cannot_take_keeps_its_sidecar(native_tree, monkeypatch):
    agents, project = native_tree
    spec = {
        "name": "custom",
        "resources": ["skill://catalog/s/SKILL.md"],
        "mcpServers": {"injected": {"command": "helper", "env": {"TOKEN": "v1"}}},
    }
    (view,), metadata = _pre_ledger_backlog(agents, project, 1, spec)
    alias = agents / f"{view}.json"
    # The alias bytes moved, so the drain takes the re-serialized pair path.
    alias.write_text(alias.read_text(encoding="utf-8").replace('"v1"', '"v2"'), encoding="utf-8")

    def refused(metadata_dir, entries):
        raise OSError("read-only metadata directory")

    monkeypatch.setattr(projection, "_record_view_ledger_entries", refused)
    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)

    assert projection.drain_stale_aliases() == 1
    assert not alias.exists() and (metadata / f"{view}.json").is_file()
    assert projection.source_agent_name(view) == "custom"


def test_the_bound_drops_redundant_entries_then_the_oldest_retired_ones(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(projection, "_VIEW_SOURCES_MAX", 2)
    monkeypatch.setattr(projection, "_LEDGER_EVICTION_WARNED", False)
    names = ["kirocrew-skill-view-" + f"{n:024x}" for n in range(4)]
    projection._record_view_ledger_entries(tmp_path, {names[0]: "a", names[1]: "b"})
    # names[0]'s sidecar is present but unreadable, so it answers for nothing;
    # names[1]'s still maps it to "b", so that entry is redundant and goes first,
    # although names[0] is older.
    (tmp_path / f"{names[0]}.json").write_text("not json", encoding="utf-8")
    (tmp_path / f"{names[1]}.json").write_text(
        '{"x-kirocrew-managed": "skill-view", "x-kirocrew-agent": "b"}', encoding="utf-8"
    )
    held = projection._record_view_ledger_entries(tmp_path, {names[2]: "c"})
    assert held == {names[0]: "a", names[2]: "c"}
    # Full of views nothing else records: the oldest goes, the new pair is held
    # (a full ledger never stops a sweep), and the loss is said out loud.
    with caplog.at_level("WARNING", logger=projection.logger.name):
        held = projection._record_view_ledger_entries(tmp_path, {names[3]: "d"})
    assert held == {names[2]: "c", names[3]: "d"}
    assert "dropped the 1 oldest retired view name" in caplog.text
    # A ledger already past the bound (one written under a larger bound) is
    # trimmed back to it, oldest first, the next time anything is written.
    monkeypatch.setattr(projection, "_VIEW_SOURCES_MAX", 1)
    assert projection._record_view_ledger_entries(tmp_path, {}) == {names[3]: "d"}


def test_a_batch_rewrites_its_held_pairs_so_the_bound_cannot_evict_them(tmp_path, monkeypatch):
    monkeypatch.setattr(projection, "_VIEW_SOURCES_MAX", 2)
    names = ["kirocrew-skill-view-" + f"{n:024x}" for n in range(3)]
    metadata = tmp_path / "agents" / projection._PROJECTION_METADATA_DIR_NAME
    metadata.mkdir(parents=True)
    projection._record_view_ledger_entries(metadata, {names[0]: "a", names[1]: "b"})
    ledger = projection._ViewLedgerWrites(tmp_path / "agents", "home")
    # names[0] is held but the oldest entry; adding names[2] alone would evict it
    # while the sweep is about to delete its sidecar.
    assert ledger.retain_many({names[0]: "a", names[2]: "c"})
    assert projection._read_view_ledger(metadata) == {names[0]: "a", names[2]: "c"}
    # A batch larger than the bound cannot all be held, so none of it may go.
    monkeypatch.setattr(projection, "_VIEW_SOURCES_MAX", 1)
    assert not projection._ViewLedgerWrites(tmp_path / "agents", "home").retain_many(
        {names[1]: "b", names[2]: "c"}
    )
    assert projection._read_view_ledger(metadata) == {names[2]: "c"}


def test_a_refused_write_is_not_retried_for_every_candidate(tmp_path, monkeypatch):
    metadata = tmp_path / "agents" / projection._PROJECTION_METADATA_DIR_NAME
    metadata.mkdir(parents=True)
    calls = []

    def refused(metadata_dir, entries):
        calls.append(entries)
        raise OSError("no space left on device")

    monkeypatch.setattr(projection, "_record_view_ledger_entries", refused)
    ledger = projection._ViewLedgerWrites(tmp_path / "agents", "home")
    names = ["kirocrew-skill-view-" + f"{n:024x}" for n in range(3)]
    assert not any([ledger.retain(name, {"x-kirocrew-agent": "a"}) for name in names])
    assert len(calls) == 1
    assert not ledger.retain_many({names[0]: "a"})
    assert len(calls) == 1


def test_the_orphan_sweep_unlinks_nothing_past_its_deadline(native_tree, monkeypatch):
    agents, project = native_tree
    views, metadata = _pre_ledger_backlog(agents, project, 2)
    for view in views:
        (agents / f"{view}.json").unlink()
    crew_home_id = projection.data_home().absolute().as_posix()
    # The deadline runs out while the batch's ledger write is in flight: the
    # batch is recorded, and nothing is unlinked past the deadline.
    spent = []
    real_write = projection._record_view_ledger_entries

    def slow_write(metadata_dir, entries):
        spent.append(True)
        return real_write(metadata_dir, entries)

    monkeypatch.setattr(projection, "_record_view_ledger_entries", slow_write)
    monkeypatch.setattr(projection.time, "monotonic", lambda: 100.0 if spent else 0.0)
    assert projection._sweep_orphan_sidecars(agents, crew_home_id, skip=set(), deadline=50.0) == 0
    assert spent and projection._read_view_ledger(metadata) == {view: "custom" for view in views}
    assert all((metadata / f"{view}.json").is_file() for view in views)


def test_the_view_ledger_keeps_its_newest_admissible_entries_over_redundant_ones(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(projection, "_VIEW_SOURCES_MAX", 2)
    names = ["kirocrew-skill-view-" + f"{n:024x}" for n in range(3)]
    projection._record_view_ledger(tmp_path, {"a": names[0], "b": names[1]})
    # The oldest view's sidecar still answers for it, so the bound may drop it.
    (tmp_path / f"{names[0]}.json").write_text(
        '{"x-kirocrew-managed": "skill-view", "x-kirocrew-agent": "a"}', encoding="utf-8"
    )
    projection._record_view_ledger(tmp_path, {"c": names[2], "x" * 10_000: VIEW})
    assert projection._read_view_ledger(tmp_path) == {names[1]: "b", names[2]: "c"}
    (tmp_path / projection._VIEW_LEDGER_NAME).write_text(
        '{"../escape": "a", "%s": "%s"}' % (names[0], OTHER_VIEW), encoding="utf-8"
    )
    assert projection._read_view_ledger(tmp_path) == {}


def test_the_view_memory_retains_no_oversized_agent_name():
    projection._remember_view_sources(
        {"x" * 10_000: VIEW, "a\nforged": VIEW, OTHER_VIEW: VIEW, "custom": OTHER_VIEW}
    )
    assert projection._VIEW_SOURCES == {OTHER_VIEW: "custom"}


def test_a_view_nothing_records_is_refused_not_guessed(native_tree):
    with pytest.raises(projection.RetiredSkillView, match="Pick the agent"):
        projection.source_agent_name(VIEW)


def test_an_agent_name_passes_through_without_a_read(monkeypatch):
    monkeypatch.setattr(projection, "kiro_agents_dir", MagicMock(side_effect=AssertionError))
    assert projection.source_agent_name("custom") == "custom"


def test_set_mode_never_sends_a_stale_view_name(native_tree):
    agents, project = native_tree
    first = projection.prepare_native_skill_projection(project)
    stale = first.agent("custom")
    del first
    gc.collect()
    (agents / "custom.json").write_text('{"name":"custom","description":"v2"}', encoding="utf-8")
    current = projection.prepare_native_skill_projection(project)
    assert current.agent("custom") != stale
    sent = current.request("session/set_mode", {"sessionId": "s", "modeId": stale})
    assert sent["modeId"] == current.agent("custom")
    frame = current.frame({"result": {"modes": {"currentModeId": stale}}})
    assert frame["result"]["modes"]["currentModeId"] == "custom"


def test_the_projection_refuses_a_view_it_cannot_attribute():
    prepared = projection.NativeSkillProjection({"custom": OTHER_VIEW})
    assert prepared.agent(OTHER_VIEW) == OTHER_VIEW
    with pytest.raises(projection.RetiredSkillView):
        prepared.request("session/set_mode", {"modeId": VIEW})


@pytest.mark.asyncio
async def test_create_and_load_map_a_stored_view_before_any_guard():
    projection._remember_view_sources({"custom": VIEW})
    rt = AcpRuntime(work_dir="/tmp")
    assert await rt._source_agent(VIEW) == "custom"
    assert await rt._source_agent("kirocrew") == "kirocrew"
    assert await rt._source_agent(None) is None
    with pytest.raises(AcpRuntimeError, match="Pick the agent"):
        await rt._source_agent(OTHER_VIEW)


@pytest.mark.asyncio
async def test_a_handle_set_mode_sends_the_source_agent():
    from kiro_crew.acp.session_handle import AcpSessionHandle

    projection._remember_view_sources({"custom": VIEW})
    runtime = MagicMock()
    runtime.send_request = AsyncMock()
    handle = AcpSessionHandle.__new__(AcpSessionHandle)
    handle._runtime = runtime
    handle._session_id = "s"
    await handle.set_mode(VIEW)
    params = runtime.send_request.await_args.args[1]
    assert params["modeId"] == "custom"


# ── The error names the source agent and keeps the operator's config ──


def _mode_not_found(name: str) -> dict:
    return {"code": -32603, "message": "Internal error", "data": f"Mode '{name}' not found"}


def test_a_missing_view_error_names_its_source_agent():
    projection._remember_view_sources({"custom": VIEW})
    text = _format_runtime_rpc_error(_mode_not_found(VIEW))
    assert "agent 'custom'" in text and VIEW in text
    assert "setup" not in text


def test_a_missing_view_nothing_remembers_still_reads_as_a_view(tmp_path):
    text = _format_runtime_rpc_error(_mode_not_found(OTHER_VIEW))
    assert "skill view" in text and "this agent" in text
    assert "setup" not in text


def test_a_missing_real_spec_suggests_setup_without_clean(tmp_path):
    with patch("kiro_crew.acp.runtime.kiro_agents_dir", return_value=tmp_path):
        text = _format_runtime_rpc_error(_mode_not_found("kirocrew"))
    assert "kirocrew setup --agent-only`" in text
    assert "--clean" not in text


def test_the_direct_client_launches_the_source_agent(tmp_path):
    from kiro_crew.acp.client import AcpClient, AcpError

    projection._remember_view_sources({"custom": VIEW})
    client = AcpClient(work_dir=tmp_path / "wd", agent=VIEW, sandbox_mode="off")
    client._prepare_spawn_workspace()
    assert client._agent == "custom"
    client = AcpClient(work_dir=tmp_path / "wd", agent=OTHER_VIEW, sandbox_mode="off")
    with pytest.raises(AcpError, match="Pick the agent"):
        client._prepare_spawn_workspace()
