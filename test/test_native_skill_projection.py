"""Native metadata cannot grow with the catalog behind an authored mapping."""

from __future__ import annotations

import gc
import hashlib
import json
import os
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conftest import requires_symlinks
from kiro_crew.acp import skill_projection as projection
from kiro_crew.agent_spec_format import iter_agent_spec_files
from kiro_crew.hooks import FileTooLargeError


@pytest.fixture
def native_tree(tmp_path, monkeypatch):
    monkeypatch.delenv("KIROCREW_NATIVE_SKILL_PROJECTION", raising=False)
    home = tmp_path / "kiro"
    crew_home = tmp_path / "crew"
    agents = home / "agents"
    agents.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(projection, "kiro_home", lambda: home)
    monkeypatch.setattr(projection, "data_home", lambda: crew_home, raising=False)
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
    return home, agents, project


def test_native_view_bounds_metadata_and_preserves_original_scope(native_tree):
    home, agents, project = native_tree
    source = agents / "custom.json"
    spec = {
        "name": "custom",
        "prompt": "file://instructions.md",
        "tools": ["read", "@kirocrew-core"],
        "allowedTools": ["read"],
        "resources": ["file://RULES.md", *[f"skill://catalog/s{n}/SKILL.md" for n in range(1024)]],
    }
    original = json.dumps(spec)
    source.write_text(original, encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    alias = prepared.agent("custom")
    view = json.loads((agents / f"{alias}.json").read_text(encoding="utf-8"))
    assert source.read_text(encoding="utf-8") == original
    assert all(not r.startswith("skill://") for r in view["resources"])
    assert len(view["resources"]) == 4
    assert view["tools"] == spec["tools"] and view["allowedTools"] == spec["allowedTools"]
    assert view["prompt"] == "file://" + (agents / "instructions.md").as_posix()
    assert iter_agent_spec_files(agents) == [source]
    settings = json.loads((project / ".kiro/settings/cli.json").read_text(encoding="utf-8"))
    assert settings["chat.disableInheritingDefaultResources"] is True
    assert projection.prepare_native_skill_projection(project).agent("custom") == alias


def test_native_view_preserves_explicit_noninheritance_and_other_settings(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text(
        '{"name":"custom","resources":["file://RULES.md"]}', encoding="utf-8"
    )
    settings_path = project / ".kiro/settings/cli.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps({"chat.disableInheritingDefaultResources": True, "toolSearch.enabled": False}),
        encoding="utf-8",
    )
    prepared = projection.prepare_native_skill_projection(project)
    view = json.loads((agents / f"{prepared.agent('custom')}.json").read_text(encoding="utf-8"))
    assert view["resources"] == ["file://RULES.md"]
    assert json.loads(settings_path.read_text(encoding="utf-8"))["toolSearch.enabled"] is False


def test_transport_keeps_original_agent_identity_and_rejects_unprepared_modes():
    prepared = projection.NativeSkillProjection({"custom": "native-alias"})
    request = {"sessionId": "s", "modeId": "custom"}
    assert prepared.request("session/set_mode", request)["modeId"] == "native-alias"
    assert request["modeId"] == "custom"
    frame = prepared.frame(
        {
            "result": {
                "modes": {
                    "currentModeId": "native-alias",
                    "availableModes": [
                        {"id": "native-alias", "name": "native-alias"},
                        {"id": "unbounded-original"},
                    ],
                }
            }
        }
    )
    assert frame["result"]["modes"] == {
        "currentModeId": "custom",
        "availableModes": [{"id": "custom", "name": "custom"}],
    }
    with pytest.raises(ValueError, match="no prepared"):
        prepared.request("session/set_mode", {"modeId": "unknown"})


@pytest.mark.parametrize(
    "command", ["/agent swap custom", {"command": "agent", "args": {"value": "swap custom"}}]
)
def test_native_agent_switch_cannot_escape_crew_scope(command):
    prepared = projection.NativeSkillProjection({"custom": "native-alias"})
    with pytest.raises(ValueError, match="agent selector"):
        prepared.request("_kiro.dev/commands/execute", {"command": command})


def test_custom_agent_gets_only_the_scoped_search_capability(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text(
        json.dumps(
            {
                "name": "custom",
                "tools": ["read"],
                "allowedTools": [],
                "resources": ["skill://skills/a/SKILL.md"],
            }
        ),
        encoding="utf-8",
    )
    prepared = projection.prepare_native_skill_projection(project)
    view = json.loads((agents / f"{prepared.agent('custom')}.json").read_text(encoding="utf-8"))
    assert view["tools"] == ["read", "@kirocrew-core/skill_search"]
    assert view["allowedTools"] == []
    assert "kirocrew-core" in view["mcpServers"]
    assert "autoApprove" not in view["mcpServers"]["kirocrew-core"]


def test_global_inheritance_preference_is_refreshed(native_tree):
    home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    projection.prepare_native_skill_projection(project)
    settings = home / "settings" / "cli.json"
    settings.parent.mkdir()
    settings.write_text('{"chat.disableInheritingDefaultResources":true}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    view = json.loads((agents / f"{prepared.agent('custom')}.json").read_text(encoding="utf-8"))
    assert view["resources"] == []


def test_projected_search_uses_the_managed_command_and_preserves_approval(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text(
        json.dumps(
            {
                "name": "custom",
                "resources": ["skill://skills/a/SKILL.md"],
                "mcpServers": {
                    "kirocrew-core": {"command": "other-server", "args": [], "autoApprove": []}
                },
            }
        ),
        encoding="utf-8",
    )
    prepared = projection.prepare_native_skill_projection(project)
    entry = prepared.specs["custom"]["mcpServers"]["kirocrew-core"]
    assert entry["command"] == "test-core" and entry["autoApprove"] == []


def test_explicit_search_exclusion_fails_only_that_agent(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text(
        json.dumps(
            {
                "name": "custom",
                "resources": ["skill://skills/a/SKILL.md"],
                "excludedTools": ["@kirocrew-core/skill_search"],
            }
        ),
        encoding="utf-8",
    )
    prepared = projection.prepare_native_skill_projection(project)
    with pytest.raises(ValueError, match="explicitly excluded"):
        prepared.agent("custom")


def test_unmapped_custom_agent_does_not_gain_tools_or_servers(native_tree):
    _home, agents, project = native_tree
    spec = {"name": "custom", "tools": ["read"], "excludedTools": ["@kirocrew-core/skill_search"]}
    (agents / "custom.json").write_text(json.dumps(spec), encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    view = prepared.specs["custom"]
    assert view["tools"] == ["read"]
    assert "mcpServers" not in view
    assert "custom" not in prepared.search_agents


@pytest.mark.parametrize(
    "field,value",
    [
        ("disabled", None),
        ("disabled", "false"),
        ("disabled", 0),
        ("disabledTools", None),
        ("disabledTools", "skill_search"),
        ("disabledTools", {}),
        ("disabledTools", [1]),
    ],
)
def test_invalid_core_restrictions_fail_only_the_affected_agent(
    native_tree, monkeypatch, field, value
):
    _home, agents, project = native_tree
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [
            SimpleNamespace(name=name, filename=f"{name}.json", scope="global")
            for name in ("custom", "healthy")
        ],
    )
    for name, core in (
        ("custom", {field: value}),
        ("healthy", {"disabled": False, "disabledTools": []}),
    ):
        (agents / f"{name}.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "resources": ["skill://skills/a/SKILL.md"],
                    "mcpServers": {"kirocrew-core": core},
                }
            ),
            encoding="utf-8",
        )
    prepared = projection.prepare_native_skill_projection(project)
    assert prepared is not None
    with pytest.raises(ValueError, match=field):
        prepared.agent("custom")
    assert (agents / f"{prepared.agent('healthy')}.json").exists()
    assert prepared.search_agents == {"healthy"}


@pytest.mark.parametrize(
    "original",
    [
        {},
        {"chat.disableInheritingDefaultResources": False},
        {"chat.disableInheritingDefaultResources": True},
        {"chat.disableInheritingDefaultResources": None},
        {"chat.disableInheritingDefaultResources": "false"},
        {"chat.disableInheritingDefaultResources": 1},
    ],
)
def test_rollback_restores_original_local_value_and_presence(native_tree, monkeypatch, original):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    settings = project / ".kiro/settings/cli.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps(original), encoding="utf-8")
    projection.prepare_native_skill_projection(project)
    projection.prepare_native_skill_projection(project)
    current = json.loads(settings.read_text(encoding="utf-8"))
    current["toolSearch.enabled"] = False
    settings.write_text(json.dumps(current), encoding="utf-8")
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "0")
    assert projection.prepare_native_skill_projection(project) is None
    restored = json.loads(settings.read_text(encoding="utf-8"))
    expected = {**original, "toolSearch.enabled": False}
    # JSON distinguishes numeric 1 from true, unlike Python dictionary equality.
    assert json.dumps(restored, sort_keys=True) == json.dumps(expected, sort_keys=True)
    # Repeated rollback does not recreate the overlay.
    before = settings.read_bytes()
    assert projection.prepare_native_skill_projection(project) is None
    assert settings.read_bytes() == before


@pytest.mark.parametrize("operator_value", [False, None, "deleted"])
def test_rollback_preserves_operator_changes(native_tree, monkeypatch, operator_value):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    projection.prepare_native_skill_projection(project)
    settings = project / ".kiro/settings/cli.json"
    current = json.loads(settings.read_text(encoding="utf-8"))
    key = "chat.disableInheritingDefaultResources"
    if operator_value == "deleted":
        current.pop(key)
        expected = {}
    else:
        current[key] = operator_value
        expected = {key: operator_value}
    settings.write_text(json.dumps(current), encoding="utf-8")
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "0")
    assert projection.prepare_native_skill_projection(project) is None
    assert json.loads(settings.read_text(encoding="utf-8")) == expected


def test_disabled_projection_does_not_enumerate_agents_or_create_settings(native_tree, monkeypatch):
    _home, agents, project = native_tree

    def unexpected(**kwargs):
        pytest.fail("disabled projection must not read authored agents")

    monkeypatch.setattr(projection, "list_agents", unexpected)
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "0")
    assert projection.prepare_native_skill_projection(project) is None
    assert list(agents.iterdir()) == []
    assert not (project / ".kiro").exists()


