"""V2 keeps actual essential sources complete through every provider lifecycle."""

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from conftest import requires_symlinks
from kiro_crew import context as context_module
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.context import CONTEXT_GROUP_LESSONS, ContextBuilder
from kiro_crew.learn import LessonStore
from kiro_crew.member_essential_context import MemberEssentialContextError
from kiro_crew.members import slug_for_name, write_member_rules
from kiro_crew.memory import MemoryStore
from kiro_crew.memory_stores import (
    MEMBER_MEMORY_MANIFEST,
    UnknownMemoryStore,
    memory_store_dir_for,
    persist_member_config,
    provision_member_memory,
)
from kiro_crew.skills import SkillsLoader


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = KiroCrewConfig.load()
    cfg.agents["writer"] = KiroCrewAgentConfig(
        kiro_agent="writer-template", description="A careful bilingual writer"
    )
    store = provision_member_memory(cfg, "writer")
    persist_member_config(cfg, "writer", create=True)
    project = tmp_path / "project"
    (project / ".kiro" / "agents").mkdir(parents=True)
    (project / ".kiro" / "steering").mkdir()
    (project / ".kiro" / "agents" / "writer-template.json").write_text(
        json.dumps(
            {
                "name": "writer-template",
                "prompt": "Bound Soul: preserve the user's voice.",
                "resources": ["file://declared-guide.md"],
            }
        ),
        encoding="utf-8",
    )
    for path, body in {
        "AGENTS.md": "Project rules: run the review checks.",
        "SOUL.md": "Project Soul: write with empathy.",
        "declared-guide.md": "Declared guide: examples must be reproducible.",
        ".kiro/steering/always.md": "Always guide: explain assumptions.",
        ".kiro/steering/manual.md": "---\ninclusion: manual\n---\nMANUAL_SECRET",
        ".kiro/steering/match.md": "---\ninclusion: fileMatch\n---\nMATCH_SECRET",
        ".kiro/steering/auto.md": "---\ninclusion: auto\n---\nAUTO_SECRET",
    }.items():
        (project / path).write_text(body, encoding="utf-8")
    write_member_rules(slug_for_name("writer"), member="writer", text="Do not publish drafts.")
    forbidden = Mock(side_effect=AssertionError("essential context performed retrieval"))
    tier = SimpleNamespace(
        algorithm_version="v2",
        recall=forbidden,
        get_semantic_context=forbidden,
        get_episodic_context=forbidden,
        has_any_lesson=lambda: True,
        get_lessons_context=lambda **kwargs: "",
    )
    monkeypatch.setattr(context_module, "_memory_stores", {})
    monkeypatch.setattr(context_module, "_vector_stores", {store: tier})
    memory = ContextBuilder.get_memory_for(memory_store=store)
    memory.write_preferences("Preference anchor: 请保留中文原文。")
    memory.write_projects("Project anchor: the launch guide is authoritative.")
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "global"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path / "lessons"),
    )
    return SimpleNamespace(
        builder=builder, store=store, project=project, memory=memory, forbidden=forbidden
    )


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
def test_every_lifecycle_derives_owner_and_injects_actual_sources(env, fresh, options):
    message, _ = env.builder.build_message(
        "Continue",
        fresh,
        "cron:member-task",
        memory_store=env.store,
        project=str(env.project),
        **options,
    )
    for expected in (
        "You are writer.",
        "A careful bilingual writer",
        "Do not publish drafts.",
        "Bound Soul: preserve the user's voice.",
        "Project rules: run the review checks.",
        "Project Soul: write with empathy.",
        "Declared guide: examples must be reproducible.",
        "Always guide: explain assumptions.",
        "Preference anchor: 请保留中文原文。",
        "Project anchor: the launch guide is authoritative.",
        "memory_recall",
    ):
        assert expected in message
    assert message.count("[V2 ESSENTIAL CONTEXT") == 1
    assert "MANUAL_SECRET" not in message
    assert "MATCH_SECRET" not in message
    assert "AUTO_SECRET" not in message
    env.forbidden.assert_not_called()


