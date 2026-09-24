"""V2 keeps actual essential sources complete through every provider lifecycle."""

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from conftest import make_dir_link, requires_symlinks
from kiro_crew import context as context_module
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.context import CONTEXT_GROUP_LESSONS, ContextBuilder
from kiro_crew.learn import LessonStore
from kiro_crew.member_essential_context import MemberEssentialContextError
from kiro_crew.members import slug_for_name, write_member_rules
from kiro_crew.memory import MemoryStore
from kiro_crew.memory_stores import (
    UnknownMemoryStore,
    memory_store_dir_for,
    persist_member_config,
    provision_member_memory,
)
from kiro_crew.skills import SkillsLoader


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "host-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("KIRO_HOME", str(home / ".kiro"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # The host floor pins these hooks separately from the lazy KIRO_HOME path.
    # Keep template readers on the same test-owned root as template writers.
    agents = home / ".kiro" / "agents"
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", agents)
    monkeypatch.setattr("kiro_crew.agent_discovery._KIRO_AGENTS_DIR", agents)
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
    for template in ("critic-runtime", "task-template"):
        (project / ".kiro" / "agents" / f"{template}.json").write_text(
            json.dumps({"name": template, "prompt": "Execution task instructions."}),
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
    from kiro_crew.vector_memory import open_member_database

    tier = open_member_database(
        memory_store_dir_for(store) / "memory.db",
        member_id=cfg.agents["writer"].member_id,
        store_id=store,
    )
    monkeypatch.setattr(tier, "recall", forbidden)
    monkeypatch.setattr(tier, "get_semantic_context", forbidden)
    monkeypatch.setattr(tier, "get_episodic_context", forbidden)
    monkeypatch.setattr(tier, "get_lessons_context", lambda **kwargs: "")
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
    try:
        yield SimpleNamespace(
            builder=builder,
            store=store,
            member=cfg.agents["writer"].member_id,
            project=project,
            memory=memory,
            forbidden=forbidden,
        )
    finally:
        # ``open_member_database`` opens a process-lifetime SQLite connection
        # plus a store-use lock descriptor that nothing else closes; release
        # them so each parametrisation does not leak two descriptors.
        tier.close()


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
        member=env.member,
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
        memory_store=env.store, member=env.member, project=str(env.project), model_window=32_000
    )
    assert body in first
    (env.project / "SOUL.md").write_text("UPDATED_SOUL", encoding="utf-8")
    followup, _ = env.builder.build_message(
        "Continue",
        False,
        memory_store=env.store,
        member=env.member,
        project=str(env.project),
        model_window=32_000,
    )
    assert body in followup and "UPDATED_SOUL" in followup
    env.forbidden.assert_not_called()


def test_oversized_essential_refuses_with_source_name_instead_of_partial_prompt(env):
    (env.project / "AGENTS.md").write_text("x" * 64_001, encoding="utf-8")
    with pytest.raises(MemberEssentialContextError, match="AGENTS.md"):
        env.builder.build_message(
            "Continue", False, memory_store=env.store, member=env.member, project=str(env.project)
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
        vector_store=env.memory.vector_store,
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
        "Continue",
        False,
        memory_store=env.store,
        member=env.member,
        project=str(env.project),
        **options,
    )
    assert "You are writer." in message and "Do not publish drafts." in message
    assert "Bound Soul" in message
    assert "Project Soul" not in message and "Preference anchor" not in message
    assert "call memory_recall" not in message


def test_unknown_member_identity_refuses_without_inference_from_store(env):
    with pytest.raises(UnknownMemoryStore, match="member identity"):
        env.builder.build_message("Continue", False, memory_store=env.store, member="other")


def test_unavailable_database_preserves_complete_member_context(env, monkeypatch):
    def unavailable(*args, **kwargs):
        raise UnknownMemoryStore("member database unavailable")

    monkeypatch.setattr(env.builder, "get_memory_for", unavailable)
    monkeypatch.setattr(context_module, "_vector_stores", {})
    message = env.builder.build_session_context(
        memory_store=env.store,
        member=env.member,
        project=str(env.project),
    )
    assert "Do not publish drafts." in message
    assert "Bound Soul: preserve the user's voice." in message
    assert "Project rules: run the review checks." in message
    assert "[Member memory unavailable]" in message
    assert "Global memory was not used" in message


def test_cold_member_prompt_never_constructs_learned_memory(env, monkeypatch):
    monkeypatch.setattr(context_module, "_memory_stores", {})
    monkeypatch.setattr(context_module, "_vector_stores", {})
    forbidden = Mock(side_effect=AssertionError("prompt opened the learned store"))
    monkeypatch.setattr(env.builder, "get_memory_for", forbidden)
    message, _ = env.builder.build_message(
        "Continue after restart",
        True,
        member=env.member,
        memory_store=env.store,
        project=str(env.project),
    )
    assert "Do not publish drafts." in message
    assert "Bound Soul: preserve the user's voice." in message
    assert "Preference anchor" in message
    assert "[Member memory unavailable]" in message
    forbidden.assert_not_called()