@pytest.mark.parametrize(
    "source,inherited,expected",
    [
        ("global", True, {}),
        ("local", True, {"chat.disableInheritingDefaultResources": False}),
        ("local", False, {"chat.disableInheritingDefaultResources": True}),
    ],
)
def test_rollback_of_legacy_owned_overlay(native_tree, monkeypatch, source, inherited, expected):
    _home, _agents, project = native_tree
    settings = project / ".kiro/settings/cli.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "kirocrew.skillDiscovery.inheritFiles": inherited,
                "kirocrew.skillDiscovery.inheritSource": source,
                "chat.disableInheritingDefaultResources": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "0")
    assert projection.prepare_native_skill_projection(project) is None
    assert json.loads(settings.read_text(encoding="utf-8")) == expected


def test_rollback_never_changes_unmanaged_settings(native_tree, monkeypatch):
    _home, _agents, project = native_tree
    settings = project / ".kiro/settings/cli.json"
    settings.parent.mkdir(parents=True)
    original = '{ "chat.disableInheritingDefaultResources": true, "other": 42 }'
    settings.write_text(original, encoding="utf-8")
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "0")
    assert projection.prepare_native_skill_projection(project) is None
    assert settings.read_text(encoding="utf-8") == original


def test_running_projection_keeps_its_mode_until_restart(native_tree, monkeypatch):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "0")
    refreshed = projection.prepare_native_skill_projection(project, enabled=True)
    assert refreshed.aliases == prepared.aliases
    assert projection.prepare_native_skill_projection(project) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_client_spawn_uses_authored_agent_only_when_rolled_back(
    native_tree, monkeypatch, enabled
):
    from kiro_crew.acp import client as client_module

    home, agents, project = native_tree
    monkeypatch.setenv("KIRO_HOME", str(home))
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "1" if enabled else "0")
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    monkeypatch.setattr(
        client_module, "_resolve_kiro_bin_for_spawn", AsyncMock(return_value="test-kiro")
    )
    monkeypatch.setattr(client_module, "ensure_agent_materialized", lambda agent: None)
    monkeypatch.setattr(client_module, "require_fresh_derived_spec", lambda *args: None)
    monkeypatch.setattr(client_module, "require_fork_governance", lambda *args: None)
    monkeypatch.setattr(
        client_module, "delegated_workspace_exposes_sealed_target", lambda path: None
    )

    class StopSpawn(Exception):
        pass

    captured = []

    def stop_at_sandbox(argv, **kwargs):
        captured.extend(argv)
        raise StopSpawn

    monkeypatch.setattr(client_module, "wrap_argv", stop_at_sandbox)
    client = client_module.AcpClient(work_dir=project, agent="custom", sandbox_mode="off")
    with pytest.raises(StopSpawn):
        await client._spawn()
    assert captured[:3] == ["test-kiro", "acp", "--agent"]
    if enabled:
        assert captured[3] == client._native_skill_projection.agent("custom")
    else:
        assert captured[3] == "custom"
        assert client._native_skill_projection is None