def test_tail_and_updated_soul_survive_small_ordinary_context_budget(env):
    body = "Complete guide:\n" + "Important rule.\n" * 2200 + "TAIL_MUST_SURVIVE"
    (env.project / "AGENTS.md").write_text(body, encoding="utf-8")
    first = env.builder.build_session_context(
        memory_store=env.store, project=str(env.project), model_window=32_000
    )
    assert body in first
    (env.project / "SOUL.md").write_text("UPDATED_SOUL", encoding="utf-8")
    followup, _ = env.builder.build_message(
        "Continue", False, memory_store=env.store, project=str(env.project), model_window=32_000
    )
    assert body in followup and "UPDATED_SOUL" in followup
    env.forbidden.assert_not_called()


def test_oversized_essential_refuses_with_source_name_instead_of_partial_prompt(env):
    (env.project / "AGENTS.md").write_text("x" * 64_001, encoding="utf-8")
    with pytest.raises(MemberEssentialContextError, match="AGENTS.md"):
        env.builder.build_message(
            "Continue", False, memory_store=env.store, project=str(env.project)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("document", ["preferences", "projects"])
async def test_profile_save_rejects_oversized_candidate_without_replacing_anchors(env, document):
    from member_memory_helpers import request

    from kiro_crew.dashboard.handlers import memory as handlers

    state = SimpleNamespace(
        context_builder=env.builder,
        owner_id="owner",
        conversation_log=None,
        sessions=None,
        _restricted_keys=set(),
        _slots={},
        _store_markdown={env.store: env.memory},
    )
    before = env.memory.read_preferences(), env.memory.read_projects()
    req = request(
        SimpleNamespace(state=state),
        body={"content": "x" * 64_001},
        query={"store": env.store},
        owner=True,
        session="dashboard:ui",
    ).clone(method="PUT")
    handler = getattr(handlers, f"api_memory_{document}")
    response = await handler(req)
    assert response.status == 400
    assert json.loads(response.text)["code"] == "essential_context_invalid"
    assert f"{document}.md" in response.text
    assert (env.memory.read_preferences(), env.memory.read_projects()) == before
    env.forbidden.assert_not_called()


def test_profile_preflight_includes_the_other_anchor_without_persisting_candidate(env):
    from kiro_crew.config.sections import WorkspaceConfig
    from kiro_crew.dashboard.handlers.memory import _validate_private_profile_update

    cfg = KiroCrewConfig.load()
    cfg.workspaces["writer-project"] = WorkspaceConfig(dir=str(env.project))
    cfg.agents["writer"].workspace = "writer-project"
    cfg.save()
    env.memory.write_preferences("p" * 35_000)
    before = env.memory.read_projects()
    with pytest.raises(MemberEssentialContextError, match="preferences.md|projects.md"):
        _validate_private_profile_update(
            SimpleNamespace(context_builder=env.builder), env.store, "projects.md", "q" * 35_000
        )
    assert env.memory.read_projects() == before
    _validate_private_profile_update(
        SimpleNamespace(context_builder=env.builder), env.store, "projects.md", "A concise guide"
    )


def test_validated_profile_commit_excludes_a_concurrent_sibling_writer(env):
    competing = MemoryStore(
        workspace=env.memory._workspace,
        index_db=env.memory._index_db,
        memory_version=2,
    )
    competing.init()
    env.memory.write_preferences("before")
    writer_started = threading.Event()
    release_writer = threading.Event()

    def write_sibling() -> None:
        release_writer.wait()
        writer_started.set()
        competing.write_preferences("after")

    thread = threading.Thread(target=write_sibling)
    thread.start()

    def validate(normalized: str) -> None:
        assert normalized.startswith("# Active Projects")
        release_writer.set()
        assert writer_started.wait(timeout=5)
        # The second instance has reached its write call, but cannot replace
        # the sibling anchor while this validation owns the shared file lock.
        assert env.memory.read_preferences() == "before"

    try:
        env.memory.write_private_profile_validated("projects.md", "candidate", validate)
    finally:
        release_writer.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert env.memory.read_projects().splitlines()[-1] == "candidate"
    assert env.memory.read_preferences() == "after"


@pytest.mark.parametrize(
    "options", [{"blocks_reads": True}, {"context_groups": frozenset({CONTEXT_GROUP_LESSONS})}]
)
def test_explicit_withholding_keeps_conduct_but_never_reads_project_or_memory(env, options):
    # If read, this binary source causes a refusal; withholding must skip it.
    (env.project / "AGENTS.md").write_bytes(b"\xff")
    env.memory._guarded_entry = Mock(side_effect=AssertionError("withheld memory read"))
    message, _ = env.builder.build_message(
        "Continue", False, memory_store=env.store, project=str(env.project), **options
    )
    assert "You are writer." in message and "Do not publish drafts." in message
    assert "Bound Soul" in message
    assert "Project Soul" not in message and "Preference anchor" not in message
    assert "call memory_recall" not in message


def test_other_member_claim_and_missing_private_manifest_never_use_generic_identity(env):
    with pytest.raises(UnknownMemoryStore, match="belongs to"):
        env.builder.build_message("Continue", False, memory_store=env.store, member="other")
    (memory_store_dir_for(env.store) / MEMBER_MEMORY_MANIFEST).unlink()
    with pytest.raises(UnknownMemoryStore):
        env.builder.build_message("Continue", False, memory_store=env.store)


def test_unreadable_anchor_refuses_even_on_warm_turn(env):
    env.memory._preferences_file.write_bytes(b"\xff")
    with pytest.raises(MemberEssentialContextError, match="preferences"):
        env.builder.build_message(
            "Continue", False, memory_store=env.store, project=str(env.project)
        )


def test_declared_resource_cannot_escape_admitted_project_root(env, tmp_path):
    (tmp_path / "outside.md").write_text("OTHER_PROJECT_SECRET", encoding="utf-8")
    spec = env.project / ".kiro" / "agents" / "writer-template.json"
    spec.write_text(
        json.dumps({"name": "writer-template", "resources": ["file://../outside.md"]}),
        encoding="utf-8",
    )
    with pytest.raises(MemberEssentialContextError, match="outside.md"):
        env.builder.build_message(
            "Continue", False, memory_store=env.store, project=str(env.project)
        )


def test_nonrecursive_glob_does_not_expand_declared_scope(env):
    guides = env.project / "guides"
    (guides / "nested").mkdir(parents=True)
    (guides / "one.md").write_text("DIRECT_GUIDE", encoding="utf-8")
    (guides / "nested" / "two.md").write_text("NESTED_NOT_DECLARED", encoding="utf-8")
    spec = env.project / ".kiro" / "agents" / "writer-template.json"
    spec.write_text(json.dumps({"name": "writer-template", "resources": ["file://guides/*.md"]}))
    message, _ = env.builder.build_message(
        "Continue", False, memory_store=env.store, project=str(env.project)
    )
    assert "DIRECT_GUIDE" in message and "NESTED_NOT_DECLARED" not in message


def test_unreadable_project_template_does_not_fall_back_to_another_soul(env, monkeypatch):
    from kiro_crew import agent

    spec = env.project / ".kiro" / "agents" / "writer-template.json"
    spec.write_bytes(b"\xff")
    fallback = Mock(side_effect=AssertionError("unreadable private persona fell back"))
    monkeypatch.setattr(agent, "agent_spec_path", fallback)
    with pytest.raises(MemberEssentialContextError, match="writer-template.json"):
        env.builder.build_message(
            "Continue", False, memory_store=env.store, project=str(env.project)
        )
    fallback.assert_not_called()


@pytest.mark.parametrize("leaf", ["workspace/memory/preferences.md", "members/peer/briefing.md"])
def test_global_template_cannot_import_managed_memory_or_peer_briefing(env, monkeypatch, leaf):
    from pathlib import Path

    from kiro_crew import agent
    from kiro_crew.config import config_dir
    from kiro_crew.member_essential_context import documents_for_member

    path = config_dir() / leaf
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("OTHER_MEMORY_SECRET", encoding="utf-8")
    spec = env.project.parent / "global-template.json"
    spec.write_text(
        json.dumps({"name": "writer-template", "resources": [f"file://{path.as_posix()}"]})
    )
    monkeypatch.setattr(agent, "agent_spec_path", lambda _: spec)
    fake_home = config_dir().parent
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    with pytest.raises(MemberEssentialContextError, match="managed memory/member state"):
        documents_for_member("writer-template", None)


def test_runtime_override_keeps_the_memory_owners_soul(env):
    message, _ = env.builder.build_message(
        "Critique this draft",
        False,
        agent="critic-runtime",
        memory_store=env.store,
        project=str(env.project),
    )
    assert "You are writer." in message
    assert "Bound Soul: preserve the user's voice." in message


def test_literal_steering_prefix_does_not_enumerate_unrelated_project_root(env, monkeypatch):
    from pathlib import Path

    from kiro_crew import member_essential_context as essentials

    original = essentials.os.scandir

    def scoped_scan(path):
        if isinstance(path, int):
            return original(path)
        assert Path(path) != env.project, "literal project root was needlessly enumerated"
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(essentials.os, "scandir", scoped_scan)
        paths = essentials._matches(env.project, ".kiro/steering/**/*.md")
    assert env.project / ".kiro/steering/always.md" in paths


@requires_symlinks
@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows refuses a linked ANCESTOR by design (validate_file_path's "
    "linked-ancestor gate), so a symlinked declared root is correctly rejected there; "
    "the $HOME-symlink layout this admits is a POSIX arrangement.",
)
def test_matches_admits_a_declared_root_reached_through_a_symlink(env, tmp_path):
    """A root whose own spelling contains a link must still admit its documents.

    ``validate_file_path`` resolves, so an unresolved root matched nothing and was
    additionally refused as a linked directory on its first visit. That is the
    ordinary ``$HOME`` layout on hosts where ``/home/<user>`` links elsewhere,
    where it refused EVERY essential source for every private member.
    """
    from kiro_crew import member_essential_context as essentials

    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(env.project, target_is_directory=True)
    paths = essentials._matches(linked_root, ".kiro/steering/**/*.md")
    assert any(path.name == "always.md" for path in paths)


@requires_symlinks
@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows refuses a linked ANCESTOR by design (validate_file_path's "
    "linked-ancestor gate), so a symlinked declared root is correctly rejected there; "
    "the $HOME-symlink layout this admits is a POSIX arrangement.",
)
def test_read_admits_a_document_under_a_symlinked_root(env, tmp_path):
    """The containment check compares real paths, so either spelling admits."""
    from kiro_crew import member_essential_context as essentials

    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(env.project, target_is_directory=True)
    assert "Project rules" in essentials._read(linked_root / "AGENTS.md", linked_root)


@requires_symlinks
@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows refuses a linked ANCESTOR by design (validate_file_path's "
    "linked-ancestor gate), so a symlinked declared root is correctly rejected there; "
    "the $HOME-symlink layout this admits is a POSIX arrangement.",
)
def test_symlinked_root_still_refuses_a_document_outside_it(env, tmp_path):
    """Normalizing the root's spelling must not widen what the root contains."""
    from kiro_crew import member_essential_context as essentials

    outside = tmp_path / "outside.md"
    outside.write_text("OUTSIDE_SECRET", encoding="utf-8")
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(env.project, target_is_directory=True)
    with pytest.raises(MemberEssentialContextError, match="outside the admitted document root"):
        essentials._read(outside, linked_root)


def test_owner_cleared_empty_anchors_are_valid_but_missing_source_refuses(env):
    env.memory._preferences_file.write_text("", encoding="utf-8")
    env.memory._projects_file.write_text("", encoding="utf-8")
    message, _ = env.builder.build_message(
        "Continue", False, memory_store=env.store, project=str(env.project)
    )
    assert "You are writer." in message and "Project Soul" in message
    assert "Preference anchor" not in message and "Project anchor" not in message
    env.memory._preferences_file.unlink()
    with pytest.raises(MemberEssentialContextError, match="preferences"):
        env.builder.build_message(
            "Continue", False, memory_store=env.store, project=str(env.project)
        )


def test_default_workspace_guides_are_allowed_without_its_global_memory(env):
    from kiro_crew.config import config_dir
    from kiro_crew.member_essential_context import documents_for_member

    project = config_dir() / "workspace"
    project.mkdir(parents=True, exist_ok=True)
    (project / "AGENTS.md").write_text("DEFAULT_WORKSPACE_GUIDE", encoding="utf-8")
    (project / "SOUL.md").write_text("DEFAULT_WORKSPACE_SOUL", encoding="utf-8")
    agents = project / ".kiro/agents"
    agents.mkdir(parents=True, exist_ok=True)
    spec = agents / "workspace-template.json"
    spec.write_text(json.dumps({"name": "workspace-template", "prompt": "WORKSPACE_PERSONA"}))
    documents = documents_for_member("workspace-template", str(project))
    assert "DEFAULT_WORKSPACE_GUIDE" in [body for _, body in documents]
    assert "DEFAULT_WORKSPACE_SOUL" in [body for _, body in documents]
    memory = project / "memory"
    memory.mkdir(exist_ok=True)
    (memory / "preferences.md").write_text("GLOBAL_SECRET", encoding="utf-8")
    spec.write_text(json.dumps({"name": "workspace-template", "resources": ["file://memory/*.md"]}))
    with pytest.raises(MemberEssentialContextError, match="managed memory/member state"):
        documents_for_member("workspace-template", str(project))


@requires_symlinks
def test_glob_leaf_link_cannot_silently_drop_a_declared_guide(env):
    guides = env.project / "guides"
    guides.mkdir()
    (guides / "linked.md").symlink_to(env.project / "AGENTS.md")
    spec = env.project / ".kiro" / "agents" / "writer-template.json"
    spec.write_text(json.dumps({"name": "writer-template", "resources": ["file://guides/*.md"]}))
    with pytest.raises(MemberEssentialContextError, match="linked.md"):
        env.builder.build_message(
            "Continue", False, memory_store=env.store, project=str(env.project)
        )


@pytest.mark.parametrize("field, value", [("prompt", 42), ("resources", "file://guide.md")])
def test_malformed_declared_template_fields_refuse_explicitly(env, field, value):
    spec = env.project / ".kiro" / "agents" / "writer-template.json"
    spec.write_text(json.dumps({"name": "writer-template", field: value}), encoding="utf-8")
    with pytest.raises(MemberEssentialContextError, match=field):
        env.builder.build_message(
            "Continue", False, memory_store=env.store, project=str(env.project)
        )


def test_linked_directory_is_refused_before_enumerating_outside_sources(env, tmp_path):
    from conftest import make_dir_link

    target = tmp_path / "other-project"
    target.mkdir()
    (target / "secret.md").write_text("SIBLING_SECRET", encoding="utf-8")
    make_dir_link(env.project / "guides", target)
    spec = env.project / ".kiro" / "agents" / "writer-template.json"
    spec.write_text(json.dumps({"name": "writer-template", "resources": ["file://guides/*.md"]}))
    with pytest.raises(MemberEssentialContextError, match="guides"):
        env.builder.build_message(
            "Continue", False, memory_store=env.store, project=str(env.project)
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
