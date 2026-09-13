"""Outbound prompt replay, owner persona and store lifecycle contracts."""

import asyncio
import json
import threading
from unittest.mock import AsyncMock, Mock

import pytest
from test_chat_runner_coverage import _complete, _drive, _runner_state, _set_stream, _slot
from test_member_essential_context import env as _essential_env

from kiro_crew import context as ctx
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.context import ContextBuilder
from kiro_crew.history import ConversationLog
from kiro_crew.learn import LessonStore
from kiro_crew.member_essential_context import documents_for_member
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

env = _essential_env


def builder(tmp_path, log=None):
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "memory"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path / "lessons"),
        conversation_log=log,
    )


def test_explicit_replay_never_also_reads_fallback(tmp_path):
    log = ConversationLog(base_dir=tmp_path / "history")
    log.append("audit", "user", "OLD_USER")
    log.append("audit", "assistant", "OLD_REPLY")
    replay = ctx.build_session_replay(log, "audit")
    prompt, _ = builder(tmp_path, log).build_message(
        "AUDIT_CURRENT_REQUEST_9C", True, "audit", compressed_history=replay
    )
    assert prompt.count("OLD_USER") == 1
    assert prompt.count("OLD_REPLY") == 1


@pytest.mark.parametrize("minimal", [False, True])
@pytest.mark.parametrize("override", [False, True])
def test_owner_persona_once_with_global_and_project_templates(env, minimal, override):
    agents = kiro_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "writer-template.json").write_text(
        json.dumps({"name": "writer-template", "prompt": "GLOBAL_PERSONA"}), encoding="utf-8"
    )
    if not override:
        (env.project / ".kiro/agents/writer-template.json").unlink()
    prompt, _ = env.builder.build_message(
        "AUDIT_CURRENT_REQUEST_9C",
        True,
        agent="writer-template",
        memory_store=env.store,
        project=str(env.project),
        minimal_context=minimal,
    )
    expected = "Bound Soul: preserve the user's voice." if override else "GLOBAL_PERSONA"
    assert prompt.count(expected) == 1
    if override:
        assert "GLOBAL_PERSONA" not in prompt


def test_distinct_execution_template_still_has_its_instructions(env):
    agents = kiro_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "executor.json").write_text(
        json.dumps({"name": "executor", "prompt": "EXECUTION_TASK"}), encoding="utf-8"
    )
    prompt, _ = env.builder.build_message(
        "AUDIT_CURRENT_REQUEST_9C",
        True,
        agent="executor",
        memory_store=env.store,
        project=str(env.project),
    )
    assert prompt.count("EXECUTION_TASK") == 1
    assert prompt.count("Bound Soul: preserve the user's voice.") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("disk_current,extra_reply", [(True, False), (False, False), (False, True)])
async def test_runner_merges_disk_and_unflushed_tail_once(tmp_path, disk_current, extra_reply):
    state, client = _runner_state(tmp_path)
    client.mcp_session_report = Mock(return_value=None)
    client.client = Mock(pop_pending_oauth_requests=Mock(return_value=[]))
    state.sessions._sessions = {}
    state.sessions.consume_replay_suppression = Mock(return_value=False)
    state.sessions.consume_needs_reinjection = Mock(return_value=False)
    slot = _slot()
    state._slots[slot.key] = slot
    key = f"dashboard:{slot.key}"
    for role, content, mid in [("user", "OLD_USER", "u1"), ("assistant", "OLD_REPLY", "a1")]:
        slot.append(role, content, meta={"mid": mid})
        await asyncio.to_thread(state.conversation_log.append, key, role, content, mid=mid)
    if extra_reply:
        slot.append("assistant", "UNFLUSHED_REPLY", meta={"mid": "a2"})
    slot.append("user", "AUDIT_CURRENT_REQUEST_9C", meta={"mid": "u2"})
    if disk_current:
        await asyncio.to_thread(
            state.conversation_log.append, key, "user", "AUDIT_CURRENT_REQUEST_9C", mid="u2"
        )
    state.context_builder = builder(tmp_path, state.conversation_log)
    _set_stream(client, [_complete()])
    await _drive(state, slot, "AUDIT_CURRENT_REQUEST_9C")
    prompt = client.stream.call_args_list[0].args[0]
    assert prompt.count("OLD_USER") == 1
    assert prompt.count("OLD_REPLY") == 1
    assert prompt.count("AUDIT_CURRENT_REQUEST_9C") == 1
    assert prompt.count("UNFLUSHED_REPLY") == int(extra_reply)


