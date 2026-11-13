"""Only the harness that reads the workspace effort overlay is seeded from it.

``<work_dir>/.kiro/settings/cli.json`` holds ``chat.modelDefaults.<model>.<key>.effort``.
Its scope is the work directory, so every session in one project shares one entry per
model. kiro-cli reads it at spawn. KAS never opens it and takes its level as the
``effortLevel`` config option, so a KAS provider that read the file back at
construction started a brand-new chat at whatever level another chat in the same work
dir had picked last, while the composer still said "Default". A slot keeps its own pick
across a restart through its transcript metadata, which the factory hands back as
``effort_per_model``.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.acp_cli_overlay import _read_cli_overlay, _write_cli_overlay

MODEL = "claude-opus-5.5"
LEVELS = ["low", "medium", "high", "xhigh", "max"]


def _overlay(work_dir: Path) -> Path:
    return work_dir / ".kiro" / "settings" / "cli.json"


def _provider(work_dir: Path, backend: str, **kwargs: object) -> AcpProvider:
    """A provider built as the factory builds one: a real client on *work_dir*."""
    return AcpProvider(acp_backend=backend, work_dir=work_dir, model=MODEL, **kwargs)


def _kas_wire(provider: AcpProvider, *, push_error: Exception | None = None) -> MagicMock:
    """Swap in the KAS client as it stands after ``session/new``: it serves ``effortLevel``."""
    client = MagicMock()
    client.backend = ACP_BACKEND_KAS
    client._model = MODEL
    client._work_dir = provider._client._work_dir
    client.set_config_option = AsyncMock(side_effect=push_error)
    client.send_command = AsyncMock()
    client.supports_config_option = MagicMock(side_effect=lambda cid: cid == "effortLevel")
    client.get_valid_effort_levels = MagicMock(return_value=list(LEVELS))
    provider._client = client
    return client


class TestANewKasChatStartsAtTheModelDefault:
    @pytest.mark.asyncio
    async def test_another_chats_level_in_the_work_dir_is_not_inherited(self, tmp_path):
        # An earlier chat in this project picked max, which landed in the shared file.
        _write_cli_overlay(tmp_path, MODEL, "max")
        # A brand-new chat: no slot pick, no crew pin, no role or global default.
        provider = _provider(tmp_path, ACP_BACKEND_KAS)

        await provider._recover_effort_overlay()
        assert provider._effort_per_model == {}
        assert provider._resolve_effort() is None
        client = _kas_wire(provider)
        await provider._apply_initial_effort()
        # Nothing is pushed, so KAS runs the model's own default.
        client.set_config_option.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_level_the_slot_pinned_still_reaches_the_session(self, tmp_path):
        # A restart rebuilds the provider from the slot's own metadata: the factory
        # hands the restored pick back as ``effort_per_model``.
        _write_cli_overlay(tmp_path, MODEL, "max")
        provider = _provider(tmp_path, ACP_BACKEND_KAS, effort_per_model={MODEL: "xhigh"})

        client = _kas_wire(provider)
        await provider._apply_initial_effort()
        client.set_config_option.assert_awaited_once_with("effortLevel", "xhigh")


class TestKasLeavesTheSharedFileAlone:
    def test_a_kas_session_with_a_level_writes_no_overlay(self, tmp_path):
        provider = _provider(tmp_path, ACP_BACKEND_KAS, effort_per_model={MODEL: "max"})
        assert not _overlay(tmp_path).exists()

        assert provider._apply_effort_overlay() is True
        assert not _overlay(tmp_path).exists()

    @pytest.mark.asyncio
    async def test_a_refused_live_change_rolls_back_without_touching_the_file(self, tmp_path):
        # A level a kiro-cli session in the same project left in the file.
        _write_cli_overlay(tmp_path, MODEL, "high")
        provider = _provider(tmp_path, ACP_BACKEND_KAS)
        _kas_wire(provider, push_error=RuntimeError("effortLevel refused"))

        with patch(
            "kiro_crew.dashboard.chat_persistence.get_reasoning_effort_values",
            return_value=frozenset(LEVELS),
        ):
            with pytest.raises(RuntimeError, match="effortLevel refused"):
                await provider.change_effort("max")

        assert provider._effort_per_model == {}
        assert _read_cli_overlay(tmp_path) == {MODEL: "high"}

    @pytest.mark.asyncio
    async def test_clearing_a_kas_level_asks_for_a_reset_and_leaves_the_file(self, tmp_path):
        _write_cli_overlay(tmp_path, MODEL, "high")
        provider = _provider(tmp_path, ACP_BACKEND_KAS, effort_per_model={MODEL: "max"})
        client = _kas_wire(provider)

        # False asks the handler for a reset; the cold start reads no file.
        assert await provider.clear_effort() is False
        assert provider._effort_per_model == {}
        client.set_config_option.assert_not_awaited()
        assert _read_cli_overlay(tmp_path) == {MODEL: "high"}


class TestKiroCliStillMirrorsTheFileItReads:
    @pytest.mark.asyncio
    async def test_kiro_cli_reads_the_level_back_when_it_starts(self, tmp_path):
        # kiro-cli applies the file's level at spawn, so the map names that level.
        _write_cli_overlay(tmp_path, MODEL, "max")
        provider = _provider(tmp_path, ACP_BACKEND_KIRO)
        # Construction runs on the event loop and opens no file.
        assert provider._effort_per_model == {}

        await provider._recover_effort_overlay()

        assert provider._effort_per_model == {MODEL: "max"}
        assert provider._resolve_effort() == "max"

    @pytest.mark.asyncio
    async def test_a_caller_supplied_level_wins_over_the_file(self, tmp_path):
        _write_cli_overlay(tmp_path, MODEL, "max")
        provider = _provider(tmp_path, ACP_BACKEND_KIRO, effort_per_model={MODEL: "low"})

        await provider._recover_effort_overlay()

        assert provider._effort_per_model == {MODEL: "low"}

    @pytest.mark.asyncio
    async def test_a_restart_does_not_adopt_another_chats_later_pick(self, tmp_path):
        provider = _provider(tmp_path, ACP_BACKEND_KIRO)
        await provider._recover_effort_overlay()
        assert provider._effort_per_model == {}
        # A sibling chat in the same work dir picks a level after this one started.
        _write_cli_overlay(tmp_path, MODEL, "max")

        await provider._recover_effort_overlay()

        assert provider._effort_per_model == {}

    @pytest.mark.asyncio
    async def test_the_recovery_read_runs_off_the_event_loop(self, tmp_path):
        _write_cli_overlay(tmp_path, MODEL, "max")
        provider = _provider(tmp_path, ACP_BACKEND_KIRO)
        loop_thread = threading.get_ident()
        read_on: list[int] = []

        def _spy(work_dir):
            read_on.append(threading.get_ident())
            return _read_cli_overlay(work_dir)

        with patch("kiro_crew.providers.acp_cli_overlay._read_cli_overlay", _spy):
            await provider._recover_effort_overlay()

        assert read_on
        assert loop_thread not in read_on

    def test_construction_writes_no_overlay_and_the_apply_does(self, tmp_path):
        provider = _provider(tmp_path, ACP_BACKEND_KIRO, effort_per_model={MODEL: "low"})
        assert not _overlay(tmp_path).exists()

        assert provider._apply_effort_overlay() is True

        assert _read_cli_overlay(tmp_path) == {MODEL: "low"}
