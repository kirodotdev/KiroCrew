"""How a template's prompt and declared resources are admitted, refused and budgeted.

Malformed declared fields and linked directories refuse explicitly; a product-managed
prompt defers to the session start; crew-managed state is never admitted; a workspace
glob prunes managed subtrees and names what it skipped; the document cap, the declared
file sizes, the projected launch-path record and object-form resource entries.

Lifecycle injection, member identity and the basic reads are pinned in
``test_member_essential_context.py``.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import member_essential_context_helpers
import pytest
from member_essential_context_helpers import _write_template

from conftest import make_dir_link, requires_symlinks
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.member_essential_context import MemberEssentialContextError

# Fixtures shared with the sibling modules, bound by assignment so pytest registers
# them for the tests in this module.
_close_skills_loaders = member_essential_context_helpers._close_skills_loaders
env = member_essential_context_helpers.env


@pytest.mark.parametrize("field, value", [("prompt", 42), ("resources", "file://guide.md")])
def test_malformed_declared_template_fields_refuse_explicitly(env, field, value):
    spec = env.project / ".kiro" / "agents" / "writer-template.json"
    spec.write_text(json.dumps({"name": "writer-template", field: value}), encoding="utf-8")
    with pytest.raises(MemberEssentialContextError, match=field):
        env.builder.build_message(
            "Continue", False, memory_store=env.store, member=env.member, project=str(env.project)
        )


def test_linked_directory_is_refused_before_enumerating_outside_sources(env, tmp_path):

    target = tmp_path / "other-project"
    target.mkdir()
    (target / "secret.md").write_text("SIBLING_SECRET", encoding="utf-8")
    make_dir_link(env.project / "guides", target)
    spec = env.project / ".kiro" / "agents" / "writer-template.json"
    spec.write_text(json.dumps({"name": "writer-template", "resources": ["file://guides/*.md"]}))
    with pytest.raises(MemberEssentialContextError, match="guides"):
        env.builder.build_message(
            "Continue", False, memory_store=env.store, member=env.member, project=str(env.project)
        )


def test_refused_workspace_root_is_never_resolved(env, monkeypatch, tmp_path):
    """A root ``validate_file_path`` refuses (a UNC share on Windows) is not probed.

    ``realpath`` on such a root IS the outbound SMB probe, so the isolation
    check must compare it lexically instead of resolving it.
    """
    from kiro_crew import member_essential_context as mec

    refused = Path("//share-host/ws-share/workspace")
    real_validate = mec.validate_file_path
    monkeypatch.setattr(
        mec,
        "validate_file_path",
        lambda raw: None if "share-host" in raw else real_validate(raw),
    )
    monkeypatch.setattr(
        mec.KiroCrewConfig,
        "load",
        classmethod(lambda cls: SimpleNamespace(workspaces={"shared": None})),
    )
    monkeypatch.setattr(mec, "workspace_dir_for", lambda name: refused)
    real_realpath = os.path.realpath
    resolved: list[str] = []

    def recording_realpath(p, *a, **k):
        resolved.append(str(p))
        return real_realpath(p, *a, **k)

    monkeypatch.setattr(os.path, "realpath", recording_realpath)

    mec._refuse_managed_source(tmp_path / "project" / "guide.md")

    assert not any("share-host" in p for p in resolved), resolved
    assert mec._comparable_root(refused) == Path(os.path.abspath(refused))


@pytest.mark.parametrize("source", ["package", "development", "user-override"])
@pytest.mark.parametrize("spec_uses_stub", [False, True], ids=["file-pointer", "native-stub"])
@pytest.mark.parametrize(
    "fresh, options",
    [
        (True, {}),
        (False, {}),
        (False, {"needs_reinjection": True}),
        (True, {"resumed": True}),
        (True, {"minimal_context": True}),
    ],
)
def test_inherited_product_prompt_uses_session_start_not_essentials(
    env, tmp_path, monkeypatch, source, spec_uses_stub, fresh, options
):
    from kiro_crew import agent
    from kiro_crew.config import config_dir

    package = tmp_path / "installed-package" / "config"
    development = tmp_path / "checkout"
    monkeypatch.setattr(agent, "_BUNDLED_CFG_DIR", package)
    monkeypatch.setattr(agent, "_project_dir", lambda: development)
    paths = {
        "package": package / "prompt.md",
        "development": development / "agents" / "prompt.md",
        "user-override": config_dir() / "prompt.md",
    }
    prompt_path = paths[source]
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("PRODUCT_PROMPT_AT_SESSION_START", encoding="utf-8")
    assert agent._prompt_path() == prompt_path
    spec = env.project / ".kiro" / "agents" / "writer-template.json"
    spec_prompt = agent._NATIVE_PROMPT_STUB if spec_uses_stub else f"file://{prompt_path}"
    spec.write_text(
        json.dumps({"name": "writer-template", "prompt": spec_prompt}),
        encoding="utf-8",
    )

    message, _ = env.builder.build_message(
        "Continue",
        fresh,
        memory_store=env.store,
        member=env.member,
        project=str(env.project),
        **options,
    )

    assert message.count("[V2 ESSENTIAL CONTEXT") == 1
    assert "You are writer." in message and "Do not publish drafts." in message
    assert "Preference anchor" in message and "Project rules" in message
    assert f"[Essential source: {prompt_path}]" not in message
    if fresh and not options.get("resumed"):
        assert message.count("PRODUCT_PROMPT_AT_SESSION_START") == 1
    if spec_uses_stub:
        # Essentials sanitise the stub's [AGENT SYSTEM PROMPT] markers, so a
        # byte-exact match would miss a leak; assert on its marker-free tail.
        assert "follow it as your authoritative contract" not in message
    env.forbidden.assert_not_called()


def test_managed_stub_reaches_owner_session_start(env, tmp_path, monkeypatch):
    """A private-owner fork whose spec carries the native stub resolves to the
    product contract at session start via the owner-template load, not the stub
    text (see agent-spec-fields.md → Prompt)."""
    from kiro_crew import agent

    package = tmp_path / "installed-package" / "config"
    monkeypatch.setattr(agent, "_BUNDLED_CFG_DIR", package)
    monkeypatch.setattr(agent, "_project_dir", lambda: None)
    prompt_path = package / "prompt.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("PRODUCT_PROMPT_AT_SESSION_START", encoding="utf-8")
    assert agent._prompt_path() == prompt_path
    spec = env.project / ".kiro" / "agents" / "writer-template.json"
    spec.write_text(
        json.dumps({"name": "writer-template", "prompt": agent._NATIVE_PROMPT_STUB}),
        encoding="utf-8",
    )

    # agent="writer-template" == the member's own template, so the owner-template
    # session-start load (context._load_agent_prompt) runs — not the direct read.
    message, _ = env.builder.build_message(
        "Continue",
        True,
        agent="writer-template",
        memory_store=env.store,
        member=env.member,
        project=str(env.project),
    )

    assert message.count("PRODUCT_PROMPT_AT_SESSION_START") == 1
    assert "follow it as your authoritative contract" not in message
    env.forbidden.assert_not_called()


def test_stale_installed_product_prompt_uses_current_session_start(env, tmp_path, monkeypatch):
    """A prior bundled gateway's URI remains managed after its install changes."""
    from kiro_crew import agent

    package = tmp_path / "current-package" / "config"
    monkeypatch.setattr(agent, "_BUNDLED_CFG_DIR", package)
    monkeypatch.setattr(agent, "_project_dir", lambda: None)
    package.mkdir(parents=True)
    (package / "prompt.md").write_text("CURRENT_PRODUCT_PROMPT", encoding="utf-8")
    stale = (
        "file:///Applications/KiroCrew.app/Contents/Resources/backend-dist/"
        "kirocrew-backend-arm64/lib/python3.12/site-packages/kiro_crew/config/prompt.md"
    )
    spec = env.project / ".kiro" / "agents" / "writer-template.json"
    spec.write_text(json.dumps({"name": "writer-template", "prompt": stale}), encoding="utf-8")

    message, _ = env.builder.build_message(
        "Continue", True, memory_store=env.store, member=env.member, project=str(env.project)
    )

    assert message.count("CURRENT_PRODUCT_PROMPT") == 1
    assert stale not in message
    env.forbidden.assert_not_called()


