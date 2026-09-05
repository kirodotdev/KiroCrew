"""Unit tests for the unified LLM pool."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.effort import select_effort_level
from kiro_crew.knowledge.llm_pool import (
    DEFAULT_EXTRACTION_EFFORT,
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
    _get_workload_effort,
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


def _make_pool_with_fake_workers(pool_size: int = 3, responses: list[str] | None = None) -> LLMPool:
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
    chat, honouring ``agent.sandbox`` (default ``"auto"`` — engages OS-level
    isolation and defers to kiro-cli's internal sandbox on macOS when enabled).
    These lock in the shipped behaviour."""

    def test_default_is_auto(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_sandbox_mode() == "auto"

    def test_reads_sandbox_from_config(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"agent": {"sandbox": "off"}}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_sandbox_mode() == "off"

    def test_unparseable_config_defaults_auto(self, tmp_path):
        # A file that isn't valid JSON parses to {} → sandbox UNSET → the
        # intended default "auto" (OS-level isolation engaged).
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text("not json")
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_sandbox_mode() == "auto"

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
        Present-but-invalid fails secure to 'auto'; absent takes 'auto'."""
        assert _get_sandbox_mode({"agent": {"sandbox": "strict"}}) == "strict"
        assert (
            _get_sandbox_mode({"agent": {"sandbox": "nope"}}) == "auto"
        )  # malformed → fail secure
        assert _get_sandbox_mode({}) == "auto"  # unset → intended default


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
        assert _get_sandbox_mode(bad) == "auto"

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
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=mock_client) as mk,
        ):
            worker = AcpWorker()
            await worker.start()
        assert mk.call_args.kwargs["sandbox_mode"] == "off"

    @pytest.mark.asyncio
    async def test_start_defaults_sandbox_to_auto(self, tmp_path):
        # With no config, the sandbox mode defaults to "auto" — engaging
        # OS-level isolation (namespace on Linux, seatbelt on macOS) and
        # automatically deferring to kiro-cli's internal sandbox when enabled.
        mock_client = AsyncMock()
        mock_client.is_ready = True
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=mock_client) as mk,
        ):
            worker = AcpWorker()
            await worker.start()
        assert mk.call_args.kwargs["sandbox_mode"] == "auto"


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
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=fresh),
        ):
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
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=fresh),
        ):
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
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=fresh),
            patch(
                "kiro_crew.knowledge.llm_pool.register_protected_pid", side_effect=registered.append
            ),
            patch(
                "kiro_crew.knowledge.llm_pool.unregister_protected_pid",
                side_effect=unregistered.append,
            ),
        ):
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
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", side_effect=[first, second]),
            patch(
                "kiro_crew.knowledge.llm_pool.register_protected_pid", side_effect=registered.append
            ),
            patch(
                "kiro_crew.knowledge.llm_pool.unregister_protected_pid",
                side_effect=unregistered.append,
            ),
        ):
            worker = AcpWorker()
            await worker.start()  # register 100
            await worker.start()  # stale-drop: unregister 100, then register 200
        assert registered == [100, 200]
        assert unregistered == [100]


def _mock_effort_client(
    levels: list[str], *, supports: bool = True, claude: bool = False
) -> AsyncMock:
    client = AsyncMock()
    client.is_ready = True
    client._pid = None
    client._is_claude = claude
    # A concrete effort-capable model: the kiro path gates /effort on model
    # support, so the default harness must look capable for the push tests.
    # The gate tests below override this ("auto" / delete it).
    client._model = "claude-fable-5"
    client.is_process_alive = lambda: True
    client.supports_config_option = MagicMock(return_value=supports)
    client.get_valid_effort_levels = MagicMock(return_value=levels)
    client.send_command = AsyncMock()
    client.set_config_option = AsyncMock()
    return client


class TestAcpWorkerEffort:
    @pytest.mark.asyncio
    async def test_applies_kiro_effort_after_ready_before_use(self, tmp_path):
        client = _mock_effort_client(["low", "medium", "high"])
        events: list[str] = []
        client.ensure_ready.side_effect = lambda: events.append("ready")
        client.send_command.side_effect = lambda *_args, **_kwargs: events.append("set")
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client),
        ):
            worker = AcpWorker(effort="high")
            await worker.start()

        assert events == ["ready", "set"]
        client.send_command.assert_awaited_once_with("/effort", args={"level": "high"})
        client.set_config_option.assert_not_awaited()
        assert worker._effective_effort == "high"

    @pytest.mark.asyncio
    async def test_applies_claude_effort_via_config_option(self, tmp_path):
        client = _mock_effort_client(["low", "medium", "high"], claude=True)
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client),
        ):
            worker = AcpWorker(effort="high")
            await worker.start()

        client.set_config_option.assert_awaited_once_with("effort", "high")
        client.send_command.assert_not_awaited()
        assert worker._effective_effort == "high"

    @pytest.mark.asyncio
    async def test_reapplies_effort_after_respawn(self, tmp_path):
        first = _mock_effort_client(["low", "medium", "high"])
        second = _mock_effort_client(["low", "medium", "high"])
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", side_effect=[first, second]),
        ):
            worker = AcpWorker(effort="high")
            await worker.start()
            await worker.start()

        first.send_command.assert_awaited_once_with("/effort", args={"level": "high"})
        second.send_command.assert_awaited_once_with("/effort", args={"level": "high"})

    @pytest.mark.asyncio
    async def test_downgrades_to_highest_supported_lower_level(self, tmp_path, caplog):
        client = _mock_effort_client(["low", "medium"])
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client),
        ):
            worker = AcpWorker(effort="high")
            await worker.start()

        client.send_command.assert_awaited_once_with("/effort", args={"level": "medium"})
        assert worker._effective_effort == "medium"
        assert "downgraded" in caplog.text

    @pytest.mark.asyncio
    async def test_uses_provider_default_when_claude_effort_is_unsupported(self, tmp_path, caplog):
        client = _mock_effort_client([], supports=False, claude=True)
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client),
        ):
            worker = AcpWorker(effort="high")
            await worker.start()

        client.set_config_option.assert_not_awaited()
        client.send_command.assert_not_awaited()
        assert worker._effective_effort is None
        assert "unsupported" in caplog.text

    @pytest.mark.asyncio
    async def test_uses_provider_default_when_kiro_effort_command_fails(self, tmp_path, caplog):
        client = _mock_effort_client(["low", "medium", "high"])
        client.send_command.side_effect = RuntimeError("effort command rejected")
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client),
        ):
            worker = AcpWorker(effort="high")
            await worker.start()

        client.send_command.assert_awaited_once_with("/effort", args={"level": "high"})
        assert worker._effective_effort is None
        assert "could not apply effort=high" in caplog.text


class TestLLMPoolEffort:
    @pytest.mark.asyncio
    async def test_passes_effort_to_acp_worker(self):
        pool = LLMPool(pool_size=1, effort="high")
        pool._provider_type = "acp"
        pool._sandbox_mode = "auto"
        fake_worker = AsyncMock()
        with patch(
            "kiro_crew.knowledge.llm_pool.AcpWorker", return_value=fake_worker
        ) as worker_type:
            result = await pool._create_worker()

        worker_type.assert_called_once_with(sandbox_mode="auto", effort="high")
        assert result is fake_worker

    @pytest.mark.asyncio
    async def test_default_pool_does_not_pass_effort(self):
        pool = LLMPool(pool_size=1)
        pool._provider_type = "acp"
        pool._sandbox_mode = "auto"
        fake_worker = AsyncMock()
        with patch(
            "kiro_crew.knowledge.llm_pool.AcpWorker", return_value=fake_worker
        ) as worker_type:
            await pool._create_worker()

        worker_type.assert_called_once_with(sandbox_mode="auto", effort=None)

    @pytest.mark.asyncio
    async def test_fetch_sized_pool_ignores_extraction_size_config(self):
        pool = LLMPool(pool_size=1)
        created: list[FakeWorker] = []

        async def _mock_create():
            worker = FakeWorker()
            created.append(worker)
            return worker

        pool._create_worker = _mock_create  # type: ignore[assignment]
        with patch(
            "kiro_crew.knowledge.llm_pool._read_config",
            return_value={"knowledge": {"extraction_pool_size": 3}},
        ):
            await pool.start()

        assert pool._pool_size == 1
        assert len(created) == 1

    @pytest.mark.asyncio
    async def test_bound_key_resizes_from_config_when_explicit(self):
        pool = LLMPool(pool_size=3, config_pool_size_key="extraction_pool_size")
        created: list[FakeWorker] = []

        async def _mock_create():
            worker = FakeWorker()
            created.append(worker)
            return worker

        pool._create_worker = _mock_create  # type: ignore[assignment]
        with patch(
            "kiro_crew.knowledge.llm_pool._read_config",
            return_value={"knowledge": {"extraction_pool_size": 5}},
        ):
            await pool.start()

        assert pool._pool_size == 5
        assert pool._semaphore._value == 5
        assert len(created) == 5


class TestLLMPoolEffortResolution:
    """``_get_workload_effort`` — the pure per-workload resolution chain."""

    def test_explicit_key_wins_over_role_chain(self):
        config = {
            "knowledge": {"extraction_effort": "low"},
            "agent": {"role_efforts": {"background": "high"}},
        }
        assert _get_workload_effort(config, "extraction_effort", "high") == "low"

    def test_empty_key_falls_through_to_role(self):
        config = {
            "knowledge": {"fetch_effort": ""},
            "agent": {"role_efforts": {"background": "medium"}},
        }
        assert _get_workload_effort(config, "fetch_effort", "") == "medium"

    @pytest.mark.parametrize("bad", ["ultra", 7, True, [], None])
    def test_invalid_key_falls_through_to_role_chain(self, bad):
        # A typo must not silently raise cost: garbage in the key continues
        # down the chain (and lands on the ROLE value here, not the fallback).
        config = {
            "knowledge": {"fetch_effort": bad},
            "agent": {"role_efforts": {"background": "low"}},
        }
        assert _get_workload_effort(config, "fetch_effort", "high") == "low"

    @pytest.mark.parametrize("bad", ["ultra", 7, True])
    def test_non_string_role_effort_is_skipped(self, bad):
        config = {
            "knowledge": {},
            "agent": {"role_efforts": {"background": bad}},
        }
        assert _get_workload_effort(config, "extraction_effort", "high") == "high"
        assert _get_workload_effort(config, "fetch_effort", "") is None

    def test_role_efforts_non_dict_is_skipped(self):
        config = {"knowledge": {}, "agent": {"role_efforts": "auto"}}
        assert _get_workload_effort(config, "extraction_effort", "high") == "high"

    def test_chain_absent_lands_on_fallback(self):
        assert _get_workload_effort({}, "extraction_effort", "high") == "high"
        assert _get_workload_effort({}, "fetch_effort", "") is None

    def test_extraction_low_now_wins(self, tmp_path):
        # Deliberate flip of the old pin: an operator's explicit "low" beats
        # the high last-resort that previously ignored it.
        config = {"knowledge": {"extraction_effort": "low"}}
        assert _get_workload_effort(config, "extraction_effort", "high") == "low"

    @pytest.mark.asyncio
    async def test_pool_resolves_effort_from_config_at_start(self):
        pool = LLMPool(
            pool_size=1,
            effort_key="extraction_effort",
            fallback_effort=DEFAULT_EXTRACTION_EFFORT,
        )
        pool._create_worker = _mock_fake_create()  # type: ignore[assignment]
        with patch(
            "kiro_crew.knowledge.llm_pool._read_config",
            return_value={"knowledge": {"extraction_effort": "low"}},
        ):
            await pool.start()

        assert pool._effort == "low"

    @pytest.mark.asyncio
    async def test_pool_without_effort_key_keeps_constructor_effort(self):
        pool = LLMPool(pool_size=1, effort="medium")
        pool._create_worker = _mock_fake_create()  # type: ignore[assignment]
        with patch(
            "kiro_crew.knowledge.llm_pool._read_config",
            return_value={"knowledge": {"extraction_effort": "low"}},
        ):
            await pool.start()

        assert pool._effort == "medium"


def _mock_fake_create():
    created: list[FakeWorker] = []

    async def _create():
        worker = FakeWorker()
        created.append(worker)
        return worker

    return _create


class TestSelectEffortLevelHelper:
    """The shared ladder arithmetic now living in ``kiro_crew.effort``."""

    def test_exact_match(self):
        assert select_effort_level("high", ["low", "medium", "high"]) == "high"

    def test_descends_to_highest_supported_lower(self):
        assert select_effort_level("high", ["low", "medium"]) == "medium"

    def test_nothing_at_or_below_returns_none(self):
        assert select_effort_level("low", ["medium", "high"]) is None

    def test_empty_supported_returns_requested(self):
        # A lazy backend that advertises nothing gets the requested value so
        # ACP can confirm or reject it.
        assert select_effort_level("high", []) == "high"

    def test_invalid_entries_are_ignored(self):
        assert select_effort_level("high", ["bogus", "medium"]) == "medium"


class TestAcpWorkerEffortModelGate:
    """The kiro backend gates /effort on model support before pushing."""

    @pytest.mark.asyncio
    async def test_auto_model_skips_push_with_info_log(self, tmp_path, caplog):
        # "auto" is the stock default model and does not support effort; the
        # worker must neither warn per spawn nor send a command kiro rejects.
        caplog.set_level(logging.INFO, logger="kiro_crew.knowledge.llm_pool")
        client = _mock_effort_client(["low", "medium", "high"])
        client._model = "auto"
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client),
        ):
            worker = AcpWorker(effort="high")
            await worker.start()

        client.send_command.assert_not_awaited()
        client.set_config_option.assert_not_awaited()
        assert worker._effective_effort is None
        gate_records = [r for r in caplog.records if "does not support effort" in r.getMessage()]
        assert gate_records, "expected the model-gate INFO log"
        assert gate_records[-1].levelno == logging.INFO

    @pytest.mark.asyncio
    async def test_capable_model_still_pushes(self, tmp_path):
        client = _mock_effort_client(["low", "medium", "high"])
        client._model = "claude-fable-5"
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client),
        ):
            worker = AcpWorker(effort="high")
            await worker.start()

        client.send_command.assert_awaited_once_with("/effort", args={"level": "high"})
        assert worker._effective_effort == "high"

    @pytest.mark.asyncio
    async def test_missing_model_attr_gates_conservatively(self, tmp_path):
        client = _mock_effort_client(["low", "medium", "high"])
        # No _model attribute at all (older client): treat as unsupported.
        del client._model
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client),
        ):
            worker = AcpWorker(effort="high")
            await worker.start()

        client.send_command.assert_not_awaited()
        assert worker._effective_effort is None

    @pytest.mark.asyncio
    async def test_claude_path_is_not_model_gated(self, tmp_path):
        # The claude backend keeps its supports_config_option gate; a capable
        # config-option client pushes regardless of the model id heuristic.
        client = _mock_effort_client(["low", "medium", "high"], claude=True)
        client._model = "auto"
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client),
        ):
            worker = AcpWorker(effort="high")
            await worker.start()

        client.set_config_option.assert_awaited_once_with("effort", "high")


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

        new_client = AsyncMock()
        new_client._pid = 4242
        new_client.is_process_alive = lambda: True

        # Patch the sweep shield like the other AcpWorker tests: it is a
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


class _ReapProbe:
    """A PIPE-stdio child double that records how it is reaped.

    Mirrors the pin ``test_source_providers`` uses for the same class. A killed
    child blocked writing into a full pipe -- or a surviving descendant still
    holding the pipes open -- makes a bare ``await proc.wait()`` hang the caller
    forever, so the bounded reap must drain via ``communicate()`` and never touch
    ``wait()``.
    """

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode: "int | None" = None
        self.kill_calls = 0
        self.wait_calls = 0
        self.communicate_calls = 0

    async def communicate(self):
        self.communicate_calls += 1
        self.returncode = -9
        return b"", b""

    def kill(self) -> None:
        self.kill_calls += 1

    async def wait(self) -> int:
        self.wait_calls += 1
        return -9


def _patch_tree_kill(monkeypatch, sink):
    """Fake pid + patched tree kill so no test can reach a real killpg.

    Both the async helper (used by ``kill_and_reap``) and the legacy sync entry
    point are patched, so the pin stays safe even when run against unmodified
    code that never calls either.
    """
    from kiro_crew import platform_compat

    async def _fake_tree(pid, sig):
        sink.append((pid, sig))
        return True

    monkeypatch.setattr(platform_compat, "kill_process_tree_async", _fake_tree)
    monkeypatch.setattr(platform_compat, "kill_process_tree", lambda *a, **k: sink.append(a))
    # The child must not look like it shares this process's group, or
    # kill_and_reap correctly skips the tree kill.
    monkeypatch.setattr(platform_compat, "IS_POSIX", True, raising=False)
    monkeypatch.setattr(platform_compat.os, "getpgid", lambda pid: 999_000 + pid, raising=False)


class TestCCWorkerReapsTheProcessTree:
    """``claude -p`` forks helpers, so a pid-scoped kill re-parents them to init.

    ``spine/agent_runner.py``'s ``_terminate_group`` already records this for the
    same argv ("agents kept costing money after Stop"), and
    ``apps/registry.py``'s ``_communicate_with_timeout`` states the rule that a
    caller MUST spawn with ``start_new_session`` for the group signal to land.
    These pin both halves: without the spawn flag the child shares the gateway's
    group and ``kill_and_reap`` deliberately skips its tree kill, so the flag is
    load-bearing rather than cosmetic.
    """

    @pytest.mark.asyncio
    async def test_spawn_requests_its_own_session(self):
        from kiro_crew import platform_compat

        captured: "dict[str, object]" = {}

        async def _fake_spawn(*argv, **kwargs):
            captured.update(kwargs)
            return _ReapProbe()

        worker = CCWorker()
        worker._claude_bin = "/usr/bin/true"
        # `**k` is required, not defensive: _spawn reaches wrap_argv through
        # sandbox.wrap_argv_async, which forwards its sandbox options (always at
        # least `mode`) into the `_prepare` seam. A positional-only stub raises
        # TypeError from inside wrap_argv_async instead of exercising the spawn.
        with (
            patch("kiro_crew.knowledge.llm_pool.wrap_argv", lambda cmd, **k: (cmd, None)),
            patch("kiro_crew.knowledge.llm_pool.cgroup_scope_argv", lambda argv: argv),
            patch(
                "kiro_crew.knowledge.llm_pool.create_subprocess_limited",
                _fake_spawn,
            ),
        ):
            await worker._spawn()
            worker._reader_task.cancel()

        # Both halves of the platform_compat recipe, asserted the way
        # test_source_providers does: the POSIX flag is IS_POSIX (not an
        # unconditional True -- on Windows setsid does not exist and the value is
        # legitimately False), and the Windows flag is what makes the child tree
        # `taskkill /T`-reapable there. Asserting membership separately keeps this
        # load-bearing on Windows too, where `is IS_POSIX` alone would also be
        # satisfied by the kwarg being absent.
        assert "start_new_session" in captured and "creationflags" in captured, (
            "the worker must be spawned with process-group isolation, or its forked "
            "helpers are unreachable as a tree and get re-parented to init on cleanup"
        )
        assert captured["start_new_session"] is platform_compat.IS_POSIX
        assert captured["creationflags"] == platform_compat.CREATE_NEW_PROCESS_GROUP

    @pytest.mark.asyncio
    async def test_shutdown_reaps_the_tree_via_communicate_not_wait(self, monkeypatch):
        tree_kills: "list[tuple]" = []
        _patch_tree_kill(monkeypatch, tree_kills)

        worker = CCWorker()
        proc = _ReapProbe()
        worker._proc = proc

        await worker.shutdown()

        assert tree_kills and tree_kills[0][0] == proc.pid
        assert proc.communicate_calls == 1
        assert proc.wait_calls == 0
        assert worker._proc is None

    @pytest.mark.asyncio
    async def test_reset_conversation_reaps_the_stale_tree(self, monkeypatch):
        tree_kills: "list[tuple]" = []
        _patch_tree_kill(monkeypatch, tree_kills)

        worker = CCWorker()
        old = _ReapProbe(pid=5150)
        worker._proc = old

        replacement = _ReapProbe(pid=5151)

        async def _fake_respawn():
            worker._proc = replacement
            worker._event_queue = asyncio.Queue()

        monkeypatch.setattr(worker, "_spawn", _fake_respawn)

        await worker.reset_conversation()

        assert (
            tree_kills and tree_kills[0][0] == old.pid
        ), "the stale worker's whole tree must be reaped on recycle"
        assert old.communicate_calls == 1
        assert old.wait_calls == 0
        assert replacement.kill_calls == 0
        assert worker.calls_since_reset == 0