@pytest.mark.parametrize("entrypoint", ["message", "session"])
@pytest.mark.parametrize("selection", ["missing-alias", "colliding-alias", "captured-member"])
def test_execution_namespace_controls_member_prompt_identity(env, entrypoint, selection):
    from dataclasses import replace

    from kiro_crew.execution_context import execution_for_store, resolve_member_execution

    config = KiroCrewConfig.load()
    template = "writer" if selection == "colliding-alias" else "critic-runtime"
    (env.project / ".kiro" / "agents" / f"{template}.json").write_text(
        json.dumps({"name": template, "prompt": "CAPTURED_TEMPLATE_INSTRUCTIONS"}),
        encoding="utf-8",
    )
    if selection == "captured-member":
        execution = replace(
            resolve_member_execution(config, "writer"),
            selection_kind="template",
            selection_name=template,
            template_id=template,
        )
        config.agents["writer"].kiro_agent = "task-template"
        config.save()
    else:
        execution = execution_for_store("default", template_id=template)
    options = dict(
        execution_context=execution,
        agent=template,
        project=str(env.project),
        minimal_context=True,
    )
    if entrypoint == "message":
        prompt, _ = env.builder.build_message("Continue the task", True, **options)
        assert "Continue the task" in prompt
    else:
        prompt = env.builder.build_session_context(**options)
    if selection == "captured-member":
        assert "A careful bilingual writer" in prompt
        assert "Do not publish drafts." in prompt
        assert "Preference anchor" in prompt
        assert "CAPTURED_TEMPLATE_INSTRUCTIONS" in prompt
        assert "Execution task instructions." not in prompt
        assert execution.store.store_id == env.store
    else:
        assert "[MEMBER IDENTITY]" not in prompt
        assert "[V2 ESSENTIAL CONTEXT" not in prompt
        assert "Do not publish drafts." not in prompt
        assert "Preference anchor" not in prompt
        assert execution.store.store_id == "default"
    env.forbidden.assert_not_called()


@pytest.mark.parametrize("other_v2", [False, True])
def test_captured_member_id_wins_over_another_members_alias(env, other_v2):
    from kiro_crew.execution_context import resolve_member_execution

    config = KiroCrewConfig.load()
    execution = resolve_member_execution(config, "writer")
    config.agents["original-writer"] = config.agents.pop("writer")
    config.agents[env.member] = KiroCrewAgentConfig(
        kiro_agent="critic-runtime", description="OTHER_MEMBER_PERSONA"
    )
    if other_v2:
        provision_member_memory(config, env.member)
    config.save()
    message, _ = env.builder.build_message(
        "Keep the captured identity",
        True,
        execution_context=execution,
        project=str(env.project),
    )
    assert "A careful bilingual writer" in message
    assert "Bound Soul: preserve the user's voice." in message
    assert "OTHER_MEMBER_PERSONA" not in message
    assert "Execution task instructions." not in message
    env.forbidden.assert_not_called()


@pytest.mark.parametrize("entrypoint", ["message", "session"])
@pytest.mark.parametrize("replacement_v2", [False, True])
def test_deleted_captured_member_never_renders_replacement_persona(env, entrypoint, replacement_v2):
    from kiro_crew.execution_context import resolve_member_execution

    config = KiroCrewConfig.load()
    captured = resolve_member_execution(config, "writer")
    assert captured.member_id == "writer"
    del config.agents["writer"]
    config.agents["writer"] = KiroCrewAgentConfig(
        kiro_agent="critic-runtime", description="REPLACEMENT_PERSONA"
    )
    if replacement_v2:
        provision_member_memory(config, "writer")
    config.save()
    write_member_rules(
        config.agents["writer"].member_id or "writer",
        member="writer",
        text="REPLACEMENT_PERMANENT_RULE",
    )
    options = dict(execution_context=captured, project=str(env.project))
    # Refuse the missing stable identity instead of combining a replacement's
    # persona/rules with the retained execution's old memory anchors.
    with pytest.raises(UnknownMemoryStore, match="member identity"):
        if entrypoint == "message":
            env.builder.build_message("Continue the retained task", True, **options)
        else:
            env.builder.build_session_context(**options)


