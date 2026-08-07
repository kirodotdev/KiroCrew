"""Unit tests for the unified LLM pool."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.knowledge.llm_pool import (
    DEFAULT_IDLE_TTL_SECS,
    WORKER_RECYCLE_CALLS,
    WORKER_RECYCLE_PCT,
    AcpWorker,
    CCWorker,
    LLMPool,
    Worker,
    _get_idle_ttl,
    _get_provider_type,
    _get_sandbox_mode,
    _read_config,
)


@pytest.fixture(autouse=True)
def _config_dir_tracks_patched_home(monkeypatch):
    """Keep ``llm_pool.config_dir()`` pointed at ``<patched home>/.kirocrew``.

    The data home moved from ``~/.kirocrew`` to ``~/.kiro/crew`` (``config_dir()``),
    and ``_read_config`` now reads ``config_dir()/config.json`` rather than
    ``Path.home()/".kirocrew"/"config.json"``. These tests patch
    ``llm_pool.Path.home`` per-test and write ``config.json`` under
    ``<home>/.kirocrew`` — but ``config_dir()`` reads ``KIROCREW_HOME`` (pinned to
    a *different* tmp dir by the conftest ``_isolate_kirocrew_home`` fixture), so
    without this redirect the config would never be found. Redirect
    ``config_dir`` to ``Path.home()/".kirocrew"`` (evaluated lazily, so it tracks
    whatever ``Path.home()`` each test patches), preserving the existing
    ``.kirocrew/config.json`` layout the tests build.
    """
    monkeypatch.setattr(
        "kiro_crew.knowledge.llm_pool.config_dir", lambda: Path.home() / ".kirocrew"
    )

# ---------------------------------------------------------------------------
# Fixtures — mock workers that don't spawn real processes
# ---------------------------------------------------------------------------


class FakeWorker(Worker):
    """In-memory worker for testing pool mechanics."""

    def __init__(self, responses: list[str] | None = None):
        self._responses = list(responses or ["ok"])
        self._call_count = 0
        self._alive = True
        self._started = False
        self.resets = 0

    async def start(self) -> None:
        self._started = True

    async def send_message(self, prompt: str, timeout: float = 60.0) -> str:
        if not self._alive:
            raise RuntimeError("worker is dead")
        idx = min(self._call_count, len(self._responses) - 1)
        self._call_count += 1
        return self._responses[idx]

    async def shutdown(self) -> None:
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive

    async def reset_conversation(self) -> None:
        self.resets += 1
        self.calls_since_reset = 0


class DeadOnSecondCallWorker(Worker):
    """Dies after first send_message call."""

    def __init__(self) -> None:
        self._alive = True
        self._called = False

    async def start(self) -> None:
        self._alive = True

    async def send_message(self, prompt: str, timeout: float = 60.0) -> str:
        if self._called:
            self._alive = False
            raise RuntimeError("process died")
        self._called = True
        return "first_response"

    async def shutdown(self) -> None:
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive

    async def reset_conversation(self) -> None:
        self.calls_since_reset = 0


def _make_pool_with_fake_workers(
    pool_size: int = 3, responses: list[str] | None = None
) -> LLMPool:
    """Create a pool pre-loaded with FakeWorkers (skips real process spawn)."""
    pool = LLMPool(pool_size=pool_size)
    pool._started = True
    pool._provider_type = "test"
    for i in range(pool_size):
        worker = FakeWorker(responses=responses)
        worker._started = True
        pool._workers.append(worker)
        pool._available.put_nowait(i)
    return pool


# ---------------------------------------------------------------------------
# Tests: Pool basics
# ---------------------------------------------------------------------------


class TestLLMPoolBasics:
    def test_init_defaults(self):
        pool = LLMPool()
        assert pool._pool_size == 3
        assert pool._started is False

    def test_init_custom_size(self):
        pool = LLMPool(pool_size=5)
        assert pool._pool_size == 5

    @pytest.mark.asyncio
    async def test_send_returns_response(self):
        pool = _make_pool_with_fake_workers(pool_size=2, responses=["hello"])
        result = await pool.send("prompt")
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_send_batch_returns_ordered(self):
        pool = _make_pool_with_fake_workers(pool_size=2, responses=["r"])
        results = await pool.send_batch(["a", "b", "c"])
        assert results == ["r", "r", "r"]

    @pytest.mark.asyncio
    async def test_send_batch_empty(self):
        pool = _make_pool_with_fake_workers(pool_size=2)
        results = await pool.send_batch([])
        assert results == []

    @pytest.mark.asyncio
    async def test_shutdown_clears_workers(self):
        pool = _make_pool_with_fake_workers(pool_size=2)
        await pool.shutdown()
        assert pool._workers == []
        assert pool._started is False


# ---------------------------------------------------------------------------
# Tests: Semaphore and concurrency
# ---------------------------------------------------------------------------


class TestLLMPoolConcurrency:
    @pytest.mark.asyncio
    async def test_acquire_release_cycle(self):
        pool = _make_pool_with_fake_workers(pool_size=2)
        idx, worker = await pool.acquire()
        assert isinstance(worker, FakeWorker)
        pool.release(idx)

    @pytest.mark.asyncio
    async def test_semaphore_blocks_when_all_busy(self):
        pool = _make_pool_with_fake_workers(pool_size=1, responses=["slow"])
        # Acquire the only worker
        idx, worker = await pool.acquire()

        # Second acquire should block
        acquired = asyncio.Event()

        async def _try_acquire():
            await pool.acquire()
            acquired.set()

        task = asyncio.create_task(_try_acquire())
        await asyncio.sleep(0.05)
        assert not acquired.is_set()

        # Release unblocks
        pool.release(idx)
        await asyncio.sleep(0.05)
        assert acquired.is_set()
        task.cancel()

    @pytest.mark.asyncio
    async def test_concurrent_sends_bounded_by_pool_size(self):
        """Pool size=2, 4 concurrent sends — max 2 in-flight at any time."""
        in_flight = 0
        max_in_flight = 0

        class CountingWorker(Worker):
            async def start(self) -> None:
                pass

            async def send_message(self, prompt: str, timeout: float = 60.0) -> str:
                nonlocal in_flight, max_in_flight
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                await asyncio.sleep(0.02)
                in_flight -= 1
                return "done"

            async def shutdown(self) -> None:
                pass

            def is_alive(self) -> bool:
                return True

            async def reset_conversation(self) -> None:
                self.calls_since_reset = 0

        pool = LLMPool(pool_size=2)
        pool._started = True
        pool._provider_type = "test"
        for i in range(2):
            pool._workers.append(CountingWorker())
            pool._available.put_nowait(i)

        await pool.send_batch(["a", "b", "c", "d"])
        assert max_in_flight <= 2


# ---------------------------------------------------------------------------
# Tests: Dead worker replacement
# ---------------------------------------------------------------------------


class TestLLMPoolWorkerReplacement:
    @pytest.mark.asyncio
    async def test_dead_worker_gets_replaced(self):
        pool = _make_pool_with_fake_workers(pool_size=1, responses=["alive"])
        # Kill the worker
        fake = pool._workers[0]
        assert isinstance(fake, FakeWorker)
        fake._alive = False

        replacement_created = False

        async def _mock_create_worker():
            nonlocal replacement_created
            replacement_created = True
            w = FakeWorker(responses=["replaced"])
            w._started = True
            return w

        pool._create_worker = _mock_create_worker  # type: ignore[assignment]
        idx, worker = await pool.acquire()
        assert replacement_created
        result = await worker.send_message("test")
        assert result == "replaced"
        pool.release(idx)

    @pytest.mark.asyncio
    async def test_send_with_dead_worker_still_succeeds(self):
        pool = _make_pool_with_fake_workers(pool_size=1, responses=["alive"])
        fake = pool._workers[0]
        assert isinstance(fake, FakeWorker)
        fake._alive = False

        async def _mock_create_worker():
            w = FakeWorker(responses=["recovered"])
            w._started = True
            return w

        pool._create_worker = _mock_create_worker  # type: ignore[assignment]
        result = await pool.send("test")
        assert result == "recovered"


# ---------------------------------------------------------------------------
# Tests: send_batch error handling
# ---------------------------------------------------------------------------


class TestLLMPoolBatchErrors:
    @pytest.mark.asyncio
    async def test_batch_item_failure_returns_empty_string(self):
        class FailOnSecondWorker(Worker):
            def __init__(self) -> None:
                self._count = 0

            async def start(self) -> None:
                pass

            async def send_message(self, prompt: str, timeout: float = 60.0) -> str:
                self._count += 1
                if self._count == 2:
                    raise RuntimeError("boom")
                return f"ok-{self._count}"

            async def shutdown(self) -> None:
                pass

            def is_alive(self) -> bool:
                return True

            async def reset_conversation(self) -> None:
                self.calls_since_reset = 0

        pool = LLMPool(pool_size=1)
        pool._started = True
        pool._provider_type = "test"
        pool._workers.append(FailOnSecondWorker())
        pool._available.put_nowait(0)

        results = await pool.send_batch(["a", "b", "c"])
        # Second item failed, gets ""
        assert results[1] == ""
        # Others succeed (order may vary due to serial with pool_size=1)
        assert "ok" in results[0] or results[0] == ""


# ---------------------------------------------------------------------------
# Tests: Provider detection
# ---------------------------------------------------------------------------


class TestProviderDetection:
    def test_default_is_acp(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_provider_type() == "acp"

    def test_reads_claude_code_from_config(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"agent": {"provider": "claude_code"}}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_provider_type() == "claude_code"

    def test_reads_acp_from_config(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"agent": {"provider": "acp"}}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_provider_type() == "acp"

    def test_handles_malformed_config(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text("not json")
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_provider_type() == "acp"


# ---------------------------------------------------------------------------
# Tests: sandbox mode (knowledge workers honour agent.sandbox; default auto)
# ---------------------------------------------------------------------------


class TestSandboxMode:
    """Knowledge workers (kiro + claude) run under the same OS-level sandbox as
    chat, honouring ``agent.sandbox`` (default ``"off"`` — defers to kiro-cli's
    internal agent sandbox). The earlier hardcoded ``"off"`` bypassed
    least-privilege; these lock in the restored behaviour."""

    def test_default_is_off(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_sandbox_mode() == "off"

    def test_reads_sandbox_from_config(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"agent": {"sandbox": "off"}}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_sandbox_mode() == "off"

    def test_unparseable_config_defaults_off(self, tmp_path):
        # A file that isn't valid JSON parses to {} → sandbox UNSET → the
        # intended default "off" (not a present-but-malformed value).
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text("not json")
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_sandbox_mode() == "off"

    def test_unknown_mode_falls_back_to_auto_fail_secure(self, tmp_path):
        """A PRESENT but unrecognised value is a config error → fail SECURE to
        'auto' (never silently unsandboxed). Distinct from an absent value, which
        takes the intended 'off' default."""
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"agent": {"sandbox": "bogus"}}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_sandbox_mode() == "auto"

    @pytest.mark.parametrize("mode", ["auto", "standard", "strict", "cc", "off"])
    def test_all_valid_modes_pass_through(self, mode, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"agent": {"sandbox": mode}}))
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_sandbox_mode() == mode

    def test_accepts_prereadm_config_dict(self):
        """Pure-parser path: a passed dict is used without touching disk.
        Present-but-invalid fails secure to 'auto'; absent takes 'off'."""
        assert _get_sandbox_mode({"agent": {"sandbox": "strict"}}) == "strict"
        assert _get_sandbox_mode({"agent": {"sandbox": "nope"}}) == "auto"  # malformed → fail secure
        assert _get_sandbox_mode({}) == "off"  # unset → intended default


# ---------------------------------------------------------------------------
# Tests: shared config read (single disk read threaded into pure parsers)
# ---------------------------------------------------------------------------


class TestReadConfig:
    def test_missing_file_returns_empty(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _read_config() == {}

    def test_reads_dict(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"agent": {"provider": "claude_code"}}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _read_config() == {"agent": {"provider": "claude_code"}}

    def test_malformed_returns_empty(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text("not json")
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _read_config() == {}

    def test_non_dict_json_returns_empty(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text("[1, 2, 3]")
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _read_config() == {}

    def test_parsers_accept_config_dict(self):
        """provider parser reads the passed dict, no disk access."""
        data = {
            "agent": {"provider": "claude_code"},
            "knowledge": {},
        }
        assert _get_provider_type(data) == "claude_code"

    @pytest.mark.parametrize(
        "bad",
        [
            {"agent": "acp"},
            {"agent": None},
            {"agent": 42},
            {"knowledge": "claude_code"},
            {"knowledge": None},
            {"agent": ["acp"]},
        ],
    )
    def test_parsers_survive_non_dict_sections(self, bad):
        """A hand-edited config with a non-dict ``agent``/``knowledge`` section
        must not crash the pure parsers — they fall back to defaults, matching
        the no-op-on-malformed-config contract of ``_read_config``."""
        assert _get_provider_type(bad) == "acp"
        assert _get_sandbox_mode(bad) == "off"

    def test_read_config_coerces_non_dict_sections(self, tmp_path):
        """``_read_config`` normalises non-dict ``agent``/``knowledge`` to ``{}``
        so downstream ``.get(...).get(...)`` chains are always dict-safe."""
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"agent": "acp", "knowledge": 7}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            data = _read_config()
        assert data["agent"] == {}
        assert data["knowledge"] == {}

    @pytest.mark.asyncio
    async def test_start_passes_configured_sandbox_to_client(self, tmp_path):
        """AcpWorker.start wires the configured sandbox mode into AcpClient."""
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"agent": {"sandbox": "off"}}')
        mock_client = AsyncMock()
        mock_client.is_ready = True
        with patch("pathlib.Path.home", return_value=tmp_path), \
             patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=mock_client) as mk:
            worker = AcpWorker()
            await worker.start()
        assert mk.call_args.kwargs["sandbox_mode"] == "off"

    @pytest.mark.asyncio
    async def test_start_defaults_sandbox_to_off(self, tmp_path):
        # With no config, the sandbox mode defaults to "off" — deferring
        # isolation to kiro-cli's internal agent sandbox (kiro-cli >= 2.13).
        mock_client = AsyncMock()
        mock_client.is_ready = True
        with patch("pathlib.Path.home", return_value=tmp_path), \
             patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=mock_client) as mk:
            worker = AcpWorker()
            await worker.start()
        assert mk.call_args.kwargs["sandbox_mode"] == "off"


# ---------------------------------------------------------------------------
# Tests: Pool start (mocked workers)
# ---------------------------------------------------------------------------


class TestLLMPoolStart:
    @pytest.mark.asyncio
    async def test_start_creates_workers(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"agent": {"provider": "acp"}}')

        with patch("pathlib.Path.home", return_value=tmp_path):
            pool = LLMPool(pool_size=2)

            # Mock _create_worker to avoid spawning real processes
            workers_created = []

            async def _mock_create():
                w = FakeWorker(responses=["ok"])
                w._started = True
                workers_created.append(w)
                return w

            pool._create_worker = _mock_create  # type: ignore[assignment]
            await pool.start()

        assert pool._started is True
        assert len(pool._workers) == 2
        assert len(workers_created) == 2

    @pytest.mark.asyncio
    async def test_start_idempotent(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"agent": {"provider": "acp"}}')

        with patch("pathlib.Path.home", return_value=tmp_path):
            pool = LLMPool(pool_size=1)
            call_count = 0

            async def _mock_create():
                nonlocal call_count
                call_count += 1
                w = FakeWorker()
                w._started = True
                return w

            pool._create_worker = _mock_create  # type: ignore[assignment]
            await pool.start()
            await pool.start()  # second call should no-op

        assert call_count == 1


# ---------------------------------------------------------------------------
# Tests: Context manager
# ---------------------------------------------------------------------------


class TestLLMPoolContextManager:
    @pytest.mark.asyncio
    async def test_async_context_manager(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"agent": {"provider": "acp"}}')

        with patch("pathlib.Path.home", return_value=tmp_path):
            pool = LLMPool(pool_size=1)

            async def _mock_create():
                w = FakeWorker(responses=["ctx"])
                w._started = True
                return w

            pool._create_worker = _mock_create  # type: ignore[assignment]

            async with pool as p:
                assert p._started is True
                result = await p.send("test")
                assert result == "ctx"

            assert p._started is False


# ---------------------------------------------------------------------------
# Tests: AcpWorker (mocked AcpClient)
# ---------------------------------------------------------------------------


class TestAcpWorker:
    @pytest.mark.asyncio
    async def test_send_message(self):
        mock_client = AsyncMock()
        mock_client.is_ready = True
        mock_client.send_message = AsyncMock(return_value="response")
        mock_client.is_process_alive = lambda: True

        worker = AcpWorker()
        worker._client = mock_client

        result = await worker.send_message("hello", timeout=30.0)
        assert result == "response"
        mock_client.send_message.assert_called_once_with("hello", timeout=30.0)

    @pytest.mark.asyncio
    async def test_is_alive_true(self):
        mock_client = AsyncMock()
        mock_client.is_process_alive = lambda: True

        worker = AcpWorker()
        worker._client = mock_client
        assert worker.is_alive() is True

    @pytest.mark.asyncio
    async def test_is_alive_false_no_client(self):
        worker = AcpWorker()
        assert worker.is_alive() is False

    @pytest.mark.asyncio
    async def test_shutdown(self):
        mock_client = AsyncMock()
        worker = AcpWorker()
        worker._client = mock_client

        await worker.shutdown()
        mock_client.shutdown.assert_called_once()
        assert worker._client is None

    @pytest.mark.asyncio
    async def test_start_shuts_down_stale_client_before_respawn(self, tmp_path):
        """A re-``start`` (e.g. ``send_message`` after a stalled handshake) must
        shut the previous client down before creating a new one, so the prior
        subprocess is not orphaned."""
        stale = AsyncMock()
        fresh = AsyncMock()
        fresh.is_ready = True
        with patch("pathlib.Path.home", return_value=tmp_path), \
             patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=fresh):
            worker = AcpWorker()
            worker._client = stale
            await worker.start()
        stale.shutdown.assert_called_once()
        assert worker._client is fresh

    @pytest.mark.asyncio
    async def test_start_swallows_stale_shutdown_error(self, tmp_path):
        """A failure shutting the stale client down must not abort the respawn."""
        stale = AsyncMock()
        stale.shutdown.side_effect = RuntimeError("boom")
        fresh = AsyncMock()
        fresh.is_ready = True
        with patch("pathlib.Path.home", return_value=tmp_path), \
             patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=fresh):
            worker = AcpWorker()
            worker._client = stale
            await worker.start()
        assert worker._client is fresh

    @pytest.mark.asyncio
    async def test_start_registers_pid_shutdown_unregisters(self, tmp_path):
        """AcpWorker must shield its live kiro-cli PID from the gateway orphan
        sweep (register on start, unregister on shutdown) — otherwise a busy
        knowledge worker is SIGKILLed mid-task as a false orphan ("ACP process
        exited (code=1)")."""
        fresh = AsyncMock()
        fresh.is_ready = True
        fresh._pid = 7777
        registered: list[int] = []
        unregistered: list[int] = []
        with patch("pathlib.Path.home", return_value=tmp_path), \
             patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=fresh), \
             patch("kiro_crew.knowledge.llm_pool.register_protected_pid",
                   side_effect=registered.append), \
             patch("kiro_crew.knowledge.llm_pool.unregister_protected_pid",
                   side_effect=unregistered.append):
            worker = AcpWorker()
            await worker.start()
            assert registered == [7777], "worker did not shield its PID on start"
            await worker.shutdown()
            assert unregistered == [7777], "worker did not release its PID on shutdown"

    @pytest.mark.asyncio
    async def test_respawn_reshields_new_pid(self, tmp_path):
        """A re-``start`` (respawn under a new PID) must release the old PID's
        shield and register the new one, so a dead PID is never left shielded."""
        first = AsyncMock()
        first.is_ready = True
        first._pid = 100
        second = AsyncMock()
        second.is_ready = True
        second._pid = 200
        registered: list[int] = []
        unregistered: list[int] = []
        with patch("pathlib.Path.home", return_value=tmp_path), \
             patch("kiro_crew.knowledge.llm_pool.AcpClient", side_effect=[first, second]), \
             patch("kiro_crew.knowledge.llm_pool.register_protected_pid",
                   side_effect=registered.append), \
             patch("kiro_crew.knowledge.llm_pool.unregister_protected_pid",
                   side_effect=unregistered.append):
            worker = AcpWorker()
            await worker.start()     # register 100
            await worker.start()     # stale-drop: unregister 100, then register 200
        assert registered == [100, 200]
        assert unregistered == [100]


