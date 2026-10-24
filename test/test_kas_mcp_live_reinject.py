"""A KAS session applies a dashboard MCP config change by re-sending its projection.

The engine registers a session's ``_meta.kiro.customAgents`` batch once, so the
only live apply is ``session/close`` on the resident session, ``session/load`` of
the SAME id with the new batch, and ``session/set_mode`` -- measured in
``test/fixtures/acp_frames/kas/mcp-live-apply-live.jsonl``. These tests pin the
host-side half: the mark a dashboard write leaves, the evict-then-load the next
turn performs, what the new handle inherits, and how a failure converges. No
engine is spawned; the runtime is a fake that records the verbs.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpError
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    METHOD_SESSION_CLOSE,
)

_SID = "sess-live-1"


def _handle(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = dict(
        session_id=_SID,
        active_agent="kirocrew",
        _bound_cwd="/work",
        _crew_agent="kirocrew",
        model="",
        keep_transcript=False,
        memory_mode="persistent",
        child_fidelity_aware=False,
        set_model=AsyncMock(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _runtime(backend: str = ACP_BACKEND_KAS, *, alive: bool = True) -> SimpleNamespace:
    new_handle = _handle(model="")
    kept: list[str] = []
    runtime = SimpleNamespace(
        acp_backend=backend,
        _agent="kirocrew",
        is_alive=lambda: alive,
        evict_session=AsyncMock(),
        load_session=AsyncMock(return_value=new_handle),
        new_handle=new_handle,
        kept_during=kept,
    )

    @contextmanager
    def keep_record_on_teardown(session_id: str) -> Iterator[None]:
        kept.append(f"open:{session_id}")
        try:
            yield
        finally:
            kept.append(f"close:{session_id}")

    runtime.keep_record_on_teardown = keep_record_on_teardown
    return runtime


def _provider(
    runtime: SimpleNamespace, handle: SimpleNamespace | None = None
) -> AcpSessionProvider:
    provider = AcpSessionProvider(handle or _handle(), runtime, session_key="dashboard:abc")  # type: ignore[arg-type]
    # The gateway claim reads real state; it is not what these tests pin.
    provider.reclaim = MagicMock()  # type: ignore[method-assign]
    return provider


class TestTheMark:
    def test_kas_declares_and_takes_the_mark(self) -> None:
        provider = _provider(_runtime())
        assert provider.mcp_config_wire_reinject is True
        assert provider._mcp_reinject_pending is False
        provider.mark_mcp_config_changed()
        assert provider._mcp_reinject_pending is True

    def test_a_non_member_neither_declares_nor_takes_it(self) -> None:
        """kiro-cli watches the file itself; a mark here would make its next turn
        attempt a verb sequence the host was never measured to take."""
        provider = _provider(_runtime(ACP_BACKEND_KIRO))
        assert provider.mcp_config_wire_reinject is False
        provider.mark_mcp_config_changed()
        assert provider._mcp_reinject_pending is False


class TestTheApply:
    @pytest.mark.asyncio
    async def test_nothing_pending_touches_nothing(self) -> None:
        runtime = _runtime()
        provider = _provider(runtime)
        old = provider._handle
        await provider.apply_pending_mcp_reinjection()
        runtime.evict_session.assert_not_awaited()
        runtime.load_session.assert_not_awaited()
        assert provider._handle is old

    @pytest.mark.asyncio
    async def test_evicts_then_loads_the_same_session_id(self) -> None:
        """Close first, then load the same id: the engine takes a new batch only
        for a session it is not holding."""
        runtime = _runtime()
        provider = _provider(runtime)
        provider.mark_mcp_config_changed()
        await provider.apply_pending_mcp_reinjection()
        runtime.evict_session.assert_awaited_once_with(_SID)
        runtime.load_session.assert_awaited_once()
        args, kwargs = runtime.load_session.await_args
        assert args == ("", _SID)
        assert kwargs["cwd"] == "/work"
        assert kwargs["agent"] == "kirocrew"
        assert kwargs["crew_agent"] == "kirocrew"
        assert kwargs["session_key"] == "dashboard:abc"
        assert kwargs["member_session_key"] == ""
        assert provider._handle is runtime.new_handle
        assert provider._mcp_reinject_pending is False
        provider.reclaim.assert_called_once_with()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_the_load_runs_inside_the_record_keeping_scope(self) -> None:
        """Every failure arm of the load ends the session through terminate_session,
        whose KAS verb deletes the record; the scope turns that into an eviction."""
        runtime = _runtime()
        provider = _provider(runtime)
        provider.mark_mcp_config_changed()

        async def _load(*args: object, **kwargs: object) -> SimpleNamespace:
            assert runtime.kept_during == [f"open:{_SID}"], "load must run inside the scope"
            return runtime.new_handle

        runtime.load_session = AsyncMock(side_effect=_load)
        await provider.apply_pending_mcp_reinjection()
        assert runtime.kept_during == [f"open:{_SID}", f"close:{_SID}"]

    @pytest.mark.asyncio
    async def test_a_mark_landing_during_the_load_survives_it(self) -> None:
        """A second Settings write while the load runs is a change the load did
        not read; its mark must outlive this apply so the next boundary re-loads."""
        runtime = _runtime()
        provider = _provider(runtime)
        provider.mark_mcp_config_changed()

        async def _load(*args: object, **kwargs: object) -> SimpleNamespace:
            assert provider._mcp_reinject_pending is False, "the mark is consumed before the await"
            provider.mark_mcp_config_changed()
            return runtime.new_handle

        runtime.load_session = AsyncMock(side_effect=_load)
        await provider.apply_pending_mcp_reinjection()
        assert provider._handle is runtime.new_handle
        assert provider._mcp_reinject_pending is True

    @pytest.mark.asyncio
    async def test_a_mark_landing_during_the_model_re_apply_survives_it(self) -> None:
        runtime = _runtime()
        provider = _provider(runtime, _handle(model="some-pinned-id"))
        provider.mark_mcp_config_changed()

        async def _set_model(model: str) -> None:
            provider.mark_mcp_config_changed()

        runtime.new_handle.set_model = AsyncMock(side_effect=_set_model)
        await provider.apply_pending_mcp_reinjection()
        assert provider._mcp_reinject_pending is True

    @pytest.mark.asyncio
    async def test_a_cancel_during_the_model_re_apply_leaves_the_new_handle(self) -> None:
        """The swap happens before the model round trip: a cancel there (a turn
        ceiling) must not leave the provider on the evicted handle."""
        import asyncio

        runtime = _runtime()
        provider = _provider(runtime, _handle(model="some-pinned-id"))
        runtime.new_handle.set_model = AsyncMock(side_effect=asyncio.CancelledError())
        provider.mark_mcp_config_changed()
        with pytest.raises(asyncio.CancelledError):
            await provider.apply_pending_mcp_reinjection()
        assert provider._handle is runtime.new_handle
        assert provider._mcp_reinject_pending is False

    @pytest.mark.asyncio
    async def test_a_cancelled_load_restores_the_mark(self) -> None:
        import asyncio

        runtime = _runtime()
        runtime.load_session = AsyncMock(side_effect=asyncio.CancelledError())
        provider = _provider(runtime)
        provider.mark_mcp_config_changed()
        with pytest.raises(asyncio.CancelledError):
            await provider.apply_pending_mcp_reinjection()
        assert provider._mcp_reinject_pending is True

    @pytest.mark.asyncio
    async def test_the_new_handle_inherits_what_the_wire_cannot_restore(self) -> None:
        runtime = _runtime()
        provider = _provider(
            runtime,
            _handle(keep_transcript=True, memory_mode="ephemeral", child_fidelity_aware=True),
        )
        provider.mark_mcp_config_changed()
        await provider.apply_pending_mcp_reinjection()
        assert runtime.new_handle.keep_transcript is True
        assert runtime.new_handle.memory_mode == "ephemeral"
        # This turn's admission opted the OLD handle in; the new one must not fall
        # back to the fail-closed default mid-turn.
        assert runtime.new_handle.child_fidelity_aware is True

    @pytest.mark.asyncio
    async def test_a_pinned_model_is_re_applied(self) -> None:
        runtime = _runtime()
        provider = _provider(runtime, _handle(model="some-pinned-id"))
        provider.mark_mcp_config_changed()
        await provider.apply_pending_mcp_reinjection()
        runtime.new_handle.set_model.assert_awaited_once_with("some-pinned-id")

    @pytest.mark.asyncio
    async def test_the_default_model_is_left_to_the_engine(self) -> None:
        runtime = _runtime()
        provider = _provider(runtime, _handle(model=""))
        provider.mark_mcp_config_changed()
        await provider.apply_pending_mcp_reinjection()
        runtime.new_handle.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_load_keeps_the_mark_and_refuses_the_turn(self) -> None:
        """The change stays pending so the next boundary retries; the turn that
        found the failure is not run on a session the engine may have dropped."""
        runtime = _runtime()
        runtime.load_session = AsyncMock(side_effect=RuntimeError("engine said no"))
        provider = _provider(runtime)
        old = provider._handle
        provider.mark_mcp_config_changed()
        with pytest.raises(AcpError):
            await provider.apply_pending_mcp_reinjection()
        assert provider._mcp_reinject_pending is True
        assert provider._handle is old

    @pytest.mark.asyncio
    async def test_a_dead_runtime_drops_the_mark_without_a_verb(self) -> None:
        """The resident copy died with the process; the next start re-projects."""
        runtime = _runtime(alive=False)
        provider = _provider(runtime)
        provider.mark_mcp_config_changed()
        await provider.apply_pending_mcp_reinjection()
        runtime.evict_session.assert_not_awaited()
        runtime.load_session.assert_not_awaited()
        assert provider._mcp_reinject_pending is False

    @pytest.mark.asyncio
    async def test_a_member_dm_carries_its_member_key_onto_the_load(self, monkeypatch) -> None:
        """A member DM re-mounts its dispatch server on load, as the first load did."""
        import kiro_crew.members as members

        monkeypatch.setattr(members, "is_member_session_key", lambda key: key == "member:m1")
        runtime = _runtime()
        provider = AcpSessionProvider(_handle(), runtime, session_key="member:m1")  # type: ignore[arg-type]
        provider.reclaim = MagicMock()  # type: ignore[method-assign]
        provider.mark_mcp_config_changed()
        await provider.apply_pending_mcp_reinjection()
        assert runtime.load_session.await_args.kwargs["member_session_key"] == "member:m1"


class TestTheTurnBoundary:
    @pytest.mark.asyncio
    async def test_a_refused_apply_releases_the_shared_turn_claim(self, monkeypatch) -> None:
        """The apply runs before the turn's own try/finally, so its claim is
        released on the failure path too."""
        runtime = _runtime()
        runtime.load_session = AsyncMock(side_effect=RuntimeError("engine said no"))
        provider = _provider(runtime)
        provider.mark_mcp_config_changed()
        released: list[object] = []
        monkeypatch.setattr(provider, "_claim_shared_turn", lambda: "claim-1")

        async def _end(claim: object) -> None:
            released.append(claim)

        monkeypatch.setattr(provider, "_end_shared_turn", _end)
        with pytest.raises(AcpError):
            async for _ in provider.stream("hello"):
                pass
        assert released == ["claim-1"]


class TestKeepRecordOnTeardown:
    @staticmethod
    def _runtime() -> object:
        from kiro_crew.acp.runtime_class import AcpRuntime

        runtime = object.__new__(AcpRuntime)
        runtime._dead = False
        runtime._process = object()
        # The harness resolves from the backend id; KAS's teardown verb is the
        # record-deleting one this scope exists to avoid.
        runtime._acp_backend = ACP_BACKEND_KAS
        runtime.unregister_session = MagicMock()  # type: ignore[method-assign]
        return runtime

    @pytest.mark.asyncio
    async def test_terminate_inside_the_scope_evicts_instead_of_deleting(self) -> None:
        from kiro_crew.acp.runtime_class import AcpRuntime

        runtime = self._runtime()
        sent: list[str] = []

        async def _send(method: str, params: dict, timeout: float = 0.0) -> dict:
            sent.append(method)
            return {}

        runtime._send_and_await = _send  # type: ignore[attr-defined]
        with AcpRuntime.keep_record_on_teardown(runtime, _SID):
            await AcpRuntime.terminate_session(runtime, _SID)
        assert sent == [METHOD_SESSION_CLOSE]
        runtime.unregister_session.assert_called_once_with(_SID)  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_terminate_outside_the_scope_uses_the_harness_verb(self) -> None:
        from kiro_crew.acp.runtime_class import AcpRuntime

        runtime = self._runtime()
        sent: list[str] = []

        async def _send(method: str, params: dict, timeout: float = 0.0) -> dict:
            sent.append(method)
            return {}

        runtime._send_and_await = _send  # type: ignore[attr-defined]
        with AcpRuntime.keep_record_on_teardown(runtime, _SID):
            pass
        await AcpRuntime.terminate_session(runtime, _SID)
        assert sent == ["_kiro/session/delete"]

    @pytest.mark.asyncio
    async def test_another_session_is_not_covered_by_the_scope(self) -> None:
        from kiro_crew.acp.runtime_class import AcpRuntime

        runtime = self._runtime()
        sent: list[str] = []

        async def _send(method: str, params: dict, timeout: float = 0.0) -> dict:
            sent.append(method)
            return {}

        runtime._send_and_await = _send  # type: ignore[attr-defined]
        with AcpRuntime.keep_record_on_teardown(runtime, _SID):
            await AcpRuntime.terminate_session(runtime, "sess-other")
        assert sent == ["_kiro/session/delete"]


class TestEvict:
    @pytest.mark.asyncio
    async def test_sends_the_standard_close_and_unregisters(self) -> None:
        """``session/close`` keeps the stored record; KAS's own teardown verb
        (``_kiro/session/delete``) would remove what the load restores."""
        from kiro_crew.acp.runtime_class import AcpRuntime

        runtime = object.__new__(AcpRuntime)
        runtime._dead = False
        runtime._process = object()
        sent: list[tuple[str, dict]] = []

        async def _send(method: str, params: dict, timeout: float = 0.0) -> dict:
            sent.append((method, params))
            return {}

        runtime._send_and_await = _send  # type: ignore[method-assign]
        runtime.unregister_session = MagicMock()  # type: ignore[method-assign]
        await AcpRuntime.evict_session(runtime, _SID)
        assert sent == [(METHOD_SESSION_CLOSE, {"sessionId": _SID})]
        runtime.unregister_session.assert_called_once_with(_SID)

    @pytest.mark.asyncio
    async def test_a_refused_close_still_unregisters(self) -> None:
        from kiro_crew.acp.runtime_class import AcpRuntime

        runtime = object.__new__(AcpRuntime)
        runtime._dead = False
        runtime._process = object()
        runtime._send_and_await = AsyncMock(side_effect=RuntimeError("no such session"))  # type: ignore[method-assign]
        runtime.unregister_session = MagicMock()  # type: ignore[method-assign]
        await AcpRuntime.evict_session(runtime, _SID)
        runtime.unregister_session.assert_called_once_with(_SID)

    @pytest.mark.asyncio
    async def test_a_dead_runtime_sends_nothing(self) -> None:
        from kiro_crew.acp.runtime_class import AcpRuntime

        runtime = object.__new__(AcpRuntime)
        runtime._dead = True
        runtime._process = None
        runtime._send_and_await = AsyncMock()  # type: ignore[method-assign]
        runtime.unregister_session = MagicMock()  # type: ignore[method-assign]
        await AcpRuntime.evict_session(runtime, _SID)
        runtime._send_and_await.assert_not_awaited()
        runtime.unregister_session.assert_called_once_with(_SID)