@pytest.mark.parametrize("template", ["writer-template", "kirocrew"])
@pytest.mark.parametrize("source", ["inline", "relative", "absolute"])
def test_custom_persona_is_not_classified_by_template_name(env, template, source):
    cfg = KiroCrewConfig.load()
    cfg.agents["writer"].kiro_agent = template
    cfg.save()
    spec = env.project / ".kiro" / "agents" / f"{template}.json"
    spec.parent.mkdir(parents=True, exist_ok=True)
    # A relative file:// prompt resolves against the agent file's own directory,
    # so the persona for that case lives beside the spec, not at the root.
    persona = spec.parent / "prompt.md"
    persona.write_text("CUSTOM_OWNER_PERSONA", encoding="utf-8")
    prompts = {
        "inline": "CUSTOM_OWNER_PERSONA",
        "relative": "file://prompt.md",
        "absolute": f"file://{persona}",
    }
    spec.write_text(json.dumps({"name": template, "prompt": prompts[source]}), encoding="utf-8")

    message, _ = env.builder.build_message(
        "Continue",
        False,
        agent="task-template",
        memory_store=env.store,
        member=env.member,
        project=str(env.project),
    )

    assert "CUSTOM_OWNER_PERSONA" in message
    assert "Do not publish drafts." in message