@pytest.mark.parametrize("entrypoint", ["message", "session"])
@pytest.mark.parametrize("selected_v2", [False, True])
def test_explicit_member_name_does_not_select_another_members_id(env, entrypoint, selected_v2):
    config = KiroCrewConfig.load()
    config.agents["original-writer"] = config.agents.pop("writer")
    config.agents["writer"] = KiroCrewAgentConfig(
        kiro_agent="critic-runtime", description="EXPLICIT_NAME_PERSONA"
    )
    store = provision_member_memory(config, "writer") if selected_v2 else "default"
    config.save()
    options = dict(member="writer", memory_store=store, project=str(env.project))
    if entrypoint == "message":
        prompt, _ = env.builder.build_message("Use the selected member", True, **options)
    else:
        prompt = env.builder.build_session_context(**options)
    assert "EXPLICIT_NAME_PERSONA" in prompt
    assert "A careful bilingual writer" not in prompt
    assert "Bound Soul: preserve the user's voice." not in prompt


def test_strict_member_section_and_profile_validation_refuse_deleted_id(env):
    config = KiroCrewConfig.load()
    config.agents["writer"] = KiroCrewAgentConfig(description="REPLACEMENT_V1_PERSONA")
    config.save()
    with pytest.raises(UnknownMemoryStore, match="member identity"):
        env.builder._build_member_section(env.member, strict=True)
    with pytest.raises(UnknownMemoryStore, match="member identity"):
        env.builder._build_v2_essentials(env.store, member=env.member)


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [False, True])
async def test_workflow_cold_prompt_prepares_only_optional_lessons(env, monkeypatch, available):
    import asyncio

    from kiro_crew import embeddings
    from kiro_crew.execution_context import resolve_member_execution
    from kiro_crew.workflow_memory import WorkflowScope

    vectors = {}
    monkeypatch.setattr(context_module, "_memory_stores", {})
    monkeypatch.setattr(context_module, "_vector_stores", vectors)
    no_model = Mock(side_effect=AssertionError("prompt performed embedding"))
    monkeypatch.setattr(embeddings, "_shared_sync_embed", no_model)
    if not available:

        async def unavailable(_store):
            raise UnknownMemoryStore("synthetic unavailable database")

        monkeypatch.setattr(env.builder, "ensure_store", unavailable)
    config = await asyncio.to_thread(KiroCrewConfig.load)
    execution = resolve_member_execution(config, "writer")
    scope = WorkflowScope("wf_000099", env.store, "", execution_context=execution)
    try:
        message = await scope.prompt(
            env.builder,
            scope.worker_key("cold"),
            "Continue after restart",
            is_new=True,
            agent="writer-template",
            cwd=str(env.project),
            provider=None,
        )
        assert "Do not publish drafts." in message
        assert "Bound Soul: preserve the user's voice." in message
        assert ("[Member memory unavailable]" in message) is not available
        assert bool(vectors) is available
        no_model.assert_not_called()
    finally:
        for store in vectors.values():
            store.close()


@pytest.mark.parametrize("options", [{"blocks_reads": True}, {"context_groups": frozenset()}])
def test_withheld_memory_never_opens_database(env, monkeypatch, options):
    forbidden = Mock(side_effect=AssertionError("withheld memory opened a database"))
    monkeypatch.setattr(env.builder, "get_memory_for", forbidden)
    message = env.builder.build_session_context(
        memory_store=env.store,
        member=env.member,
        project=str(env.project),
        **options,
    )
    assert "Do not publish drafts." in message
    assert "Bound Soul: preserve the user's voice." in message
    forbidden.assert_not_called()


def test_unreadable_anchor_refuses_even_on_warm_turn(env):
    env.memory._preferences_file.write_bytes(b"\xff")
    with pytest.raises(MemberEssentialContextError, match="preferences"):
        env.builder.build_message(
            "Continue", False, memory_store=env.store, member=env.member, project=str(env.project)
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
            "Continue", False, memory_store=env.store, member=env.member, project=str(env.project)
        )


