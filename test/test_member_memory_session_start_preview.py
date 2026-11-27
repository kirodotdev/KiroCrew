"""Test recall's session-start preview is the session-start build's own text."""

from __future__ import annotations

import json

import pytest
from member_memory_helpers import env as _member_env
from member_memory_helpers import request

from kiro_crew.dashboard.handlers import memory

env = _member_env
pytestmark = pytest.mark.xdist_group("member_memory_api")


@pytest.fixture
def on_teardown():
    """Run each registered callable after the test, whichever way it ended."""
    callbacks: list = []
    yield callbacks.append
    for callback in reversed(callbacks):
        callback()


def _session_start_builder(env, monkeypatch, on_teardown):
    """A real ContextBuilder over the fixture's global store, as the gateway wires it."""
    from kiro_crew import context as ctx
    from kiro_crew.hooks import HookManager
    from kiro_crew.learn import LessonStore
    from kiro_crew.skills import SkillsLoader

    global_memory = env.state.context_builder.memory
    skills = SkillsLoader(skills_path=env.home / "empty-skills", install_builtins=False)
    on_teardown(skills.close)
    builder = ctx.ContextBuilder(
        memory=global_memory,
        skills=skills,
        lessons=LessonStore(base_dir=env.home / "lessons"),
        hooks=HookManager(),
    )
    monkeypatch.setattr(ctx, "agent_skill_globs", lambda agent: [])
    monkeypatch.setattr(ctx, "kiro_agents_dir", lambda: env.home / "empty-agents")
    monkeypatch.setattr(
        builder, "get_memory_for", lambda workspace=None, memory_store=None: global_memory
    )
    # The gateway's cache holds the member tier ensure_store opened.
    monkeypatch.setitem(ctx._vector_stores, "member-alice", env.tiers["member-alice"])
    env.state.context_builder = builder
    return builder


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["", "member-alice"], ids=["global_v1", "member_v2"])
async def test_no_query_preview_is_the_session_start_text(env, monkeypatch, on_teardown, store):
    builder = _session_start_builder(env, monkeypatch, on_teardown)
    tier = env.tiers[store]
    assert tier.algorithm_version == ("v2" if store else "v1")
    if not store:
        builder.memory.write_preferences("# Preferences\nUse short answers. PREF_SENTINEL\n")
    tier.set_semantic(
        "lesson.worktree",
        json.dumps(
            {"rule": "Use a worktree LESSON_SENTINEL", "applies": "always", "category": "tool"}
        ),
        1.0,
        "user_explicit",
    )
    query = {"store": store} if store else {}
    response = await memory.api_memory_context_preview(
        request(env, owner=True, session="dashboard:ui", query=query)
    )
    assert response.status == 200, response.text
    parts = json.loads(response.text)["session_start"]["parts"]
    if store:
        started = builder.build_session_context(
            member="alice", memory_store=store, _v2_essentials=""
        )
    else:
        started = builder.build_session_context()
        assert "PREF_SENTINEL" in _family(parts, "memory")
    assert _order(parts) == ["memory", "lessons"]
    assert "[Memory tools]" in _family(parts, "memory")
    assert "LESSON_SENTINEL" in _family(parts, "lessons")
    # Each part is the session-start build's own text, not a copy of its wiring.
    _assert_parts_in(parts, started)


def _family(parts, family: str) -> str:
    """The text of every *family* part, joined."""
    return "".join(part["text"] for part in parts if part["family"] == family)


def _order(parts) -> list[str]:
    """The families in prompt order, a run of one family's blocks counted once."""
    order: list[str] = []
    for part in parts:
        if not order or order[-1] != part["family"]:
            order.append(part["family"])
    return order


def _assert_parts_in(parts, started: str) -> None:
    for part in parts:
        assert part["text"] in started, part["family"]


@pytest.fixture
def provider(monkeypatch, tmp_path):
    """The fixture memory provider app, served over its real wire."""
    from memory_provider_helpers import fixture_provider

    with fixture_provider(monkeypatch, tmp_path / "provider.json") as served:
        yield served


def _provider_preview(builder, store=None, *, private=False):
    from kiro_crew.context_assembly.store_admission import session_start_store_preview

    parts = session_start_store_preview(builder, store, private=private)
    return [{"family": family, "text": text} for family, text in parts]


def test_a_replacing_provider_previews_its_own_blocks(env, monkeypatch, on_teardown, provider):
    from memory_provider_helpers import FIXTURE_ID, set_provider

    from kiro_crew.memory_provider.contract import NewItem, ProviderScope

    builder = _session_start_builder(env, monkeypatch, on_teardown)
    builder.memory.write_preferences("# Preferences\nCREW_PREF_SENTINEL\n")
    set_provider("replace", FIXTURE_ID)
    scope = ProviderScope("default", None, "persistent", "")
    provider.fixture.blocking_put(scope, NewItem(kind="fact", text="PROVIDER_FACT"), "f")
    provider.fixture.blocking_put(scope, NewItem(kind="rule", text="PROVIDER_RULE"), "r")

    parts = _provider_preview(builder)
    started = builder.build_session_context()
    assert "PROVIDER_FACT" in _family(parts, "memory")
    assert "PROVIDER_RULE" in _family(parts, "lessons")
    # Crew's own memory is not read while a provider replaces it, here or in the build.
    assert "CREW_PREF_SENTINEL" not in "".join(part["text"] for part in parts)
    assert "CREW_PREF_SENTINEL" not in started
    _assert_parts_in(parts, started)


