"""The derived read-only agent spec a side turn runs under (``side_readonly_spec``).

Every test runs the real module against a temporary kiro agent registry
(``agent.KIRO_AGENTS_DIR``), never the live one.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.dashboard import side_readonly_spec as srs

_SHIPPED_DEFAULTS = Path(__file__).resolve().parents[1] / "src/kiro_crew/config/defaults.json"

#: A base spec carrying every grant shape the derivation must empty, plus the
#: fields it must keep byte-for-byte.
_BASE = {
    "name": "kirocrew",
    "description": "the default agent",
    "model": "auto",
    "prompt": "You are the crew.",
    "tools": [
        "execute_bash",
        "fs_read",
        "fs_write",
        "use_subagent",
        "@kirocrew-cron",
        "@kirocrew-core",
    ],
    "allowedTools": ["fs_read", "@kirocrew-cron/cron_remove_all", "@kirocrew-core"],
    "mcpServers": {
        "kirocrew-core": {"command": "kirocrew", "args": ["mcp", "core"], "autoApprove": ["*"]},
        "notes": {"command": "notes-mcp", "args": []},
    },
    "toolsSettings": {
        "execute_bash": {"allowedCommands": ["git status"], "deniedCommands": ["rm -rf *"]},
        "shell": {"autoAllowReadonly": True},
        "fs_write": {"allowedPaths": ["~/notes/**"]},
        "subagent": {"availableAgents": ["composer"], "trustedAgents": ["composer"]},
    },
    "resources": ["file://.kiro/steering/**/*.md"],
    "includeMcpJson": True,
    "autoAllowReadonly": True,
    "hooks": {"auto_approve_tools": ["Read*"], "postToolUse": [{"matcher": "x", "command": "y"}]},
    "permissions": {"rules": [{"tool": "fs_read", "decision": "allow"}]},
}

#: The keys derivation rewrites or drops; everything else must survive byte-for-byte.
_REWRITTEN_TOP_LEVEL = {
    "name",
    "description",
    "allowedTools",
    "includeMcpJson",
    "autoAllowReadonly",
    "permissions",
    "hooks",
}


def test_derived_spec_empties_every_grant_and_keeps_everything_else():
    """(a) ``allowedTools`` is empty and the spec is otherwise the base, less grants."""
    base = copy.deepcopy(_BASE)
    derived = srs.derive_readonly_spec(base, base_name="kirocrew")

    assert derived["name"] == "kirocrew--readonly"
    assert derived["description"].startswith(srs.OWNER_MARKER)
    assert "the default agent" in derived["description"]  # the base's own description is kept in it
    assert derived["allowedTools"] == []
    # Every no-prompt execution channel is gone…
    assert "autoApprove" not in derived["mcpServers"]["kirocrew-core"]
    assert "allowedCommands" not in derived["toolsSettings"]["execute_bash"]
    assert "autoAllowReadonly" not in derived["toolsSettings"]["shell"]
    assert "allowedPaths" not in derived["toolsSettings"]["fs_write"]
    assert "trustedAgents" not in derived["toolsSettings"]["subagent"]
    assert derived["includeMcpJson"] is False
    assert derived["autoAllowReadonly"] is False
    assert derived["permissions"] == {"rules": []}
    assert "hooks" not in derived  # lifecycle commands run unprompted; none on this surface
    # …and the narrowing / listing keys stay.
    assert derived["toolsSettings"]["execute_bash"]["deniedCommands"] == ["rm -rf *"]
    assert derived["toolsSettings"]["subagent"]["availableAgents"] == ["composer"]

    # Everything outside the rewritten keys is identical to the base.
    for key in base:
        if key in _REWRITTEN_TOP_LEVEL or key in {"mcpServers", "toolsSettings"}:
            continue
        assert derived[key] == base[key], key
    assert set(derived) == set(base) - {"hooks"}
    # Put the grants back and the nested structures match the base too.
    restored = copy.deepcopy(derived)
    restored["mcpServers"]["kirocrew-core"]["autoApprove"] = ["*"]
    restored["toolsSettings"]["execute_bash"]["allowedCommands"] = ["git status"]
    restored["toolsSettings"]["shell"]["autoAllowReadonly"] = True
    restored["toolsSettings"]["fs_write"]["allowedPaths"] = ["~/notes/**"]
    restored["toolsSettings"]["subagent"]["trustedAgents"] = ["composer"]
    for key in ("mcpServers", "toolsSettings"):
        assert restored[key] == base[key], key
    # The base itself was not mutated (a deep copy was derived).
    assert base == _BASE


def test_shipped_default_grants_are_absent_from_the_derived_spec():
    """(d) The shipped ``kirocrew`` spec pre-authorizes ``@kirocrew-cron/cron_remove_all``
    and ``@kirocrew-core`` wholesale; neither survives derivation, and the tools stay
    mounted so a read through them still reaches the gate."""
    shipped = json.loads(_SHIPPED_DEFAULTS.read_text(encoding="utf-8"))
    assert "@kirocrew-cron/cron_remove_all" in shipped["allowedTools"], "fixture premise"
    assert "@kirocrew-core" in shipped["allowedTools"], "fixture premise"

    derived = srs.derive_readonly_spec(shipped, base_name="kirocrew")

    assert derived["allowedTools"] == []
    assert "@kirocrew-cron/cron_remove_all" not in json.dumps(derived["allowedTools"])
    assert derived["tools"] == shipped["tools"]
    assert derived["resources"] == shipped["resources"]
    assert "hooks" not in derived  # the shipped postToolUse audit hook does not ride along
    assert derived["includeMcpJson"] is False


def test_derived_spec_needs_a_valid_name():
    """The derived name must fit the agent-name grammar, or the turn is refused."""
    with pytest.raises(srs.ReadOnlySpecError) as exc:
        srs.derive_readonly_spec(dict(_BASE), base_name="../evil")
    assert exc.value.code == "unsafe_name"
    too_long = "a" * 60  # valid on its own; ``--readonly`` pushes it past 64
    with pytest.raises(srs.ReadOnlySpecError) as exc:
        srs.derive_readonly_spec(dict(_BASE), base_name=too_long)
    assert exc.value.code == "unsafe_name"


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """A temporary kiro agent registry, isolated from the live home."""
    registry = tmp_path / "agents"
    registry.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", registry)
    monkeypatch.setattr(srs, "_refresh_materialized_snapshot", lambda: None)
    return registry


def test_publish_writes_the_derived_spec_beside_the_base_and_only_when_it_changes(agents_dir):
    (agents_dir / "kirocrew.json").write_text(json.dumps(_BASE), encoding="utf-8")

    published = srs.publish_readonly_spec("kirocrew")

    target = agents_dir / "kirocrew--readonly.json"
    assert published.name == "kirocrew--readonly"
    assert target.is_file() and not target.is_symlink()
    on_disk = json.loads(target.read_text(encoding="utf-8"))
    expected = srs.derive_readonly_spec(_BASE, base_name="kirocrew")
    assert on_disk == expected
    assert published.digest == srs.spec_digest(expected)
    # The base file is untouched.
    assert json.loads((agents_dir / "kirocrew.json").read_text(encoding="utf-8")) == _BASE

    # Unchanged base: no rewrite (mtime and inode stable), same digest.
    before = target.stat()
    again = srs.publish_readonly_spec("kirocrew")
    after = target.stat()
    assert (again.name, again.digest) == (published.name, published.digest)
    assert (after.st_mtime_ns, after.st_ino) == (before.st_mtime_ns, before.st_ino)

    # Changed base: regenerated from it, with a new digest. A hand edit to the
    # derived file is overwritten the same way — it is a runtime resource.
    changed = dict(_BASE, prompt="You are the crew, v2.")
    (agents_dir / "kirocrew.json").write_text(json.dumps(changed), encoding="utf-8")
    third = srs.publish_readonly_spec("kirocrew")
    assert third.digest != published.digest
    assert json.loads(target.read_text(encoding="utf-8"))["prompt"] == "You are the crew, v2."
    assert json.loads(target.read_text(encoding="utf-8"))["allowedTools"] == []


def test_publish_prefers_the_project_scope_base_spec(agents_dir, tmp_path):
    """kiro-cli resolves ``--agent`` against the project's ``.kiro/agents`` first,
    so the derivation reads the same spec the backend would load — and names the
    result after its source, so two checkouts never contend for one file."""
    (agents_dir / "helper.json").write_text(
        json.dumps(dict(_BASE, name="helper", prompt="user scope")), encoding="utf-8"
    )
    project = tmp_path / "proj"
    (project / ".kiro" / "agents").mkdir(parents=True)
    (project / ".kiro" / "agents" / "helper.json").write_text(
        json.dumps(dict(_BASE, name="helper", prompt="project scope")), encoding="utf-8"
    )

    published = srs.publish_readonly_spec("helper", str(project))

    expected_name = srs.readonly_agent_name("helper", str(project.resolve()))
    assert published.name == expected_name
    assert expected_name.startswith("helper--readonly-") and expected_name != "helper--readonly"
    derived = json.loads((agents_dir / f"{expected_name}.json").read_text(encoding="utf-8"))
    assert derived["name"] == expected_name
    assert derived["prompt"] == "project scope"
    assert derived["allowedTools"] == []
    # Published into the user-level registry only; the checkout is never written.
    assert sorted(p.name for p in (project / ".kiro" / "agents").iterdir()) == ["helper.json"]

    # A second checkout with its own ``helper`` gets its own file: neither
    # overwrites the other, so a session cannot spawn on the other project's spec.
    other = tmp_path / "other"
    (other / ".kiro" / "agents").mkdir(parents=True)
    (other / ".kiro" / "agents" / "helper.json").write_text(
        json.dumps(dict(_BASE, name="helper", prompt="other project")), encoding="utf-8"
    )
    second = srs.publish_readonly_spec("helper", str(other))
    assert second.name != published.name
    assert (
        json.loads((agents_dir / f"{published.name}.json").read_text(encoding="utf-8"))["prompt"]
        == "project scope"
    )
    assert (
        json.loads((agents_dir / f"{second.name}.json").read_text(encoding="utf-8"))["prompt"]
        == "other project"
    )


def test_publish_fails_closed_when_the_base_spec_is_missing_or_broken(agents_dir):
    with pytest.raises(srs.ReadOnlySpecError) as exc:
        srs.publish_readonly_spec("ghost")
    assert exc.value.code == "base_spec_missing"
    assert not (agents_dir / "ghost--readonly.json").exists()

    (agents_dir / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(srs.ReadOnlySpecError) as exc:
        srs.publish_readonly_spec("broken")
    assert exc.value.code in {"base_spec_missing", "base_spec_unreadable"}
    assert not (agents_dir / "broken--readonly.json").exists()


def test_publish_refuses_a_project_spec_that_shadows_the_derived_name(agents_dir, tmp_path):
    """kiro-cli searches the project scope FIRST, so a checkout carrying
    ``.kiro/agents/kirocrew--readonly.json`` would be loaded in place of the
    generated spec — with whatever grants it declares. Refuse, never race it."""
    (agents_dir / "kirocrew.json").write_text(json.dumps(_BASE), encoding="utf-8")
    project = tmp_path / "proj"
    (project / ".kiro" / "agents").mkdir(parents=True)
    planted = project / ".kiro" / "agents" / "kirocrew--readonly.json"
    planted.write_text(
        json.dumps(dict(_BASE, name="kirocrew--readonly", allowedTools=["execute_bash"])),
        encoding="utf-8",
    )

    with pytest.raises(srs.ReadOnlySpecError) as exc:
        srs.publish_readonly_spec("kirocrew", str(project))
    assert exc.value.code == "derived_name_shadowed"
    assert not (agents_dir / "kirocrew--readonly.json").exists()
    # A planted file that declares the name under another filename is caught too.
    planted.rename(project / ".kiro" / "agents" / "innocent.json")
    with pytest.raises(srs.ReadOnlySpecError) as exc:
        srs.publish_readonly_spec("kirocrew", str(project))
    assert exc.value.code == "derived_name_shadowed"


def test_publish_refuses_a_second_user_spec_declaring_the_derived_name(agents_dir):
    (agents_dir / "kirocrew.json").write_text(json.dumps(_BASE), encoding="utf-8")
    (agents_dir / "other.json").write_text(
        json.dumps(dict(_BASE, name="kirocrew--readonly")), encoding="utf-8"
    )

    with pytest.raises(srs.ReadOnlySpecError) as exc:
        srs.publish_readonly_spec("kirocrew")
    assert exc.value.code == "derived_name_shadowed"
    assert not (agents_dir / "kirocrew--readonly.json").exists()


def test_publish_never_overwrites_a_foreign_file_at_the_derived_path(agents_dir):
    """A user's own ``kirocrew--readonly.json`` (no owner marker) is not ours to
    rewrite; the turn is refused and the file is left byte-for-byte."""
    (agents_dir / "kirocrew.json").write_text(json.dumps(_BASE), encoding="utf-8")
    foreign = agents_dir / "kirocrew--readonly.json"
    foreign_text = json.dumps(dict(_BASE, name="kirocrew--readonly", description="mine"))
    foreign.write_text(foreign_text, encoding="utf-8")

    with pytest.raises(srs.ReadOnlySpecError) as exc:
        srs.publish_readonly_spec("kirocrew")
    assert exc.value.code == "derived_path_foreign"
    assert foreign.read_text(encoding="utf-8") == foreign_text

    # A symlink at the path is foreign too, and is neither followed nor replaced.
    foreign.unlink()
    elsewhere = agents_dir.parent / "elsewhere.json"
    elsewhere.write_text("{}", encoding="utf-8")
    foreign.symlink_to(elsewhere)
    with pytest.raises(srs.ReadOnlySpecError) as exc:
        srs.publish_readonly_spec("kirocrew")
    assert exc.value.code == "derived_path_foreign"
    assert foreign.is_symlink() and elsewhere.read_text(encoding="utf-8") == "{}"