@pytest.mark.asyncio
async def test_cancelled_init_closes_after_worker_finishes(tmp_path, monkeypatch):
    from kiro_crew import embeddings, memory_stores, vector_memory

    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    store = Mock()

    def init():
        started.set()
        assert release.wait(5)

    def close():
        assert release.is_set()
        closed.set()

    store.init.side_effect = init
    store.close.side_effect = close
    monkeypatch.setattr(vector_memory, "VectorMemoryStore", Mock(return_value=store))
    monkeypatch.setattr(memory_stores, "resolve_store_path", lambda name: tmp_path / "memory.db")
    monkeypatch.setattr(memory_stores, "require_memory_store", lambda name: None)
    monkeypatch.setattr(embeddings, "model_file_present", lambda: False)
    monkeypatch.setattr(embeddings, "reconcile_store_embedding_space", lambda store: None)
    monkeypatch.setattr(ctx, "_vector_stores", {})
    monkeypatch.setattr(ctx, "_memory_stores", {})
    task = asyncio.create_task(ctx._build_store_vectors("audit"))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert not closed.is_set()
    finally:
        release.set()
        await asyncio.get_running_loop().shutdown_default_executor()
    assert closed.is_set()
    assert "audit" not in ctx._vector_stores


def test_same_text_different_ids_and_newest_budget(tmp_path):
    log = ConversationLog(base_dir=tmp_path / "history")
    log.append("audit", "user", "REPEATED_TEXT", mid="one")
    pending = [
        {"role": "user", "content": "REPEATED_TEXT", "meta": {"mid": "two"}},
        {"role": "user", "content": "REQUEST", "meta": {"mid": "three"}},
    ]
    replay = ctx.build_session_replay(
        log, "audit", pending_messages=pending, current_message=pending[-1]
    )
    assert replay.count("REPEATED_TEXT") == 2
    assert "REQUEST" not in replay
    pending = [
        {"role": "assistant", "content": f"OLD_{i}" + "x" * 2000, "meta": {"mid": str(i)}}
        for i in range(50)
    ] + [{"role": "assistant", "content": "NEWEST_REPLY", "meta": {"mid": "new"}}]
    replay = ctx.build_session_replay(None, "audit", pending_messages=pending, model_window=32_000)
    assert "NEWEST_REPLY" in replay
    assert "OLD_0" not in replay


@pytest.mark.parametrize("field", ["mid", "sendId"])
def test_current_identity_excludes_only_this_delivery(field):
    current = {"role": "user", "content": "same", "meta": {field: "current"}}
    disk = [
        {"role": "user", "content": "same", "meta": {field: "prior"}},
        dict(current),
    ]
    assert ctx._merge_replay_rows(disk, [current], current) == disk[:1]