@pytest.mark.parametrize("value", ["false", 1, False, True])
@pytest.mark.parametrize("source", ["local", "global"])
def test_only_literal_true_suppresses_inherited_instruction_files(native_tree, value, source):
    home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    settings = (project / ".kiro" if source == "local" else home) / "settings" / "cli.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"chat.disableInheritingDefaultResources": value}), encoding="utf-8"
    )
    prepared = projection.prepare_native_skill_projection(project)
    resources = prepared.specs["custom"]["resources"]
    assert any("AGENTS.md" in item for item in resources) is (value is not True)
    assert any("steering" in item for item in resources) is (value is not True)


@pytest.mark.parametrize("value", ["false", 1, False, True])
def test_global_preference_refresh_uses_only_literal_true(native_tree, value):
    home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    settings = home / "settings" / "cli.json"
    settings.parent.mkdir()
    settings.write_text('{"chat.disableInheritingDefaultResources":true}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    assert first.specs["custom"]["resources"] == []
    settings.write_text(
        json.dumps({"chat.disableInheritingDefaultResources": value}), encoding="utf-8"
    )
    refreshed = projection.prepare_native_skill_projection(project)
    resources = refreshed.specs["custom"]["resources"]
    assert any("AGENTS.md" in item for item in resources) is (value is not True)
    assert any("steering" in item for item in resources) is (value is not True)


# ── Managed skill-view alias lifecycle ───────────────────────────────────────


def _alias_file(agents, prepared, name="custom"):
    return agents / f"{prepared.agent(name)}.json"


def _metadata_file(agents, prepared, name="custom"):
    return agents / projection._PROJECTION_METADATA_DIR_NAME / f"{prepared.agent(name)}.json"


def test_generated_view_keeps_lifecycle_ownership_out_of_the_agent_spec(native_tree):
    home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    alias_path = _alias_file(agents, prepared)
    view = json.loads(alias_path.read_text(encoding="utf-8"))
    assert not any(str(key).startswith("x-kirocrew-") for key in view)
    metadata = json.loads(_metadata_file(agents, prepared).read_text(encoding="utf-8"))
    assert metadata[projection._MANAGED_MARKER] == projection._MANAGED_MARKER_VALUE
    assert metadata[projection._MANAGED_CREW_HOME] == (home.parent / "crew").as_posix()
    assert metadata[projection._MANAGED_WORK_DIR] == project.absolute().as_posix()
    assert metadata[projection._MANAGED_AGENT] == "custom"
    assert metadata[projection._MANAGED_SOURCE] == (agents / "custom.json").as_posix()
    assert (
        metadata[projection._MANAGED_ALIAS_SHA256]
        == hashlib.sha256(alias_path.read_bytes()).hexdigest()
    )


def test_prune_keeps_alias_held_by_a_live_projection(native_tree, monkeypatch):
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom"}', encoding="utf-8")
    live = projection.prepare_native_skill_projection(project)
    live_alias = _alias_file(agents, live)
    source.unlink()
    (agents / "other.json").write_text('{"name":"other"}', encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="other", filename="other.json", scope="global")],
    )

    projection.prepare_native_skill_projection(project)
    assert live_alias.exists()


def test_prune_reclaims_alias_whose_agent_file_was_deleted(native_tree, monkeypatch):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    stale = _alias_file(agents, first)
    assert stale.exists()
    del first
    gc.collect()
    # The agent stops resolving, and a spawn for a DIFFERENT live agent runs.
    (agents / "custom.json").unlink()
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="other", filename="other.json", scope="global")],
    )
    (agents / "other.json").write_text('{"name":"other"}', encoding="utf-8")
    second = projection.prepare_native_skill_projection(project)
    assert not stale.exists()
    assert _alias_file(agents, second, "other").exists()


