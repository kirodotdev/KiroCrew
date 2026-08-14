"""Freeing one session uses a different VERB on each backend.

kiro-cli's terminate evicts the session from the multiplexed process and leaves
the transcript for the caller to unlink. KAS's extension namespace is ``_kiro/``
(no ``.dev``) and it offers no evict-only verb, so its delete disposes the
resident AND removes the persisted record. Reusing the kiro verb against KAS
returns ``-32603``, so the session would never be freed and RSS would grow with
every background task.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp.runtime import AcpRuntime
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