@pytest.mark.asyncio
async def test_actual_native_resume_does_not_replay_slot_tail(tmp_path):
    from kiro_crew.providers.acp import AcpProvider

    state, _ = _runner_state(tmp_path)
    client = AcpProvider(work_dir=tmp_path)
    client.client._resumed = True
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    state.sessions._sessions = {}
    slot = _slot()
    state._slots[slot.key] = slot
    slot.append("user", "NATIVE_OLD_USER", meta={"mid": "old"})
    slot.append("user", "NATIVE_CURRENT", meta={"mid": "new"})
    state.context_builder = builder(tmp_path, state.conversation_log)
    _set_stream(client, [_complete()])
    await _drive(state, slot, "NATIVE_CURRENT")
    prompt = client.stream.call_args_list[0].args[0]
    assert "NATIVE_OLD_USER" not in prompt
    assert prompt.count("NATIVE_CURRENT") == 1
    assert "SESSION RESUMED" in prompt
    state.sessions.consume_replay_suppression.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["normal", "init_failure", "generation_changed", "race_loser"])
async def test_store_worker_ownership_on_every_exit(tmp_path, monkeypatch, outcome):
    from kiro_crew import embeddings, memory_stores, vector_memory

    store = Mock()
    winner = Mock()
    monkeypatch.setattr(ctx, "_vector_stores", {})
    monkeypatch.setattr(ctx, "_memory_stores", {})
    monkeypatch.setattr(ctx, "_store_cache_generation", 100)

    def init():
        if outcome == "init_failure":
            raise RuntimeError("INIT_FAILED")
        if outcome == "generation_changed":
            with ctx._stores_lock:
                ctx._store_cache_generation += 1
        if outcome == "race_loser":
            with ctx._stores_lock:
                ctx._vector_stores["audit"] = winner

    store.init.side_effect = init
    monkeypatch.setattr(vector_memory, "VectorMemoryStore", Mock(return_value=store))
    monkeypatch.setattr(memory_stores, "resolve_store_path", lambda name: tmp_path / "memory.db")
    monkeypatch.setattr(memory_stores, "require_memory_store", lambda name: None)
    monkeypatch.setattr(embeddings, "model_file_present", lambda: False)
    monkeypatch.setattr(embeddings, "reconcile_store_embedding_space", lambda store: None)
    if outcome == "init_failure":
        with pytest.raises(RuntimeError, match="INIT_FAILED"):
            await ctx._build_store_vectors("audit")
    elif outcome == "generation_changed":
        with pytest.raises(memory_stores.UnknownMemoryStore, match="cache changed"):
            await ctx._build_store_vectors("audit")
    else:
        result = await ctx._build_store_vectors("audit")
        assert result is (winner if outcome == "race_loser" else store)
    assert store.close.call_count == int(outcome != "normal")
    if outcome == "normal":
        assert ctx._vector_stores["audit"] is store
        store.close()


@pytest.mark.asyncio
async def test_cancelled_real_store_releases_lock_fd(env, monkeypatch):
    import os

    from kiro_crew import embeddings, member_memory_backup, platform_compat
    from kiro_crew.vector_memory import VectorMemoryStore

    initialized = threading.Event()
    release = threading.Event()
    stores = []
    fds = []
    real_init = VectorMemoryStore.init

    def init(store):
        real_init(store)
        stores.append(store)
        fds.append(store._store_use_lock_fd)
        initialized.set()
        assert release.wait(5)

    monkeypatch.setattr(VectorMemoryStore, "init", init)
    monkeypatch.setattr(ctx, "_vector_stores", {})
    monkeypatch.setattr(embeddings, "model_file_present", lambda: False)
    task = asyncio.create_task(ContextBuilder.ensure_store(env.store))
    contender = None
    try:
        assert await asyncio.to_thread(initialized.wait, 5)
        if platform_compat.IS_POSIX:
            assert fds[0] is not None
            contender = await asyncio.to_thread(
                member_memory_backup._open_store_use_lock, stores[0]._db_path
            )
            assert os.fstat(contender).st_ino == os.fstat(fds[0]).st_ino
            assert not platform_compat.try_acquire_lock(contender, exclusive=True)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert stores[0]._store_use_lock_fd == fds[0]
        release.set()
        await asyncio.get_running_loop().shutdown_default_executor()
        assert stores[0]._store_use_lock_fd is None
        assert stores[0]._db is None
        if contender is not None:
            assert platform_compat.try_acquire_lock(contender, exclusive=True)
            platform_compat.release_lock(contender)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.get_running_loop().shutdown_default_executor()
        for store in stores:
            store.close()
        if contender is not None:
            os.close(contender)