def test_every_reclaim_stage_admits_an_ordinary_local_alias(native_tree):
    """Each reclaim stage, asserted in order, so a failure names its own stage.

    The reclaim chain is a sequence of fail-safe gates: any one of them
    answering "uncertain" retains the alias, which is the right answer for a
    real hazard and an INVISIBLE no-op when the gate is simply wrong about an
    ordinary local file. The end-to-end prune tests above cannot tell those
    apart -- they assert only the final "file is gone", so every stage failing
    produces one identical message. This walks the same stages a stale alias
    passes through and asserts each separately, so a host where one gate
    misjudges an ordinary path reports WHICH gate rather than "not pruned".
    """
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    alias_path = _alias_file(agents, prepared)
    metadata_path = _metadata_file(agents, prepared)
    assert alias_path.exists(), "the alias was not published"
    assert metadata_path.exists(), "the ownership sidecar was not published"

    # Drop the projection (its lease must be released and reclaimed) and make
    # the recorded pair unregenerable, which is what marks the alias stale.
    del prepared
    gc.collect()
    source.unlink()

    assert projection._active_aliases() == set(), "a dropped projection still claims its aliases"
    assert not projection._alias_has_external_lease(
        agents, alias_path.stem
    ), "the dropped projection's lease still reads as live"

    raw = alias_path.read_bytes()
    managed = projection._managed_metadata_for_alias(agents, alias_path, raw)
    assert managed is not None, "the ownership sidecar was not admitted for these alias bytes"
    metadata, recorded_path, recorded_identity, _recorded_raw = managed
    assert recorded_path == metadata_path
    assert recorded_identity is not None
    assert metadata[projection._MANAGED_CREW_HOME] == projection.data_home().absolute().as_posix()
    assert projection._managed_metadata_path_is_safe(
        Path(metadata[projection._MANAGED_WORK_DIR])
    ), "the recorded work directory was judged unsafe to probe"
    assert projection._managed_metadata_path_is_safe(
        Path(metadata[projection._MANAGED_SOURCE])
    ), "the recorded authored source was judged unsafe to probe"
    assert projection._managed_alias_is_stale(metadata), "the unregenerable pair was not stale"

    info = alias_path.stat()
    assert projection._unlink_alias_if_unchanged(
        alias_path, (info.st_dev, info.st_ino)
    ), "the identity-checked unlink refused an unchanged alias"
    assert not alias_path.exists()


def test_a_held_lease_stays_readable_so_it_only_keeps_the_aliases_it_names(native_tree):
    """A held lease must not make every OTHER alias read as live.

    Windows file locks are mandatory: ``file_lock`` takes ``msvcrt.locking`` on
    byte 0, and reading that byte from any other handle -- including another
    handle in this same process -- fails with a lock violation. A lease that
    carried its lifetime lock on the record a reader must parse therefore turned
    every probe into an exception, which this function answers as "live", so one
    held lease kept EVERY alias and nothing was ever reclaimed. Pruning always
    runs while the current projection holds its own lease, so that made the whole
    reclaim a no-op on Windows while passing on POSIX, where locks are advisory.
    The record and the lock target are separate files for exactly this reason.
    """
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    mine = _alias_file(agents, prepared).stem

    lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
    records = list(lease_dir.glob(f"*{projection._PROJECTION_LEASE_RECORD_SUFFIX}"))
    assert len(records) == 1, "the live projection published no readable lease record"
    assert json.loads(records[0].read_bytes())["aliases"] == [mine]

    # The structural half, checkable on any platform: the lock target is a
    # SEPARATE file. A single-file lease cannot satisfy the mandatory-lock
    # constraint above, so its absence is the regression, not a style choice.
    holder = records[0].with_name(
        records[0].name[: -len(projection._PROJECTION_LEASE_RECORD_SUFFIX)]
        + projection._PROJECTION_LEASE_HOLDER_SUFFIX
    )
    assert holder.exists(), "the lease has no separate lock target to hold"
    assert holder != records[0]

    # The lease naming `mine` is HELD by this process for prepared's lifetime.
    assert projection._alias_has_external_lease(agents, mine) is True
    assert projection._alias_has_external_lease(agents, "kirocrew-skill-view-notnamedhere") is False
    assert prepared is not None


def test_a_lease_is_never_published_past_the_bound_its_reader_enforces(native_tree):
    """The writer must not publish a record the reader answers "live" to.

    The reader treats an over-bound record as uncertain, which means live, which
    means keep. A writer allowed past that bound could therefore publish a
    record that is unreclaimable by construction: a crash leaves it behind and
    every later probe reads it as held, disabling pruning permanently. Refusing
    publication falls back to authored native agents, which the next spawn
    retries. Both ends read one constant so they cannot drift apart.
    """
    _home, agents, _project = native_tree
    cap = projection._PROJECTION_LEASE_MAX_ALIASES
    at_cap = {f"kirocrew-skill-view-{n:024x}" for n in range(cap)}

    stack = projection._acquire_projection_lease(agents, at_cap)
    try:
        lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
        records = list(lease_dir.glob(f"*{projection._PROJECTION_LEASE_RECORD_SUFFIX}"))
        assert len(records) == 1
        assert len(records[0].read_bytes()) <= projection._PROJECTION_LEASE_MAX_BYTES
        # A record AT the cap is admitted, so it keeps only what it names.
        assert (
            projection._alias_has_external_lease(agents, "kirocrew-skill-view-notnamedhere")
            is False
        )
    finally:
        stack.close()

    with pytest.raises(OSError, match="exceed its reader's bound"):
        projection._acquire_projection_lease(agents, at_cap | {"kirocrew-skill-view-onemore"})