@pytest.mark.parametrize("template", ["writer-template", "kirocrew"])
def test_unmanaged_prompt_outside_root_is_not_exempted_by_name(env, tmp_path, template):
    cfg = KiroCrewConfig.load()
    cfg.agents["writer"].kiro_agent = template
    cfg.save()
    persona = tmp_path / "prompt.md"
    persona.write_text("OUTSIDE_PERSONA", encoding="utf-8")
    spec = env.project / ".kiro" / "agents" / f"{template}.json"
    spec.write_text(json.dumps({"name": template, "prompt": f"file://{persona}"}), encoding="utf-8")

    with pytest.raises(MemberEssentialContextError, match="outside the admitted document root"):
        env.builder.build_message(
            "Continue", False, memory_store=env.store, member=env.member, project=str(env.project)
        )


def test_managed_source_resolves_each_declared_root_once_per_call(tmp_path, monkeypatch):
    from collections import Counter

    from kiro_crew import member_essential_context as mec
    from kiro_crew.config.loader import WorkspaceConfig

    cfg = KiroCrewConfig.load()
    cfg.workspaces["second"] = WorkspaceConfig(dir=str(tmp_path / "second"))
    cfg.save()
    original = mec._comparable_root
    calls = []

    def resolve(root):
        calls.append(root)
        return original(root)

    monkeypatch.setattr(mec, "_comparable_root", resolve)
    mec._refuse_managed_source(mec.config_dir() / "workspace" / "document.txt")
    first = Counter(calls)
    assert len(first) >= 4
    assert set(first.values()) == {1}, first
    calls.clear()
    mec._refuse_managed_source(mec.config_dir() / "workspace" / "document.txt")
    assert Counter(calls) == first, "Every new call must revalidate all roots"


@pytest.mark.parametrize("root_index", [0, 1, 2])
@pytest.mark.parametrize(
    "leaf",
    [
        "members",
        "member-rules",
        "member-memory-bindings",
        "backups",
        "trust",
        "memory_stores",
        "lessons",
    ],
)
def test_managed_source_keeps_every_admin_root_protected(tmp_path, monkeypatch, root_index, leaf):
    from kiro_crew import member_essential_context as mec

    home = tmp_path / "host"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    roots = [mec.config_dir(), home / ".kiro/crew", home / ".kirocrew"]
    with pytest.raises(MemberEssentialContextError, match="managed memory/member state"):
        mec._refuse_managed_source(roots[root_index] / leaf / "record.txt")


def test_managed_source_rechecks_workspace_configuration_between_calls(tmp_path):
    from kiro_crew import member_essential_context as mec
    from kiro_crew.config.loader import WorkspaceConfig

    candidate = mec.config_dir() / "custom-project" / "guide.txt"
    cfg = KiroCrewConfig.load()
    cfg.workspaces["custom"] = WorkspaceConfig(dir=str(candidate.parent))
    cfg.save()
    mec._refuse_managed_source(candidate)
    del cfg.workspaces["custom"]
    cfg.save()
    with pytest.raises(MemberEssentialContextError, match="managed memory/member state"):
        mec._refuse_managed_source(candidate)


@pytest.mark.parametrize("placement", ["ancestor", "equal", "protected-child"])
def test_workspace_overlap_does_not_admit_admin_state(tmp_path, placement):
    from kiro_crew import member_essential_context as mec
    from kiro_crew.config.loader import WorkspaceConfig

    admin = mec.config_dir()
    workspace = {
        "ancestor": admin.parent,
        "equal": admin,
        "protected-child": admin / "member-rules",
    }[placement]
    cfg = KiroCrewConfig.load()
    cfg.workspaces["overlap"] = WorkspaceConfig(dir=str(workspace))
    cfg.save()
    with pytest.raises(MemberEssentialContextError, match="managed memory/member state"):
        mec._refuse_managed_source(admin / "member-rules" / "rule.txt")


