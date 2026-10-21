"""The one thread that owns the Mastermind tree.

The tree reads and fsyncs its logs, so none of its work may run on the
gateway's event loop. Every call goes to a single worker thread instead, which
also makes that thread the tree's only owner: the tree itself is not
thread-safe, and nothing else touches it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar

from kiro_crew.mastermind.tree import Job, MemoryTree

T = TypeVar("T")


class TreeWorker:
    """Async access to one :class:`MemoryTree`, built and used on its own thread."""

    def __init__(self, directory: Path, **tree_args: Any) -> None:
        self._directory = directory
        self._tree_args = tree_args
        self._tree: MemoryTree | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mastermind-tree")

    def _get(self) -> MemoryTree:
        if self._tree is None:
            self._tree = MemoryTree(self._directory, **self._tree_args)
        return self._tree

    async def run(self, fn: Callable[[MemoryTree], T]) -> T:
        """Run ``fn(tree)`` on the worker thread."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: fn(self._get()))

    async def jobs(self, limit: int) -> list[Job]:
        return await self.run(lambda t: t.jobs(limit))

    async def finish(self, lv: int, i: int, line: str) -> None:
        await self.run(lambda t: t.finish(lv, i, line))

    async def release(self, lv: int, i: int) -> None:
        await self.run(lambda t: t.release(lv, i))

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
