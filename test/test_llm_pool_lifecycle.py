"""LLM pool lifecycle tests: idle-TTL scale-to-zero, conversation recycling and URL fetches.

This file pins ``fetch_url_content`` returning a pool's stripped reply and refusing an empty
one, the ``knowledge.pool_idle_ttl_secs`` reader, the reaper that scales a fully idle pool
to zero and the respawn on the next acquire, and the recycling that resets a worker's
conversation once its context or call count crosses the threshold. The pool's own mechanics
live in ``test_llm_pool.py``, reasoning effort in ``test_llm_pool_effort.py``, and the
``CCWorker`` in ``test_llm_pool_ccworker.py``.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest
from llm_pool_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    FakeWorker,
    _config_dir_tracks_patched_home,
    _make_pool_with_fake_workers,
    _zero_tool_client,
)

from kiro_crew.knowledge.llm_pool import (
    DEFAULT_IDLE_TTL_SECS,
    WORKER_RECYCLE_CALLS,
    WORKER_RECYCLE_PCT,
    AcpWorker,
    LLMPool,
    Worker,
    _get_idle_ttl,
)

# ---------------------------------------------------------------------------
# Tests: fetch_url_content with pool
# ---------------------------------------------------------------------------


class TestFetchUrlContent:
    @pytest.mark.asyncio
    async def test_fetch_returns_stripped_content(self):
        from kiro_crew.knowledge.agent_fetch import fetch_url_content

        pool = _make_pool_with_fake_workers(
            pool_size=1,
            responses=[
                "  This is a document with enough content to pass the minimum length validation check.  "
            ],
        )
        result = await fetch_url_content("https://example.com/doc", pool)
        assert (
            result
            == "This is a document with enough content to pass the minimum length validation check."
        )

    @pytest.mark.asyncio
    async def test_fetch_raises_on_empty(self):
        from kiro_crew.knowledge.agent_fetch import fetch_url_content

        pool = _make_pool_with_fake_workers(pool_size=1, responses=[""])
        with pytest.raises(RuntimeError, match="empty content"):
            await fetch_url_content("https://example.com/doc", pool)

    @pytest.mark.asyncio
    async def test_fetch_raises_on_whitespace_only(self):
        from kiro_crew.knowledge.agent_fetch import fetch_url_content

        pool = _make_pool_with_fake_workers(pool_size=1, responses=["   \n  "])
        with pytest.raises(RuntimeError, match="empty content"):
            await fetch_url_content("https://example.com/doc", pool)


# ---------------------------------------------------------------------------
# Tests: idle-TTL config reader
# ---------------------------------------------------------------------------


class TestIdleTtlConfig:
    """``knowledge.pool_idle_ttl_secs`` reader: default, override, 0-disable,
    and rejection of bad/typed-wrong values back to the default."""

    def test_default_is_300(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_idle_ttl() == DEFAULT_IDLE_TTL_SECS == 300.0

    def test_reads_value_from_config(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"knowledge": {"pool_idle_ttl_secs": 60}}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_idle_ttl() == 60.0

    def test_zero_disables(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"knowledge": {"pool_idle_ttl_secs": 0}}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_idle_ttl() == 0.0

    def test_negative_falls_back_to_default(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"knowledge": {"pool_idle_ttl_secs": -5}}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_idle_ttl() == DEFAULT_IDLE_TTL_SECS

    def test_bool_falls_back_to_default(self, tmp_path):
        # JSON ``true`` is an int subclass in Python; must not read as 1s TTL.
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"knowledge": {"pool_idle_ttl_secs": true}}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_idle_ttl() == DEFAULT_IDLE_TTL_SECS

    def test_string_falls_back_to_default(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"knowledge": {"pool_idle_ttl_secs": "600"}}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_idle_ttl() == DEFAULT_IDLE_TTL_SECS


# ---------------------------------------------------------------------------
# Tests: idle-TTL reaper (scale-to-zero)
# ---------------------------------------------------------------------------


class TestIdleReaper:
    """The reaper scales a fully-idle pool to zero after the TTL and the pool
    transparently respawns on the next acquire."""

    @pytest.mark.asyncio
    async def test_reaps_when_idle_past_ttl(self):
        pool = _make_pool_with_fake_workers(pool_size=3)
        pool._idle_ttl = 0.01
        pool._in_use = 0
        pool._idle_since = time.monotonic() - 1.0  # well past the TTL
        workers = list(pool._workers)

        reaped = await pool._maybe_scale_to_zero()

        assert reaped is True
        assert pool._started is False
        assert pool._workers == []
        assert all(not w.is_alive() for w in workers)  # every worker shut down
        assert pool._idle_since is None
        assert pool._in_use == 0

    @pytest.mark.asyncio
    async def test_no_reap_while_busy(self):
        pool = _make_pool_with_fake_workers(pool_size=3)
        pool._idle_ttl = 0.01
        pool._in_use = 1  # a worker is checked out
        pool._idle_since = None

        reaped = await pool._maybe_scale_to_zero()

        assert reaped is False
        assert pool._started is True
        assert len(pool._workers) == 3

    @pytest.mark.asyncio
    async def test_no_reap_before_ttl(self):
        pool = _make_pool_with_fake_workers(pool_size=2)
        pool._idle_ttl = 100.0
        pool._in_use = 0
        pool._idle_since = time.monotonic()  # just went idle

        reaped = await pool._maybe_scale_to_zero()

        assert reaped is False
        assert pool._started is True
        assert len(pool._workers) == 2

    @pytest.mark.asyncio
    async def test_ttl_zero_never_reaps(self):
        pool = _make_pool_with_fake_workers(pool_size=1)
        pool._idle_ttl = 0.0  # disabled
        pool._in_use = 0
        pool._idle_since = time.monotonic() - 10_000

        reaped = await pool._maybe_scale_to_zero()

        assert reaped is False
        assert pool._started is True

    @pytest.mark.asyncio
    async def test_release_marks_idle_transition(self):
        pool = _make_pool_with_fake_workers(pool_size=2)
        idx, _ = await pool.acquire()
        assert pool._in_use == 1
        assert pool._idle_since is None  # busy → no idle clock

        pool.release(idx)
        assert pool._in_use == 0
        assert pool._idle_since is not None  # idle clock started

    @pytest.mark.asyncio
    async def test_acquire_respawns_after_reap(self, tmp_path):
        # Simulate a pool the reaper already scaled to zero.
        pool = LLMPool(pool_size=2)
        pool._started = False
        pool._workers = []

        created: list[FakeWorker] = []

        async def _fake_create():
            w = FakeWorker(responses=["respawned"])
            w._started = True
            created.append(w)
            return w

        pool._create_worker = _fake_create  # type: ignore[assignment]
        # No config on disk → idle_ttl defaults to 300 (>0) → a reaper is armed.
        with patch("pathlib.Path.home", return_value=tmp_path):
            idx, worker = await pool.acquire()

        assert pool._started is True
        assert isinstance(worker, FakeWorker)
        assert worker.is_alive()
        assert len(created) == 2  # pool respawned to full size
        assert pool._reaper_task is not None

        pool.release(idx)
        await pool.shutdown()  # cancels the armed reaper task
        assert pool._reaper_task is None

    @pytest.mark.asyncio
    async def test_shutdown_cancels_reaper(self, tmp_path):
        pool = LLMPool(pool_size=1)

        async def _fake_create():
            w = FakeWorker()
            w._started = True
            return w

        pool._create_worker = _fake_create  # type: ignore[assignment]
        with patch("pathlib.Path.home", return_value=tmp_path):
            await pool.start()
        assert pool._reaper_task is not None
        task = pool._reaper_task

        await pool.shutdown()

        assert task.cancelled() or task.done()
        assert pool._reaper_task is None
        assert pool._started is False

    @pytest.mark.asyncio
    async def test_shutdown_drains_abandoned_reaping_workers(self):
        # Simulate a reaper that shutdown() cancelled mid-teardown: it left the
        # workers it was shutting down stashed on _reaping_workers. shutdown()
        # must still drain them (review-bot post 3).
        pool = _make_pool_with_fake_workers(pool_size=2)
        abandoned = [FakeWorker(), FakeWorker()]
        for w in abandoned:
            w._started = True
        pool._reaping_workers = abandoned
        live = list(pool._workers)

        await pool.shutdown()

        assert all(not w.is_alive() for w in abandoned)  # abandoned set drained
        assert all(not w.is_alive() for w in live)  # live set drained too
        assert pool._reaping_workers is None
        assert pool._started is False


# ---------------------------------------------------------------------------
# Tests: conversation recycling (billed auto-compaction avoidance)
# ---------------------------------------------------------------------------


class _PctWorker(Worker):
    """Worker whose reported context percentage the test drives directly."""

    def __init__(self, pct: float = 0.0) -> None:
        self.pct = pct
        self.resets = 0
        self.sends = 0

    async def start(self) -> None:
        pass

    async def send_message(self, prompt: str, timeout: float = 60.0) -> str:
        self.sends += 1
        return "ok"

    async def shutdown(self) -> None:
        pass

    def is_alive(self) -> bool:
        return True

    def context_pct(self) -> float:
        return self.pct

    async def reset_conversation(self) -> None:
        self.resets += 1
        self.calls_since_reset = 0


def _pool_with(worker: Worker) -> LLMPool:
    pool = LLMPool(pool_size=1)
    pool._started = True
    pool._provider_type = "test"
    pool._workers.append(worker)
    pool._available.put_nowait(0)
    return pool


class TestWorkerConversationRecycle:
    """The pool must drop a worker's transcript before the backend compacts it.

    Every knowledge prompt is self-contained, so the accumulated transcript buys
    nothing while the backend's own auto-compaction bills a summarization turn
    over all of it.
    """

    @pytest.mark.asyncio
    async def test_recycles_when_context_crosses_threshold(self):
        worker = _PctWorker(pct=WORKER_RECYCLE_PCT)
        pool = _pool_with(worker)

        await pool.send("prompt")

        assert worker.resets == 1
        assert worker.calls_since_reset == 0

    @pytest.mark.asyncio
    async def test_no_recycle_below_threshold(self):
        worker = _PctWorker(pct=WORKER_RECYCLE_PCT - 1)
        pool = _pool_with(worker)

        await pool.send("prompt")

        assert worker.resets == 0
        assert worker.calls_since_reset == 1

    @pytest.mark.asyncio
    async def test_recycles_on_call_count_when_backend_reports_no_pct(self):
        """A 0% reading is indistinguishable from an empty transcript, so the
        call count is the fallback that keeps an untelemetered backend from
        growing without bound."""
        worker = _PctWorker(pct=0.0)
        pool = _pool_with(worker)

        for _ in range(WORKER_RECYCLE_CALLS - 1):
            await pool.send("prompt")
        assert worker.resets == 0

        await pool.send("prompt")
        assert worker.resets == 1
        assert worker.calls_since_reset == 0

    @pytest.mark.asyncio
    async def test_recycle_happens_between_calls_not_mid_call(self):
        """The reset lands while the worker is still checked out, so a second
        caller can never send into a half-reset session."""
        worker = _PctWorker(pct=WORKER_RECYCLE_PCT)
        order: list[str] = []

        original_send = worker.send_message
        original_reset = worker.reset_conversation

        async def _send(prompt: str, timeout: float = 60.0) -> str:
            order.append("send")
            return await original_send(prompt, timeout=timeout)

        async def _reset() -> None:
            order.append("reset")
            await original_reset()

        worker.send_message = _send  # type: ignore[method-assign]
        worker.reset_conversation = _reset  # type: ignore[method-assign]

        pool = _pool_with(worker)
        await pool.send("a")
        await pool.send("b")

        assert order == ["send", "reset", "send", "reset"]

    @pytest.mark.asyncio
    async def test_send_batch_recycles_through_the_same_chokepoint(self):
        worker = _PctWorker(pct=WORKER_RECYCLE_PCT)
        pool = _pool_with(worker)

        await pool.send_batch(["a", "b", "c"])

        assert worker.sends == 3
        assert worker.resets == 3

    @pytest.mark.asyncio
    async def test_failed_reset_still_releases_the_worker(self):
        """A reset failure must not wedge the pool — the worker is released and
        ``acquire()`` replaces it on the next checkout."""
        worker = _PctWorker(pct=WORKER_RECYCLE_PCT)

        async def _boom() -> None:
            raise RuntimeError("respawn failed")

        worker.reset_conversation = _boom  # type: ignore[method-assign]
        pool = _pool_with(worker)

        assert await pool.send("prompt") == "ok"
        # Permit returned and the index is queued again.
        assert pool._available.qsize() == 1
        assert pool._in_use == 0
        assert await pool.send("prompt") == "ok"

    @pytest.mark.asyncio
    async def test_acp_worker_reset_respawns_the_client(self):
        """``AcpWorker.reset_conversation`` must produce a NEW ACP session, not
        reuse the one carrying the transcript."""
        worker = AcpWorker(sandbox_mode="off")
        old_client = AsyncMock()
        old_client.is_process_alive = lambda: True
        worker._client = old_client
        worker.calls_since_reset = 7

        new_client = _zero_tool_client()
        new_client._pid = 4242
        new_client.is_process_alive = lambda: True

        # Patch the sweep shield like the AcpWorker tests in test_llm_pool.py: it is a
        # process-global registry, and a real registration leaked from a test
        # makes _collect_active_pids report a non-empty protected set to whatever
        # else shares this worker.
        with (
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=new_client),
            patch("kiro_crew.knowledge.llm_pool.register_protected_pid"),
            patch("kiro_crew.knowledge.llm_pool.unregister_protected_pid"),
        ):
            await worker.reset_conversation()

        old_client.shutdown.assert_awaited_once()
        assert worker._client is new_client
        new_client.ensure_ready.assert_awaited_once()
        assert worker.calls_since_reset == 0

    @pytest.mark.asyncio
    async def test_acp_worker_context_pct_reads_client_stats(self):
        worker = AcpWorker()
        assert worker.context_pct() == 0.0

        client = AsyncMock()
        client.last_prompt_stats.context_pct = 63.5
        worker._client = client
        assert worker.context_pct() == 63.5
