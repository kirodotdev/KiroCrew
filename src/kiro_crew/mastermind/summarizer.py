"""Writes the tree's summaries in the background.

Each job is one tool-free model call (``summarize``, given the whole prompt,
returning the reply text). A line over ``NODE_BYTES`` is asked again, showing
where it must end, up to ``SUMMARY_TRIES`` times; the shortest is kept. A call
that fails (backend down, signed out) gives the job back to the tree and tries
again after ``SUMMARY_RETRY_SECS``: an outage only delays a summary.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from kiro_crew.mastermind.constants import (
    MAX_PARALLEL_SUMMARIES,
    NODE_BYTES,
    SUMMARY_RETRY_SECS,
    SUMMARY_TRIES,
)
from kiro_crew.mastermind.prompts import SUMMARY_SYSTEM, over_task
from kiro_crew.mastermind.tree import Job, clip, flat, utf8_len
from kiro_crew.mastermind.worker import TreeWorker

logger = logging.getLogger(__name__)

# A reply that starts with a view line's ``id+n|`` copied that from the prompt.
_LINE_REF = re.compile(r"^\s*\d+\+\d+\|\s*")

Summarize = Callable[[str], Awaitable[str]]


class Summarizer:
    """Keeps up to ``parallel`` summary calls running while the tree has jobs."""

    def __init__(
        self,
        tree: TreeWorker,
        summarize: Summarize,
        *,
        parallel: int = MAX_PARALLEL_SUMMARIES,
        retry_secs: float = SUMMARY_RETRY_SECS,
    ) -> None:
        self.tree = tree
        self._summarize = summarize
        self._parallel = parallel
        self._retry_secs = retry_secs
        self._tasks: set[asyncio.Task[None]] = set()
        self._retry: asyncio.TimerHandle | None = None
        self._closed = False

    def kick(self) -> None:
        """Start the jobs the tree has, up to the limit. Call on the gateway loop."""
        if not self._closed:
            self._spawn(self._start())

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _start(self) -> None:
        try:
            jobs = await self.tree.jobs(self._parallel)
        except Exception:  # noqa: BLE001 -- an unreadable tree retries later
            logger.warning("mastermind: could not read the tree's jobs", exc_info=True)
            self._arm_retry()
            return
        for job in jobs:
            if self._closed:
                await self.tree.release(job.lv, job.i)
                continue
            self._spawn(self._run(job))

    async def idle(self) -> None:
        """Wait until no summary is running (tests and shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        if self._retry is not None:
            self._retry.cancel()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _run(self, job: Job) -> None:
        try:
            line = await self._write(job)
            await self.tree.finish(job.lv, job.i, line)
        except asyncio.CancelledError:
            await asyncio.shield(self.tree.release(job.lv, job.i))
            raise
        except Exception as exc:  # noqa: BLE001 -- any failure waits and retries
            logger.warning(
                "mastermind: summary %d/%d failed, retrying later: %s", job.lv, job.i, exc
            )
            await self.tree.release(job.lv, job.i)
            self._arm_retry()
            return
        self.kick()

    async def _write(self, job: Job) -> str:
        """The shortest line the model wrote for *job* (clipped if none fits)."""
        lines: list[str] = []
        prompt = SUMMARY_SYSTEM + "\n\n" + job.prompt
        for _ in range(SUMMARY_TRIES):
            reply = _LINE_REF.sub("", flat((await self._summarize(prompt)).strip()))
            if not reply:
                break
            lines.append(reply)
            if utf8_len(reply) <= NODE_BYTES:
                break
            prompt = SUMMARY_SYSTEM + "\n\n" + job.prompt + "\n" + over_task(reply, clip(reply))
        if not lines:
            logger.info("mastermind: summary %d/%d came back empty; clipping", job.lv, job.i)
            return clip(flat(job.source))
        return min(lines, key=utf8_len)

    def _arm_retry(self) -> None:
        if self._retry is not None or self._closed:
            return

        def fire() -> None:
            self._retry = None
            self.kick()

        self._retry = asyncio.get_running_loop().call_later(self._retry_secs, fire)