def _legacy_alias(agents, digest="0" * 24, name=None, resources=None, age_secs=3600.0, owned=True):
    """Write an alias the way a pre-lifecycle build did: no record of any kind.

    Backdated by default. A freshly written one is indistinguishable from a
    pre-lease publisher's in-flight spawn, which the reclaim deliberately spares.
    """
    stem = f"{projection.NATIVE_SKILL_ALIAS_PREFIX}{digest}"
    view = {"name": name if name is not None else stem, "resources": resources or ["file://R.md"]}
    if owned:
        # One positive mark the projection itself writes; shape alone must not
        # authorize an unlink.
        view["mcpServers"] = {"kirocrew-core": {"command": "test-core", "args": []}}
    path = agents / f"{stem}.json"
    path.write_text(json.dumps(view), encoding="utf-8")
    if age_secs:
        old = time.time() - age_secs
        os.utime(path, (old, old))
    return path


def test_prune_spares_a_legacy_alias_that_may_be_mid_publish(native_tree):
    """The one window the re-preparation contract does not cover.

    A publisher from a build predating the lease holds no lease, so between its
    write and kiro-cli reading `--agent` its alias is indistinguishable from
    backlog -- and that process will NOT re-prepare, because it already did, so
    deleting it is a failed spawn rather than an eviction. The age gate exists
    only to exclude that window; it is not a liveness proxy.
    """
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    fresh = _legacy_alias(agents, "f" * 24, age_secs=0)
    stale = _legacy_alias(agents, "e" * 24)

    assert projection.prepare_native_skill_projection(project) is not None

    assert fresh.exists(), "an alias that may be mid-publish was reclaimed"
    assert not stale.exists(), "the aged backlog was not reclaimed"


def test_prune_reclaims_the_backlog_left_by_builds_that_wrote_no_ownership(native_tree):
    """The accumulated aliases are the harm; ownership-only reclaim never reaches them.

    Shipped builds published aliases with neither a sidecar nor an in-spec
    marker, so a reclaim keyed on a recorded pair skips every one of them and
    only post-upgrade growth is bounded -- on the hosts that reported thousands
    of these, the whole per-turn tool-spec cost would persist untouched.
    """
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    legacy = _legacy_alias(agents, "a" * 24)
    assert legacy.exists()

    prepared = projection.prepare_native_skill_projection(project)

    assert prepared is not None
    assert not legacy.exists(), "the pre-upgrade backlog was left on disk"
    assert _alias_file(agents, prepared).exists(), "this run's own alias was reclaimed"


def test_prune_leaves_an_unattributable_alias_even_with_the_right_name(native_tree):
    """Shape is not provenance, and an unlink is not undoable.

    An operator's own agent could in principle carry this name, so one positive
    mark the projection itself writes is required as well. A view with neither
    Crew's managed server entry nor this host's absolute steering resource is
    left alone -- a smaller reclaim than the name shape would allow, and the
    right side to err on when the alternative is deleting somebody else's file.
    """
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    unattributable = _legacy_alias(agents, "9" * 24, owned=False)
    attributable = _legacy_alias(agents, "8" * 24)

    assert projection.prepare_native_skill_projection(project) is not None

    assert unattributable.exists(), "an alias Crew cannot claim was deleted on its name alone"
    assert not attributable.exists(), "an alias carrying Crew's own mark was left behind"


def test_prune_leaves_a_prefixed_file_that_is_not_a_projected_view(native_tree):
    """A name is not authorization. Only a view this module could have written goes."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    # Right prefix, wrong digest shape.
    short = _legacy_alias(agents, "b" * 12)
    # Right shape, but renamed -- not a projection, which always self-renames.
    renamed = _legacy_alias(agents, "c" * 24, name="someone-elses-agent")
    # Right shape, but it still carries skill resources, which a view never does.
    unstripped = _legacy_alias(agents, "d" * 24, resources=["skill://cat/s/SKILL.md"])

    assert projection.prepare_native_skill_projection(project) is not None

    assert short.exists()
    assert renamed.exists()
    assert unstripped.exists()


def test_prune_keeps_a_legacy_alias_a_held_lease_still_names(native_tree):
    """The lease gate governs the legacy path too; it is checked before ownership."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    legacy = _legacy_alias(agents, "e" * 24)
    external = projection._acquire_projection_lease(agents, {legacy.stem})
    try:
        assert projection.prepare_native_skill_projection(project) is not None
        assert legacy.exists(), "a held lease did not protect the legacy alias"
    finally:
        external.close()
    assert projection.prepare_native_skill_projection(project) is not None
    assert not legacy.exists()


def test_prune_caps_reclaims_per_run_so_the_backlog_drains_over_spawns(native_tree, monkeypatch):
    """A multi-thousand backlog must not be drained under one held lock.

    The prune runs while the publication lock is held, and that lock's own
    acquisition ceiling is 2s -- so a single sweep over the whole accumulated
    backlog would make a concurrent spawn fail to acquire and fall back to
    authored agents. The backlog is bounded and shrinking, so a per-run cap
    reclaims it just as completely across successive spawns.
    """
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    monkeypatch.setattr(projection, "_PRUNE_MAX_RECLAIMS_PER_RUN", 3)
    backlog = [_legacy_alias(agents, f"{n:024x}") for n in range(8)]

    assert projection.prepare_native_skill_projection(project) is not None
    assert sum(1 for p in backlog if p.exists()) == 5, "the cap did not bound one run"

    for _ in range(3):
        assert projection.prepare_native_skill_projection(project) is not None
    assert not any(p.exists() for p in backlog), "successive runs did not drain the backlog"


