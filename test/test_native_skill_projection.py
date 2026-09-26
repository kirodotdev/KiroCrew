"""Native metadata cannot grow with the catalog behind an authored mapping."""

from __future__ import annotations

import errno
import gc
import hashlib
import itertools
import json
import os
import shutil
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conftest import requires_symlinks
from kiro_crew.acp import skill_projection as projection
from kiro_crew.agent_spec_format import iter_agent_spec_files
from kiro_crew.hooks import FileTooLargeError


def _hold_the_prune_clock(monkeypatch):
    """Freeze ``time.monotonic`` for a test that asserts how many aliases one
    prune walk RECLAIMS rather than how the time budget cuts it short.

    Those counts only hold if the walk finishes. The budget is real wall-clock
    time, so on a slow CI runner it ends a nine-candidate walk one reclaim early
    and the count assertion reads that as a wrong cap. A clock that never
    advances leaves the budget itself untouched and simply never spends it;
    the budget's own semantics are pinned by :func:`_prune_walk`, which drives
    this same clock one tick per candidate, and by the zero-budget tests.
    """
    monkeypatch.setattr(projection.time, "monotonic", lambda: 0.0)


def _alias_is_live(directory, alias):
    """Test-only single-alias liveness over the one product lease scan.

    The product ``_alias_has_external_lease`` wrapper was removed -- it had zero
    product callers, so retaining it as a "for direct callers" convenience was
    misleading. The prune consults ``_scan_projection_leases`` once and tests
    membership in its returned ``live_aliases`` set; these tests do the same via
    this helper so they exercise the real scan (including its crash-residue
    reclamation and uncertainty accounting) rather than a shim that outlived its
    only users. Uncertainty is live, matching the fail-closed prune contract.
    """
    live_aliases, uncertain = projection._scan_projection_leases(directory)
    if uncertain:
        return True
    return alias in live_aliases


@pytest.fixture
def cyclic_gc_quiesced():
    """Keep a cyclic-GC pass (and any finalizer it runs) out of a deep JSON parse.

    A recursion-limit fixture drives the parser to the bottom of the stack; a
    collection firing there could run an inherited finalizer with no stack
    left. Drain first, disable for the parse, then restore and drain again.
    """
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()
        gc.collect()


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
    assert "x-kirocrew-work-dir" not in metadata
    assert metadata[projection._MANAGED_AGENT] == "custom"
    assert metadata[projection._MANAGED_SOURCE] == (agents / "custom.json").as_posix()
    assert (
        metadata[projection._MANAGED_ALIAS_SHA256]
        == hashlib.sha256(alias_path.read_bytes()).hexdigest()
    )


def test_current_publication_recovers_from_deeply_nested_sidecar(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    metadata_path = _metadata_file(agents, first)
    metadata_path.write_text("[" * 20000 + "]" * 20000, encoding="utf-8")

    prepared = projection.prepare_native_skill_projection(project)

    assert prepared is not None
    assert prepared.agent("custom") == first.agent("custom")
    alias_path = _alias_file(agents, prepared)
    assert json.loads(alias_path.read_text(encoding="utf-8")) == prepared.specs["custom"]
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert (
        metadata[projection._MANAGED_ALIAS_SHA256]
        == hashlib.sha256(alias_path.read_bytes()).hexdigest()
    )


def test_legacy_alias_rejects_deeply_nested_json(native_tree):
    _home, agents, _project = native_tree
    path = agents / f"{projection.NATIVE_SKILL_ALIAS_PREFIX}{'a' * 24}.json"

    assert not projection._is_legacy_projected_view(path, b"[" * 20000 + b"]" * 20000)


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

    # Drop the projection: its lease must be released and reclaimed, which is
    # what makes the alias unused. Nothing about the authored source changes.
    del prepared
    gc.collect()

    assert projection._active_aliases() == set(), "a dropped projection still claims its aliases"
    assert not _alias_is_live(
        agents, alias_path.stem
    ), "the dropped projection's lease still reads as live"

    raw = alias_path.read_bytes()
    managed = projection._managed_metadata_for_alias(agents, alias_path, raw)
    assert managed is not None, "the ownership sidecar was not admitted for these alias bytes"
    metadata, recorded_path, recorded_identity, _recorded_raw = managed
    assert recorded_path == metadata_path
    assert recorded_identity is not None
    assert metadata[projection._MANAGED_CREW_HOME] == projection.data_home().absolute().as_posix()

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
    assert _alias_is_live(agents, mine) is True
    assert _alias_is_live(agents, "kirocrew-skill-view-notnamedhere") is False
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
        assert _alias_is_live(agents, "kirocrew-skill-view-notnamedhere") is False
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


def test_prune_spares_a_legacy_alias_that_may_be_mid_publish(native_tree, monkeypatch):
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
    # Both candidates must be classified, so the walk must not end on the clock.
    _hold_the_prune_clock(monkeypatch)

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


def test_prune_leaves_an_unattributable_alias_even_with_the_right_name(native_tree, monkeypatch):
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
    # Both candidates must be classified, so the walk must not end on the clock.
    _hold_the_prune_clock(monkeypatch)

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
    monkeypatch.setattr(projection, "_PROJECTION_PRUNE_WORK_LIMIT", 4, raising=False)
    monkeypatch.setattr(projection, "_PRUNE_MAX_RECLAIMS_PER_RUN", 3)
    # The survivor counts below assume each walk ends on the cap, not the clock.
    _hold_the_prune_clock(monkeypatch)
    backlog = [_legacy_alias(agents, f"{n:024x}") for n in range(8)]

    assert projection.prepare_native_skill_projection(project) is not None
    assert sum(1 for p in backlog if p.exists()) == 4, "the cap did not bound one run"

    for _ in range(3):
        assert projection.prepare_native_skill_projection(project) is not None
    assert not any(p.exists() for p in backlog), "successive runs did not drain the backlog"


def _crew_home_id():
    """The owner id the spawn path passes, for a test that drives the prune itself."""
    return projection.data_home().absolute().as_posix()


def _prune_walk(monkeypatch, classifications, blocked=frozenset()):
    """Record each candidate the walk classifies, and end it after *classifications*.

    The classification step runs once per candidate the budgeted loop examines,
    so the names it sees ARE the candidates this call examined, and answering
    "not reclaimed" for *blocked* pins an entry unreclaimable without inventing a
    live projection.

    Time is frozen and advanced ONLY by that probe, so one tick means one
    candidate. A clock that ticked per READ cannot express this: the
    classification path reads the clock too, so the budget would be spent by
    reads rather than by work. Nothing sleeps, and the count is exact.
    """
    seen = []
    real = projection._reclaim_prune_candidate
    if classifications == 0:
        monkeypatch.setattr(projection, "_PRUNE_MAX_SECONDS_PER_RUN", 0.0)
    budget = projection._PRUNE_MAX_SECONDS_PER_RUN
    ticks = [0]

    def clock():
        # Counted, never accumulated: summing budget/N N times lands either side of
        # the budget by one float ulp, which is one candidate either way.
        return 0.0 if not classifications else budget * ticks[0] / classifications

    def probe(directory, path, crew_home_id):
        seen.append(path.stem)
        ticks[0] += 1
        return False if path.stem in blocked else real(directory, path, crew_home_id)

    monkeypatch.setattr(projection, "_reclaim_prune_candidate", probe)
    monkeypatch.setattr(projection.time, "monotonic", clock)
    return seen


def test_a_backlog_cannot_stretch_the_locked_section_past_this_calls_budget(
    native_tree, monkeypatch
):
    """The prune holds the publication lock, whose acquisition ceiling is fixed.

    So the section it holds has to be bounded by something other than the size of
    the pile it is draining. The reclaim cap is not that bound: a candidate that
    is kept, active or leased costs a full classification and never increments
    it, so a backlog whose entries are ALL unreclaimable costs the full walk and
    buys no reclaim at all -- the case this budget exists for.
    """
    _home, agents, _project = native_tree
    backlog = [_legacy_alias(agents, f"{n:024x}") for n in range(24)]
    _prune_walk(monkeypatch, 8)

    projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set())

    survivors = sum(1 for p in backlog if p.exists())
    assert survivors == len(backlog) - 8, "the walk did not stop on its own time budget"


def test_the_walk_stops_on_its_deadline_without_reclaiming_anything(native_tree, monkeypatch):
    """The deadline is the guarantee; the candidate cap only makes cost predictable.

    Per-candidate cost is not flat -- classification reads each alias and its
    sidecar -- so a count alone cannot bound wall-clock time. A spent
    budget is proved by a zero-length walk, which needs no clock and no sleep.
    """
    _home, agents, _project = native_tree
    backlog = [_legacy_alias(agents, f"{n:024x}") for n in range(6)]
    seen = _prune_walk(monkeypatch, 0)

    projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set())

    assert seen == [], "the walk classified a candidate after its deadline had passed"
    assert all(p.exists() for p in backlog), "a deletion happened past the deadline"


def test_the_walk_starts_where_the_rotation_points(native_tree, monkeypatch):
    """A fixed start examines one prefix forever; the offset is what moves it.

    Driven through the prune itself rather than a spawn: publishing rewrites an
    alias and its sidecar, and a directory whose entries have been rewritten is
    free to enumerate them in a different order on another platform. Asserting a
    position across that would assert the filesystem, not the rotation.
    """
    _home, agents, _project = native_tree
    backlog = [_legacy_alias(agents, f"{n:024x}") for n in range(12)]
    order = list(agents.glob(f"{projection.NATIVE_SKILL_ALIAS_PREFIX}*.json"))
    assert len(order) == len(backlog), "the walk sees entries this test did not seed"
    monkeypatch.setattr(projection, "_prune_start_offset", lambda count: count - 1)
    seen = _prune_walk(monkeypatch, 1, blocked={p.stem for p in order})

    projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set())

    assert seen == [order[-1].stem], "the walk ignored the rotation and took the prefix"


def test_an_unreclaimable_prefix_cannot_hide_the_backlog_behind_it(native_tree, monkeypatch):
    """Rotation has to REACH every entry across calls, not merely differ per call.

    With a bounded walk and a fixed start, entries that are kept, active or leased
    at the front of the directory's own order hide everything behind them for
    good: the walk spends its whole budget on them every single call.
    """
    _home, agents, _project = native_tree
    backlog = [_legacy_alias(agents, f"{n:024x}") for n in range(12)]
    order = list(agents.glob(f"{projection.NATIVE_SKILL_ALIAS_PREFIX}*.json"))
    assert len(order) == len(backlog), "the walk sees entries this test did not seed"
    pinned = order[:4]
    reclaimable = order[4:]
    assert reclaimable, "no entry sits behind the pinned prefix"

    turns = iter(range(0, 64, 4))

    def rotate(count):
        return next(turns, 0) % count if count else 0

    monkeypatch.setattr(projection, "_prune_start_offset", rotate)
    _prune_walk(monkeypatch, 4, blocked={p.stem for p in pinned})

    for _ in range(8):
        projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set())

    assert all(p.exists() for p in pinned), "a pinned entry was reclaimed"
    assert not any(p.exists() for p in reclaimable), (
        "entries behind the unreclaimable prefix were never reached, so the bounded "
        "walk disabled its own cleanup"
    )


