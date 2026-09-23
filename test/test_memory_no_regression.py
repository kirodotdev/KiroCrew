"""Synthetic base-visible facts must survive startup or the real recall render."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from member_memory_helpers import document_store
from member_memory_helpers import env as _env
from member_memory_helpers import request
from test_default_context_admission import rig as _rig
from test_default_context_admission import seed_skill

from kiro_crew import context as ctx
from kiro_crew import mcp_core, member_memory_auth, memory_stores
from kiro_crew.config import loader
from kiro_crew.dashboard.handlers import discover, memory_member
from kiro_crew.hooks import HookManager
from kiro_crew.learn import LessonStore
from kiro_crew.mcp_tools import learn
from kiro_crew.mcp_tools import skills as skill_tools
from kiro_crew.memory import MemoryStore
from kiro_crew.memory_recall import bound_recall_payload
from kiro_crew.skills import SkillsLoader
from kiro_crew.vector_memory import VectorMemoryStore

env = _env
rig = _rig
pytestmark = pytest.mark.xdist_group("memory_no_regression")


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["global", "workspace", "named"])
@pytest.mark.parametrize("vectors", [False, True])
@pytest.mark.parametrize("mode", ["persistent", "incognito", "temporary"])
async def test_v1_base_visible_matrix(env, monkeypatch, binding, vectors, mode):
    name = "legacy-team" if binding == "named" else ""
    if name:
        cfg = loader.KiroCrewConfig.load()
        cfg.memory_stores[name] = loader.MemoryStoreConfig(memory_version=1)
        cfg.save()
        directory = env.home / "memory_stores" / name
        directory.mkdir()
        tier = VectorMemoryStore(db_path=directory / "memory.db")
        tier.init()
        env.tiers[name] = tier
        monkeypatch.setattr(memory_stores, "_DECLARED_MEMO", None)
    tier = env.tiers[name]
    if vectors:
        tier.embed_fn = lambda text: [0.1] * 8
    store = await document_store(env, name)
    if binding == "workspace":
        store = MemoryStore(workspace=env.home / "other-workspace")
        store.init()
        store.vector_store = tier
    store.vector_store = tier
    store.write_preferences("# Preferences\nUse short answers. PREF_SENTINEL\n")
    store.write_projects(
        "# 支付迁移 Payment migration\n- Paymentquartz: schema verified PROJECT_SENTINEL\n- 支付迁移已完成，下一步回归测试。中文项目证据\n"
    )
    store.append_history("# Dailyquartz\nYesterday task finished HISTORY_SENTINEL")
    from datetime import datetime, timedelta

    archived_day = (datetime.now() - timedelta(days=4)).date().isoformat()
    archived = store._history_dir / f"{archived_day}.md"
    archived.write_text(f"# {archived_day}\n#### Archivequartz\nARCHIVE_SENTINEL", encoding="utf-8")
    store.rebuild_index()
    tier.set_semantic(
        "project.ledgerquartz",
        "Ledgerquartz ledger fixed FACT_SENTINEL " + "detail " * 250,
        1.0,
        "user_explicit",
    )
    tier.set_semantic("pref.short", "STRUCTURED_PREF_SENTINEL", 1.0, "user_explicit")
    assert tier.write_episodic(
        "Episodequartz deployment completed EPISODE_SENTINEL", importance=1.0
    )
    stored_vector = tier.db.execute(
        "SELECT embedding FROM semantic_memory WHERE key=?", ("project.ledgerquartz",)
    ).fetchone()[0]
    assert (stored_vector is not None) is vectors
    # This is the legacy first-turn memory projection, using the same records.
    base = store.get_context(include_activity=True)
    base_episode = store.get_context(query="Episodequartz", include_activity=True)
    for marker in (
        "PREF_SENTINEL",
        "PROJECT_SENTINEL",
        "HISTORY_SENTINEL",
        "STRUCTURED_PREF_SENTINEL",
    ):
        assert marker in base
    assert "ARCHIVE_SENTINEL" in base
    assert "ARCHIVE_SENTINEL" not in store.activity_index()
    base_fact = store.get_context(query="Ledgerquartz", include_activity=True)
    assert "FACT_SENTINEL" in base_fact
    assert "EPISODE_SENTINEL" in base_episode
    skills = SkillsLoader(skills_path=env.home / "empty-skills", install_builtins=False)
    builder = ctx.ContextBuilder(
        memory=store,
        skills=skills,
        lessons=LessonStore(base_dir=env.home / "lessons"),
        hooks=HookManager(),
    )
    monkeypatch.setattr(ctx, "agent_skill_globs", lambda agent: [])
    monkeypatch.setattr(ctx, "kiro_agents_dir", lambda: env.home / "empty-agents")
    monkeypatch.setattr(builder, "get_memory_for", lambda workspace=None, memory_store=None: store)
    session = "dashboard:matrix"
    if name:
        # Internal recall resolves a named store from the session's recorded
        # execution, never from transcript metadata alone.
        env.bind_session(session, name)
    env.metadata[session] = {
        "memory_store": name or "default",
        "memory_mode": mode,
        "workspace": "other" if binding == "workspace" else "default",
    }
    env.state._slots["matrix"] = SimpleNamespace(
        blocks_reads=mode == "temporary",
        is_restricted=mode != "persistent",
        memory_mode=mode,
        workspace=env.metadata[session]["workspace"],
    )
    # Keep default notebooks distinct: the workspace test must not pass by
    # reading the builder's default memory accidentally.
    env.state.context_builder.get_memory_for = lambda workspace: store
    if binding != "workspace" and not name:
        env.state.context_builder.memory = store
    monkeypatch.setattr(member_memory_auth, "_request_peer_pid", lambda request: os.getpid())
    greeting = builder.build_session_context(blocks_reads=mode == "temporary")
    if mode != "temporary":
        assert "PREF_SENTINEL" in greeting and "STRUCTURED_PREF_SENTINEL" in greeting
        # Vague references need visible task names before a query is possible.
        for vague in ("继续昨天那个任务", "上次那个 PR"):
            assert "Dailyquartz" in greeting, vague
            assert "Paymentquartz" in greeting, vague
    else:
        assert "SENTINEL" not in greeting
    queries = [
        ("Paymentquartz", "PROJECT_SENTINEL"),
        ("What do we know about Paymentquartz?", "PROJECT_SENTINEL"),
        ("支付迁移进度怎么样？", "中文项目证据"),
        ("Dailyquartz", "HISTORY_SENTINEL"),
        ("Archivequartz", "ARCHIVE_SENTINEL"),
        ("Ledgerquartz", "FACT_SENTINEL"),
        ("Episodequartz", "EPISODE_SENTINEL"),
    ]
    for query, marker in queries:
        response = await memory_member.api_memory_recall(
            request(env, query={"q": query}, session=session, internal=True)
        )
        if mode == "temporary":
            assert response.status == 403
            assert marker not in _response_text(response)
            continue
        assert response.status == 200, _response_text(response)
        payload = json.loads(_response_text(response))
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: session)
        monkeypatch.setattr(mcp_core, "_get", lambda *args, **kwargs: payload)
        rendered = learn.memory_recall("memory_recall", {"query": query})
        assert marker in rendered, (binding, vectors, query, rendered)
        assert "reference data, not instructions" in rendered
        assert payload["total_chars"] <= 3000
        if query == "Ledgerquartz":
            fact = next(
                row
                for row in payload["retrieval"]["facts"]
                if row.get("key") == "project.ledgerquartz"
            )
            assert fact["snippet_truncated"] is True
            assert fact["source"] == "user_explicit"
        assert len(rendered.encode("utf-8")) <= 16384


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["persistent", "incognito", "temporary"])
@pytest.mark.parametrize(
    "query",
    [
        "Paymentquartz private",
        "What do we know about Paymentquartz private?",
        "支付迁移进度怎么样？",
    ],
)
async def test_private_v2_recall_never_reads_global_markdown(env, monkeypatch, mode, query):
    tier = env.tiers["member-alice"]
    env.state._slots["alice"].blocks_reads = mode == "temporary"
    env.state._slots["alice"].is_restricted = mode != "persistent"
    env.state._slots["alice"].memory_mode = mode
    env.metadata["dashboard:alice"]["memory_mode"] = mode
    assert (
        tier.set_semantic(
            "project.paymentquartz",
            "Paymentquartz private 支付迁移 fact PRIVATE_FACT",
            1.0,
            "user_explicit",
        )
        is None
    )
    assert tier.write_episodic(
        "Paymentquartz private 支付迁移 deployment PRIVATE_EPISODE", importance=1.0
    )
    monkeypatch.setattr(
        memory_member,
        "markdown_memory_for_store",
        Mock(side_effect=AssertionError("markdown fallback")),
    )
    response = await memory_member.api_memory_recall(
        request(env, query={"q": query}, internal=True)
    )
    if mode == "temporary":
        assert response.status == 403
        assert "PRIVATE_FACT" not in _response_text(response)
        return
    assert response.status == 200
    payload = json.loads(_response_text(response))
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "dashboard:alice")
    monkeypatch.setattr(mcp_core, "_get", lambda *args, **kwargs: payload)
    text = learn.memory_recall("memory_recall", {"query": query})
    assert "PRIVATE_FACT" in text and "PRIVATE_EPISODE" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["empty", "query"])
async def test_index_fault_is_not_no_memory(env, monkeypatch, broken):
    store = await document_store(env, "")
    store.write_projects("# Paymentquartz\nEvidence exists")
    env.state._slots["global"] = SimpleNamespace(blocks_reads=False)
    env.metadata["dashboard:global"] = {"memory_store": "default"}
    monkeypatch.setattr(member_memory_auth, "_request_peer_pid", lambda request: os.getpid())
    if broken == "empty":
        monkeypatch.setattr(store, "index_row_count", lambda: 0)
    else:
        monkeypatch.setattr(store, "search", Mock(side_effect=OSError("offline")))
    response = await memory_member.api_memory_recall(
        request(env, query={"q": "Paymentquartz"}, session="dashboard:global", internal=True)
    )
    payload = json.loads(_response_text(response))
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "dashboard:global")
    monkeypatch.setattr(mcp_core, "_get", lambda *args, **kwargs: payload)
    assert "index_unavailable" in learn.memory_recall("memory_recall", {"query": "Paymentquartz"})


@pytest.mark.asyncio
async def test_empty_notebook_index_is_rebuilt_from_files_before_recall_answers(env, monkeypatch):
    store = await document_store(env, "")
    store.write_projects("# Paymentquartz\nEvidence exists on disk")
    env.state._slots["global"] = SimpleNamespace(blocks_reads=False)
    env.metadata["dashboard:global"] = {"memory_store": "default"}
    monkeypatch.setattr(member_memory_auth, "_request_peer_pid", lambda request: os.getpid())
    # A real empty index: the rows are gone, the files it mirrors are not.
    conn = store._get_db()
    try:
        conn.execute("DELETE FROM memory_fts")
        conn.commit()
    finally:
        conn.close()
    assert store.index_row_count() == 0

    response = await memory_member.api_memory_recall(
        request(env, query={"q": "Paymentquartz"}, session="dashboard:global", internal=True)
    )
    payload = json.loads(_response_text(response))

    assert payload["markdown_status"] == "ready"
    assert payload["markdown_status_repair"] == "rebuilt"
    assert store.index_row_count() > 0
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "dashboard:global")
    monkeypatch.setattr(mcp_core, "_get", lambda *args, **kwargs: payload)
    text = learn.memory_recall("memory_recall", {"query": "Paymentquartz"})
    assert "Evidence exists on disk" in text
    assert "index_unavailable" not in text


def test_noise_never_displaces_relevant_episode():
    payload = {
        "lessons_context": "rule " * 180,
        "retrieval": {
            "facts": [
                {"id": f"markdown:{i}", "snippet": "noise " * 43, "recall_relevance": 0.5}
                for i in range(5)
            ],
            "episodes": [
                {"id": "correct", "text": "EPISODE_SENTINEL " * 16, "recall_relevance": 1.0}
            ],
        },
    }
    result = bound_recall_payload(payload, context_cap=1000)
    assert result["retrieval"]["episodes"][0]["id"] == "correct"
    assert len(result["retrieval"]["facts"]) < 5


def test_overflow_retains_navigation_and_latest_user(rig, tmp_path):
    from kiro_crew.history import ConversationLog

    builder, memory, skills, lessons, _ = rig
    memory.write_preferences("Required preference.\n" * 2000)
    memory.write_projects("# Paymentquartz\nTask detail")
    from kiro_crew.learn import Lesson

    lessons.save(
        Lesson(ts="2026-09-19", rule="Always retain safe behavior. " * 1500, category="knowledge")
    )
    log = ConversationLog(base_dir=tmp_path / "log")
    log.append("dashboard:synthetic", "user", "PREVIOUS_USER_REQUEST")
    log.append("dashboard:synthetic", "assistant", "long answer " * 3000)
    builder.conversation_log = log
    seed_skill(skills._dir, "discoverable")
    text = builder.build_session_context(session_key="dashboard:synthetic", model_window=200_000)
    for expected in (
        "PREVIOUS_USER_REQUEST",
        "memory_recall",
        "Paymentquartz",
        "skill_search",
        "Context budget: omitted",
    ):
        assert expected in text


def test_disabled_metrics_skip_measurement(rig, monkeypatch):
    import logging

    from kiro_crew.metrics.recorder import MetricsRecorder

    builder, _, _, _, _ = rig
    monkeypatch.setattr(ctx, "get_recorder", lambda: MetricsRecorder(None))
    monkeypatch.setattr(ctx.logger, "level", logging.INFO)
    measure = Mock(side_effect=AssertionError("measurement"))
    monkeypatch.setattr("kiro_crew.context_blocks.measure_prompt", measure)
    builder.build_message("hello", False)
    measure.assert_not_called()


@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize("reinject", [False, True])
def test_project_body_and_discovery_survive(rig, tmp_path, pinned, reinject):
    from kiro_crew import skill_trust

    builder, _, skills, _, _ = rig
    project = tmp_path / "project"
    body = "Project instructions.\n" * 350
    path = seed_skill(project / ".kiro" / "skills", "payment", always=pinned, body=body)
    seed_skill(skills._dir, "searchable")
    if not skill_trust.project_skill_traversal_supported():
        text, _ = builder.build_message(
            "current", not reinject, project=str(project), needs_reinjection=reinject
        )
        assert body not in text
        assert str(path) not in text
        assert skills.catalog_project_skills(project) == []
        matches = skills.search_skills("payment这个PR", project_dir=project)
        assert all(not m.get("confine_root") and m["name"] != "payment" for m in matches)
        assert skills.load_skill("payment", project) is None
        return
    skill_trust.grant_project_trust(project)
    text, _ = builder.build_message(
        "current", not reinject, project=str(project), needs_reinjection=reinject
    )
    assert (body in text) is pinned
    assert "skill_search" in text
    assert str(path) not in text
    assert skills.search_skills("payment这个PR", project_dir=project)
    assert body in (skills.read_scoped_skill("payment", project_dir=project) or "")


def test_skill_search_reads_project_bodies_only_under_the_body_cap(rig, tmp_path, monkeypatch):
    from kiro_crew import skill_trust
    from kiro_crew import skills as skills_mod

    builder, _, skills, _, _ = rig
    project = tmp_path / "project"
    oversized_marker = "OVERSIZED_CONFINED_BODY"
    # Metadata never mentions the query term, so only the body grep can match.
    seed_skill(project / ".kiro" / "skills", "small", body="Talks about quartzmill.")
    huge_path = seed_skill(
        project / ".kiro" / "skills",
        "huge",
        body=oversized_marker + "\n" + "quartzmill " * (skills_mod.PROJECT_SKILL_BODY_CAP // 4),
    )
    seed_skill(
        skills._dir, "big-global", body="quartzmill " * (skills_mod.PROJECT_SKILL_BODY_CAP // 4)
    )
    if not skill_trust.project_skill_traversal_supported():
        names = {m["name"] for m in skills.search_skills("quartzmill", project_dir=project)}
        assert names == {"big-global"}
        assert skills.catalog_project_skills(project) == []
        assert skills.load_skill("small", project) is None
        assert skills.load_skill("huge", project) is None
        return
    skill_trust.grant_project_trust(project)

    assert "huge" not in {row["key"] for row in skills.catalog_project_skills(project)}
    listed = {row["key"]: row for row in skills.list_skills(project)}
    assert listed["huge"]["size_bytes"] > skills_mod.PROJECT_SKILL_BODY_CAP
    assert str(huge_path) not in skills._fm_cache
    assert skills.load_skill("huge", project) is None

    reads: list[tuple[str, int | None]] = []
    real_load = skills.load_skill

    def spy(key, project_dir=None, *args, **kwargs):
        reads.append((key, kwargs.get("max_bytes")))
        return real_load(key, project_dir, *args, **kwargs)

    monkeypatch.setattr(skills, "load_skill", spy)
    names = {m["name"] for m in skills.search_skills("quartzmill", project_dir=project)}
    context, _ = builder.build_message("quartzmill", True, project=str(project))

    assert "small" in names and "big-global" in names
    assert "huge" not in names
    assert oversized_marker not in context
    assert str(huge_path) not in skills._fm_cache
    assert all(key != "huge" for key, _ in reads)
    assert ("small", skills_mod.PROJECT_SKILL_BODY_CAP) in reads
    # An unconfined body is served by the term index, so the search does not read
    # it through the loader at all; "big-global" in `names` above is what proves
    # it is still matched. The project body cap stays a confined-only rule.
    assert all(key != "big-global" for key, _ in reads)


@pytest.mark.asyncio
async def test_project_search_agent_route_loads_confined_body(rig, tmp_path, monkeypatch):
    from member_memory_helpers import make_request

    from kiro_crew import skill_trust

    builder, _, skills, _, _ = rig
    project = tmp_path / "project"
    path = seed_skill(project / ".kiro" / "skills", "payment", body="CONFINED_BODY")
    state = SimpleNamespace(
        context_builder=builder, _slots={"synthetic": SimpleNamespace(project=str(project))}
    )
    from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS

    assert "/api/skills/-/discover" in _MIXED_INTERNAL_API_PATHS
    monkeypatch.setattr(
        discover, "_get_registry", Mock(side_effect=AssertionError("external search"))
    )
    if not skill_trust.project_skill_traversal_supported():
        response = await discover.api_skills_discover(
            make_request(
                state,
                "/api/skills/-/discover",
                query={"scope": "installed", "q": "payment这个PR"},
                session="dashboard:synthetic",
                internal=True,
            )
        )
        assert response.status == 200
        assert json.loads(_response_text(response))["matches"] == []
        assert skills.catalog_project_skills(project) == []
        assert skills.search_skills("payment这个PR", project_dir=project) == []
        assert skills.load_skill("payment", project) is None
        return
    skill_trust.grant_project_trust(project)
    response = await discover.api_skills_discover(
        make_request(
            state,
            "/api/skills/-/discover",
            query={"scope": "installed", "q": "payment这个PR"},
            session="dashboard:synthetic",
            internal=True,
        )
    )
    assert response.status == 200
    payload = json.loads(_response_text(response))
    assert payload["matches"] and "path" not in payload["matches"][0]
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:synthetic")
    # The confined-body route is reached only through a SIGNED identity.
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "dashboard:synthetic")
    monkeypatch.setattr(mcp_core, "_get", lambda *args, **kwargs: payload)
    output = skill_tools.skill_search("skill_search", {"query": "payment这个PR"})
    assert "CONFINED_BODY" in output and str(path) not in output


def test_question_words_do_not_admit_unrelated_notebooks(tmp_path):
    store = MemoryStore(workspace=tmp_path)
    store.write_projects("Paymentquartz migration verified")
    store.write_preferences("What do we know about lunch and how should we eat?")
    rows = store.search("What do we know about Paymentquartz?", match_any=True)
    assert len(rows) == 1 and "migration verified" in rows[0]["snippet"]
    assert store.search('" OR NOT NEAR *', match_any=True) == []


def test_activity_index_keeps_recent_names_when_projects_overflow(rig):
    _, memory, _, _, _ = rig
    memory.write_projects("# Projects\n" + "- Old project entry\n" * 1000)
    memory.append_history("# Recenttaskquartz\nRecent work")
    index = memory.activity_index()
    assert len(index) <= 1800
    assert "Recenttaskquartz" in index
    assert index.endswith("[End of memory activity index]\n\n")


@pytest.mark.asyncio
async def test_ten_thousand_character_slack_thread_does_not_call_model(tmp_path, monkeypatch):
    from kiro_crew import llm_helpers
    from kiro_crew.history import ConversationLog

    log = ConversationLog(base_dir=tmp_path / "logs")
    log.append("slack:thread", "user", "Original task " + "x" * 5000)
    log.append("slack:thread", "assistant", "Result " + "y" * 5000)
    model = Mock(side_effect=AssertionError("unnecessary compression"))
    monkeypatch.setattr(llm_helpers, "background_turn", model)
    result = await ctx.compress_thread_history(
        log, "slack:thread", "continue", Mock(), model_window=1_000_000
    )
    assert result is not None
    assert "Original task" in result and "Result" in result
    model.assert_not_called()


def test_session_only_source_snippets_are_retained(rig):
    builder, _, _, _, _ = rig
    builder.conversation_log = SimpleNamespace(
        recent=lambda *args, **kwargs: [],
        recent_with_provenance=lambda *args, **kwargs: [
            {
                "source_thread": "prior-thread",
                "ts": "2026-09-18T12:00:00",
                "snippet": "SOURCE_ONLY_SENTINEL",
            }
        ],
    )
    text = builder.build_session_context(session_key="dashboard:synthetic", resumed=True)
    assert "SOURCE_ONLY_SENTINEL" in text and "prior-thread" in text
    hidden = builder.build_session_context(
        session_key="dashboard:synthetic", resumed=True, context_groups=frozenset()
    )
    assert "SOURCE_ONLY_SENTINEL" not in hidden


def test_cancel_restore_span_counts_only_the_real_request(rig):
    from kiro_crew.context_blocks import measure_prompt

    builder, _, _, _, _ = rig
    prefix = "[PREVIOUS TURN WAS CANCELLED BY THE USER — context restore]\nOld task\n[END PREVIOUS TURN]\n\n"
    user = "继续支付迁移"
    span: list[int] = []
    text, _ = builder.build_message(
        prefix + user,
        False,
        session_key="slack:synthetic",
        user_text_range=(len(prefix), len(prefix) + len(user)),
        user_span_out=span,
    )
    assert text[span[0] : span[1]] == user
    reading = measure_prompt(text, user_span=(span[0], span[1]), lifecycle="warm")
    assert reading["blocks"]["your_message"]["chars"] == len(user)
    assert reading["blocks"]["cancelled_turn"]["chars"] > 0


def test_skill_search_filters_scope_and_identical_copies(rig, tmp_path):
    _, _, skills, _, _ = rig
    first = seed_skill(skills._dir, "one/payment", body="Payment helper")
    second = seed_skill(skills._dir, "two/payment", body="Payment helper")
    second.write_bytes(first.read_bytes())
    scoped = seed_skill(skills._dir, "foreign-payment", body="Foreign helper")
    scoped.write_text(
        scoped.read_text(encoding="utf-8").replace(
            "description:", "repo_scope: other-repo\ndescription:"
        ),
        encoding="utf-8",
    )
    matches = skills.search_skills("payment这个PR", project_dir=tmp_path / "this-repo")
    assert len(matches) == 1
    assert matches[0]["name"] == "one/payment"


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_vector_failure_falls_back_only_for_authorized_v1(env, monkeypatch, private):
    from unittest.mock import AsyncMock

    store = await document_store(env, "" if not private else "member-alice")
    store.write_projects("# Paymentquartz\nNOTEBOOK_WITHOUT_VECTOR")
    key = "dashboard:alice" if private else "dashboard:global"
    if not private:
        env.state._slots["global"] = SimpleNamespace(blocks_reads=False)
        env.metadata[key] = {"memory_store": "default"}
        monkeypatch.setattr(member_memory_auth, "_request_peer_pid", lambda request: os.getpid())
    monkeypatch.setattr(
        memory_member,
        "vector_memory_for_store",
        AsyncMock(side_effect=OSError("vector unavailable")),
    )
    response = await memory_member.api_memory_recall(
        request(
            env,
            query={"q": "Paymentquartz", **({"store": "member-alice"} if private else {})},
            session=key,
            internal=not private,
            owner=private,
        )
    )
    if private:
        assert response.status == 503
        assert "NOTEBOOK_WITHOUT_VECTOR" not in _response_text(response)
    else:
        assert response.status == 200
        payload = json.loads(_response_text(response))
        assert payload["vector_status"] == "unavailable"
        assert "NOTEBOOK_WITHOUT_VECTOR" in payload["semantic_context"]


def _response_text(response) -> str:
    text = response.text
    assert isinstance(text, str)
    return text


def test_activity_index_reserves_each_recent_day(rig):
    from datetime import datetime, timedelta

    _, memory, _, _, _ = rig
    memory.write_projects("# Projects\n" + "- Project title\n" * 1000)
    memory.append_history("# Today task\nToday detail")
    for age in range(3):
        date = (datetime.now() - timedelta(days=age)).date().isoformat()
        path = memory._history_dir / f"{date}.md"
        path.write_text(
            f"# {date}\n"
            + "#### Older task\nOld detail\n" * 100
            + f"#### Latesttask{age}\nLatest detail",
            encoding="utf-8",
        )
    index = memory.activity_index()
    assert len(index) <= 1800
    for age in range(3):
        assert f"Latesttask{age}" in index
        assert (datetime.now() - timedelta(days=age)).date().isoformat() in index