# ---------------------------------------------------------------------------
# Tests: CCWorker (mocked subprocess)
# ---------------------------------------------------------------------------


class TestCCWorker:
    @pytest.mark.asyncio
    async def test_is_alive_no_proc(self):
        worker = CCWorker()
        assert worker.is_alive() is False

    @pytest.mark.asyncio
    async def test_shutdown_no_proc(self):
        worker = CCWorker()
        await worker.shutdown()  # should not raise

    @pytest.mark.asyncio
    async def test_start_raises_without_claude(self):
        with patch("kiro_crew.knowledge.llm_pool.shutil.which", return_value=None):
            worker = CCWorker()
            with pytest.raises(RuntimeError, match="claude CLI not found"):
                await worker.start()


# ---------------------------------------------------------------------------
# Tests: fetch_url_content with pool
# ---------------------------------------------------------------------------


class TestFetchUrlContent:
    @pytest.mark.asyncio
    async def test_fetch_returns_stripped_content(self):
        from kiro_crew.knowledge.agent_fetch import fetch_url_content

        pool = _make_pool_with_fake_workers(pool_size=1, responses=["  This is a document with enough content to pass the minimum length validation check.  "])
        result = await fetch_url_content("https://example.com/doc", pool)
        assert result == "This is a document with enough content to pass the minimum length validation check."

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

        new_client = AsyncMock()
        new_client._pid = 4242
        new_client.is_process_alive = lambda: True

        # Patch the sweep shield like the other AcpWorker tests: it is a
        # process-global registry, and a real registration leaked from a test
        # makes _collect_active_pids report a non-empty protected set to whatever
        # else shares this worker.
        with patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=new_client), \
             patch("kiro_crew.knowledge.llm_pool.register_protected_pid"), \
             patch("kiro_crew.knowledge.llm_pool.unregister_protected_pid"):
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