def test_the_rotation_offset_is_not_a_constant_and_stays_in_range(native_tree):
    """The seam's own contract: in range, and genuinely moving."""
    assert projection._prune_start_offset(0) == 0
    assert projection._prune_start_offset(1) == 0
    drawn = {projection._prune_start_offset(64) for _ in range(256)}
    assert drawn, "the seam returned nothing"
    assert all(0 <= offset < 64 for offset in drawn), "an offset fell outside the list"
    assert len(drawn) > 1, "a constant offset walks one prefix forever"


def test_every_work_dir_shares_one_alias_per_agent(native_tree, tmp_path):
    """The alias is named by the view, not by where it is spawned.

    Every subagent and cron run spawns in its own directory. Keying the alias on
    that directory wrote a full copy of every agent per run, and kiro-cli's
    subagent tool lists every copy it finds in the agents directory.
    """
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    runs = []
    for n in range(5):
        run_dir = tmp_path / f"subagent_{n:08x}"
        run_dir.mkdir()
        runs.append(projection.prepare_native_skill_projection(run_dir))
    live = projection.prepare_native_skill_projection(project)
    assert {run.agent("custom") for run in runs} == {live.agent("custom")}
    views = list(agents.glob(f"{projection.NATIVE_SKILL_ALIAS_PREFIX}*.json"))
    assert views == [_alias_file(agents, live)]


def test_two_crew_homes_never_share_an_alias(native_tree, monkeypatch, tmp_path):
    """Identical views from two data homes must not contend for one file.

    A shared file would flip its ownership sidecar to whichever home spawned
    last, so each home's cleanup would misjudge the other's live view.
    """
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    monkeypatch.setattr(projection, "data_home", lambda: tmp_path / "crew-a")
    first = projection.prepare_native_skill_projection(project)
    monkeypatch.setattr(projection, "data_home", lambda: tmp_path / "crew-b")
    second = projection.prepare_native_skill_projection(project)
    assert first.agent("custom") != second.agent("custom")
    assert _alias_file(agents, first).exists()
    assert _alias_file(agents, second).exists()


def test_republishing_identical_view_keeps_the_file_in_place(native_tree, tmp_path):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    before = os.stat(_alias_file(agents, first))
    other = tmp_path / "other"
    other.mkdir()
    second = projection.prepare_native_skill_projection(other)
    after = os.stat(_alias_file(agents, second))
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


def test_editing_an_agent_publishes_a_new_alias_and_reclaims_the_old(native_tree):
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom","description":"v1"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    stale = _alias_file(agents, first)
    del first
    gc.collect()
    source.write_text('{"name":"custom","description":"v2"}', encoding="utf-8")
    second = projection.prepare_native_skill_projection(project)
    assert _alias_file(agents, second) != stale
    assert not stale.exists()


def test_prune_reclaims_unused_alias_whose_work_dir_still_exists(native_tree, tmp_path):
    """A per-run work directory outlives its run; its aliases must not.

    Every subagent and cron run spawns in its own ``workspace_root()/<key>``
    directory, and nothing removes that directory when the run ends. Keying the
    reclaim on the directory's existence therefore keeps one alias per agent for
    every run ever spawned, until the directory holds enough files that kiro-cli
    fails with EMFILE on every spawn. Liveness is the lease, not the directory.
    """
    _home, agents, project = native_tree
    run_dir = tmp_path / "subagent_deadbeef"
    run_dir.mkdir()
    source = agents / "custom.json"
    source.write_text('{"name":"custom","description":"old"}', encoding="utf-8")
    ended = projection.prepare_native_skill_projection(run_dir)
    alias = _alias_file(agents, ended)
    metadata = _metadata_file(agents, ended)
    del ended
    gc.collect()
    assert run_dir.is_dir(), "the run directory is deliberately left in place"

    source.write_text('{"name":"custom","description":"new"}', encoding="utf-8")
    projection.prepare_native_skill_projection(project)
    assert not alias.exists(), "an unused alias survived because its work dir still exists"
    assert not metadata.exists(), "the ownership sidecar outlived its alias"


def test_prune_reclaims_at_least_as_many_aliases_as_one_spawn_publishes(
    native_tree, monkeypatch, tmp_path
):
    """The reclaim cap covers the count one run publishes.

    Each spawn publishes one alias per agent and leaves that many behind when
    it ends. A cap below that count reclaims less than each run adds, so a
    steady spawn rate grows the directory without bound (143 agents against a
    cap of 64 on the reporting host).
    """
    _home, agents, project = native_tree
    names = ["alpha", "beta", "gamma"]
    for name in names:
        (agents / f"{name}.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name=n, filename=f"{n}.json", scope="global") for n in names],
    )
    monkeypatch.setattr(projection, "_PRUNE_MAX_RECLAIMS_PER_RUN", 1)
    _hold_the_prune_clock(monkeypatch)
    run_dir = tmp_path / "subagent_00000001"
    run_dir.mkdir()
    ended = projection.prepare_native_skill_projection(run_dir)
    left_behind = [_alias_file(agents, ended, n) for n in names]
    del ended
    gc.collect()
    for name in names:
        (agents / f"{name}.json").write_text(
            json.dumps({"name": name, "description": "edited"}), encoding="utf-8"
        )

    live = projection.prepare_native_skill_projection(project)
    assert live is not None
    assert not any(p.exists() for p in left_behind), "one spawn reclaimed fewer than it published"
    assert all(_alias_file(agents, live, n).exists() for n in names)


def test_prune_drains_headroom_beyond_one_spawn_publishes(native_tree, monkeypatch, tmp_path):
    """The cap drains headroom in addition to covering one spawn's aliases."""
    _home, agents, project = native_tree
    names = ["alpha", "beta", "gamma"]
    for name in names:
        (agents / f"{name}.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name=n, filename=f"{n}.json", scope="global") for n in names],
    )
    monkeypatch.setattr(projection, "_PRUNE_MAX_RECLAIMS_PER_RUN", 2)
    _hold_the_prune_clock(monkeypatch)

    def edit(version):
        for name in names:
            (agents / f"{name}.json").write_text(
                json.dumps({"name": name, "description": version}), encoding="utf-8"
            )

    run_dirs = [tmp_path / f"subagent_{n:08x}" for n in range(2)]
    ended = []
    for n, run_dir in enumerate(run_dirs):
        run_dir.mkdir()
        edit(f"v{n}")
        ended.append(projection.prepare_native_skill_projection(run_dir))
    edit("live")
    backlog = [_alias_file(agents, prepared, name) for prepared in ended for name in names]
    del ended
    gc.collect()
    assert sum(path.exists() for path in backlog) == 6

    live = projection.prepare_native_skill_projection(project)
    assert live is not None
    assert sum(path.exists() for path in backlog) == 1

    second_live = projection.prepare_native_skill_projection(project)
    assert second_live is not None
    assert not any(path.exists() for path in backlog)