@requires_symlinks
@pytest.mark.skipif(
    os.name == "nt", reason="Windows rejects linked ancestors at the existing path gate"
)
def test_managed_source_rechecks_admin_symlink_target_between_calls(tmp_path, monkeypatch):
    from kiro_crew import member_essential_context as mec

    home = tmp_path / "host"
    (home / ".kiro").mkdir(parents=True)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    alias = home / ".kiro" / "crew"
    alias.symlink_to(first, target_is_directory=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    with pytest.raises(MemberEssentialContextError, match="managed memory/member state"):
        mec._refuse_managed_source(first / "members" / "record.txt")
    mec._refuse_managed_source(second / "members" / "record.txt")
    alias.unlink()
    alias.symlink_to(second, target_is_directory=True)
    with pytest.raises(MemberEssentialContextError, match="managed memory/member state"):
        mec._refuse_managed_source(second / "members" / "record.txt")


def test_managed_source_never_resolves_unvalidated_candidate(tmp_path, monkeypatch):
    from kiro_crew import member_essential_context as mec

    candidate = Path("//untrusted-candidate/guide.txt")
    original = os.path.realpath

    def resolve(path, *args, **kwargs):
        assert "untrusted-candidate" not in str(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", resolve)
    mec._refuse_managed_source(candidate)


@pytest.mark.parametrize("pattern", ["*/AGENTS.md", "**/AGENTS.md"])
@pytest.mark.parametrize("leaf", ["memory", "memory_index", "lessons", ".lessons"])
def test_workspace_glob_excludes_managed_subtrees_before_scanning(env, monkeypatch, pattern, leaf):
    from kiro_crew import member_essential_context as essentials
    from kiro_crew.config import config_dir

    project = config_dir() / "workspace"
    managed = project / leaf
    managed.mkdir(parents=True, exist_ok=True)
    (managed / "AGENTS.md").write_text("MANAGED_CONTENT_MUST_NOT_LOAD", encoding="utf-8")
    guides = project / "guides"
    guides.mkdir(exist_ok=True)
    (guides / "AGENTS.md").write_text("WORKSPACE_CHILD_GUIDE", encoding="utf-8")
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "writer-template.json").write_text(
        json.dumps({"name": "writer-template", "resources": [f"file://{pattern}"]}),
        encoding="utf-8",
    )
    original_scan = essentials.os.scandir

    def scan(path):
        if not isinstance(path, int):
            assert not Path(path).is_relative_to(managed), "managed subtree was enumerated"
        return original_scan(path)

    monkeypatch.setattr(essentials.os, "scandir", scan)
    message, _ = env.builder.build_message(
        "Continue", False, memory_store=env.store, member=env.member, project=str(project)
    )
    assert "WORKSPACE_CHILD_GUIDE" in message
    assert "MANAGED_CONTENT_MUST_NOT_LOAD" not in message
    assert "You are writer." in message
    # The prune stays; it names only a prefix collision, never the real store.
    note_label = "[Essential source: essential-context#managed-skipped:writer-template]"
    if leaf == "memory_index":
        assert note_label in message
        assert str(managed) in message
    else:
        assert note_label not in message
        assert str(managed) not in message


def test_plain_workspace_glob_over_real_managed_store_adds_no_note(env, caplog):
    """A broad glob walking past the workspace's own memory/lessons store is
    routine: no note, no warning, only a debug line."""
    from kiro_crew.config import config_dir
    from kiro_crew.member_essential_context import (
        ESSENTIAL_MANAGED_SKIP_SOURCE,
        documents_for_member,
    )

    project = config_dir() / "workspace"
    for name in ("memory", "lessons", ".lessons"):
        (project / name).mkdir(parents=True, exist_ok=True)
        (project / name / "AGENTS.md").write_text("MANAGED", encoding="utf-8")
    for name in ("memory.db", "memory.db-wal", "memory_index.db-shm", "lessons.jsonl"):
        (project / name).write_text("", encoding="utf-8")
    (project / "guides").mkdir(exist_ok=True)
    (project / "guides" / "AGENTS.md").write_text("WORKSPACE_CHILD_GUIDE", encoding="utf-8")
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    _write_template(agents, "writer-template", ["file://*/AGENTS.md", "file://*", "file://**/*.md"])
    with caplog.at_level("DEBUG", logger="kiro_crew.member_essential_context"):
        documents = documents_for_member("writer-template", str(project))
    assert not any(s.startswith(ESSENTIAL_MANAGED_SKIP_SOURCE) for s, _ in documents)
    assert "WORKSPACE_CHILD_GUIDE" in "\n".join(body for _, body in documents)
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("pruned" in r.getMessage() for r in caplog.records if r.levelname == "DEBUG")


def test_two_templates_skipping_different_entries_still_build(env):
    """An owner and an execution template whose globs skip different
    prefix-colliding entries each get their own note, so merging them
    does not trip the changed-during-preparation guard."""
    from kiro_crew.config import config_dir

    project = config_dir() / "workspace"
    project.mkdir(parents=True, exist_ok=True)
    (project / "memory-notes.md").write_text("PREFIX_A", encoding="utf-8")
    (project / "lessons-notes.md").write_text("PREFIX_B", encoding="utf-8")
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    _write_template(agents, "writer-template", ["file://memory*.md"])
    _write_template(agents, "runner-template", ["file://lessons*.md"])
    message = env.builder._build_v2_essentials(
        env.store,
        member=env.member,
        project=str(project),
        execution_template="runner-template",
    )
    assert "essential-context#managed-skipped:writer-template" in message
    assert "essential-context#managed-skipped:runner-template" in message
    assert str(project / "memory-notes.md") in message
    assert str(project / "lessons-notes.md") in message
    assert "PREFIX_A" not in message
    assert "PREFIX_B" not in message


def test_native_reads_carry_no_skip_note(env):
    """The note is not a host-native source: a native-only read and the
    launch documents leave it out, so wire dedup cannot strip its body."""
    from kiro_crew.config import config_dir
    from kiro_crew.member_essential_context import (
        ESSENTIAL_MANAGED_SKIP_SOURCE,
        documents_for_member,
        kiro_launch_documents,
    )

    project = config_dir() / "workspace"
    project.mkdir(parents=True, exist_ok=True)
    (project / "memory-notes.md").write_text("PREFIX_A", encoding="utf-8")
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    _write_template(agents, "writer-template", ["file://*.md"])
    for documents in (
        documents_for_member("writer-template", str(project), native_only=True),
        kiro_launch_documents("writer-template", str(project)),
    ):
        assert not any(s.startswith(ESSENTIAL_MANAGED_SKIP_SOURCE) for s, _ in documents)
    full = dict(documents_for_member("writer-template", str(project)))
    assert f"{ESSENTIAL_MANAGED_SKIP_SOURCE}:writer-template" in full


def test_workspace_glob_names_each_prefix_colliding_entry_it_skips(env, caplog):
    from kiro_crew.config import config_dir
    from kiro_crew.member_essential_context import (
        ESSENTIAL_MANAGED_SKIP_SOURCE,
        documents_for_member,
    )

    project = config_dir() / "workspace"
    project.mkdir(parents=True, exist_ok=True)
    (project / "memory-notes").mkdir()
    (project / "memory-notes" / "guide.md").write_text("PREFIX_DIR_CONTENT", encoding="utf-8")
    (project / "lessons-archive.md").write_text("PREFIX_FILE_CONTENT", encoding="utf-8")
    (project / "plain.md").write_text("PLAIN_GUIDE", encoding="utf-8")
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "writer-template.json").write_text(
        json.dumps({"name": "writer-template", "resources": ["file://*.md", "file://*/guide.md"]}),
        encoding="utf-8",
    )
    core: set[str] = set()
    with caplog.at_level("WARNING", logger="kiro_crew.member_essential_context"):
        documents = documents_for_member("writer-template", str(project), core_sources_out=core)
    bodies = dict(documents)
    joined = "\n".join(bodies.values())
    assert "PLAIN_GUIDE" in joined
    assert "PREFIX_DIR_CONTENT" not in joined
    assert "PREFIX_FILE_CONTENT" not in joined
    note = bodies[f"{ESSENTIAL_MANAGED_SKIP_SOURCE}:writer-template"]
    assert "entries matched a declared resource pattern" in note
    assert str(project / "memory-notes") in note
    assert str(project / "lessons-archive.md") in note
    # A size-limited envelope must not drop the note silently.
    assert f"{ESSENTIAL_MANAGED_SKIP_SOURCE}:writer-template" in core
    logged = [r.getMessage() for r in caplog.records]
    assert any("memory-notes" in line for line in logged)
    assert any("lessons-archive.md" in line for line in logged)