def test_legacy_execution_uses_project_override(env):
    agents = kiro_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "writer-template.json").write_text(
        json.dumps({"name": "writer-template", "prompt": "GLOBAL_PERSONA"}), encoding="utf-8"
    )
    assert ContextBuilder._load_agent_prompt("writer-template", str(env.project)) == (
        "Bound Soul: preserve the user's voice."
    )


def test_relative_prompt_file_resolves_like_essentials(env, tmp_path, monkeypatch):
    (env.project / "persona").mkdir()
    (env.project / "persona" / "task.md").write_text("PROJECT_RELATIVE_PERSONA", encoding="utf-8")
    (env.project / ".kiro/agents/task-template.json").write_text(
        json.dumps({"name": "task-template", "prompt": "file://persona/task.md"}),
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere" / "persona"
    elsewhere.mkdir(parents=True)
    (elsewhere / "task.md").write_text("CWD_PERSONA", encoding="utf-8")
    monkeypatch.chdir(elsewhere.parent)

    assert ContextBuilder._load_agent_prompt("task-template", str(env.project)) == (
        "PROJECT_RELATIVE_PERSONA"
    )
    essentials = dict(
        documents_for_member("task-template", str(env.project), include_project=False)
    )
    assert essentials[str(env.project / "persona" / "task.md")] == "PROJECT_RELATIVE_PERSONA"


def test_product_prompt_reference_is_not_lost_for_private_fork(env):
    (env.project / ".kiro/agents/writer-template.json").write_text(
        json.dumps({"name": "writer-template", "prompt": f"file://{ctx._prompt_path()}"}),
        encoding="utf-8",
    )
    prompt, _ = env.builder.build_message(
        "REQUEST",
        True,
        agent="writer-template",
        memory_store=env.store,
        project=str(env.project),
    )
    assert prompt.startswith("[AGENT SYSTEM PROMPT]\nYou are ")
    assert prompt.count("[END AGENT SYSTEM PROMPT]\n\n") == 1
    assert prompt.count("[V2 ESSENTIAL CONTEXT") == 1


def test_legacy_rows_match_one_for_one_without_collapsing_same_source():
    row = {"role": "user", "content": "repeat", "ts": "2026-09-12T10:00:00Z"}
    assert ctx._merge_replay_rows([dict(row), dict(row)], [dict(row)], None) == [row, row]
    assert ctx._merge_replay_rows([dict(row)], [dict(row), dict(row)], None) == [row, row]


def test_explicit_empty_replay_never_reads_fallback(tmp_path):
    log = ConversationLog(base_dir=tmp_path / "history")
    log.append("audit", "user", "SUPPRESSED_OLD_TURN")
    prompt, _ = builder(tmp_path, log).build_message(
        "REQUEST", True, "audit", compressed_history=""
    )
    assert "SUPPRESSED_OLD_TURN" not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["cron", "recovery", "user_replay"])
