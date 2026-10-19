"""V2 keeps actual essential sources complete through every provider lifecycle."""

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import member_essential_context_helpers
import pytest

from conftest import make_dir_link, requires_symlinks
from kiro_crew import context as context_module
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.context import CONTEXT_GROUP_LESSONS
from kiro_crew.member_essential_context import MemberEssentialContextError
from kiro_crew.members import slug_for_name, write_member_rules
from kiro_crew.memory import MemoryStore
from kiro_crew.memory_stores import (
    UnknownMemoryStore,
    provision_member_memory,
)

# The shared fixtures, bound here by name so pytest registers them for this module.
_close_skills_loaders = member_essential_context_helpers._close_skills_loaders
env = member_essential_context_helpers.env


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


def test_oversized_guide_is_left_out_by_name_instead_of_refusing_the_turn(env):
    """An over-budget guide drops out whole and is named; the turn still runs.

    This replaced ``test_oversized_essential_refuses_with_source_name_instead_of_
    partial_prompt``: the source is still named and never cut mid-body, but the
    member's core keeps working instead of every turn refusing.
    """
    (env.project / "AGENTS.md").write_text("x" * 64_001, encoding="utf-8")
    message, _ = env.builder.build_message(
        "Continue", False, memory_store=env.store, member=env.member, project=str(env.project)
    )
    assert "x" * 1_000 not in message
    assert "[Essential source: essential-context#omitted]" in message
    assert f"{env.project / 'AGENTS.md'} (64,001 characters)" in message
    assert "Bound Soul: preserve the user's voice." in message
    assert "Preference anchor: 请保留中文原文。" in message


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


def _delegate_expectations(prompt: str, store: str, *, template_instructions: bool = True) -> None:
    """The subset a member turn with the desk WITHHELD receives.

    Shared by the template-selected delegate and the member selection off its
    desk (a plain chat on the alias, a cron or channel turn): the two arms of
    ``_desk_withheld``, one section shape.

    Identity, `[PERMANENT RULES]`, the store's anchors and the selected template's
    own instructions stay; the desk protocol and the briefing do not, in any of
    their spellings (the full layer, the platform placeholder, the scope
    placeholder), and neither does the rules header's reference to a protocol
    that is not there.
    """
    assert "You are writer." in prompt
    assert "A careful bilingual writer" in prompt
    assert "[PERMANENT RULES" in prompt
    assert "Do not publish drafts." in prompt
    assert "Preference anchor" in prompt
    if template_instructions:
        assert "Execution task instructions." in prompt
    assert "[HOW YOU WORK]" not in prompt
    assert "Front desk vs workshop" not in prompt
    assert "[CURRENT ASSIGNMENT" not in prompt
    assert "working protocol above" not in prompt
    assert prompt.count("[V2 ESSENTIAL CONTEXT") == 1
    assert store in prompt or "memory_recall" in prompt