def test_alias_count_stays_bounded_across_many_ended_runs(native_tree, tmp_path):
    """Spawning N runs in N directories, each ending, leaves one run's aliases."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    for n in range(12):
        run_dir = tmp_path / f"subagent_{n:08x}"
        run_dir.mkdir()
        prepared = projection.prepare_native_skill_projection(run_dir)
        assert prepared is not None
        del prepared
        gc.collect()
    live = projection.prepare_native_skill_projection(project)
    aliases = sorted(p.name for p in agents.glob(f"{projection.NATIVE_SKILL_ALIAS_PREFIX}*.json"))
    assert aliases == [f"{live.agent('custom')}.json"]


def test_prune_keeps_alias_replaced_after_unused_classification(native_tree, monkeypatch):
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    unused = _alias_file(agents, first)
    replacement = unused.read_text(encoding="utf-8")
    del first
    gc.collect()

    (agents / "other.json").write_text('{"name":"other"}', encoding="utf-8")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="other", filename="other.json", scope="global")],
    )
    original_metadata = projection._managed_metadata_for_alias
    replacement_identity = []

    def replace_after_classification(directory, path, raw):
        result = original_metadata(directory, path, raw)
        if (
            result is not None
            and result[0].get(projection._MANAGED_AGENT) == "custom"
            and not replacement_identity
        ):
            # Model another gateway atomically recreating the alias after this
            # gateway classified the old one as unused and before it unlinks.
            projection.atomic_write(unused, replacement, restrict_to_owner=True)
            current = unused.stat()
            replacement_identity.append((current.st_dev, current.st_ino))
        return result

    monkeypatch.setattr(projection, "_managed_metadata_for_alias", replace_after_classification)
    projection.prepare_native_skill_projection(project)

    assert replacement_identity
    current = unused.stat()
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
    holder = lease_dir / "crashed.hold"
    projection.atomic_write(
        stale,
        json.dumps({"aliases": ["kirocrew-skill-view-stale"]}),
        restrict_to_owner=True,
    )
    projection.atomic_write(holder, "", restrict_to_owner=True)

    assert not _alias_is_live(agents, "kirocrew-skill-view-stale")
    assert not stale.exists()
    assert not holder.exists()


def test_lease_scan_does_not_mutate_before_finding_later_held_match(native_tree, monkeypatch):
    _home, agents, _project = native_tree
    alias = "kirocrew-skill-view-held"
    lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
    lease_dir.mkdir()
    stale = lease_dir / "stale-first.json"
    stale_holder = lease_dir / "stale-first.hold"
    projection.atomic_write(
        stale,
        json.dumps({"aliases": ["kirocrew-skill-view-stale"]}),
        restrict_to_owner=True,
    )
    projection.atomic_write(stale_holder, "", restrict_to_owner=True)
    held = projection._acquire_projection_lease(agents, {alias})
    held_record = next(path for path in lease_dir.glob("*.json") if path != stale)

    class MutationSensitiveEntries:
        def __init__(self):
            self.active = False
            self.index = 0

        def __enter__(self):
            self.active = True
            return self

        def __exit__(self, *_args):
            self.active = False

        def __iter__(self):
            return self

        def __next__(self):
            if self.index == 0:
                self.index += 1
                return SimpleNamespace(name=stale.name)
            if self.index == 1 and stale.exists():
                self.index += 1
                return SimpleNamespace(name=held_record.name)
            raise StopIteration

    entries = MutationSensitiveEntries()
    real_scandir = projection.os.scandir
    in_scan_unlinks = []
    real_unlink = projection._unlink_projection_lease_if_unchanged

    def mutation_sensitive_scandir(path, *args, **kwargs):
        if path == lease_dir:
            return entries
        return real_scandir(path, *args, **kwargs)

    def record_unlink(path, identity):
        if entries.active:
            in_scan_unlinks.append(path)
        return real_unlink(path, identity)

    try:
        with monkeypatch.context() as scan_patch:
            scan_patch.setattr(projection.os, "scandir", mutation_sensitive_scandir)
            scan_patch.setattr(projection, "_unlink_projection_lease_if_unchanged", record_unlink)
            # The alias is live because a held lease names it; the scan collects
            # that into its union rather than early-returning on the match.
            assert _alias_is_live(agents, alias) is True
            # The invariant that still holds: nothing is unlinked DURING the
            # active scandir iteration -- cleanup is deferred until it closes.
            assert in_scan_unlinks == []
        # Held leases preserve liveness without suppressing reclamation of residue
        # in the SAME clean scan: liveness is carried by the union, so after the
        # scandir closes the unlocked crash residue is reclaimed even though a
        # held lease was found later in the pass.
        assert not stale.exists() and not stale_holder.exists()
    finally:
        held.close()


def test_lease_scan_error_defers_unlocked_residue_cleanup(native_tree, monkeypatch):
    _home, agents, _project = native_tree
    lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
    lease_dir.mkdir()
    stale = lease_dir / "stale-before-error.json"
    holder = lease_dir / "stale-before-error.hold"
    projection.atomic_write(
        stale,
        json.dumps({"aliases": ["kirocrew-skill-view-stale"]}),
        restrict_to_owner=True,
    )
    projection.atomic_write(holder, "", restrict_to_owner=True)

    class ErrorAfterFirstEntry:
        def __init__(self):
            self.yielded = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def __iter__(self):
            return self

        def __next__(self):
            if not self.yielded:
                self.yielded = True
                return SimpleNamespace(name=stale.name)
            raise OSError("test scandir iteration error")

    real_scandir = projection.os.scandir
    normalized_lease_dir = os.path.normcase(os.path.normpath(os.fspath(lease_dir)))

    def error_after_first_entry_scandir(path, *args, **kwargs):
        try:
            normalized_path = os.path.normcase(os.path.normpath(os.fspath(path)))
        except TypeError:
            return real_scandir(path, *args, **kwargs)
        if normalized_path == normalized_lease_dir:
            return ErrorAfterFirstEntry()
        return real_scandir(path, *args, **kwargs)

    with monkeypatch.context() as scan_patch:
        scan_patch.setattr(projection.os, "scandir", error_after_first_entry_scandir)
        assert _alias_is_live(agents, "kirocrew-skill-view-other") is True

    assert stale.exists() and holder.exists()


def test_lease_scan_cap_drains_validated_residue_before_answering_live(native_tree, monkeypatch):
    """A cap keeps the answer live but reclaims the residue it already validated.

    Hitting the record ceiling is uncertainty, so the alias is retained -- but the
    crash/finalizer residue the scan lock-validated BEFORE the cap has been proven
    stale and must not be absorbed. A prior build discarded it on any short scan,
    so a flood of records past the ceiling could permanently defer cleanup of
    leases already known dead. Only the records seen before the cap are reclaimed;
    a record beyond the ceiling is never scanned and never touched. This is the
    one behaviour that separates a cap from an open/iteration error, which trusts
    nothing it saw.
    """
    _home, agents, _project = native_tree
    lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
    lease_dir.mkdir()
    stale = lease_dir / "stale-before-cap.json"
    stale_holder = lease_dir / "stale-before-cap.hold"
    projection.atomic_write(
        stale,
        json.dumps({"aliases": ["kirocrew-skill-view-stale"]}),
        restrict_to_owner=True,
    )
    projection.atomic_write(stale_holder, "", restrict_to_owner=True)
    beyond = lease_dir / "beyond-cap.json"
    beyond_holder = lease_dir / "beyond-cap.hold"
    projection.atomic_write(
        beyond,
        json.dumps({"aliases": ["kirocrew-skill-view-beyond"]}),
        restrict_to_owner=True,
    )
    projection.atomic_write(beyond_holder, "", restrict_to_owner=True)

    monkeypatch.setattr(projection, "_PROJECTION_LEASE_SCAN_LIMIT", 1, raising=False)

    class OrderedEntries:
        def __init__(self):
            self.active = False
            self._entries = iter(())

        def __enter__(self):
            self.active = True
            self._entries = iter(
                (SimpleNamespace(name=stale.name), SimpleNamespace(name=beyond.name))
            )
            return self

        def __exit__(self, *_args):
            self.active = False

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._entries)

    entries = OrderedEntries()
    in_scan_unlinks = []
    real_scandir = projection.os.scandir
    real_unlink = projection._unlink_projection_lease_if_unchanged
    normalized_lease_dir = os.path.normcase(os.path.normpath(os.fspath(lease_dir)))

    def ordered_scandir(path, *args, **kwargs):
        try:
            normalized_path = os.path.normcase(os.path.normpath(os.fspath(path)))
        except TypeError:
            return real_scandir(path, *args, **kwargs)
        if normalized_path == normalized_lease_dir:
            return entries
        return real_scandir(path, *args, **kwargs)

    def record_unlink(path, identity):
        if entries.active:
            in_scan_unlinks.append(path)
        return real_unlink(path, identity)

    with monkeypatch.context() as scan_patch:
        scan_patch.setattr(projection.os, "scandir", ordered_scandir)
        scan_patch.setattr(projection, "_unlink_projection_lease_if_unchanged", record_unlink)
        assert _alias_is_live(agents, "kirocrew-skill-view-query") is True

    # The residue validated before the cap is reclaimed only after iterator
    # closure; the record past the cap was never scanned and remains on disk.
    assert in_scan_unlinks == []
    assert not stale.exists() and not stale_holder.exists()
    assert beyond.exists() and beyond_holder.exists()


def test_lease_scan_overflow_is_uncertain_and_retains_stale_alias(native_tree, monkeypatch):
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    stale = _alias_file(agents, first)
    del first
    gc.collect()
    source.unlink()

    # The scan ceiling bounds EVERY directory entry, not just the ``.json``
    # records (see _scan_projection_leases / defect: unrelated or ``.hold``
    # entries once bypassed the cap and could force an unbounded walk). With the
    # entries ordered record, holder, record, holder, ... a ceiling of 3 admits
    # residue-0's record and holder plus residue-1's record before it trips, so
    # exactly two records are probed and residue past that is never scanned.
    monkeypatch.setattr(projection, "_PROJECTION_LEASE_SCAN_LIMIT", 3, raising=False)
    lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
    lease_dir.mkdir(exist_ok=True)
    for index in range(3):
        stem = f"residue-{index}"
        (lease_dir / f"{stem}{projection._PROJECTION_LEASE_RECORD_SUFFIX}").write_text(
            json.dumps({"aliases": [f"other-{index}"]}), encoding="utf-8"
        )
        (lease_dir / f"{stem}{projection._PROJECTION_LEASE_HOLDER_SUFFIX}").write_text(
            "", encoding="utf-8"
        )

    # Pin the entry order so the ceiling's interaction with the record/holder
    # interleave is deterministic rather than dependent on filesystem scandir
    # order. Records and holders alternate, matching on-disk creation order.
    ordered_names = []
    for index in range(3):
        ordered_names.append(f"residue-{index}{projection._PROJECTION_LEASE_RECORD_SUFFIX}")
        ordered_names.append(f"residue-{index}{projection._PROJECTION_LEASE_HOLDER_SUFFIX}")

    class OrderedEntries:
        def __init__(self):
            self._entries = iter(SimpleNamespace(name=name) for name in ordered_names)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._entries)

    real_scandir = projection.os.scandir
    normalized_lease_dir = os.path.normcase(os.path.normpath(os.fspath(lease_dir)))

    def ordered_scandir(path, *args, **kwargs):
        try:
            same = os.path.normcase(os.path.normpath(os.fspath(path))) == normalized_lease_dir
        except TypeError:
            same = False
        if same:
            return OrderedEntries()
        return real_scandir(path, *args, **kwargs)

    # Scoped: os.scandir is process-global, and the tmp-tree teardown (an
    # os.walk-based rmtree on Windows) must see the real one.
    with monkeypatch.context() as scan_patch:
        scan_patch.setattr(projection.os, "scandir", ordered_scandir)

        holder_probes = 0
        real_open_lock_file = projection.platform_compat.open_lock_file

        def count_holder_probes(path):
            nonlocal holder_probes
            if path.parent == lease_dir:
                holder_probes += 1
            return real_open_lock_file(path)

        monkeypatch.setattr(projection.platform_compat, "open_lock_file", count_holder_probes)

        projection._prune_stale_managed_aliases(
            agents, projection.data_home().absolute().as_posix(), keep=set()
        )

    # Ceiling 3 over record, holder, record, ... admits residue-0's record and
    # holder and residue-1's record before tripping, so two records are probed.
    assert holder_probes == 2
    assert stale.exists(), "a capped lease scan must retain rather than authorize deletion"
    # The two residue leases lock-validated before the cap are reclaimed; residue
    # past the ceiling was never scanned and must remain untouched.
    remaining_records = sum(
        1
        for index in range(3)
        if (lease_dir / f"residue-{index}{projection._PROJECTION_LEASE_RECORD_SUFFIX}").exists()
    )
    remaining_holders = sum(
        1
        for index in range(3)
        if (lease_dir / f"residue-{index}{projection._PROJECTION_LEASE_HOLDER_SUFFIX}").exists()
    )
    assert remaining_records == 1
    assert remaining_holders == 1


def test_prune_work_cap_counts_retained_candidates_before_reclaims(native_tree, monkeypatch):
    """Reconciled contract: retained candidates do NOT starve the stale-work budget.

    Before this fix the bounded candidate scan counted kept/active/live aliases
    toward the work limit, so a retained entry sorting ahead of the backlog
    consumed a stale-work slot and left real backlog unexamined -- and a run's own
    just-published aliases sort early, making the starvation order-dependent
    rather than rare. The scan now skips retained entries WITHOUT charging the
    stale-work budget, while still bounding total traversal at the class limit
    plus the bounded skip set, so it is not an unbounded bypass. Here three live
    aliases are interleaved ahead of the stale backlog; with a stale-work limit of
    3 all three stale candidates are still examined and reclaimed, and the lease
    directory is scanned exactly once.
    """
    live_stems = {f"kirocrew-skill-view-{0xA00 + index:024x}" for index in range(3)}
    _home, agents, _project = native_tree
    stale = [_legacy_alias(agents, f"{index:024x}") for index in range(3)]

    monkeypatch.setattr(projection, "_PROJECTION_PRUNE_WORK_LIMIT", 3, raising=False)
    monkeypatch.setattr(projection, "_PRUNE_MAX_RECLAIMS_PER_RUN", 64)
    # Every stale candidate must be reached, so the walk must not end on the clock.
    _hold_the_prune_clock(monkeypatch)

    scans = 0

    def one_batch_scan(_directory):
        nonlocal scans
        scans += 1
        # Every live alias is reported by the single batch scan; the prune folds
        # them into the skip set the candidate generator excludes without charging
        # the stale-work budget.
        return set(live_stems), False

    monkeypatch.setattr(projection, "_scan_projection_leases", one_batch_scan)

    projection._prune_stale_managed_aliases(
        agents, projection.data_home().absolute().as_posix(), keep=set()
    )

    assert scans == 1, "the lease directory must be scanned once per prune, never per candidate"
    assert not any(
        path.exists() for path in stale
    ), "live aliases consumed stale-work budget and starved backlog reclamation"


def test_is_legacy_projected_view_deeply_nested_alias_reads_as_unreadable(
    native_tree, cyclic_gc_quiesced
):
    """A legacy-named alias nested past the interpreter limit is not legacy, and does not abort.

    ``_is_legacy_projected_view`` is the third ownership JSON reader; its
    ``json.loads`` guard caught only ``(ValueError, TypeError)``, so a
    hand-authored alias whose JSON nests past the recursion limit raised
    ``RecursionError`` (a ``RuntimeError``) straight through the reader and
    aborted the caller instead of failing closed. The reader must treat such a
    file as an unreadable view (return ``False``), never legacy to reclaim.
    Fails at pytest call on 694b43971 (2000 levels does not trip recursion, so
    the fixture nests 100000 deep, matching the sidecar/lease readers' fixtures).
    """
    _home, agents, _project = native_tree
    alias_path = agents / f"{projection.NATIVE_SKILL_ALIAS_PREFIX}{'0' * 24}.json"
    deep = ("[" * 100000 + "]" * 100000).encode("utf-8")
    alias_path.write_bytes(deep)
    assert projection._is_legacy_projected_view(alias_path, deep) is False


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


def test_census_counts_what_the_reclaim_would_keep_and_remove(native_tree, tmp_path, monkeypatch):
    """The read-only census agrees with the lifecycle it describes, and changes nothing."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    crew_home = projection.data_home().absolute().as_posix()

    ended_dir = tmp_path / "subagent_00000001"
    ended_dir.mkdir()
    ended = projection.prepare_native_skill_projection(ended_dir)
    assert ended is not None
    published = len(ended.aliases)
    del ended
    gc.collect()
    live = projection.prepare_native_skill_projection(project)
    assert live is not None

    before = sorted(str(p) for p in agents.rglob("*"))
    census = projection.census_projected_aliases(agents)
    assert sorted(str(p) for p in agents.rglob("*")) == before
    # `live` reclaimed the ended run's aliases on its own spawn (cap covers
    # them), so the directory holds exactly the live set, all lease-named.
    assert census == {
        "total": published,
        "leased": published,
        "foreign_home": 0,
        "foreign_leased": 0,
        "unreadable_leases": 0,
        "truncated": 0,
    }

    # An alias another data home recorded is counted as foreign, never as
    # something this gateway will drain; a malformed lease record is reported
    # rather than skipped, because the reclaim treats it as "everything live".
    foreign = agents / f"{projection.NATIVE_SKILL_ALIAS_PREFIX}{'f' * 24}.json"
    foreign.write_text("{}", encoding="utf-8")
    (agents / projection._PROJECTION_METADATA_DIR_NAME / f"{foreign.stem}.json").write_text(
        json.dumps(
            {
                projection._MANAGED_MARKER: projection._MANAGED_MARKER_VALUE,
                projection._MANAGED_CREW_HOME: crew_home + "-other",
            }
        ),
        encoding="utf-8",
    )
    (agents / projection._PROJECTION_LEASE_DIR_NAME / "9-broken.json").write_text(
        "{", encoding="utf-8"
    )
    census = projection.census_projected_aliases(agents)
    assert census == {
        "total": published + 1,
        "leased": published,
        "foreign_home": 1,
        "foreign_leased": 0,
        "unreadable_leases": 1,
        "truncated": 0,
    }

    # The other home's LIVE aliases -- named by its lease -- are split out too,
    # since this gateway's reclaim refuses them whether or not the lease holds.
    (agents / projection._PROJECTION_LEASE_DIR_NAME / "9-theirs.json").write_text(
        json.dumps({"aliases": [foreign.stem]}), encoding="utf-8"
    )
    census = projection.census_projected_aliases(agents)
    assert census["leased"] == published + 1
    assert census["foreign_home"] == 0
    assert census["foreign_leased"] == 1

    # A RecursionError from json.loads is an unreadable record, not an abort.
    (agents / projection._PROJECTION_LEASE_DIR_NAME / "9-deep.json").write_text(
        "[" * 100000 + "]" * 100000, encoding="utf-8"
    )
    census = projection.census_projected_aliases(agents)
    assert census["unreadable_leases"] == 2
    assert (
        projection._read_lease_record(
            agents / projection._PROJECTION_LEASE_DIR_NAME / "9-deep.json"
        )
        is None
    )

    # Retention is bounded and the bound is reported, not silently exceeded.
    monkeypatch.setattr(projection, "_PROJECTION_LEASE_SCAN_LIMIT", 1, raising=False)
    census = projection.census_projected_aliases(agents)
    assert census["truncated"] == 1
    assert census["total"] == published + 1
    del live


def test_boot_drain_pause_outlasts_the_lock_poll_cap():
    """A spawn blocked on the publication lock polls with backoff up to the
    lock's poll cap. A between-batch gap shorter than that cap can open and
    close while the waiter sleeps, so it never takes the lock and runs into
    the 2s acquisition ceiling instead."""
    from kiro_crew import platform_compat

    assert projection._DRAIN_BATCH_PAUSE_SECS > platform_compat._LOCK_POLL_MAX_SECS
    assert projection._DRAIN_BATCH_PAUSE_SECS < projection._PROJECTION_LOCK_TIMEOUT_SECS


def _released_backlog(agents, project, count):
    """*count* stale aliases of one agent, each with its sidecar, no lease held.

    Every version stays held while the next is published, so no spawn prunes
    it; releasing them all at once leaves a backlog, as a capped prune does.
    """
    source = agents / "custom.json"
    held = []
    for n in range(count):
        source.write_text(json.dumps({"name": "custom", "description": f"v{n}"}), encoding="utf-8")
        held.append(projection.prepare_native_skill_projection(project))
    backlog = [(_alias_file(agents, p), _metadata_file(agents, p)) for p in held]
    del held
    gc.collect()
    assert all(alias.exists() and meta.exists() for alias, meta in backlog)
    return backlog


def _recorded_batches(monkeypatch):
    """What each of the drain's prune batches reclaimed, in order."""
    real_prune = projection._prune_stale_managed_aliases
    batches = []

    def recording(directory, crew_home_id, **kwargs):
        reclaimed = real_prune(directory, crew_home_id, **kwargs)
        batches.append(reclaimed)
        return reclaimed

    monkeypatch.setattr(projection, "_prune_stale_managed_aliases", recording)
    return batches


def test_boot_drain_clears_a_backlog_past_the_per_spawn_cap(native_tree, monkeypatch):
    _home, agents, project = native_tree
    backlog = _released_backlog(agents, project, 7)
    monkeypatch.setattr(projection, "_PRUNE_MAX_RECLAIMS_PER_RUN", 2)
    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)

    assert projection.drain_stale_aliases() == 7
    assert not any(alias.exists() or meta.exists() for alias, meta in backlog)