def _without_documents(provider, builder):
    """The fixture provider without ``documents: readwrite``, beside Crew's global V1 store."""
    from unittest.mock import patch

    from kiro_crew.context import ContextBuilder

    caps = provider.fixture.capabilities()
    no_docs = type(caps)(**{**caps.__dict__, "documents": "none"})
    return (
        patch.object(provider.fixture, "capabilities", lambda: no_docs),
        patch.object(ContextBuilder, "get_memory_for", staticmethod(lambda **_k: builder.memory)),
    )


@pytest.mark.parametrize("with_rules", [True, False], ids=["rules", "findings_only"])
def test_a_replacing_provider_keeps_lessons_and_crew_documents_apart(
    env, monkeypatch, on_teardown, provider, with_rules
):
    from memory_provider_helpers import FIXTURE_ID, set_provider

    from kiro_crew.memory_provider.contract import NewItem, ProviderScope

    builder = _session_start_builder(env, monkeypatch, on_teardown)
    builder.memory.write_preferences("# Preferences\nCREW_DOC_SENTINEL\n")
    set_provider("replace", FIXTURE_ID)
    scope = ProviderScope("default", None, "persistent", "")
    provider.fixture.blocking_put(scope, NewItem(kind="fact", text="PROVIDER_FACT"), "f")
    if with_rules:
        provider.fixture.blocking_put(scope, NewItem(kind="rule", text="PROVIDER_RULE"), "r")
    provider.fixture.blocking_put(scope, NewItem(kind="finding", text="PROVIDER_FINDING"), "n")

    caps_patch, memory_patch = _without_documents(provider, builder)
    with caps_patch, memory_patch:
        parts = _provider_preview(builder)
        started = builder.build_session_context()
    lessons = _family(parts, "lessons")
    memory_text = _family(parts, "memory")
    # The findings are lessons with or without rules; the Crew documents that follow
    # the provider's lessons are memory.
    assert "PROVIDER_FINDING" in lessons
    assert ("PROVIDER_RULE" in lessons) is with_rules
    assert "PROVIDER_FACT" in memory_text and "CREW_DOC_SENTINEL" in memory_text
    assert "PROVIDER_FACT" not in lessons and "CREW_DOC_SENTINEL" not in lessons
    assert _order(parts) == ["memory", "lessons", "memory"]
    _assert_parts_in(parts, started)


def test_an_augmenting_provider_adds_its_block_to_the_preview(
    env, monkeypatch, on_teardown, provider
):
    from memory_provider_helpers import READONLY_ID, set_provider

    from kiro_crew.memory_provider.contract import NewItem, ProviderScope

    builder = _session_start_builder(env, monkeypatch, on_teardown)
    builder.memory.write_preferences("# Preferences\nCREW_PREF_SENTINEL\n")
    set_provider("augment", READONLY_ID)
    scope = ProviderScope("default", None, "persistent", "")
    provider.fixture.blocking_put(scope, NewItem(kind="fact", text="AUGMENT_FACT"), "a")

    parts = _provider_preview(builder)
    started = builder.build_session_context()
    assert "CREW_PREF_SENTINEL" in _family(parts, "memory")
    assert "AUGMENT_FACT" in _family(parts, "provider") and "AUGMENT_FACT" in started
    assert "AUGMENT_FACT" not in _family(parts, "memory")
    _assert_parts_in(parts, started)


def test_a_private_member_previews_an_augmenting_providers_facts(
    env, monkeypatch, on_teardown, provider
):
    from memory_provider_helpers import READONLY_ID, set_provider

    from kiro_crew.memory_provider.contract import NewItem, ProviderScope

    builder = _session_start_builder(env, monkeypatch, on_teardown)
    set_provider("augment", READONLY_ID)
    scope = ProviderScope("member-alice", None, "persistent", "")
    provider.fixture.blocking_put(scope, NewItem(kind="fact", text="MEMBER_AUGMENT_FACT"), "m")

    parts = _provider_preview(builder, "member-alice", private=True)
    started = builder.build_session_context(
        member="alice", memory_store="member-alice", _v2_essentials=""
    )
    # The member's [Memory tools] pointer has no end marker; the provider's facts
    # after it are their own part, not hidden inside the pointer.
    assert "[Memory tools]" in _family(parts, "memory")
    assert "MEMBER_AUGMENT_FACT" in _family(parts, "provider")
    assert "MEMBER_AUGMENT_FACT" in started
    _assert_parts_in(parts, started)