@pytest.mark.parametrize("flushed", [False, True])
@pytest.mark.parametrize("older_same_text", [False, True])
@pytest.mark.parametrize("resumed", [False, True])
@pytest.mark.parametrize("explicit_identity", [False, True])
async def test_current_inject_is_not_historical_input(
    tmp_path, monkeypatch, kind, flushed, older_same_text, resumed, explicit_identity
):
    state, client = _runner_state(tmp_path)
    client.mcp_session_report = Mock(return_value=None)
    client.client = Mock(pop_pending_oauth_requests=Mock(return_value=[]))
    state.sessions._sessions = {}
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, resumed))
    slot = _slot()
    state._slots[slot.key] = slot
    key = f"dashboard:{slot.key}"
    text = "CURRENT_INJECT_MARKER"
    if older_same_text:
        slot.append("user", text, meta={"mid": "older"})
        await asyncio.to_thread(state.conversation_log.append, key, "user", text, mid="older")
    current = slot.append("inject", text, meta={"mid": "current", "injectKind": kind})
    if flushed:
        await asyncio.to_thread(state.conversation_log.append, key, "inject", text, mid="current")
    observed = []
    replay = ctx.build_session_replay

    def observe(*args, **kwargs):
        observed.append(kwargs.get("current_message"))
        return replay(*args, **kwargs)

    monkeypatch.setattr(ctx, "build_session_replay", observe)
    if explicit_identity:
        from functools import partial

        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr(
            chat_runner, "_run_chat", partial(chat_runner._run_chat, _current_message=current)
        )
    state.context_builder = builder(tmp_path, state.conversation_log)
    _set_stream(client, [_complete()])
    await _drive(state, slot, text)
    prompt = client.stream.call_args_list[0].args[0]
    assert prompt.count(text) == 1 + int(older_same_text and not resumed)
    if resumed:
        assert observed == []
    else:
        assert observed == [current]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["cron", "recovery", "user_replay"])
async def test_queue_passes_exact_inject_row_to_runner(tmp_path, monkeypatch, kind):
    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.chat_utils import CRON_NOTIFICATION_KIND, SYNTHETIC_RECOVERY_KIND

    state, _ = _runner_state(tmp_path)
    state.subagents = None
    slot = _slot()
    state._slots[slot.key] = slot
    text = "queued delivery"
    if kind == "cron":
        text = f"{chat_runner.CRON_NOTIFY_PREFIX}daily]: queued delivery"
    slot._queue = [
        {
            "id": "queued-delivery",
            "content": text,
            "kind": CRON_NOTIFICATION_KIND if kind == "cron" else SYNTHETIC_RECOVERY_KIND,
            "payload": "original" if kind == "user_replay" else "continuation",
        }
    ]
    run = AsyncMock()

    def capture(state, slot, coroutine):
        coroutine.close()
        return Mock()

    monkeypatch.setattr(chat_runner, "_run_chat", run)
    monkeypatch.setattr(chat_runner, "spawn_guarded_turn", capture)
    assert await chat_runner._start_next_queued_turn(state, slot)
    current = run.call_args.kwargs["_current_message"]
    assert current is slot.messages[-1]
    assert current["role"] == "inject"
    assert current["meta"]["injectKind"] == kind
    assert current["meta"]["mid"]


@pytest.mark.parametrize("reader", ["context", "essentials"])
@pytest.mark.parametrize("origin", ["project", "global"])
@pytest.mark.parametrize("case", ["escape", "symlink_escape", "inside_parent", "absolute"])
def test_prompt_file_source_boundary(env, tmp_path, monkeypatch, reader, origin, case):
    from pathlib import Path

    from conftest import make_dir_link

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KIRO_HOME", str(home / ".kiro"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", home / ".kiro" / "agents")
    root = env.project if origin == "project" else home
    (root / "persona").mkdir()
    (root / "inside.txt").write_text("INSIDE_PERSONA", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("OUTSIDE_PERSONA", encoding="utf-8")
    if case == "escape":
        uri = "file://../outside/private.txt"
    elif case == "symlink_escape":
        make_dir_link(root / "linked", outside)
        uri = "file://linked/private.txt"
    elif case == "inside_parent":
        uri = "file://persona/../inside.txt"
    else:
        uri = f"file://{root / 'inside.txt'}"
    agents = root / ".kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "boundary-template.json").write_text(
        json.dumps({"name": "boundary-template", "prompt": uri}), encoding="utf-8"
    )
    if reader == "context":
        body = ContextBuilder._load_agent_prompt("boundary-template", str(env.project))
    else:
        body = "\n".join(
            content
            for _, content in documents_for_member(
                "boundary-template", str(env.project), include_project=False
            )
        )
    assert body == ("" if case in {"escape", "symlink_escape"} else "INSIDE_PERSONA")


