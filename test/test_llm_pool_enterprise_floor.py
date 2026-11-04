"""The knowledge pool never falls back to Kiro CLI in an enterprise build (RFC D6).

In ``oss`` the pool runs on Kiro CLI whenever the selected backend cannot carry its
zero-tool session (KAS included). In an enterprise build that automatic fallback is
gone: Kiro CLI runs the pool only when it is picked (``agent.acp_backend`` ``"kiro"``)
and the remote ``agent_backend`` policy allows it, and every other case refuses
before any process exists.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from llm_pool_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _zero_tool_client,
)

from kiro_crew.agent_sdk import backends as acp_backends
from kiro_crew.knowledge import llm_pool as llm_pool_mod
from kiro_crew.knowledge.llm_pool import (
    AcpWorker,
    KnowledgeHarnessUnavailable,
    _get_acp_backend,
)

KIRO = acp_backends.ACP_BACKEND_KIRO
KAS = acp_backends.ACP_BACKEND_KAS
OPENCODE = acp_backends.ACP_BACKEND_OPENCODE


@pytest.fixture(autouse=True)
def _registry(_floor_monkeypatch):
    baseline = set(acp_backends._baseline)
    selectable = set(acp_backends._selectable)
    llm_pool_mod._WARNED_UNSAFE_BACKENDS.clear()
    yield
    acp_backends._baseline.clear()
    acp_backends._baseline.update(baseline)
    acp_backends._selectable.clear()
    acp_backends._selectable.update(selectable)


@pytest.fixture
def enterprise(monkeypatch):
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")


def _selectable(monkeypatch, *backends):
    monkeypatch.setattr(acp_backends, "_baseline", set(backends))
    monkeypatch.setattr(acp_backends, "_selectable", set(backends))


CONFIGS = [{}, {"agent": {"acp_backend": "kas"}}, {"agent": {"acp_backend": "codex"}}]


KIRO_PICK = {"agent": {"acp_backend": acp_backends.STORED_BACKEND_KIRO}}


class TestTheBackendChoice:
    @pytest.mark.parametrize("config", CONFIGS, ids=["default", "kas", "codex"])
    def test_enterprise_never_falls_back_even_when_kiro_is_allowed(
        self, enterprise, monkeypatch, config
    ):
        _selectable(monkeypatch, KIRO, KAS, acp_backends.ACP_BACKEND_CODEX)
        with pytest.raises(KnowledgeHarnessUnavailable):
            _get_acp_backend(config)

    def test_enterprise_explicit_kiro_pick_the_policy_allows_runs(self, enterprise, monkeypatch):
        _selectable(monkeypatch, KIRO, KAS)
        assert _get_acp_backend(KIRO_PICK) == KIRO

    def test_enterprise_explicit_kiro_pick_the_policy_denies_refuses(self, enterprise, monkeypatch):
        # The denied pick resolves to the KAS floor, which cannot run the pool.
        _selectable(monkeypatch, KAS)
        with pytest.raises(KnowledgeHarnessUnavailable):
            _get_acp_backend(KIRO_PICK)

    @pytest.mark.parametrize("config", CONFIGS, ids=["default", "kas", "codex"])
    def test_enterprise_with_kiro_denied_refuses(self, enterprise, monkeypatch, config):
        _selectable(monkeypatch, KAS)
        with pytest.raises(KnowledgeHarnessUnavailable) as exc:
            _get_acp_backend(config)
        message = str(exc.value)
        assert message.startswith("knowledge_harness_unavailable:")
        assert "does not fall back to the Kiro CLI harness" in message
        assert "kiro-cli update" in message
        assert "--agent-engine v3" in message
        assert "Nothing runs on another harness" in message

    @pytest.mark.parametrize("config", CONFIGS, ids=["default", "kas", "codex"])
    def test_oss_falls_back_to_kiro_whatever_is_selectable(self, monkeypatch, config):
        _selectable(monkeypatch, KAS)
        assert _get_acp_backend(config) == KIRO


class TestThePoolSpawnsNothingWhenRefused:
    @pytest.mark.asyncio
    async def test_enterprise_denied_worker_starts_no_client(
        self, enterprise, monkeypatch, tmp_path
    ):
        _selectable(monkeypatch, KAS)
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "kiro_crew.knowledge.llm_pool.AcpClient",
                side_effect=AssertionError("no process may start"),
            ),
        ):
            worker = AcpWorker()
            with pytest.raises(KnowledgeHarnessUnavailable):
                await worker.start()
        assert worker._client is None

    @pytest.mark.asyncio
    async def test_enterprise_denied_unconfirmed_spec_does_not_restart_on_kiro(
        self, enterprise, monkeypatch, tmp_path
    ):
        _selectable(monkeypatch, KAS, OPENCODE)
        first = _zero_tool_client()
        first.is_ready = True
        first.backend = OPENCODE
        first.spec_zero_tools = False
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", side_effect=[first]) as mk,
        ):
            worker = AcpWorker(acp_backend=OPENCODE)
            with pytest.raises(KnowledgeHarnessUnavailable):
                await worker.start()
        assert mk.call_count == 1, "a Kiro CLI worker was started after the refusal"
        first.shutdown.assert_awaited_once()
        assert worker._client is None

    @pytest.mark.asyncio
    async def test_enterprise_allowed_unconfirmed_spec_still_does_not_restart_on_kiro(
        self, enterprise, monkeypatch, tmp_path
    ):
        _selectable(monkeypatch, KIRO, KAS, OPENCODE)
        first = _zero_tool_client()
        first.is_ready = True
        first.backend = OPENCODE
        first.spec_zero_tools = False
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", side_effect=[first]) as mk,
        ):
            worker = AcpWorker(acp_backend=OPENCODE)
            with pytest.raises(KnowledgeHarnessUnavailable):
                await worker.start()
        assert mk.call_count == 1
        assert worker._client is None

    @pytest.mark.asyncio
    async def test_oss_unconfirmed_spec_restarts_on_kiro(self, monkeypatch, tmp_path):
        _selectable(monkeypatch, KAS, OPENCODE)
        first = _zero_tool_client()
        first.is_ready = True
        first.backend = OPENCODE
        first.spec_zero_tools = False
        second = _zero_tool_client()
        second.is_ready = True
        second.backend = KIRO
        second.spec_zero_tools = False
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", side_effect=[first, second]) as mk,
        ):
            worker = AcpWorker(acp_backend=OPENCODE)
            await worker.start()
        assert mk.call_args_list[1].kwargs["acp_backend"] == KIRO
        assert worker._client is second

    @pytest.mark.asyncio
    async def test_a_worker_restart_rechecks_the_policy(self, enterprise, monkeypatch, tmp_path):
        # A worker that chose Kiro CLI while it was allowed restarts with that
        # choice; a policy that removed Kiro CLI since then refuses the restart.
        _selectable(monkeypatch, KAS)
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "kiro_crew.knowledge.llm_pool.AcpClient",
                side_effect=AssertionError("no process may start"),
            ),
        ):
            worker = AcpWorker(acp_backend=KIRO)
            with pytest.raises(KnowledgeHarnessUnavailable):
                await worker.start()
        assert worker._client is None

    @pytest.mark.asyncio
    async def test_oss_worker_restart_on_kiro_is_unchanged(self, monkeypatch, tmp_path):
        _selectable(monkeypatch, KAS)
        client = _zero_tool_client()
        client.is_ready = True
        client.backend = KIRO
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", side_effect=[client]) as mk,
        ):
            worker = AcpWorker(acp_backend=KIRO)
            try:
                await worker.start()
            except RuntimeError as exc:
                # The zero-tool spec check may refuse here; only the harness matters.
                assert "knowledge_harness_unavailable" not in str(exc)
        assert mk.call_args_list[0].kwargs["acp_backend"] == KIRO

    @pytest.mark.asyncio
    async def test_enterprise_restart_of_an_allowed_kiro_pick_starts(
        self, enterprise, monkeypatch, tmp_path
    ):
        _selectable(monkeypatch, KIRO, KAS)
        client = _zero_tool_client()
        client.is_ready = True
        client.backend = KIRO
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", side_effect=[client]) as mk,
        ):
            worker = AcpWorker(acp_backend=KIRO)
            try:
                await worker.start()
            except RuntimeError as exc:
                # The zero-tool spec check may refuse here; only the harness matters.
                assert "knowledge_harness_unavailable" not in str(exc)
        assert mk.call_args_list[0].kwargs["acp_backend"] == KIRO