def _template_delegate(config):
    """The execution both delegate paths mint for a member caller naming a template.

    `session_create(agent=<template>)` and the subagent admission gate agree on it:
    the member's store and id stay, the selection namespace is the template's.
    """
    from dataclasses import replace

    from kiro_crew.execution_context import resolve_member_execution

    return replace(
        resolve_member_execution(config, "writer"),
        selection_kind="template",
        selection_name="task-template",
        template_id="task-template",
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
def test_template_selected_delegate_keeps_identity_and_rules_but_not_the_desk_protocol(
    env, fresh, options
):
    """The member's store under a selected template is the member's DELEGATE.

    It reads and writes the member's memory, so it is that member and runs under
    that member's rules; it was picked to do the work, so the desk protocol whose
    second item hands substantial work to a separate session -- and the working
    briefing that protocol maintains -- are withheld on every lifecycle the
    envelope is rebuilt for.
    """
    delegate = _template_delegate(KiroCrewConfig.load())
    prompt, _ = env.builder.build_message(
        "Do the task",
        fresh,
        "dashboard:delegate",
        execution_context=delegate,
        agent="task-template",
        project=str(env.project),
        **options,
    )
    _delegate_expectations(prompt, env.store)
    assert delegate.store.store_id == env.store
    env.forbidden.assert_not_called()


def test_template_selected_delegate_session_start_withholds_the_desk_protocol(env):
    prompt = env.builder.build_session_context(
        execution_context=_template_delegate(KiroCrewConfig.load()),
        agent="task-template",
        project=str(env.project),
    )
    _delegate_expectations(prompt, env.store)
    env.forbidden.assert_not_called()


@pytest.mark.parametrize("entrypoint", ["message", "session"])
def test_member_at_its_desk_gets_its_whole_desk(env, entrypoint):
    """The counterpart: a member turn the caller names as the DESK keeps all four
    layers, protocol included. ``member=`` is what the dashboard passes for a
    ``mode == "member"`` slot -- the pinned DM thread."""
    from kiro_crew.execution_context import resolve_member_execution

    execution = resolve_member_execution(KiroCrewConfig.load(), "writer")
    options = dict(execution_context=execution, project=str(env.project), member="writer")
    if entrypoint == "message":
        prompt, _ = env.builder.build_message("Continue", True, **options)
    else:
        prompt = env.builder.build_session_context(**options)
    assert "You are writer." in prompt
    assert "This DM thread is your durable working relationship" in prompt
    assert "[HOW YOU WORK]" in prompt
    assert "Front desk vs workshop" in prompt
    assert "[PERMANENT RULES" in prompt
    assert "working protocol above included" in prompt
    assert "[CURRENT ASSIGNMENT" in prompt
    env.forbidden.assert_not_called()


@pytest.mark.parametrize("entrypoint", ["message", "session"])
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
def test_member_selected_off_its_desk_keeps_identity_and_rules_but_not_the_desk(
    env, entrypoint, fresh, options
):
    """A member SELECTION with no desk argument is not the member's desk.

    An ordinary dashboard chat that resolved to a crew alias -- every plain chat
    lands on the stock ``default`` alias this way -- a cron turn or a channel turn
    carries the member's record and therefore its identity, rules and memory, but
    no caller named it as the member's DM thread. It gets exactly what a delegate
    gets: identity and `[PERMANENT RULES]`, no `[HOW YOU WORK]`, no briefing (so
    nothing tells it to rewrite the briefing file), and no sentence describing a
    DM thread it is not in. Pinned on every lifecycle the section is rebuilt for
    and on both entry points.
    """
    from kiro_crew.execution_context import resolve_member_execution

    execution = resolve_member_execution(KiroCrewConfig.load(), "writer")
    base = dict(execution_context=execution, project=str(env.project))
    if entrypoint == "message":
        prompt, _ = env.builder.build_message(
            "Continue", fresh, "dashboard:chat-1-123", **base, **options
        )
    else:
        if not fresh:
            pytest.skip("build_session_context is the session-start entry point")
        prompt = env.builder.build_session_context(**base, **options)
    _delegate_expectations(prompt, env.store, template_instructions=False)
    assert "This DM thread is your durable working relationship" not in prompt
    env.forbidden.assert_not_called()


def test_a_delegate_with_memory_withheld_gets_no_layer_four_placeholder_either(env):
    """The scope notice explains a member's missing layer 4; a delegate has none.

    With the memory group withheld, the member AT ITS DESK is told why its
    briefing is missing and not to fill the gap; the delegate on the same store
    keeps identity and rules and gets no `[CURRENT ASSIGNMENT` line of any kind.
    """
    from kiro_crew.context import CONTEXT_GROUP_PROJECT
    from kiro_crew.execution_context import resolve_member_execution

    config = KiroCrewConfig.load()
    options = dict(project=str(env.project), context_groups=frozenset({CONTEXT_GROUP_PROJECT}))
    member_prompt, _ = env.builder.build_message(
        "Continue",
        True,
        execution_context=resolve_member_execution(config, "writer"),
        member="writer",
        **options,
    )
    assert "withheld by this turn's memory/privacy scope]" in member_prompt
    delegate_prompt, _ = env.builder.build_message(
        "Do the task",
        True,
        execution_context=_template_delegate(config),
        agent="task-template",
        **options,
    )
    assert "You are writer." in delegate_prompt
    assert "[PERMANENT RULES" in delegate_prompt
    assert "[CURRENT ASSIGNMENT" not in delegate_prompt
    assert "[HOW YOU WORK]" not in delegate_prompt
    env.forbidden.assert_not_called()


def test_spawn_agent_delegate_of_a_member_is_the_same_delegate(env):
    """`spawn_run(agent=<template>)` from a member parent mints the same record
    shape the session arm does, so the sub-agent's prompt withholds the same
    layers -- one predicate, both delegate paths."""
    from unittest.mock import MagicMock

    from kiro_crew.execution_context import resolve_member_execution
    from kiro_crew.subagent import SubagentManager

    config = KiroCrewConfig.load()
    parent = resolve_member_execution(config, "writer")
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    admitted = manager._admission.resolve_spawn_execution(
        parent_session_key="dashboard:writer", agent="task-template", _record=parent
    )
    assert admitted.selection_kind == "template"
    assert admitted.member_id == parent.member_id
    assert admitted.store == parent.store
    prompt, _ = env.builder.build_message(
        "Do the task",
        True,
        "subagent:delegate",
        execution_context=admitted,
        agent="task-template",
        project=str(env.project),
    )
    _delegate_expectations(prompt, env.store)
    env.forbidden.assert_not_called()


def test_a_member_with_no_persisted_id_keeps_its_rules_when_session_create_names_a_template(
    env,
):
    """The record shape the split cannot express, on the `session_create` arm.

    A member whose record predates persisted identity is named by `selection_kind
    == "member"` and `selection_name` alone. Rewriting those to the template's
    namespace would leave a record ContextBuilder attributes to no member -- no
    identity and, the part that matters, no `[PERMANENT RULES]`. The arm keeps the
    selection and changes only the template (the record
    `test_explicit_template_child_of_a_member_with_no_persisted_id_keeps_its_selection`
    pins through the arm itself), so the child stays that member with its rules.
    It does NOT keep the desk: no caller names a delegate as the member's
    DM thread, so the surface half of ``_desk_withheld`` withholds the protocol
    and the briefing without the record having to say "this member, under that
    template".
    """
    from dataclasses import replace

    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

    config = KiroCrewConfig.load()
    config.agents["scribe"] = KiroCrewAgentConfig(
        kiro_agent="critic-runtime", description="A scribe with no persisted identity"
    )
    config.save()
    write_member_rules(slug_for_name("scribe"), member="scribe", text="Scribe: keep every draft.")
    legacy = ExecutionContext(
        None, MemoryStoreRef("default"), "member", "critic-runtime", selection_name="scribe"
    )
    child = replace(legacy, template_id="task-template")
    prompt, _ = env.builder.build_message(
        "Do the task",
        True,
        "session_create:delegate",
        execution_context=child,
        agent="task-template",
        project=str(env.project),
    )
    assert "You are scribe." in prompt
    assert "A scribe with no persisted identity" in prompt
    assert "[PERMANENT RULES" in prompt
    assert "Scribe: keep every draft." in prompt
    assert "Execution task instructions." in prompt
    assert "[HOW YOU WORK]" not in prompt
    assert "[CURRENT ASSIGNMENT" not in prompt
    assert "This DM thread" not in prompt
    env.forbidden.assert_not_called()


def test_a_member_with_no_persisted_id_spawns_a_plain_template_child(env):
    """The spawn gate's `agent=` child of such a member is a template run.

    `with_template` flips the selection namespace for every record, and a member
    with no persisted id has no identity field to survive that, so its
    `spawn_run(agent=...)` child is a plain template run on the parent's store
    with no member section -- neither the desk protocol nor the rules (the record
    cannot say "this member, under that template"; a record-shape change for the
    memory model). A continuation of
    that child is a template turn: `run_turn.py` takes the kind from the record, the
    member refresh is skipped, the spawn template is kept, and no member is
    looked up, so a renamed alias is not an error.
    """
    from unittest.mock import MagicMock

    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
    from kiro_crew.subagent import SubagentManager

    config = KiroCrewConfig.load()
    config.agents["scribe"] = KiroCrewAgentConfig(
        kiro_agent="critic-runtime", description="A scribe with no persisted identity"
    )
    config.save()
    write_member_rules(slug_for_name("scribe"), member="scribe", text="Scribe: keep every draft.")
    legacy = ExecutionContext(
        None, MemoryStoreRef("default"), "member", "critic-runtime", selection_name="scribe"
    )
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    child = manager._admission.resolve_spawn_execution(
        parent_session_key="dashboard:scribe", agent="task-template", _record=legacy
    )
    assert (child.member_id, child.selection_kind, child.selection_name) == (
        None,
        "template",
        "task-template",
    )
    assert child.template_id == "task-template"
    assert child.store == legacy.store
    prompt, _ = env.builder.build_message(
        "Do the task",
        True,
        "subagent:delegate",
        execution_context=child,
        agent="task-template",
        project=str(env.project),
    )
    assert "Execution task instructions." in prompt
    assert "You are scribe." not in prompt
    assert "[PERMANENT RULES" not in prompt
    assert "[HOW YOU WORK]" not in prompt
    continued = manager._admission.resolve_spawn_execution(
        conversation_key="subagent:child", _record=child
    )
    assert continued.template_id == "task-template"
    assert continued == child
    config.agents["scribe-renamed"] = config.agents.pop("scribe")
    config.save()
    renamed = manager._admission.resolve_spawn_execution(
        conversation_key="subagent:child", _record=child
    )
    assert renamed == child
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