def test_absolute_prompt_file_keeps_existing_reader_rules(env, tmp_path):
    from kiro_crew.member_essential_context import MemberEssentialContextError

    outside = tmp_path / "absolute-persona.txt"
    outside.write_text("ABSOLUTE_PERSONA", encoding="utf-8")
    (env.project / ".kiro/agents/absolute-template.json").write_text(
        json.dumps({"name": "absolute-template", "prompt": f"file://{outside}"}),
        encoding="utf-8",
    )
    assert (
        ContextBuilder._load_agent_prompt("absolute-template", str(env.project))
        == "ABSOLUTE_PERSONA"
    )
    with pytest.raises(MemberEssentialContextError, match="outside the admitted document root"):
        documents_for_member("absolute-template", str(env.project), include_project=False)


@pytest.mark.parametrize("reader", ["context", "essentials"])
def test_relative_prompt_read_rejects_ancestor_swap(env, tmp_path, monkeypatch, reader):
    from conftest import make_dir_link
    from kiro_crew import member_essential_context as essentials

    directory = env.project / "persona"
    directory.mkdir()
    (directory / "prompt.txt").write_text("INSIDE_PERSONA", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "prompt.txt").write_text("OUTSIDE_PERSONA", encoding="utf-8")
    (env.project / ".kiro/agents/swap-template.json").write_text(
        json.dumps({"name": "swap-template", "prompt": "file://persona/prompt.txt"}),
        encoding="utf-8",
    )
    resolve = essentials.resolve_relative_prompt_path
    checked = []

    def swap_after_validation(*args):
        result = resolve(*args)
        assert result is not None
        checked.append(result)
        directory.rename(env.project / "original-persona")
        make_dir_link(directory, outside)
        return result

    monkeypatch.setattr(essentials, "resolve_relative_prompt_path", swap_after_validation)
    if reader == "context":
        assert ContextBuilder._load_agent_prompt("swap-template", str(env.project)) == ""
    else:
        with pytest.raises(essentials.MemberEssentialContextError):
            documents_for_member("swap-template", str(env.project), include_project=False)
    assert len(checked) == 1


@pytest.mark.parametrize("metadata", ["x", 17, ["x"]])
def test_persisted_scalar_metadata_replays_with_legacy_identity(tmp_path, metadata):
    log = ConversationLog(base_dir=tmp_path / "history")
    row = {
        "role": "user",
        "content": "PERSISTED_METADATA_BODY",
        "ts": "2026-09-13T00:00:00Z",
        "meta": metadata,
    }
    path = log._path("audit")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    loaded = log.read_messages_chained("audit")
    assert loaded[0]["meta"] == metadata
    replay = ctx.build_session_replay(log, "audit", pending_messages=[dict(row)])
    assert replay.count(row["content"]) == 1
    assert ctx._replay_identity(loaded[0]) == ("legacy", row["ts"], row["role"], row["content"])
    assert ctx._merge_replay_rows(loaded, [dict(row)], None) == [row]


@pytest.mark.parametrize("oversized", [False, True])
def test_relative_prompt_byte_reader_normalizes_or_refuses(env, monkeypatch, oversized):
    from kiro_crew import hooks

    (env.project / "reader.txt").write_bytes(b"first\r\nsecond\rlast")
    (env.project / ".kiro/agents/reader-template.json").write_text(
        json.dumps({"name": "reader-template", "prompt": "file://reader.txt"}),
        encoding="utf-8",
    )
    if oversized:
        monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 4)
    expected = "" if oversized else "first\nsecond\nlast"
    assert ContextBuilder._load_agent_prompt("reader-template", str(env.project)) == expected