def test_prune_reclaims_alias_whose_work_dir_was_deleted(native_tree, monkeypatch, tmp_path):
    _home, agents, project = native_tree
    gone = tmp_path / "gone-workdir"
    gone.mkdir()
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(gone)
    stale = _alias_file(agents, first)
    assert stale.exists()
    del first
    gc.collect()
    shutil.rmtree(gone)
    # A spawn for a still-live work_dir triggers the prune of the dead one.
    projection.prepare_native_skill_projection(project)
    assert not stale.exists()


def test_prune_keeps_alias_replaced_after_stale_classification(native_tree, monkeypatch):
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    stale = _alias_file(agents, first)
    replacement = stale.read_text(encoding="utf-8")
    del first
    gc.collect()
    source.unlink()

    (agents / "other.json").write_text('{"name":"other"}', encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="other", filename="other.json", scope="global")],
    )
    original_is_stale = projection._managed_alias_is_stale
    replacement_identity = []

    def replace_after_classification(spec):
        result = original_is_stale(spec)
        if result and spec.get(projection._MANAGED_AGENT) == "custom":
            # Model another gateway atomically recreating the source and alias
            # after this gateway classified the old alias as stale.
            source.write_text('{"name":"custom"}', encoding="utf-8")
            projection.atomic_write(stale, replacement, restrict_to_owner=True)
            current = stale.stat()
            replacement_identity.append((current.st_dev, current.st_ino))
        return result

    monkeypatch.setattr(projection, "_managed_alias_is_stale", replace_after_classification)
    projection.prepare_native_skill_projection(project)

    assert replacement_identity
    current = stale.stat()
    assert (current.st_dev, current.st_ino) == replacement_identity[0]


def test_projection_lock_covers_alias_publication_and_pruning(native_tree, monkeypatch):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    lock_held = 0
    real_file_lock = projection.platform_compat.file_lock
    real_atomic_write = projection.atomic_write
    real_prune = projection._prune_stale_managed_aliases

    @contextmanager
    def observed_file_lock(fd, **kwargs):
        nonlocal lock_held
        is_alias_lock = kwargs == {
            "exclusive": True,
            "timeout": projection._PROJECTION_LOCK_TIMEOUT_SECS,
        }
        with real_file_lock(fd, **kwargs):
            if is_alias_lock:
                lock_held += 1
            try:
                yield
            finally:
                if is_alias_lock:
                    lock_held -= 1

    def observed_atomic_write(path, *args, **kwargs):
        if path.parent == agents and path.stem.startswith(projection.NATIVE_SKILL_ALIAS_PREFIX):
            assert lock_held
        return real_atomic_write(path, *args, **kwargs)

    def observed_prune(*args, **kwargs):
        assert lock_held
        return real_prune(*args, **kwargs)

    monkeypatch.setattr(projection.platform_compat, "file_lock", observed_file_lock)
    monkeypatch.setattr(projection, "atomic_write", observed_atomic_write)
    monkeypatch.setattr(projection, "_prune_stale_managed_aliases", observed_prune)

    prepared = projection.prepare_native_skill_projection(project)
    assert prepared is not None
    assert _alias_file(agents, prepared).exists()


@requires_symlinks
def test_projection_lock_refuses_planted_symlink(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    target = agents / "unrelated.lock"
    target.write_text("unrelated", encoding="utf-8")
    (agents / projection._PROJECTION_LOCK_NAME).symlink_to(target)

    prepared = projection.prepare_native_skill_projection(project)

    assert prepared is None
    assert target.read_text(encoding="utf-8") == "unrelated"
    assert not list(agents.glob(f"{projection.NATIVE_SKILL_ALIAS_PREFIX}*.json"))
    assert not (project / ".kiro/settings/cli.json").exists()


@pytest.mark.parametrize("failure_at", ["open", "acquire"])
def test_projection_lock_failure_keeps_aliases_and_preserves_startup(
    native_tree, monkeypatch, failure_at
):
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    stale = _alias_file(agents, first)
    settings = project / ".kiro/settings/cli.json"
    assert json.loads(settings.read_text(encoding="utf-8"))[projection._INHERIT_SETTING] is True
    settings_before = settings.read_bytes()
    del first
    gc.collect()
    source.unlink()

    (agents / "other.json").write_text('{"name":"other"}', encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="other", filename="other.json", scope="global")],
    )

    if failure_at == "open":

        def lock_failure(_path):
            raise OSError("test lock unavailable")

        monkeypatch.setattr(projection.platform_compat, "open_lock_file", lock_failure)
    else:

        @contextmanager
        def lock_failure(_fd, **_kwargs):
            raise OSError("test lock unavailable")
            yield

        monkeypatch.setattr(projection.platform_compat, "file_lock", lock_failure)
    prepared = projection.prepare_native_skill_projection(project)

    assert prepared is None
    assert list(agents.glob(f"{projection.NATIVE_SKILL_ALIAS_PREFIX}*.json")) == [stale]
    assert settings.read_bytes() == settings_before


