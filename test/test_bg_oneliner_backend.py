"""``run_bg_oneliner`` answers on the backend its caller requires, read off the handle.

A caller that must stay on one backend names it with ``expected_backend``; the check is
made on the session the acquisition handed over, not on the configuration read before
it. ``known_backend`` is the read both that check and the callers share: it keeps
kiro-cli's id (``""``) apart from a backend it cannot read (``None``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.session_handle import AcpSessionHandle, WatchdogSettings
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
from kiro_crew.llm_helpers import BackgroundBackendMismatch, known_backend, run_bg_oneliner


class _Raising:
    @property
    def backend(self) -> str:
        raise RuntimeError("no backend here")


class TestKnownBackend:
    def test_kiro_is_told_from_an_unreadable_backend(self) -> None:
        assert known_backend(SimpleNamespace(backend=ACP_BACKEND_KIRO)) == ""
        assert known_backend(SimpleNamespace()) is None

    def test_a_wrapped_client_and_a_handle_are_read_through(self) -> None:
        wrapper = SimpleNamespace(_client=SimpleNamespace(backend="claude"))
        assert known_backend(wrapper) == "claude"
        assert known_backend(SimpleNamespace(acp_backend="kas")) == "kas"
        handle = SimpleNamespace(provider=SimpleNamespace(_client=SimpleNamespace(backend="codex")))
        assert known_backend(handle) == "codex"

    def test_a_value_that_is_not_a_string_is_unreadable(self) -> None:
        assert known_backend(MagicMock()) is None
        assert known_backend(_Raising()) is None
        assert known_backend(SimpleNamespace(backend=None)) is None


def _handle(acp_backend: str | None) -> MagicMock:
    handle = MagicMock()
    if acp_backend is None:
        del handle.acp_backend
        del handle.backend
        del handle.provider
    else:
        handle.acp_backend = acp_backend
    handle.served_model = ""
    handle.last_prompt_stats = None
    handle.destroy = AsyncMock()
    handle.set_model = AsyncMock()
    handle.prompts: list[str] = []

    async def _prompt(text, *, allow_image=True):
        handle.prompts.append(text)
        yield SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="ok")
        yield SimpleNamespace(kind=EVENT_COMPLETE, text="")

    handle.prompt = _prompt
    del handle.shares_background_conversation
    return handle


def _sessions(handle: MagicMock) -> MagicMock:
    sessions = MagicMock()
    sessions.get_bg_session = AsyncMock(return_value=handle)
    sessions.recycle_background = AsyncMock()
    return sessions


class TestRunBgOnelinerExpectedBackend:
    @pytest.mark.asyncio
    async def test_a_handle_on_another_backend_is_destroyed_unprompted(self) -> None:
        handle = _handle("kas")
        with pytest.raises(BackgroundBackendMismatch):
            await run_bg_oneliner(_sessions(handle), "title this", expected_backend="claude")
        assert handle.prompts == []
        handle.set_model.assert_not_awaited()
        handle.destroy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_handle_whose_backend_cannot_be_read_refuses(self) -> None:
        handle = _handle(None)
        with pytest.raises(BackgroundBackendMismatch):
            await run_bg_oneliner(_sessions(handle), "title this", expected_backend="claude")
        assert handle.prompts == []
        handle.destroy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_caller_that_names_no_backend_sends_on_any_handle(self) -> None:
        handle = _handle(None)
        assert await run_bg_oneliner(_sessions(handle), "title this") == "ok"
        assert len(handle.prompts) == 1

    @pytest.mark.asyncio
    async def test_a_matching_handle_sends_once(self) -> None:
        handle = _handle(ACP_BACKEND_KIRO)
        text = await run_bg_oneliner(
            _sessions(handle), "title this", expected_backend=ACP_BACKEND_KIRO
        )
        assert text == "ok"
        assert len(handle.prompts) == 1
        handle.destroy.assert_awaited_once()


def _real_handle(backend: str) -> AcpSessionHandle:
    runtime = MagicMock()
    runtime.acp_backend = backend
    runtime.is_alive.return_value = True
    runtime.send_request = AsyncMock(return_value=1)
    runtime.send_notification = AsyncMock()
    handle = AcpSessionHandle(
        session_id="sess-bg",
        queue=asyncio.Queue(),
        runtime=runtime,
        watchdog=WatchdogSettings(),
    )
    prompts: list[str] = []

    async def _prompt(text, *, allow_image=True):
        prompts.append(text)
        yield SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="ok")
        yield SimpleNamespace(kind=EVENT_COMPLETE, text="")

    setattr(handle, "prompt", _prompt)
    setattr(handle, "destroy", AsyncMock())
    setattr(handle, "sent_prompts", prompts)
    return handle


class TestRunBgOnelinerOnASharedRuntimeHandle:
    def test_a_kiro_runtime_handle_reads_as_kiro(self) -> None:
        assert known_backend(_real_handle(ACP_BACKEND_KIRO)) == ""

    @pytest.mark.asyncio
    async def test_a_kiro_runtime_handle_sends_when_kiro_is_required(self) -> None:
        handle = _real_handle(ACP_BACKEND_KIRO)
        text = await run_bg_oneliner(
            _sessions(handle), "title this", expected_backend=ACP_BACKEND_KIRO
        )
        assert text == "ok"
        assert getattr(handle, "sent_prompts") == ["title this"]

    @pytest.mark.asyncio
    async def test_a_claude_runtime_handle_is_destroyed_unprompted_when_kiro_is_required(
        self,
    ) -> None:
        handle = _real_handle("claude")
        with pytest.raises(BackgroundBackendMismatch):
            await run_bg_oneliner(
                _sessions(handle), "title this", expected_backend=ACP_BACKEND_KIRO
            )
        assert getattr(handle, "sent_prompts") == []
        cast(AsyncMock, handle.destroy).assert_awaited_once()
