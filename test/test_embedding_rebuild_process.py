"""Real child-process exits and cross-process SQLite publication races."""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_embedding_rebuild_generation import rebuild_home as _rebuild_home
from test_embedding_rebuild_generation import vector_blobs

from kiro_crew import embeddings as emb
from kiro_crew.dashboard.handlers import memory

rebuild_home = _rebuild_home

_CHILD = r"""
import asyncio, json, os, sys
from pathlib import Path
from types import SimpleNamespace
from kiro_crew import embeddings as emb
from kiro_crew.dashboard.handlers import memory
from kiro_crew.vector_memory import VectorMemoryStore
home = Path(sys.argv[1])
mode = sys.argv[2]
model = home / 'model.gguf'
backend = emb.default_embedding_backend()
backend._llm = SimpleNamespace(create_embedding=lambda texts: {'data': [{'embedding': [1., 0.]} for _ in texts]}, close=lambda: None)
emb.install_shared_embedder(backend)
if mode == 'rollback':
    async def run_rollback():
        rollback, _ = await memory._write_embed_model_config(str(model), 2)
        (home / 'child-ready').write_text('ready')
        assert sys.stdin.readline().strip() == 'release'
        try:
            await rollback()
        except ValueError:
            print('refused')
        else:
            print('restored')
    asyncio.run(run_rollback())
    emb.reset_shared_embedder()
    sys.exit(0)
if mode == 'crash':
    asyncio.run(memory._write_embed_model_config(str(model), 2))
    paths = [home / 'memory.db', home / 'memory_stores/legacy/memory.db', home / 'memory_stores/member-late/memory.db']
    for path in paths[:int(sys.argv[3])]:
        store = VectorMemoryStore(db_path=path, embedding_dim=2)
        store.init()
        emb.align_store_embedding_space(store)
    os._exit(23)
name = sys.argv[3] if len(sys.argv) > 3 else 'default'
path = home / 'memory.db' if name == 'default' else home / 'memory_stores' / name / 'memory.db'
store = VectorMemoryStore(db_path=path, embedding_dim=2)
store.init()
if mode == 'lazy-lesson':
    store.write_lesson('Always preserve glacier archives')
    backend._llm.create_embedding = lambda texts: {'data': [{'embedding': [1., 0.] if text == 'Always preserve glacier archives' else [0., 1.]} for text in texts]}
if mode.startswith('backfill-'):
    if mode == 'backfill-episode':
        store.write_episodic('child process deployment record', defer_embedding=True)
    elif mode == 'backfill-fact':
        store.set_semantic('project.child', 'child process deployment fact', 1., 'user_explicit')
    else:
        store.write_lesson('Check child process deployment safely')
store.embed_fn = emb.make_sync_embed_fn()
original = store._try_embed
first = True
def paused(*args, **kwargs):
    global first
    vector = original(*args, **kwargs)
    if first and (mode != 'lazy-lesson' or args[0] == 'Always preserve glacier archives'):
        first = False
        assert vector is not None
        (home / 'child-ready').write_text('ready')
        assert sys.stdin.readline().strip() == 'release'
    return vector
store._try_embed = paused
if mode == 'episode':
    store.write_episodic('child process deployment record')
elif mode == 'fact':
    store.set_semantic('project.child', 'child process deployment fact', 1., 'user_explicit')
elif mode == 'lesson':
    store.write_lesson('Check child process deployment safely')
elif mode == 'lazy-lesson':
    store.write_lesson('Check deployment health before release')
else:
    store.backfill_missing_embeddings(pace=False)
store.close()
emb.reset_shared_embedder()
"""


def child(home, mode, *args):
    env = dict(os.environ, KIROCREW_HOME=str(home))
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(home), mode, *map(str, args)],
        cwd=home,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


