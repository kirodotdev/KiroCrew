"""Fresh-session lesson ranking must not wait behind the embedding queue indefinitely."""

from __future__ import annotations

import asyncio
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

    def slow_queued_embed(_text: str, **_kwargs) -> None:
        work = embedding_work.get()
        if work is None:
            time.sleep(0.35)
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
    started = time.monotonic()
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
        await ticker_task
        store.close()

    elapsed = time.monotonic() - started
    assert elapsed < 0.2
    assert ticks >= 3
    assert rendered.index("always prefer orchid deployment checks") < rendered.index(
        "never discard unrelated release evidence"
    )
