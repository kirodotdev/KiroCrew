"""An unreadable ``cli.json`` makes the spawn fall back to the authored agent.

``prepare_native_skill_projection`` reads the global and the workspace Kiro settings.
When either file is present but cannot be read (the credential gate refuses it, a
stalled mount), the preparation returns ``None`` -- no view, the authored agent -- and
writes nothing, the same answer :func:`inherits_default_resources` gives. The read
error never reaches the spawn. An absent file is the normal case and projects.
"""

from __future__ import annotations

import json

import native_skill_projection_helpers
import pytest

from kiro_crew.acp import skill_projection as projection

_prune_on_every_spawn = native_skill_projection_helpers._prune_on_every_spawn
_no_eager_prune = native_skill_projection_helpers._no_eager_prune
_end_projections_before_tmp_path_goes = (
    native_skill_projection_helpers._end_projections_before_tmp_path_goes
)
native_tree = native_skill_projection_helpers.native_tree


def _refuse_reads_of(monkeypatch, refused):
    """Make the credential gate refuse every read of *refused*, as on the reporter's host."""
    real = projection.safe_read_file_bytes
    state = {"refused": str(refused)}

    def gate(path, *args, **kwargs):
        if str(path) == state["refused"]:
            return None
        return real(path, *args, **kwargs)

    monkeypatch.setattr(projection, "safe_read_file_bytes", gate)
    monkeypatch.setattr(projection, "_settings_read_sleep", lambda _pause: None)
    return state


def _published_aliases(agents):
    return sorted(
        p.name for p in agents.iterdir() if p.name.startswith(projection.NATIVE_SKILL_ALIAS_PREFIX)
    )


@pytest.fixture
def custom_agent(native_tree):
    home, agents, project = native_tree
    (agents / "custom.json").write_text(
        json.dumps({"name": "custom", "resources": ["file://RULES.md"]}), encoding="utf-8"
    )
    return home, agents, project


def test_unreadable_global_settings_fall_back_without_raising(custom_agent, monkeypatch):
    home, agents, project = custom_agent
    global_settings = home / "settings" / "cli.json"
    global_settings.parent.mkdir(parents=True, exist_ok=True)
    global_settings.write_text("{}", encoding="utf-8")
    _refuse_reads_of(monkeypatch, global_settings)

    assert projection.prepare_native_skill_projection(project, enabled=True) is None
    # Nothing is published and the workspace overlay is never written.
    assert _published_aliases(agents) == []
    assert not (project / ".kiro" / "settings" / "cli.json").exists()
    # The read-only twin gives the same answer for the same file.
    assert projection.inherits_default_resources(project) is True


def test_unreadable_workspace_settings_fall_back_and_stay_untouched(custom_agent, monkeypatch):
    _home, agents, project = custom_agent
    workspace_settings = project / ".kiro" / "settings" / "cli.json"
    workspace_settings.parent.mkdir(parents=True)
    original = json.dumps({"toolSearch.enabled": False})
    workspace_settings.write_text(original, encoding="utf-8")
    gate = _refuse_reads_of(monkeypatch, workspace_settings)

    assert projection.prepare_native_skill_projection(project, enabled=True) is None
    assert _published_aliases(agents) == []
    assert workspace_settings.read_text(encoding="utf-8") == original
    assert projection.inherits_default_resources(project) is True

    # The locks were released: once the file reads again, the next spawn projects.
    gate["refused"] = None
    prepared = projection.prepare_native_skill_projection(project, enabled=True)
    assert prepared is not None and prepared.agent("custom")


def test_unreadable_workspace_settings_skip_the_disabled_rollback(custom_agent, monkeypatch):
    _home, agents, project = custom_agent
    workspace_settings = project / ".kiro" / "settings" / "cli.json"
    workspace_settings.parent.mkdir(parents=True)
    original = json.dumps({projection._INHERIT_SETTING: True})
    workspace_settings.write_text(original, encoding="utf-8")
    _refuse_reads_of(monkeypatch, workspace_settings)

    assert projection.prepare_native_skill_projection(project, enabled=False) is None
    assert workspace_settings.read_text(encoding="utf-8") == original
    assert _published_aliases(agents) == []


def test_absent_settings_still_project(custom_agent):
    _home, _agents, project = custom_agent
    prepared = projection.prepare_native_skill_projection(project, enabled=True)
    assert prepared is not None and prepared.agent("custom")
