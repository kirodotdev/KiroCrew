"""Freeing one session uses a different VERB on each backend.

kiro-cli's terminate evicts the session from the multiplexed process and leaves
the transcript for the caller to unlink. KAS's extension namespace is ``_kiro/``
(no ``.dev``) and it offers no evict-only verb, so its delete disposes the
resident AND removes the persisted record. Reusing the kiro verb against KAS
returns ``-32603``, so the session would never be freed and RSS would grow with
every background task.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.acp.runtime import (
    _TERMINATE_TIMEOUT,
    AcpRequestTimeout,
    AcpRuntime,
    AcpRuntimeError,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    METHOD_KAS_SESSION_DELETE,
    METHOD_SESSION_TERMINATE,
)


def _runtime(tmp_path, backend: str) -> AcpRuntime:
    return AcpRuntime(
        work_dir=tmp_path / f"ws-{backend or 'kiro'}",
        sandbox_mode="off",
        acp_backend=backend,
    )


class TestTeardownVerbPerBackend:
    def test_kas_uses_the_kiro_slash_delete_verb(self, tmp_path):
        assert _runtime(tmp_path, ACP_BACKEND_KAS)._session_teardown_method() == (
            METHOD_KAS_SESSION_DELETE
        )

    def test_kiro_still_uses_terminate(self, tmp_path):
        assert _runtime(tmp_path, "")._session_teardown_method() == METHOD_SESSION_TERMINATE

    def test_the_two_verbs_are_not_the_same_namespace(self):
        """A regression here would send the wrong extension namespace."""
        assert METHOD_KAS_SESSION_DELETE == "_kiro/session/delete"
        assert METHOD_SESSION_TERMINATE == "_kiro.dev/session/terminate"


class TestTeardownIsStillBestEffort:
    """Teardown must neither hang nor raise, on either backend.

    The local unregister has to run even when the request cannot be delivered, or
    the reader loop keeps routing frames to an abandoned queue.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("backend", [ACP_BACKEND_KAS, ""])
    async def test_a_dead_runtime_skips_the_round_trip(self, tmp_path, backend):
        runtime = _runtime(tmp_path, backend)
        runtime._dead = True
        # Never spawned, so a round-trip would raise rather than no-op.
        await runtime.terminate_session("sess-1")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("backend", [ACP_BACKEND_KAS, ""])
    async def test_the_queue_is_unregistered_even_when_the_send_fails(
        self, tmp_path, backend, monkeypatch
    ):
        import asyncio

        runtime = _runtime(tmp_path, backend)
        runtime._session_queues["sess-1"] = asyncio.Queue()
        runtime._process = object()  # type: ignore[assignment]

        async def boom(*_a, **_k):
            raise RuntimeError("backend refused")

        monkeypatch.setattr(runtime, "_send_and_await", boom)
        await runtime.terminate_session("sess-1")
        assert "sess-1" not in runtime._session_queues


def _readiness_subject(tmp_path, monkeypatch, wait_mcp_ready):
    """Build one failed KAS session beside an unaffected resident sibling."""
    import kiro_crew.acp.runtime as runtime_mod

    runtime = _runtime(tmp_path, ACP_BACKEND_KAS)
    runtime._process = object()  # type: ignore[assignment]
    failed_queue = asyncio.Queue()
    sibling_queue = asyncio.Queue()
    runtime._session_queues.update(failed=failed_queue, sibling=sibling_queue)
    send = AsyncMock(return_value={})
    monkeypatch.setattr(runtime, "_send_and_await", send)
    monkeypatch.setattr(runtime_mod, "required_managed_servers", lambda *_a, **_k: {"core"})
    monkeypatch.setattr(runtime_mod, "active_custom_agent", lambda *_a, **_k: None)
    handle = SimpleNamespace(session_id="failed", wait_mcp_ready=wait_mcp_ready)
    return runtime, handle, send, sibling_queue


def _assert_readiness_cleanup(runtime, send, sibling_queue, *, resumed):
    assert runtime._session_queues == {"sibling": sibling_queue}
    if resumed:
        send.assert_not_awaited()
    else:
        send.assert_awaited_once_with(
            METHOD_KAS_SESSION_DELETE,
            {"sessionId": "failed"},
            timeout=_TERMINATE_TIMEOUT,
        )


class TestReadinessFailureLifetime:
    """Fresh failures are disposable; resumed KAS history is not."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("resumed", [False, True], ids=["new", "load"])
    @pytest.mark.parametrize(
        "failure_type",
        [AcpRuntimeError, AcpRequestTimeout],
        ids=["readiness-error", "timeout"],
    )
    async def test_failure_cleans_up_for_the_session_lifetime(
        self, tmp_path, monkeypatch, resumed, failure_type
    ):
        failure = failure_type("managed MCP startup failed")
        runtime, handle, send, sibling_queue = _readiness_subject(
            tmp_path,
            monkeypatch,
            AsyncMock(side_effect=failure),
        )
        params = {"sessionId": "failed"} if resumed else {}

        with pytest.raises(failure_type) as caught:
            await runtime._wait_managed_mcp(handle, params, "worker", 30.0, 0)

        assert caught.value is failure
        _assert_readiness_cleanup(runtime, send, sibling_queue, resumed=resumed)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("resumed", [False, True], ids=["new", "load"])
    async def test_cancellation_cleans_up_for_the_session_lifetime(
        self, tmp_path, monkeypatch, resumed
    ):
        entered = asyncio.Event()

        async def wait_mcp_ready(*_a, **_k):
            entered.set()
            await asyncio.wait_for(asyncio.Event().wait(), timeout=10.0)

        runtime, handle, send, sibling_queue = _readiness_subject(
            tmp_path,
            monkeypatch,
            wait_mcp_ready,
        )
        params = {"sessionId": "failed"} if resumed else {}
        task = asyncio.create_task(runtime._wait_managed_mcp(handle, params, "worker", 30.0, 0))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5.0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5.0)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5.0)

        _assert_readiness_cleanup(runtime, send, sibling_queue, resumed=resumed)