def test_prune_keeps_a_live_pairs_alias(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    alias = _alias_file(agents, first)
    # A second spawn for the same live pair must not remove the shared alias.
    projection.prepare_native_skill_projection(project)
    assert alias.exists()


def test_prune_never_touches_another_homes_alias(native_tree):
    _home, agents, project = native_tree
    # A foreign instance's alias: correct marker, DIFFERENT home, dead pair.
    foreign = agents / f"{projection.NATIVE_SKILL_ALIAS_PREFIX}foreignaliasfilename01.json"
    foreign.write_text(
        json.dumps(
            {
                "name": foreign.stem,
                projection._MANAGED_MARKER: projection._MANAGED_MARKER_VALUE,
                projection._MANAGED_CREW_HOME: "/some/other/crew/home",
                projection._MANAGED_WORK_DIR: "/nonexistent/workdir",
                projection._MANAGED_AGENT: "ghost",
                projection._MANAGED_SOURCE: "/nonexistent/workdir/.kiro/agents/ghost.json",
            }
        ),
        encoding="utf-8",
    )
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    projection.prepare_native_skill_projection(project)
    assert foreign.exists()


def test_prune_keeps_other_crew_homes_alias_after_its_work_dir_disappears(
    native_tree, monkeypatch, tmp_path
):
    _home, agents, project = native_tree
    first_crew_home = tmp_path / "crew-a"
    monkeypatch.setattr(projection, "data_home", lambda: first_crew_home)
    gone = tmp_path / "gone-for-first-home"
    gone.mkdir()
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(gone)
    foreign_after_switch = _alias_file(agents, first)
    shutil.rmtree(gone)

    monkeypatch.setattr(projection, "data_home", lambda: tmp_path / "crew-b")
    projection.prepare_native_skill_projection(project)
    assert foreign_after_switch.exists()


def test_prune_uses_the_recorded_source_when_filename_differs_from_agent(
    native_tree, monkeypatch, tmp_path
):
    _home, agents, first_project = native_tree
    source = agents / "authored-filename.json"
    source.write_text('{"name":"custom"}', encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [
            SimpleNamespace(name="custom", filename="authored-filename.json", scope="global")
        ],
    )
    first = projection.prepare_native_skill_projection(first_project)
    live_alias = _alias_file(agents, first)

    second_project = tmp_path / "second-project"
    second_project.mkdir()
    (agents / "other.json").write_text('{"name":"other"}', encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="other", filename="other.json", scope="global")],
    )
    projection.prepare_native_skill_projection(second_project)
    assert live_alias.exists()


def test_prune_keeps_oversized_alias_and_continues_startup(native_tree, monkeypatch):
    _home, agents, project = native_tree
    oversized = agents / f"{projection.NATIVE_SKILL_ALIAS_PREFIX}oversizedfilename0001.json"
    oversized.write_text("oversized", encoding="utf-8")
    original_read = projection.safe_read_file_bytes

    def read_with_oversized_failure(path):
        if path == str(oversized):
            raise FileTooLargeError("test oversized alias")
        return original_read(path)

    monkeypatch.setattr(projection, "safe_read_file_bytes", read_with_oversized_failure)
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    assert prepared is not None
    assert prepared.agent("custom")
    assert oversized.exists()


def test_prune_leaves_unmarked_and_malformed_prefix_files_alone(native_tree):
    _home, agents, project = native_tree
    # A prefix-named file with NO marker (a scanner-hostile squatter) and a
    # prefix-named file with unparseable content: neither is ours to delete.
    unmarked = agents / f"{projection.NATIVE_SKILL_ALIAS_PREFIX}unmarkedfilename000001.json"
    unmarked.write_text('{"name":"squatter"}', encoding="utf-8")
    malformed = agents / f"{projection.NATIVE_SKILL_ALIAS_PREFIX}malformedfilename00001.json"
    malformed.write_text("{ not json", encoding="utf-8")
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    projection.prepare_native_skill_projection(project)
    assert unmarked.exists()
    assert malformed.exists()


@pytest.mark.parametrize("remote_verdict", [True, None])
def test_prune_refuses_windows_remote_metadata_before_any_path_probe(
    native_tree, monkeypatch, remote_verdict
):
    _home, agents, project = native_tree
    monkeypatch.setattr(projection.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        projection.platform_compat,
        "path_volume_is_remote",
        lambda _path: remote_verdict,
    )

    def unexpected_probe(_path):
        pytest.fail("remote managed metadata must not reach Path.is_dir")

    monkeypatch.setattr(type(project), "is_dir", unexpected_probe)
    spec = {
        projection._MANAGED_WORK_DIR: r"\\attacker.example\share\project",
        projection._MANAGED_AGENT: "custom",
        projection._MANAGED_SOURCE: str(agents / "custom.json"),
    }

    assert projection._managed_alias_is_stale(spec) is False


def test_prune_refuses_windows_linked_ancestor_before_directory_probe(native_tree, monkeypatch):
    _home, agents, project = native_tree
    monkeypatch.setattr(projection.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        projection.platform_compat,
        "path_volume_is_remote",
        lambda _path: False,
    )
    monkeypatch.setattr(
        projection.platform_compat,
        "first_linked_ancestor",
        lambda _path: str(project.parent),
    )

    def unexpected_probe(_path):
        pytest.fail("linked managed metadata must not reach Path.is_dir")

    monkeypatch.setattr(type(project), "is_dir", unexpected_probe)
    spec = {
        projection._MANAGED_WORK_DIR: str(project),
        projection._MANAGED_AGENT: "custom",
        projection._MANAGED_SOURCE: str(agents / "custom.json"),
    }

    assert projection._managed_alias_is_stale(spec) is False


def test_alias_deletion_is_retained_without_identity_safe_unlink(tmp_path, monkeypatch):
    alias = tmp_path / "alias.json"
    alias.write_text("managed", encoding="utf-8")
    identity = alias.stat().st_dev, alias.stat().st_ino
    monkeypatch.setattr(projection.pinned_fs, "supports_pinned_walk", lambda: False)
    monkeypatch.setattr(projection.platform_compat, "IS_WINDOWS", False)

    def unexpected_unlink(_path):
        pytest.fail("a platform without identity-safe unlink must retain the alias")

    monkeypatch.setattr(type(alias), "unlink", unexpected_unlink)

    assert projection._unlink_alias_if_unchanged(alias, identity) is False
    assert alias.read_text(encoding="utf-8") == "managed"