def test_workspace_glob_without_prefix_collision_adds_no_skip_note(env):
    from kiro_crew.config import config_dir
    from kiro_crew.member_essential_context import (
        ESSENTIAL_MANAGED_SKIP_SOURCE,
        documents_for_member,
    )

    project = config_dir() / "workspace"
    project.mkdir(parents=True, exist_ok=True)
    (project / "plain.md").write_text("PLAIN_GUIDE", encoding="utf-8")
    # A managed-named file the pattern could never return is not reported.
    (project / "memory_index.db").write_text("", encoding="utf-8")
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "writer-template.json").write_text(
        json.dumps({"name": "writer-template", "resources": ["file://*/AGENTS.md"]}),
        encoding="utf-8",
    )
    documents = documents_for_member("writer-template", str(project))
    assert not any(s.startswith(ESSENTIAL_MANAGED_SKIP_SOURCE) for s, _ in documents)


@pytest.mark.parametrize("resource", ["memory/AGENTS.md", "memory/*.md", "memory/**/AGENTS.md"])
def test_workspace_explicit_managed_prefix_still_refuses(env, resource):
    from kiro_crew.config import config_dir
    from kiro_crew.member_essential_context import documents_for_member

    project = config_dir() / "workspace"
    (project / "memory").mkdir(parents=True, exist_ok=True)
    (project / "memory" / "AGENTS.md").write_text("MANAGED_CONTENT", encoding="utf-8")
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "writer-template.json").write_text(
        json.dumps({"name": "writer-template", "resources": [f"file://{resource}"]}),
        encoding="utf-8",
    )
    with pytest.raises(MemberEssentialContextError, match="managed memory/member state"):
        documents_for_member("writer-template", str(project))


