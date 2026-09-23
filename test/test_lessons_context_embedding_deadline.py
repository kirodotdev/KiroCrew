"""Fresh-session lesson ranking must not wait behind the embedding queue indefinitely."""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import MagicMock

import pytest

import kiro_crew.context as context_mod
from kiro_crew.context import ContextBuilder
from kiro_crew.embeddings import PRIORITY_INTERACTIVE, embedding_work
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader
from kiro_crew.vector_memory import VectorMemoryStore


@pytest.mark.asyncio
async def test_slow_v1_query_embeds_fall_back_without_holding_prompt_build(monkeypatch, tmp_path):
    """A queued embed expires while lexical ranking still supplies the lessons block."""
    monkeypatch.setattr(context_mod, "_PROMPT_BUILD_EMBED_TIMEOUT_SECS", 0.05)

    store = VectorMemoryStore(tmp_path / "memory.db")
    store.init()
    store.write_lesson("always prefer orchid deployment checks")
    store.write_lesson("never discard unrelated release evidence")

    # An embed with no carried deadline blocks until the test lets it go, so a
    # build that waits for it never returns and ``wait_for`` below trips. The
    # good path never opens this gate before the build is back.
    no_deadline_release = threading.Event()

    def slow_queued_embed(_text: str, **_kwargs) -> None:
        work = embedding_work.get()
        if work is None:
            no_deadline_release.wait()
            return None
        while not work.expired():
            time.sleep(0.005)
        return None

    store.embed_fn = slow_queued_embed
    memory = MagicMock()
    memory._memory_version = 1
    memory.vector_store = store

    def memory_context(**kwargs) -> str:
        store._try_embed(kwargs["query"], PRIORITY_INTERACTIVE)
        return ""

    memory.get_context.side_effect = memory_context
    memory.activity_index.return_value = ""
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "workspace"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path),
    )
    monkeypatch.setattr(builder, "get_memory_for", lambda *_args, **_kwargs: memory)

    ticks = 0
    done = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    ticker_task = asyncio.create_task(ticker())
    try:
        rendered, _ = await asyncio.wait_for(
            run_in_embed_pool(
                builder.build_message,
                "orchid deployment",
                True,
            ),
            timeout=1.0,
        )
    finally:
        done.set()
        no_deadline_release.set()
        await ticker_task
        store.close()

    # The ticker is scheduled before the build is awaited, so the loop runs it
    # the moment the build yields: one tick proves the build left the loop
    # free, whatever the runner's scheduling; a build that ran inline records
    # none. Any higher count would be a wall-clock claim.
    assert ticks >= 1
    assert rendered.index("always prefer orchid deployment checks") < rendered.index(
        "never discard unrelated release evidence"
    )