def test_boot_drain_keeps_aliases_a_live_projection_holds(native_tree, monkeypatch):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    live = projection.prepare_native_skill_projection(project)
    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)
    projection.drain_stale_aliases()
    assert _alias_file(agents, live).exists()
    assert _metadata_file(agents, live).exists()


def test_boot_drain_retries_a_batch_that_missed_the_lock(native_tree, monkeypatch):
    """One contended batch at boot -- a spawn publishing while the drain
    runs -- must not abandon the whole backlog until the next restart."""
    _home, agents, project = native_tree
    backlog = _released_backlog(agents, project, 5)
    real_lock = projection._projection_alias_lock
    # The second batch misses the lock; every later one takes it, however many
    # the drain needs (a scripted list that runs dry would raise StopIteration
    # into the drain's catch-all and end it early, hiding a short count).
    outcomes = itertools.chain(["ok", "held"], itertools.repeat("ok"))
    attempts = []

    def flaky(directory):
        outcome = next(outcomes)
        attempts.append(outcome)
        if outcome == "held":
            raise OSError("held")
        return real_lock(directory)

    monkeypatch.setattr(projection, "_projection_alias_lock", flaky)
    monkeypatch.setattr(projection, "_PRUNE_MAX_RECLAIMS_PER_RUN", 2)
    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)
    # The exact batch sequence below assumes each walk ends on the cap, not the clock.
    _hold_the_prune_clock(monkeypatch)

    assert projection.drain_stale_aliases() == 5
    assert not any(alias.exists() or meta.exists() for alias, meta in backlog)
    # Batches reclaim 2, miss, 2, 1, then three idle batches end the drain: the
    # miss counted as one idle batch, and the reclaims after it restarted the
    # idle run rather than ending the loop.
    assert attempts == ["ok", "held", "ok", "ok", "ok", "ok", "ok"]


def test_boot_drain_ends_when_the_lock_is_never_available(native_tree, monkeypatch):
    """The lock helper raises OSError for a busy lock AND for a lock-file fault
    (a symlinked lock, a permission error). Either one, repeated, is a run of
    idle batches: the drain ends at that bound instead of paying every batch,
    and raises nothing into the boot janitor."""
    _home, agents, _project = native_tree
    attempts = []

    def faulted(_directory):
        attempts.append(1)
        raise OSError("skill projection lock is a symlink or junction")

    monkeypatch.setattr(projection, "_projection_alias_lock", faulted)
    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)

    assert projection.drain_stale_aliases() == 0
    assert len(attempts) == projection._DRAIN_IDLE_BATCHES


def test_boot_drain_continues_past_a_batch_the_budget_cut_short(native_tree, monkeypatch):
    """A batch that spends its time budget before reaching a reclaimable entry
    returns zero, and that zero is not the end of the backlog: the per-spawn
    prune walks from a random offset, so a prefix of kept or leased entries
    can eat a whole budget. Only a run of consecutive zeros, each from a fresh
    offset, means the sweep is done."""
    _home, agents, project = native_tree
    backlog = _released_backlog(agents, project, 5)
    real_budget = projection._PRUNE_MAX_SECONDS_PER_RUN
    batches = _recorded_batches(monkeypatch)
    recording = projection._prune_stale_managed_aliases
    # Every batch after the starved one must finish its walk for the reclaim
    # counts below to mean anything; with the clock held, the real budget is
    # never spent by wall-clock time.
    _hold_the_prune_clock(monkeypatch)

    def starve_the_first_batch(directory, crew_home_id, **kwargs):
        # A zero budget is spent at the first candidate, before any is classified.
        monkeypatch.setattr(
            projection, "_PRUNE_MAX_SECONDS_PER_RUN", 0.0 if not batches else real_budget
        )
        return recording(directory, crew_home_id, **kwargs)

    monkeypatch.setattr(projection, "_prune_stale_managed_aliases", starve_the_first_batch)
    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)

    assert projection.drain_stale_aliases() == 5
    # The reclaiming batch restarted the idle count, so the drain paid the full
    # run of idle batches after it rather than stopping at the first zero.
    assert batches == [0, 5] + [0] * projection._DRAIN_IDLE_BATCHES
    assert not any(alias.exists() or meta.exists() for alias, meta in backlog)