def test_lock_failure_never_overwrites_a_newer_settings_generation(native_tree, monkeypatch):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    assert projection.prepare_native_skill_projection(project) is not None
    settings = project / ".kiro/settings/cli.json"
    newer = []

    def concurrent_update_then_failure(_directory):
        current = json.loads(settings.read_text(encoding="utf-8"))
        current["toolSearch.enabled"] = False
        settings.write_text(json.dumps(current, sort_keys=True), encoding="utf-8")
        newer.append(settings.read_bytes())
        raise OSError("test lock timeout after concurrent settings update")

    monkeypatch.setattr(projection, "_projection_alias_lock", concurrent_update_then_failure)

    assert projection.prepare_native_skill_projection(project) is None
    assert newer and settings.read_bytes() == newer[0]


def test_prune_keeps_alias_while_an_external_projection_lease_is_locked(native_tree, monkeypatch):
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    stale = _alias_file(agents, first)
    external = projection._acquire_projection_lease(agents, {stale.stem})
    del first
    gc.collect()
    source.unlink()

    (agents / "other.json").write_text('{"name":"other"}', encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="other", filename="other.json", scope="global")],
    )
    live = projection.prepare_native_skill_projection(project)
    assert stale.exists()

    external.close()
    projection.prepare_native_skill_projection(project)
    assert live is not None
    assert not stale.exists()


def test_projection_finalizer_removes_its_lease_sidecar(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
    assert len(list(lease_dir.glob("*.json"))) == 1

    del prepared
    gc.collect()

    assert list(lease_dir.glob("*.json")) == []


def test_lease_scan_reclaims_valid_unlocked_crash_residue(native_tree):
    _home, agents, _project = native_tree
    lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
    lease_dir.mkdir()
    stale = lease_dir / "crashed.json"
    projection.atomic_write(
        stale,
        json.dumps({"aliases": ["kirocrew-skill-view-stale"]}),
        restrict_to_owner=True,
    )

    assert not projection._alias_has_external_lease(agents, "kirocrew-skill-view-stale")
    assert not stale.exists()


def test_windows_alias_unlink_rechecks_identity_under_publication_lock(tmp_path, monkeypatch):
    alias = tmp_path / "alias.json"
    alias.write_text("managed", encoding="utf-8")
    identity = alias.stat().st_dev, alias.stat().st_ino
    monkeypatch.setattr(projection.pinned_fs, "supports_pinned_walk", lambda: False)
    monkeypatch.setattr(projection.platform_compat, "IS_WINDOWS", True)

    assert projection._unlink_alias_if_unchanged(alias, identity)
    assert not alias.exists()


def test_projection_and_provider_serialize_workspace_settings_writes(native_tree, monkeypatch):
    from kiro_crew.providers import acp as provider_acp

    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    provider_write_reached = threading.Event()
    provider_done = threading.Event()
    provider_errors = []
    real_provider_atomic_write = provider_acp.atomic_write
    real_projection_atomic_write = projection.atomic_write
    update_thread = None
    started = False

    def observed_provider_atomic_write(*args, **kwargs):
        provider_write_reached.set()
        return real_provider_atomic_write(*args, **kwargs)

    def update_tool_search():
        try:
            provider_acp._write_tool_search_overlay(project, True, 17, 4096)
        except BaseException as exc:
            provider_errors.append(exc)
        finally:
            provider_done.set()

    def start_concurrent_writer(path, *args, **kwargs):
        nonlocal update_thread, started
        if (
            not started
            and path.parent == agents
            and path.stem.startswith(projection.NATIVE_SKILL_ALIAS_PREFIX)
        ):
            started = True
            update_thread = threading.Thread(target=update_tool_search)
            update_thread.start()
            assert not provider_write_reached.wait(
                0.1
            ), "the provider reached its cli.json commit while projection held the settings lock"
        return real_projection_atomic_write(path, *args, **kwargs)

    monkeypatch.setattr(provider_acp, "atomic_write", observed_provider_atomic_write)
    monkeypatch.setattr(projection, "atomic_write", start_concurrent_writer)

    prepared = projection.prepare_native_skill_projection(project)
    assert prepared is not None
    assert update_thread is not None
    update_thread.join(timeout=3.0)
    assert not update_thread.is_alive()
    assert provider_done.is_set() and not provider_errors
    settings = json.loads((project / ".kiro/settings/cli.json").read_text(encoding="utf-8"))
    assert settings[projection._MANAGED_SETTING] is True
    assert settings["toolSearch.enabled"] is True
    assert settings["toolSearch.minPct"] == 17
    assert settings["toolSearch.minTokens"] == 4096


@requires_symlinks
def test_workspace_settings_lock_refuses_a_planted_symlink(tmp_path):
    from kiro_crew.workspace_cli_settings import (
        CLI_SETTINGS_LOCK_NAME,
        workspace_cli_settings_lock,
    )

    project = tmp_path / "project"
    settings = project / ".kiro" / "settings"
    settings.mkdir(parents=True)
    target = tmp_path / "unrelated.lock"
    target.write_text("unrelated", encoding="utf-8")
    (settings / CLI_SETTINGS_LOCK_NAME).symlink_to(target)

    with pytest.raises(OSError, match="symlink or junction"):
        with workspace_cli_settings_lock(project):
            pytest.fail("a planted settings lock must never be acquired")

    assert target.read_text(encoding="utf-8") == "unrelated"


def test_workspace_settings_lock_failure_publishes_no_alias(native_tree, monkeypatch):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")

    @contextmanager
    def unavailable(_work_dir):
        raise OSError("test settings lock unavailable")
        yield

    monkeypatch.setattr(projection, "workspace_cli_settings_lock", unavailable)

    assert projection.prepare_native_skill_projection(project) is None
    assert not list(agents.glob(f"{projection.NATIVE_SKILL_ALIAS_PREFIX}*.json"))
    assert not (project / ".kiro/settings/cli.json").exists()