def test_glob_keeps_memory_named_directory_in_an_ordinary_project(env):
    from kiro_crew.member_essential_context import documents_for_member

    memory = env.project / "memory"
    memory.mkdir()
    (memory / "AGENTS.md").write_text("LEGITIMATE_PROJECT_GUIDE", encoding="utf-8")
    (env.project / ".kiro" / "agents" / "writer-template.json").write_text(
        json.dumps({"name": "writer-template", "resources": ["file://*/AGENTS.md"]}),
        encoding="utf-8",
    )
    documents = documents_for_member("writer-template", str(env.project))
    assert "LEGITIMATE_PROJECT_GUIDE" in [body for _, body in documents]


def test_document_cap_ignores_on_demand_resource_schemes(env):
    """Only file:// declarations become documents, so only they spend the budget.

    An agent that declares many skills is the common shape: the skills stay on
    demand and are never read here, so a template with one guide and seventy
    skills loads exactly one document and must not be refused as oversized.
    """
    from kiro_crew.member_essential_context import (
        documents_for_member,
        projected_resource_documents,
    )

    skills = [f"skill://tooling/skill-{index}/SKILL.md" for index in range(70)]
    resources = ["file://declared-guide.md", *skills]
    (env.project / ".kiro" / "agents" / "writer-template.json").write_text(
        json.dumps({"name": "writer-template", "resources": resources}),
        encoding="utf-8",
    )
    documents = documents_for_member("writer-template", str(env.project))
    assert "Declared guide: examples must be reproducible." in [body for _, body in documents]

    projected = projected_resource_documents(
        {"id": "writer-template", "resources": resources}, str(env.project)
    )
    assert list(projected.values()) == ["Declared guide: examples must be reproducible."]


