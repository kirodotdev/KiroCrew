"""Reasoning-effort tests for the LLM pool: how workers apply effort and how the pool sets it.

This file pins how an ``AcpWorker`` applies the requested effort on each backend's channel
(the kiro ``/effort`` command or a config option, downgraded to a lower offered level or
left at the provider default when it cannot land), how ``LLMPool`` hands effort to its
workers and resolves it per workload from config, and that only a pool bound to a config
size key resizes from it. The pool's own mechanics and the ``AcpWorker``'s start live in
``test_llm_pool.py``, the ``CCWorker`` in ``test_llm_pool_ccworker.py``, and idle reaping
and conversation recycling in ``test_llm_pool_lifecycle.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from llm_pool_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    FakeWorker,
    _config_dir_tracks_patched_home,
    _zero_tool_client,
)

from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
)
from kiro_crew.knowledge.llm_pool import (
    DEFAULT_EXTRACTION_EFFORT,
    AcpWorker,
    LLMPool,
    _get_workload_effort,
)


def _mock_effort_client(
    levels: list[str],
    *,
    supports: bool = True,
    claude: bool = False,
    backend: str | None = None,
) -> AsyncMock:
    """An AcpClient double whose BACKEND decides the effort channel.

    ``_apply_effort`` asks ``SessionCapabilities.effort_via_config_option`` and
    ``effort_via_slash_command`` off ``client.backend`` rather than reading the
    private ``_is_claude``, so the double sets the public backend string and the
    capabilities answer for it. The ``claude`` keyword stays, because that is what
    the callers are asserting about.
    """
    client = _zero_tool_client()
    client.is_ready = True
    client._pid = None
    if backend is None:
        backend = ACP_BACKEND_CLAUDE if claude else ACP_BACKEND_KIRO
    client.backend = backend
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
    async def test_applies_codex_effort_under_the_codex_option_id(self, tmp_path):
        """codex-acp spells the effort option ``reasoning_effort``. Writing
        ``effort`` there draws "unknown config option", which the except below the
        push turns into "using provider default" -- so the pool silently runs at
        whatever effort the adapter chose."""
        from kiro_crew.acp.types import ACP_BACKEND_CODEX, effort_config_option_id

        client = _mock_effort_client(["low", "medium", "high"], backend=ACP_BACKEND_CODEX)
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client),
        ):
            worker = AcpWorker(effort="high")
            await worker.start()

        option = effort_config_option_id(ACP_BACKEND_CODEX)
        client.set_config_option.assert_awaited_once_with(option, "high")
        client.send_command.assert_not_awaited()
        assert worker._effective_effort == "high"

    @pytest.mark.asyncio
    async def test_codex_support_precheck_asks_about_the_codex_option(self, tmp_path):
        """The precheck must ask about the id it is about to write; asking about
        ``effort`` on codex answers False and skips a push that would land."""
        from kiro_crew.acp.types import ACP_BACKEND_CODEX, effort_config_option_id

        client = _mock_effort_client(["low", "medium", "high"], backend=ACP_BACKEND_CODEX)
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client),
        ):
            worker = AcpWorker(effort="high")
            await worker.start()

        client.supports_config_option.assert_called_with(effort_config_option_id(ACP_BACKEND_CODEX))

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
    @pytest.mark.parametrize("backend", ["some-harness-nobody-registered"])
    async def test_backend_with_no_effort_channel_sends_nothing_and_warns(
        self, backend, tmp_path, caplog
    ):
        """A harness with neither ``session/set_config_option`` for effort nor the
        kiro-native ``/effort`` command gets nothing sent: the pool says the
        provider default applies instead of sending a command that cannot land."""
        client = _mock_effort_client(["low", "medium", "high"], backend=backend)
        client.spec_zero_tools = True
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client),
            caplog.at_level("WARNING", logger="kiro_crew.knowledge.llm_pool"),
        ):
            worker = AcpWorker(effort="high", acp_backend=backend)
            await worker.start()

        client.send_command.assert_not_awaited()
        client.set_config_option.assert_not_awaited()
        assert worker._effective_effort is None
        assert any(
            "effort=high unsupported on backend=" in r.getMessage()
            and "using provider default" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("supports", "written"), [(True, "medium"), (False, None)], ids=["variants", "none"]
    )
    async def test_opencode_writes_effort_only_on_a_model_with_variants(
        self, supports, written, tmp_path
    ):
        """opencode serves an ``effort`` select only on a model with variants: there
        the highest offered level at or below the request is written, and on a model
        without one nothing is sent."""
        client = _mock_effort_client(
            ["low", "medium"], supports=supports, backend=ACP_BACKEND_OPENCODE
        )
        client.spec_zero_tools = True
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client),
        ):
            worker = AcpWorker(effort="high", acp_backend=ACP_BACKEND_OPENCODE)
            await worker.start()

        client.send_command.assert_not_awaited()
        if written is None:
            client.set_config_option.assert_not_awaited()
        else:
            client.set_config_option.assert_awaited_once_with("effort", written)
        assert worker._effective_effort == written

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("levels", "written"),
        [(["off", "low", "medium", "high", "max"], "high"), (["off"], None)],
        ids=["thinking-model", "off-only"],
    )
    async def test_goose_writes_thinking_effort_only_where_a_level_is_offered(
        self, levels, written, tmp_path
    ):
        """goose's option is per model: a thinking model takes the level under
        ``thinking_effort``, and an option offering ``off`` alone takes nothing."""
        client = _mock_effort_client(levels, backend=ACP_BACKEND_GOOSE)
        client.spec_zero_tools = True
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client),
        ):
            worker = AcpWorker(effort="high", acp_backend=ACP_BACKEND_GOOSE)
            await worker.start()

        client.send_command.assert_not_awaited()
        if written is None:
            client.set_config_option.assert_not_awaited()
        else:
            client.set_config_option.assert_awaited_once_with("thinking_effort", written)
        assert worker._effective_effort == written

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
        fake_worker = AsyncMock()
        # The tier is read per worker construction, so state the answer here
        # rather than depending on whatever this host has configured.
        with (
            patch(
                "kiro_crew.knowledge.llm_pool.AcpWorker", return_value=fake_worker
            ) as worker_type,
            patch("kiro_crew.knowledge.llm_pool._get_sandbox_mode", return_value="auto"),
            patch(
                "kiro_crew.knowledge.llm_pool._get_acp_backend",
                return_value=ACP_BACKEND_KIRO,
            ),
        ):
            result = await pool._create_worker()

        worker_type.assert_called_once_with(
            sandbox_mode="auto", effort="high", acp_backend=ACP_BACKEND_KIRO
        )
        assert result is fake_worker

    @pytest.mark.asyncio
    async def test_default_pool_does_not_pass_effort(self):
        pool = LLMPool(pool_size=1)
        pool._provider_type = "acp"
        fake_worker = AsyncMock()
        # Same reason as the sibling above: state the tier, do not inherit it.
        with (
            patch(
                "kiro_crew.knowledge.llm_pool.AcpWorker", return_value=fake_worker
            ) as worker_type,
            patch("kiro_crew.knowledge.llm_pool._get_sandbox_mode", return_value="auto"),
            patch(
                "kiro_crew.knowledge.llm_pool._get_acp_backend",
                return_value=ACP_BACKEND_KIRO,
            ),
        ):
            await pool._create_worker()

        worker_type.assert_called_once_with(
            sandbox_mode="auto", effort=None, acp_backend=ACP_BACKEND_KIRO
        )

    @pytest.mark.asyncio
    async def test_fetch_sized_pool_ignores_extraction_size_config(self):
        # No config_pool_size_key bound: knowledge.extraction_pool_size must
        # not resize this pool (the fetch pool has exactly one worker).
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

    @pytest.mark.asyncio
    async def test_unbound_pool_ignores_extraction_size_config(self):
        # auto_research_llm_pool (no bound key) is unaffected by the knowledge
        # pool-size config even when the key is explicitly present.
        pool = LLMPool(pool_size=2)
        created: list[FakeWorker] = []

        async def _mock_create():
            worker = FakeWorker()
            created.append(worker)
            return worker

        pool._create_worker = _mock_create  # type: ignore[assignment]
        with patch(
            "kiro_crew.knowledge.llm_pool._read_config",
            return_value={"knowledge": {"extraction_pool_size": 4}},
        ):
            await pool.start()

        assert pool._pool_size == 2
        assert len(created) == 2


class TestLLMPoolEffortResolution:
    """``_get_workload_effort`` — the pure per-workload resolution chain."""

    @pytest.mark.parametrize("explicit", ["low", "medium", "high"])
    def test_explicit_key_wins_over_fallback_and_role(self, explicit):
        # The pin wins, and a background-role pin on the SAME config never
        # moves it: extraction effort is independent of the role policy.
        config = {
            "knowledge": {"extraction_effort": explicit},
            "agent": {"role_efforts": {"background": "high"}},
        }
        assert _get_workload_effort(config, "extraction_effort", "high") == explicit

    @pytest.mark.parametrize("role", ["low", "medium", "high"])
    def test_empty_key_ignores_background_role_lands_on_fallback(self, role):
        # No role-policy leg in the chain: an unset key keeps the historical
        # high regardless of agent.role_efforts.background.
        config = {
            "knowledge": {"extraction_effort": ""},
            "agent": {"role_efforts": {"background": role}},
        }
        assert _get_workload_effort(config, "extraction_effort", "high") == "high"

    def test_empty_key_with_role_unset_lands_on_fallback(self):
        config = {"knowledge": {"extraction_effort": ""}, "agent": {}}
        assert (
            _get_workload_effort(config, "extraction_effort", DEFAULT_EXTRACTION_EFFORT) == "high"
        )

    @pytest.mark.parametrize("bad", ["ultra", 7, True, []])
    def test_invalid_key_lands_on_fallback(self, bad):
        # Garbage in the key never changes cost silently: it logs and lands on
        # the same fallback an absent key would (here the role value is low —
        # still ignored).
        config = {
            "knowledge": {"extraction_effort": bad},
            "agent": {"role_efforts": {"background": "low"}},
        }
        assert _get_workload_effort(config, "extraction_effort", "high") == "high"

    def test_chain_absent_lands_on_fallback(self):
        assert _get_workload_effort({}, "extraction_effort", DEFAULT_EXTRACTION_EFFORT) == "high"
        assert _get_workload_effort({}, "other_key", "") is None

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


class TestAcpWorkerEffortModelGate:
    """The config-option backends gate on supports_config_option, not on the
    served model id (the model heuristic was removed with the per-backend
    effort option ids)."""

    @pytest.mark.asyncio
    async def test_config_option_backend_is_not_model_gated(self, tmp_path):
        # The config-option backends keep their supports_config_option gate; a
        # capable client pushes regardless of the model id heuristic.
        client = _mock_effort_client(["low", "medium", "high"], claude=True)
        client._model = "auto"
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client),
        ):
            worker = AcpWorker(effort="high")
            await worker.start()

        client.set_config_option.assert_awaited_once_with("effort", "high")