def test_boot_drain_ends_after_a_run_of_batches_that_reclaim_nothing(native_tree, monkeypatch):
    """The continue-past-a-cut-short-batch rule must not turn a clean directory
    into a thousand-batch walk: a short run of batches that reclaim nothing
    ends it."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    live = projection.prepare_native_skill_projection(project)
    batches = _recorded_batches(monkeypatch)
    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)

    assert projection.drain_stale_aliases() == 0
    assert batches == [0] * projection._DRAIN_IDLE_BATCHES
    assert _alias_file(agents, live).exists()


def test_boot_drain_ends_on_idle_batches_when_every_budget_is_spent(native_tree, monkeypatch):
    """The stopping rule cannot be "a walk that saw every candidate": on a large
    directory whose entries mostly belong to another data home or are leased,
    every batch spends its time budget first, so no walk is ever exhaustive.
    Such a drain must still end on the idle-batch run instead of paying all
    _DRAIN_MAX_BATCHES batches for nothing."""
    _home, agents, project = native_tree
    backlog = _released_backlog(agents, project, 3)
    batches = _recorded_batches(monkeypatch)
    # Spent at the first candidate, on every batch: reclaimed is always zero and
    # the walk never classifies anything.
    monkeypatch.setattr(projection, "_PRUNE_MAX_SECONDS_PER_RUN", 0.0)
    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)

    assert projection.drain_stale_aliases() == 0
    assert batches == [0] * projection._DRAIN_IDLE_BATCHES
    # Nothing was reclaimable within the budget, so the backlog is untouched and
    # later spawns (or the next boot) clear it.
    assert all(alias.exists() and meta.exists() for alias, meta in backlog)


def test_boot_drain_idle_count_restarts_on_any_reclaim(native_tree, monkeypatch):
    """Only CONSECUTIVE zeros end the drain. A reclaim restarts the count, so a
    backlog that yields intermittently is cleared rather than abandoned at the
    first pair of quiet batches."""
    _home, agents, _project = native_tree
    # Never runs dry: a StopIteration would end the drain through its catch-all.
    reclaims = itertools.chain([0, 0, 1], itertools.repeat(0))
    batches = []

    def scripted(directory, crew_home_id, **kwargs):
        reclaimed = next(reclaims)
        batches.append(reclaimed)
        return reclaimed

    monkeypatch.setattr(projection, "_prune_stale_managed_aliases", scripted)
    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)

    assert projection.drain_stale_aliases() == 1
    # Two idle, one reclaim that restarts the count, then a full idle run.
    assert batches == [0, 0, 1] + [0] * projection._DRAIN_IDLE_BATCHES


def test_boot_drain_ends_when_the_alias_directory_cannot_be_listed(native_tree, monkeypatch):
    """An unlistable directory reclaims nothing on every batch, so the drain
    ends on the idle run without touching the backlog and without raising."""
    import errno

    _home, agents, project = native_tree
    backlog = _released_backlog(agents, project, 2)
    batches = _recorded_batches(monkeypatch)
    real_scandir = projection.os.scandir
    normalized_agents = os.path.normcase(os.path.normpath(os.fspath(agents)))

    def unlistable(path, *args, **kwargs):
        try:
            same = os.path.normcase(os.path.normpath(os.fspath(path))) == normalized_agents
        except TypeError:
            same = False
        if same:
            raise OSError(errno.EIO, "input/output error", str(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)
    # Scoped: os.scandir is process-global, and the tmp-tree teardown (an
    # os.walk-based rmtree on Windows) must see the real one.
    with monkeypatch.context() as scan_patch:
        scan_patch.setattr(projection.os, "scandir", unlistable)
        drained = projection.drain_stale_aliases()

    assert drained == 0
    assert batches == [0] * projection._DRAIN_IDLE_BATCHES
    assert all(alias.exists() and meta.exists() for alias, meta in backlog)


# ── Residual bounds: dual-coverage adoption, crash-residue recovery, batch lease scan ──


def test_lease_scan_unions_aliases_from_every_held_lease(native_tree):
    """Finding 3: one scan unions the aliases named by every held lease."""
    _home, agents, _project = native_tree
    a = projection._acquire_projection_lease(
        agents, {"kirocrew-skill-view-aaa", "kirocrew-skill-view-bbb"}
    )
    b = projection._acquire_projection_lease(agents, {"kirocrew-skill-view-ccc"})
    try:
        live, uncertain = projection._scan_projection_leases(agents)
    finally:
        a.close()
        b.close()
    assert uncertain is False
    assert live == {
        "kirocrew-skill-view-aaa",
        "kirocrew-skill-view-bbb",
        "kirocrew-skill-view-ccc",
    }


def test_prune_scans_the_lease_directory_once_for_many_candidates(native_tree, monkeypatch):
    """Finding 3: the lease directory is scanned ONCE per prune, not once per candidate.

    On the old per-candidate path this scanned the whole lease directory once for
    every retained candidate; the batch scan reads it once. Fails at pytest call
    on fcef4c9a4 (four candidates -> four scans there, one here).
    """
    _home, agents, _project = native_tree
    candidates = [_legacy_alias(agents, f"{index:024x}") for index in range(4)]
    external = projection._acquire_projection_lease(agents, {p.stem for p in candidates})
    lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
    normalized_lease_dir = os.path.normcase(os.path.normpath(os.fspath(lease_dir)))
    scandir_calls = 0
    real_scandir = projection.os.scandir

    def counting_scandir(path, *args, **kwargs):
        nonlocal scandir_calls
        try:
            same = os.path.normcase(os.path.normpath(os.fspath(path))) == normalized_lease_dir
        except TypeError:
            same = False
        if same:
            scandir_calls += 1
        return real_scandir(path, *args, **kwargs)

    # Scoped: os.scandir is process-global, and the tmp-tree teardown (an
    # os.walk-based rmtree on Windows) must see the real one.
    with monkeypatch.context() as scan_patch:
        scan_patch.setattr(projection.os, "scandir", counting_scandir)
        try:
            projection._prune_stale_managed_aliases(
                agents, projection.data_home().absolute().as_posix(), keep=set()
            )
        finally:
            external.close()

    assert scandir_calls == 1, "the lease directory must be scanned once, not once per candidate"
    assert all(p.exists() for p in candidates)


def test_prune_skips_all_candidate_deletion_when_lease_scan_is_uncertain(native_tree, monkeypatch):
    """Finding 3: any lease-scan uncertainty authorizes NO candidate deletion."""
    _home, agents, _project = native_tree
    stale = [_legacy_alias(agents, f"{index:024x}") for index in range(3)]
    monkeypatch.setattr(projection, "_scan_projection_leases", lambda _directory: (set(), True))

    projection._prune_stale_managed_aliases(
        agents, projection.data_home().absolute().as_posix(), keep=set()
    )

    assert all(p.exists() for p in stale), "an uncertain lease scan must authorize no deletion"


# ── Residual bounds, second pass: unbounded lease traversal, keep-starved prune,
#    crash-residue digest validity, sidecar-less foreign adoption, cap summary ──


def test_lease_record_scan_bounds_all_entries_not_only_records(native_tree, monkeypatch):
    """Defect 1: the scan ceiling must bound EVERY entry, not only ``.json`` records.

    The lease scan counted only ``.json`` records toward
    _PROJECTION_LEASE_SCAN_LIMIT, so a directory padded with arbitrary ``.hold``
    or unrelated entries slipped past the ceiling: a scan holding the publication
    lock could then walk a number of entries proportional to the whole directory
    rather than to the limit. With the ceiling of 2 and a leading pad of
    non-record entries, the fixed scan stops after 2 walked entries and yields no
    record; the old code walked every pad entry looking for records. Counting the
    walked entries proves the bound is over entries, not records.
    """
    lease_dir = agents_dir(native_tree) / projection._PROJECTION_LEASE_DIR_NAME
    lease_dir.mkdir()
    monkeypatch.setattr(projection, "_PROJECTION_LEASE_SCAN_LIMIT", 2, raising=False)

    # Many non-record entries first, then a real record far past the ceiling.
    pad_names = [f"pad-{index}{projection._PROJECTION_LEASE_HOLDER_SUFFIX}" for index in range(50)]
    record_name = f"real{projection._PROJECTION_LEASE_RECORD_SUFFIX}"
    ordered = [*pad_names, record_name]

    walked = 0

    class CountingEntries:
        def __init__(self):
            self._entries = iter(ordered)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal walked
            name = next(self._entries)
            walked += 1
            return SimpleNamespace(name=name)

    real_scandir = projection.os.scandir
    normalized = os.path.normcase(os.path.normpath(os.fspath(lease_dir)))

    def counting_scandir(path, *args, **kwargs):
        try:
            same = os.path.normcase(os.path.normpath(os.fspath(path))) == normalized
        except TypeError:
            same = False
        return CountingEntries() if same else real_scandir(path, *args, **kwargs)

    # Scoped: os.scandir is process-global, and the tmp-tree teardown (an
    # os.walk-based rmtree on Windows) must see the real one.
    with monkeypatch.context() as scan_patch:
        scan_patch.setattr(projection.os, "scandir", counting_scandir)

        records = []

        def recording_probe(lease_path):
            records.append(lease_path.name)
            return [], None

        scan_patch.setattr(projection, "_probe_projection_lease", recording_probe)
        live, uncertain = projection._scan_projection_leases(agents_dir(native_tree))

    # Fixed: the ceiling stops the walk after it has counted the limit, pulling
    # at most _PROJECTION_LEASE_SCAN_LIMIT + 1 entries from the iterator (the last
    # one is the entry that trips the break), so no record is reached. The old
    # code walked all 51 entries because pads did not count toward the ceiling.
    assert walked <= projection._PROJECTION_LEASE_SCAN_LIMIT + 1
    assert walked < len(ordered), "the scan walked the whole padded directory"
    assert records == []
    assert live == set() and uncertain is True


def test_lease_record_scan_still_yields_records_within_the_bound(native_tree, monkeypatch):
    """Defect 1 companion: a record within the ceiling is still yielded.

    Bounding all entries must not stop yielding legitimate records that fall
    within the ceiling -- only the padding past it is skipped.
    """
    lease_dir = agents_dir(native_tree) / projection._PROJECTION_LEASE_DIR_NAME
    lease_dir.mkdir()
    monkeypatch.setattr(projection, "_PROJECTION_LEASE_SCAN_LIMIT", 4, raising=False)
    ordered = [
        f"a{projection._PROJECTION_LEASE_RECORD_SUFFIX}",
        f"a{projection._PROJECTION_LEASE_HOLDER_SUFFIX}",
        f"b{projection._PROJECTION_LEASE_RECORD_SUFFIX}",
        f"b{projection._PROJECTION_LEASE_HOLDER_SUFFIX}",
        f"c{projection._PROJECTION_LEASE_RECORD_SUFFIX}",
    ]

    class OrderedEntries:
        def __init__(self):
            self._entries = iter(SimpleNamespace(name=name) for name in ordered)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._entries)

    real_scandir = projection.os.scandir
    normalized = os.path.normcase(os.path.normpath(os.fspath(lease_dir)))

    def ordered_scandir(path, *args, **kwargs):
        try:
            same = os.path.normcase(os.path.normpath(os.fspath(path))) == normalized
        except TypeError:
            same = False
        return OrderedEntries() if same else real_scandir(path, *args, **kwargs)

    # Scoped: os.scandir is process-global, and the tmp-tree teardown (an
    # os.walk-based rmtree on Windows) must see the real one.
    with monkeypatch.context() as scan_patch:
        scan_patch.setattr(projection.os, "scandir", ordered_scandir)

        record_names = []

        def recording_probe(lease_path):
            record_names.append(lease_path.name)
            return [lease_path.stem], None

        scan_patch.setattr(projection, "_probe_projection_lease", recording_probe)
        live, uncertain = projection._scan_projection_leases(agents_dir(native_tree))

    # Ceiling 4 over record, holder, record, holder, record admits a.json and
    # b.json (records at walked positions 1 and 3); c.json sits past the ceiling.
    assert record_names == [
        f"a{projection._PROJECTION_LEASE_RECORD_SUFFIX}",
        f"b{projection._PROJECTION_LEASE_RECORD_SUFFIX}",
    ]
    # Both held records union into the live set, and the cap answers uncertain.
    assert live == {"a", "b"} and uncertain is True


def test_prune_does_not_let_the_run_keep_alias_starve_backlog_reclamation(native_tree, monkeypatch):
    """Defect 2: a kept alias sorting early must not consume a stale-work slot.

    The bounded candidate scan counted the run's own just-published keep alias
    toward the stale-work limit. Because that alias sorts early in the directory,
    it stole a work slot and left one backlog alias unexamined: with a work limit
    of 4 and reclaim cap _PRUNE_MAX_RECLAIMS_PER_RUN + len(keep) = 4, the old code
    reclaimed only 3 of the first 4 stale candidates. The fix skips the keep alias
    without charging the stale budget, so all 4 stale candidates in the window are
    examined and reclaimed. A pinned entry order puts the keep alias FIRST, which
    is the order-dependent worst case the CI regression only hits by luck.
    """
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    monkeypatch.setattr(projection, "_PROJECTION_PRUNE_WORK_LIMIT", 4, raising=False)
    monkeypatch.setattr(projection, "_PRUNE_MAX_RECLAIMS_PER_RUN", 3)
    # The exact reclaim count below assumes the walk ends on the cap, not the clock.
    _hold_the_prune_clock(monkeypatch)
    backlog = [_legacy_alias(agents, f"{index:024x}") for index in range(8)]

    # The run's own alias is named by its view digest, so it is identified at
    # scan time as the one Crew-prefixed alias that is not part of the backlog.
    backlog_names = {path.name for path in backlog}
    real_scandir = projection.os.scandir
    normalized_agents = os.path.normcase(os.path.normpath(os.fspath(agents)))

    def keep_first_scandir(path, *args, **kwargs):
        try:
            same = os.path.normcase(os.path.normpath(os.fspath(path))) == normalized_agents
        except TypeError:
            same = False
        if not same:
            return real_scandir(path, *args, **kwargs)
        # Materialize the real entries, then force the run's own keep alias to the
        # front so it lands inside the bounded candidate window.
        entries = list(real_scandir(path, *args, **kwargs))
        entries.sort(
            key=lambda e: (
                0
                if e.name.startswith(projection.NATIVE_SKILL_ALIAS_PREFIX)
                and e.name.endswith(".json")
                and e.name not in backlog_names
                else 1
            )
        )

        class _Ctx:
            def __init__(self):
                self._entries = iter(entries)

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return None

            def __iter__(self):
                return self

            def __next__(self):
                return next(self._entries)

        return _Ctx()

    # Scoped: os.scandir is process-global, and the tmp-tree teardown (an
    # os.walk-based rmtree on Windows) must see the real one.
    with monkeypatch.context() as scan_patch:
        scan_patch.setattr(projection.os, "scandir", keep_first_scandir)

        prepared = projection.prepare_native_skill_projection(project)
    assert prepared is not None
    # Exactly four backlog aliases reclaimed in the first run despite the keep
    # alias sorting first; the old keep-starved scan reclaimed only three.
    assert sum(1 for path in backlog if not path.exists()) == 4
    keep_stem = prepared.aliases["custom"]
    assert (agents / f"{keep_stem}.json").exists(), "the run's own alias must survive"


def test_prune_candidate_traversal_counts_unrelated_entries_and_caps_skip_credit(
    native_tree, monkeypatch
):
    """Neither arbitrary padding nor a large skip set can extend the walk."""
    _home, agents, _project = native_tree
    work_limit = 3
    skip = {f"kirocrew-skill-view-{0xB00 + index:024x}" for index in range(20)}
    names = [
        *(f"unrelated-{index}.json" for index in range(50)),
        *(f"{stem}.json" for stem in sorted(skip)),
        *(f"kirocrew-skill-view-{index:024x}.json" for index in range(2)),
    ]
    walked = 0

    class CountingEntries:
        def __init__(self):
            self._entries = iter(names)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal walked
            name = next(self._entries)
            walked += 1
            return SimpleNamespace(name=name)

    real_scandir = projection.os.scandir

    def counting_scandir(path, *args, **kwargs):
        if path == agents:
            return CountingEntries()
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(projection, "_PROJECTION_PRUNE_WORK_LIMIT", work_limit, raising=False)
    # Scoped: os.scandir is process-global, and the tmp-tree teardown (an
    # os.walk-based rmtree on Windows) must see the real one.
    with monkeypatch.context() as scan_patch:
        scan_patch.setattr(projection.os, "scandir", counting_scandir)

        yielded = list(projection._projection_prune_candidates(agents, skip))
    assert yielded == []
    ceiling = (2 * work_limit) + work_limit
    assert walked <= ceiling + 1
    assert walked < len(names), "the prune walked the padded directory"

    spec = (Path(__file__).parents[1] / "docs/system-specs/modules/acp-client.md").read_text(
        encoding="utf-8"
    )
    normalized_spec = " ".join(spec.split())
    for contract in (
        "Reclaims, stale-candidate work, and total traversal have separate PER RUN ceilings",
        "every directory entry consumes the separately bounded traversal budget",
        "skip credit is capped at one work-limit",
        "the ceiling guarantees bounded entry traversal rather than eventual drain",
    ):
        assert contract in normalized_spec


def test_deeply_nested_ownership_sidecar_reads_as_unowned_in_the_prune(
    native_tree, cyclic_gc_quiesced
):
    """A recursive JSON shape is unreadable ownership, not a prune exception."""
    _home, agents, project = native_tree
    source = agents / "custom.json"
    source.write_text('{"name":"custom"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    assert first is not None
    alias_path = _alias_file(agents, first)
    metadata_path = _metadata_file(agents, first)
    del first
    gc.collect()

    metadata_path.write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
    alias_raw = alias_path.read_bytes()

    assert projection._managed_metadata_for_alias(agents, alias_path, alias_raw) is None
    projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set())
    assert alias_path.read_bytes() == alias_raw


def test_alias_has_external_lease_product_symbol_is_removed(native_tree):
    """Defect 6: the zero-consumer product wrapper is deleted, not merely unused.

    Its only callers were tests, which now exercise _scan_projection_leases
    directly. Leaving it in place kept misleading "for direct callers"
    documentation alive, so its removal is part of the fix.
    """
    assert not hasattr(projection, "_alias_has_external_lease")
    # The batch scan the tests now use directly is present and callable.
    _home, agents, _project = native_tree
    live, uncertain = projection._scan_projection_leases(agents)
    assert live == set() and uncertain is False


def agents_dir(native_tree):
    return native_tree[1]


def test_prune_classification_seam_is_the_production_step(native_tree):
    """The per-candidate unit a test observes is the production classification.

    Kept, active and leased names never reach the budgeted loop, so the unit it
    pays for is the ownership classification in ``_reclaim_prune_candidate``.
    """
    _home, agents, _project = native_tree
    backlog = _legacy_alias(agents, "c" * 24)
    assert projection._reclaim_prune_candidate(agents, backlog, _crew_home_id()) is True
    assert not backlog.exists()


def test_capped_lease_scan_reports_its_ceiling_and_reclaimed_count(
    native_tree, monkeypatch, caplog
):
    """A capped scan authorizes no alias deletion, so it has to say it capped.

    Otherwise an incremental drain is invisible: the prune returns before its own
    reclaim log, and per-lease reclaims are DEBUG only.
    """
    _home, agents, _project = native_tree
    lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
    lease_dir.mkdir()
    for n in range(3):
        projection.atomic_write(
            lease_dir / f"residue-{n}.json",
            json.dumps({"aliases": [f"kirocrew-skill-view-{n}"]}),
            restrict_to_owner=True,
        )
        projection.atomic_write(lease_dir / f"residue-{n}.hold", "", restrict_to_owner=True)
    monkeypatch.setattr(projection, "_PROJECTION_LEASE_SCAN_LIMIT", 3, raising=False)

    with caplog.at_level("INFO", logger=projection.logger.name):
        live, uncertain = projection._scan_projection_leases(agents)

    assert live == set() and uncertain is True
    remaining = {p.name for p in lease_dir.glob("*.json")}
    reclaimed = 3 - len(remaining)
    assert f"lease scan reached its 3-entry ceiling; reclaimed {reclaimed} stale" in caplog.text


def test_lease_scan_stops_on_its_deadline_and_answers_uncertain(native_tree, monkeypatch, caplog):
    """A count alone cannot bound wall-clock time under the publication lock.

    The entry cap bounds how many parse-plus-lock-probe steps run, not how long a
    slow filesystem takes over them, so the scan also checks a deadline between
    entries. Expiry is treated like the cap: residue validated before it drains,
    nothing past it is examined, and the pass answers uncertain so no alias is
    deleted. The clock jumps past the budget once the first entry has been
    examined, so no sleep is needed.
    """
    _home, agents, _project = native_tree
    lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
    lease_dir.mkdir()
    for n in range(3):
        projection.atomic_write(
            lease_dir / f"residue-{n}.json",
            json.dumps({"aliases": [f"kirocrew-skill-view-{n}"]}),
            restrict_to_owner=True,
        )
        projection.atomic_write(lease_dir / f"residue-{n}.hold", "", restrict_to_owner=True)
    budget = projection._PROJECTION_LEASE_SCAN_MAX_SECONDS
    calls = [0]

    def clock():
        # The deadline read and the first entry's check see t=0; every later
        # check sees the budget spent.
        calls[0] += 1
        return 0.0 if calls[0] <= 2 else budget

    probed: list[str] = []
    real_probe = projection._probe_projection_lease

    def probe(path):
        probed.append(path.name)
        return real_probe(path)

    monkeypatch.setattr(projection.time, "monotonic", clock)
    monkeypatch.setattr(projection, "_probe_projection_lease", probe)

    with caplog.at_level("INFO", logger=projection.logger.name):
        live, uncertain = projection._scan_projection_leases(agents)

    assert live == set() and uncertain is True
    assert len(probed) <= 1, "the scan probed a lease after its deadline was spent"
    assert "lease scan spent its 0.4-second budget after 1 entr(ies)" in caplog.text
    remaining = {p.name for p in lease_dir.iterdir()}
    assert len(remaining) >= 4, "the scan reclaimed residue it never examined"


def test_candidate_walk_deferral_is_logged_at_info(native_tree, monkeypatch, caplog):
    """Past the candidate window an alias is not promised a turn, so say so at INFO.

    The lease-scan ceiling is already INFO; the candidate-walk ceiling reports
    the same kind of deferral and must be visible the same way.
    """
    _home, agents, _project = native_tree
    for n in range(3):
        _legacy_alias(agents, f"{n:024x}")
    monkeypatch.setattr(projection, "_PROJECTION_PRUNE_WORK_LIMIT", 1)

    with caplog.at_level("INFO", logger=projection.logger.name):
        candidates = list(projection._projection_prune_candidates(agents, set()))

    assert len(candidates) == 1
    assert any(
        record.levelname == "INFO" and "deferred remaining alias pruning" in record.getMessage()
        for record in caplog.records
    )


def test_capped_lease_scan_counts_only_fully_removed_pairs(native_tree, monkeypatch, caplog):
    """A lease whose holder unlink fails is still half present, so it is not reclaimed.

    The cap log reports how much of a backlog drained; counting a pair whose
    ``.hold`` survived would report progress the next scan will not see.
    """
    _home, agents, _project = native_tree
    lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
    lease_dir.mkdir()
    for n in range(3):
        projection.atomic_write(
            lease_dir / f"residue-{n}.json",
            json.dumps({"aliases": [f"kirocrew-skill-view-{n}"]}),
            restrict_to_owner=True,
        )
        projection.atomic_write(lease_dir / f"residue-{n}.hold", "", restrict_to_owner=True)
    monkeypatch.setattr(projection, "_PROJECTION_LEASE_SCAN_LIMIT", 4, raising=False)
    real_unlink = projection._unlink_projection_lease_if_unchanged

    def holder_unlink_fails(path, identity):
        if path.name.endswith(projection._PROJECTION_LEASE_HOLDER_SUFFIX):
            return False
        return real_unlink(path, identity)

    monkeypatch.setattr(projection, "_unlink_projection_lease_if_unchanged", holder_unlink_fails)

    with caplog.at_level("INFO", logger=projection.logger.name):
        live, uncertain = projection._scan_projection_leases(agents)

    assert live == set() and uncertain is True
    # Every holder survived its failed unlink, so each record is kept too: the
    # pair is retried whole by the next scan and no pair counts as reclaimed.
    assert len(list(lease_dir.glob("*.json"))) == 3
    assert len(list(lease_dir.glob("*.hold"))) == 3
    assert "lease scan reached its 4-entry ceiling; reclaimed 0 stale" in caplog.text


def test_unreadable_lease_record_still_drains_validated_residue(native_tree, monkeypatch, caplog):
    """One persistent bad record must not stop stale-lease cleanup forever.

    The pass stays uncertain, so no alias is deleted, but the residue it already
    lock-validated is reclaimed and the blocking record is reported at INFO.
    """
    _home, agents, _project = native_tree
    lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
    lease_dir.mkdir()
    stale = lease_dir / "a-stale.json"
    stale_holder = lease_dir / "a-stale.hold"
    projection.atomic_write(
        stale, json.dumps({"aliases": ["kirocrew-skill-view-stale"]}), restrict_to_owner=True
    )
    projection.atomic_write(stale_holder, "", restrict_to_owner=True)
    bad = lease_dir / "b-bad.json"
    projection.atomic_write(bad, "not json", restrict_to_owner=True)
    projection.atomic_write(lease_dir / "b-bad.hold", "", restrict_to_owner=True)
    real_scandir = projection.os.scandir

    def ordered_scandir(path, *args, **kwargs):
        if Path(path) == lease_dir:
            names = [stale.name, stale_holder.name, bad.name, "b-bad.hold"]

            class Entries:
                def __enter__(self):
                    return iter(SimpleNamespace(name=name) for name in names)

                def __exit__(self, *_args):
                    return None

            return Entries()
        return real_scandir(path, *args, **kwargs)

    with monkeypatch.context() as scan_patch:
        scan_patch.setattr(projection.os, "scandir", ordered_scandir)
        with caplog.at_level("INFO", logger=projection.logger.name):
            live, uncertain = projection._scan_projection_leases(agents)

    assert live == set() and uncertain is True
    assert not stale.exists() and not stale_holder.exists()
    assert bad.exists()
    assert "lease record b-bad.json is unreadable; reclaimed 1 stale lease(s)" in caplog.text


def test_unreadable_lease_record_does_not_hide_stale_leases_after_it(native_tree, monkeypatch):
    """A bad record EARLY in directory order must not stop the walk.

    Stopping at the first unreadable record left every stale lease behind it
    unreclaimed on every stable-order scan, for as long as the bad record stayed.
    The pass stays uncertain, so no alias is deleted, but the stale lease after
    the bad record is reclaimed.
    """
    _home, agents, _project = native_tree
    lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
    lease_dir.mkdir()
    bad = lease_dir / "a-bad.json"
    projection.atomic_write(bad, "not json", restrict_to_owner=True)
    projection.atomic_write(lease_dir / "a-bad.hold", "", restrict_to_owner=True)
    stale = lease_dir / "b-stale.json"
    stale_holder = lease_dir / "b-stale.hold"
    projection.atomic_write(
        stale, json.dumps({"aliases": ["kirocrew-skill-view-stale"]}), restrict_to_owner=True
    )
    projection.atomic_write(stale_holder, "", restrict_to_owner=True)
    real_scandir = projection.os.scandir

    def ordered_scandir(path, *args, **kwargs):
        if Path(path) == lease_dir:
            names = [bad.name, "a-bad.hold", stale.name, stale_holder.name]

            class Entries:
                def __enter__(self):
                    return iter(SimpleNamespace(name=name) for name in names)

                def __exit__(self, *_args):
                    return None

            return Entries()
        return real_scandir(path, *args, **kwargs)

    with monkeypatch.context() as scan_patch:
        scan_patch.setattr(projection.os, "scandir", ordered_scandir)
        live, uncertain = projection._scan_projection_leases(agents)

    assert live == set() and uncertain is True
    assert not stale.exists() and not stale_holder.exists()
    assert bad.exists()


def test_record_less_holder_litter_is_reclaimed_and_cannot_pin_the_cap(native_tree, monkeypatch):
    """A ``.hold`` whose record is gone names no alias but consumes the ceiling.

    Left alone, enough of it would cap every scan and defer pruning forever, so
    an unlocked record-less holder is reclaimed as residue. A held one is kept.
    """
    _home, agents, _project = native_tree
    lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
    lease_dir.mkdir()
    orphans = [lease_dir / f"{1000 + n}-{uuid.uuid4().hex}.hold" for n in range(4)]
    for orphan in orphans:
        projection.atomic_write(orphan, "", restrict_to_owner=True)
    held = projection._acquire_projection_lease(agents, {"kirocrew-skill-view-live"})
    try:
        monkeypatch.setattr(projection, "_PROJECTION_LEASE_SCAN_LIMIT", 3, raising=False)
        drained = 0
        for _ in range(4):
            live, uncertain = projection._scan_projection_leases(agents)
            if not uncertain:
                break
            drained += 1
        assert not any(orphan.exists() for orphan in orphans)
        assert live == {"kirocrew-skill-view-live"} and uncertain is False
        assert drained >= 1, "the ceiling was never reached, so the litter was not exercised"
        # The live lease's own pair is untouched.
        assert len(list(lease_dir.glob("*.json"))) == 1
        assert len(list(lease_dir.glob("*.hold"))) == 1
    finally:
        held.close()


def test_record_less_holder_reclaim_spares_files_the_writer_did_not_name(native_tree):
    """Only a ``<pid>-<uuid4 hex>`` holder is ever reclaimed as record-less litter.

    An empty ``.hold`` carries no proof that this module wrote it, so a file
    such as an operator's ``notes.hold`` in the lease directory must survive a
    scan; the old head unlinked every unlocked record-less ``*.hold``. A
    writer-shaped stem with content survives too: the writer only publishes
    empty holders, and head ``f0ecf1e46`` unlinked it on the name alone.
    """
    _home, agents, _project = native_tree
    lease_dir = agents / projection._PROJECTION_LEASE_DIR_NAME
    lease_dir.mkdir()
    foreign = [
        lease_dir / "notes.hold",
        lease_dir / f"{uuid.uuid4().hex}.hold",
        lease_dir / f"12-{uuid.uuid4().hex.upper()}.hold",
        lease_dir / f"12-{uuid.uuid4().hex}-copy.hold",
    ]
    # The writer's exact stem shape, but with content: the module only ever
    # publishes EMPTY holders, so the name alone does not prove authorship.
    foreign.append(lease_dir / f"34-{uuid.uuid4().hex}.hold")
    for path in foreign:
        path.write_text("operator data", encoding="utf-8")
    ours = lease_dir / f"12-{uuid.uuid4().hex}.hold"
    projection.atomic_write(ours, "", restrict_to_owner=True)

    live, uncertain = projection._scan_projection_leases(agents)

    assert live == set() and uncertain is False
    assert not ours.exists()
    for path in foreign:
        assert path.read_text(encoding="utf-8") == "operator data", path.name


def test_census_truncates_wherever_the_reclaim_lease_scan_caps(native_tree, monkeypatch):
    """The census and the reclaim scan share one entry-level lease ceiling.

    The scan charges every lease-directory entry (records AND ``.hold``
    sidecars), so a directory of held pairs can cap it -- and defer every
    prune -- while the record count alone is still under the census bound.
    The census must then report ``truncated`` instead of promising a drain.
    """
    _home, agents, _project = native_tree
    projection.atomic_write(
        agents / "kirocrew-skill-view-orphan.json", "{}", restrict_to_owner=True
    )
    leases = [
        projection._acquire_projection_lease(agents, {f"kirocrew-skill-view-live{n}"})
        for n in range(3)
    ]
    try:
        # Three records (under the census's own record bound) but six entries.
        monkeypatch.setattr(projection, "_PROJECTION_LEASE_SCAN_LIMIT", 4, raising=False)
        _live, uncertain = projection._scan_projection_leases(agents)
        assert uncertain is True
        census = projection.census_projected_aliases(agents)
        assert census["truncated"] == 1
    finally:
        for lease in leases:
            lease.close()


# ── A refused unlink is reported, not swallowed ──


_dir_fd_seam_only = pytest.mark.skipif(
    os.name != "posix",
    reason="these drive the dir_fd unlink seam; Windows unlinks by name and reports the "
    "parent open instead, so the injected errno never reaches the reporter there",
)


def _seed_backlog(agents, count):
    """*count* reclaimable legacy aliases, backdated past the minimum age."""
    return [_legacy_alias(agents, f"{n:024x}") for n in range(count)]


@_dir_fd_seam_only
def test_a_refused_unlink_is_reported_once_per_interval_with_its_count(
    native_tree, monkeypatch, caplog
):
    """Silence here is what sent an outside report after the wrong root cause.

    A permission or read-only refusal answering ``False`` like a deliberate keep,
    with nothing logged, makes an agents directory this process cannot write look
    exactly like one with nothing to reclaim. One warning per interval,
    carrying how many refusals it stands for -- never one line per file.
    """
    _home, agents, _project = native_tree
    backlog = _seed_backlog(agents, 5)
    monkeypatch.setattr(projection, "_UNLINK_WARNING_LAST", 0.0)
    monkeypatch.setattr(projection, "_UNLINK_WARNING_SUPPRESSED", 0)
    monkeypatch.setattr(projection.platform_compat, "IS_WINDOWS", False)

    def refuse(*args, on_error=None, **kwargs):
        # What pinned_fs.unlink_verified does when the OS refuses the unlink
        # itself: report through the seam, answer False.
        assert on_error is not None, "the prune did not ask to hear about a refusal"
        on_error(PermissionError(13, "Permission denied"))
        return False

    monkeypatch.setattr(projection.pinned_fs, "unlink_verified", refuse)
    monkeypatch.setattr(projection.pinned_fs, "supports_pinned_walk", lambda: True)
    monkeypatch.setattr(projection.os, "supports_dir_fd", {projection.os.unlink})
    with caplog.at_level("WARNING", logger=projection.logger.name):
        projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set())
        warnings = [r for r in caplog.records if "cannot remove stale alias" in r.getMessage()]
        assert len(warnings) == 1, "a refused unlink was either silent or logged per file"
        assert "unlink refused, EACCES (Permission denied)" in warnings[0].getMessage()
        assert "is not writable by this process" in warnings[0].getMessage()
        assert "0 similar refusal(s)" in warnings[0].getMessage()
        assert all(p.exists() for p in backlog)

        # Past the interval the next refusal reports again, carrying the ones it
        # swallowed in between.
        monkeypatch.setattr(
            projection,
            "_UNLINK_WARNING_LAST",
            time.monotonic() - projection._UNLINK_WARNING_INTERVAL_SECS - 1,
        )
        projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set())
        warnings = [r for r in caplog.records if "cannot remove stale alias" in r.getMessage()]
        assert len(warnings) == 2
        assert "4 similar refusal(s)" in warnings[1].getMessage()


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (FileNotFoundError(errno.ENOENT, "No such file or directory"), "ENOENT"),
        (OSError(errno.EMFILE, "Too many open files"), "EMFILE"),
        (OSError(errno.EIO, "Input/output error"), "EIO"),
    ],
)
@_dir_fd_seam_only
def test_a_refusal_that_is_not_a_permission_problem_is_worded_neutrally(
    native_tree, monkeypatch, caplog, exc, code
):
    """Only EACCES/EPERM/EROFS mean the directory is unwritable.

    A prune in another process can take the file between the walk's stat and this
    unlink (ENOENT), or the process can be out of descriptors (EMFILE); calling
    either "the directory is not writable" sends an operator to fix a mount or an
    owner that is fine. The line still says what failed and with which errno,
    through the same throttle and count.
    """
    _home, agents, _project = native_tree
    backlog = _seed_backlog(agents, 2)
    monkeypatch.setattr(projection, "_UNLINK_WARNING_LAST", 0.0)
    monkeypatch.setattr(projection, "_UNLINK_WARNING_SUPPRESSED", 0)
    monkeypatch.setattr(projection.platform_compat, "IS_WINDOWS", False)

    def refuse(*args, on_error=None, **kwargs):
        assert on_error is not None
        on_error(exc)
        return False

    monkeypatch.setattr(projection.pinned_fs, "unlink_verified", refuse)
    monkeypatch.setattr(projection.pinned_fs, "supports_pinned_walk", lambda: True)
    monkeypatch.setattr(projection.os, "supports_dir_fd", {projection.os.unlink})
    with caplog.at_level("WARNING", logger=projection.logger.name):
        projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set())
    warnings = [r for r in caplog.records if "cannot remove stale alias" in r.getMessage()]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert f"unlink refused, {code} ({exc.strerror})" in message
    assert str(agents) in message and "0 similar refusal(s)" in message
    assert "not writable" not in message
    assert projection._UNLINK_WARNING_SUPPRESSED == 1, "the second refusal was not counted"
    assert all(p.exists() for p in backlog)


@_dir_fd_seam_only
@pytest.mark.parametrize("code", [errno.EPERM, errno.EROFS])
def test_every_permission_class_errno_is_diagnosed_as_unwritable(
    native_tree, monkeypatch, caplog, code
):
    _home, agents, _project = native_tree
    _seed_backlog(agents, 1)
    monkeypatch.setattr(projection, "_UNLINK_WARNING_LAST", 0.0)
    monkeypatch.setattr(projection, "_UNLINK_WARNING_SUPPRESSED", 0)
    monkeypatch.setattr(projection.platform_compat, "IS_WINDOWS", False)

    def refuse(*args, on_error=None, **kwargs):
        on_error(OSError(code, os.strerror(code)))
        return False

    monkeypatch.setattr(projection.pinned_fs, "unlink_verified", refuse)
    monkeypatch.setattr(projection.pinned_fs, "supports_pinned_walk", lambda: True)
    monkeypatch.setattr(projection.os, "supports_dir_fd", {projection.os.unlink})
    with caplog.at_level("WARNING", logger=projection.logger.name):
        projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set())
    (warning,) = [r for r in caplog.records if "cannot remove stale alias" in r.getMessage()]
    assert errno.errorcode[code] in warning.getMessage()
    assert "is not writable by this process" in warning.getMessage()


@_dir_fd_seam_only
def test_a_refused_lease_unlink_reports_through_the_same_throttle(native_tree, monkeypatch, caplog):
    """The lease and sidecar unlinks share the alias reporter, not a second one.

    The same read-only or foreign-owned directory that refuses an alias unlink
    refuses its lease record, holder and ownership sidecar, and a rule applied
    to one of two paths is the same silence on the other. One reporter, one
    interval, one suppressed count: a refused lease unlink is the first line,
    the refused alias unlinks in the same interval are the count on the next.
    """
    _home, agents, _project = native_tree
    backlog = _seed_backlog(agents, 3)
    monkeypatch.setattr(projection, "_UNLINK_WARNING_LAST", 0.0)
    monkeypatch.setattr(projection, "_UNLINK_WARNING_SUPPRESSED", 0)
    monkeypatch.setattr(projection.platform_compat, "IS_WINDOWS", False)

    def refuse(*args, on_error=None, **kwargs):
        assert on_error is not None, "the lease unlink did not ask to hear about a refusal"
        on_error(PermissionError(13, "Permission denied"))
        return False

    monkeypatch.setattr(projection.pinned_fs, "unlink_verified", refuse)
    monkeypatch.setattr(projection.pinned_fs, "supports_pinned_walk", lambda: True)
    monkeypatch.setattr(projection.os, "supports_dir_fd", {projection.os.unlink})
    # Off the prune's walk, so the count below is exactly the alias refusals.
    lease_dir = agents.parent / "elsewhere" / projection._PROJECTION_LEASE_DIR_NAME
    lease_dir.mkdir(parents=True)
    record = lease_dir / f"1-{'ab' * 16}{projection._PROJECTION_LEASE_RECORD_SUFFIX}"
    record.write_text('{"aliases":[]}')
    info = os.stat(record)
    identity = (info.st_dev, info.st_ino)
    with caplog.at_level("WARNING", logger=projection.logger.name):
        assert (
            projection._unlink_projection_lease_if_unchanged(
                record, identity, what="stale lease record"
            )
            is False
        )
        warnings = [r for r in caplog.records if "cannot remove" in r.getMessage()]
        assert len(warnings) == 1, "a refused lease unlink was silent"
        message = warnings[0].getMessage()
        assert "stale lease record" in message and record.name in message
        assert "unlink refused, EACCES (Permission denied)" in message
        assert "is not writable by this process" in message and str(lease_dir) in message
        assert "0 similar refusal(s)" in message
        assert record.exists()

        # Within the interval the alias refusals are swallowed into the SAME count.
        projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set())
        assert len([r for r in caplog.records if "cannot remove" in r.getMessage()]) == 1
        assert all(p.exists() for p in backlog)
        monkeypatch.setattr(
            projection,
            "_UNLINK_WARNING_LAST",
            time.monotonic() - projection._UNLINK_WARNING_INTERVAL_SECS - 1,
        )
        assert (
            projection._unlink_projection_lease_if_unchanged(
                record, identity, what="stale lease record"
            )
            is False
        )
        warnings = [r for r in caplog.records if "cannot remove" in r.getMessage()]
        assert len(warnings) == 2
        assert "3 similar refusal(s)" in warnings[1].getMessage()

    # An identity change is a deliberate keep and stays silent: no report, no count.
    caplog.clear()
    changed = (identity[0], identity[1] + 1)
    with caplog.at_level("WARNING", logger=projection.logger.name):
        assert projection._unlink_projection_lease_if_unchanged(record, changed) is False
    assert not [r for r in caplog.records if "cannot remove" in r.getMessage()]
    assert projection._UNLINK_WARNING_SUPPRESSED == 0


@pytest.mark.skipif(
    not projection.pinned_fs.supports_pinned_walk() or os.name != "posix",
    reason="a directory without write permission is a POSIX way to refuse an unlink",
)
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permission bits"
)
def test_a_read_only_agents_directory_produces_the_refusal_warning(
    native_tree, monkeypatch, caplog
):
    """End to end through the real unlink: the reported field case, with no seam.

    The prune classifies the alias as reclaimable, the kernel refuses the unlink,
    and the warning names the directory -- which is what an operator needs to
    stop looking for a code bug and fix a mount or an owner.
    """
    _home, agents, _project = native_tree
    backlog = _seed_backlog(agents, 3)
    monkeypatch.setattr(projection, "_UNLINK_WARNING_LAST", 0.0)
    monkeypatch.setattr(projection, "_UNLINK_WARNING_SUPPRESSED", 0)
    original_mode = stat.S_IMODE(os.stat(agents).st_mode)
    os.chmod(agents, original_mode & ~stat.S_IWUSR)
    try:
        with caplog.at_level("WARNING", logger=projection.logger.name):
            projection._prune_stale_managed_aliases(agents, _crew_home_id(), keep=set())
    finally:
        os.chmod(agents, original_mode)
    warnings = [r for r in caplog.records if "cannot remove stale alias" in r.getMessage()]
    assert len(warnings) == 1
    assert str(agents) in warnings[0].getMessage()
    # The kernel's real answer is EACCES, the one case the line may diagnose as
    # an unwritable directory.
    assert "EACCES" in warnings[0].getMessage()
    assert "is not writable by this process" in warnings[0].getMessage()
    assert all(p.exists() for p in backlog)