@pytest.mark.parametrize("completed", [0, 1, 2])
def test_process_exit_after_config_or_partial_invalidation(rebuild_home, completed):
    home = rebuild_home
    originals = [home.global_store, *home.late]
    for store in originals:
        store.close()
    process = child(home.root, "crash", completed)
    try:
        out, err = process.communicate(timeout=15)
        assert process.returncode == 23, (out, err)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    generation = emb.embedding_rebuild_generation()
    assert generation
    for original in originals:
        reopened = home.open_store(original._db_path)
        emb.align_store_embedding_space(reopened)
        assert reopened.recorded_rebuild_generation() == generation
        assert vector_blobs(reopened) == [None, None]
        assert emb.align_store_embedding_space(reopened) == 0


@pytest.mark.parametrize(
    "kind", ["episode", "fact", "lesson", "backfill-episode", "backfill-fact", "backfill-lesson"]
)
@pytest.mark.parametrize("name", ["default", "legacy", "member-late"])
def test_cross_process_old_vector_cannot_publish_after_rebuild(rebuild_home, kind, name):
    home = rebuild_home
    target = {"default": home.global_store, "legacy": home.late[0], "member-late": home.late[1]}[
        name
    ]
    process = child(home.root, kind, name)
    try:
        deadline = time.monotonic() + 10
        while not (home.root / "child-ready").exists():
            assert process.poll() is None, process.communicate(timeout=1)
            assert time.monotonic() < deadline
            time.sleep(0.01)
        asyncio.run(memory._write_embed_model_config(str(home.model), 2))
        emb.align_store_embedding_space(target)
        out, err = process.communicate("release\n", timeout=10)
        assert process.returncode == 0, (out, err)
        if kind.endswith("episode"):
            row = target.db.execute(
                "SELECT embedding FROM episodic_memories WHERE text = ?",
                ("child process deployment record",),
            ).fetchone()
        elif kind.endswith("fact"):
            row = target.db.execute(
                "SELECT embedding FROM semantic_memory WHERE key = 'project.child'"
            ).fetchone()
        else:
            row = target.db.execute(
                "SELECT embedding FROM semantic_memory WHERE key LIKE 'lesson.%'"
            ).fetchone()
        assert row is not None and row[0] is None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize("edit", ["unrelated", "model", "request"])
def test_child_config_rollback_keeps_competing_edits(rebuild_home, edit):
    import json

    from kiro_crew.config.loader import update_config_locked

    home = rebuild_home
    process = child(home.root, "rollback")
    try:
        deadline = time.monotonic() + 10
        while not (home.root / "child-ready").exists():
            assert process.poll() is None, process.communicate(timeout=1)
            assert time.monotonic() < deadline
            time.sleep(0.01)
        generation = emb.embedding_rebuild_generation()

        def modify(data):
            data["unrelated"] = "retained"
            if edit == "model":
                data["memory"]["embed_model_id"] = "competing-model"
            if edit == "request":
                data["memory"]["embed_rebuild_generation"] = "competing-request"
            return data

        update_config_locked(home.config, mutate=modify)
        output, errors = process.communicate("release\n", timeout=10)
        assert process.returncode == 0, errors
        assert ("restored" if edit == "unrelated" else "refused") in output
        data = json.loads(home.config.read_text(encoding="utf-8"))
        assert data["unrelated"] == "retained"
        assert data["memory"]["embed_rebuild_generation"] == (
            "competing-request" if edit == "request" else generation
        )
        if edit == "model":
            assert data["memory"]["embed_model_id"] == "competing-model"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize("name", ["default", "legacy"])
def test_subprocess_lazy_lesson_backfill_preserves_both_rules(rebuild_home, name):
    test_cross_process_old_vector_cannot_publish_after_rebuild(rebuild_home, "lazy-lesson", name)
    target = rebuild_home.global_store if name == "default" else rebuild_home.late[0]
    rows = target.db.execute(
        "SELECT value_json, embedding FROM semantic_memory WHERE key LIKE 'lesson.%' AND is_deleted = 0"
    ).fetchall()
    assert len(rows) == 2
    assert all(row["embedding"] is None for row in rows)
    assert any("glacier archives" in row["value_json"] for row in rows)
    assert any("deployment health" in row["value_json"] for row in rows)