def test_nonrecursive_glob_does_not_expand_declared_scope(env):
    guides = env.project / "guides"
    (guides / "nested").mkdir(parents=True)
    (guides / "one.md").write_text("DIRECT_GUIDE", encoding="utf-8")
    (guides / "nested" / "two.md").write_text("NESTED_NOT_DECLARED", encoding="utf-8")
    spec = env.project / ".kiro" / "agents" / "writer-template.json"
    spec.write_text(json.dumps({"name": "writer-template", "resources": ["file://guides/*.md"]}))
    message, _ = env.builder.build_message(
        "Continue", False, memory_store=env.store, member=env.member, project=str(env.project)
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
            "Continue", False, memory_store=env.store, member=env.member, project=str(env.project)
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
        member=env.member,
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


@requires_symlinks
@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows refuses a linked ANCESTOR by design (validate_file_path's "
    "linked-ancestor gate), so a symlinked declared root is correctly rejected there; "
    "the $HOME-symlink layout this admits is a POSIX arrangement.",
)
def test_absolute_resource_in_realpath_spelling_admits_under_a_linked_root(env, tmp_path):
    """A realpath-spelled declaration must resolve against a link-spelled root.

    An installer records an absolute ``file://`` resource in its realpath spelling
    while the declared root stays the link, which is the ordinary ``$HOME`` layout
    wherever ``/home/<user>`` points at another filesystem. The lexical
    ``relative_to`` then called a resource genuinely inside the root outside it
    and refused every absolute essential source on such a host.
    """
    from kiro_crew import member_essential_context as essentials

    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(env.project, target_is_directory=True)
    real = Path(os.path.realpath(str(linked_root))) / "declared-guide.md"
    assert str(real) != str(linked_root / "declared-guide.md")
    paths = essentials._resource_paths([f"file://{real}"], linked_root, linked_root)
    assert paths, "a realpath-spelled resource under the linked root was refused"
    match, root = paths[0]
    assert "Declared guide" in essentials._read(match, root)


@requires_symlinks
@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows refuses a linked ANCESTOR by design (validate_file_path's "
    "linked-ancestor gate), so a symlinked declared root is correctly rejected there; "
    "the $HOME-symlink layout this admits is a POSIX arrangement.",
)
def test_absolute_resource_outside_a_linked_root_is_still_refused(env, tmp_path):
    """Accepting the root's other spelling must not admit a sibling of the root."""
    from kiro_crew import member_essential_context as essentials

    outside = tmp_path / "outside-guide.md"
    outside.write_text("OUTSIDE_SECRET", encoding="utf-8")
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(env.project, target_is_directory=True)
    with pytest.raises(MemberEssentialContextError, match="outside"):
        essentials._resource_paths([f"file://{outside}"], linked_root, linked_root)


def test_absolute_resource_in_link_spelling_admits_under_a_resolved_root(env, tmp_path):
    """A link-spelled declaration must resolve against a realpath-spelled root.

    The reverse of the installer case: a project root is stored resolved while
    the template records the resource through the ``$HOME`` link, so neither
    spelling of the root is a lexical prefix of the declaration.
    """
    from kiro_crew import member_essential_context as essentials

    linked_root = tmp_path / "linked-root"
    make_dir_link(linked_root, env.project)
    real_root = Path(os.path.realpath(str(linked_root)))
    declared = linked_root / "declared-guide.md"
    paths = essentials._resource_paths([f"file://{declared}"], real_root, real_root)
    assert paths, "a link-spelled resource under the resolved root was refused"
    match, root = paths[0]
    assert "Declared guide" in essentials._read(match, root)


def test_link_spelled_resource_still_refuses_outside_and_links_below_root(env, tmp_path):
    """Matching the root's link spelling admits neither a sibling nor a link below it."""
    from kiro_crew import member_essential_context as essentials

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "guide.md").write_text("OUTSIDE_SECRET", encoding="utf-8")

    linked_root = tmp_path / "linked-root"
    make_dir_link(linked_root, env.project)
    real_root = Path(os.path.realpath(str(linked_root)))
    with pytest.raises(MemberEssentialContextError, match="outside"):
        essentials._resource_paths([f"file://{outside / 'guide.md'}"], real_root, real_root)
    make_dir_link(env.project / "escape", outside)
    paths = essentials._resource_paths(
        [f"file://{linked_root / 'escape' / 'guide.md'}"], real_root, real_root
    )
    with pytest.raises(MemberEssentialContextError, match="outside"):
        for match, root in paths:
            essentials._read(match, root)


def test_owner_cleared_empty_anchors_are_valid_but_missing_source_refuses(env):
    env.memory._preferences_file.write_text("", encoding="utf-8")
    env.memory._projects_file.write_text("", encoding="utf-8")
    message, _ = env.builder.build_message(
        "Continue", False, memory_store=env.store, member=env.member, project=str(env.project)
    )
    assert "You are writer." in message and "Project Soul" in message
    assert "Preference anchor" not in message and "Project anchor" not in message
    env.memory._preferences_file.unlink()
    with pytest.raises(MemberEssentialContextError, match="preferences"):
        env.builder.build_message(
            "Continue", False, memory_store=env.store, member=env.member, project=str(env.project)
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
            "Continue", False, memory_store=env.store, member=env.member, project=str(env.project)
        )


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


@pytest.mark.parametrize("template", ["writer-template", "kirocrew"])
@pytest.mark.parametrize("source", ["inline", "relative", "absolute"])
def test_custom_persona_is_not_classified_by_template_name(env, template, source):
    cfg = KiroCrewConfig.load()
    cfg.agents["writer"].kiro_agent = template
    cfg.save()
    persona = env.project / "prompt.md"
    persona.write_text("CUSTOM_OWNER_PERSONA", encoding="utf-8")
    prompts = {
        "inline": "CUSTOM_OWNER_PERSONA",
        "relative": "file://prompt.md",
        "absolute": f"file://{persona}",
    }
    spec = env.project / ".kiro" / "agents" / f"{template}.json"
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
