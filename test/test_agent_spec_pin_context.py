"""Execution context and side-session readers share the selected spec's contents."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import agent, agent_discovery, agent_spec_pins
from kiro_crew.context_assembly.agent_spec import _load_agent_prompt
from kiro_crew.dashboard.side_readonly_spec import ReadOnlySpecError, _read_base_spec
from kiro_crew.member_essential_context import (
    MemberEssentialContextError,
    read_template_spec,
    resolve_template_path,
)


@pytest.fixture
def pinned_context(tmp_path, monkeypatch):
    directory = tmp_path / "user" / ".kiro" / "agents"
    directory.mkdir(parents=True)
    for label in ("A", "B"):
        (directory / f"Pkg{label}-helper.json").write_text(
            json.dumps(
                {
                    "name": "helper",
                    "prompt": f"Prompt {label}",
                    "tools": ["read"],
                    "resources": [f"skill://skills/{label}/SKILL.md"],
                }
            ),
            encoding="utf-8",
        )
    pins = {"helper": "PkgB-helper.json"}
    monkeypatch.setattr(agent_spec_pins, "spec_pins", lambda: dict(pins))
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: directory)
    monkeypatch.setattr(agent_discovery, "_KIRO_AGENTS_DIR", directory)
    return directory, pins


def test_context_prompt_uses_the_pinned_file(pinned_context):
    assert _load_agent_prompt("helper") == "Prompt B"


def test_member_essentials_resolve_the_pinned_template(pinned_context):
    directory, _pins = pinned_context
    path, spec = read_template_spec("helper", None, operation="member_essentials", source="context")
    assert path == directory / "PkgB-helper.json"
    assert spec["prompt"] == "Prompt B"


def test_mate_scope_template_read_follows_the_pin(pinned_context, tmp_path, monkeypatch):
    from kiro_crew.config import paths

    directory, _pins = pinned_context
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: empty)
    monkeypatch.setattr(paths, "mate_agents_dir", lambda: directory)
    assert resolve_template_path("helper") == directory / "PkgB-helper.json"


def test_readonly_side_session_uses_the_pinned_base(pinned_context):
    spec, project = _read_base_spec("helper", None)
    assert spec["prompt"] == "Prompt B"
    assert spec["resources"] == ["skill://skills/B/SKILL.md"]
    assert project is None


@pytest.mark.parametrize("with_project", [False, True])
def test_skill_scope_uses_the_pinned_file_and_tracks_pin_changes(
    pinned_context, tmp_path, with_project
):
    directory, pins = pinned_context
    project = tmp_path / "project" if with_project else None
    if project is not None:
        project.mkdir()
    anchor = project or directory.parent.parent
    for label in ("B", "A"):
        pins["helper"] = f"Pkg{label}-helper.json"
        assert agent_discovery.agent_skill_globs(
            "helper", agents_dir=directory, project_dir=project, strict=True
        ) == [str(anchor / "skills" / label / "SKILL.md")]


@pytest.mark.parametrize("pin", [None, "missing.json"])
@pytest.mark.parametrize("with_project", [False, True])
def test_unresolved_skill_scope_never_uses_another_claimant(
    pinned_context, tmp_path, pin, with_project
):
    directory, pins = pinned_context
    pins.clear()
    if pin is not None:
        pins["helper"] = pin
    project = tmp_path / "project" if with_project else None
    if project is not None:
        project.mkdir()
    with pytest.raises(agent_discovery.SkillScopeResolutionError):
        agent_discovery.agent_skill_globs(
            "helper", agents_dir=directory, project_dir=project, strict=True
        )
    assert (
        agent_discovery.agent_skill_globs("helper", agents_dir=directory, project_dir=project) == []
    )


def test_project_spec_precedes_the_user_pin(pinned_context, tmp_path):
    directory, pins = pinned_context
    pins["helper"] = "missing.json"
    project = tmp_path / "project"
    specs = project / ".kiro" / "agents"
    specs.mkdir(parents=True)
    (specs / "helper.json").write_text(
        json.dumps(
            {
                "name": "helper",
                "prompt": "Project prompt",
                "resources": ["skill://skills/project/SKILL.md"],
            }
        ),
        encoding="utf-8",
    )
    assert _load_agent_prompt("helper", str(project)) == "Project prompt"
    assert agent_discovery.agent_skill_globs(
        "helper", agents_dir=directory, project_dir=project, strict=True
    ) == [str(project / "skills" / "project" / "SKILL.md")]
    assert _read_base_spec("helper", str(project))[0]["prompt"] == "Project prompt"


def test_broken_pin_refuses_template_and_readonly_base(pinned_context):
    _directory, pins = pinned_context
    pins["helper"] = "missing.json"
    with pytest.raises(MemberEssentialContextError):
        resolve_template_path("helper")
    with pytest.raises(ReadOnlySpecError):
        _read_base_spec("helper", None)


def test_cached_claim_cannot_skip_a_consumer_fence(pinned_context):
    directory, _pins = pinned_context
    with pytest.raises(TypeError, match="parsed_rows and fence"):
        agent_discovery.declared_spec_claim(
            directory,
            "helper",
            operation="agent_skill_globs",
            source="test",
            parsed_rows=[],
            fence=lambda path: True,
        )


def _link_claimant(directory: Path, store: Path) -> None:
    """Replace ``PkgA-helper.json`` with a link to the same content under *store*."""
    store.mkdir()
    real = store / "PkgA-helper.json"
    real.write_text((directory / "PkgA-helper.json").read_text(encoding="utf-8"), encoding="utf-8")
    (directory / "PkgA-helper.json").unlink()
    (directory / "PkgA-helper.json").symlink_to(real)


def test_a_linked_pinned_claimant_is_read_behind_the_linked_fence(pinned_context, tmp_path):
    """A pinned link is no path answer, never the other claimant: readers take it
    through their linked reader, which applies their own fence and the pin."""
    directory, pins = pinned_context
    _link_claimant(directory, directory / "store")
    pins["helper"] = "PkgA-helper.json"
    assert agent.session_agent_spec_path("helper") is None
    path, spec = read_template_spec("helper", None, operation="member_essentials", source="context")
    assert path == directory / "PkgA-helper.json"
    assert spec["prompt"] == "Prompt A"
    assert _read_base_spec("helper", None)[0]["prompt"] == "Prompt A"


def test_a_pinned_link_the_fence_refuses_never_reads_the_other_claimant(
    pinned_context, tmp_path, monkeypatch
):
    """A pinned link whose target the managed-state fence refuses is refused, not
    swapped for the one claimant the fence still admits."""
    import kiro_crew.member_essential_context as essential

    directory, pins = pinned_context
    _link_claimant(directory, tmp_path / "managed")
    pins["helper"] = "PkgA-helper.json"
    monkeypatch.setattr(
        essential,
        "_managed_template_target_refused",
        lambda _agents_dir: lambda path: "managed" in Path(path).resolve().parts,
    )
    with pytest.raises(MemberEssentialContextError):
        read_template_spec("helper", None, operation="member_essentials", source="context")
    with pytest.raises(ReadOnlySpecError):
        _read_base_spec("helper", None)


def test_a_pin_on_the_lone_claimant_keeps_its_overlay(pinned_context, tmp_path):
    """A pin naming the only claimant has nothing to choose, so the overlay lookup
    finds that file's overlay instead of pooling nothing."""
    from kiro_crew.mcp_gateway.session_servers import _load_overlay_for_agent

    directory, pins = pinned_context
    (directory / "PkgA-helper.json").unlink()
    pins["helper"] = "PkgB-helper.json"
    overlays = tmp_path / "overlays"
    overlays.mkdir()
    overlay = {"name": "helper", "mcpServers": {}}
    (overlays / "PkgB-helper.json").write_text(json.dumps(overlay), encoding="utf-8")
    assert _load_overlay_for_agent(overlays, "helper") == overlay


def test_a_projection_refusal_keeps_the_claimants_for_the_chooser(pinned_context):
    """A kiro-cli start refused for an unpinned contest still carries the
    resolver's exception, so the dashboard can render the file chooser."""
    from kiro_crew.acp.skill_projection_parts.native_projection import NativeSkillProjection
    from kiro_crew.acp.skill_projection_parts.preparation import _pinned_source
    from kiro_crew.agent_spec_pin_ops import ambiguity_meta

    directory, pins = pinned_context
    pins.clear()
    errors: dict[str, str] = {}
    refusals: dict[str, ValueError] = {}
    assert _pinned_source(directory, "helper", errors, refusals) is None
    projection = NativeSkillProjection({}, errors=errors, refusals=refusals)
    with pytest.raises(ValueError) as raised:
        projection.spawn_agent("helper")
    meta = ambiguity_meta(raised.value)
    assert meta is not None
    assert meta["agent"] == "helper"
    assert sorted(row["file"] for row in meta["claimants"]) == [
        "PkgA-helper.json",
        "PkgB-helper.json",
    ]