def test_declared_file_size_does_not_stat_a_rejected_path(env, monkeypatch):
    from kiro_crew import member_essential_context as module

    guide = env.project / "declared-guide.md"
    stat_calls = []
    real_stat = os.stat

    def recording_stat(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and Path(path) == guide:
            stat_calls.append(path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(module, "validate_file_path", lambda _path: None)
    monkeypatch.setattr(module.os, "stat", recording_stat)

    with pytest.raises(MemberEssentialContextError, match="outside admitted path screen"):
        module._declared_file_size(guide)

    assert stat_calls == []


def test_declared_file_size_stats_the_admitted_path(env, monkeypatch):
    from kiro_crew import member_essential_context as module

    guide = env.project / "declared-guide.md"
    admitted = env.project / "admitted-guide.md"
    admitted.write_text("admitted", encoding="utf-8")
    stat_calls = []
    real_stat = os.stat

    def recording_stat(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and Path(path) in (guide, admitted):
            stat_calls.append((path, kwargs))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(module, "validate_file_path", lambda _path: str(admitted))
    monkeypatch.setattr(module.os, "stat", recording_stat)

    size = module._declared_file_size(guide)

    assert size == real_stat(admitted, follow_symlinks=False).st_size
    assert stat_calls == [(str(admitted), {"follow_symlinks": False})]


def test_projected_launch_paths_name_the_declared_guides_without_reading_them(env, monkeypatch):
    """kiro-cli re-reads a declared guide for every request, so the launch record
    needs its path only; the body is never read."""
    from kiro_crew import member_essential_context as module

    guide = env.project / "declared-guide.md"
    definition = {"id": "writer-template", "resources": ["file://declared-guide.md"]}
    read = Mock(side_effect=AssertionError("the launch record must not read a guide"))
    monkeypatch.setattr(module, "_read", read)

    paths = module.projected_launch_paths(definition, str(env.project))

    assert paths == [str(guide)]
    read.assert_not_called()


def test_projected_launch_paths_match_the_documents_the_launch_reader_reads(env):
    """The folder skips the record by path, so the record must spell each guide
    exactly as the launch reader does."""
    from kiro_crew import member_essential_context as module

    definition = {"id": "writer-template", "resources": ["file://declared-guide.md"]}

    paths = module.projected_launch_paths(definition, str(env.project))

    assert paths == list(module.projected_resource_documents(definition, str(env.project)))


def test_projected_launch_paths_record_nothing_past_the_size_bound(env, monkeypatch):
    """kiro-cli drops whole resources past its budget, so a launch whose declared
    files exceed the bound records nothing and the folder sends every guide."""
    from kiro_crew import member_essential_context as module

    guide = env.project / "declared-guide.md"
    definition = {"id": "writer-template", "resources": ["file://declared-guide.md"]}

    monkeypatch.setattr(module, "_LAUNCH_RECORD_MAX_BYTES", guide.stat().st_size)
    assert module.projected_launch_paths(definition, str(env.project)) == [str(guide)]

    monkeypatch.setattr(module, "_LAUNCH_RECORD_MAX_BYTES", guide.stat().st_size - 1)
    assert module.projected_launch_paths(definition, str(env.project)) == []


def test_projected_launch_size_bound_counts_every_declared_file(env, monkeypatch):
    """kiro-cli budgets every declared file it loads, so a declared file the
    launch record never names counts, and so does the sum of files that each
    fit the bound alone."""
    from kiro_crew import member_essential_context as module

    guide = env.project / "declared-guide.md"
    reference = env.project / "reference.json"
    reference.write_text("r" * 480, encoding="utf-8")
    assert reference.stat().st_size < 500 < guide.stat().st_size + reference.stat().st_size
    monkeypatch.setattr(module, "_LAUNCH_RECORD_MAX_BYTES", 500)

    guide_only = {"id": "writer-template", "resources": ["file://declared-guide.md"]}
    assert module.projected_launch_paths(guide_only, str(env.project)) == [str(guide)]
    with_reference = {
        "id": "writer-template",
        "resources": ["file://declared-guide.md", "file://reference.json"],
    }
    assert module.projected_launch_paths(with_reference, str(env.project)) == []


@pytest.mark.parametrize(
    "extra_resources",
    [
        pytest.param(["file://missing.json"], id="missing-literal-file"),
        pytest.param(["file://data-a/*.json", "file://data-b/*.json"], id="many-non-markdown"),
    ],
)
def test_projected_launch_size_check_refuses_no_view_the_record_names(
    env, monkeypatch, extra_resources
):
    """A declared file that does not exist adds no bytes, and non-markdown
    matches do not count against the reader's document limit."""
    from kiro_crew import member_essential_context as module

    guide = env.project / "declared-guide.md"
    for directory in ("data-a", "data-b"):
        (env.project / directory).mkdir()
        for index in range(40):
            (env.project / directory / f"{index}.json").write_text("d", encoding="utf-8")
    definition = {
        "id": "writer-template",
        "resources": ["file://declared-guide.md", *extra_resources],
    }

    assert module.projected_launch_paths(definition, str(env.project)) == [str(guide)]


def test_projected_launch_paths_stat_failure_is_a_record_failure(env, monkeypatch):
    from kiro_crew import member_essential_context as module

    definition = {"id": "writer-template", "resources": ["file://declared-guide.md"]}
    real_stat = os.stat

    def failing_stat(path, *args, **kwargs):
        if Path(path).name == "declared-guide.md":
            raise PermissionError("stat denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "stat", failing_stat)

    with pytest.raises(MemberEssentialContextError, match="declared-guide.md: .*stat denied"):
        module.projected_launch_paths(definition, str(env.project))


def test_declared_files_fit_counts_an_unmeasurable_file_as_too_large(env, monkeypatch):
    from kiro_crew import member_essential_context as module

    definition = {"id": "writer-template", "resources": ["file://declared-guide.md"]}
    assert module._declared_files_fit(definition, str(env.project)) is True

    def denied(path):
        raise MemberEssentialContextError(f"Essential source {path}: stat denied") from (
            PermissionError("stat denied")
        )

    monkeypatch.setattr(module, "_declared_file_size", denied)
    assert module._declared_files_fit(definition, str(env.project)) is False


def test_document_cap_still_bounds_declared_file_resources(env):
    from kiro_crew.member_essential_context import (
        documents_for_member,
        projected_resource_documents,
    )

    resources = [f"file://guide-{index}.md" for index in range(65)]
    (env.project / ".kiro" / "agents" / "writer-template.json").write_text(
        json.dumps({"name": "writer-template", "resources": resources}),
        encoding="utf-8",
    )
    with pytest.raises(MemberEssentialContextError, match="too many resources"):
        documents_for_member("writer-template", str(env.project))
    with pytest.raises(MemberEssentialContextError, match="exceeds the document limit"):
        projected_resource_documents(
            {"id": "writer-template", "resources": resources}, str(env.project)
        )


def test_object_form_declaration_reaches_the_launch_document_path(env):
    """The launch path, not just the helper, has to admit an object declaration.

    kiro-cli documents no string form for a knowledge base, so a member bound to
    such a template could not start: the launch-document build refused the spec
    before the child process existed.
    """
    from kiro_crew.member_essential_context import kiro_launch_documents

    resources = [
        "file://declared-guide.md",
        {
            "type": "knowledgeBase",
            "source": "file://kb",
            "name": "ProjectDocs",
            "indexType": "best",
            "include": ["**/*.md"],
            "autoUpdate": True,
        },
    ]
    (env.project / ".kiro" / "agents" / "writer-template.json").write_text(
        json.dumps({"name": "writer-template", "resources": resources}),
        encoding="utf-8",
    )
    documents = kiro_launch_documents("writer-template", str(env.project))
    assert "Declared guide: examples must be reproducible." in [body for _, body in documents]


def test_object_form_declaration_is_admitted_and_read_by_nobody(env):
    """Admitting the entry must not turn its source into an essential document.

    No path is derived from the entry, so a readable directory of markdown
    behind ``source`` contributes no text.
    """
    from kiro_crew.member_essential_context import (
        documents_for_member,
        projected_resource_documents,
    )

    source_dir = env.project / "kb"
    source_dir.mkdir()
    (source_dir / "inside.md").write_text("KB_SOURCE_BODY", encoding="utf-8")
    resources = [
        "file://declared-guide.md",
        {"type": "knowledgeBase", "source": "file://kb", "name": "ProjectDocs"},
    ]
    (env.project / ".kiro" / "agents" / "writer-template.json").write_text(
        json.dumps({"name": "writer-template", "resources": resources}),
        encoding="utf-8",
    )
    bodies = [body for _, body in documents_for_member("writer-template", str(env.project))]
    assert "Declared guide: examples must be reproducible." in bodies
    assert not any("KB_SOURCE_BODY" in body for body in bodies)

    projected = projected_resource_documents(
        {"id": "writer-template", "resources": resources}, str(env.project)
    )
    assert list(projected.values()) == ["Declared guide: examples must be reproducible."]


def test_object_form_source_cannot_widen_the_admitted_roots(env):
    """A source outside every admitted root is still not a location this reads.

    A ``file://`` declaration that escaped its root would be refused by ``_read``;
    an object declaration is never resolved at all. Spelled without ``name`` and
    also declared alone, so admission depends neither on an optional key nor on
    a ``file://`` sibling.
    """
    from kiro_crew.member_essential_context import (
        documents_for_member,
        kiro_launch_documents,
        projected_resource_documents,
    )

    outside = env.project.parent / "outside-kb"
    outside.mkdir()
    (outside / "secret.md").write_text("OUTSIDE_ROOT_BODY", encoding="utf-8")
    nameless = {"type": "knowledgeBase", "source": f"file://{outside}"}
    resources = ["file://declared-guide.md", nameless]
    (env.project / ".kiro" / "agents" / "writer-template.json").write_text(
        json.dumps({"name": "writer-template", "resources": resources}),
        encoding="utf-8",
    )
    bodies = [body for _, body in documents_for_member("writer-template", str(env.project))]
    assert "Declared guide: examples must be reproducible." in bodies
    assert not any("OUTSIDE_ROOT_BODY" in body for body in bodies)

    (env.project / ".kiro" / "agents" / "writer-template.json").write_text(
        json.dumps({"name": "writer-template", "resources": [nameless]}),
        encoding="utf-8",
    )
    launched = kiro_launch_documents("writer-template", str(env.project))
    assert not any("OUTSIDE_ROOT_BODY" in body for _, body in launched)
    assert (
        projected_resource_documents(
            {"id": "writer-template", "resources": [nameless]}, str(env.project)
        )
        == {}
    )


def test_object_form_declarations_do_not_spend_the_document_budget(env):
    """An entry nothing reads cannot exhaust the budget for entries that are read."""
    from kiro_crew.member_essential_context import documents_for_member

    knowledge_bases = [
        {"type": "knowledgeBase", "source": f"file://kb-{index}", "name": f"kb-{index}"}
        for index in range(70)
    ]
    resources = ["file://declared-guide.md", *knowledge_bases]
    (env.project / ".kiro" / "agents" / "writer-template.json").write_text(
        json.dumps({"name": "writer-template", "resources": resources}),
        encoding="utf-8",
    )
    documents = documents_for_member("writer-template", str(env.project))
    assert "Declared guide: examples must be reproducible." in [body for _, body in documents]


@pytest.mark.parametrize("malformed", [42, ["file://nested.md"], None])
def test_resources_still_refuse_an_entry_that_is_neither_uri_nor_object(env, malformed):
    """Admitting the object form is not the same as admitting anything.

    kiro-cli refuses the same shapes (``resource must be a string (file:// or
    skill://) or an object``). Asserted on the refusal, not its wording.
    """
    from kiro_crew.member_essential_context import (
        documents_for_member,
        projected_resource_documents,
    )

    resources = ["file://declared-guide.md", malformed]
    (env.project / ".kiro" / "agents" / "writer-template.json").write_text(
        json.dumps({"name": "writer-template", "resources": resources}),
        encoding="utf-8",
    )
    with pytest.raises(MemberEssentialContextError):
        documents_for_member("writer-template", str(env.project))
    with pytest.raises(MemberEssentialContextError):
        projected_resource_documents(
            {"id": "writer-template", "resources": resources}, str(env.project)
        )
